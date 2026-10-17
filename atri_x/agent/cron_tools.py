"""后台任务里 shell 工具的风险裁决（Agent 侧）。对应 Go：``internal/agent/cron_tools.go``。

职责：``confirmBackgroundSandboxShell`` —— 后台 cron / Atrinis 的 shell 调用**不等待
用户确认**：critical 直接拒绝并把提醒写进工具结果，其余风险（含 high）自动确认。

与 Go 的差异（形式，不是简化）：

* 纯决策部分已经在 :func:`atri_x.background.confirm_background_sandbox_shell`
  实现（共享契约，见 ``atri_x/background/background.py``）；本模块只负责 Go 里
  与 Agent 绑定的两件事：**写审计** 与 **拒绝时把提醒文本放进 tool 消息**。
* Go 的 ``message *llm.LLMMessage`` 出参 → 直接返回 :class:`ShellRiskDecision`
  并把文本写进传入的 ``message.segments``（与 Go 相同：调用方持有同一个对象）。
* 审计字段逐字照抄 Go（``background_shell_rejected`` /
  ``background_shell_auto_confirmed``，含 ``sandbox_dir`` 与参数预览）。
"""

from __future__ import annotations

from typing import Any

from atri_x.background import (
    BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED,
    BACKGROUND_SHELL_AUDIT_REJECTED,
    BACKGROUND_SHELL_CRITICAL_MESSAGE,
    ShellRiskDecision,
    confirm_background_sandbox_shell as _decide,
)
from atri_x.llm import text_segments
from atri_x.tool.sandbox import sandbox_context_from_context

from atri_x.agent.tools import preview_arguments

__all__ = [
    "confirm_background_sandbox_shell",
    "background_shell_decision",
]


def confirm_background_sandbox_shell(
    agent: Any,
    ctx: Any,
    session_id: str,
    call: Any,
    risk: str,
    message: Any = None,
) -> ShellRiskDecision:
    """后台 shell 的风险裁决 + 审计。对应 Go ``confirmBackgroundSandboxShell``。

    返回值与 Go 的 ``(bool, bool)`` 一致：``decision.confirm`` / ``decision.handled``，
    也可以解包成元组（:class:`ShellRiskDecision` 实现了 ``__iter__``）。
    """
    sandbox, ok = sandbox_context_from_context(ctx)
    decision = _decide(sandbox if ok else None, str(getattr(call, "name", "") or ""), risk)
    if not decision.handled:
        return decision
    kind = str(getattr(sandbox, "background_kind", "") or "")
    fields = {
        "session_id": session_id,
        "kind": kind,
        "tool": getattr(call, "name", ""),
        "risk": risk,
        "sandbox_dir": getattr(sandbox, "dir", ""),
        "arguments": preview_arguments(getattr(call, "arguments", "") or ""),
    }
    if decision.confirm:
        _audit(agent, BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, result="succeeded", **fields)
        return decision
    _audit(agent, BACKGROUND_SHELL_AUDIT_REJECTED, result="rejected", **fields)
    if message is not None:
        message.segments = text_segments(BACKGROUND_SHELL_CRITICAL_MESSAGE)
    return decision


def background_shell_decision(sandbox: Any, tool_name: str, risk: str) -> ShellRiskDecision:
    """纯决策入口（无审计、无 Agent）。对应共享包里的同名函数。"""
    return _decide(sandbox, tool_name, risk)


def _audit(agent: Any, event: str, **fields: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in fields.items():
        args.append(key)
        args.append(value)
    audit(*args)
