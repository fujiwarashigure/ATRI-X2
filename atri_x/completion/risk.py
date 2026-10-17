"""风险确认阶段的命令补全来源。

对应 Go：``internal/completion/risk.go``。

``session.Service`` / ``turn.Manager`` 属于并行任务的模块，这里只按方法鸭子类型
消费（``sessions.current(ctx, scope)`` / ``turns.snapshot(session_id).phase``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from atri_x.command import Router
from atri_x.completion.completion import Item, Request

KIND_RISK_CONFIRMATION = "risk_confirmation"
KindRiskConfirmation = KIND_RISK_CONFIRMATION

# 对应 Go ``turn.PhaseAwaitRiskConfirm`` 的值（避免为常量拉入整个 turn 包）。
PHASE_AWAIT_RISK_CONFIRM = "awaiting_risk_confirm"

# ScopeFunc 取当前会话的 scope（Go ``func(context.Context) session.Scope``）。
ScopeFunc = Callable[[Any], Any]


@dataclass
class RiskConfirmationSource:
    """在等待风险确认时补全确认/取消命令。对应 Go ``RiskConfirmationSource``。"""

    router: Router | None = None
    sessions: Any = None
    turns: Any = None
    scope: ScopeFunc | None = None
    command_names: Sequence[str] = field(default_factory=list)

    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``RiskConfirmationSource.Complete``。"""
        if (
            self.router is None
            or self.sessions is None
            or self.turns is None
            or self.scope is None
            or not self.command_names
        ):
            return []
        try:
            session = await self.sessions.current(ctx, self.scope(ctx))
        except BaseException:
            return []
        if session is None:
            return []
        try:
            phase = self.turns.snapshot(session.id).phase
        except BaseException:
            return []
        if phase != PHASE_AWAIT_RISK_CONFIRM:
            return []
        parsed = self.router.parse(req.text)
        if not parsed.ok or has_any_command_args(req.text, parsed.prefix):
            return []
        out: list[Item] = []
        for name in self.command_names:
            if name.startswith(parsed.name):
                out.append(
                    Item(
                        text=parsed.prefix + name,
                        label=name,
                        kind=KIND_RISK_CONFIRMATION,
                    )
                )
        return out


def has_any_command_args(text: str, prefix: str) -> bool:
    """对应 Go ``hasAnyCommandArgs``：命令名之后是否已经有参数。

    Go 的 ``strings.Cut`` 返回 ``(before, after, found)``；Python 的
    ``str.partition`` 返回 ``(before, sep, after)``，因此顺序不同。
    """
    text = (text or "").lstrip(" \t")
    if not text.startswith(prefix):
        return False
    rest = text[len(prefix):].strip()
    _, sep, args = rest.partition(" ")
    return sep != "" and args.strip() != ""


__all__ = [name for name in dir() if not name.startswith("_")]
