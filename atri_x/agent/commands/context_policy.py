"""``/overflow``（别名 ``/ctxpolicy``、``/longmsg``）：长消息溢出策略。

对应 Go：``internal/agent/commands/context_policy.go``（93 行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result
from atri_x.security import RoleSuperadmin
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = ["ContextPolicyModule", "new_context_policy"]

_HELP = """Usage:
  /overflow
  /overflow show
  /overflow [--chat|--work|--all] <reject|truncate|summarize>
  /overflow reset [--chat|--work|--all]

Values:
  reject       Reject the oversized message, do not call the model.
  truncate     Keep a fitting prefix and continue.
  summarize    Summarize the oversized message with the compact model.

Examples:
  /overflow
  /overflow --work summarize
  /overflow --chat truncate
  /overflow reset --work"""


class ContextPolicyCommand:
    """对应 Go ``contextPolicyCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="overflow",
            aliases=["ctxpolicy", "longmsg"],
            usage=(
                "/overflow [show|reset] [--chat|--work|--all] "
                "[reject|truncate|summarize]"
            ),
            description="Show or change long-message overflow policy for this chat.",
            min_role=RoleSuperadmin,
            allow_group_admin=True,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if self._deps.context_policy is None:
            raise ValueError("context policy service is not configured")
        args = (req.args or "").split()
        if not args:
            return Result(content=self._deps.context_policy.context_policy_status(ctx))
        target = "all"
        value = ""
        for arg in args:
            normalized = (arg or "").strip().lower()
            if normalized in ("--chat", "chat"):
                target = "chat"
            elif normalized in ("--work", "work"):
                target = "work"
            elif normalized in ("--all", "all"):
                target = "all"
            elif normalized in ("show", "status"):
                value = "show"
            else:
                value = normalized
        if value in ("", "show", "status"):
            return Result(content=self._deps.context_policy.context_policy_status(ctx))
        if value == "reset":
            content = await _maybe_await(
                self._deps.context_policy.reset_context_policy(ctx, target)
            )
            return Result(content=content)
        content = await _maybe_await(
            self._deps.context_policy.set_context_policy(ctx, target, value)
        )
        return Result(content=content)


async def _maybe_await(value: Any) -> Any:
    """服务实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def new_context_policy(deps: Deps) -> Any:
    """对应 Go ``NewContextPolicy``。"""
    return ContextPolicyCommand(deps)


class ContextPolicyModule:
    """对应 Go ``ContextPolicyModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_context_policy)
