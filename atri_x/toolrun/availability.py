"""工具在上下文中的可用性与不可用原因。

对应 Go：``internal/toolrun/availability.go``。

`info_available_in_context` 是 `atri_x.tool` 的判断的**唯一入口转发**；把
`unavailable_reason` 放在这里是为了让「工具为什么不可用」的人类可读文本与
判定逻辑相邻，不会被分散到调用方。
"""

from __future__ import annotations

from typing import Any

from atri_x.tool.sandbox import background_context, info_available_in_context
from atri_x.tool.tool import Info

#: 前台专属工具在后台上下文里的原因文本（对应 Go 的字面量，供测试与日志断言）。
REASON_FOREGROUND_ONLY = "tool is only available in foreground sessions"

#: 其它不可用情形的原因文本。
REASON_UNAVAILABLE = "tool is unavailable in this context"


def available_in_context(ctx: Any, info: Info) -> bool:
    """对应 Go ``AvailableInContext``。"""
    return info_available_in_context(ctx, info)


def unavailable_reason(ctx: Any, info: Info) -> str:
    """对应 Go ``unavailableReason``。"""
    if getattr(info, "foreground_only", False) and background_context(ctx):
        return REASON_FOREGROUND_ONLY
    return REASON_UNAVAILABLE


# Go 的导出名
AvailableInContext = available_in_context

__all__ = [
    "AvailableInContext",
    "REASON_FOREGROUND_ONLY",
    "REASON_UNAVAILABLE",
    "available_in_context",
    "unavailable_reason",
]
