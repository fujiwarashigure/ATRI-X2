"""旁路 LLM 用量的统一出口。**Python 版新增**。

背景：``llm_usage`` 审计事件原来只有主聊天（``agent.chat_llm``）和 DSH 路由会写，
压缩摘要 / 群分析 / 视觉理解 / 会话标题 / 图片提示词重写这 5 条**旁路**完全不计费、
不出现在 ``/usage`` 里。改法不是给每个组件硬塞一个 Agent 引用（它们大多与 Agent
无关，也有独立使用场景），而是留一个进程级 sink：组件只上报"我花了一次调用"，
由 Agent 构造时订阅的实现把它写成同一种审计事件。

没有订阅者时（组件被单独使用 / 测试）本模块是彻底的 no-op，不改变任何行为。
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.llm.llm import Usage

__all__ = [
    "UsageSink",
    "current_usage_sink",
    "record_side_usage",
    "set_usage_sink",
]

#: sink 收的载荷：原因、provider、model、用量、耗时。
UsageSink = Callable[[dict[str, Any]], None]

_sink: UsageSink | None = None


def set_usage_sink(sink: UsageSink | None) -> None:
    """安装 / 清除进程级用量 sink（Agent 构造时安装，清空用 ``None``）。"""
    global _sink
    _sink = sink


def current_usage_sink() -> UsageSink | None:
    """当前安装的 sink（测试用）。"""
    return _sink


def record_side_usage(
    *,
    reason: str,
    provider: str = "",
    model: str = "",
    usage: Usage | None = None,
    elapsed_ms: int = 0,
) -> None:
    """上报一次旁路调用的用量。没有 sink 或没有用量时什么都不做。"""
    sink = _sink
    if sink is None or usage is None:
        return
    sink(
        {
            "reason": reason,
            "provider": provider,
            "model": model,
            "usage": usage,
            "elapsed_ms": int(elapsed_ms or 0),
        }
    )
