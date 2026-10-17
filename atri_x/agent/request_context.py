"""回合请求 ID 在 ctx 上的读写。

对应 Go：``internal/agent/request_context.go``。

``ctx`` 是鸭子类型（见 :mod:`atri_x.delivery` 的 ``ctx_value`` /
``ctx_with_value``）：优先用 ctx 自带的 ``value`` / ``with_value``
（:class:`atri_x.safecontext.CancelToken` 即这套接口），否则退回 ``Mapping``。
"""

from __future__ import annotations

from typing import Any

from atri_x.delivery import ctx_value, ctx_with_value

__all__ = ["TurnRequestID", "turn_request_id_from_context", "with_turn_request_id"]

#: Go 的 ``turnRequestIDKey{}``。
TurnRequestID = object()


def with_turn_request_id(ctx: Any, ident: str) -> Any:
    """对应 Go ``withTurnRequestID``（``id`` 为空时原样返回）。"""
    if not ident:
        return ctx
    return ctx_with_value(ctx, TurnRequestID, ident)


def turn_request_id_from_context(ctx: Any) -> str:
    """对应 Go ``turnRequestIDFromContext``。"""
    value = ctx_value(ctx, TurnRequestID)
    return value if isinstance(value, str) else ""
