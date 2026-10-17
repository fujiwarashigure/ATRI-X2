"""命令前缀的读取与取消命令识别。

对应 Go：``internal/agent/prefix.go``。
"""

from __future__ import annotations

from typing import Any

__all__ = ["command_prefix", "command_prefixes", "is_cancel_command"]

#: Router 缺失或前缀为空时的回退前缀，与 Go 一致。
DEFAULT_COMMAND_PREFIX = "/"


def command_prefix(agent: Any) -> str:
    """当前配置的主命令前缀。对应 Go ``Agent.commandPrefix``。"""
    router = getattr(agent, "commands", None) if agent is not None else None
    if router is not None:
        prefix = (getattr(router, "primary_prefix", None) or (lambda: ""))()
        prefix = (prefix or "").strip()
        if prefix:
            return prefix
    return DEFAULT_COMMAND_PREFIX


def command_prefixes(agent: Any) -> list[str]:
    """当前配置的全部命令前缀。对应 Go ``Agent.commandPrefixes``。"""
    router = getattr(agent, "commands", None) if agent is not None else None
    if router is not None:
        getter = getattr(router, "prefixes", None)
        prefixes = list(getter() or []) if callable(getter) else []
        if prefixes:
            return prefixes
    return [DEFAULT_COMMAND_PREFIX]


def is_cancel_command(agent: Any, text: str) -> bool:
    """输入是否是 Hook 会话取消命令（``<prefix>cancel``）。

    对应 Go ``Agent.isCancelCommand``。Python 侧保留成自由函数，由
    :class:`atri_x.agent.core.Agent` 以同名方法转发。
    """
    value = (text or "").strip()
    if not value:
        return False
    return value == command_prefix(agent) + "cancel"
