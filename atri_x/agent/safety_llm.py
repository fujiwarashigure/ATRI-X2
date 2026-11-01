"""模型输出侧的内容安全接线（从 :mod:`atri_x.agent.chat_llm` 抽出）。

拆出来的原因：``chat_llm.py`` 已超 600 行并被行数棘轮冻结（只降不升），1.1.8 要在
这里加"流式 delta 缓冲 + 远程审核"，先在 P1 做纯搬迁。

1.1.8 起这里还会是流式缓冲（:mod:`atri_x.safety.stream`）的唯一接线处：
调用方每次拿到一段 delta 都交给这里，由它决定"立刻发 / 攒到句末再发 / 不发"。
P1 阶段只保留原有的"最终文本过滤"。

**不做什么**：不碰 LLM 调用本身、不改 usage 记账。
"""

from __future__ import annotations

from typing import Any

from atri_x.safety import mode_from_host, scope_from_host

__all__ = ["afilter_llm_text", "filter_llm_text"]


async def afilter_llm_text(agent: Any, ctx: Any, text: str) -> str:
    """异步版：本地层 + 远程层 + 三档裁决（1.1.8 起模型输出的主路径）。"""
    guard = getattr(agent, "safety_guard", None)
    if guard is None:
        return text
    filtered, _decision = await guard.afilter_text(
        text,
        stage="llm",
        scope=scope_from_host(agent, ctx),
        mode=mode_from_host(agent, ctx),
    )
    return filtered


def filter_llm_text(agent: Any, ctx: Any, text: str) -> str:
    """把模型输出文本过一遍安全闸门，返回"应该采用"的文本。

    ``guard is None`` / ``mode = off`` 时原样返回（不配置就完全无行为变化）。
    ``enforce`` 下返回脱敏或拦截文案；``audit`` 下永远返回原文（只写审计）。
    """
    guard = getattr(agent, "safety_guard", None)
    if guard is None:
        return text
    filtered, _decision = guard.filter_text(
        text,
        stage="llm",
        scope=scope_from_host(agent, ctx),
        mode=mode_from_host(agent, ctx),
    )
    return filtered
