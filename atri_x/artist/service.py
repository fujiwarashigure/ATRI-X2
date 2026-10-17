"""画师串服务：身份视图、可见性、检索/推荐、保存/删除与方言编译。

对应 Go：``internal/artist/service.go``。

四条不可动摇的规则（与 Go 注释里的意图一致）：

* **身份只来自服务端调用上下文**：模型可以"请求"一个 scope，但说不了自己是谁。
  :class:`Viewer` 的每个字段都来自 ctx，工具参数里没有它们。
* **可见性只有一处**：全局对所有人可见；群组合只在它自己那个平台群里可见；个人组合
  只有本人可见。超级管理员**也看不到**别人的个人组合——产品里没有这个需求，收进来
  只是等着泄露。
* **保存时 scope 与归属重新算**：模型传了别人的 scope id，也只会写进它自己的 scope。
* **拒绝解释的表达式逐字保留**：:func:`compile_raw` 遇到 ``unparsed`` token 原样输出，
  猜它比留着它更糟。

与 Go 的差异：

* ``rand.Read`` → :func:`secrets.token_bytes`；``sha256`` 摘要前 4 字节 → ``hexdigest()[:8]``。
* Go 的 ``unicode.IsLetter`` 判断非 ASCII 字母/数字：Python 用 ``str.isalnum()`` 等价表达。
* 哨兵 error → :mod:`atri_x.errors` 的模块级单例（``is`` 判定）。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from atri_x.errors import (
    ErrDisabled,
    ErrForbidden,
    ErrInvalid,
    ErrNotFound,
    ForbiddenError,
    InvalidError,
)

from .metadata_store import GenerationStore
from .parser import classify, normalize_tag
from .render import Render, render_preset, render_tag
from .search import Match, rank, search
from .store import (
    MAX_RAW_STRING_RUNES,
    REVISION_ANY,
    REVISION_CREATE,
    Store,
)
from .types import (
    Artist,
    Dialect,
    Extraction,
    ImageMetadata,
    Metadata,
    Preset,
    PresetStatus,
    Scope,
    SourceKind,
    TokenKind,
    normalize_dialect,
)


@dataclass
class Viewer:
    """一次调用背后的服务端身份。对应 Go ``Viewer``。"""

    actor_id: str = ""
    platform: str = ""
    scope_id: str = ""
    superadmin: bool = False


@dataclass
class Compiled:
    """可以直接追加进生成提示词的画师块。对应 Go ``Compiled``。"""

    preset_id: str = ""
    name: str = ""
    dialect: Dialect = Dialect.AUTO
    original: str = ""
    positive: str = ""
    negative: str = ""
    warnings: list[str] = field(default_factory=list)


def group_scope_key(platform: str, scope_id: str) -> str:
    """群 scope 的存储键（平台参与其中）。对应 Go ``GroupScopeKey``。"""
    return (platform or "").strip() + "/" + (scope_id or "").strip()


class Service:
    """串起提示词解析、图片元数据读取、串库与方言渲染。对应 Go ``Service``。"""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store
        #: 入库时留档的生成参数。它**故意**与串库解耦：读图片自己的生成参数
        #: 不需要可写的串库，而且这份留档能扛过导入转码。
        self.generations: GenerationStore | None = None

    # -- 基础信息 ---------------------------------------------------------
    def enabled(self) -> bool:
        """对应 Go ``Service.Enabled``。"""
        store = self._store
        return store is not None and store.enabled()

    def store(self) -> Store | None:
        """暴露底层 store（诊断命令要读库 revision）。对应 Go ``Service.Store``。"""
        return self._store

    def resolver(self) -> Callable[[str], "tuple[str, bool]"] | None:
        """解析期用的目录查表。对应 Go ``Service.Resolver``。"""
        if not self.enabled():
            return None
        return self._store.resolver()

    # -- 解析 -------------------------------------------------------------
    def extract_text(self, text: str) -> Extraction:
        """从调用方给的文本里解析画师串。对应 Go ``ExtractText``。"""
        from .parser import extract_artists_with

        return extract_artists_with(text, self.resolver())

    def extract_image_metadata(self, media_id: str, data: bytes) -> ImageMetadata:
        """读某张已入库图片的生成参数并解析画师串。对应 Go ``ExtractImageMetadata``。"""
        record, found, record_err = self._record_for(media_id)
        if found and record.transcoded:
            return self._image_metadata_from(
                record.metadata(), "archive", record_err
            )
        from .metadata import extract_metadata

        meta, ok = extract_metadata(data or b"")
        if ok:
            return self._image_metadata_from(meta, "embedded", record_err)
        if found:
            return self._image_metadata_from(record.metadata(), "archive", record_err)
        out = ImageMetadata(
            extraction=Extraction(source=SourceKind.NONE, dialect=Dialect.AUTO)
        )
        if record_err is not None:
            out.extraction.warning(
                "读取入库时留存的生成参数失败：" + str(record_err)
            )
        return out

    def _image_metadata_from(
        self, meta: Metadata, origin: str, record_err: BaseException | None
    ) -> ImageMetadata:
        """把一次解析结果变成对外的答案。对应 Go ``imageMetadataFrom``。"""
        from .parser import extract_from_graph

        positive = (meta.positive or "").strip()
        if positive == "" and meta.prompts:
            positive = ", ".join(meta.prompts)
        out = extract_from_graph(positive, self.resolver())
        for warning in meta.warnings or []:
            out.warning(warning)
        if record_err is not None:
            out.warning("读取入库时留存的生成参数失败：" + str(record_err))
        if not out.artists:
            out.has_original = False
            out.original = ""
            if positive == "":
                out.warning("图片元数据里没有找到提示词文本。")
            else:
                out.warning("图片元数据里记录了提示词，但没有识别出画师标签。")
        return ImageMetadata(
            extraction=out, metadata=meta, found=True, origin=origin
        )

    def _record_for(
        self, media_id: str
    ) -> tuple[Any, bool, BaseException | None]:
        """读某个 media 对象的入库留档。对应 Go ``recordFor``。"""
        key = (media_id or "").strip()
        generations = self.generations
        if key == "" or generations is None or not generations.enabled():
            return None, False, None
        try:
            record, ok = generations.lookup(key)
        except BaseException as exc:  # noqa: BLE001 - 读不了要变成可见 warning
            return None, False, exc
        if not ok:
            return None, False, None
        return record, True, None

    # -- 可见性 -----------------------------------------------------------
    def _visible(self, viewer: Viewer) -> tuple[list[Artist], list[Preset]]:
        """viewer 能看到的条目。对应 Go ``Service.visible``。"""
        if not self.enabled():
            raise ErrDisabled
        library = self._store.library()
        actor = (viewer.actor_id or "").strip()
        group = group_scope_key(viewer.platform, viewer.scope_id)
        has_group = (viewer.scope_id or "").strip() != ""
        presets: list[Preset] = []
        for preset in library.presets or []:
            if preset.scope == Scope.GLOBAL:
                presets.append(preset)
            elif preset.scope == Scope.GROUP:
                if has_group and preset.scope_id == group:
                    presets.append(preset)
            elif preset.scope == Scope.USER:
                if actor != "" and preset.scope_id == actor:
                    presets.append(preset)
        return list(library.artists or []), presets

    def visible_artists(self, viewer: Viewer) -> list[Artist]:
        """目录是全库共享的；由它组合出来的 preset 才分 scope。对应 Go ``VisibleArtists``。"""
        artists, _presets = self._visible(viewer)
        return artists

    def visible_presets(self, viewer: Viewer) -> list[Preset]:
        """对应 Go ``VisiblePresets``。"""
        _artists, presets = self._visible(viewer)
        return presets

    def preset(self, viewer: Viewer, id: str) -> Preset:
        """取一个 viewer 有权看到的 preset。对应 Go ``Service.Preset``。"""
        if not self.enabled():
            raise ErrDisabled
        preset, found = self._store.preset(id)
        if not found:
            raise ErrNotFound
        if not self._can_view(viewer, preset):
            raise ErrForbidden
        return preset

    def _can_view(self, viewer: Viewer, preset: Preset) -> bool:
        if preset.scope == Scope.GLOBAL:
            return True
        if preset.scope == Scope.GROUP:
            return (viewer.scope_id or "").strip() != "" and preset.scope_id == group_scope_key(
                viewer.platform, viewer.scope_id
            )
        if preset.scope == Scope.USER:
            return (viewer.actor_id or "").strip() != "" and preset.scope_id == viewer.actor_id
        return False

    def _can_write(self, viewer: Viewer, preset: Preset) -> None:
        """viewer 能否在 preset 所在的 scope 里创建/修改。对应 Go ``canWrite``。"""
        if preset.scope == Scope.GLOBAL:
            if not viewer.superadmin:
                raise ForbiddenError(
                    f"{ErrForbidden}: 全局画师串组合只有超级管理员可以修改"
                )
            return
        if preset.scope == Scope.GROUP:
            if (viewer.scope_id or "").strip() == "":
                raise ForbiddenError(
                    f"{ErrForbidden}: 当前不在群聊会话里，无法修改本群画师串组合"
                )
            if preset.scope_id != group_scope_key(viewer.platform, viewer.scope_id):
                raise ForbiddenError(f"{ErrForbidden}: 该画师串组合属于其它群")
            return
        if preset.scope == Scope.USER:
            if (viewer.actor_id or "").strip() == "":
                raise ForbiddenError(f"{ErrForbidden}: 无法确定调用者身份")
            if preset.scope_id != viewer.actor_id:
                raise ForbiddenError(f"{ErrForbidden}: 该画师串组合属于其它用户")
            return
        raise InvalidError(f"{ErrInvalid}: 未知的 scope")

    # -- 检索 -------------------------------------------------------------
    def search(self, viewer: Viewer, query: str, limit: int) -> list[Match]:
        """对应 Go ``Service.Search``。"""
        artists, presets = self._visible(viewer)
        return search(artists, presets, query, limit)

    def recommend(self, viewer: Viewer, style_text: str, limit: int) -> list[Match]:
        """按风格描述推荐"已确认"的可见组合。对应 Go ``Service.Recommend``。"""
        artists, presets = self._visible(viewer)
        if (style_text or "").strip() == "":
            return []
        approved = [item for item in presets if item.status == PresetStatus.APPROVED]
        matches = rank(artists, approved, style_text, limit)
        return [item for item in matches if item.kind == "preset"]

    # -- 写入 -------------------------------------------------------------
    def save(self, viewer: Viewer, preset: Preset, expected_revision: int) -> Preset:
        """按调用方请求的 scope 创建/更新 preset。对应 Go ``Service.Save``。"""
        if not self.enabled():
            raise ErrDisabled
        preset = _normalize_preset_local(preset)
        if (preset.id or "").strip() == "":
            preset.id = new_preset_id(preset.name)
        if preset.scope == Scope.USER:
            if (viewer.actor_id or "").strip() == "":
                raise ForbiddenError(f"{ErrForbidden}: 无法确定调用者身份")
            preset.scope_id = viewer.actor_id
        elif preset.scope == Scope.GROUP:
            if (viewer.scope_id or "").strip() == "":
                raise ForbiddenError(
                    f"{ErrForbidden}: 当前不在群聊会话里，无法保存本群画师串组合"
                )
            preset.scope_id = group_scope_key(viewer.platform, viewer.scope_id)
        elif preset.scope == Scope.GLOBAL:
            if not viewer.superadmin:
                raise ForbiddenError(
                    f"{ErrForbidden}: 全局画师串组合只有超级管理员可以保存"
                )
            preset.scope_id = ""
        else:
            raise InvalidError(f"{ErrInvalid}: 未知的 scope")
        if expected_revision not in (REVISION_ANY, REVISION_CREATE):
            # 更新不能"接管"一个已经住在别的 scope 里的条目，所以先看库里真正存的是什么。
            existing, found = self._store.preset(preset.id)
            if found:
                self._can_write(viewer, existing)
        return self._store.upsert_preset(preset, expected_revision)

    def delete(self, viewer: Viewer, id: str, expected_revision: int) -> Preset:
        """删除一个 viewer 有权改的 preset。对应 Go ``Service.Delete``。"""
        if not self.enabled():
            raise ErrDisabled
        current = self.preset(viewer, id)
        self._can_write(viewer, current)
        return self._store.delete_preset(current.id, expected_revision)

    # -- 编译 -------------------------------------------------------------
    def compile(
        self, viewer: Viewer, preset_id: str, raw_string: str, dialect: Dialect
    ) -> Compiled:
        """把 preset id 或原文画师串编译成可拼进提示词的块。对应 Go ``Service.Compile``。"""
        if not dialect.valid():
            raise InvalidError(f"{ErrInvalid}: 未知的提示词方言")
        if (preset_id or "").strip() != "":
            preset = self.preset(viewer, preset_id)
            rendered: Render = render_preset(preset, self._tag_lookup(), dialect)
            return Compiled(
                preset_id=preset.id,
                name=preset.name,
                dialect=dialect,
                original=rendered.artist_raw,
                positive=rendered.positive,
                negative=rendered.negative,
                warnings=list(rendered.warnings),
            )
        raw = (raw_string or "").strip()
        if raw == "":
            raise InvalidError(f"{ErrInvalid}: 需要 artist_preset_id 或 artist_string")
        if len(raw) > MAX_RAW_STRING_RUNES:
            raise InvalidError(
                f"{ErrInvalid}: 画师串不能超过 {MAX_RAW_STRING_RUNES} 个字符"
            )
        return compile_raw(raw, dialect, self.resolver())

    def _tag_lookup(self) -> Callable[[str], "tuple[str, bool]"] | None:
        if not self.enabled():
            return None

        def lookup(id: str) -> tuple[str, bool]:
            item, found = self._store.artist(id)
            if not found:
                return "", False
            return item.canonical_tag, True

        return lookup


def compile_raw(
    raw: str, dialect: Dialect, resolve: Callable[[str], "tuple[str, bool]"] | None
) -> Compiled:
    """把原文画师串按方言渲染。对应 Go ``CompileRaw``。"""
    tokens = classify(raw, resolve)
    parts: list[str] = []
    warnings: list[str] = []
    for token in tokens:
        if token.kind == TokenKind.UNPARSED:
            # 本包拒绝解释的表达式逐字保留；猜它比留着它更糟。
            parts.append(token.raw)
            continue
        rendered, tag_warnings = render_tag(token.value, token.weight, dialect)
        if rendered == "":
            continue
        parts.append(rendered)
        for warning in tag_warnings:
            if warning not in warnings:
                warnings.append(warning)
    return Compiled(
        name=raw,
        dialect=dialect,
        original=raw,
        positive=", ".join(parts),
        warnings=warnings,
    )


def no_metadata_hint(mime_type: str) -> str:
    """解释"这张图为什么没有生成参数"。对应 Go ``NoMetadataHint``。"""
    lowered = (mime_type or "").strip().lower()
    if "jpeg" in lowered or "jpg" in lowered:
        return (
            "这张图没有生成参数，而且它是 JPEG：ATRI-X 入库时会把超过 4 MiB 或长边达到 "
            "4096 像素的图片转码成 JPEG，PNG 里的生成参数会在那一步丢掉。请重新发送原始 "
            "PNG 文件（未压缩、未截图、未经平台转存）；开启 image_generation.metadata_store "
            "后，参数会在入库转码之前留档，之后再取都不会丢。"
        )
    return (
        "这张图里没有找到生成参数：PNG 的 tEXt/iTXt/zTXt、A1111 的 parameters、"
        "NovelAI、ComfyUI 元数据都没有命中。常见原因是出图工具没有写入参数，或图片经过"
        "截图、二次编辑、平台转存后参数已丢失。请发送带参数的原图；如果确实没有参数，"
        "可以用 include_artist=recommend 让视觉模型按画面风格推荐串库里的组合。"
    )


def new_artist_id(tag: str) -> str:
    """由标签名推出目录 id（确定性）。对应 Go ``NewArtistID``。"""
    from .store import valid_id

    slug = slugify(tag)
    if valid_id(slug):
        return slug
    digest = hashlib.sha256(normalize_tag(tag).encode("utf-8")).hexdigest()
    return "artist-" + digest[:8]


def new_preset_id(name: str) -> str:
    """由显示名推出稳定、URL 安全的 id。对应 Go ``NewPresetID``。"""
    slug = slugify(name)
    if slug == "":
        slug = "preset"
    if len(slug) > 32:
        slug = slug[:32].strip("-_.")
        if slug == "":
            slug = "preset"
    try:
        suffix = secrets.token_bytes(3).hex()
    except Exception:  # pragma: no cover - 熵池不可用时与 Go 一样退到 -1
        return slug + "-1"
    return slug + "-" + suffix


def slugify(value: str) -> str:
    """把名字压成 ASCII slug。对应 Go ``slugify``。"""
    out: list[str] = []
    last_dash = False
    for ch in (value or "").strip().lower():
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_":
            out.append(ch)
            last_dash = False
        elif ch.isalpha() or ch.isdigit():
            # 非 ASCII 的字母/数字直接丢掉：id 保持 ASCII 才好打、好记日志、好当键。
            continue
        elif not last_dash and out:
            out.append("-")
            last_dash = True
    return "".join(out).strip("-_.")


def _normalize_preset_local(preset: Preset) -> Preset:
    """延迟到 store 的归一化，避免本模块 import 顺序影响。"""
    from .store import normalize_preset

    return normalize_preset(preset)


def with_dialect(value: str) -> Dialect:
    """便捷包装：字符串 → 方言。"""
    return normalize_dialect(value)


__all__ = [name for name in dir() if not name.startswith("_")]
