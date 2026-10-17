"""``/grouppolicy``（别名 ``/gpolicy``、``/grouppolicies``）：当前群的策略配置。

对应 Go：``internal/agent/commands/group_policy.go``（93 行）。

作用域只能落在当前群：群管理员可改普通群策略，``allowed-models`` /
``learning-moderation`` / ``learning-moderation-actions`` 只能由超管配置
（由 :class:`atri_x.agent.commands.deps.GroupPolicyService` 实现侧裁决）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result
from atri_x.security import RoleSuperadmin
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = ["GroupPolicyModule", "new_group_policy"]

_HELP = """Usage:
  /grouppolicy
  /grouppolicy show
  /grouppolicy wake <词1,词2>
  /grouppolicy response <mention|all|keyword|reply|off>
  /grouppolicy default-mode <work|chat|inherit>
  /grouppolicy default-model <别名|clear>
  /grouppolicy allowed-models <别名或provider/model|*>  # 仅超级管理员
  /grouppolicy tool-allow <工具名,工具名|none|clear>
  /grouppolicy image-quota <次数>
  /grouppolicy vision-quota <次数>
  /grouppolicy asr-quota <次数>
  /grouppolicy user-image-quota <次数>
  /grouppolicy user-vision-quota <次数>
  /grouppolicy user-asr-quota <次数>
  /grouppolicy chat-tokens-quota <token 数>
  /grouppolicy chat-cost-quota <金额>
  /grouppolicy quiet <HH:MM-HH:MM|clear>
  /grouppolicy analysis <on|off>
  /grouppolicy learning <on|off>
  /grouppolicy history <on|off>
  /grouppolicy asr <on|off>
  /grouppolicy learning-moderation <on|off>   # 仅超级管理员
  /grouppolicy learning-moderation-actions <view,decide,mine>  # 仅超级管理员
  /grouppolicy reset [field]

Scope:
  只能操作当前群。群管理员可修改普通群策略；allowed-models、
  learning-moderation 和 learning-moderation-actions 只能由超级管理员配置。
  tool-allow none 表示当前群禁止全部工具；clear 表示继承全局工具策略。"""


class GroupPolicyCommand:
    """对应 Go ``groupPolicyCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="grouppolicy",
            aliases=["gpolicy", "grouppolicies"],
            usage="/grouppolicy [show|reset] [field] [value]",
            description="查看或修改当前群的唤醒、响应、工具与额度策略。",
            min_role=RoleSuperadmin,
            allow_group_admin=True,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if self._deps.group_policy is None:
            raise ValueError("group policy service is not configured")
        field_raw, separator, value_raw = (req.args or "").strip().partition(" ")
        field = field_raw.strip().lower()
        value = value_raw.strip()
        has_value = separator != ""
        if field in ("", "show", "status"):
            return Result(content=self._deps.group_policy.group_policy_status(ctx))
        if field == "reset":
            content = await _maybe_await(
                self._deps.group_policy.reset_group_policy(ctx, value)
            )
            return Result(content=content)
        if not has_value:
            return Result(
                content="用法：/grouppolicy <field> <value>；发送 /grouppolicy 查看当前策略。"
            )
        content = await _maybe_await(
            self._deps.group_policy.set_group_policy(ctx, field, value)
        )
        return Result(content=content)


async def _maybe_await(value: Any) -> Any:
    """服务实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def new_group_policy(deps: Deps) -> Any:
    """对应 Go ``NewGroupPolicy``。"""
    return GroupPolicyCommand(deps)


class GroupPolicyModule:
    """对应 Go ``GroupPolicyModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_group_policy)
