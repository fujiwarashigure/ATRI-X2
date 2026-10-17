"""Atrinis 对外类型别名与常量。

对应 Go：``internal/atrinis/types.go``。

Go 这个文件只做一件事：把 ``internal/atriena`` 的协议类型与常量再导出到
``atrinis`` 包名下，让使用 Atrinis 的代码只 import 一个包。Python 侧同样只做
再导出（``from atri_x.atrinis import Request`` 与
``from atri_x.atriena import Request`` 是**同一个类**，``isinstance`` 跨模块有效）。
"""

from __future__ import annotations

from atri_x.atriena import (
    Atriwisp,
    Call,
    Event,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Request,
    Response,
    Segment,
    SegmentKind,
    SegmentKindFile,
    SegmentKindImage,
    SegmentKindText,
    StatusAccepted,
    StatusCompleted,
    StatusDelivering,
    StatusDuplicate,
    StatusFailed,
    StatusQueued,
    StatusResultReady,
    StatusRunning,
    StatusUnsupported,
    Target,
)

__all__ = [
    "Atriwisp",
    "Call",
    "Event",
    "ModeDirect",
    "ModeLLM",
    "ModeRecord",
    "Request",
    "Response",
    "Segment",
    "SegmentKind",
    "SegmentKindFile",
    "SegmentKindImage",
    "SegmentKindText",
    "StatusAccepted",
    "StatusCompleted",
    "StatusDelivering",
    "StatusDuplicate",
    "StatusFailed",
    "StatusQueued",
    "StatusResultReady",
    "StatusRunning",
    "StatusUnsupported",
    "Target",
]
