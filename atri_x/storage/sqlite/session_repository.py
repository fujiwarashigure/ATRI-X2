"""会话仓储。对应 Go：``internal/storage/sqlite/session_repository.go``。

``mutate`` 是会话字段**唯一**的并发安全写入口：单事务读-改-写同一行并返回最新行；
``update`` 用作测试夹具的整行覆盖。``list`` 除列表字段外还带一小段消息预览
（Go 侧就是逐会话再查两次，本仓保持同样的 N+1 形状，不做"顺手优化"）。
"""

from __future__ import annotations


from atri_x.storage.times import order_time_expr

#: 时间列排序表达式：去掉结尾的 ``Z`` 再比较，避免同一秒内小数位长度不同导致的乱序
#: （见 :func:`atri_x.storage.times.order_time_expr`）。
_ORDER_TIME = order_time_expr("created_at")

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from atri_x.errors import AtriXError, ErrNotFound, InvalidError
from atri_x.storage.ids import new_id
from atri_x.storage.models import (
    ListSessionsRequest,
    RoleAssistant,
    Session,
    SessionModeWork,
    SessionStatusActive,
    SessionSummary,
)
from atri_x.storage.sqlite.store import null_string, null_time, text_of
from atri_x.storage.times import format_time, is_zero_time, now, parse_optional_time, parse_time

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["SessionRepository"]

SESSION_SELECT_BY_ID = """
SELECT id, parent_session_id, fork_from_message_id, owner_id, platform, platform_scope_id,
       mode, title, status, metadata, created_at, updated_at, archived_at, pinned_at
FROM sessions
WHERE id = ?"""

SESSION_UPDATE_STATEMENT = """
UPDATE sessions
SET parent_session_id = ?, fork_from_message_id = ?, owner_id = ?, platform = ?, platform_scope_id = ?,
    mode = ?, title = ?, status = ?, metadata = ?, created_at = ?, updated_at = ?, archived_at = ?, pinned_at = ?
WHERE id = ?"""


class SessionRepository:
    """会话仓储。对应 Go ``sqlite.SessionRepository``。"""
    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    # -- 写 ----------------------------------------------------------------

    def create(self, session: Session) -> None:
        """插入一行会话（``session.id`` 为空时由实现生成，并回填到传入的对象）。"""
        if not session.id:
            session.id = new_id()
        moment = now()
        if is_zero_time(session.created_at):
            session.created_at = moment
        if is_zero_time(session.updated_at):
            session.updated_at = session.created_at
        if not session.mode:
            session.mode = SessionModeWork
        if not session.status:
            session.status = SessionStatusActive

        self._db.execute(
            """
INSERT INTO sessions (
    id, parent_session_id, fork_from_message_id, owner_id, platform, platform_scope_id,
    mode, title, status, metadata, created_at, updated_at, archived_at, pinned_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                null_string(session.parent_session_id),
                null_string(session.fork_from_message_id),
                session.owner_id,
                session.platform,
                session.platform_scope_id,
                session.mode,
                null_string(session.title),
                session.status,
                null_string(session.metadata),
                format_time(session.created_at),
                format_time(session.updated_at),
                null_time(session.archived_at),
                null_time(session.pinned_at),
            ),
        )

    def update(self, session: Session) -> None:
        """整行覆盖更新；**只用于测试夹具**（并发写入方必须走 :meth:`mutate`）。"""
        if is_zero_time(session.updated_at):
            session.updated_at = now()
        cursor = self._db.execute(
            SESSION_UPDATE_STATEMENT,
            (*_session_update_args(session), session.id),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    def mutate(self, id: str, update: Callable[[Session], None]) -> Session:
        """单事务读-改-写同一行并返回最新会话。

        对应 Go ``Mutate(ctx, id, update func(*storage.Session) error)``。
        ``update`` 在事务内被调用、直接原地修改传入的 :class:`Session`；抛异常即中止
        整个事务（等价于 Go 里回调返回 error）并原样向上传播。
        """
        if update is None:
            raise InvalidError("session mutation is required")
        with self._db.with_transaction() as tx:
            row = tx.query_row(SESSION_SELECT_BY_ID, (id,))
            if row is None:
                raise ErrNotFound
            session = _scan_session(row)
            update(session)
            if is_zero_time(session.updated_at):
                session.updated_at = now()
            tx.execute(SESSION_UPDATE_STATEMENT, (*_session_update_args(session), session.id))
            return session

    def delete(self, id: str) -> None:
        """删除会话（``messages`` / ``context_summaries`` 由外键级联删除）。"""
        cursor = self._db.execute("DELETE FROM sessions WHERE id = ?", (id,))
        if cursor.rowcount == 0:
            raise ErrNotFound

    def delete_expired(self, cutoff: datetime) -> int:
        """删除 ``updated_at`` 早于 cutoff 的未归档且未置顶会话，返回删除行数。"""
        cursor = self._db.execute(
            """
DELETE FROM sessions
WHERE archived_at IS NULL
  AND pinned_at IS NULL
  AND updated_at < ?""",
            (format_time(cutoff),),
        )
        return int(cursor.rowcount)

    # -- 读 ----------------------------------------------------------------

    def get(self, id: str) -> Session:
        """按主键取会话；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(SESSION_SELECT_BY_ID, (id,))
        if row is None:
            raise ErrNotFound
        return _scan_session(row)

    def list(self, req: ListSessionsRequest) -> list[SessionSummary]:
        """按过滤条件列会话摘要。对应 Go ``SessionRepository.List``。"""
        limit = req.limit if req.limit > 0 else 20

        where: list[str] = []
        args: list[object] = []
        if not req.include_all_platforms:
            where.append("owner_id = ?")
            where.append("platform = ?")
            args.append(req.actor_id)
            args.append(req.platform)
            if req.include_same_platform_cron:
                where.append("(platform_scope_id = ? OR platform_scope_id LIKE 'cron:%')")
                args.append(req.platform_scope_id)
            else:
                where.append("platform_scope_id = ?")
                args.append(req.platform_scope_id)
        if req.archived_only:
            where.append("archived_at IS NOT NULL")
        elif not req.include_archived:
            where.append("archived_at IS NULL")
        if req.exclude_session_id:
            where.append("s.id <> ?")
            args.append(req.exclude_session_id)
        if req.query:
            where.append("title LIKE ?")
            args.append("%" + req.query + "%")
        if not where:
            where.append("1=1")
        args.append(limit)
        args.append(max(0, req.offset))

        order_by = "CASE WHEN s.pinned_at IS NULL THEN 1 ELSE 0 END, s.pinned_at DESC, s.updated_at DESC"
        if req.order_by_updated_at:
            order_by = "s.updated_at DESC, s.id DESC"
        query = f"""
SELECT s.id, s.owner_id, s.platform, s.platform_scope_id, s.title, s.mode, s.status,
       s.created_at, s.updated_at, s.archived_at, s.pinned_at,
       COUNT(m.id) AS message_count,
       COALESCE((SELECT content FROM messages WHERE session_id = s.id AND role = 'user' ORDER BY {_ORDER_TIME} DESC, rowid DESC LIMIT 1), '') AS last_user_preview,
       COALESCE((SELECT content FROM messages WHERE session_id = s.id AND role = 'assistant' ORDER BY {_ORDER_TIME} DESC, rowid DESC LIMIT 1), '') AS last_bot_preview
FROM sessions s
LEFT JOIN messages m ON m.session_id = s.id
WHERE {" AND ".join(where)}
GROUP BY s.id
ORDER BY {order_by}
LIMIT ? OFFSET ?"""

        summaries: list[SessionSummary] = []
        for row in self._db.query(query, args):
            summaries.append(
                SessionSummary(
                    id=row["id"],
                    owner_id=row["owner_id"],
                    platform=row["platform"],
                    platform_scope_id=row["platform_scope_id"],
                    title=text_of(row["title"]),
                    mode=row["mode"],
                    status=row["status"],
                    created_at=parse_time(row["created_at"]),
                    updated_at=parse_time(row["updated_at"]),
                    archived_at=parse_optional_time(text_of(row["archived_at"])),
                    pinned_at=parse_optional_time(text_of(row["pinned_at"])),
                    message_count=int(row["message_count"]),
                    last_user_preview=row["last_user_preview"],
                    last_bot_preview=row["last_bot_preview"],
                )
            )
        for summary in summaries:
            summary.message_preview = self._message_preview(summary.id)
        return summaries

    # -- 内部 --------------------------------------------------------------

    def _message_preview(self, session_id: str) -> str:
        """一段短预览（Go ``messagePreview``）。预览是装饰性的：查不动就当没有。"""
        try:
            rows = self._db.query(
                f"""
SELECT role, content
FROM messages
WHERE session_id = ? AND role IN ('user', 'assistant')
ORDER BY {_ORDER_TIME} ASC, rowid ASC
LIMIT 4""",
                (session_id,),
            )
        except AtriXError:  # 与 Go 一致：预览查询失败返回空串
            return ""

        parts: list[str] = []
        for row in rows:
            content = text_of(row["content"])
            if content.strip() == "":
                continue
            parts.append(f"{_role_prefix(row['role'])}: {_short_preview(content, 24)}")
        if not parts:
            return ""

        preview = " / ".join(parts)
        try:
            total_row = self._db.query_row(
                """
SELECT COUNT(*) AS total
FROM messages
WHERE session_id = ? AND role IN ('user', 'assistant')""",
                (session_id,),
            )
        except AtriXError:  # 同上
            return preview
        if total_row is not None and int(total_row["total"]) > len(parts):
            preview += " / ..."
        return preview


def _session_update_args(session: Session) -> tuple[object, ...]:
    """``sessionUpdateStatement`` 的参数（Go ``sessionUpdateArgs``）。"""
    return (
        null_string(session.parent_session_id),
        null_string(session.fork_from_message_id),
        session.owner_id,
        session.platform,
        session.platform_scope_id,
        session.mode,
        null_string(session.title),
        session.status,
        null_string(session.metadata),
        format_time(session.created_at),
        format_time(session.updated_at),
        null_time(session.archived_at),
        null_time(session.pinned_at),
    )


def _scan_session(row) -> Session:
    """``sessionSelectByID`` 的行 → :class:`Session`（Go ``scanSession``）。"""
    return Session(
        id=row["id"],
        parent_session_id=text_of(row["parent_session_id"]),
        fork_from_message_id=text_of(row["fork_from_message_id"]),
        owner_id=row["owner_id"],
        platform=row["platform"],
        platform_scope_id=row["platform_scope_id"],
        mode=row["mode"],
        title=text_of(row["title"]),
        status=row["status"],
        metadata=text_of(row["metadata"]),
        created_at=parse_time(row["created_at"]),
        updated_at=parse_time(row["updated_at"]),
        archived_at=parse_optional_time(text_of(row["archived_at"])),
        pinned_at=parse_optional_time(text_of(row["pinned_at"])),
    )


def _role_prefix(role: str) -> str:
    return "b" if role == RoleAssistant else "u"


def _short_preview(text: str, max_runes: int) -> str:
    """按 **rune**（Python 的码点）截断，对应 Go ``shortPreview`` 的 ``[]rune``。"""
    collapsed = text.replace("\n", " ").strip()
    if len(collapsed) <= max_runes:
        return collapsed
    return collapsed[:max_runes] + "..."
