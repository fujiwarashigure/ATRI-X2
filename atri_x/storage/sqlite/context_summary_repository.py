"""上下文摘要仓储。对应 Go：``internal/storage/sqlite/context_summary_repository.go``。

``latest_by_session_up_to`` 的排序与 Go 一致：先按**目标消息**的
``(created_at, id)`` 取"截至检查点"，再按摘要自身的 ``(created_at, id)`` 取最新一条。
注意 Go 用的是 ``m.id`` 而不是 ``rowid``（``message_repository`` 用的是 rowid），
本模块保持原样。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atri_x.errors import ErrNotFound
from atri_x.storage.ids import new_id
from atri_x.storage.models import ContextSummary
from atri_x.storage.sqlite.store import null_string, text_of
from atri_x.storage.times import format_time, is_zero_time, now, parse_time

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["ContextSummaryRepository"]

SUMMARY_COLUMNS = """id, session_id, from_message_id, to_message_id, summary, provider, model,
       source_tokens, summary_tokens, total_tokens, cache_hit_tokens,
       trigger_reason, metadata, created_at"""


class ContextSummaryRepository:
    """上下文摘要仓储。对应 Go ``sqlite.ContextSummaryRepository``。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    def create(self, summary: ContextSummary) -> None:
        """写入一条摘要（``id`` / ``created_at`` 为空时由实现填充）。"""
        if not summary.id:
            summary.id = new_id()
        if is_zero_time(summary.created_at):
            summary.created_at = now()
        self._db.execute(
            """
INSERT INTO context_summaries (
    id, session_id, from_message_id, to_message_id, summary, provider, model,
    source_tokens, summary_tokens, total_tokens, cache_hit_tokens,
    trigger_reason, metadata, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                summary.id,
                summary.session_id,
                null_string(summary.from_message_id),
                summary.to_message_id,
                summary.summary,
                summary.provider,
                summary.model,
                summary.source_tokens,
                summary.summary_tokens,
                summary.total_tokens,
                summary.cache_hit_tokens,
                summary.trigger_reason,
                null_string(summary.metadata),
                format_time(summary.created_at),
            ),
        )

    def latest_by_session(self, session_id: str) -> ContextSummary:
        """取会话最新摘要；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(
            f"""
SELECT {SUMMARY_COLUMNS}
FROM context_summaries
WHERE session_id = ?
ORDER BY created_at DESC, id DESC
LIMIT 1""",
            (session_id,),
        )
        if row is None:
            raise ErrNotFound
        return _scan_context_summary(row)

    def latest_by_session_up_to(self, session_id: str, to_message_id: str) -> ContextSummary:
        """取会话里截至 ``to_message_id``（含）的最新摘要；不存在抛 ErrNotFound。"""
        position = self._message_position(session_id, to_message_id)
        row = self._db.query_row(
            """
SELECT cs.id, cs.session_id, cs.from_message_id, cs.to_message_id, cs.summary, cs.provider, cs.model,
       cs.source_tokens, cs.summary_tokens, cs.total_tokens, cs.cache_hit_tokens,
       cs.trigger_reason, cs.metadata, cs.created_at
FROM context_summaries cs
JOIN messages m ON m.id = cs.to_message_id AND m.session_id = cs.session_id
WHERE cs.session_id = ? AND (m.created_at < ? OR (m.created_at = ? AND m.id <= ?))
ORDER BY m.created_at DESC, m.id DESC, cs.created_at DESC, cs.id DESC
LIMIT 1""",
            (session_id, position[0], position[0], position[1]),
        )
        if row is None:
            raise ErrNotFound
        return _scan_context_summary(row)

    def _message_position(self, session_id: str, message_id: str) -> tuple[str, str]:
        """检查点消息的 ``(created_at, id)``；不属于该会话时抛 ErrNotFound。"""
        row = self._db.query_row(
            """
SELECT created_at, id
FROM messages
WHERE id = ? AND session_id = ?""",
            (message_id, session_id),
        )
        if row is None:
            raise ErrNotFound
        return text_of(row["created_at"]), row["id"]


def _scan_context_summary(row) -> ContextSummary:
    """摘要行 → :class:`ContextSummary`（Go ``scanContextSummary``）。"""
    return ContextSummary(
        id=row["id"],
        session_id=row["session_id"],
        from_message_id=text_of(row["from_message_id"]),
        to_message_id=row["to_message_id"],
        summary=row["summary"],
        provider=row["provider"],
        model=row["model"],
        source_tokens=int(row["source_tokens"]),
        summary_tokens=int(row["summary_tokens"]),
        total_tokens=int(row["total_tokens"]),
        cache_hit_tokens=int(row["cache_hit_tokens"]),
        trigger_reason=row["trigger_reason"],
        metadata=text_of(row["metadata"]),
        created_at=parse_time(row["created_at"]),
    )
