"""一次 LLM 调用的**原因**标签（Python 版新增）。

为什么需要：``/usage`` 只按 模型 / 天 / 会话 分组时，看不出"这次调用到底算哪类
开销"——用户这句话是一次直聊、一次群聊围观参与、一次定时任务、一次外部事件
触发，还是一次记忆学习。原因标签把这类归因显式写进 ``llm_usage`` 审计事件，
``/usage --by trigger`` 才能下钻。

主聊天只有六种原因（与落地评估文档一致）：
``direct_message`` / ``ambient_participation`` / ``cron`` / ``external_event`` /
``memory_learning`` / ``retry``。旁路调用（压缩摘要、群分析、视觉、标题、图片
提示词重写）用 :func:`side_reason` 生成 ``side:<name>``，与主聊天区分开。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AMBIENT_PARTICIPATION",
    "CRON",
    "DIRECT_MESSAGE",
    "EXTERNAL_EVENT",
    "MEMORY_LEARNING",
    "REASONS",
    "RETRY",
    "UNKNOWN",
    "classify_call_reason",
    "normalize_reason",
    "side_reason",
]

#: 用户直接发消息触发的对话轮。
DIRECT_MESSAGE = "direct_message"
#: 群聊里没有直接 @ 机器人、但参与判定决定插话（对应评估 §D；当前未启用）。
AMBIENT_PARTICIPATION = "ambient_participation"
#: 定时任务 / 后台任务（cron、Atriwisp 自主行动）。
CRON = "cron"
#: 外部事件（撤回、入群、webhook 等非聊天输入）触发的调用。
EXTERNAL_EVENT = "external_event"
#: 记忆学习 / 压缩摘要这类"为了记住"而发的调用。
MEMORY_LEARNING = "memory_learning"
#: provider 重试产生的调用。
RETRY = "retry"
#: 无法归因的调用（老审计记录、未知来源）。
UNKNOWN = "unknown"

#: 主聊天可用的六种原因。
REASONS = (
    DIRECT_MESSAGE,
    AMBIENT_PARTICIPATION,
    CRON,
    EXTERNAL_EVENT,
    MEMORY_LEARNING,
    RETRY,
)

#: 旁路调用的原因前缀。
_SIDE_PREFIX = "side:"


def normalize_reason(value: str) -> str:
    """把任意标签归一成已知值；空串 / 不认识的走 ``unknown``。"""
    text = (value or "").strip().lower()
    if text in REASONS:
        return text
    if text.startswith(_SIDE_PREFIX) and len(text) > len(_SIDE_PREFIX):
        return text
    return UNKNOWN


def side_reason(name: str) -> str:
    """旁路调用的原因标签：``side:compaction`` / ``side:vision`` 等。"""
    text = (name or "").strip().lower().replace(" ", "_")
    return _SIDE_PREFIX + (text or "unknown")


def classify_call_reason(ctx: Any) -> str:
    """按当前上下文推断主聊天调用的原因。

    当前只区分"后台任务"与"用户直聊"：群聊参与判定（``ambient_participation``）
    还没启用，定时任务 / Atriwisp 走后台上下文 —— 它们都不该被算成用户直聊。
    """
    if ctx is None:
        return DIRECT_MESSAGE
    from atri_x.tool.sandbox import background_context

    if background_context(ctx):
        return CRON
    return DIRECT_MESSAGE
