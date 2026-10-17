"""``artist_extract``：从提示词文本或图片自身的生成参数里提取画师串。

对应 Go：``internal/tool/builtin/artist_extract.go``。

与串库工具不同，这个工具**永远可用**：从文本里解析画师串、读图片自带的生成参数
都不需要串库，只有"风格相近推荐"那一步才需要。

与 Go 的差异：

* Go 用 ``encoding/json`` 的结构体标签序列化 ``artistExtractOutput``；Python 侧用
  同名 dataclass + ``to_json()`` 逐字段复刻 ``omitempty``，交给
  :func:`atri_x.tool.builtin.common.go_json` 输出紧凑 JSON。
* Go 的 ``warnings`` 为 nil 时序列化成 ``null``；这里固定输出 ``[]``（对调用方
  更友好，且 ``json.loads`` 后都是"空列表"这一语义）。
* ``media.Manager.ReadLimited`` 在 Python 侧是 async，因此读图与视觉分析都在
  ``async def call`` 内部 await。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from atri_x.artist import (
    Dialect,
    Extraction,
    Metadata,
    MetadataOriginArchive,
    SourceKind,
    compile_raw,
    format_weight,
    no_metadata_hint,
    normalize_dialect,
)
from atri_x.llm.llm import ToolSchema
from atri_x.media import MediaTooLargeError, ValidID, VisionImageInputLimit
from atri_x.tool.builder import enum
from atri_x.tool.builtin.artist_library import ArtistMatchJSON, artist_matches_json
from atri_x.tool.builtin.artist_tools import (
    ARTIST_EXTRACT_NAME,
    ArtistTools,
    artist_builder,
    artist_error_message,
    artist_viewer,
)
from atri_x.tool.builtin.common import append_warnings, decode_args, go_json
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

#: Go 的 ``media.ValidID`` 别名，便于按 Go 名交叉检索。
_valid_media_id = ValidID


def new_artist_extract_tool(deps: ArtistTools) -> "ArtistExtractTool":
    """对应 Go ``NewArtistExtractTool``。"""
    return ArtistExtractTool(deps)


@dataclass
class ArtistRefJSON:
    """提取结果里的一个画师标签。对应 Go ``artistRefJSON``。"""

    id: str = ""
    name: str = ""
    raw: str = ""
    weight: float = 0.0

    def to_json(self) -> dict[str, Any]:
        # id 带 omitempty，其余三个没有。
        out: dict[str, Any] = {"name": self.name, "raw": self.raw, "weight": self.weight}
        if self.id:
            out["id"] = self.id
        return out


@dataclass
class ArtistExtractOutput:
    """结构化的提取结果。对应 Go ``artistExtractOutput``。

    ``original_artist_string`` 故意是可空值：``null`` 表示"没有留下证据"，
    与"画师串是空的"是两个不同的答案。推荐结果永远不能并进这个字段。
    """

    source: str = ""
    source_label: str = ""
    dialect: str = ""
    original_artist_string: str | None = None
    artist_string: str = ""
    artists: list[ArtistRefJSON] = field(default_factory=list)
    remaining_prompt: str = ""
    positive_prompt: str = ""
    negative_prompt: str = ""
    style_description: str = ""
    style_tags: list[str] = field(default_factory=list)
    recommended_presets: list[ArtistMatchJSON] = field(default_factory=list)
    generation_parameters: dict[str, str] = field(default_factory=dict)
    metadata_source: str = ""
    metadata_origin: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "source_label": self.source_label,
            "dialect": self.dialect,
            "original_artist_string": self.original_artist_string,
            "artist_string": self.artist_string,
            "artists": [item.to_json() for item in self.artists],
            "recommended_presets": [item.to_json() for item in self.recommended_presets],
            "warnings": list(self.warnings),
        }
        for key, value in (
            ("remaining_prompt", self.remaining_prompt),
            ("positive_prompt", self.positive_prompt),
            ("negative_prompt", self.negative_prompt),
            ("style_description", self.style_description),
            ("style_tags", self.style_tags),
            ("generation_parameters", self.generation_parameters),
            ("metadata_source", self.metadata_source),
            ("metadata_origin", self.metadata_origin),
        ):
            if value:
                out[key] = value
        return out


@dataclass
class ArtistExtractArgs:
    """对应 Go ``artistExtractArgs``。"""

    text: str = ""
    image: str = ""
    dialect: str = ""
    visual_fallback: bool = False
    format: str = ""


class ArtistExtractTool:
    """对应 Go ``ArtistExtractTool``。"""

    def __init__(self, deps: ArtistTools | None = None) -> None:
        self.deps = deps if deps is not None else ArtistTools()

    def name(self) -> str:
        return ARTIST_EXTRACT_NAME

    def info_value(self) -> Info:
        return (
            artist_builder(
                ARTIST_EXTRACT_NAME,
                "从提示词文本或图片自身的生成参数里提取画师串，保留画师顺序、权重和原文；"
                "也支持从图片元数据读出完整正向/负向提示词和生成参数。默认不调用视觉模型："
                "有元数据就返回记录下来的原文，没有元数据就明确说明无法取得原始串。"
                "只有 visual_fallback=true 时才额外调用视觉模型分析画风，并按本地画师串库"
                "给出风格相近的推荐（推荐结果不是原始画师串还原）。",
            )
            .risk(RiskLow)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            artist_builder(ARTIST_EXTRACT_NAME, "从文本或图片元数据提取画师串。")
            .string(
                "text",
                "完整提示词文本，例如 \"1girl, artist:artist_a, (artist_b:1.2), "
                "soft lighting\"。与 image 二选一。",
            )
            .string(
                "image",
                "图片媒体 ID（media:<sha256>），用于读取图片自身的生成参数。与 text 二选一。",
            )
            .string(
                "dialect",
                "按指定方言渲染画师串：natural、sd_weighted、nai、tags；"
                "不填（auto）则原样返回。",
            )
            .boolean(
                "visual_fallback",
                "默认 false。没有元数据时是否调用视觉模型分析画风并推荐串库组合；"
                "会消耗视觉额度。",
            )
            .string(
                "format",
                "输出格式：text（默认，便于阅读）或 json（结构化结果）。",
                enum("text", "json"),
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, ArtistExtractArgs, self.name())
        text = (args.text or "").strip()
        image_id = (args.image or "").strip()
        if text == "" and image_id == "":
            return Result(content="需要 text（提示词文本）或 image（media:<sha256>）其中之一。")
        if text != "" and image_id != "":
            return Result(
                content=(
                    "text 和 image 只能二选一：文本提取看的是你给的串，"
                    "图片提取看的是图片自己记的参数。"
                )
            )

        extraction = Extraction()
        metadata = Metadata()
        meta_origin = ""
        style = None
        warnings: list[str] = []
        dialect = normalize_dialect(args.dialect or "")

        if text != "":
            extraction = self.deps.service.extract_text(text)
        else:
            if not _valid_media_id(image_id):
                return Result(content="image 必须是 media:<sha256> 格式的媒体 ID。")
            if self.deps.media is None:
                return Result(content="Media Center 未配置，无法读取图片。")
            try:
                data, media_item = await self.deps.media.read_limited(
                    ctx, image_id, VisionImageInputLimit
                )
            except MediaTooLargeError:
                return Result(
                    content=(
                        "参考图片过大，超过 "
                        f"{VisionImageInputLimit >> 20} MiB 处理上限。"
                    )
                )
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - Go 把任何读取错误都翻成同一句话
                return Result(content="读取图片失败：媒体不存在或已过期。")
            mime_type = getattr(media_item, "mime_type", "") or ""
            # 一次调用同时定下参数与它们的来源：问两次的话，中途的并发入库可能
            # 把字节解析出来的参数标成留档。
            found = self.deps.service.extract_image_metadata(image_id, data)
            extraction = found.extraction
            metadata = found.metadata
            meta_origin = found.origin
            if not found.found:
                warnings.append(no_metadata_hint(mime_type))
            if args.visual_fallback:
                analysis, analysis_warnings = await self._analyse_image(
                    ctx, image_id, data, mime_type
                )
                warnings.extend(analysis_warnings)
                if analysis is not None:
                    style = analysis

        # source 说明画师串从哪来。元数据读取成功时即使也跑了风格分析，仍然保留
        # 它的来源：还原出来的串优先于猜测，绝不能被重新贴标签。
        if not extraction.artists and style is not None:
            extraction.source = SourceKind.VISUAL
            extraction.has_original = False
            extraction.original = ""
        if extraction.source == "":
            extraction.source = SourceKind.NONE

        artist_string = extraction.original
        if extraction.artists and dialect != Dialect.AUTO:
            compiled = compile_raw(extraction.original, dialect, self.deps.service.resolver())
            artist_string = compiled.positive
            warnings.extend(compiled.warnings)

        output = ArtistExtractOutput(
            source=str(extraction.source),
            source_label=extraction.source.label(),
            dialect=str(dialect),
            artist_string=artist_string,
            remaining_prompt=extraction.remaining,
            negative_prompt=metadata.negative,
            style_tags=list(getattr(style, "style_tags", []) or []),
            style_description=getattr(style, "style_description", "") or "",
            generation_parameters=dict(metadata.parameters or {}),
            metadata_source=metadata.source,
            metadata_origin=meta_origin,
        )
        if extraction.has_original:
            output.original_artist_string = extraction.original
        for ref in extraction.artists:
            output.artists.append(
                ArtistRefJSON(id=ref.id, name=ref.name, raw=ref.raw, weight=ref.weight)
            )
        positive = (metadata.positive or "").strip()
        if positive != "":
            output.positive_prompt = positive
        else:
            sample = (extraction.remaining or "").strip()
            if sample != "":
                # 没有元数据时，剩余文本就是调用方自己的提示词，仍然是
                # "提示词是什么"能给出的最好答案。
                output.positive_prompt = sample

        output.recommended_presets = self._recommend_for(
            ctx, extraction, style, warnings
        )
        output.warnings = warnings

        result = Result(data=go_json(output))
        if (args.format or "").strip().lower() == "json":
            result.content = result.data if result.data != "" else "序列化提取结果失败。"
        else:
            result.content = artist_extract_text(output)
        append_warnings(result, *output.warnings)
        return result

    async def _analyse_image(
        self, ctx: Any, media_id: str, data: bytes, mime_type: str
    ) -> tuple[Any, list[str]]:
        """跑结构化分析模板。没配视觉模型或调用失败时返回 ``(None, 告警)``。

        对应 Go ``analyseImage``：调用方仍能只靠元数据回答，所以这里不抛异常。
        """
        if self.deps.vision is None:
            return None, [
                "未配置视觉模型，无法做风格分析；已只返回元数据结果。"
                "可在 services.toml 的 [image_to_prompt] 或 [vision] 配置 provider 和 model。"
            ]
        try:
            from atri_x.vision import Request as VisionRequest
            from atri_x.vision import image_analysis_prompt, parse_image_analysis

            result = await self.deps.vision.describe(
                ctx,
                VisionRequest(
                    media_id=media_id,
                    data=data,
                    mime_type=mime_type,
                    prompt=image_analysis_prompt("general", "zh"),
                ),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - Go 把失败翻成告警
            if _is_cancellation(exc):
                return None, ["视觉风格分析被取消或超时：" + str(exc)]
            return None, ["视觉风格分析失败：" + str(exc)]
        try:
            parsed = parse_image_analysis(getattr(result, "text", ""))
        except BaseException as exc:  # noqa: BLE001
            return None, ["视觉风格分析结果无法解析：" + str(exc)]
        return parsed, []

    def _recommend_for(
        self, ctx: Any, extraction: Extraction, style: Any, warnings: list[str]
    ) -> list[ArtistMatchJSON]:
        """按还原出来的画师名或分析出的风格检索串库。对应 Go ``recommendFor``。

        串库没配置、或者检索失败，都只记一条告警，而不是让整次提取失败。
        """
        out: list[ArtistMatchJSON] = []
        if not self.deps.enabled():
            return out
        parts: list[str] = []
        if style is not None:
            value = (getattr(style, "style_description", "") or "").strip()
            if value != "":
                parts.append(value)
            parts.extend(getattr(style, "style_tags", []) or [])
        if not parts:
            parts.extend(extraction.names())
        query = " ".join(parts).strip()
        if query == "":
            return out
        try:
            matches = self.deps.service.recommend(artist_viewer(ctx), query, 3)
        except Exception as exc:  # noqa: BLE001 - Go 只记告警
            warnings.append("画师串库检索失败：" + artist_error_message(exc))
            return out
        return artist_matches_json(matches)


def artist_extract_text(output: ArtistExtractOutput) -> str:
    """人类可读形式。对应 Go ``artistExtractText``。"""
    lines = [f"来源：{output.source_label}"]
    if output.source == str(SourceKind.METADATA):
        lines.append("这是图片元数据里记录的参数，属于原始证据。")
    elif output.source == str(SourceKind.TEXT):
        lines.append("这是从你给的文本里解析出来的，顺序和权重都按原文保留。")
    elif output.source == str(SourceKind.VISUAL):
        lines.append(
            "这是按画面风格做的推荐，不是原始画师串还原；"
            "同一画风可能来自不同模型、LoRA 或后期处理。"
        )
    if output.original_artist_string is not None:
        lines.append(f"原始画师串：{output.original_artist_string}")
    else:
        lines.append("原始画师串：无法取得（没有记录该参数的证据）")
    if output.artist_string != "" and output.artist_string != (
        output.original_artist_string or ""
    ):
        lines.append(f"按方言渲染：{output.artist_string}")
    if output.artists:
        names = [
            f"{item.name}({format_weight(item.weight)})" for item in output.artists
        ]
        lines.append(f"画师（{len(output.artists)}）：" + "、".join(names))
    if output.metadata_source != "":
        lines.append(f"元数据格式：{output.metadata_source}")
    if output.metadata_origin == MetadataOriginArchive:
        lines.append("参数来自入库留档：图片本体被转码后已不再携带这些参数。")
    if output.positive_prompt != "":
        lines.append(f"正向提示词：{clip_preview(output.positive_prompt, 600)}")
    if output.negative_prompt != "":
        lines.append(f"负向提示词：{clip_preview(output.negative_prompt, 300)}")
    if output.style_description != "":
        lines.append(f"风格描述：{output.style_description}")
    if output.style_tags:
        lines.append("风格关键词：" + ", ".join(output.style_tags))
    if output.generation_parameters:
        keys = sorted(output.generation_parameters)
        parts = [f"{key}={output.generation_parameters[key]}" for key in keys]
        lines.append("生成参数：" + ", ".join(parts))
    if output.recommended_presets:
        lines.append("串库里风格相近的组合：")
        for match in output.recommended_presets:
            line = f"- {match.id}（{match.title}，{match.scope}）"
            if match.reason != "":
                line += f"：{match.reason}"
            lines.append(line)
        lines.append(
            "要使用某个组合，把它保存/确认后用 image_generate 的 artist_preset_id 引用。"
        )
    elif output.source == str(SourceKind.VISUAL):
        lines.append("串库里没有相近的组合；可以把这个画风用 artist_library_save 存成新组合。")
    return "\n".join(lines).rstrip("\n")


def deref_string(value: str | None) -> str:
    """对应 Go ``derefString``。"""
    return value or ""


def clip_preview(text: str, limit: int) -> str:
    """为展示缩短字段，不动结构化结果。对应 Go ``clipPreview``。"""
    text = " ".join((text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "..."


def _is_cancellation(exc: BaseException) -> bool:
    """对应 Go 的 ``errors.Is(err, context.Canceled|DeadlineExceeded)``。"""
    try:
        from atri_x.vision import is_cancellation

        return bool(is_cancellation(exc))
    except Exception:  # pragma: no cover - vision 一定存在，这里只是兜底
        return isinstance(exc, (asyncio.CancelledError, TimeoutError))


__all__ = [name for name in dir() if not name.startswith("_")]
