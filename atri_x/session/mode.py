"""模式激活与模式校验。对应 Go：``internal/session/mode.go``。

work ↔ chat 的切换规则：没有当前会话就新建；已经是要切的模式就是
``already_active``；切到 chat 时**要求会话还没有历史消息**，否则抛
:data:`atri_x.errors.ErrChatModeRequiresEmptySession`（work 历史不能直接变成聊天的
上下文，必须先清空或新建会话）。
"""

from __future__ import annotations

from atri_x.errors import ErrChatModeRequiresEmptySession, InvalidError, NotFoundError
from atri_x.session.types import ActivateModeRequest, ActivateModeResult, CreateRequest, Scope
from atri_x.storage.models import SessionModeChat, SessionModeWork
from atri_x.storage.times import now

__all__ = ["ModeMixin", "validate_mode"]


class ModeMixin:
    """``Service`` 的模式部分（Go ``mode.go`` 的全部方法）。"""

    def activate_mode(self, scope: Scope, req: ActivateModeRequest) -> ActivateModeResult:
        """激活模式。对应 Go ``ActivateMode``。"""
        validate_mode(req.mode)
        try:
            session = self.current(scope)
        except NotFoundError:
            session = self.create(
                scope, CreateRequest(title=req.new_session_title, mode=req.mode)
            )
            return ActivateModeResult(session=session)
        if session.mode == req.mode:
            return ActivateModeResult(session=session, already_active=True)
        if req.mode == SessionModeChat:
            messages = self.store.messages().list_by_session(session.id)
            if messages:
                raise ErrChatModeRequiresEmptySession

        def update(current) -> None:
            current.mode = req.mode
            current.updated_at = now()

        updated = self.store.sessions().mutate(session.id, update)
        return ActivateModeResult(session=updated)


def validate_mode(mode: str) -> None:
    """校验模式；非法值抛 :class:`atri_x.errors.InvalidError`。对应 Go ``validateMode``。"""
    if mode in (SessionModeWork, SessionModeChat):
        return
    raise InvalidError(f'invalid session mode "{mode}"')
