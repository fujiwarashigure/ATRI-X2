"""会话消息仓储。对应 Go：``internal/storage/sqlite/message_repository.go``。

两条正文路径：

* ``content`` 是纯文本快速路径（列表预览、贴上下文都用它）；
* ``segments`` 是可选的 JSON 多模态正文，空串落库为 **NULL**（Go 用 ``nullString``）。

``append`` 在**一个事务**里插消息行 + 按 ``segments`` 里的 ``media`` 建 ``media_references``：
segments 解不开、或引用了不存在的媒体（外键失败）时整条消息回滚 —— 这是"消息与媒体引用
要么都在、要么都不在"的唯一保证，不能拆成两步。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from atri_x.errors import ErrNotFound, InvalidError
from atri_x.storage.ids import new_id
from atri_x.storage.models import Message, PlatformMessageMap, RoleTool
from atri_x.storage.sqlite.store import null_string, text_of
from atri_x.storage.times import format_time, is_zero_time, now, parse_time

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["MessageRepository"]

MESSAGE_COLUMNS = """id, session_id, role, content, parent_message_id, reply_to_platform_message_id,
       reply_to_message_id, tool_call_id, segments, metadata, created_at"""


class MessageRepository:
    """会话消息仓储。对应 Go ``sqlite.MessageRepository``。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    def append(self, message: Message) -> None:
        """追加一条消息（``created_at`` 为零值时由实现填充，并回填到传入的对象）。"""
        if not message.id:
            message.id = new_id()
        if is_zero_time(message.created_at):
            message.created_at = now()

        with self._db.with_transaction() as tx:
            tx.execute(
                """
INSERT INTO messages (
    id, session_id, role, content, parent_message_id, reply_to_platform_message_id,
    reply_to_message_id, tool_call_id, segments, metadata, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.id,
                    message.session_id,
                    message.role,
                    message.content,
                    null_string(message.parent_message_id),
                    null_string(message.reply_to_platform_message_id),
                    null_string(message.reply_to_message_id),
                    null_string(message.tool_call_id),
                    null_string(message.segments),
                    null_string(message.metadata),
                    format_time(message.created_at),
                ),
            )
            owner_type = "tool_result" if message.role == RoleTool else "message"
            for media_id in _segment_media_ids(message.segments):
                tx.execute(
                    """
INSERT INTO media_references(media_id, owner_type, owner_id, purpose, session_id, created_at)
VALUES (?, ?, ?, 'content', ?, ?) ON CONFLICT DO NOTHING""",
                    (
                        media_id,
                        owner_type,
                        message.id,
                        message.session_id,
                        format_time(message.created_at),
                    ),
                )

    def get(self, id: str) -> Message:
        """按主键取消息；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(
            f"""
SELECT {MESSAGE_COLUMNS}
FROM messages
WHERE id = ?""",
            (id,),
        )
        if row is None:
            raise ErrNotFound
        return _scan_message(row)

    def list_by_session(self, session_id: str) -> list[Message]:
        """按创建顺序（同刻按插入顺序）列出会话全部消息。"""
        rows = self._db.query(
            f"""
SELECT {MESSAGE_COLUMNS}
FROM messages
WHERE session_id = ?
ORDER BY created_at ASC, rowid ASC""",
            (session_id,),
        )
        return [_scan_message(row) for row in rows]

    def list_by_session_up_to(self, session_id: str, to_message_id: str) -> list[Message]:
        """列出会话里截至 ``to_message_id``（含）的消息。"""
        to_created_at, to_row_id = self._message_position(session_id, to_message_id)
        rows = self._db.query(
            f"""
SELECT {MESSAGE_COLUMNS}
FROM messages
WHERE session_id = ? AND (created_at < ? OR (created_at = ? AND rowid <= ?))
ORDER BY created_at ASC, rowid ASC""",
            (session_id, to_created_at, to_created_at, to_row_id),
        )
        return [_scan_message(row) for row in rows]

    def list_by_session_after(self, session_id: str, after_message_id: str) -> list[Message]:
        """列出会话里 ``after_message_id`` 之后的消息。"""
        after_created_at, after_row_id = self._message_position(session_id, after_message_id)
        rows = self._db.query(
            f"""
SELECT {MESSAGE_COLUMNS}
FROM messages
WHERE session_id = ? AND (created_at > ? OR (created_at = ? AND rowid > ?))
ORDER BY created_at ASC, rowid ASC""",
            (session_id, after_created_at, after_created_at, after_row_id),
        )
        return [_scan_message(row) for row in rows]

    def list_by_session_after_up_to(
        self, session_id: str, after_message_id: str, to_message_id: str
    ) -> list[Message]:
        """列出会话里 ``after_message_id``（不含）到 ``to_message_id``（含）之间的消息。"""
        after_created_at, after_row_id = self._message_position(session_id, after_message_id)
        to_created_at, to_row_id = self._message_position(session_id, to_message_id)
        rows = self._db.query(
            f"""
SELECT {MESSAGE_COLUMNS}
FROM messages
WHERE session_id = ?
  AND (created_at > ? OR (created_at = ? AND rowid > ?))
  AND (created_at < ? OR (created_at = ? AND rowid <= ?))
ORDER BY created_at ASC, rowid ASC""",
            (
                session_id,
                after_created_at,
                after_created_at,
                after_row_id,
                to_created_at,
                to_created_at,
                to_row_id,
            ),
        )
        return [_scan_message(row) for row in rows]

    def map_platform_message(self, mapping: PlatformMessageMap) -> None:
        """写入 ``platform_message_map``（平台消息 ID → 会话消息 ID）。"""
        if not mapping.id:
            mapping.id = new_id()
        if is_zero_time(mapping.created_at):
            mapping.created_at = now()
        self._db.execute(
            """
INSERT INTO platform_message_map (
    id, platform, platform_scope_id, platform_message_id, message_id, session_id, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mapping.id,
                mapping.platform,
                mapping.platform_scope_id,
                mapping.platform_message_id,
                mapping.message_id,
                mapping.session_id,
                format_time(mapping.created_at),
            ),
        )

    def find_by_platform_message(self, platform: str, scope_id: str, platform_message_id: str) -> Message:
        """按平台消息反查会话消息；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(
            """
SELECT m.id, m.session_id, m.role, m.content, m.parent_message_id, m.reply_to_platform_message_id,
       m.reply_to_message_id, m.tool_call_id, m.segments, m.metadata, m.created_at
FROM platform_message_map p
JOIN messages m ON m.id = p.message_id
WHERE p.platform = ? AND p.platform_scope_id = ? AND p.platform_message_id = ?""",
            (platform, scope_id, platform_message_id),
        )
        if row is None:
            raise ErrNotFound
        return _scan_message(row)

    # -- 内部 --------------------------------------------------------------

    def _message_position(self, session_id: str, message_id: str) -> tuple[str, int]:
        """取检查点消息的 ``(created_at, rowid)``；不属于该会话时抛 ErrNotFound。"""
        row = self._db.query_row(
            """
SELECT created_at, rowid AS row_id
FROM messages
WHERE id = ? AND session_id = ?""",
            (message_id, session_id),
        )
        if row is None:
            raise ErrNotFound
        return text_of(row["created_at"]), int(row["row_id"])


def _segment_media_ids(raw: str) -> list[str]:
    """从 ``segments`` JSON 里取出全部 ``media`` ID（Go 在 ``Append`` 里做同一件事）。

    Go 把 segments 解成 ``[]struct{ MediaID string `json:"media"` }``，JSON 不是数组或
    元素不是对象都会报错，进而回滚整个事务；本函数保持同样的严格性。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"decode message segments: {exc}") from exc
    if not isinstance(parsed, list):
        raise InvalidError("decode message segments: not a JSON array")
    media_ids: list[str] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise InvalidError("decode message segments: element is not a JSON object")
        media_id = item.get("media")
        if isinstance(media_id, str) and media_id:
            media_ids.append(media_id)
    return media_ids


def _scan_message(row) -> Message:
    """消息行 → :class:`Message`（Go ``scanMessage``）。"""
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        parent_message_id=text_of(row["parent_message_id"]),
        reply_to_platform_message_id=text_of(row["reply_to_platform_message_id"]),
        reply_to_message_id=text_of(row["reply_to_message_id"]),
        tool_call_id=text_of(row["tool_call_id"]),
        segments=text_of(row["segments"]),
        metadata=text_of(row["metadata"]),
        created_at=parse_time(row["created_at"]),
    )
