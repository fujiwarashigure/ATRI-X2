"""``/compact``：手动压缩当前会话上下文。

对应 Go：``internal/agent/commands/compact.go``（25 行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import (
    Info,
    Request,
    Result,
    SessionEffectMutate,
    new_func,
)
from atri_x.security import RoleUser
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = ["CompactModule", "new_compact"]


def new_compact(deps: Deps) -> Any:
    """对应 Go ``NewCompact``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        # Go 的 CompactCurrent 会做 I/O 并返回 error；实现可能是协程也可能是同步
        # 返回值，两种都接受。
        content = deps.compact.compact_current(ctx, "manual")
        if hasattr(content, "__await__"):
            content = await content
        return Result(content=content)

    return new_func(
        Info(
            name="compact",
            usage="/compact",
            description="Compact current session context.",
            session_effect=SessionEffectMutate,
            min_role=RoleUser,
        ),
        handle,
    )


class CompactModule:
    """对应 Go ``CompactModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_compact)
