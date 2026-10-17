"""会话查询：分页列表、可恢复列表与当前会话状态摘要。对应 Go：``internal/session/query.go``。

平台隔离与业务过滤都在这里收敛成 :class:`atri_x.storage.models.ListSessionsRequest`：
``owner_id`` + ``platform`` 永远带上（CLI 作用域例外，它跨平台可见），非 CLI 作用域
额外允许同平台的 cron Session（``include_same_platform_cron``），归档过滤交给
repository 的 ``archived_at`` 条件。
"""

from __future__ import annotations

from atri_x.errors import NotFoundError
from atri_x.session.shared import shared_thread_owner_id
from atri_x.session.text import preview
from atri_x.session.types import Scope, Status
from atri_x.storage.models import ListSessionsRequest, RoleAssistant, RoleUser

__all__ = ["QueryMixin"]


class QueryMixin:
    """``Service`` 的查询部分（Go ``query.go`` 的全部方法）。"""

    def list(self, scope: Scope, query: str, limit: int) -> list:
        """列会话（未归档）。对应 Go ``List``。"""
        return self.list_page(scope, query, limit, 0, False)

    def list_page(
        self, scope: Scope, query: str, limit: int, offset: int, archived_only: bool
    ) -> list:
        """分页列会话。对应 Go ``ListPage``。"""
        actor_id, include_cron = self.list_owner(scope)
        return self.store.sessions().list(
            ListSessionsRequest(
                actor_id=actor_id,
                platform=scope.platform,
                platform_scope_id=scope.platform_scope_id,
                include_all_platforms=scope.is_cli,
                include_same_platform_cron=include_cron,
                archived_only=archived_only,
                query=query,
                limit=limit,
                offset=offset,
            )
        )

    def list_resumable_page(self, scope: Scope, limit: int, offset: int) -> list:
        """列可恢复会话（排除当前会话，按最近活跃排序）。对应 Go ``ListResumablePage``。"""
        current_id = ""
        try:
            current = self.current(scope)
        except NotFoundError:
            current = None
        if current is not None:
            current_id = current.id
        actor_id, include_cron = self.list_owner(scope)
        return self.store.sessions().list(
            ListSessionsRequest(
                actor_id=actor_id,
                platform=scope.platform,
                platform_scope_id=scope.platform_scope_id,
                include_all_platforms=scope.is_cli,
                include_same_platform_cron=include_cron,
                exclude_session_id=current_id,
                order_by_updated_at=True,
                limit=limit,
                offset=offset,
            )
        )

    def status(self, scope: Scope) -> Status:
        """当前会话状态（消息数 + 最近一条 user/assistant 预览）。对应 Go ``Status``。"""
        session = self.current(scope)
        messages = self.store.messages().list_by_session(session.id)
        status = Status(session=session, message_count=len(messages))
        for message in reversed(messages):
            if message.role == RoleUser:
                if not status.last_user_preview:
                    status.last_user_preview = preview(message.content)
            elif message.role == RoleAssistant:
                if not status.last_answer_preview:
                    status.last_answer_preview = preview(message.content)
            if status.last_user_preview and status.last_answer_preview:
                break
        return status

    def list_owner(self, scope: Scope) -> tuple[str, bool]:
        """列表用的 owner 与"是否包含同平台 cron"标记。

        对应 Go ``ListPage`` / ``ListResumablePage`` 里重复的那段：共享线程作用域用
        合成 owner 且不看 cron；CLI 之外的作用域允许同平台 cron。
        """
        actor_id = scope.actor_id
        include_cron = not scope.is_cli
        if scope.shared:
            actor_id = shared_thread_owner_id(scope)
            include_cron = False
        return actor_id, include_cron
