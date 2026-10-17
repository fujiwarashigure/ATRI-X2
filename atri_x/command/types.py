"""通用命令框架的类型：``Info``、``Request``、``Result``、``SessionEffect`` 与接口。

对应 Go：``internal/command/types.go``。

风险/角色词汇在 Go 里属于 ``internal/security``；PORTING.md §3 把 ``RiskLevel``
归到 ``atri_x/tool/tool.py``，安全词汇的唯一实现见 :mod:`atri_x.tool.capability`。
这里优先从并行的 :mod:`atri_x.security` 导入，缺失时退回同一份定义，保证全仓只有
一套 ``Actor`` / ``Role`` / ``GroupRole``。

``Handler.Handle`` 与 ``Completer.Complete`` 都可能做 I/O（Go 侧返回 error），
因此 Python 侧是异步方法；``context.Context`` → 鸭子类型 ``ctx``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

try:  # pragma: no cover - 集成期走这一支
    from atri_x.security import (  # type: ignore[attr-defined]
        Actor,
        GroupRole,
        GroupRoleAdmin,
        GroupRoleMember,
        GroupRoleOwner,
        GroupRoleUnknown,
        Policy,
        Role,
        RoleSuperadmin,
        RoleUser,
    )
except ImportError:  # pragma: no cover - 兜底分支
    from atri_x.tool.capability import (  # noqa: F401
        Actor,
        GroupRole,
        GroupRoleAdmin,
        GroupRoleMember,
        GroupRoleOwner,
        GroupRoleUnknown,
        Policy,
        Role,
        RoleSuperadmin,
        RoleUser,
    )


@dataclass
class Info:
    """一条命令的元信息。对应 Go ``Info``。"""

    name: str = ""
    usage: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    help: str = ""
    session_effect: int = 0
    # min_role 控制 slash 命令准入。RoleUser 允许普通用户；留空按历史行为
    # 默认要求超管。
    min_role: str = ""
    # allow_group_admin 在超管之外额外放行群主/群管理员，但不放行普通群成员。
    allow_group_admin: bool = False
    # group_admin_needs_grant 把 allow_group_admin 变成"还需要服务端群级授权"，
    # 由命令执行器检查。用于 /learning moderation 这类敏感命令。
    group_admin_needs_grant: bool = False


class SessionEffect:
    """命令对当前 Session 的影响位。对应 Go ``SessionEffect``（``uint8`` 位标志）。"""

    NONE = 0
    SWITCH_CURRENT = 1
    MUTATE = 2

    ALL = (NONE, SWITCH_CURRENT, MUTATE)


# Go 同名的 PascalCase 常量别名。
SessionEffectNone = SessionEffect.NONE
SessionEffectSwitchCurrent = SessionEffect.SWITCH_CURRENT
SessionEffectMutate = SessionEffect.MUTATE


def can_access(info: Info, actor: Actor) -> bool:
    """对应 Go ``CanAccess``：命令级准入判定。"""
    if info.min_role == RoleUser or getattr(actor, "role", None) == RoleSuperadmin:
        return True
    if not info.allow_group_admin:
        return False
    group_role = getattr(actor, "group_role", "")
    return group_role in (GroupRoleOwner, GroupRoleAdmin)


@dataclass
class Request:
    """一次命令调用的入参。对应 Go ``Request``。"""

    raw: str = ""
    prefix: str = ""
    name: str = ""
    args: str = ""


@dataclass
class Continuation:
    """命令结果里的续接提示。对应 Go ``Continuation``。"""

    text: str = ""
    session_id: str = ""


@dataclass
class Result:
    """一次命令调用的结果。对应 Go ``Result``。"""

    content: str = ""
    continuation: Continuation | None = None


@dataclass
class CompletionRequest:
    """命令参数补全请求。对应 Go ``CompletionRequest``。"""

    raw: str = ""
    prefix: str = ""
    name: str = ""
    args: str = ""
    cursor: int = 0


@dataclass
class Completion:
    """一个命令参数补全候选。对应 Go ``Completion``。"""

    text: str = ""
    label: str = ""
    description: str = ""
    kind: str = ""
    replace_start: int = 0
    replace_end: int = 0


@runtime_checkable
class Completer(Protocol):
    """能补全自己参数的命令。对应 Go ``Completer``。"""

    async def complete(self, ctx: Any, req: CompletionRequest) -> list[Completion]: ...


@runtime_checkable
class Handler(Protocol):
    """一条可注册的命令。对应 Go ``Handler``。"""

    def info(self) -> Info: ...

    async def handle(self, ctx: Any, req: Request) -> Result | None: ...


__all__ = [name for name in dir() if not name.startswith("_")]
