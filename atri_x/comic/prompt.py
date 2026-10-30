"""漫画提示词编译（P0-5）：把结构化计划编译成**受保护**的生图提示词。

与普通生图的两条关键差异：

1. **台词与分镜是受保护块**（``PromptPriorityProtected``）。``compose_blocks``
   只会丢弃风格/修饰片段；格数、格序、人物—参考图映射、逐字台词不会被悄悄
   删掉。丢光可丢片段仍超长时直接报错，让用户缩短台词或调大上限——绝不静默
   丢分镜。
2. **不经过语义改写器**。漫画模式关掉普通场景改写，避免模型把中文台词翻译成
   英文或压缩掉。程序里唯一能做的是按 ``max_dialogue_runes`` 截断（规则规划器
   在构造计划时已经截过；这里只做防御）。

参考图编号在计划里固定：有参考图的人物按计划顺序排 ``#1..#k``，提示词里的
``参考图#k`` 与运行时传给生图服务的 ``references`` 列表必须同一顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.comic.models import (
    ComicCharacter,
    ComicPlan,
    ComicPromptError,
)
from atri_x.imagegen import (
    ComposeResult,
    PromptBlock,
    PromptPriorityOptional,
    PromptPriorityProtected,
    PromptPriorityStyle,
    compose_blocks,
)

__all__ = [
    "DEFAULT_COMIC_PROMPT_RUNES",
    "MAX_COMIC_REFERENCES",
    "ComicPrompt",
    "build_comic_prompt",
    "layout_description",
]

#: 一页最多几张参考图（与现有生图参考图上限一致）。
MAX_COMIC_REFERENCES = 4

#: 没配置上限时的漫画提示词上限。比普通生图（4000）宽，因为要放逐字台词。
DEFAULT_COMIC_PROMPT_RUNES = 6000

#: 中文台词保护声明。用户不能通过提示词库改掉它：它由代码在编译期写死。
COMIC_TEXT_LOCK = (
    "图中所有标题与对白【必须逐字使用下面给出的原文】，"
    "不要翻译、不要改写、不要省略、不要用拼音或英文替代；"
    "文字必须清晰可读，不要出现乱码或缺字。"
)


def layout_description(layout: str) -> str:
    """版面的人话描述（``grid`` / ``strip``）。"""
    if layout == "strip":
        return "竖排条漫（从上到下等宽分格）"
    return "网格分格（从左到右、从上到下依次排列）"


@dataclass
class ComicPrompt:
    """编译结果。"""

    positive: str = ""
    negative: str = ""
    warnings: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    limit: int = 0
    runes: int = 0
    #: 被编进 ``参考图#k`` 的人物（顺序即 references 顺序）。
    reference_characters: list[ComicCharacter] = field(default_factory=list)

    def reference_number(self, character_id: str) -> int:
        for index, item in enumerate(self.reference_characters):
            if item.id == character_id:
                return index + 1
        return 0


def _names(plan: ComicPlan, ids: list[str]) -> str:
    by_id = plan.character_by_id()
    names = [by_id[item].display_name for item in ids if item in by_id]
    return "、".join(names) if names else "（无）"


def build_comic_prompt(
    plan: ComicPlan,
    *,
    reference_characters: list[ComicCharacter] | None = None,
    global_preset: str = "",
    character_presets: dict[str, str] | None = None,
    extra_negative: str = "",
    negative_mode: str = "field",
    max_runes: int = 0,
) -> ComicPrompt:
    """编译漫画生图提示词。"""
    refs = list(reference_characters or [])
    if len(refs) > MAX_COMIC_REFERENCES:
        raise ComicPromptError(
            f"参考图最多 {MAX_COMIC_REFERENCES} 张，这次需要 {len(refs)} 张；"
            "请减少人物或分页。"
        )
    result = ComicPrompt(reference_characters=list(refs))
    blocks: list[PromptBlock] = []

    def add(label: str, text: str, priority: int) -> None:
        if (text or "").strip() == "":
            return
        blocks.append(PromptBlock(label=label, text=text.strip(), priority=priority))

    # 1) 硬规则（受保护）
    add(
        "comic_rules",
        f"这是一张完整的 {plan.panel_count} 格漫画页，"
        f"版面为{layout_description(plan.layout)}。"
        "严格按格序绘制，不能增加、减少或合并分镜。"
        + COMIC_TEXT_LOCK,
        PromptPriorityProtected,
    )
    add("comic_title", f"标题（逐字）：{plan.title}", PromptPriorityProtected)

    # 2) 人物—参考图映射（受保护；参考图编号与运行时列表同序）
    if refs:
        lines = []
        for index, character in enumerate(refs):
            kind = {
                "avatar": "平台头像",
                "character_image": "角色卡图片",
                "media": "指定图片",
            }.get(character.reference.kind, "参考图")
            lines.append(f"参考图#{index + 1} = {character.display_name}（{kind}）")
        no_ref = [
            item.display_name
            for item in plan.characters
            if item.reference.kind == "none"
        ]
        text = "人物与参考图对应：\n" + "\n".join(lines)
        if no_ref:
            text += "\n没有参考图的人物（按姓名与设定绘制）：" + "、".join(no_ref)
        add("comic_character_map", text, PromptPriorityProtected)
    else:
        add(
            "comic_character_map",
            "没有可用参考图；以下人物只按名字与画面描述绘制："
            + "、".join(item.display_name for item in plan.characters),
            PromptPriorityProtected,
        )

    # 3) 逐格分镜（受保护）
    annotations: list[str] = []
    for panel in plan.panels:
        part = f"第{panel.index}格｜出场：{_names(plan, panel.character_ids)}｜画面：{panel.scene}"
        if panel.dialogue:
            lines = []
            for line in panel.dialogue:
                speaker = plan.character_by_id().get(line.speaker_id)
                name = speaker.display_name if speaker else line.speaker_id
                if line.kind == "abridged":
                    annotations.append(f"第{panel.index}格「{name}」的台词是缩写，不是原文")
                elif line.kind == "invented":
                    annotations.append(f"第{panel.index}格「{name}」的台词是创作台词")
                lines.append(f"{name}：「{line.text}」")
            part += "\n对白（逐字）：" + "；".join(lines)
        if panel.caption:
            part += "\n旁白：" + panel.caption
        add(f"comic_panel_{panel.index}", part, PromptPriorityProtected)
    if annotations:
        # 改编说明写成**独立指令块**而不是塞在引号里：塞进台词会让生图模型把
        # "（缩写）"、"(invented)" 这类说明当成要画的文字。
        add(
            "comic_dialogue_notes",
            "台词来源说明（只给你看，不要把这段说明画进图里）：" + "；".join(annotations),
            PromptPriorityProtected,
        )

    # 4) 风格片段（可丢：先丢这些，再报错）
    add("global_preset", global_preset, PromptPriorityStyle)
    for character_id, preset in (character_presets or {}).items():
        if not (preset or "").strip():
            continue
        character = plan.character_by_id().get(character_id)
        name = character.display_name if character else character_id
        add(f"character_style:{character_id}", f"角色 {name} 的设定：\n{preset}", PromptPriorityStyle)
    if refs:
        add(
            "reference_consistency",
            "每个角色的脸、发型、服装与配色必须与对应编号的参考图一致，不要串角色。",
            PromptPriorityOptional,
        )

    limit = int(max_runes or 0) or DEFAULT_COMIC_PROMPT_RUNES
    composed: ComposeResult = compose_blocks(blocks, "\n\n", limit)
    result.positive = composed.prompt
    result.dropped = list(composed.dropped)
    result.limit = composed.limit
    result.runes = composed.runes
    if composed.over_limit:
        raise ComicPromptError(
            f"漫画提示词 {composed.runes} 字符，超过上限 {limit}；"
            "受保护的分镜与台词一段都不能丢。请减少格数/缩短台词，"
            "或调大 [comic].max_prompt_runes。"
        )
    if composed.dropped:
        result.warnings.append(
            "提示词超长，已整体丢弃风格片段：" + "、".join(composed.dropped)
        )

    # 5) 负向提示词：沿用 profile 的通道（field / inline / off）
    negatives: list[str] = []

    def add_negative(value: str) -> None:
        text = (value or "").strip()
        if text and text not in negatives:
            negatives.append(text)

    add_negative(extra_negative)
    mode = (negative_mode or "field").strip().lower()
    if mode == "off":
        if negatives:
            result.warnings.append(
                f"profile 的 negative_prompt_mode=off，{len(negatives)} 条负面提示词已丢弃。"
            )
    elif mode == "inline":
        if negatives:
            add("negative_inline", "avoid: " + ", ".join(negatives), PromptPriorityStyle)
            appended = compose_blocks(blocks, "\n\n", limit)
            result.positive = appended.prompt
            result.runes = appended.runes
            result.dropped = list(appended.dropped)
            if appended.over_limit:
                raise ComicPromptError(
                    f"漫画提示词 {appended.runes} 字符，超过上限 {limit}；"
                    "请减少格数/缩短台词，或调大 [comic].max_prompt_runes。"
                )
    else:
        result.negative = ", ".join(negatives)
    if result.positive.strip() == "":
        raise ComicPromptError("漫画提示词编译结果为空。")
    return result
