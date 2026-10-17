"""``/me``（别名 ``/mytasks``、``/my``、``/mypanel``）：普通成员自助面板。

对应 Go：``internal/agent/commands/member_panel.go``（57 行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result
from atri_x.security import RoleUser
from atri_x.agent.commands.deps import Deps, Registrar

__all__ = ["MemberPanelModule", "new_member_panel"]

_HELP = """Usage:
  /me              # 查看任务和额度
  /me tasks        # 只看当前会话与进行中任务
  /me quota        # 只看今日额度

说明:
  只显示当前平台/作用域内属于当前用户的请求与额度；
  /me 不会调用模型，也不会改变会话状态。"""


class MemberPanelCommand:
    """对应 Go ``memberPanelCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="me",
            aliases=["mytasks", "my", "mypanel"],
            usage="/me [tasks|quota|all]",
            description="查看我的进行中任务和今日额度。",
            min_role=RoleUser,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if self._deps.member_panel is None:
            raise ValueError("member panel service is not configured")
        view = (req.args or "").strip().lower()
        if view in ("help", "?"):
            return Result(content=self.info().help)
        content = self._deps.member_panel.member_panel(ctx, view)
        if hasattr(content, "__await__"):  # 实现可能是协程，也可能是同步返回值
            content = await content
        return Result(content=content)


def new_member_panel(deps: Deps) -> Any:
    """对应 Go ``NewMemberPanel``。"""
    return MemberPanelCommand(deps)


class MemberPanelModule:
    """对应 Go ``MemberPanelModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        registrar.register(new_member_panel(deps))
