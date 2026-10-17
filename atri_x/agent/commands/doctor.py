"""``/doctor``：只读配置检查。

对应 Go：``internal/agent/commands/doctor.go``（33 行）。

``Info.MinRole`` 保持为空（Go 如此），路由层把空 ``MinRole`` 当作"仅超管"。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result, new_func
from atri_x.agent.commands.deps import (
    Deps,
    DoctorService,  # noqa: F401  (Go 在本文件声明该接口)
    Registrar,
    register_factories,
)

__all__ = ["DoctorModule", "new_doctor"]


def new_doctor(deps: Deps) -> Any:
    """暴露只读配置检查。对应 Go ``NewDoctor``。

    它从不改动文件；``Info.MinRole`` 留空，路由把空值当超管。
    """

    async def handle(ctx: Any, req: Request) -> Result | None:
        if deps.doctor is None:
            return Result(content="配置检查未启用。")
        text = deps.doctor.doctor(ctx)
        if hasattr(text, "__await__"):  # 实现可能是协程，也可能是同步返回值
            text = await text
        return Result(content=text)

    return new_func(
        Info(
            name="doctor",
            usage="/doctor",
            description=(
                "Read-only configuration check: reports missing items, TOML errors, "
                "unknown keys and built-in Skill differences. Only superadmins can "
                "use it; nothing is modified."
            ),
        ),
        handle,
    )


class DoctorModule:
    """对应 Go ``DoctorModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_doctor)
