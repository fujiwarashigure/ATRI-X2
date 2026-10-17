"""提示词片段组装：按优先级整体丢弃，绝不切开权重表达式。

对应 Go：``internal/imagegen/prompt.go``。

Go 侧原先按 rune 硬截断提示词，会把 ``(artist_a:1.2)`` 切成 ``(artist_a:1.``；
本模块用「按优先级整体丢弃片段」取代它：protected 片段（画师串、角色硬约束）
永不丢弃，其余按 priority 从高到低丢，直到总长落入上限。即使丢光可丢片段仍
超限，也**不**退回截断，而是标记 ``over_limit``，由调用方拒绝这次请求。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ComposeResult",
    "PromptBlock",
    "PromptPriorityCore",
    "PromptPriorityOptional",
    "PromptPriorityProtected",
    "PromptPriorityStyle",
    "compose_blocks",
    "join_blocks",
]

#: 永不丢弃：用户点名的画师串或角色硬约束。
PromptPriorityProtected = 0
#: 用户自己要的内容：场景描述与对话上下文。
PromptPriorityCore = 1
#: 自动补的风格与画质修饰。
PromptPriorityStyle = 2
#: 库建议文本等附加内容。
PromptPriorityOptional = 3


@dataclass(slots=True)
class PromptBlock:
    """一段带标签的提示词片段。对应 Go ``PromptBlock``。"""

    label: str = ""
    text: str = ""
    priority: int = PromptPriorityProtected


@dataclass(slots=True)
class ComposeResult:
    """拼装结果。对应 Go ``ComposeResult``。"""

    #: 拼装后的提示词。
    prompt: str = ""
    #: 被丢弃片段的标签，按丢弃顺序（Go 里为空时是 nil，Python 里是空列表）。
    dropped: list[str] = field(default_factory=list)
    #: ``prompt`` 的 rune 长度。
    runes: int = 0
    #: 实际应用的上限；``0`` 表示未设上限。
    limit: int = 0
    #: 丢光可丢片段后仍超限。调用方此时应当拒绝请求，而不是发一段被切半的
    #: 提示词（对应 Go ``ComposeResult.OverLimit``）。
    over_limit: bool = False


def compose_blocks(blocks: list[PromptBlock], sep: str, limit: int) -> ComposeResult:
    """用 ``sep`` 连接片段，并整体丢弃片段直到结果不超过 ``limit`` 个 rune。

    片段永不被切开：切开权重表达式（``(artist_a:1.2)`` → ``(artist_a:1.``）在
    部分后端会被直接拒绝，另一些会把半截读成别的 tag。丢掉一整段可选内容比
    破坏一段受保护内容更可预测。``limit <= 0`` 表示不设上限。

    对应 Go ``ComposeBlocks``。
    """
    kept: list[PromptBlock] = []
    for block in blocks:
        if not str(block.text or "").strip():
            continue
        kept.append(block)
    result = ComposeResult(limit=limit)
    if limit <= 0:
        result.prompt = join_blocks(kept, sep)
        result.runes = len(result.prompt)
        return result
    while True:
        text = join_blocks(kept, sep)
        length = len(text)
        if length <= limit:
            result.prompt = text
            result.runes = length
            return result
        next_index = -1
        for index, block in enumerate(kept):
            if block.priority == PromptPriorityProtected:
                continue
            if next_index < 0:
                next_index = index
                continue
            # 先丢最不重要的片段（priority 数值越大越不重要）。注意上游注释写的是
            # 「同优先级丢最后一个」，但代码用的是严格大于，因此同优先级实际丢
            # **最先**出现的那一段；行为以代码为准，这里照样实现。
            if block.priority > kept[next_index].priority:
                next_index = index
        if next_index < 0:
            result.prompt = text
            result.runes = length
            result.over_limit = True
            return result
        result.dropped.append(kept[next_index].label)
        del kept[next_index]


def join_blocks(blocks: list[PromptBlock], sep: str) -> str:
    """连接非空片段并去掉首尾空白。对应 Go ``joinBlocks``。"""
    parts = [str(block.text).strip() for block in blocks if str(block.text or "").strip()]
    return sep.join(parts).strip()
