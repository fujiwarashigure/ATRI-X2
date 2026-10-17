"""运维可见的请求快照。

对应 Go：``internal/agent/ops.go``。
"""

from __future__ import annotations

from typing import Any

from atri_x.request import Snapshot

__all__ = ["active_requests"]


def active_requests(agent: Any) -> Snapshot:
    """当前在跑的 turn / tool / hook / 压缩任务快照。

    对应 Go ``Agent.ActiveRequests``。
    """
    if agent is None or getattr(agent, "requests", None) is None:
        return Snapshot()
    return agent.requests.snapshot()
