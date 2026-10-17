"""画师串库的落盘存储、归一化校验与并发写序。

对应 Go：``internal/artist/store.go``（并承接 ``types.go`` 里 Go 挂在类型上的
``Artist.Normalize`` / ``Artist.Validate`` / ``Preset.Normalize`` / ``Preset.Validate``
与 ``validID`` / ``cleanList`` / ``trimmedRunes`` 等无状态助手——``types.py`` 已说明
"只放字段定义、不做校验"，所以校验落在本模块）。

整库放在一个 JSON 文件里，进程内一把互斥锁串行化所有写操作；每次写都走
"临时文件 + 原子替换"，崩溃或磁盘写满都不会留下半截库文件。

Store 不判断权限：它只存它被告知的内容，只做结构不变式；可见性由 service 层
按服务端调用上下文决定。

与 Go 的差异：

* Go 的 ``sync.Mutex`` → :class:`threading.Lock`（本模块读写同步、不 await）。
* Go 的 ``os.ReadFile`` / ``fileops.AtomicWriteFile`` →
  :func:`atri_x.utils.fileops.read_file` / :func:`~atri_x.utils.fileops.atomic_write_file`。
* Go 的哨兵 error → :mod:`atri_x.errors` 的模块级单例异常；带细节的
  :class:`ReferencedError` / :class:`ArtistConflictError` / :class:`ArtistInvalidError`
  在本模块定义，并用 ``is`` / 类名回落与哨兵对应。
* Go 的 ``time.Time`` JSON → RFC3339（见 :mod:`atri_x.storage.times`）；零值时间按
  Go ``encoding/json`` 的实到行为**照常输出** ``0001-01-01T00:00:00Z``
  （``omitempty`` 对结构体类型的 ``time.Time`` 不生效）。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from atri_x.errors import (
    ConflictError,
    ErrDisabled,
    ErrForbidden,
    ErrInvalid,
    ErrLimitExceeded,
    ErrNotFound,
    ForbiddenError,
    InvalidError,
)
from atri_x.storage.times import ZERO_TIME, format_time, parse_optional_time
from atri_x.utils import fileops

from .types import (
    Artist,
    Dialect,
    Library,
    Preset,
    PresetArtist,
    PresetSource,
    PresetStatus,
    Scope,
    normalize_dialect,
    normalize_scope,
    normalize_source,
    normalize_status,
)

#: 库文件自身的 schema 版本。对应 Go ``LibrarySchema``。
LIBRARY_SCHEMA = 1

#: 目录/文件权限（对应 Go ``0o750`` / ``0o600``）。
_DIR_MODE = 0o750
_FILE_MODE = 0o600

#: 容量上限。对应 Go ``types.go`` 的 ``Max*`` 常量。
MAX_ARTISTS = 2000
MAX_PRESETS = 2000
MAX_PRESET_ARTISTS = 32
MAX_ALIASES = 32
MAX_STYLE_TAGS = 32
MAX_TAG_RUNES = 200
MAX_NAME_RUNES = 80
MAX_NOTE_RUNES = 2000
MAX_RAW_STRING_RUNES = 4000
MAX_WEIGHT = 10.0
MIN_WEIGHT = 0.01
MAX_ID_RUNES = 64
MAX_ID_READ_RUNES = 128

#: preset 写入的 revision 约定。对应 Go ``RevisionAny`` / ``RevisionCreate``。
REVISION_ANY = -1
REVISION_CREATE = 0

_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


# ---------------------------------------------------------------------------
# 校验助手（Go types.go 的无状态函数）
# ---------------------------------------------------------------------------
def valid_id(value: str) -> bool:
    """id 只允许字母/数字/下划线/短横/点。对应 Go ``validID``。"""
    text = value or ""
    if text == "" or len(text) > MAX_ID_RUNES:
        return False
    for ch in text:
        if ch not in _ID_CHARS:
            return False
    return text not in (".", "..")


def artist_invalid(field_name: str, reason: str) -> InvalidError:
    """构造带字段名的非法输入异常。对应 Go ``invalid``。"""
    if field_name == "":
        return InvalidError("invalid input: " + reason)
    return InvalidError("invalid " + field_name + ": " + reason)


def _trimmed_runes(value: str, limit: int) -> str:
    text = (value or "").strip()
    if text == "":
        raise artist_invalid("text", "must not be empty")
    if limit > 0 and len(text) > limit:
        raise artist_invalid("text", f"must not exceed {limit} characters")
    return text


def clean_list(values: Any, limit: int, rune_limit: int) -> list[str]:
    """去空白、丢空项、按小写去重并保序，同时卡长度。对应 Go ``cleanList``。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text == "":
            continue
        if rune_limit > 0 and len(text) > rune_limit:
            raise artist_invalid(
                "list", f"item must not exceed {rune_limit} characters"
            )
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    if limit > 0 and len(out) > limit:
        raise artist_invalid("list", f"must not contain more than {limit} items")
    return out


def normalize_artist(item: Artist) -> Artist:
    """裁剪并补默认值。对应 Go ``Artist.Normalize``。"""
    item.id = (item.id or "").strip()
    item.canonical_tag = (item.canonical_tag or "").strip()
    item.note = (item.note or "").strip()
    if item.canonical_tag == "":
        item.canonical_tag = item.id
    try:
        item.aliases = clean_list(item.aliases, MAX_ALIASES, MAX_TAG_RUNES)
    except InvalidError:
        pass
    try:
        item.style_tags = clean_list(item.style_tags, MAX_STYLE_TAGS, MAX_TAG_RUNES)
    except InvalidError:
        pass
    return item


def validate_artist(item: Artist) -> None:
    """对应 Go ``Artist.Validate``。"""
    if not valid_id(item.id):
        raise artist_invalid(
            "artist id",
            "must be 1-64 characters of letters, digits, underscore, dash or dot",
        )
    tag = (item.canonical_tag or "").strip()
    if tag == "":
        raise artist_invalid("canonical_tag", "must not be empty")
    if len(tag) > MAX_TAG_RUNES:
        raise artist_invalid(
            "canonical_tag", f"must not exceed {MAX_TAG_RUNES} characters"
        )
    if len((item.note or "").strip()) > MAX_NOTE_RUNES:
        raise artist_invalid("note", f"must not exceed {MAX_NOTE_RUNES} characters")


def normalize_preset(item: Preset) -> Preset:
    """裁剪并补默认值。对应 Go ``Preset.Normalize``。"""
    item.id = (item.id or "").strip()
    item.name = (item.name or "").strip()
    item.scope_id = (item.scope_id or "").strip()
    item.raw_string = (item.raw_string or "").strip()
    item.style_description = (item.style_description or "").strip()
    item.dialect = normalize_dialect(str(item.dialect or ""))
    item.status = normalize_status(str(item.status or ""))
    if item.source is None:
        item.source = PresetSource()
    item.source.type = normalize_source(str(item.source.type or ""))
    item.source.media_id = (item.source.media_id or "").strip()
    item.source.operator = (item.source.operator or "").strip()
    item.source.note = (item.source.note or "").strip()
    return item


def validate_preset(item: Preset) -> None:
    """只校验形状；scope 可见性由 service/store 负责。对应 Go ``Preset.Validate``。"""
    if not valid_id(item.id):
        raise artist_invalid(
            "preset id",
            "must be 1-64 characters of letters, digits, underscore, dash or dot",
        )
    try:
        _trimmed_runes(item.name, MAX_NAME_RUNES)
    except InvalidError as exc:
        raise artist_invalid("name", str(exc)) from exc
    scope = item.scope
    if not (isinstance(scope, Scope) and scope.valid()):
        raise artist_invalid("scope", "must be user, group or global")
    if scope != Scope.GLOBAL and (item.scope_id or "").strip() == "":
        raise artist_invalid("scope_id", "is required for user and group scopes")
    dialect = item.dialect
    if not (isinstance(dialect, Dialect) and dialect.valid()):
        raise artist_invalid("dialect", "unknown dialect")
    status = item.status
    if not (isinstance(status, PresetStatus) and status.valid()):
        raise artist_invalid("status", "must be approved or pending")
    source_type = item.source.type if item.source is not None else None
    if not (isinstance(source_type, object) and source_type) or not getattr(
        source_type, "valid", lambda: False
    )():
        raise artist_invalid(
            "source.type", "must be text, metadata or visual_recommendation"
        )
    if len(item.raw_string or "") > MAX_RAW_STRING_RUNES:
        raise artist_invalid(
            "raw_string", f"must not exceed {MAX_RAW_STRING_RUNES} characters"
        )
    if len(item.style_description or "") > MAX_NOTE_RUNES:
        raise artist_invalid(
            "style_description", f"must not exceed {MAX_NOTE_RUNES} characters"
        )
    source_note = item.source.note if item.source is not None else ""
    if source_note != "" and len(source_note) > MAX_NOTE_RUNES:
        raise artist_invalid(
            "source.note", f"must not exceed {MAX_NOTE_RUNES} characters"
        )
    slots = item.artists or []
    if len(slots) > MAX_PRESET_ARTISTS:
        raise artist_invalid(
            "artists", f"must not contain more than {MAX_PRESET_ARTISTS} items"
        )
    for index, slot in enumerate(slots):
        if not valid_id(slot.artist_id):
            raise artist_invalid(f"artists[{index}].artist_id", "unknown artist id")
        if slot.weight < MIN_WEIGHT or slot.weight > MAX_WEIGHT:
            raise artist_invalid(
                f"artists[{index}].weight",
                f"must be between {MIN_WEIGHT:g} and {MAX_WEIGHT:g}",
            )
    for field_name, values, limit in (
        ("compatible_models", item.compatible_models, 16),
        ("positive_tags", item.positive_tags, MAX_STYLE_TAGS),
        ("negative_tags", item.negative_tags, MAX_STYLE_TAGS),
    ):
        try:
            clean_list(values, limit, MAX_TAG_RUNES)
        except InvalidError as exc:
            raise artist_invalid(field_name, str(exc)) from exc
    if len(slots) == 0 and (item.raw_string or "") == "" and not item.positive_tags:
        raise artist_invalid(
            "preset", "must carry an artist string, artist slots or positive tags"
        )


# ---------------------------------------------------------------------------
# 异常类型（对应 Go store.go / types.go 的包装 error）
# ---------------------------------------------------------------------------
class ReferencedError(InvalidError):
    """某个画师串仍被 preset 引用，不能删。对应 Go ``ReferencedError``。

    ``errors.Is(err, ErrInvalid)`` 在 Python 侧靠继承
    :class:`atri_x.errors.InvalidError` 表达。
    """

    def __init__(self, artist_id: str, presets: list[str]) -> None:
        self.artist_id = artist_id
        self.presets = list(presets)
        super().__init__(
            f"artist {artist_id} is still used by {len(self.presets)} presets: "
            + ", ".join(self.presets)
        )


class ArtistConflictError(ConflictError):
    """revision 竞争失败。对应 Go ``ConflictError``。"""

    def __init__(
        self, id: str, expected_revision: int, current_revision: int
    ) -> None:
        self.id = id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"preset {id} revision conflict: expected {expected_revision}, "
            f"current {current_revision}"
        )


# ---------------------------------------------------------------------------
# JSON 编解码（Go encoding/json 的字段名与 omitempty 口径）
# ---------------------------------------------------------------------------
def _json_time(value: datetime | None) -> str:
    if value is None:
        return format_time(ZERO_TIME)
    return format_time(value)


def _parse_json_time(raw: Any) -> datetime:
    if not isinstance(raw, str) or raw.strip() == "":
        return ZERO_TIME
    return parse_optional_time(raw) or ZERO_TIME


def _omit(value: Any) -> bool:
    """Go ``omitempty``：空串/0/false/空切片/空 map 视为空。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def artist_to_json(item: Artist) -> dict[str, Any]:
    """对应 Go ``Artist`` 的 struct tag 序列化。"""
    out: dict[str, Any] = {
        "id": item.id,
        "canonical_tag": item.canonical_tag,
        "created_at": _json_time(item.created_at),
        "updated_at": _json_time(item.updated_at),
    }
    if item.aliases:
        out["aliases"] = list(item.aliases)
    if item.style_tags:
        out["style_tags"] = list(item.style_tags)
    if not _omit(item.note):
        out["note"] = item.note
    return out


def preset_to_json(item: Preset) -> dict[str, Any]:
    """对应 Go ``Preset`` 的 struct tag 序列化。"""
    out: dict[str, Any] = {
        "id": item.id,
        "name": item.name,
        "scope": str(item.scope),
        "scope_id": item.scope_id,
        "revision": item.revision,
        "dialect": str(item.dialect),
        "source": _preset_source_to_json(item.source),
        "status": str(item.status),
        "created_at": _json_time(item.created_at),
        "updated_at": _json_time(item.updated_at),
    }
    if item.compatible_models:
        out["compatible_models"] = list(item.compatible_models)
    if item.artists:
        out["artists"] = [
            {"artist_id": slot.artist_id, "weight": slot.weight}
            for slot in item.artists
        ]
    if not _omit(item.raw_string):
        out["raw_string"] = item.raw_string
    if item.positive_tags:
        out["positive_tags"] = list(item.positive_tags)
    if item.negative_tags:
        out["negative_tags"] = list(item.negative_tags)
    if not _omit(item.style_description):
        out["style_description"] = item.style_description
    return out


def _preset_source_to_json(source: PresetSource | None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": str(source.type) if source is not None else ""}
    if source is None:
        return out
    if not _omit(source.media_id):
        out["media_id"] = source.media_id
    if not _omit(source.operator):
        out["operator"] = source.operator
    if not _omit(source.note):
        out["note"] = source.note
    return out


def library_to_json(library: Library) -> dict[str, Any]:
    """对应 Go ``Library`` 的 struct tag 序列化（``artists`` / ``presets`` 不省略）。"""
    return {
        "schema": library.schema,
        "revision": library.revision,
        "updated_at": _json_time(library.updated_at),
        "artists": [artist_to_json(item) for item in library.artists or []],
        "presets": [preset_to_json(item) for item in library.presets or []],
    }


def artist_from_json(raw: Any) -> Artist:
    data = raw if isinstance(raw, dict) else {}
    return Artist(
        id=str(data.get("id") or ""),
        canonical_tag=str(data.get("canonical_tag") or ""),
        aliases=[str(item) for item in data.get("aliases") or []],
        style_tags=[str(item) for item in data.get("style_tags") or []],
        note=str(data.get("note") or ""),
        created_at=_parse_json_time(data.get("created_at")),
        updated_at=_parse_json_time(data.get("updated_at")),
    )


def preset_from_json(raw: Any) -> Preset:
    data = raw if isinstance(raw, dict) else {}
    source = data.get("source")
    source = source if isinstance(source, dict) else {}
    slots: list[PresetArtist] = []
    for item in data.get("artists") or []:
        if not isinstance(item, dict):
            continue
        weight = item.get("weight")
        slots.append(
            PresetArtist(
                artist_id=str(item.get("artist_id") or ""),
                weight=float(weight) if isinstance(weight, (int, float)) else 0.0,
            )
        )
    return Preset(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        scope=normalize_scope(str(data.get("scope") or "")),
        scope_id=str(data.get("scope_id") or ""),
        revision=int(data.get("revision") or 0),
        dialect=normalize_dialect(str(data.get("dialect") or "")),
        compatible_models=[str(item) for item in data.get("compatible_models") or []],
        artists=slots,
        raw_string=str(data.get("raw_string") or ""),
        positive_tags=[str(item) for item in data.get("positive_tags") or []],
        negative_tags=[str(item) for item in data.get("negative_tags") or []],
        style_description=str(data.get("style_description") or ""),
        source=PresetSource(
            type=normalize_source(str(source.get("type") or "")),
            media_id=str(source.get("media_id") or ""),
            operator=str(source.get("operator") or ""),
            note=str(source.get("note") or ""),
        ),
        status=normalize_status(str(data.get("status") or "")),
        created_at=_parse_json_time(data.get("created_at")),
        updated_at=_parse_json_time(data.get("updated_at")),
    )


def library_from_json(raw: Any) -> Library:
    data = raw if isinstance(raw, dict) else {}
    schema = data.get("schema")
    library = Library(
        schema=int(schema) if isinstance(schema, (int, float)) else 0,
        revision=int(data.get("revision") or 0),
        updated_at=_parse_json_time(data.get("updated_at")),
        artists=[artist_from_json(item) for item in data.get("artists") or []],
        presets=[preset_from_json(item) for item in data.get("presets") or []],
    )
    if library.schema <= 0:
        library.schema = LIBRARY_SCHEMA
    library.artists = _normalize_artists(library.artists)
    library.presets = _normalize_presets(library.presets)
    return library


def _normalize_artists(items: list[Artist]) -> list[Artist]:
    out: list[Artist] = []
    seen: set[str] = set()
    for item in items:
        item = normalize_artist(item)
        if not valid_id(item.id) or item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


def _normalize_presets(items: list[Preset]) -> list[Preset]:
    out: list[Preset] = []
    seen: set[str] = set()
    for item in items:
        item = normalize_preset(item)
        if not valid_id(item.id) or item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


def _clone_artists(items: list[Artist]) -> list[Artist]:
    if not items:
        return []
    return [
        replace(item, aliases=list(item.aliases or []), style_tags=list(item.style_tags or []))
        for item in items
    ]


def _clone_presets(items: list[Preset]) -> list[Preset]:
    if not items:
        return []
    return [
        replace(
            item,
            artists=[PresetArtist(slot.artist_id, slot.weight) for slot in item.artists or []],
            compatible_models=list(item.compatible_models or []),
            positive_tags=list(item.positive_tags or []),
            negative_tags=list(item.negative_tags or []),
            source=replace(item.source)
            if item.source is not None
            else PresetSource(),
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
@dataclass
class _State:
    library: Library = field(
        default_factory=lambda: Library(
            schema=LIBRARY_SCHEMA, artists=[], presets=[]
        )
    )
    loaded: bool = False


class Store:
    """文件承载的画师串库。对应 Go ``Store``。"""

    def __init__(self, path: str = "", now: Callable[[], datetime] | None = None) -> None:
        self.path = (path or "").strip()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._mu = threading.Lock()
        self._state = _State()

    # -- 基础信息 ---------------------------------------------------------
    def enabled(self) -> bool:
        """对应 Go ``Store.Enabled``。"""
        return self.path != ""

    def backing_path(self) -> str:
        """对应 Go ``Store.Path``。"""
        return self.path

    def set_clock(self, now: Callable[[], datetime] | None) -> None:
        """替换时间源（测试用）。对应 Go ``Store.SetClock``。"""
        if now is not None:
            self._now = now

    def _timestamp(self) -> datetime:
        value = self._now() if self._now is not None else datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # -- 读 ---------------------------------------------------------------
    def _load_locked(self) -> None:
        """读一次库文件。缺失=空库；存在但解析失败=报错，绝不静默重置。"""
        if self._state.loaded:
            return
        self._state.loaded = True
        self._state.library = Library(schema=LIBRARY_SCHEMA, artists=[], presets=[])
        try:
            raw = fileops.read_file(self.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InvalidError(f"read artist library: {exc}") from exc
        text = raw.text or ""
        if text.strip() == "":
            return
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise InvalidError(f"parse artist library: {exc}") from exc
        self._state.library = library_from_json(data)

    def library(self) -> Library:
        """整库深拷贝。对应 Go ``Store.Library``。"""
        if not self.enabled():
            raise ErrDisabled
        with self._mu:
            self._load_locked()
            source = self._state.library
            return Library(
                schema=source.schema,
                revision=source.revision,
                updated_at=source.updated_at,
                artists=_clone_artists(source.artists or []),
                presets=_clone_presets(source.presets or []),
            )

    def revision(self) -> int:
        """对应 Go ``Store.Revision``。"""
        return self.library().revision

    def artist(self, id: str) -> tuple[Artist, bool]:
        """对应 Go ``Store.Artist``。"""
        library = self.library()
        key = (id or "").strip()
        for item in library.artists or []:
            if item.id == key:
                return item, True
        return Artist(), False

    def preset(self, id: str) -> tuple[Preset, bool]:
        """按 id 取 preset（不看 scope）。对应 Go ``Store.Preset``。"""
        library = self.library()
        key = (id or "").strip()
        for item in library.presets or []:
            if item.id == key:
                return item, True
        return Preset(), False

    def lookup(self, name: str) -> tuple[Artist, bool]:
        """按 canonical_tag / id / alias 查找（大小写不敏感）。对应 Go ``Store.Lookup``。"""
        try:
            library = self.library()
        except InvalidError:
            return Artist(), False
        return lookup_artist(library.artists or [], name)

    def resolver(self) -> Callable[[str], tuple[str, bool]] | None:
        """解析期用的查表函数。对应 Go ``Store.Resolver``。"""
        if not self.enabled():
            return None

        def resolve(name: str) -> tuple[str, bool]:
            item, ok = self.lookup(name)
            if not ok:
                return "", False
            return item.id, True

        return resolve

    # -- 写 ---------------------------------------------------------------
    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path)
        if directory not in ("", "."):
            os.makedirs(directory, mode=_DIR_MODE, exist_ok=True)
        library = self._state.library
        library.schema = LIBRARY_SCHEMA
        library.revision += 1
        library.updated_at = self._timestamp()
        try:
            text = json.dumps(
                library_to_json(library), ensure_ascii=False, indent="  "
            )
        except (TypeError, ValueError) as exc:
            raise InvalidError(f"encode artist library: {exc}") from exc
        try:
            fileops.atomic_write_file(
                self.path, (text + "\n").encode("utf-8"), _FILE_MODE
            )
        except OSError as exc:
            raise InvalidError(f"write artist library: {exc}") from exc

    def _preset_index_locked(self, id: str) -> int:
        for index, item in enumerate(self._state.library.presets or []):
            if item.id == id:
                return index
        return -1

    def _artist_index_locked(self, id: str) -> int:
        for index, item in enumerate(self._state.library.artists or []):
            if item.id == id:
                return index
        return -1

    def put_artist(self, item: Artist) -> Artist:
        """插入或整体替换一个画师条目。对应 Go ``Store.PutArtist``。"""
        if not self.enabled():
            raise ErrDisabled
        item = normalize_artist(item)
        validate_artist(item)
        with self._mu:
            self._load_locked()
            now = self._timestamp()
            index = self._artist_index_locked(item.id)
            artists = self._state.library.artists or []
            if index < 0:
                if len(artists) >= MAX_ARTISTS:
                    raise InvalidError(
                        f"{ErrLimitExceeded}: artist catalog is limited to "
                        f"{MAX_ARTISTS} entries"
                    )
                item.created_at = now
                item.updated_at = now
                artists.append(item)
            else:
                current = artists[index]
                item.created_at = current.created_at
                item.updated_at = now
                artists[index] = item
            self._state.library.artists = artists
            self._save_locked()
            return item

    def delete_artist(self, id: str) -> None:
        """删除一个画师条目；仍被 preset 引用时拒绝。对应 Go ``Store.DeleteArtist``。"""
        if not self.enabled():
            raise ErrDisabled
        key = (id or "").strip()
        with self._mu:
            self._load_locked()
            index = self._artist_index_locked(key)
            if index < 0:
                raise ErrNotFound
            referencing: list[str] = []
            for preset in self._state.library.presets or []:
                for slot in preset.artists or []:
                    if slot.artist_id == key:
                        referencing.append(preset.id)
                        break
            if referencing:
                raise ReferencedError(key, referencing)
            artists = self._state.library.artists or []
            del artists[index]
            self._state.library.artists = artists
            self._save_locked()

    def upsert_preset(self, preset: Preset, expected_revision: int) -> Preset:
        """插入或更新一个 preset。对应 Go ``Store.UpsertPreset``。"""
        if not self.enabled():
            raise ErrDisabled
        preset = normalize_preset(preset)
        validate_preset(preset)
        with self._mu:
            self._load_locked()
            now = self._timestamp()
            presets = self._state.library.presets or []
            index = self._preset_index_locked(preset.id)
            if index < 0:
                if expected_revision > 0:
                    raise ArtistConflictError(preset.id, expected_revision, 0)
                if len(presets) >= MAX_PRESETS:
                    raise InvalidError(
                        f"{ErrLimitExceeded}: preset library is limited to "
                        f"{MAX_PRESETS} entries"
                    )
                preset.revision = 1
                preset.created_at = now
                preset.updated_at = now
                presets.append(preset)
            else:
                current = presets[index]
                if expected_revision >= 0 and expected_revision != current.revision:
                    raise ArtistConflictError(
                        preset.id, expected_revision, current.revision
                    )
                if current.scope != preset.scope or current.scope_id != preset.scope_id:
                    raise ForbiddenError(
                        f"{ErrForbidden}: 画师串组合的 scope 不能修改"
                        f"（{current.scope.label()} → {preset.scope.label()}）"
                    )
                preset.revision = current.revision + 1
                preset.created_at = current.created_at
                preset.updated_at = now
                presets[index] = preset
            self._state.library.presets = presets
            self._save_locked()
            return self._state.library.presets[
                self._preset_index_locked(preset.id)
            ]

    def delete_preset(self, id: str, expected_revision: int) -> Preset:
        """删除一个 preset 并返回被删的条目。对应 Go ``Store.DeletePreset``。"""
        if not self.enabled():
            raise ErrDisabled
        key = (id or "").strip()
        with self._mu:
            self._load_locked()
            index = self._preset_index_locked(key)
            if index < 0:
                raise ErrNotFound
            presets = self._state.library.presets or []
            current = presets[index]
            if expected_revision >= 0 and expected_revision != current.revision:
                raise ArtistConflictError(key, expected_revision, current.revision)
            del presets[index]
            self._state.library.presets = presets
            self._save_locked()
            return current


# 模块级哨兵：与 :mod:`atri_x.errors` 同一批单例，``artist_tools`` 会用 ``is`` 判定。
ErrDisabledSentinel = ErrDisabled
ReferencedSentinel = ErrInvalid


def lookup_artist(items: list[Artist], name: str) -> tuple[Artist, bool]:
    """按 canonical_tag / id / alias 查表。对应 Go ``lookupArtist``。"""
    from .parser import normalize_tag

    key = normalize_tag(name)
    if key == "":
        return Artist(), False
    for item in items:
        if normalize_tag(item.canonical_tag) == key or normalize_tag(item.id) == key:
            return item, True
    for item in items:
        for alias in item.aliases or []:
            if normalize_tag(alias) == key:
                return item, True
    return Artist(), False


__all__ = [name for name in dir() if not name.startswith("_")]
