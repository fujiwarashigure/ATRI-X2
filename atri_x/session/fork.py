"""从 assistant 消息 Fork 出新的当前分支。对应 Go：``internal/session/fork.go``。

Fork 只记录**检查点**（``parent_session_id`` + ``fork_from_message_id``）；历史消息
范围与媒体引用继承由 Session/Storage 支持（``ListBySessionUpTo``、
``media_fork_history`` 视图与 ``media_fork_created`` 触发器），**不在平台层拼接**。
"""

from __future__ import annotations

from atri_x.errors import ForbiddenError, InvalidError
from atri_x.session.shared import session_owner_id, shared_thread_metadata_for_scope
from atri_x.session.text import fork_title
from atri_x.session.types import Scope
from atri_x.storage.models import RoleAssistant, Session, SessionStatusActive

__all__ = ["ForkMixin"]


class ForkMixin:
    """``Service`` 的 Fork 部分（Go ``fork.go`` 的方法）。"""

    def fork(self, scope: Scope, from_message_id: str) -> Session:
        """从某条 assistant 消息 Fork。对应 Go ``Fork``。"""
        from_message_id = (from_message_id or "").strip()
        if not from_message_id:
            raise InvalidError("message id is required")
        message = self.store.messages().get(from_message_id)
        if message.role != RoleAssistant:
            raise InvalidError("can only fork from assistant messages")
        source = self.store.sessions().get(message.session_id)
        if not self.can_access(scope, source):
            raise ForbiddenError(f"session {source.id} is not in current platform scope")

        fork = Session(
            parent_session_id=source.id,
            fork_from_message_id=message.id,
            owner_id=session_owner_id(scope),
            platform=scope.platform,
            platform_scope_id=scope.platform_scope_id,
            mode=source.mode,
            status=SessionStatusActive,
            title=fork_title(source.title),
            metadata=shared_thread_metadata_for_scope(scope, ""),
        )
        self.store.sessions().create(fork)
        self.set_current(scope, fork.id)
        return fork
