"""``image_to_prompt``：把参考图片反推成可直接用于绘图的提示词。

对应 Go：``internal/tool/builtin/image_to_prompt.go``。

``ImagePromptService`` 是工具背后的共享图片描述引擎（本仓由
:class:`atri_x.vision.Service` 实现）；工具本身故意不持有缓存、请求合并或传输细节，
这样内置工具与聊天里的自动兜底共用同一个引擎、同一份缓存和同一套超时/凭据规则。

与 Go 的差异：

* Go 的 ``t.Artists.Service.ExtractImageMetadata`` 在 ``Service`` 为 nil 时会空指针
  崩溃；本仓在缺失时跳过元数据读取并当作"没有元数据"处理（不崩，也不假装读到）。
* Go 用 ``encoding/json`` 的结构体标签序列化输出；Python 侧用同名 dataclass +
  ``to_json()`` 逐字段复刻 ``omitempty``，交给
  :func:`atri_x.tool.builtin.common.go_json` 输出紧凑 JSON。
* ``include_artist`` 在 Go 里是 ``*string``（用来区分"显式传了"和"默认"）；
  Python 侧用 ``None`` 表示未传，语义一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from atri_x.artist import Dialect, Extraction, Metadata, SourceKind, no_metadata_hint
from atri_x.llm.llm import ToolSchema
from atri_x.media import MediaTooLargeError, ValidID, VisionImageInputLimit
from atri_x.tool.builder import Builder, enum, required
from atri_x.tool.builtin.artist_extract import clip_preview
from atri_x.tool.builtin.artist_library import ArtistMatchJSON, artist_matches_json
from atri_x.tool.builtin.artist_tools import (
    ArtistTools,
    artist_error_message,
    artist_viewer,
)
from atri_x.tool.builtin.common import append_warnings, decode_args, go_json
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

#: 反推提示词工具名。对应 Go ``ImageToPromptName``。
IMAGE_TO_PROMPT_NAME = "image_to_prompt"

#: ``include_artist`` 的三种取值。对应 Go ``imageInclude*``。
IMAGE_INCLUDE_NONE = "none"
IMAGE_INCLUDE_METADATA = "metadata"
IMAGE_INCLUDE_RECOMMEND = "recommend"


class ImagePromptService(Protocol):
    """工具背后的共享图片描述引擎。对应 Go ``ImagePromptService``。

    本仓由 :class:`atri_x.vision.Service` 实现（``async def describe(ctx, req)``）；
    工具故意不持有自己的缓存、请求合并或传输细节，这样内置工具与聊天里的自动兜底
    共用同一个引擎、同一份缓存和同一套超时/凭据规则。
    """

    async def describe(self, ctx: Any, req: Any) -> Any:  # pragma: no cover - 协议声明
        ...


def new_image_to_prompt_tool(center: Any, service: Any) -> "ImageToPromptTool":
    """对应 Go ``NewImageToPromptTool``。"""
    return ImageToPromptTool(center, service)


def image_to_prompt_builder() -> Builder:
    """对应 Go ``imageToPromptBuilder``。"""
    return (
        Builder(IMAGE_TO_PROMPT_NAME)
        .description(
            "把参考图片反推成可直接用于绘图的提示词，覆盖主体、外观、服装、姿势、构图、"
            "背景、光线、色彩与画风。会先读取图片自身记录的生成参数（PNG tEXt/iTXt/zTXt、"
            "A1111 parameters、NovelAI、ComfyUI），有记录就把原始画师串和正负向提示词一并回报；"
            "这类参数是图片自己记的，不是从画面猜的。只有在 include_artist=recommend 时才额外"
            "按画风推荐本地画师串库里的组合。不猜测人物身份，也不补充图片里不存在的细节。"
        )
        .risk(RiskLow)
        .tags("image", "prompt", "artist")
    )


@dataclass
class ImageArtistJSON:
    """画师串部分。对应 Go ``imageArtistJSON``。"""

    source: str = ""
    original_artist_string: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "original_artist_string": self.original_artist_string,
        }


@dataclass
class ImageToPromptOutput:
    """``format=json`` 的结构化结果。对应 Go ``imageToPromptOutput``。

    ``original_artist_string`` 在没有记录证据时永远是 ``null``；风格推荐单独汇报，
    绝不写进它。
    """

    positive_prompt: str = ""
    negative_prompt: str = ""
    style_description: str = ""
    style_tags: list[str] = field(default_factory=list)
    artist_extraction: ImageArtistJSON = field(default_factory=ImageArtistJSON)
    recommended_presets: list[ArtistMatchJSON] = field(default_factory=list)
    generation_parameters: dict[str, str] = field(default_factory=dict)
    metadata_source: str = ""
    metadata_origin: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "artist_extraction": self.artist_extraction.to_json(),
            "recommended_presets": [item.to_json() for item in self.recommended_presets],
            "warnings": list(self.warnings),
        }
        for key, value in (
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
class ImageMetadataRecord:
    """图片自己记录下来的东西，与视觉模型的描述分开。对应 Go ``imageMetadataRecord``。

    这个区分就是全部意义所在：只有前者能当作"原始参数"呈现。
    """

    has_artist: bool = False
    artist_original: str = ""
    positive: str = ""
    negative: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    source: str = ""
    recommended: list[ArtistMatchJSON] = field(default_factory=list)
    hint: str = ""


@dataclass
class ImageToPromptArgs:
    """对应 Go ``imageToPromptArgs``。"""

    image: str = ""
    target: str = ""
    # include_artist 用 None 表示"没传"，与 Go 的 *string 等价。
    include_artist: str | None = None
    language: str = ""
    format: str = ""


class ImageToPromptTool:
    """读一张已存图片并交给视觉模型。对应 Go ``ImageToPromptTool``。"""

    def __init__(
        self,
        media: Any = None,
        service: Any = None,
        artists: ArtistTools | None = None,
    ) -> None:
        self.media = media
        self.service = service
        # Artists 提供画师串库。可选：没有它时工具仍能汇报图片自身记录的元数据，
        # 只是无法推荐风格组合。
        self.artists = artists if artists is not None else ArtistTools()

    def name(self) -> str:
        return IMAGE_TO_PROMPT_NAME

    def info_value(self) -> Info:
        return image_to_prompt_builder().build_info()

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(IMAGE_TO_PROMPT_NAME)
            .description("把参考图片反推成绘图提示词，并回报图片元数据里记录的生成参数。")
            .string(
                "image",
                "参考图片的媒体 ID，格式 media:<sha256>；"
                "取当前消息里 [图片 N；媒体 ID：media:...] 的 ID。",
                required(),
            )
            .string(
                "target",
                "提示词面向的绘图模型：general（默认，通用）、sdxl、flux。",
                enum("general", "sdxl", "flux"),
            )
            .string("language", "提示词语言：zh（默认，中文）、en（英文）。", enum("zh", "en"))
            .string(
                "format",
                "输出格式：text（默认，只要提示词）或 json（结构化结果，含画师串、正负向、风格与推荐）。",
                enum("text", "json"),
            )
            .string(
                "include_artist",
                "画师串处理方式：none（不处理）、metadata（默认，只回报图片元数据里记录的"
                "原始画师串）、recommend（额外按画风推荐串库里的组合，会多一次结构化视觉分析）。"
                "显式传这个参数时，即使没有找到参数也会明确说明。",
                enum("none", "metadata", "recommend"),
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        from atri_x.vision import Request as VisionRequest
        from atri_x.vision import (
            image_analysis_prompt,
            image_prompt,
            normalize_image_prompt_language,
            normalize_image_prompt_target,
            parse_image_analysis,
        )

        args = decode_args(req.arguments, ImageToPromptArgs, self.name())
        image_id = (args.image or "").strip()
        if image_id == "":
            return Result(content="image 不能为空，请传入 media:<sha256> 媒体 ID。")
        if not ValidID(image_id):
            return Result(content="image 必须是 media:<sha256> 格式的媒体 ID。")
        if self.service is None:
            return Result(
                content=(
                    "图片反推提示词未配置视觉模型，请在 services.toml 的 "
                    "[image_to_prompt] 设置 provider 和 model。"
                )
            )
        if self.media is None:
            return Result(content="Media Center 未配置，无法读取图片。")
        target = normalize_image_prompt_target(args.target or "")
        language = normalize_image_prompt_language(args.language or "")

        include = IMAGE_INCLUDE_METADATA
        explicit_include = args.include_artist is not None
        if explicit_include:
            include = (args.include_artist or "").strip().lower()
            if include not in (
                IMAGE_INCLUDE_NONE,
                IMAGE_INCLUDE_METADATA,
                IMAGE_INCLUDE_RECOMMEND,
            ):
                return Result(
                    content="include_artist 只能是 none、metadata 或 recommend。"
                )
        structured = (args.format or "").strip().lower() == "json"

        try:
            data, media_item = await self.media.read_limited(
                ctx, image_id, VisionImageInputLimit
            )
        except MediaTooLargeError:
            return Result(
                content=(
                    f"参考图片过大，超过 {VisionImageInputLimit >> 20} MiB 处理上限"
                )
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - Go 把任何读取错误翻成同一句话
            return Result(content="读取图片失败：媒体不存在或已过期")
        mime_type = getattr(media_item, "mime_type", "") or ""

        # 生成参数优先来自入库时留档的记录：超过入库上限的图片已经被重编码成
        # JPEG，原来装着参数的 PNG 文本块在这一步就丢了。
        extraction = Extraction(source=SourceKind.NONE, dialect=Dialect.AUTO)
        metadata = Metadata()
        has_metadata = False
        metadata_origin = ""
        if include != IMAGE_INCLUDE_NONE or structured:
            if self.artists.service is not None:
                found = self.artists.service.extract_image_metadata(image_id, data)
                extraction = found.extraction
                metadata = found.metadata
                has_metadata = found.found
                metadata_origin = found.origin

        # 只有真正需要那些额外字段时才用结构化模板；纯文本调用保持原模板，
        # 这样它的缓存条目继续有效。
        use_structured = structured or include == IMAGE_INCLUDE_RECOMMEND
        vision_text = ""
        analysis = None
        result_warnings: list[str] = []
        try:
            if use_structured:
                described = await self.service.describe(
                    ctx,
                    VisionRequest(
                        media_id=image_id,
                        data=data,
                        mime_type=mime_type,
                        prompt=image_analysis_prompt(target, language),
                    ),
                )
                try:
                    parsed = parse_image_analysis(getattr(described, "text", ""))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - Go 翻成可执行提示
                    return Result(
                        content=(
                            "视觉模型没有返回可解析的结构化分析（"
                            + str(exc)
                            + "）。可以改用 format=text 重试。"
                        )
                    )
                analysis = parsed
                vision_text = parsed.positive_prompt
                result_warnings.extend(parsed.warnings)
            else:
                described = await self.service.describe(
                    ctx,
                    VisionRequest(
                        media_id=image_id,
                        data=data,
                        mime_type=mime_type,
                        prompt=image_prompt(target, language),
                    ),
                )
                vision_text = getattr(described, "text", "")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            return self._error_result(exc, "反推提示词失败：")

        output = ImageToPromptOutput(
            positive_prompt=vision_text,
            negative_prompt=(metadata.negative or "").strip(),
            generation_parameters=dict(metadata.parameters or {}),
            metadata_source=metadata.source,
            metadata_origin=metadata_origin,
            artist_extraction=ImageArtistJSON(source=str(extraction.source)),
        )
        if analysis is not None:
            output.style_description = analysis.style_description
            output.style_tags = list(analysis.style_tags)
            negative = (analysis.negative_prompt or "").strip()
            if negative != "" and output.negative_prompt == "":
                output.negative_prompt = negative
        if extraction.has_original:
            output.artist_extraction.original_artist_string = extraction.original
        # 记录下来的正向提示词是证据，压得过对像素的描述——但只在调用方
        # 确实问了元数据的时候。
        if include != IMAGE_INCLUDE_NONE:
            positive = (metadata.positive or "").strip()
            if positive != "":
                output.positive_prompt = positive
        result_warnings.extend(metadata.warnings or [])

        if include == IMAGE_INCLUDE_RECOMMEND:
            output.recommended_presets = self._recommend_for_image(
                ctx, analysis, result_warnings
            )
        recorded = ImageMetadataRecord(
            source=metadata.source,
            positive=(metadata.positive or "").strip(),
            negative=(metadata.negative or "").strip(),
            parameters=dict(metadata.parameters or {}),
            recommended=output.recommended_presets,
        )
        if extraction.has_original:
            recorded.artist_original = extraction.original
            recorded.has_artist = True
        if include != IMAGE_INCLUDE_NONE and not has_metadata:
            recorded.hint = no_metadata_hint(mime_type)
        if explicit_include and not has_metadata:
            result_warnings.append(recorded.hint)
        output.warnings = result_warnings

        result = Result()
        if structured:
            result.data = go_json(output)
            result.content = result.data
            append_warnings(result, *result_warnings)
            return result

        content = (vision_text or "").strip()
        if include != IMAGE_INCLUDE_NONE:
            section = image_metadata_section(recorded, explicit_include)
            if section != "":
                content = (content + "\n\n" + section).strip()
        result.content = content
        append_warnings(result, *result_warnings)
        return result

    def _recommend_for_image(
        self, ctx: Any, analysis: Any, warnings: list[str]
    ) -> list[ArtistMatchJSON]:
        """按分析出的风格检索串库。对应 Go ``recommendForImage``。"""
        out: list[ArtistMatchJSON] = []
        if analysis is None or not self.artists.enabled():
            return out
        parts: list[str] = []
        value = (getattr(analysis, "style_description", "") or "").strip()
        if value != "":
            parts.append(value)
        parts.extend(list(getattr(analysis, "style_tags", []) or []))
        query = " ".join(parts).strip()
        if query == "":
            return out
        try:
            matches = self.artists.service.recommend(artist_viewer(ctx), query, 3)
        except Exception as exc:  # noqa: BLE001 - Go 只记告警
            warnings.append("画师串库检索失败：" + artist_error_message(exc))
            return out
        return artist_matches_json(matches)

    def _error_result(self, exc: BaseException, prefix: str) -> Result:
        """对应 Go ``imageToPromptErrorResult``：取消/超时按真正的调用失败抛出。"""
        if _is_cancellation(exc):
            raise exc
        return Result(content=prefix + str(exc))


def image_metadata_section(recorded: ImageMetadataRecord, explicit: bool) -> str:
    """渲染图片自身元数据记录的内容。对应 Go ``imageMetadataSection``。

    没有值得说的东西时返回空串，这样对没有参数的图片的普通调用仍然只返回提示词；
    显式传 include_artist 时永远给一个答案，包括"什么都没找到"。
    """
    has_evidence = (
        recorded.has_artist
        or recorded.positive != ""
        or recorded.negative != ""
        or bool(recorded.parameters)
        or recorded.source != ""
        or bool(recorded.recommended)
    )
    if not has_evidence:
        if not explicit:
            return ""
        if recorded.hint != "":
            return "[图片生成参数]\n" + recorded.hint
        return "[图片生成参数]\n没有找到生成参数。"
    lines = ["[图片生成参数]"]
    if recorded.has_artist:
        lines.append(f"画师串（图片元数据记录的原文）：{recorded.artist_original}")
    else:
        lines.append("画师串：图片元数据里没有记录画师标签。")
    if recorded.source != "":
        lines.append(f"元数据格式：{recorded.source}")
    if recorded.positive != "":
        lines.append(f"正向提示词（元数据原文）：{clip_preview(recorded.positive, 600)}")
    if recorded.parameters:
        keys = sorted(recorded.parameters)
        parts = [f"{key}={recorded.parameters[key]}" for key in keys]
        lines.append("生成参数：" + ", ".join(parts))
    if recorded.negative != "":
        lines.append(f"负向提示词：{clip_preview(recorded.negative, 300)}")
    if recorded.recommended:
        lines.append("串库里画风相近的组合：")
        for match in recorded.recommended:
            line = f"- {match.id}（{match.title}，{match.scope}）"
            if match.reason != "":
                line += f"：{match.reason}"
            lines.append(line)
        lines.append(
            "推荐是按画面风格匹配的，不是原始画师串还原；"
            "要使用请用 image_generate 的 artist_preset_id 引用。"
        )
    return "\n".join(lines).rstrip("\n")


def _is_cancellation(exc: BaseException) -> bool:
    """对应 Go 的 ``errors.Is(err, context.Canceled|DeadlineExceeded)``。"""
    try:
        from atri_x.vision import is_cancellation

        return bool(is_cancellation(exc))
    except Exception:  # pragma: no cover - vision 一定存在，这里只是兜底
        return isinstance(exc, (asyncio.CancelledError, TimeoutError))


__all__ = [name for name in dir() if not name.startswith("_")]
