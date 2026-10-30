"""聊天漫画工具：``comic_plan``（只规划、预览）与 ``comic_render``（真正生成）。

两条入口的设计意图：

* ``comic_plan`` **不消耗生图额度**，只做取材 + 人物解析 + 分镜规划，把结构化
  计划存盘并返回人话预览。模型可以先给用户看，再决定要不要画。
* ``comic_render`` 绑定 ``plan_id + revision`` 生成**一张**整页漫画（不是生成 N
  张图）。``count=6`` 是错的语义，所以这里没有 ``count`` 参数，格数是
  ``panel_count``，只存在于计划里。

权限与配额：

* 与 ``image_generate`` 共用同一批生图 profile / 限流器 / Media Center；
* 生图 profile 的 ``superadmin_only`` 在服务层重新校验（不能借漫画绕过）；
* 工具自身风险等级低，但真正花钱的是 ``comic_render``；默认 profile 是超管专属
  时，``comic_render`` 的 Info 也会标 ``superadmin_only``，执行器层面直接挡住。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.comic.models import (
    ComicError,
    ComicPlan,
    ComicRenderError,
)
from atri_x.comic.service import ComicService, PlanRequest, RenderRequest
from atri_x.delivery import KindImage, Output, Source
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText, ToolSchema
from atri_x.tool.builder import Builder, enum, required
from atri_x.tool.builtin.character import character_viewer
from atri_x.tool.builtin.common import append_warnings, decode_args
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

__all__ = [
    "COMIC_PLAN_NAME",
    "COMIC_RENDER_NAME",
    "ComicPlanArgs",
    "ComicPlanTool",
    "ComicRenderArgs",
    "ComicRenderTool",
    "new_comic_tools",
    "render_plan_preview",
]

#: 两个工具名（注册表 / GUI 目录 / 测试三处必须一致）。
COMIC_PLAN_NAME = "comic_plan"
COMIC_RENDER_NAME = "comic_render"

_PLAN_DESCRIPTION = (
    "把当前群聊聊天改编成一页结构化漫画分镜（不消耗生图额度，也不发图）。"
    "只从**当前会话**取材：默认取最近 12 条消息；可用 anchor 指定引用消息、"
    "用 context_query 按关键词取材。人物默认按发言人对齐角色卡，没有角色卡的"
    "群友用平台头像；用户明确说「就用头像」时传 character_mode=avatars。"
    "返回 plan_id 与逐格预览，确认后用 comic_render 真正生成。"
)

_RENDER_DESCRIPTION = (
    "按 comic_plan 产出的计划生成一张整页漫画图（一张图包含全部格子，"
    "不是每格一张）。必须传 plan_id，可用 revision 绑定具体版本；计划改过之后"
    "旧 revision 会被拒绝。失败/超时会记录状态，不会自动重复扣费；"
    "如果只是投递失败，用 task_id 重发已有图片，不会重新生成。"
)


@dataclass
class ComicPlanArgs:
    """``comic_plan`` 的入参。"""

    panel_count: int = 0
    language: str = ""
    layout: str = ""
    title: str = ""
    instructions: str = ""
    character_mode: str = ""
    character_ids: list[str] = field(default_factory=list)
    reference_images: list[str] = field(default_factory=list)
    anchor: str = ""
    context_query: str = ""
    context_limit: int = 0


@dataclass
class ComicRenderArgs:
    """``comic_render`` 的入参。"""

    plan_id: str = ""
    revision: int = 0
    task_id: str = ""
    profile: str = ""
    size: str = ""
    quality: str = ""
    send: bool | None = None
    dry_run: bool = False


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return []


class ComicPlanTool:
    """``comic_plan``：取材 + 分镜规划 + 预览。"""

    def __init__(self, service: ComicService) -> None:
        self.service = service

    def name(self) -> str:
        return COMIC_PLAN_NAME

    def info(self) -> Info:
        return (
            Builder(self.name())
            .description(_PLAN_DESCRIPTION)
            .risk(RiskLow)
            .tags("image", "comic")
            .depends_on(
                "search_chat_history",
                "get_chat_history_around",
                "character_list",
                "character_read",
            )
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            Builder(self.name())
            .description("规划一页群聊漫画的分镜（不生成图片、不扣生图额度）。")
            .integer(
                "panel_count",
                "格数，默认 6，最多由 [comic].max_panels 决定（默认 12）。"
                "注意这不是生成几张图——它是一张图里的格子数。",
            )
            .string("language", "台词语言，zh（默认）或 en。")
            .string("layout", "版面：grid（网格，默认）或 strip（竖排条漫）。", enum("grid", "strip"))
            .string("title", "标题提示；不填时用聊天内容自动拟。")
            .string("instructions", "本次改编的额外要求（例如「保持吐槽风格」）。")
            .string(
                "character_mode",
                "人物来源：auto（默认，角色卡优先、否则头像）、characters（只用角色卡）、"
                "avatars（全部用群友头像，用户明确说「就用头像」时用这个）。",
                enum("auto", "characters", "avatars"),
            )
            .string_array("character_ids", "可选：显式指定要画进去的角色卡 ID 列表。")
            .string_array(
                "reference_images",
                "可选：本次显式参考图，格式 <角色ID或群友ID或显示名>=media:<sha256>；"
                "也可以 <角色ID>=<角色图片名>。没有 = 的条目按顺序补给没有参考图的人物。",
            )
            .string(
                "anchor",
                "可选：以某条消息为中心取材（传消息 ID，或 current 表示当前这条消息）。"
                "锚点不存在时会明确报错，不会退回「最近几条」。",
            )
            .string("context_query", "可选：按关键词取材；不填则取最近的消息。")
            .integer("context_limit", "取材条数，默认跟随 [comic].context_limit（12）。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, ComicPlanArgs, self.name())
        request = PlanRequest(
            panel_count=_as_int(args.panel_count),
            language=(args.language or "").strip(),
            layout=(args.layout or "").strip(),
            title_hint=(args.title or "").strip(),
            instructions=(args.instructions or "").strip(),
            character_mode=(args.character_mode or "").strip() or "auto",
            character_ids=_as_str_list(args.character_ids),
            reference_images=_as_str_list(args.reference_images),
            anchor_message_id=(args.anchor or "").strip(),
            context_query=(args.context_query or "").strip(),
            context_limit=_as_int(args.context_limit),
        )
        viewer = character_viewer(ctx)
        try:
            outcome = await self.service.plan(ctx, request, viewer=viewer)
        except ComicError as exc:
            return Result(content=str(exc))
        plan = outcome.plan
        result = Result(
            content=render_plan_preview(plan),
            data=json.dumps(plan.to_dict(), ensure_ascii=False),
            metadata={
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "panel_count": plan.panel_count,
                "planner": plan.planner,
            },
        )
        append_warnings(result, *outcome.warnings)
        result.content = (result.content or "").rstrip() + (
            "\n\n确认无误后调用 comic_render(plan_id=\"%s\", revision=%d) 生成整页漫画。"
            % (plan.plan_id, plan.revision)
        )
        return result


class ComicRenderTool:
    """``comic_render``：按计划生成一张整页漫画。"""

    def __init__(self, service: ComicService) -> None:
        self.service = service

    def name(self) -> str:
        return COMIC_RENDER_NAME

    def info(self) -> Info:
        builder = (
            Builder(self.name())
            .description(_RENDER_DESCRIPTION)
            .risk(RiskLow)
            .tags("image", "comic")
            .depends_on("comic_plan")
        )
        cfg = self._default_config()
        if getattr(cfg, "superadmin_only", False):
            builder = builder.superadmin_only()
        return builder.build_info()

    def schema(self) -> ToolSchema:
        return (
            Builder(self.name())
            .description("按 comic_plan 的计划生成一张整页漫画（所有格在同一张图里）。")
            .string("plan_id", "comic_plan 返回的计划 ID。", required())
            .integer(
                "revision",
                "要生成第几版计划；不填用最新版。计划更新后旧版本会被拒绝，"
                "避免改了台词却拿到旧图。",
            )
            .string(
                "task_id",
                "可选：重发已有任务时传它。只发送记录里的 MediaID，不会重新生成、"
                "不会重复扣费。",
            )
            .string("profile", "可选生图 profile（[image_generation.profiles.<name>]）。")
            .string("size", "可选尺寸；不填用 profile 默认。")
            .string("quality", "可选画质：low / medium / high。")
            .boolean("send", "是否直接发送到当前聊天；不填跟随 [comic].send_by_default。")
            .boolean(
                "dry_run",
                "只编译并显示最终漫画提示词与参考图顺序，不调用生图接口、不保存、不发送。",
            )
            .build_schema()
        )

    def _default_config(self) -> Any:
        profile = self.service.profiles.get(self.service.default_profile)
        return getattr(profile, "config", None) if profile is not None else None

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, ComicRenderArgs, self.name())
        request = RenderRequest(
            plan_id=(args.plan_id or "").strip(),
            revision=_as_int(args.revision),
            task_id=(args.task_id or "").strip(),
            profile=(args.profile or "").strip(),
            size=(args.size or "").strip(),
            quality=(args.quality or "").strip(),
            send=args.send if isinstance(args.send, bool) else None,
            dry_run=bool(args.dry_run),
        )
        if request.plan_id == "":
            return Result(content="plan_id 不能为空，请先调用 comic_plan。")
        viewer = character_viewer(ctx)
        try:
            outcome = await self.service.render(ctx, request, viewer=viewer)
        except ComicError as exc:
            return Result(content=str(exc))
        if outcome.dry_run:
            return self._dry_run_result(outcome)
        return self._success_result(outcome, args.send)

    def _dry_run_result(self, outcome: Any) -> Result:
        payload = {
            "dry_run": True,
            "plan_id": outcome.plan.plan_id,
            "revision": outcome.plan.revision,
            "panel_count": outcome.plan.panel_count,
            "references": [
                f"#{index + 1} {item.display_name}:{item.reference.describe()}"
                for index, item in enumerate(outcome.plan.reference_order())
            ],
            "prompt": outcome.prompt,
            "negative": outcome.negative,
            "warnings": list(outcome.warnings),
        }
        lines = [
            "dry_run：只编译漫画提示词，不调用生图接口、不保存、不发送、不计额度。",
            f"计划 {outcome.plan.plan_id} 第 {outcome.plan.revision} 版，"
            f"{outcome.plan.panel_count} 格，标题：{outcome.plan.title}",
            "参考图顺序：" + (
                "；".join(payload["references"]) if payload["references"] else "（无参考图）"
            ),
            "",
            "最终提示词：",
            outcome.prompt,
        ]
        if outcome.negative:
            lines.extend(["", "负向提示词：", outcome.negative])
        result = Result(content="\n".join(lines), data=json.dumps(payload, ensure_ascii=False))
        append_warnings(result, *outcome.warnings)
        return result

    def _success_result(self, outcome: Any, send_arg: Any) -> Result:
        record = outcome.record
        should_send = (
            bool(send_arg)
            if isinstance(send_arg, bool)
            else bool(getattr(self.service.options, "send_by_default", True))
        )
        lines: list[str] = []
        if outcome.reused:
            lines.append("已重发已有漫画图片（没有重新生成）。\n")
        else:
            lines.append("已生成整页漫画。\n")
        lines.append(
            f"计划：{outcome.plan.plan_id} 第 {outcome.plan.revision} 版，"
            f"{outcome.plan.panel_count} 格｜标题：{outcome.plan.title}\n"
        )
        lines.append(f"任务：{record.task_id}\n")
        if record.reference_descriptors:
            lines.append("参考图：\n- " + "\n- ".join(record.reference_descriptors) + "\n")
        lines.append(
            f"model: {record.model}\nsize: {record.size}\nquality: {record.quality}\n"
            f"media: {outcome.media_id}"
        )
        revised = (outcome.revised_prompt or "").strip()
        if revised:
            lines.append("\nrevised_prompt: " + revised[:200])
        result = Result(content="".join(lines))
        result.segments.append(MessageSegment(type=SegmentText, text=result.content))
        result.segments.append(
            MessageSegment(
                type=SegmentImage,
                media=outcome.media_id,
                name=outcome.media_name,
                mime_type=outcome.mime_type,
            )
        )
        if should_send and outcome.media_id:
            result.outputs.append(
                Output(
                    kind=KindImage,
                    name=outcome.media_name,
                    source=Source(media_id=outcome.media_id, mime_type=outcome.mime_type),
                )
            )
        result.metadata = {
            "task_id": record.task_id,
            "plan_id": outcome.plan.plan_id,
            "revision": outcome.plan.revision,
            "media_id": outcome.media_id,
            "generation": record.generation,
            "storage": record.storage,
            "delivery": record.delivery,
        }
        append_warnings(result, *outcome.warnings)
        return result


def render_plan_preview(plan: ComicPlan) -> str:
    """把计划渲染成人话预览（给用户/模型看，不参与生成）。"""
    lines: list[str] = [
        f"漫画分镜已规划（计划 {plan.plan_id} 第 {plan.revision} 版，"
        f"{plan.panel_count} 格，未消耗生图额度）。\n",
        f"标题：{plan.title}",
        f"版面：{'竖排条漫' if plan.layout == 'strip' else '网格分格'}；"
        f"语言：{'中文' if plan.language == 'zh' else '英文'}；"
        f"规划方式：{'规则规划' if plan.planner == 'rules' else '模型规划'}\n",
        "人物与参考图：",
    ]
    reference_number = 0
    for character in plan.characters:
        if character.reference.kind != "none":
            reference_number += 1
            lines.append(
                f"- 参考图#{reference_number} {character.display_name}（{character.reference.describe()}）"
            )
        else:
            lines.append(f"- {character.display_name}（无参考图，仅按文字设定绘制）")
    lines.append("")
    lines.append("分镜：")
    names = plan.character_by_id()
    for panel in plan.panels:
        cast = "、".join(names[item].display_name for item in panel.character_ids if item in names)
        lines.append(f"{panel.index}. 出场：{cast or '（无）'}｜画面：{panel.scene}")
        for line in panel.dialogue:
            speaker = names.get(line.speaker_id)
            who = speaker.display_name if speaker else line.speaker_id
            suffix = ""
            if line.kind == "abridged":
                suffix = "（缩写）"
            elif line.kind == "invented":
                suffix = "（创作台词）"
            lines.append(f"   对白｜{who}：「{line.text}」{suffix}")
        if panel.caption:
            lines.append(f"   旁白：{panel.caption}")
    source = plan.source
    lines.extend(
        [
            "",
            f"取材：{source.platform} / {source.scope_id}，共 {source.message_count} 条消息"
            + (f"（锚点 {source.anchor_message_id}）" if source.anchor_message_id else "")
            + (f"；消息 ID：{', '.join(source.message_ids[:8])}"
               + ("…" if len(source.message_ids) > 8 else "")
               if source.message_ids
               else ""),
        ]
    )
    if plan.adaptation_notes:
        lines.append("改编说明：" + plan.adaptation_notes)
    return "\n".join(lines)


def new_comic_tools(service: ComicService) -> list[Any]:
    """构造两个漫画工具（装配层用）。"""
    return [ComicPlanTool(service), ComicRenderTool(service)]
