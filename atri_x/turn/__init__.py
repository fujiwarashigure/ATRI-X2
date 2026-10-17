"""单个 Session 的 turn 阶段、pending 追加与确认状态。

对应 Go：``internal/turn/``。
"""

from __future__ import annotations

from atri_x.turn.manager import (
    Input,
    Manager,
    Part,
    Phase,
    PhaseAwaitAppendConfirm,
    PhaseAwaitRiskConfirm,
    PhaseCompact,
    PhaseIdle,
    PhaseLLM,
    PhaseTool,
    RiskConfirmation,
    RiskConfirmationResponse,
    Snapshot,
    Speaker,
    is_cancel,
    is_confirm,
    merge_inputs,
    normalize_input,
    text_input,
    tools_string,
)

__all__ = [name for name in dir() if not name.startswith("_")]
