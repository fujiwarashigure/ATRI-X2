"""Prompt 预算、token 估算与文本截断。

对应 Go：``internal/contextmgr/budget.go``。

**为什么不引入 tokenizer**：上游刻意用保守的启发式估算（CJK 偏保守地按
1 token/字、ASCII 按 3.5 字符/token），避免依赖具体 provider 的分词器。
估算偏高比偏低安全 —— 偏高只会多压缩一点，偏低会撞上 provider 的上下文上限。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Sequence

from atri_x.llm.llm import LLMMessage, SegmentType, ToolSchema

#: 对应 Go 的 const 块。
DEFAULT_MAX_PROMPT_RATIO = 0.8
DEFAULT_SINGLE_MESSAGE_MAX_RATIO = 0.5
DEFAULT_RESERVE_OUTPUT_TOKENS = 0
DEFAULT_USER_ORIGINAL_MAX_RUNES = 4000

#: 一张图片的固定估算。对应 Go ``imageTokenEstimate``。
IMAGE_TOKEN_ESTIMATE = 1024

#: 每条消息的固定开销。对应 Go ``MessageTokenOverhead``。
MESSAGE_TOKEN_OVERHEAD = 4

#: 单个文件段的固定开销。对应 Go ``EstimateMessageTokens`` 的 ``64 +``。
FILE_TOKEN_OVERHEAD = 64

#: ASGI 之外没有 provider 元数据时的兜底窗口。
#: 对应 Go ``config.DefaultContextWindow``（= 256000）。
FALLBACK_CONTEXT_WINDOW = 256000


@dataclass
class PromptBudget:
    """单次 LLM 请求的估算输入预算。对应 Go ``PromptBudget``。"""

    window: int = 0
    input_limit: int = 0
    single_message_limit: int = 0
    reserve_output_tokens: int = 0
    max_prompt_ratio: float = 0.0
    single_message_max_ratio: float = 0.0


def new_prompt_budget(
    window: int,
    max_prompt_ratio: float = 0.0,
    single_message_max_ratio: float = 0.0,
    reserve: int = 0,
) -> PromptBudget:
    """构造预算。``reserve <= 0`` 时按窗口算一个保守值。对应 Go ``NewPromptBudget``。"""
    max_prompt_ratio, single_message_max_ratio = _normalize_ratios(
        max_prompt_ratio, single_message_max_ratio
    )
    if window <= 0:
        return PromptBudget(
            max_prompt_ratio=max_prompt_ratio,
            single_message_max_ratio=single_message_max_ratio,
        )
    reserve = _normalize_reserve(reserve, window)
    input_limit = int(window * max_prompt_ratio)
    if window - reserve < input_limit:
        input_limit = window - reserve
    if input_limit < 0:
        input_limit = 0
    single_limit = int(window * single_message_max_ratio)
    if single_limit < 0:
        single_limit = 0
    return PromptBudget(
        window=window,
        input_limit=input_limit,
        single_message_limit=single_limit,
        reserve_output_tokens=reserve,
        max_prompt_ratio=max_prompt_ratio,
        single_message_max_ratio=single_message_max_ratio,
    )


def _normalize_ratios(a: float, b: float) -> tuple[float, float]:
    """与 Go 一致：不在 ``(0, 1)`` 开区间内的比值一律落到默认值。"""
    if a <= 0 or a >= 1:
        a = DEFAULT_MAX_PROMPT_RATIO
    if b <= 0 or b >= 1:
        b = DEFAULT_SINGLE_MESSAGE_MAX_RATIO
    return a, b


def _normalize_reserve(reserve: int, window: int) -> int:
    """与 Go 一致：``window/5`` 与 4096 取小、再与 8 取大；``>= window`` 时退回 ``window/5``。"""
    if reserve <= 0:
        reserve = min(window // 5, 4096)
        if reserve < 8:
            reserve = 8
    if reserve >= window:
        reserve = window // 5
    return reserve


def estimate_text_tokens(text: str) -> int:
    """估算文本 token 数。对应 Go ``EstimateTextTokens``。

    分类规则与 Go 的 ``unicode.Is`` 表对应（CJK 表用码点区间近似，
    覆盖 Han / Hangul / Hiragana / Katakana 与全角形式）。
    """
    if not text:
        return 0
    total = 0.0
    ascii_count = 0
    for ch in text:
        code = ord(ch)
        if code < 0x80:
            ascii_count += 1
        elif _is_cjk(code):
            total += 1.0
        else:
            total += 0.65
    total += ascii_count / 3.5
    if total < 1:
        total = 1
    return int(math.ceil(total))


def _is_cjk(code: int) -> bool:
    """对齐 Go 的 ``unicode.Han`` / ``Hangul`` / ``Hiragana`` / ``Katakana`` / 全角块。"""
    return (
        # Han（含扩展 A / 兼容区 / 扩展 B 及以后）
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
        # Hangul
        or 0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xAC00 <= code <= 0xD7AF
        # Hiragana / Katakana
        or 0x3040 <= code <= 0x309F
        or 0x30A0 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        # 全角形式
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_message_tokens(message: LLMMessage) -> int:
    """估算单条消息。对应 Go ``EstimateMessageTokens``。"""
    total = MESSAGE_TOKEN_OVERHEAD
    for segment in message.segments:
        if segment.type == SegmentType.TEXT:
            total += estimate_text_tokens(segment.text)
        elif segment.type == SegmentType.IMAGE:
            total += IMAGE_TOKEN_ESTIMATE
        elif segment.type == SegmentType.FILE:
            total += FILE_TOKEN_OVERHEAD + estimate_text_tokens(segment.text)
        else:
            total += estimate_text_tokens(segment.text)
    for call in message.tool_calls:
        total += (
            MESSAGE_TOKEN_OVERHEAD
            + estimate_text_tokens(call.name)
            + estimate_text_tokens(call.arguments)
        )
    return total


def estimate_messages_tokens(messages: Sequence[LLMMessage]) -> int:
    """对应 Go ``EstimateMessagesTokens``。"""
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_tools_tokens(tools: Sequence[ToolSchema]) -> int:
    """估算工具 schema 的 token 数。对应 Go ``EstimateToolsTokens``。

    Go 用 ``json.Marshal`` 后估算；序列化失败时退回 ``len(tools) * 128``。
    """
    if not tools:
        return 0
    try:
        payload = json.dumps(
            [tool_schema_to_json(t) for t in tools],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return len(tools) * 128
    return estimate_text_tokens(payload)


def tool_schema_to_json(tool: ToolSchema) -> dict[str, Any]:
    """把 :class:`ToolSchema` 还原成 OpenAI 风格 JSON（与 Go 的 tag 一致）。"""
    return {
        "type": tool.type or "function",
        "function": {
            "name": tool.function.name,
            "description": tool.function.description,
            "parameters": tool.function.parameters or {},
        },
    }


def truncate_text_with_marker(text: str, max_runes: int = 0, marker: str = "") -> str:
    """按 rune 截断并在截断处追加标记。对应 Go ``TruncateTextWithMarker``。

    ``max_runes <= 0`` 时用 :data:`DEFAULT_USER_ORIGINAL_MAX_RUNES`。
    注意：先 ``strip()`` 再按 rune 截断（与 Go 的顺序一致）。
    """
    if max_runes <= 0:
        max_runes = DEFAULT_USER_ORIGINAL_MAX_RUNES
    runes = list((text or "").strip())
    if len(runes) <= max_runes:
        return "".join(runes)
    return "".join(runes[:max_runes]) + marker


def truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """二分出不超 ``max_tokens`` 的最长文本前缀。对应 Go ``TruncateTextToTokens``。

    Go 用整数二分；这里保持同一算法（不用近似比例），以便与上游结果一致。
    """
    text = (text or "").strip()
    if not text or max_tokens <= 0 or estimate_text_tokens(text) <= max_tokens:
        return text
    runes = list(text)
    lo, hi = 1, len(runes)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if estimate_text_tokens("".join(runes[:mid])) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best <= 0:
        return ""
    return "".join(runes[:best])


__all__ = [
    "DEFAULT_MAX_PROMPT_RATIO",
    "DEFAULT_RESERVE_OUTPUT_TOKENS",
    "DEFAULT_SINGLE_MESSAGE_MAX_RATIO",
    "DEFAULT_USER_ORIGINAL_MAX_RUNES",
    "FALLBACK_CONTEXT_WINDOW",
    "FILE_TOKEN_OVERHEAD",
    "IMAGE_TOKEN_ESTIMATE",
    "MESSAGE_TOKEN_OVERHEAD",
    "PromptBudget",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "estimate_tools_tokens",
    "new_prompt_budget",
    "tool_schema_to_json",
    "truncate_text_to_tokens",
    "truncate_text_with_marker",
]
