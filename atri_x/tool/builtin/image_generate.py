"""``image_generate``：调用 OpenAI 兼容的生图端点并组装最终提示词。

对应 Go：``internal/tool/builtin/image_generate.go``（前半部分：工具本体、批量生图
与结果封装）；提示词编译与参考图收集拆到
:mod:`atri_x.tool.builtin.image_generate_compose`，自动选角与上下文拆到
:mod:`atri_x.tool.builtin.image_generate_context`。

最终提示词 = 全局预设 + 所有已索引角色的图片预设 + （可选的）语义改写后的场景描述。
画师串在改写**之后**才编译进来，因此改写器永远看不到它、改不动它、也丢不掉它的权重。

与 Go 的差异：

* Go 的 ``Call`` 是值接收者，``t.client = profile.Client; t.cfg = profile.Config``
  只改本地副本；Python 的 ``self`` 是共享的，所以这里构造一个
  :class:`_CallView` 浅视图承载本次调用的 ``client`` / ``cfg``，其余属性转发给工具
  实例。这样跨调用不会串 profile。
* Go 的 goroutine + channel 批量并发生成改成 ``asyncio`` 任务 + 信号量。
* Go 的 ``concurrency.ErrFull`` / ``ErrTimeout`` 是哨兵错误，用 ``is`` 判定。
* 时间戳 ``time.Now().Format("20060102-150405")`` → ``strftime("%Y%m%d-%H%M%S")``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atri_x.character import Store
from atri_x.delivery import KindImage, Output, Source
from atri_x.errors import AtriXError
from atri_x.imagegen import (
    Client,
    Config,
    OptimizeOptions,
    ProfileFrom,
    Request,
    prompt_lib,
)
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText, ToolSchema
from atri_x.media import Input as MediaInput
from atri_x.ops.concurrency import ErrFull, ErrTimeout
# safety-guard: 提示词审核 + 图片出口（guard 由 agent 挂到工具 ctx 上）
from atri_x.safety import guard_from_context, scope_from_context
from atri_x.tool.builder import Builder, enum, required
from atri_x.tool.builtin.artist_tools import ArtistTools, artist_viewer
from atri_x.tool.builtin.character import CHARACTER_READ_NAME, character_viewer
from atri_x.tool.builtin.common import append_warnings, decode_args
from atri_x.tool.builtin.image_generate_compose import (
    MAX_IMAGE_BATCH_COUNT,
    collect_references,
    compose_image_request,
    first_image_character,
    image_artifact_name,
    image_dry_run_result,
    image_prompt_dialect,
    image_protected_block_names,
    image_setting,
    preview_text,
    resolve_artist_block,
)
from atri_x.tool.builtin.image_generate_context import (
    ImagePromptRewriteRequest,
    format_image_context,
    image_character_names,
    image_character_prompts,
    image_character_summary,
    image_context_query,
    resolve_image_characters,
    search_image_context,
    should_rewrite_image_prompt,
)
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

#: 工具名。对应 Go ``ImageGenerateName``。
IMAGE_GENERATE_NAME = "image_generate"

#: 单次最多几张（与 compose 模块共用）。对应 Go ``maxImageBatchCount``。
MAX_IMAGE_BATCH_WORKERS = 4

#: 未显式传 count 时的张数。对应 Go ``defaultImageBatchSize``。
DEFAULT_IMAGE_BATCH_SIZE = 1

#: 提示词库检索的提示条数。对应 Go ``Call`` 里的 ``LibraryHints(scene, 3)``。
PROMPT_LIBRARY_HINT_LIMIT = 3


@dataclass
class ImageProfile:
    """一个具名的生图端点。对应 Go ``ImageProfile``。"""

    name: str = ""
    client: Client | None = None
    config: Config = field(default_factory=Config)


@dataclass
class ImageGenerateArgs:
    """对应 Go ``imageGenerateArgs``。

    Go 的 ``save_to_character`` / ``send`` / ``optimize`` / ``rewrite`` / ``dry_run``
    是 ``*bool``；Python 用 ``None`` 表示"没传"，语义一致。
    """

    prompt: str = ""
    character_id: str = ""
    character_ids: list[str] = field(default_factory=list)
    size: str = ""
    quality: str = ""
    reference_image: str = ""
    reference_images: list[str] = field(default_factory=list)
    count: int = 0
    save_to_character: bool | None = None
    send: bool | None = None
    optimize: bool | None = None
    term_mode: str = ""
    profile: str = ""
    mode: str = ""
    character_query: str = ""
    context_query: str = ""
    context_limit: int = 0
    rewrite: bool | None = None
    artist_preset_id: str = ""
    artist_string: str = ""
    artist_mode: str = ""
    negative_prompt: str = ""
    dry_run: bool | None = None


class _CallView:
    """本次调用的浅视图：只覆盖 ``client`` / ``cfg``，其余属性转发给工具实例。

    对应 Go ``Call`` 的值接收者语义（``t.client`` / ``t.cfg`` 的赋值不影响原实例）。
    """

    __slots__ = ("_tool", "client", "cfg")

    def __init__(self, tool: "ImageGenerateTool", client: Any, cfg: Config) -> None:
        self._tool = tool
        self.client = client
        self.cfg = cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)


class ImageGenerateTool:
    """对应 Go ``ImageGenerateTool``。"""

    def __init__(
        self,
        profiles: dict[str, ImageProfile] | None = None,
        default_profile: str = "",
        characters: Store | None = None,
        center: Any = None,
        history: Any = None,
        rewriter: Any = None,
        limiter: Any = None,
        artists: ArtistTools | None = None,
    ) -> None:
        self.profiles = dict(profiles or {})
        self.default_profile = (default_profile or "").strip()
        self.characters = characters
        self.media = center
        self.history = history
        self.rewriter = rewriter
        self.limiter = limiter
        self.client: Client | None = None
        self.cfg = Config()
        profile = self.profiles.get(self.default_profile)
        if profile is not None:
            self.client = profile.client
            self.cfg = profile.config
        # 画师串库可选：没有它时工具仍能组装提示词，只是展不开画师串。
        self.artists = artists if artists is not None else ArtistTools()

    def name(self) -> str:
        return IMAGE_GENERATE_NAME

    def info_value(self) -> Info:
        builder = (
            Builder(IMAGE_GENERATE_NAME)
            .description(
                "调用生图服务（OpenAI 兼容 images/generations，默认 GPT Image 2.5）生成图片。"
                "最终提示词 = 全局预设 + 所有已索引角色的图片预设 + 提示词优化后的场景描述；"
                "消息里用 @char:<id> 启用多个角色、或调用时传 character_ids，"
                "会自动把多个角色放进同一张图。mode=auto（默认）会从 prompt 自动识别多个"
                "角色名/别名、按需拉当前群聊上下文、并在 prompt 太短或含糊时用低成本模型做"
                "语义改写；内置提示词库会自动补充用途比例、画风锚点和负面词。除非显式传 "
                "count>1，否则一次调用只生成一张包含全部索引角色的图。"
            )
            .risk(RiskLow)
            .tags("image", "character")
            .depends_on(
                CHARACTER_READ_NAME,
                "send_file",
                "prompt_library_search",
                "search_chat_history",
            )
        )
        if getattr(self.cfg, "superadmin_only", False):
            builder = builder.superadmin_only()
        return builder.build_info()

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(IMAGE_GENERATE_NAME)
            .description("生成图片。默认一张图包含所有已索引角色。")
            .string(
                "prompt",
                "画面/场景描述。不要重复角色外貌，角色预设会自动拼接。",
                required(),
            )
            .string(
                "character_id",
                "可选单个角色 id；也支持 auto（按 character_query 或 prompt 自动选角）。",
            )
            .string_array(
                "character_ids",
                "可选多个角色 id；和本轮 @char:<id> 一起使用。"
                "除非传 count>1，否则所有角色会画进同一张图。",
            )
            .string(
                "size",
                "可选尺寸，例如 1024x1024、1536x1024、1024x1536；不填用默认。",
            )
            .string("quality", "可选画质：low、medium、high；不填用默认。")
            .string(
                "reference_image",
                "可选单张参考图：media:<sha256> 或角色图片名（如 avatar.png）。"
                "多角色时可用 <character_id>:<图片名> 指定归属。需服务端支持。",
            )
            .string_array(
                "reference_images",
                "可选多张参考图，每张为 media:<sha256> 或 <character_id>:<图片名>；"
                "需服务端支持多图参考。",
            )
            .integer(
                "count",
                "可选，强制生成的图片张数，默认 1，单次最多 4。"
                "每一张都会包含全部已索引角色。",
            )
            .boolean("save_to_character", "可选，是否把结果写回角色 images/；默认跟随配置。")
            .boolean("send", "可选，生成后是否直接发送到当前聊天；默认 false。")
            .boolean("optimize", "可选，是否用内置提示词库优化 prompt；默认跟随配置。")
            .string(
                "term_mode",
                "可选，phrase 或 tag；tag 会追加单个单词的 tag 串。默认跟随配置。",
            )
            .string(
                "profile",
                "可选，生图配置 profile 名（[image_generation.profiles.<name>]）；"
                "不填时用本轮 @image: 声明的或默认 profile。",
            )
            .string(
                "mode",
                "可选，auto（默认，自动选角/拉群聊上下文/按需改写）或 manual（只用显式参数）。",
            )
            .string("character_query", "可选，自动选角时的检索词；不填时用 prompt。")
            .string(
                "context_query",
                "可选，拉取当前群聊上下文的关键词；auto 表示从 prompt 自动提取，off 关闭。",
            )
            .integer("context_limit", "可选，拉取群聊消息条数，默认 6。")
            .boolean("rewrite", "可选，是否用低成本模型做语义改写；默认跟随配置。")
            .string(
                "artist_preset_id",
                "可选，指定一个画师串组合（来自 artist_library_search）。"
                "组合会作为独立片段拼进最终提示词，且不会被语义改写改动。",
            )
            .string(
                "artist_string",
                "可选，本次临时使用的画师串原文；顺序和权重原样保留，"
                "比 artist_preset_id 优先。",
            )
            .string(
                "artist_mode",
                "可选：off（默认，不加画师串）、explicit（用 artist_preset_id / "
                "artist_string 或配置的默认组合）、auto（按场景从串库自动选一个已确认的组合）。",
                enum("off", "explicit", "auto"),
            )
            .string(
                "negative_prompt",
                "可选，本次追加的负面提示词；会与配置和角色的负面词一起按 profile 的 "
                "negative_prompt_mode 传递。",
            )
            .boolean(
                "dry_run",
                "可选，只编译并显示最终提示词、负向提示词、画师串和方言，"
                "不调用生图接口、不保存、不发送、不计生图额度。",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, ImageGenerateArgs, self.name())
        if (args.prompt or "").strip() == "":
            return Result(content="prompt 不能为空。")
        client = self.client
        cfg = self.cfg
        if self.profiles:
            profile_name = (args.profile or "").strip()
            if profile_name == "":
                profile_name = ProfileFrom(ctx)
            if profile_name == "":
                profile_name = self.default_profile
            profile = self.profiles.get(profile_name)
            if profile is None:
                return Result(content="没有配置生图 profile：" + profile_name)
            # 默认 profile 已经被 Info().SuperadminOnly 在执行器那层拦过，
            # 只有额外 profile 需要在这里再查一次。
            if (
                profile_name != self.default_profile
                and getattr(profile.config, "superadmin_only", False)
                and not character_viewer(ctx).superadmin
            ):
                return Result(content="生图 profile " + profile_name + " 仅超级管理员可用。")
            client = profile.client
            cfg = profile.config
        tool = _CallView(self, client, cfg)
        if tool.client is None or not tool.client.enabled():
            return Result(
                content="生图服务未启用。请在 app.toml 配置 [image_generation] 并设置 API Key。"
            )
        viewer = character_viewer(ctx)
        mode = (args.mode or "").strip().lower() or "auto"
        auto = mode == "auto"
        try:
            items = resolve_image_characters(tool, ctx, args, args.prompt, viewer, auto)
        except BaseException as exc:  # noqa: BLE001 - Go 把错误翻成结果正文
            return Result(content=str(exc))

        context_text = ""
        context_count = 0
        query, want = image_context_query(tool, args, args.prompt, auto)
        if want:
            rows = search_image_context(tool, ctx, query, _as_int(args.context_limit))
            if rows:
                context_text = format_image_context(rows)
                context_count = len(rows)

        scene = (args.prompt or "").strip()
        rewrite_model = ""
        if should_rewrite_image_prompt(tool, args, scene, context_text) and tool.rewriter is not None:
            hints = ""
            try:
                hints = prompt_lib().library_hints(scene, PROMPT_LIBRARY_HINT_LIMIT)
            except BaseException:  # noqa: BLE001 - Go 里库加载失败就不给提示
                hints = ""
            request = ImagePromptRewriteRequest(
                scene=scene,
                character_name=image_character_names(items),
                character_prompt=image_character_prompts(items),
                context_text=context_text,
                library_hints=hints,
                max_runes=int(getattr(tool.cfg, "max_prompt_runes", 0) or 0),
                target_model=str(getattr(tool.cfg, "model", "") or ""),
                target_dialect=str(image_prompt_dialect(tool)),
                model_family=str(getattr(tool.cfg, "model_family", "") or ""),
                protected_blocks=image_protected_block_names(args),
            )
            try:
                value = await tool.rewriter.rewrite_image_prompt(ctx, request)
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - Go 里改写失败就退回原文
                value = ""
            if (value or "").strip() != "":
                scene = value.strip()
                rewrite_model = str(getattr(tool.cfg, "optimize_rewrite_model", "") or "")
        if context_text != "" and rewrite_model == "":
            scene = (scene + "\n\n" + context_text).strip()

        optimizer_negative = ""
        optimizer_match = ""
        optimize = (
            str(getattr(tool.cfg, "optimize", "") or "").strip().lower() == "rules"
        )
        if args.optimize is not None:
            optimize = bool(args.optimize)
        if optimize:
            try:
                library = prompt_lib()
            except BaseException:  # noqa: BLE001
                library = None
            if library is not None:
                term_mode = str(getattr(tool.cfg, "optimize_term_mode", "") or "")
                if (args.term_mode or "").strip() != "":
                    term_mode = args.term_mode
                optimized = library.optimize(
                    scene,
                    OptimizeOptions(
                        max_anchors=int(getattr(tool.cfg, "optimize_max_anchors", 0) or 0),
                        max_negatives=int(getattr(tool.cfg, "optimize_max_negatives", 0) or 0),
                        max_added_runes=int(
                            getattr(tool.cfg, "optimize_max_added_runes", 0) or 0
                        ),
                        max_tags=int(getattr(tool.cfg, "optimize_max_tags", 0) or 0),
                        term_mode=term_mode,
                    ),
                )
                scene = optimized.prompt
                optimizer_negative = optimized.negative
                optimizer_match = optimized.matched

        first = first_image_character(items)
        size = _first_non_empty(
            [args.size, image_setting(first, "size"), str(getattr(tool.cfg, "size", "") or "")]
        )
        quality = _first_non_empty(
            [
                args.quality,
                image_setting(first, "quality"),
                str(getattr(tool.cfg, "quality", "") or ""),
            ]
        )

        try:
            references = await collect_references(tool, ctx, args, items, viewer)
        except BaseException as exc:  # noqa: BLE001
            return Result(content=str(exc))

        # 画师串在改写之后才编译，所以改写器永远看不到它、改不动它、也丢不掉权重。
        # 用户钉死的东西必须原封不动地穿过模型调用。
        dialect = image_prompt_dialect(tool)
        artist_block, artist_used, artist_warnings = resolve_artist_block(
            tool, ctx, args, scene, artist_viewer(ctx)
        )

        try:
            composition = compose_image_request(
                tool,
                items,
                scene,
                artist_block.positive,
                optimizer_negative,
                args.negative_prompt or "",
                len(references) > 0,
            )
        except BaseException as exc:  # noqa: BLE001
            return Result(content=str(exc))

        count = _as_int(args.count)
        if count <= 0:
            count = DEFAULT_IMAGE_BATCH_SIZE
        requested_count = count
        if count > MAX_IMAGE_BATCH_COUNT:
            count = MAX_IMAGE_BATCH_COUNT

        if args.dry_run:
            return image_dry_run_result(
                tool,
                composition,
                dialect,
                artist_block,
                artist_used,
                size,
                quality,
                count,
                artist_warnings,
            )

        # safety-guard: 真正请求 provider 之前，先审核拼接好的提示词。
        # 命中词表类且处于 enforce 时直接失败（不发起请求），audit 只记审计。
        safety_guard = guard_from_context(ctx)
        if safety_guard is not None:
            safety_scope = scope_from_context(ctx)
            positive, positive_decision = safety_guard.filter_text(
                composition.positive,
                stage="image_prompt",
                scope=safety_scope,
                meta={"tool": self.name()},
            )
            negative, negative_decision = safety_guard.filter_text(
                composition.negative,
                stage="image_prompt",
                scope=safety_scope,
                meta={"tool": self.name()},
            )
            blocked = [
                decision
                for decision in (positive_decision, negative_decision)
                if decision.action == "block" and safety_guard.enforcing
            ]
            if blocked:
                return Result(
                    content="内容安全策略拒绝：生图提示词不合规，本次未发起生成请求。"
                )
            if positive != composition.positive or negative != composition.negative:
                composition.positive = positive
                composition.negative = negative

        request = Request(
            prompt=composition.positive,
            negative_prompt=composition.negative,
            size=size,
            quality=quality,
            references=references,
        )
        results, generation_errors = await generate_images(tool, ctx, request, count)

        save_to_character = bool(getattr(tool.cfg, "save_to_character", False))
        if args.save_to_character is not None:
            save_to_character = bool(args.save_to_character)
        base = items[0].id if items else "image"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stored_results: list[Any] = []
        stored_media: list[Any] = []
        stored_names: list[str] = []
        failures: list[str] = []
        writeback_errors: list[str] = []
        for index, result in enumerate(results):
            if result is None:
                if index < len(generation_errors) and generation_errors[index] is not None:
                    failures.append(
                        f"第 {index + 1} 张：{image_generate_error_message(generation_errors[index])}"
                    )
                continue
            suffix = f"-{index + 1:02d}" if count > 1 else ""
            name = image_artifact_name(base, timestamp, suffix, result.mime_type)
            try:
                stored = await store_image(tool, ctx, result.data, result.mime_type, name)
            except BaseException as exc:  # noqa: BLE001
                failures.append(f"第 {index + 1} 张保存失败：{exc}")
                continue
            stored_results.append(result)
            stored_media.append(stored)
            stored_names.append(name)
            if (
                save_to_character
                and items
                and tool.characters is not None
                and _enabled(tool.characters)
            ):
                try:
                    tool.characters.add_image(
                        ctx, items[0].id, name, result.mime_type, stored.id, result.data, viewer
                    )
                except BaseException as exc:  # noqa: BLE001
                    writeback_errors.append(f"{name}：{exc}")
        if not stored_media:
            message = "生图失败。"
            if failures:
                message += "\n" + "\n".join(failures)
            return Result(content=message)

        lines: list[str] = []
        if requested_count > 1:
            lines.append(f"已生成 {len(stored_media)}/{requested_count} 张图片。\n")
        else:
            lines.append("已生成图片。\n")
        if requested_count > count:
            lines.append(f"单次最多生成 {MAX_IMAGE_BATCH_COUNT} 张，本次已按上限执行。\n")
        if items:
            lines.append("角色：" + image_character_summary(items) + "\n")
        if context_count > 0:
            lines.append(f"参考对话：{context_count} 条\n")
        if rewrite_model != "":
            lines.append("语义改写：" + rewrite_model + "\n")
        if references:
            lines.append(f"参考图：{len(references)} 张\n")
        if artist_used:
            lines.append(f"画师串：{artist_block.name}（{artist_block.original}）\n")
        first_result = stored_results[0]
        lines.append(
            f"model: {first_result.model}\n"
            f"size: {_first_non_empty([size, str(getattr(tool.cfg, 'size', '') or '')])}\n"
            f"prompt: {preview_text(composition.positive)}"
        )
        revised = (first_result.revised_prompt or "").strip()
        if revised != "":
            if optimizer_match != "":
                lines.append("\noptimizer: " + optimizer_match)
            lines.append("\nrevised_prompt: " + preview_text(revised))
        if failures:
            lines.append("\n失败：\n- " + "\n- ".join(failures))
        if writeback_errors:
            lines.append("\n写回角色失败：\n- " + "\n- ".join(writeback_errors))
        media_ids = [stored.id for stored in stored_media]
        lines.append("\nmedia: " + ", ".join(media_ids))

        result = image_result_batch(
            tool, stored_results, stored_media, stored_names, "".join(lines), args.send
        )
        append_warnings(result, *artist_warnings)
        append_warnings(result, *composition.warnings)
        # safety-guard: 图片输出边界。**不做图片内容识别**，只按
        # ``[safety].block_image_output`` 在出口丢弃（发送边界还会再兜一次）。
        safety_guard = guard_from_context(ctx)
        if safety_guard is not None and result.outputs:
            decision = safety_guard.allow_media_output(
                "image",
                stage="tool",
                scope=scope_from_context(ctx),
                meta={"tool": self.name()},
            )
            if not decision.allowed:
                note = decision.text or "（这张图片因内容安全策略被拦截，未发送。）"
                result.outputs = []
                append_warnings(result, note)
                result.content = (result.content or "") + "\n" + note
                result.segments.append(MessageSegment(type=SegmentText, text=note))
        return result


def image_concurrency_message(exc: BaseException) -> str:
    """把并发/取消类错误翻成用户可读的一句话。对应 Go ``imageConcurrencyMessage``。"""
    if exc is ErrFull:
        return "生图服务繁忙，请稍后重试。"
    if exc is ErrTimeout:
        return "生图服务排队超时，请稍后重试。"
    if isinstance(exc, asyncio.CancelledError):
        return "生图已取消。"
    if isinstance(exc, TimeoutError):
        return "生图排队超时，请稍后重试。"
    return "生图服务暂时不可用，请稍后重试。"


def image_generate_error_message(exc: BaseException | None) -> str:
    """对应 Go ``imageGenerateErrorMessage``。"""
    if exc is None:
        return ""
    if exc is ErrFull or exc is ErrTimeout or _is_concurrency(exc):
        return image_concurrency_message(exc)
    return str(exc)


def _is_concurrency(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.CancelledError, TimeoutError))


def image_result_batch(
    tool: Any,
    results: list[Any],
    stored: list[Any],
    names: list[str],
    summary: str,
    send: bool | None,
) -> Result:
    """把一批生成结果包成多模态结果。对应 Go ``imageResultBatch``。"""
    out = Result(content=summary)
    out.segments.append(MessageSegment(type=SegmentText, text=summary))
    should_send = bool(getattr(tool.cfg, "send_by_default", False))
    if send is not None:
        should_send = bool(send)
    for index in range(len(stored)):
        if index >= len(results) or index >= len(names):
            continue
        if stored[index] is None or results[index] is None:
            continue
        out.segments.append(
            MessageSegment(
                type=SegmentImage,
                media=stored[index].id,
                name=names[index],
                mime_type=results[index].mime_type,
            )
        )
        if should_send:
            out.outputs.append(
                Output(
                    kind=KindImage,
                    name=names[index],
                    source=Source(
                        media_id=stored[index].id, mime_type=results[index].mime_type
                    ),
                )
            )
    return out


async def generate_images(
    tool: Any, ctx: Any, request: Request, count: int
) -> tuple[list[Any], list[BaseException | None]]:
    """并发生成 ``count`` 张。对应 Go ``generateImages``。

    Go 用 worker goroutine + channel；这里用 ``asyncio`` 任务 + 信号量。
    """
    if count <= 1:
        try:
            return [await generate_one(tool, ctx, request)], [None]
        except BaseException as exc:  # noqa: BLE001
            return [None], [exc]
    results: list[Any] = [None] * count
    errors: list[BaseException | None] = [None] * count
    workers = min(count, MAX_IMAGE_BATCH_WORKERS)
    semaphore = asyncio.Semaphore(workers)

    async def run(index: int) -> None:
        async with semaphore:
            try:
                results[index] = await generate_one(tool, ctx, request)
            except BaseException as exc:  # noqa: BLE001
                errors[index] = exc

    try:
        await asyncio.gather(*(run(index) for index in range(count)))
    except asyncio.CancelledError:
        for index in range(count):
            if errors[index] is None and results[index] is None:
                errors[index] = asyncio.CancelledError()
        raise
    for index in range(count):
        if errors[index] is None and results[index] is None:
            errors[index] = asyncio.CancelledError()
    return results, errors


async def generate_one(tool: Any, ctx: Any, request: Request) -> Any:
    """生成一张（受限流器约束）。对应 Go ``generateOne``。"""
    if tool.limiter is not None:
        release = await tool.limiter.acquire(ctx)
        try:
            return await tool.client.generate(ctx, request)
        finally:
            release()
    return await tool.client.generate(ctx, request)


async def store_image(tool: Any, ctx: Any, data: bytes, mime_type: str, name: str) -> Any:
    """把生成结果存进 Media Center。对应 Go ``storeImage``。"""
    if tool.media is None:
        raise AtriXError("Media Center 未配置")
    return await tool.media.import_reader(
        ctx, data, len(data), MediaInput(name=name, mime_type=mime_type)
    )


def _first_non_empty(values: list[str]) -> str:
    for value in values:
        if (value or "").strip() != "":
            return value.strip()
    return ""


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _enabled(target: Any) -> bool:
    checker = getattr(target, "enabled", None)
    return bool(checker()) if callable(checker) else target is not None


__all__ = [name for name in dir() if not name.startswith("_")]
