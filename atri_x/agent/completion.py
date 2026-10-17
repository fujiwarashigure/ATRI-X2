"""结构化补全服务对外的两个门面。

对应 Go：``internal/agent/completion.go``。

``completion`` 的唯一出处是 :mod:`atri_x.completion`（``Service`` /
``Request`` / ``texts``），本模块只把 Agent 手里的服务暴露给平台适配器与测试。
"""

from __future__ import annotations

from typing import Any

from atri_x.completion import Request, Service, texts

__all__ = ["complete", "completion_service"]


def completion_service(agent: Any) -> Service | None:
    """平台适配器取结构化补全服务。对应 Go ``Agent.CompletionService``。"""
    return getattr(agent, "completion", None)


def complete(agent: Any, text: str) -> list[str]:
    """旧式适配器与测试用的纯文本补全。对应 Go ``Agent.Complete``。"""
    service = getattr(agent, "completion", None)
    if service is None:
        return []
    return texts(service.complete(None, Request(text=text)))
