"""回合输出的"最后一公里"闸门。

对应 Go：``internal/agent/turn_gate.go``。

请求管理器已经取消了 ctx；这里记下的状态是给"忽略取消、迟到返回结果"的工具/
provider 用的最后一道闸：被标记取消的 request_id 不再产生任何用户可见输出。
"""

from __future__ import annotations

import threading
from typing import Any

from atri_x.delivery import ctx_error
from atri_x.agent.request_context import turn_request_id_from_context

__all__ = [
    "clear_turn_state",
    "mark_turn_canceled",
    "turn_output_allowed",
]


def _lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "turn_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.turn_mu = lock
    return lock


def _states(agent: Any) -> dict[str, bool]:
    states = getattr(agent, "turn_states", None)
    if states is None:
        states = {}
        agent.turn_states = states
    return states


def mark_turn_canceled(agent: Any, request_id: str) -> None:
    """记录该回合不得再发出任何用户可见输出。对应 Go ``markTurnCanceled``。"""
    ident = (request_id or "").strip()
    if agent is None or not ident:
        return
    with _lock(agent):
        _states(agent)[ident] = True


def clear_turn_state(agent: Any, request_id: str) -> None:
    """回合及其排队发送都结束后摘掉终态标记。幂等。对应 Go ``clearTurnState``。"""
    ident = (request_id or "").strip()
    if agent is None or not ident:
        return
    with _lock(agent):
        _states(agent).pop(ident, None)


def turn_output_allowed(agent: Any, ctx: Any) -> bool:
    """ctx 上是否还允许发送输出。对应 Go ``turnOutputAllowed``。

    没有回合 request id 的 ctx 不做闸门，命令、通知与后台生命周期消息照常工作。
    """
    if agent is None:
        return True
    if ctx is not None and ctx_error(ctx) is not None:
        return False
    blocked = getattr(agent, "group_runtime_blocked", None)
    if callable(blocked) and blocked(ctx):
        return False
    request_id = turn_request_id_from_context(ctx) if ctx is not None else ""
    if not request_id:
        return True
    with _lock(agent):
        canceled = _states(agent).get(request_id, False)
    return not canceled
