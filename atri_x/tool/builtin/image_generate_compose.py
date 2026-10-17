"""``image_generate`` 的提示词编译与参考图收集。

拆分来源：``internal/tool/builtin/image_generate.go`` 的后半部分
（``promptDialect`` / ``imageArtistBlock`` / ``resolveArtistBlock`` /
``negativeParts`` / ``composeImageRequest`` / ``imageDryRunResult`` /
``multiCharacterConstraint`` / ``collectReferences`` 一族 / ``extFromMIME`` /
``previewText``）。前半部分（工具本体与批量生图）留在
:mod:`atri_x.tool.builtin.image_generate`。

这里的核心约束有两条：

* 负向提示词默认走独立通道，因为加权标签模型会把 ``avoid: blurry`` 读成
  "画点糊的东西"；只有 profile 明确要求时才折进正向提示词。
* 超长时**整段丢弃**片段，绝不按字符切一半——``(artist_a:1.2)`` 不能被截成
  ``(artist_a:1.`` 发出去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.artist import Dialect, normalize_dialect
from atri_x.character import Character, Viewer
from atri_x.imagegen import (
    ComposeBlocks,
    NegativePromptModeInline,
    NegativePromptModeOff,
    NormalizeNegativePromptMode,
    PromptBlock,
    PromptPriorityCore,
    PromptPriorityOptional,
    PromptPriorityProtected,
    PromptPriorityStyle,
)
from atri_x.media import ID_PREFIX, ValidID
from atri_x.tool.builtin.artist_tools import artist_error_message
from atri_x.tool.builtin.common import first_non_empty_string, go_json, safe_file_name
from atri_x.tool.builtin.image_generate_context import ImageToolMessage

#: 单次最多几张。对应 Go ``maxImageBatchCount``。
MAX_IMAGE_BATCH_COUNT = 4

#: 最多几张参考图。对应 Go ``maxImageReferences``。
MAX_IMAGE_REFERENCES = 4


@dataclass
class ImageComposition:
    """一次生图调用编译出的提示词对。对应 Go ``imageComposition``。"""

    positive: str = ""
    negative: str = ""
    dropped: list[str] = field(default_factory=list)
    limit: int = 0
    runes: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImageArtistBlock:
    """一次调用的画师串编译结果。对应 Go ``imageArtistBlock``。"""

    preset_id: str = ""
    name: str = ""
    positive: str = ""
    negative: str = ""
    original: str = ""


def image_prompt_dialect(tool: Any) -> Dialect:
    """解析配置的提示词方言。对应 Go ``promptDialect``。

    配置为 auto 时退到声明的模型族，最后退到自然语言——这正是 OpenAI 兼容的
    images 端点期望的形态。
    """
    cfg = tool.cfg
    dialect = normalize_dialect(str(getattr(cfg, "prompt_dialect", "") or ""))
    if dialect != Dialect.AUTO:
        return dialect
    family = (str(getattr(cfg, "model_family", "") or "")).strip().lower()
    if family in ("sdxl", "sd", "stable_diffusion", "a1111", "webui"):
        return Dialect.SD_WEIGHTED
    if family in ("novelai", "nai"):
        return Dialect.NAI
    return Dialect.NATURAL


def image_artist_requested(args: Any) -> bool:
    """这次调用有没有要画师串。对应 Go ``imageArtistRequested``。"""
    if (getattr(args, "artist_string", "") or "").strip() or (
        getattr(args, "artist_preset_id", "") or ""
    ).strip():
        return True
    mode = (str(getattr(args, "artist_mode", "") or "")).strip().lower()
    return mode not in ("", "off", "none", "false", "0")


def image_protected_block_names(args: Any) -> list[str]:
    """点名改写器不许碰的片段。对应 Go ``imageProtectedBlockNames``。

    只点名不给内容：重点就是让模型**不要**复述它们，真正的文本在改写之后由程序
    拼进去。
    """
    if not image_artist_requested(args):
        return []
    return ["画师串（由程序按用户原文单独拼接，不要输出、不要改写它的顺序和权重）"]


def resolve_artist_block(
    tool: Any, ctx: Any, args: Any, scene: str, viewer: Any
) -> tuple[ImageArtistBlock, bool, list[str]]:
    """编译这次调用要追加的画师串。对应 Go ``resolveArtistBlock``。

    调用方没要求、profile 也没声明默认组合时什么都不加：默默给每张图挂上画师串
    会改变现有部署真正生成出来的东西。
    """
    mode = (str(getattr(args, "artist_mode", "") or "")).strip().lower()
    raw = (getattr(args, "artist_string", "") or "").strip()
    preset_id = (getattr(args, "artist_preset_id", "") or "").strip()
    default_preset = (str(getattr(tool.cfg, "artist_default_preset", "") or "")).strip()
    if mode == "":
        if raw != "" or preset_id != "":
            mode = "explicit"
        elif default_preset != "":
            mode = "explicit"
            preset_id = default_preset
        else:
            mode = "off"
    if mode in ("off", "none", "false", "0"):
        return ImageArtistBlock(), False, []
    service = getattr(getattr(tool, "artists", None), "service", None)
    if service is None or not _enabled(service):
        return (
            ImageArtistBlock(),
            False,
            [
                "画师串库未启用，本次没有加入画师串。"
                "请在 services.toml 里配置 [image_generation.artist_library]。"
            ],
        )
    warnings: list[str] = []
    if mode in ("auto", "recommend") and raw == "" and preset_id == "":
        try:
            matches = service.recommend(viewer, scene, 1)
        except Exception as exc:  # noqa: BLE001
            return (
                ImageArtistBlock(),
                False,
                ["从串库自动选择画师串失败：" + artist_error_message(exc)],
            )
        if not matches:
            return (
                ImageArtistBlock(),
                False,
                ["artist_mode=auto，但串库里没有匹配的已确认组合；本次没有加入画师串。"],
            )
        preset_id = matches[0].id
        warnings.append(
            f"artist_mode=auto 自动选择了组合「{matches[0].title}」（{matches[0].id}）。"
        )
    try:
        compiled = service.compile(viewer, preset_id, raw, image_prompt_dialect(tool))
    except Exception as exc:  # noqa: BLE001
        return ImageArtistBlock(), False, ["编译画师串失败：" + artist_error_message(exc)]
    warnings.extend(compiled.warnings or [])
    block = ImageArtistBlock(
        preset_id=compiled.preset_id,
        name=compiled.name,
        positive=compiled.positive,
        negative=compiled.negative,
        original=compiled.original,
    )
    if block.positive.strip() == "" and block.negative.strip() == "":
        return (
            ImageArtistBlock(),
            False,
            [*warnings, "画师串编译结果为空，本次没有加入画师串。"],
        )
    return block, True, warnings


def negative_parts(
    tool: Any, items: list[Character], optimizer_negative: str, extra_negative: str
) -> list[str]:
    """按发送顺序收集本次的负面提示词并去重。对应 Go ``negativeParts``。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = (value or "").strip()
        if value == "" or value in seen:
            return
        seen.add(value)
        out.append(value)

    add(extra_negative)
    add(optimizer_negative)
    add(str(getattr(tool.cfg, "negative_prompt", "") or ""))
    for item in items:
        if item is not None:
            add(str(getattr(item.image, "negative_prompt", "") or ""))
    return out


def compose_image_request(
    tool: Any,
    items: list[Character],
    scene: str,
    artist_block: str,
    optimizer_negative: str,
    extra_negative: str,
    reference_used: bool,
) -> ImageComposition:
    """编译正负向提示词。对应 Go ``composeImageRequest``。"""
    blocks: list[PromptBlock] = []

    def add(label: str, text: str, priority: int) -> None:
        if (text or "").strip() == "":
            return
        blocks.append(PromptBlock(label=label, text=text, priority=priority))

    cfg = tool.cfg
    add("global_preset", str(getattr(cfg, "preset_prompt", "") or ""), PromptPriorityStyle)
    if len(items) > 1:
        for item in items:
            if item is None:
                continue
            preset = (item.image_prompt() or "").strip()
            if preset == "":
                continue
            add(
                "character:" + item.id,
                f"角色 {item.name}（{item.id}）：\n{preset}",
                PromptPriorityStyle,
            )
    elif len(items) == 1 and items[0] is not None:
        add(
            "character:" + items[0].id,
            (items[0].image_prompt() or "").strip(),
            PromptPriorityStyle,
        )
    if reference_used:
        if len(items) > 1:
            add(
                "reference_consistency",
                "参考图中的每个角色都要与对应角色设定一致；"
                "保持各自的脸、发型、服装和配色独立，不要串角色。",
                PromptPriorityOptional,
            )
        else:
            add(
                "reference_consistency",
                "Keep face, hairstyle, outfit, and overall style consistent "
                "with the reference image",
                PromptPriorityOptional,
            )
    add("scene", (scene or "").strip(), PromptPriorityCore)
    add("artist_string", artist_block, PromptPriorityProtected)
    if len(items) > 1:
        add(
            "multi_character_constraint",
            multi_character_constraint(items),
            PromptPriorityProtected,
        )

    composition = ImageComposition()
    negatives = negative_parts(tool, items, optimizer_negative, extra_negative)
    mode = NormalizeNegativePromptMode(getattr(cfg, "negative_prompt_mode", ""))
    if mode == NegativePromptModeOff:
        if negatives:
            composition.warnings.append(
                f"profile 的 negative_prompt_mode=off，{len(negatives)} 条负面提示词"
                "已全部丢弃，不会传给生图服务。"
            )
    elif mode == NegativePromptModeInline:
        if negatives:
            add("negative_inline", "avoid: " + ", ".join(negatives), PromptPriorityStyle)
    else:
        composition.negative = ", ".join(negatives)

    limit = int(getattr(cfg, "max_prompt_runes", 0) or 0)
    if limit <= 0:
        limit = 4000
    composed = ComposeBlocks(blocks, "\n\n", limit)
    composition.positive = composed.prompt
    composition.dropped = list(composed.dropped)
    composition.limit = composed.limit
    composition.runes = composed.runes
    if composed.over_limit:
        raise ImageToolMessage(
            f"最终提示词 {composed.runes} 个字符，超过 max_prompt_runes={limit}，"
            "而且自动补充的片段已经全部丢弃。"
            "请缩短场景描述或画师串，或调大 image_generation.max_prompt_runes；"
            "为避免把 (画师:权重) 这类表达式切成半截，这里不会按字符硬截断"
        )
    if composed.dropped:
        composition.warnings.append(
            "提示词超长，已整体丢弃这些片段：" + "、".join(composed.dropped)
        )
    if composition.positive.strip() == "":
        raise ImageToolMessage("prompt 为空")
    return composition


def image_dry_run_result(
    tool: Any,
    composition: ImageComposition,
    dialect: Dialect,
    block: ImageArtistBlock,
    artist_used: bool,
    size: str,
    quality: str,
    count: int,
    warnings: list[str],
) -> Any:
    """渲染编译结果而不调用生图服务。对应 Go ``imageDryRunResult``。

    调用方可以在花掉额度之前先看清到底会发什么——尤其是画师串和它的方言。
    """
    from atri_x.tool.builtin.common import append_warnings
    from atri_x.tool.tool import Result

    all_warnings = [*warnings, *composition.warnings]
    cfg = tool.cfg
    mode = NormalizeNegativePromptMode(getattr(cfg, "negative_prompt_mode", ""))
    resolved_size = first_non_empty_string([size, str(cfg.size or "")])
    resolved_quality = first_non_empty_string([quality, str(cfg.quality or "")])
    payload: dict[str, Any] = {
        "dry_run": True,
        "model": cfg.model,
        "dialect": str(dialect),
        "dialect_name": dialect.label(),
        "size": resolved_size,
        "quality": resolved_quality,
        "count": count,
        "prompt": composition.positive,
        "negative": composition.negative,
        "negative_mode": str(mode),
        "prompt_runes": composition.runes,
        "max_runes": composition.limit,
        "dropped": list(composition.dropped),
        "warnings": all_warnings,
    }
    if artist_used:
        payload["artist"] = {
            "preset_id": block.preset_id,
            "name": block.name,
            "original": block.original,
            "applied": block.positive,
            "negative": block.negative,
        }
    result = Result(data=go_json(payload))
    lines = [
        "dry_run：只编译，不调用生图接口、不保存、不发送、不计生图额度。\n",
        f"model: {cfg.model}\n方言: {dialect.label()}\n"
        f"size: {resolved_size}\nquality: {resolved_quality}\n张数: {count}\n",
    ]
    if artist_used:
        if block.preset_id != "":
            lines.append(f"画师串组合: {block.name}（{block.preset_id}）\n")
        lines.append(f"画师串原文: {block.original}\n画师串实际拼接: {block.positive}\n")
        if block.negative != "":
            lines.append(f"画师串负向: {block.negative}\n")
    else:
        lines.append("画师串: 本次未使用\n")
    lines.append(
        f"最终正向提示词（{composition.runes} 字符，上限 {composition.limit}）:\n"
        f"{composition.positive}\n"
    )
    if composition.negative != "":
        lines.append(
            f"负向提示词（独立字段 {cfg.negative_prompt_field}）:\n"
            f"{composition.negative}\n"
        )
    elif mode == NegativePromptModeInline:
        lines.append("负向提示词: 已按 negative_prompt_mode=inline 折进正向提示词\n")
    else:
        lines.append("负向提示词: 未使用\n")
    if composition.dropped:
        lines.append("因超长丢弃的片段: " + "、".join(composition.dropped) + "\n")
    result.content = "".join(lines)
    append_warnings(result, *all_warnings)
    return result


def multi_character_constraint(items: list[Character]) -> str:
    """对应 Go ``multiCharacterConstraint``。"""
    names = [f"{item.name}({item.id})" for item in items if item is not None]
    return (
        "画面必须同时出现以下全部角色："
        + "、".join(names)
        + "。保持每个角色的脸、发型、服装、配色和特征独立，"
        "不要遗漏、合并、替换或交换角色。"
    )


@dataclass
class ImageReferenceSource:
    """一张参考图的来源。对应 Go ``imageReferenceSource``。"""

    source: str = ""
    item: Character | None = None


async def collect_references(
    tool: Any, ctx: Any, args: Any, items: list[Character], viewer: Viewer
) -> list[Any]:
    """收集本次的参考图。对应 Go ``collectReferences``。

    与 Go 的差异：本仓的 ``media.Manager`` / ``Store.ReadImage`` 都是 async，
    因此这一族整体是 async（Go 里是同步的）。
    """
    from atri_x.imagegen import Reference

    sources = reference_sources(args, items)
    if not sources:
        return []
    if not getattr(tool.cfg, "supports_reference", False):
        raise ImageToolMessage(
            "当前生图服务未开启参考图支持（image_generation.supports_reference=false）"
        )
    if len(sources) > MAX_IMAGE_REFERENCES:
        sources = sources[:MAX_IMAGE_REFERENCES]
    out: list[Reference] = []
    for source in sources:
        data, mime_type = await reference_bytes_from_source(
            tool, ctx, source.source, source.item, items, viewer
        )
        if not data:
            continue
        out.append(Reference(data=data, mime_type=mime_type))
    return out


def reference_sources(args: Any, items: list[Character]) -> list[ImageReferenceSource]:
    """对应 Go ``referenceSources``。"""
    sources: list[ImageReferenceSource] = []
    seen: set[str] = set()
    first = first_image_character(items)

    def add(source: str, item: Character | None) -> None:
        source = (source or "").strip()
        if source == "" or source in seen:
            return
        seen.add(source)
        sources.append(ImageReferenceSource(source=source, item=item))

    add(getattr(args, "reference_image", "") or "", first)
    for value in getattr(args, "reference_images", None) or []:
        add(value, first)
    if sources:
        return sources
    for item in items:
        if len(sources) >= MAX_IMAGE_REFERENCES:
            break
        references = getattr(item.image, "references", None) if item is not None else None
        if references:
            add(references[0], item)
    return sources


async def reference_bytes_from_source(
    tool: Any,
    ctx: Any,
    source: str,
    item: Character | None,
    items: list[Character],
    viewer: Viewer,
) -> tuple[bytes, str]:
    """读出一张参考图的字节。对应 Go ``referenceBytesFromSource``。"""
    source = (source or "").strip()
    if source == "":
        return b"", ""
    if source.startswith(ID_PREFIX):
        if not ValidID(source):
            raise ImageToolMessage("参考图 media id 不合法")
        if tool.media is None:
            raise ImageToolMessage("Media Center 未配置")
        try:
            data, meta = await tool.media.read(ctx, source)
        except Exception as exc:  # noqa: BLE001
            raise ImageToolMessage(f"读取参考图失败：{exc}") from exc
        return data, (getattr(meta, "mime_type", "") or "")
    target = item
    image_name = source
    # 支持 character_id:图片名，多角色时用于明确参考图归属。
    if ":" in source and tool.characters is not None and _enabled(tool.characters):
        id_value, _, name = source.partition(":")
        try:
            target = tool.characters.get_visible(ctx, id_value, viewer)
            image_name = name
        except Exception:  # noqa: BLE001 - Go 里取不到就退回原 target
            target = item
            image_name = source
    if target is None and items:
        target = items[0]
    if target is None:
        raise ImageToolMessage(
            "使用角色参考图需要指定 character_id 或先用 @char:<id> 启用角色"
        )
    if tool.characters is None or not _enabled(tool.characters):
        raise ImageToolMessage("角色素材库未配置")
    try:
        data, mime_type = tool.characters.read_image(ctx, target.id, image_name, viewer)
    except Exception as exc:  # noqa: BLE001
        raise ImageToolMessage(f'读取角色图片 "{image_name}" 失败：{exc}') from exc
    return data, mime_type


def first_image_character(items: list[Character]) -> Character | None:
    """对应 Go ``firstImageCharacter``。"""
    for item in items:
        if item is not None:
            return item
    return None


def image_setting(item: Character | None, field_name: str) -> str:
    """取角色的某个生图设置。对应 Go ``imageSetting``。"""
    if item is None:
        return ""
    return str(getattr(item.image, field_name, "") or "")


def ext_for_mime(mime_type: str) -> str:
    """对应 Go ``extForMIME``。"""
    folded = (mime_type or "").strip().lower()
    if folded in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if folded == "image/webp":
        return ".webp"
    return ".png"


def preview_text(text: str) -> str:
    """对应 Go ``previewText``（压空白，超 200 字符截断）。"""
    text = " ".join((text or "").split())
    if len(text) <= 200:
        return text
    return text[:200] + "..."


def image_artifact_name(base: str, timestamp: str, suffix: str, mime_type: str) -> str:
    """按 Go 的 sprintf 形状拼出落盘文件名。对应 Go ``Call`` 里的 ``safeFileName``。"""
    return safe_file_name(f"{base}-{timestamp}{suffix}{ext_for_mime(mime_type)}")


def _enabled(target: Any) -> bool:
    checker = getattr(target, "enabled", None)
    return bool(checker()) if callable(checker) else target is not None


__all__ = [name for name in dir() if not name.startswith("_")]
