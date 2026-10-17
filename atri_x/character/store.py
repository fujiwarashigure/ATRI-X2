"""基于目录的角色库：读取、可见性、备份清单。

对应 Go：``internal/character/store.go``（写入部分在
:mod:`atri_x.character.write`，检索在 :mod:`atri_x.character.search`）。

目录布局与上游一致::

    <root>/<id>/character.toml   metadata + image index
    <root>/<id>/profile.md       persona
    <root>/<id>/world.md         background
    <root>/<id>/greeting.md      opening line
    <root>/<id>/examples.md      few-shot examples
    <root>/<id>/image_prompt.md  图片提示词
    <root>/<id>/notes/*.md       free-form notes
    <root>/<id>/images/*         original images

**同步实现**：Go 侧这一包全是阻塞文件 I/O + ``sync.Mutex``，没有 goroutine，因此
Python 侧保持同步方法 + :class:`threading.RLock`（对应 ``sync.Mutex``），不做
async 化。``ctx`` 参数只为取消语义保留（鸭子类型，不 import safecontext）。
"""

from __future__ import annotations

import datetime
import os
import re
import threading
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Go 的 character.ErrNotFound / ErrForbidden / ErrConflict / ErrInvalidID 在本模块
# 以同名再导出，但**定义**在 atri_x.errors（全仓唯一出处），这样
# ``except ErrNotFound`` 与 ``is`` 判定跨模块都成立。
from atri_x.errors import AtriXError, ErrConflict, ErrForbidden, ErrInvalidID, ErrNotFound

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "DOC_ORDER",
    "FILE_NAME",
    "IMAGES_DIR",
    "MAX_DESCRIPTION_RUNES",
    "MAX_DOCS_PER_CHARACTER",
    "MAX_DOC_BYTES",
    "MAX_IMAGES_PER_CHARACTER",
    "MAX_LIST_ENTRIES",
    "MAX_LIST_ENTRY_RUNES",
    "MAX_NAME_RUNES",
    "NOTES_DIR",
    "SCAN_COOLDOWN",
    "AssetManifest",
    "AssetManifestEntry",
    "Character",
    "CharacterError",
    "CharacterManifest",
    "Image",
    "ImageSettings",
    "SearchResult",
    "Store",
    "Viewer",
    "Visibility",
    "VisibilityPrivate",
    "VisibilityPublic",
    "WriteRequest",
    "active_ids",
    "with_active",
]

FILE_NAME = "character.toml"
IMAGES_DIR = "images"
NOTES_DIR = "notes"
#: 扫描冷却（秒）。对应 Go ``scanCooldown = 3 * time.Second``。
SCAN_COOLDOWN = 3.0

#: 用户可写数据的上限。
MAX_DOC_BYTES = 128 << 10
MAX_DOCS_PER_CHARACTER = 64
MAX_IMAGES_PER_CHARACTER = 32
MAX_NAME_RUNES = 128
MAX_DESCRIPTION_RUNES = 500
MAX_LIST_ENTRIES = 20
MAX_LIST_ENTRY_RUNES = 64

#: 文档顺序（也决定 ``Prompt`` 里的段落顺序）。对应 Go ``docOrder``。
DOC_ORDER = ("profile", "world", "greeting", "examples", "image_prompt")

#: ``/character list`` 的默认条数。对应 Go ``DefaultSearchLimit``。
DEFAULT_SEARCH_LIMIT = 5


class CharacterError(AtriXError):
    """角色库的非哨兵错误（参数非法、未配置等）。

    Go 里这些是 ``fmt.Errorf``；哨兵错误用 :mod:`atri_x.errors` 里的
    ``err_not_found`` / ``ErrForbidden`` / ``ErrInvalidID`` 等（见下方再导出）。
    """


#: Go 哨兵错误的交叉检索别名：``character.ErrNotFound`` /
#: ``character.ErrForbidden`` / ``character.ErrConflict`` / ``character.ErrInvalidID``
#: —— 与 :mod:`atri_x.errors` 里的是**同一个对象**（哨兵判定用 ``is``）。
__all__ += ["ErrConflict", "ErrForbidden", "ErrInvalidID", "ErrNotFound"]

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
#: Go ``namePattern = ^[\p{L}\p{N}][\p{L}\p{N}_.-]{0,63}$``：Python 的 ``[^\W_]``
#: 正是「字母或数字」，``[\w.-]`` 正是「字母 / 数字 / 下划线 / . / -」。
_NAME_PATTERN = re.compile(r"^[^\W_][\w.-]{0,63}$")


class Visibility(StrEnum):
    """谁可以读这个角色。对应 Go ``Visibility``。"""

    PUBLIC = "public"
    PRIVATE = "private"


VisibilityPublic = Visibility.PUBLIC
VisibilityPrivate = Visibility.PRIVATE


@dataclass(slots=True)
class Viewer:
    """请求角色的人。对应 Go ``Viewer``。"""

    platform: str = ""
    actor_id: str = ""
    superadmin: bool = False


@dataclass(slots=True)
class Image:
    """一张已入库的角色图片。对应 Go ``Image``。"""

    name: str = ""
    mime_type: str = ""
    media_id: str = ""
    path: str = ""
    size: int = 0
    version: str = ""
    source: str = ""
    created_at: str = ""


@dataclass(slots=True)
class ImageSettings:
    """角色被 ``image_generate`` 使用时的渲染参数。对应 Go ``ImageSettings``。"""

    preset_prompt: str = ""
    negative_prompt: str = ""
    size: str = ""
    quality: str = ""
    references: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Character:
    """一个解析后的角色目录。对应 Go ``Character``。"""

    id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    owner_platform: str = ""
    owner_id: str = ""
    visibility: Visibility = Visibility.PUBLIC
    version: str = ""
    source: str = ""
    created_at: str = ""
    updated_at: str = ""
    dir: str = ""
    images: list[Image] = field(default_factory=list)
    image: ImageSettings = field(default_factory=ImageSettings)
    docs: dict[str, str] = field(default_factory=dict)

    def visible_to(self, viewer: Viewer) -> bool:
        """``viewer`` 是否可读。对应 Go ``Character.VisibleTo``。"""
        if self.visibility != VisibilityPrivate:
            return True
        if viewer.superadmin:
            return True
        return (
            bool(viewer.actor_id)
            and bool(self.owner_id)
            and self.owner_platform == viewer.platform
            and self.owner_id == viewer.actor_id
        )

    def manageable_by(self, viewer: Viewer) -> bool:
        """``viewer`` 是否可写 / 可删。对应 Go ``Character.ManageableBy``。"""
        if viewer.superadmin:
            return True
        if not viewer.actor_id or not self.owner_id:
            return False
        return self.owner_platform == viewer.platform and self.owner_id == viewer.actor_id

    def prompt(self, max_runes: int = 0) -> str:
        """渲染可注入的 persona 块。对应 Go ``Character.Prompt``。"""
        parts: list[str] = ["[角色设定]\n", "id: " + self.id + "\n", "name: " + self.name]
        if self.aliases:
            parts.append("\naliases: " + ", ".join(self.aliases))
        if self.description:
            parts.append("\ndescription: " + self.description)
        if self.tags:
            parts.append("\ntags: " + ", ".join(self.tags))
        for kind in DOC_ORDER:
            content = str(self.docs.get(kind) or "").strip()
            if not content:
                continue
            parts.append("\n\n## " + kind + "\n" + content)
        text = "".join(parts).strip()
        if max_runes > 0 and len(text) > max_runes:
            text = text[:max_runes] + "\n...[角色设定过长，已截断]"
        return text

    def image_prompt(self) -> str:
        """该角色生效的图片预设。对应 Go ``Character.ImagePrompt``。"""
        parts: list[str] = []
        if str(self.image.preset_prompt or "").strip():
            parts.append(str(self.image.preset_prompt).strip())
        doc = str(self.docs.get("image_prompt") or "").strip()
        if doc:
            parts.append(doc)
        return "\n\n".join(parts)


@dataclass(slots=True)
class WriteRequest:
    """创建或更新角色。对应 Go ``WriteRequest``。

    ``docs`` 里值为 ``None`` 表示该文档不变，空串表示删除；``remove_docs``
    整体删除文档。``aliases`` / ``tags`` / ``description`` / ``version`` /
    ``source`` / ``image`` 为 ``None`` 时保持原值（与 Go 的 nil 指针一致）。
    """

    id: str = ""
    name: str = ""
    aliases: list[str] | None = None
    description: str | None = None
    tags: list[str] | None = None
    visibility: str = ""
    owner_platform: str = ""
    owner_id: str = ""
    version: str | None = None
    source: str | None = None
    docs: dict[str, str | None] | None = None
    remove_docs: list[str] | None = None
    image: ImageSettings | None = None


@dataclass(slots=True)
class SearchResult:
    """全库检索的一条命中。对应 Go ``SearchResult``。"""

    id: str = ""
    name: str = ""
    tags: list[str] = field(default_factory=list)
    snippet: str = ""
    score: int = 0


@dataclass(slots=True)
class AssetManifestEntry:
    """备份清单里的一项。对应 Go ``AssetManifestEntry``。"""

    kind: str = ""
    name: str = ""
    path: str = ""
    size: int = 0
    version: str = ""
    source: str = ""
    sha256: str = ""


@dataclass(slots=True)
class CharacterManifest:
    """一个角色的版本 / 来源 / 备份清单。对应 Go ``CharacterManifest``。"""

    id: str = ""
    name: str = ""
    version: str = ""
    source: str = ""
    updated_at: str = ""
    assets: list[AssetManifestEntry] = field(default_factory=list)


@dataclass(slots=True)
class AssetManifest:
    """整个角色库的备份清单。对应 Go ``AssetManifest``。"""

    generated_at: str = ""
    characters: list[CharacterManifest] = field(default_factory=list)


@dataclass(slots=True)
class _CharacterMeta:
    """``character.toml`` 的 ``[character]`` 段。对应 Go ``characterMeta``。"""

    id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    owner_platform: str = ""
    owner_id: str = ""
    visibility: str = ""
    version: str = ""
    source: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class _FileFormat:
    """``character.toml`` 的整体结构。对应 Go ``fileFormat``。"""

    character: _CharacterMeta = field(default_factory=_CharacterMeta)
    image: ImageSettings = field(default_factory=ImageSettings)
    images: list[Image] = field(default_factory=list)


class Store:
    """目录支撑、带缓存的角色库。对应 Go ``Store``。"""

    def __init__(self, root: str = "") -> None:
        text = str(root or "").strip()
        if not text:
            text = "characters"
        self.root = os.path.normpath(text)
        self._lock = threading.RLock()
        self._loaded_at: float | None = None
        self._entries: dict[str, Character] = {}
        self._order: list[str] = []

    # -- 生命周期 -----------------------------------------------------------

    def enabled(self) -> bool:
        """角色库是否可用。对应 Go ``Store.Enabled``。"""
        return bool(str(self.root or "").strip())

    def refresh(self, ctx: Any = None) -> None:
        """强制全量重扫根目录。对应 Go ``Store.Refresh``。"""
        if not self.enabled():
            return
        with self._lock:
            self._scan_locked(ctx)

    def _ensure(self, ctx: Any = None) -> None:
        """必要时重扫（带冷却）。对应 Go ``Store.ensure``。"""
        if not self.enabled():
            return
        with self._lock:
            if self._loaded_at is not None and (time.monotonic() - self._loaded_at) < SCAN_COOLDOWN:
                return
            self._scan_locked(ctx)

    def _scan_locked(self, ctx: Any = None) -> None:
        """对应 Go ``Store.scanLocked``：``ctx`` 已取消时抛该 ctx 的错误。"""
        os.makedirs(self.root, mode=0o755, exist_ok=True)
        try:
            names = sorted(entry.name for entry in _dir_entries(self.root) if entry.is_dir())
        except OSError as exc:
            raise CharacterError(f"read character root: {exc}") from exc
        entries: dict[str, Character] = {}
        order: list[str] = []
        for name in names:
            _raise_if_cancelled(ctx)
            if not _ID_PATTERN.match(name):
                continue
            try:
                item = _read_character(os.path.join(self.root, name))
            except (OSError, ValueError, CharacterError):
                continue
            entries[item.id] = item
            order.append(item.id)
        order.sort()
        self._entries = entries
        self._order = order
        self._loaded_at = time.monotonic()

    # -- 读取 ---------------------------------------------------------------

    def list(self, ctx: Any = None, viewer: Viewer | None = None) -> list[Character]:
        """``viewer`` 可见的角色，按 id 排序。对应 Go ``Store.List``。"""
        person = viewer or Viewer()
        self._ensure(ctx)
        with self._lock:
            return [
                self._entries[item_id]
                for item_id in self._order
                if self._entries[item_id].visible_to(person)
            ]

    def get(self, ctx: Any, id: str) -> Character:
        """按 id 取角色（不看可见性）。对应 Go ``Store.Get``。"""
        key = normalize_id(id)
        if not valid_id(key):
            raise ErrInvalidID
        self._ensure(ctx)
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                raise ErrNotFound
            return item

    def get_visible(self, ctx: Any, id: str, viewer: Viewer | None = None) -> Character:
        """按 id 取角色，不可读时抛 ``ErrForbidden``。对应 Go ``Store.GetVisible``。"""
        item = self.get(ctx, id)
        if not item.visible_to(viewer or Viewer()):
            raise ErrForbidden
        return item

    def read_image(
        self, ctx: Any, id: str, name: str, viewer: Viewer | None = None
    ) -> tuple[bytes, str]:
        """读取一张已入库图片。对应 Go ``Store.ReadImage``。"""
        item = self.get_visible(ctx, id, viewer)
        safe = safe_image_name(name)
        if not safe:
            raise CharacterError("invalid image name")
        for image in item.images:
            if image.name != safe:
                continue
            path = safe_join(item.dir, image.path)
            try:
                with open(path, "rb") as handle:
                    return handle.read(), image.mime_type
            except OSError as exc:
                raise CharacterError(str(exc)) from exc
        raise ErrNotFound

    # -- 备份清单（实现体在 manifest.py，按职责从 store.go 拆出） -------------

    def manifest(self, ctx: Any = None, viewer: Viewer | None = None) -> AssetManifest:
        """所有可见角色的版本 / 来源 / 校验和清单。对应 Go ``Store.Manifest``。"""
        from atri_x.character.manifest import manifest

        return manifest(self, ctx, viewer)

    def write_manifest(self, ctx: Any, path: str, viewer: Viewer | None = None) -> None:
        """原子写一份 JSON 备份清单。对应 Go ``Store.WriteManifest``。"""
        from atri_x.character.manifest import write_manifest

        write_manifest(self, ctx, path, viewer)

    # -- 写入 / 检索转发（实现体在 write / search，按 Go 的文件边界拆分） ------
    #
    # write.py / search.py 需要 import 本模块的类型，因此这里反向引用只能放在
    # 方法内部（延迟导入），签名与 Go 一一对应。

    def write(self, ctx: Any, req: WriteRequest, viewer: Viewer | None = None) -> Character:
        """对应 Go ``Store.Write``。"""
        from atri_x.character.write import write

        return write(self, ctx, req, viewer)

    def delete(self, ctx: Any, id: str, viewer: Viewer | None = None) -> None:
        """对应 Go ``Store.Delete``。"""
        from atri_x.character.write import delete

        delete(self, ctx, id, viewer)

    def add_image(
        self,
        ctx: Any,
        id: str,
        name: str,
        mime_type: str,
        media_id: str,
        data: bytes,
        viewer: Viewer | None = None,
    ) -> Image:
        """对应 Go ``Store.AddImage``。"""
        from atri_x.character.write import add_image

        return add_image(self, ctx, id, name, mime_type, media_id, data, viewer)

    def add_image_with_meta(
        self,
        ctx: Any,
        id: str,
        name: str,
        mime_type: str,
        media_id: str,
        version: str,
        source: str,
        data: bytes,
        viewer: Viewer | None = None,
    ) -> Image:
        """对应 Go ``Store.AddImageWithMeta``。"""
        from atri_x.character.write import add_image_with_meta

        return add_image_with_meta(
            self, ctx, id, name, mime_type, media_id, version, source, data, viewer
        )

    def remove_image(self, ctx: Any, id: str, name: str, viewer: Viewer | None = None) -> None:
        """对应 Go ``Store.RemoveImage``。"""
        from atri_x.character.write import remove_image

        remove_image(self, ctx, id, name, viewer)

    def search(
        self,
        ctx: Any,
        query: str,
        match_mode: str = "",
        tag: str = "",
        limit: int = 0,
        viewer: Viewer | None = None,
    ) -> list[SearchResult]:
        """对应 Go ``Store.Search``。"""
        from atri_x.character.search import search

        return search(self, ctx, query, match_mode, tag, limit, viewer)


# ---------------------------------------------------------------------------
# ctx 值传递（Go 的 context.WithValue / ctx.Value，鸭子类型）
# ---------------------------------------------------------------------------

_ACTIVE_KEY = object()


def with_active(ctx: Any, *ids: str) -> Any:
    """记录本轮生效的角色 id。对应 Go ``WithActive``。"""
    if ctx is None or not ids:
        return ctx
    current = active_ids(ctx)
    seen = set(current)
    for item in ids:
        key = normalize_id(item)
        if not key or key in seen:
            continue
        seen.add(key)
        current.append(key)
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(_ACTIVE_KEY, current)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[_ACTIVE_KEY] = current
    return out


def active_ids(ctx: Any) -> list[str]:
    """本轮生效的角色 id。对应 Go ``ActiveIDs``。"""
    if ctx is None:
        return []
    getter = getattr(ctx, "value", None)
    if callable(getter):
        value = getter(_ACTIVE_KEY, None)
    elif isinstance(ctx, Mapping):
        value = ctx.get(_ACTIVE_KEY)
    else:
        value = None
    if not value:
        return []
    return [str(item) for item in value]


# ---------------------------------------------------------------------------
# 读取实现（Go store.go 的私有函数）
# ---------------------------------------------------------------------------

def _read_character(directory: str) -> Character:
    """读一个角色目录。对应 Go ``readCharacter``。"""
    path = os.path.join(directory, FILE_NAME)
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CharacterError(f"parse {FILE_NAME}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CharacterError(f"parse {FILE_NAME}: 顶层不是表")
    file = _file_from_toml(parsed)
    meta = file.character
    if not _ID_PATTERN.match(str(meta.id or "")):
        raise CharacterError(f'{FILE_NAME}: invalid id "{meta.id}"')
    item = Character(
        id=meta.id,
        name=str(meta.name or "").strip(),
        aliases=clean_strings(meta.aliases),
        description=str(meta.description or "").strip(),
        tags=clean_strings(meta.tags),
        owner_platform=str(meta.owner_platform or "").strip(),
        owner_id=str(meta.owner_id or "").strip(),
        visibility=normalize_visibility(meta.visibility),
        version=str(meta.version or "").strip(),
        source=str(meta.source or "").strip(),
        created_at=str(meta.created_at or "").strip(),
        updated_at=str(meta.updated_at or "").strip(),
        dir=directory,
        images=list(file.images),
        image=file.image,
        docs={},
    )
    if not item.name:
        item.name = item.id
    for kind in DOC_ORDER:
        name, ok = doc_file_name(kind)
        if not ok:
            continue
        try:
            content = _read_doc(directory, name)
        except (OSError, CharacterError):
            continue
        if content.strip():
            item.docs[kind] = content
    notes_dir = os.path.join(directory, NOTES_DIR)
    try:
        notes = sorted(entry.name for entry in _dir_entries(notes_dir) if not entry.is_dir())
    except OSError:
        notes = []
    for name in notes:
        if not name.lower().endswith(".md"):
            continue
        base = name[: -len(os.path.splitext(name)[1])]
        if not _NAME_PATTERN.match(base):
            continue
        try:
            content = _read_doc(directory, NOTES_DIR + "/" + name)
        except (OSError, CharacterError):
            continue
        if content.strip():
            item.docs[NOTES_DIR + "/" + base] = content
    item.images.sort(key=lambda image: image.name)
    return item


def _read_doc(directory: str, rel: str) -> str:
    """对应 Go ``readDoc``。"""
    path = safe_join(directory, rel)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _file_from_toml(parsed: Mapping[str, Any]) -> _FileFormat:
    """把 TOML 表映射成 :class:`_FileFormat`（未知键忽略，与 go-toml 一致）。"""
    character = parsed.get("character")
    character = character if isinstance(character, Mapping) else {}
    image = parsed.get("image")
    image = image if isinstance(image, Mapping) else {}
    raw_images = parsed.get("images")
    raw_images = raw_images if isinstance(raw_images, list) else []
    return _FileFormat(
        character=_CharacterMeta(
            id=_text(character.get("id")),
            name=_text(character.get("name")),
            aliases=_text_list(character.get("aliases")),
            description=_text(character.get("description")),
            tags=_text_list(character.get("tags")),
            owner_platform=_text(character.get("owner_platform")),
            owner_id=_text(character.get("owner_id")),
            visibility=_text(character.get("visibility")),
            version=_text(character.get("version")),
            source=_text(character.get("source")),
            created_at=_text(character.get("created_at")),
            updated_at=_text(character.get("updated_at")),
        ),
        image=ImageSettings(
            preset_prompt=_text(image.get("preset_prompt")),
            negative_prompt=_text(image.get("negative_prompt")),
            size=_text(image.get("size")),
            quality=_text(image.get("quality")),
            references=_text_list(image.get("references")),
        ),
        images=[
            Image(
                name=_text(item.get("name")),
                mime_type=_text(item.get("mime_type")),
                media_id=_text(item.get("media_id")),
                path=_text(item.get("path")),
                size=int(item.get("size") or 0),
                version=_text(item.get("version")),
                source=_text(item.get("source")),
                created_at=_text(item.get("created_at")),
            )
            for item in raw_images
            if isinstance(item, Mapping)
        ],
    )


def to_file(character: Character) -> _FileFormat:
    """把角色还原成待写入的文件结构。对应 Go ``Character.toFile``。"""
    return _FileFormat(
        character=_CharacterMeta(
            id=character.id,
            name=character.name,
            aliases=list(character.aliases),
            description=character.description,
            tags=list(character.tags),
            owner_platform=character.owner_platform,
            owner_id=character.owner_id,
            visibility=str(character.visibility),
            version=character.version,
            source=character.source,
            created_at=character.created_at,
            updated_at=character.updated_at,
        ),
        image=character.image,
        images=list(character.images),
    )


# ---------------------------------------------------------------------------
# 小工具（Go store.go / write.go 的包级函数）
# ---------------------------------------------------------------------------

def normalize_id(id: str) -> str:
    """对应 Go ``normalizeID``。"""
    return str(id or "").strip().lower()


def valid_id(value: str) -> bool:
    """id 是否合法（小写字母数字开头，最长 64）。对应 Go ``idPattern`` 判定。"""
    return bool(_ID_PATTERN.match(str(value or "")))


def normalize_visibility(value: Any) -> Visibility:
    """对应 Go ``normalizeVisibility``。"""
    if str(value or "").strip().lower() == VisibilityPrivate.value:
        return VisibilityPrivate
    return VisibilityPublic


def safe_join(directory: str, rel: str) -> str:
    """把相对路径拼进角色目录，越界即报错。对应 Go ``safeJoin``。"""
    base = os.path.abspath(directory)
    path = os.path.abspath(os.path.join(base, str(rel or "").replace("/", os.sep)))
    if path != base and not path.startswith(base + os.sep):
        raise CharacterError(f'path "{rel}" escapes character directory')
    return path


def safe_image_name(name: str) -> str:
    """把用户给的图片名收敛成安全的基名。对应 Go ``safeImageName``。"""
    text = str(name or "").strip().replace("\\", "/").rstrip("/")
    text = text.split("/")[-1] if text else ""
    text = text.strip()
    if not text or text == "." or text.startswith("."):
        return ""
    for ch in text:
        if ch in ("/", "\\", "\x00"):
            return ""
    return text


def doc_file_name(kind: str) -> tuple[str, bool]:
    """文档种类 → 相对文件名。对应 Go ``docFileName``。"""
    text = str(kind or "").strip()
    if text in DOC_ORDER:
        return text + ".md", True
    text = text.removeprefix(NOTES_DIR + "/")
    if _NAME_PATTERN.match(text):
        return NOTES_DIR + "/" + text + ".md", True
    return "", False


def clean_strings(values: list[str] | None) -> list[str]:
    """去空白、去空值、保序去重。对应 Go ``cleanStrings``。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def next_version(current: str) -> str:
    """版本号 +1（非法值回到 "1"）。对应 Go ``nextVersion``。"""
    text = str(current or "").strip()
    if not text:
        return "1"
    if not re.fullmatch(r"[+-]?[0-9]+", text):
        return "1"
    value = int(text)
    if value <= 0:
        return "1"
    return str(value + 1)


def now_string() -> str:
    """UTC 的 RFC3339 秒级时间戳。对应 Go ``nowString``。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_non_empty(*values: Any) -> str:
    """对应 Go ``firstNonEmpty``。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def go_json_indent(manifest: AssetManifest) -> str:
    """按 Go ``encoding/json`` 的 ``MarshalIndent(v, "", "  ")`` 渲染清单。

    实现在 :mod:`atri_x.character.manifest`（与 :meth:`Store.WriteManifest` 一起
    从 store.go 拆出）；这里保留同名转发，便于交叉检索。
    """
    from atri_x.character.manifest import go_json_indent as render

    return render(manifest)


def write_file_atomic(path: str, data: bytes, mode: int = 0o644) -> None:
    """原子写文件（临时文件 + 替换）。对应 Go ``writeFileAtomic``。"""
    from atri_x.utils.fileops.atomic_write import atomic_write_file

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o755, exist_ok=True)
    atomic_write_file(path, data, mode)


def _dir_entries(path: str) -> list[os.DirEntry[str]]:
    """``os.scandir`` 的一次性快照（对应 Go ``os.ReadDir``，已关闭句柄）。"""
    with os.scandir(path) as iterator:
        return list(iterator)


def _raise_if_cancelled(ctx: Any) -> None:
    """对应 Go ``if err := ctx.Err(); err != nil { return err }``。"""
    if ctx is None:
        return
    for name in ("error", "err"):
        getter = getattr(ctx, name, None)
        if callable(getter):
            err = getter()
            if err is not None:
                raise err
            return
    if getattr(ctx, "cancelled", False) is True:
        raise CharacterError("context cancelled")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []
