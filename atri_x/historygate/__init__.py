"""群聊历史写入闸门：策略 + 仓储包装。

对应 Go：``internal/historygate``（``gate.go``）。

对外表面与 Go 的 ``package historygate`` 等价：:class:`Policy`、
:class:`ChatRepository`、:class:`OutboundRepository`、``WrapChat`` → :func:`wrap_chat`、
``WrapOutbound`` → :func:`wrap_outbound`。另外按 PORTING §1.5 再导出
:class:`~atri_x.errors.RangeUnsupportedError`（Go 的包内哨兵 ``errRangeUnsupported``）。
"""

from __future__ import annotations

from atri_x.historygate.gate import (
    ChatRepository,
    OutboundRepository,
    Policy,
    RangeUnsupportedError,
    new_policy,
    policy_allowed,
    policy_set,
    wrap_chat,
    wrap_outbound,
)

__all__ = [
    "ChatRepository",
    "OutboundRepository",
    "Policy",
    "RangeUnsupportedError",
    "new_policy",
    "policy_allowed",
    "policy_set",
    "wrap_chat",
    "wrap_outbound",
]
