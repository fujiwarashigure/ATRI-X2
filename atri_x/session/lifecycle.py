"""会话生命周期：重命名、归档、置顶、删除与过期清理。对应 Go：``internal/session/lifecycle.go``。

所有字段写入都在 ``Sessions().Mutate`` 事务内**基于最新行**重新判定（归档/置顶的
幂等短路、后台接管、手动改名标记），因此并发写入不会互相覆盖。

``unarchive`` 对后台 Session 同样执行前台接管：取消归档会把该 Session 设为当前
会话，若它仍是后台任务身份，在途的后台 turn 会继续把它当任务 Session 用。
"""

from __future__ import annotations

import json
from datetime import datetime

from atri_x.errors import ForbiddenError, InvalidError
from atri_x.session.promotion import is_background, promote_to_foreground
from atri_x.session.types import Scope
from atri_x.storage.models import Session
from atri_x.storage.times import now

__all__ = ["LifecycleMixin", "rename_metadata"]


class LifecycleMixin:
    """``Service`` 的生命周期部分（Go ``lifecycle.go`` 的全部方法）。"""

    def rename(self, scope: Scope, session_id: str, title: str) -> Session:
        """手动重命名（写 ``title_renamed`` / ``title_source`` 标记）。对应 Go ``Rename``。"""
        title = (title or "").strip()
        if not title:
            raise InvalidError("title is required")
        session = self.target_session(scope, session_id)

        def update(current: Session) -> None:
            current.metadata = rename_metadata(current.metadata)
            current.title = title
            current.updated_at = now()

        return self.store.sessions().mutate(session.id, update)

    def archive(self, scope: Scope, session_id: str) -> Session:
        """归档（已归档则原样返回）。对应 Go ``Archive``。"""
        session = self.target_session(scope, session_id)

        def update(current: Session) -> None:
            if current.archived_at is not None:
                return
            moment = now()
            current.archived_at = moment
            current.updated_at = moment

        return self.store.sessions().mutate(session.id, update)

    def unarchive(self, scope: Scope, session_id: str) -> Session:
        """取消归档并设为当前（后台 Session 同时前台接管）。对应 Go ``Unarchive``。"""
        session = self.target_session(scope, session_id)

        def update(current: Session) -> None:
            # 取消归档同时把该 Session 设为当前会话，因此后台 Session 在这里同样要
            # 走接管流程，否则在途的后台 turn 会继续把它当任务 Session 用。
            if is_background(current):
                promote_to_foreground(current, scope)
            if current.archived_at is None:
                return
            current.archived_at = None
            current.updated_at = now()

        updated = self.store.sessions().mutate(session.id, update)
        self.set_current(scope, session.id)
        return updated

    def pin(self, scope: Scope, session_id: str) -> Session:
        """置顶（已置顶则原样返回）。对应 Go ``Pin``。"""
        session = self.target_session(scope, session_id)

        def update(current: Session) -> None:
            if current.pinned_at is not None:
                return
            moment = now()
            current.pinned_at = moment
            current.updated_at = moment

        return self.store.sessions().mutate(session.id, update)

    def unpin(self, scope: Scope, session_id: str) -> Session:
        """取消置顶（未置顶则原样返回）。对应 Go ``Unpin``。"""
        session = self.target_session(scope, session_id)

        def update(current: Session) -> None:
            if current.pinned_at is None:
                return
            current.pinned_at = None
            current.updated_at = now()

        return self.store.sessions().mutate(session.id, update)

    def delete(self, scope: Scope, session_id: str) -> None:
        """删除会话（并把内存 current 里指向它的一条清掉）。对应 Go ``Delete``。"""
        session = self.target_session(scope, session_id)
        self.store.sessions().delete(session.id)
        self.clear_current_if(scope, session.id)

    def cleanup_expired(self, cutoff: datetime) -> int:
        """清理持久化的过期会话（未归档、未置顶、``updated_at`` 早于 cutoff）。

        对应 Go ``CleanupExpired``。
        """
        return self.store.sessions().delete_expired(cutoff)

    def target_session(self, scope: Scope, session_id: str) -> Session:
        """解析操作目标：空 ID 取当前会话，否则按 ID 取，并做访问判定。

        对应 Go ``targetSession``。
        """
        session_id = (session_id or "").strip()
        if not session_id:
            session = self.current(scope)
        else:
            session = self.store.sessions().get(session_id)
        if not self.can_access(scope, session):
            raise ForbiddenError(f"session {session.id} is not in current platform scope")
        return session


def rename_metadata(raw: str) -> str:
    """给 metadata 打上手动改名标记。对应 Go ``renameMetadata``。

    未知键一律保留；metadata 不是合法 JSON 对象时抛
    :class:`atri_x.errors.InvalidError`（对应 Go 的 ``decode session metadata`` 错误，
    会让整个事务回滚）。键按字典序输出（Go ``json.Marshal`` 对 map 排序）。
    """
    metadata: dict[str, object] = {}
    if (raw or "").strip():
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise InvalidError(f"decode session metadata: {exc}") from exc
        if not isinstance(decoded, dict):
            raise InvalidError("decode session metadata: not a JSON object")
        metadata = decoded
    metadata["title_renamed"] = True
    metadata["title_source"] = "manual"
    try:
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise InvalidError(f"encode session metadata: {exc}") from exc
