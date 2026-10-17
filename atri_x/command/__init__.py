"""通用命令框架：``Info`` / ``Request`` / ``Result`` / ``Router``。

对应 Go 包：``internal/command``（``types.go`` / ``router.go`` / ``handler.go``）。
"""

from __future__ import annotations

from atri_x.command.handler import Func, FuncHandler, new_func  # noqa: F401
from atri_x.command.router import (  # noqa: F401
    DEFAULT_COMMAND_PREFIX,
    Parsed,
    Router,
    new_router,
    normalize_name,
    normalize_prefixes,
    primary_prefix,
)
from atri_x.command.types import (  # noqa: F401
    Actor,
    Completer,
    Completion,
    CompletionRequest,
    Continuation,
    GroupRole,
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    GroupRoleUnknown,
    Handler,
    Info,
    Policy,
    Request,
    Result,
    Role,
    RoleSuperadmin,
    RoleUser,
    SessionEffect,
    SessionEffectMutate,
    SessionEffectNone,
    SessionEffectSwitchCurrent,
    can_access,
)

__all__ = [name for name in dir() if not name.startswith("_")]
