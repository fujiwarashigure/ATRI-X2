"""Slash 命令的执行与权限裁决。

对应 Go：``internal/agent/command_runtime.go``。

它是"命令层"和"回合层"之间的接缝：先按回合相位决定命令能不能执行，再按
``security.Actor`` 裁决权限，最后交给 :class:`atri_x.command.Router` 派发。
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.command import (
    Continuation,
    Info,
    SessionEffectMutate,
    SessionEffectSwitchCurrent,
    can_access,
)
from atri_x.logging import ResultRejected
from atri_x.security import (
    GroupRoleAdmin,
    GroupRoleOwner,
    RoleSuperadmin,
    actor_from_context,
)
from atri_x.storage import Session
from atri_x.turn import PhaseAwaitAppendConfirm, PhaseAwaitRiskConfirm, PhaseIdle

from atri_x.agent.logging import audit

__all__ = [
    "CommandExecutor",
    "blocks_during_active_turn",
    "blocks_during_compact",
    "is_risk_confirmation_command",
]


def blocks_during_active_turn(effect: int) -> bool:
    """回合进行中会切换当前会话的命令要拦。对应 Go ``blocksDuringActiveTurn``。"""
    return bool(effect & SessionEffectSwitchCurrent)


def blocks_during_compact(effect: int) -> bool:
    """压缩期间会切换或改写会话的命令要拦。对应 Go ``blocksDuringCompact``。"""
    return bool(effect & (SessionEffectSwitchCurrent | SessionEffectMutate))


def is_risk_confirmation_command(text: str, router: Any) -> bool:
    """这条命令是否属于风险确认命令集。对应 Go ``isRiskConfirmationCommand``。"""
    if router is None:
        return False
    parsed = router.parse(text)
    if not parsed.ok:
        return False
    from atri_x.agent.input import risk_confirmation_command_names

    return parsed.name in risk_confirmation_command_names()


class CommandExecutor:
    """命令执行器。对应 Go ``commandExecutor``。

    所有回调字段名与 Go 保持一致（``router`` / ``sessions`` / ``turns`` / ``scope``
    / ``compact_active`` / ``send_chat`` / ``send_notice`` / ``audit`` /
    ``handle_append`` / ``handle_risk`` / ``continue_input`` / ``group_admin_grant``）。
    """

    def __init__(
        self,
        router: Any = None,
        sessions: Any = None,
        turns: Any = None,
        scope: Callable[[Any], Any] | None = None,
        compact_active: Callable[[str], bool] | None = None,
        send_chat: Callable[..., Any] | None = None,
        send_notice: Callable[..., Any] | None = None,
        audit: Callable[..., Any] | None = None,
        handle_append: Callable[..., Any] | None = None,
        handle_risk: Callable[..., Any] | None = None,
        continue_input: Callable[..., Any] | None = None,
        group_admin_grant: Callable[..., Any] | None = None,
    ) -> None:
        self.router = router
        self.sessions = sessions
        self.turns = turns
        self.scope = scope
        self.compact_active = compact_active
        self.send_chat = send_chat
        self.send_notice = send_notice
        self.audit = audit
        self.handle_append = handle_append
        self.handle_risk = handle_risk
        self.continue_input = continue_input
        self.group_admin_grant = group_admin_grant

    async def handle(self, ctx: Any, text: str) -> tuple[bool, BaseException | None]:
        """处理一条入站文本。对应 Go ``commandExecutor.Handle``。

        返回 ``(是否已处理, 错误)``；未处理表示它不是命令，调用方应继续走聊天路径。
        """
        if self.router is None or not self.router.is_command(text):
            return False, None

        parsed = self.router.parse(text)
        info, has_info = self.router.command_info(parsed.name)
        session_row = None
        session_err: BaseException | None = None
        try:
            session_row = self.sessions.current(self.scope(ctx))
        except BaseException as exc:  # noqa: BLE001 - 会话缺失不是命令失败
            session_err = exc
        if session_err is None:
            snapshot = self.turns.snapshot(session_row.id)
        else:
            snapshot = _idle_snapshot()

        if session_err is None and snapshot.phase == PhaseAwaitAppendConfirm:
            from atri_x.turn import is_cancel, is_confirm

            if is_confirm(text) or is_cancel(text):
                return True, await self.handle_append(ctx, session_row, text)
        if (
            session_err is None
            and snapshot.phase == PhaseAwaitRiskConfirm
            and is_risk_confirmation_command(text, self.router)
        ):
            return True, await self.handle_risk(ctx, session_row.id, text)
        if (
            session_err is None
            and has_info
            and snapshot.phase != PhaseIdle
            and blocks_during_active_turn(info.session_effect)
        ):
            await self.send_chat(ctx, _active_turn_blocked_text(self.router))
            return True, None
        if (
            session_err is None
            and has_info
            and self.compact_active(session_row.id)
            and blocks_during_compact(info.session_effect)
        ):
            await self.send_chat(ctx, _compact_blocked_text(self.router, text))
            return True, None

        current_actor, _ok = actor_from_context(ctx)
        if (
            has_info
            and self.scope(ctx).shared
            and info.session_effect & (SessionEffectSwitchCurrent | SessionEffectMutate)
        ):
            if (
                current_actor.role != RoleSuperadmin
                and current_actor.group_role not in (GroupRoleOwner, GroupRoleAdmin)
            ):
                await self.send_chat(
                    ctx,
                    "当前群使用共享会话，会话切换/修改命令仅限群主、群管理员或机器人超级管理员。",
                )
                return True, None
        allowed = has_info and can_access(info, current_actor)
        if has_info and info.group_admin_needs_grant:
            # 群管理员权限是按群显式委派的：AllowGroupAdmin 单独不足以放行，只有
            # 服务端授权回调（或超级管理员）能解锁。
            granted = False
            if self.group_admin_grant is not None:
                granted = bool(self.group_admin_grant(ctx, parsed.name))
            allowed = current_actor.role == RoleSuperadmin or granted
        if has_info and not allowed:
            self.audit(
                "permission_denied",
                "actor_id",
                current_actor.id,
                "command",
                text,
                "reason",
                "slash_command_requires_superadmin",
                "result",
                ResultRejected,
            )
            required = "超级管理员"
            if info.group_admin_needs_grant:
                required = "超级管理员，或已由超级管理员显式授权的当前群管理员"
            elif info.allow_group_admin:
                required = "超级管理员或群管理员"
            await self.send_chat(
                ctx, f"命令 {parsed.prefix}{parsed.name} 需要{required}权限。"
            )
            return True, None

        try:
            result = await self.router.dispatch(ctx, text)
        except BaseException as exc:  # noqa: BLE001 - 命令错误原样冒泡
            return True, exc
        if result is None:
            return True, None
        if result.content:
            err = await self.send_notice(ctx, result.content)
            if err is not None:
                return True, err
        if result.continuation is not None:
            return True, await self.continue_input(ctx, result.continuation)
        return True, None

    # -- 提示文案（对应 Go input.go 里挂在 commandExecutor 上的两个方法）--------

    def compact_command_blocked_text(self, command: str) -> str:
        return _compact_blocked_text(self.router, command)

    def active_turn_command_blocked_text(self) -> str:
        return _active_turn_blocked_text(self.router)


def _idle_snapshot() -> Any:
    from atri_x.turn import Snapshot

    return Snapshot(phase=PhaseIdle)


def _active_turn_blocked_text(router: Any) -> str:
    return (
        "当前会话处理中，暂不支持切换。"
        f"如有必要，请先使用 {_prefix(router)}stop 结束当前处理。"
    )


def _compact_blocked_text(router: Any, command: str) -> str:
    return (
        f"正在压缩当前会话，暂不执行 {command}。"
        f"请等待压缩完成，或先使用 {_prefix(router)}stop 取消。"
    )


def _prefix(router: Any) -> str:
    try:
        return router.primary_prefix() or "/"
    except Exception:  # noqa: BLE001
        return "/"


# 便于交叉检索：命令上下文里出现的类型。
__all__ += ["Continuation", "Info", "Session", "audit"]
