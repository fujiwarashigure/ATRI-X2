"""DSH 轮次的数据形状：执行计划与执行结果（纯数据，无副作用）。

从 :mod:`atri_x.agent.dsh.runtime` 拆出来，既让 runtime 保持在 600 行内，也让
``route_call`` / ``service`` / 测试可以只依赖"数据"而不拉进线程与子进程代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.agent.dsh.types import AcpUsage

__all__ = ["DshRunResult", "DshTurnPlan"]


@dataclass(frozen=True)
class DshTurnPlan:
    """一次轮次的执行计划：发什么 prompt、复用哪个会话、失败时退回什么。

    由 :meth:`atri_x.agent.dsh.runtime.DshRuntime.plan_turn` 生成（命中亲和 = 只发
    增量），调用方把它原样交给 ``DshRuntime.run``；手工构造时可以只给 ``prompt``。
    """

    prompt: str = ""
    #: 复用会话不可用（过期/被内核丢弃）时退回的完整交接 prompt。
    fallback_prompt: str = ""
    #: 要复用的 DSH 会话 id；空 = 新建。
    session_id: str = ""
    #: 计划上是否想复用（真正是否复用由 worker 线程确认）。
    reused: bool = False
    #: 聊天键（亲和缓存键）。
    key: str = ""
    #: 当轮 ATRI-X 消息序列（存亲和时记锚点用）。
    messages: tuple[Any, ...] = ()


@dataclass
class DshRunResult:
    """一次路由执行的结果（与 ACP 结果解耦，便于测试构造）。"""

    text: str = ""
    thought: str = ""
    usage: AcpUsage = field(default_factory=AcpUsage)
    stop_reason: str = ""
    session_id: str = ""
    elapsed_seconds: float = 0.0
    tool_titles: list[str] = field(default_factory=list)
    server: str = ""
    #: 内核是否真的回报了 ``usage``；为 ``False`` 时上面的 ``usage`` 只是全 0 占位。
    usage_reported: bool = False
    #: 本轮被内核拒绝的会话配置（例如 ``model`` 填了裸模型 id）：降级说明，供审计。
    warnings: list[str] = field(default_factory=list)
    #: 为拿并发槽等了多久（秒）。
    queue_wait_seconds: float = 0.0
    #: 是否复用了已有 DSH 会话（亲和命中）。
    session_reused: bool = False
    #: 是否被看门狗判定卡死并取消。
    stalled: bool = False

    @property
    def ok(self) -> bool:
        """是否正常结束（``end_turn``）。"""
        return self.stop_reason == "end_turn"

    def to_dict(self) -> dict[str, Any]:
        """给审计用的普通字典（不展开事件）。"""
        return {
            "sessionId": self.session_id,
            "stopReason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "usageReported": self.usage_reported,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "toolCalls": len(self.tool_titles),
            "server": self.server,
            "warnings": list(self.warnings),
            "queueWaitSeconds": round(self.queue_wait_seconds, 3),
            "sessionReused": self.session_reused,
            "stalled": self.stalled,
        }
