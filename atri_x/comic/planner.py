"""分镜规划（P0-4）：规则规划器 + 模型返回的结构校验。

两条路径：

* :func:`rules_plan` —— 纯程序，不需要模型。把聊天消息按顺序分给 N 格，台词
  **逐字**取原文，画面描述由程序拼接。开箱即用，也不存在"模型漏掉台词"的问题。
* :func:`parse_plan_response` / :func:`build_plan_from_mapping` —— 把规划模型返回的
  JSON 变成 :class:`~atri_x.comic.models.ComicPlan`，**逐项校验**：格数、人物 ID、
  台词说话人、来源消息、原文台词是否真的出自原文。校验不过就直接报错，不静默
  补字段。

模型调用本身在 ``atri_x.app.comic``（装配层）里，本模块不 import 任何 LLM 客户端，
因此可以完全离线单测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from atri_x.comic.context import format_context_material
from atri_x.comic.models import (
    MAX_DIALOGUE_RUNES,
    MAX_PLAN_PANELS,
    MAX_SCENE_RUNES,
    MAX_TITLE_RUNES,
    ComicCharacter,
    ComicCharacterError,
    ComicContext,
    ComicDataError,
    ComicDialogue,
    ComicPanel,
    ComicPlan,
    ComicPlanError,
    ComicSource,
    new_plan_id,
)

__all__ = [
    "ComicPlanRequest",
    "ComicPlanner",
    "build_plan_from_mapping",
    "build_plan_prompt",
    "parse_plan_json",
    "parse_plan_response",
    "rules_plan",
]

#: 提示词库里的模板 id（桌面端「提示词 → 漫画」可改）。
PLAN_PROMPT_ID = "comic.plan"

#: 模型返回里常见的代码围栏。
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


@dataclass
class ComicPlanRequest:
    """一次分镜规划的输入（规则规划与模型规划共用）。"""

    context: ComicContext = field(default_factory=ComicContext)
    characters: list[ComicCharacter] = field(default_factory=list)
    panel_count: int = 6
    language: str = "zh"
    layout: str = "grid"
    title_hint: str = ""
    instructions: str = ""
    max_scene_runes: int = MAX_SCENE_RUNES
    max_dialogue_runes: int = MAX_DIALOGUE_RUNES
    plan_id: str = ""

    def character_by_id(self) -> dict[str, ComicCharacter]:
        return {item.id: item for item in self.characters}

    def character_lines(self) -> str:
        lines: list[str] = []
        for item in self.characters:
            extra = []
            if item.member_id:
                extra.append(f"群友 {item.member_id}")
            if item.character_id:
                extra.append(f"角色卡 {item.character_id}")
            if item.reference.kind != "none":
                extra.append("有参考图")
            suffix = f"（{'；'.join(extra)}）" if extra else ""
            lines.append(f"- {item.id} = {item.display_name}{suffix}")
        return "\n".join(lines)

    def normalized_panel_count(self) -> int:
        count = int(self.panel_count or 0) or 6
        return max(1, min(count, MAX_PLAN_PANELS))


@runtime_checkable
class ComicPlanner(Protocol):
    """规划模型的鸭子类型契约：``async def plan(ctx, request) -> str``（JSON 文本）。"""

    async def plan(self, ctx: Any, request: ComicPlanRequest) -> str: ...


def build_plan_prompt(request: ComicPlanRequest) -> tuple[str, str]:
    """渲染模型规划用的 (system, user) 文本。

    系统提示词来自提示词库 ``comic.plan``（GUI 可改）；用户消息是素材与硬约束。
    """
    from atri_x.promptlib import render_prompt
    from atri_x.promptlib.defaults import PROMPT_COMIC_PLAN

    values = {
        "panel_count": request.normalized_panel_count(),
        "language": request.language,
        "layout": request.layout,
        "max_scene_runes": request.max_scene_runes,
        "max_dialogue_runes": request.max_dialogue_runes,
        "character_list": request.character_lines(),
        "title_hint": (request.title_hint or "（未指定，可自行拟定）").strip(),
        "message_material": format_context_material(request.context, max_runes=6000),
    }
    system = render_prompt(PLAN_PROMPT_ID, values) or _render_fallback(PROMPT_COMIC_PLAN, values)
    user_parts = []
    if request.instructions.strip():
        user_parts.append("[本次额外要求]\n" + request.instructions.strip())
    user_parts.append(
        f"请严格输出一个 JSON 对象：panels 恰好 {values['panel_count']} 项，"
        f"language={values['language']}，layout={values['layout']}。"
    )
    return system.strip(), "\n\n".join(user_parts)


def _render_fallback(template: str, values: dict[str, Any]) -> str:
    """提示词库不可用时的最小渲染（只替换已知变量，未知占位符保留）。"""
    text = template or ""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _split_contiguous(messages: list[Any], groups: int) -> list[list[Any]]:
    """把消息按顺序切成 ``groups`` 段（尽量均匀，每段非空）。"""
    total = len(messages)
    out: list[list[Any]] = []
    start = 0
    for index in range(groups):
        remaining_groups = groups - index
        remaining = total - start
        take = max(1, remaining // remaining_groups) if remaining_groups > 1 else remaining
        take = min(take, remaining)
        out.append(messages[start : start + take])
        start += take
    return out


def rules_plan(request: ComicPlanRequest) -> ComicPlan:
    """纯程序的规则规划：台词逐字取原文，画面描述由素材拼接。"""
    count = request.normalized_panel_count()
    messages = list(request.context.messages)
    if not messages:
        raise ComicPlanError("没有可用的聊天素材，无法规划分镜。")
    if len(messages) < count:
        raise ComicPlanError(
            f"聊天素材不足：只有 {len(messages)} 条可用消息，撑不起 {count} 格。"
            "请减少格数、扩大聊天范围，或先聊几句再来。"
            "（不会为了凑格数编造群友没说过的话）"
        )
    characters = request.character_by_id()
    character_by_name: dict[str, ComicCharacter] = {}
    for item in request.characters:
        character_by_name.setdefault(item.display_name.strip().casefold(), item)
        if item.character_id:
            character_by_name.setdefault(item.character_id.casefold(), item)

    def speaker_for(message: Any) -> ComicCharacter | None:
        name = (message.sender_name or "").strip().casefold()
        if name and name in character_by_name:
            return character_by_name[name]
        for item in request.characters:
            if item.member_id and item.member_id == message.sender_id:
                return item
        return None

    groups = _split_contiguous(messages, count)
    panels: list[ComicPanel] = []
    for index, group in enumerate(groups):
        dialogues: list[ComicDialogue] = []
        captions: list[str] = []
        panel_characters: list[str] = []
        scene_parts: list[str] = []
        for message in group:
            name = (message.sender_name or message.sender_id or "群友").strip()
            speaker = speaker_for(message)
            text = message.text.strip()
            scene_parts.append(f"{name}说「{text}」" if text else f"{name}（图片消息）")
            if speaker is None:
                captions.append(f"{name}：{text}" if text else name)
                continue
            if speaker.id not in panel_characters:
                panel_characters.append(speaker.id)
            dialogues.append(
                ComicDialogue(
                    speaker_id=speaker.id,
                    text=text[: request.max_dialogue_runes],
                    kind="original",
                    source_message_ids=[message.message_id],
                )
            )
        scene = "群聊对话场景：" + "；".join(scene_parts)
        panels.append(
            ComicPanel(
                index=index + 1,
                scene=scene[: request.max_scene_runes],
                character_ids=panel_characters,
                dialogue=dialogues,
                caption="；".join(captions)[:200],
                source_message_ids=[item.message_id for item in group],
            )
        )

    title = (request.title_hint or "").strip()
    if not title:
        first = messages[0].text.strip() or messages[0].sender_name or "群聊"
        title = "群聊漫画：" + first[:20]
    plan = ComicPlan(
        plan_id=request.plan_id or new_plan_id(),
        revision=1,
        title=title[:MAX_TITLE_RUNES],
        language=request.language,
        layout=request.layout,
        panel_count=count,
        characters=list(request.characters),
        panels=panels,
        source=ComicSource(
            platform=request.context.platform,
            scope_id=request.context.scope_id,
            scope_type=request.context.scope_type,
            message_ids=request.context.message_ids(),
            anchor_message_id=request.context.anchor_message_id,
            message_count=len(request.context.messages),
            truncated=request.context.truncated,
        ),
        adaptation_notes="规则规划：台词全部取自聊天原文，画面描述由程序拼接。",
        planner="rules",
        warnings=list(request.context.warnings),
    )
    plan.validate()
    return plan


def parse_plan_json(text: str) -> dict[str, Any]:
    """从模型输出里取出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    raw = (text or "").strip()
    raw = _FENCE_RE.sub("", raw).strip()
    if not raw:
        raise ComicPlanError("规划模型返回为空。")
    try:
        value = json.loads(raw)
    except ValueError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ComicPlanError("规划模型没有返回 JSON 对象。") from None
        try:
            value = json.loads(raw[start : end + 1])
        except ValueError as exc:
            raise ComicPlanError(f"规划模型返回的 JSON 解析失败：{exc}") from exc
    if not isinstance(value, dict):
        raise ComicPlanError("规划模型返回的顶层不是 JSON 对象。")
    return value


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _is_verbatim(text: str, sources: list[str]) -> bool:
    """台词是否真的出自素材原文（去掉空白后比较，允许只截取其中一段）。"""
    def norm(value: str) -> str:
        return "".join(str(value or "").split())

    target = norm(text)
    if not target:
        return False
    for source in sources:
        haystack = norm(source)
        if target in haystack:
            return True
    return False


def build_plan_from_mapping(
    request: ComicPlanRequest, raw: dict[str, Any], *, planner_name: str = "llm"
) -> ComicPlan:
    """把规划模型的 JSON 映射成计划并做完整校验。"""
    count = request.normalized_panel_count()
    raw_panels = raw.get("panels")
    if not isinstance(raw_panels, list):
        raise ComicPlanError("规划结果缺少 panels 数组。")
    if len(raw_panels) != count:
        raise ComicPlanError(
            f"规划结果有 {len(raw_panels)} 格，但要求恰好 {count} 格；请重新规划。"
        )
    characters = list(request.characters)
    character_ids = {item.id for item in characters}
    if not character_ids:
        raise ComicCharacterError("规划前没有解析出任何人物。")
    message_text = {item.message_id: item.text for item in request.context.messages}
    warnings: list[str] = []
    panels: list[ComicPanel] = []
    for index, raw_panel in enumerate(raw_panels):
        if not isinstance(raw_panel, dict):
            raise ComicPlanError(f"第 {index + 1} 格不是 JSON 对象。")
        scene = _clip(raw_panel.get("scene"), request.max_scene_runes)
        if not scene:
            raise ComicPlanError(f"第 {index + 1} 格缺少画面描述（scene）。")
        panel_source_ids = [
            str(item)
            for item in raw_panel.get("source_message_ids") or []
            if str(item).strip()
        ]
        for message_id in panel_source_ids:
            if message_id not in message_text:
                raise ComicPlanError(
                    f"第 {index + 1} 格引用了不存在的来源消息 {message_id}；"
                    "规划只能引用本次素材里的消息。"
                )
        speaker_ids = [
            str(item)
            for item in raw_panel.get("character_ids") or []
            if str(item).strip()
        ]
        for character_id in speaker_ids:
            if character_id not in character_ids:
                raise ComicPlanError(
                    f"第 {index + 1} 格出现了不在人物表里的人物 {character_id}；"
                    "只能使用本次解析出的角色。"
                )
        dialogues: list[ComicDialogue] = []
        raw_dialogue = raw_panel.get("dialogue") or []
        if not isinstance(raw_dialogue, list):
            raise ComicPlanError(f"第 {index + 1} 格的 dialogue 必须是数组。")
        for raw_line in raw_dialogue:
            if not isinstance(raw_line, dict):
                raise ComicPlanError(f"第 {index + 1} 格有非对象的台词条目。")
            speaker = str(raw_line.get("speaker_id") or "").strip()
            if speaker not in character_ids:
                raise ComicPlanError(
                    f"第 {index + 1} 格台词说话人 {speaker!r} 不在人物表里。"
                )
            text = _clip(raw_line.get("text"), request.max_dialogue_runes)
            if not text:
                raise ComicPlanError(f"第 {index + 1} 格有一句空台词。")
            kind = str(raw_line.get("kind") or "original").strip().lower()
            if kind not in ("original", "abridged", "invented"):
                raise ComicPlanError(
                    f"第 {index + 1} 格台词来源 kind 不合法：{raw_line.get('kind')!r}"
                )
            line_source_ids = [
                str(item)
                for item in raw_line.get("source_message_ids") or []
                if str(item).strip()
            ]
            if not line_source_ids:
                line_source_ids = list(panel_source_ids)
            sources = [message_text.get(item, "") for item in line_source_ids if item in message_text]
            if kind == "original" and not _is_verbatim(text, sources):
                # 模型声称"原文"但素材里没有：降级为改编，绝不冒称群友真说过。
                kind = "abridged"
                warnings.append(
                    f"第 {index + 1} 格台词「{text[:16]}…」在素材里找不到原文，已标记为改编。"
                )
            if not line_source_ids:
                raise ComicPlanError(
                    f"第 {index + 1} 格台词「{text[:16]}」没有绑定任何来源消息。"
                )
            dialogues.append(
                ComicDialogue(
                    speaker_id=speaker,
                    text=text,
                    kind=kind,
                    source_message_ids=line_source_ids,
                )
            )
        for line in dialogues:
            if line.speaker_id not in speaker_ids:
                speaker_ids.append(line.speaker_id)
        panels.append(
            ComicPanel(
                index=index + 1,
                scene=scene,
                character_ids=speaker_ids,
                dialogue=dialogues,
                caption=_clip(raw_panel.get("caption"), 200),
                source_message_ids=panel_source_ids,
            )
        )
    language = str(raw.get("language") or request.language).strip() or request.language
    if language not in ("zh", "en"):
        warnings.append(f"规划结果的语言 {language!r} 不受支持，已按 {request.language} 处理。")
        language = request.language
    layout = str(raw.get("layout") or request.layout).strip() or request.layout
    if layout not in ("grid", "strip"):
        layout = request.layout
    title = _clip(raw.get("title"), MAX_TITLE_RUNES) or (request.title_hint or "群聊漫画")
    plan = ComicPlan(
        plan_id=request.plan_id or new_plan_id(),
        revision=1,
        title=title,
        language=language,
        layout=layout,
        panel_count=count,
        characters=characters,
        panels=panels,
        source=ComicSource(
            platform=request.context.platform,
            scope_id=request.context.scope_id,
            scope_type=request.context.scope_type,
            message_ids=request.context.message_ids(),
            anchor_message_id=request.context.anchor_message_id,
            message_count=len(request.context.messages),
            truncated=request.context.truncated,
        ),
        adaptation_notes=_clip(raw.get("adaptation_notes"), 400),
        planner=planner_name,
        warnings=[*request.context.warnings, *warnings],
    )
    try:
        plan.validate()
    except ComicDataError as exc:
        # 统一翻成规划错误：调用方只需要处理一种"计划不可用"异常。
        raise ComicPlanError(f"规划结果未通过校验：{exc}") from exc
    return plan


def parse_plan_response(request: ComicPlanRequest, text: str) -> ComicPlan:
    """解析模型输出并建计划；任何结构问题都抛 :class:`ComicPlanError`。"""
    return build_plan_from_mapping(request, parse_plan_json(text), planner_name="llm")
