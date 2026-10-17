"""入站聊天历史与出站消息仓储。对应 Go：``internal/storage/sqlite/chat_history_repository.go``。

与主库**分开**的一个 SQLite 文件（Go 的 ``NewChatHistory``），表也建在这里而不是
``migrations.go``：``chat_messages``（入站，按 ``(platform, session… scope, message_id)``
去重覆盖）与 ``outbound_messages``（真实发出去的助手消息，供学习/分析重建"用户 → 助手"配对）。

历史查询一律带 ``(text != '' OR COALESCE(segments,'') != '')`` 条件：只有图片没有正文的消息
仍要能被检索到，而完全空的行不进历史窗口。

范围读取（``list_range``）与计数（``count_range``）是 Go 里的**可选能力**接口
（``ChatHistoryRangeRepository`` / ``OutboundMessageCounter``），消费方按鸭子类型判定；
本实现总是提供。
"""

from __future__ import annotations

from datetime import datetime

from atri_x.errors import ErrNotFound, InvalidError
from atri_x.storage.ids import new_id
from atri_x.storage.models import (
    ChatHistoryAroundRequest,
    ChatHistoryRangeRequest,
    ChatHistorySearchRequest,
    ChatMessage,
    OutboundMessage,
    OutboundMessageRangeRequest,
)
from atri_x.storage.sqlite.migrations import split_statements
from atri_x.storage.sqlite.store import (
    SQLiteDatabase,
    SQLiteError,
    null_string,
    open_database,
    text_of,
)
from atri_x.storage.times import format_time, is_zero_time, now, parse_time

__all__ = ["ChatHistoryRepository", "ChatHistoryStore", "OutboundMessageRepository", "new_chat_history"]

_MAX_RANGE_LIMIT = 5000

CHAT_MESSAGE_COLUMNS = """seq, id, platform, platform_scope_id, scope_type, platform_message_id,
       sender_id, sender_name, text, raw, segments, reply_to_platform_message_id, metadata, created_at"""

OUTBOUND_MESSAGE_COLUMNS = "seq, id, platform, platform_scope_id, platform_message_id, text, segments, created_at"

# Go 的 migrateChatHistory：建表 + 四个索引；segments 列另外 ALTER 一次（老库补列）。
CHAT_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    platform_scope_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    platform_message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NULL,
    text TEXT NOT NULL,
    raw TEXT NULL,
    segments TEXT NULL,
    reply_to_platform_message_id TEXT NULL,
    metadata TEXT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform, platform_scope_id, platform_message_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_scope_seq
ON chat_messages(platform, platform_scope_id, seq);

CREATE INDEX IF NOT EXISTS idx_chat_messages_scope_created_at
ON chat_messages(platform, platform_scope_id, created_at);

CREATE INDEX IF NOT EXISTS idx_chat_messages_sender
ON chat_messages(platform, platform_scope_id, sender_id);

CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at
ON chat_messages(created_at);
"""

OUTBOUND_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    platform_scope_id TEXT NOT NULL,
    platform_message_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    segments TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_messages_scope_seq
ON outbound_messages(platform, platform_scope_id, seq);

CREATE INDEX IF NOT EXISTS idx_outbound_messages_scope_created_at
ON outbound_messages(platform, platform_scope_id, created_at);

CREATE INDEX IF NOT EXISTS idx_outbound_messages_created_at
ON outbound_messages(created_at);
"""


class ChatHistoryStore:
    """聊天历史库门面。对应 Go ``ChatHistoryStore``（``NewChatHistory`` 的返回值）。"""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self._repository = ChatHistoryRepository(database)
        self._outbound = OutboundMessageRepository(database)

    def repository(self) -> "ChatHistoryRepository":
        return self._repository

    def outbound(self) -> "OutboundMessageRepository":
        return self._outbound

    def close(self) -> None:
        self.database.close()


def new_chat_history(
    path: str,
    *,
    journal_mode: str = "",
    synchronous: str = "",
    busy_timeout_ms: int = 0,
) -> ChatHistoryStore:
    """打开（必要时创建）聊天历史库。对应 Go ``sqlite.NewChatHistory``。"""
    if not path:
        raise InvalidError("chat history sqlite path is empty")
    database = open_database(
        path,
        journal_mode=journal_mode,
        synchronous=synchronous,
        busy_timeout_ms=busy_timeout_ms,
    )
    try:
        migrate_chat_history(database)
    except BaseException:
        database.close()
        raise
    return ChatHistoryStore(database)


def migrate_chat_history(database: SQLiteDatabase) -> None:
    """建 ``chat_messages`` / ``outbound_messages``（幂等）。对应 Go ``migrateChatHistory``。"""
    for statement in split_statements(CHAT_MESSAGES_SCHEMA):
        database.execute(statement)
    # 老库补 segments 列；已经有时 SQLite 报 "duplicate column name: segments"，忽略。
    try:
        database.execute("ALTER TABLE chat_messages ADD COLUMN segments TEXT NULL")
    except SQLiteError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
    for statement in split_statements(OUTBOUND_MESSAGES_SCHEMA):
        database.execute(statement)


class ChatHistoryRepository:
    """入站聊天历史仓储。对应 Go ``sqlite.ChatHistoryRepository``。"""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._db = database

    def append(self, message: ChatMessage) -> None:
        """写入一条历史消息（按 ``(platform, scope, platform_message_id)`` 冲突覆盖）。"""
        if not message.id:
            message.id = new_id()
        if is_zero_time(message.created_at):
            message.created_at = now()
        self._db.execute(
            """
INSERT INTO chat_messages (
    id, platform, platform_scope_id, scope_type, platform_message_id,
    sender_id, sender_name, text, raw, segments, reply_to_platform_message_id, metadata, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(platform, platform_scope_id, platform_message_id) DO UPDATE SET
    sender_id = excluded.sender_id,
    sender_name = excluded.sender_name,
    text = excluded.text,
    raw = excluded.raw,
    reply_to_platform_message_id = excluded.reply_to_platform_message_id,
    segments = excluded.segments,
    metadata = excluded.metadata""",
            (
                message.id,
                message.platform,
                message.platform_scope_id,
                message.scope_type,
                message.platform_message_id,
                message.sender_id,
                null_string(message.sender_name),
                message.text,
                null_string(message.raw),
                null_string(message.segments),
                null_string(message.reply_to_platform_message_id),
                null_string(message.metadata),
                format_time(message.created_at),
            ),
        )

    def get_by_platform_message(self, platform: str, scope_id: str, platform_message_id: str) -> ChatMessage:
        """按平台消息 ID 取历史消息；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(
            f"""
SELECT {CHAT_MESSAGE_COLUMNS}
FROM chat_messages
WHERE platform = ? AND platform_scope_id = ? AND platform_message_id = ?""",
            (platform, scope_id, platform_message_id),
        )
        if row is None:
            raise ErrNotFound
        return _scan_chat_message(row)

    def search(self, req: ChatHistorySearchRequest) -> list[ChatMessage]:
        """按关键词/发送者/时间窗检索，返回按 ``seq`` **升序**（Go 取 DESC 后再反转）。"""
        limit = req.limit if req.limit > 0 else 10
        conditions = ["platform = ?", "platform_scope_id = ?", "(text != '' OR COALESCE(segments, '') != '')"]
        params: list[object] = [req.platform, req.platform_scope_id]
        if req.query_terms:
            operator = "AND" if req.query_mode.strip().lower() == "and" else "OR"
            parts: list[str] = []
            for term in req.query_terms:
                value = (term or "").strip()
                if not value:
                    continue
                parts.append("text LIKE ?")
                params.append("%" + value + "%")
            if parts:
                conditions.append("(" + f" {operator} ".join(parts) + ")")
        if req.sender_id:
            conditions.append("sender_id = ?")
            params.append(req.sender_id)
        if req.sender_name_query:
            conditions.append("sender_name LIKE ?")
            params.append("%" + req.sender_name_query + "%")
        if req.since is not None:
            conditions.append("created_at >= ?")
            params.append(format_time(req.since))
        if req.until is not None:
            conditions.append("created_at <= ?")
            params.append(format_time(req.until))
        params.append(limit)
        rows = self._db.query(
            f"""
SELECT {CHAT_MESSAGE_COLUMNS}
FROM chat_messages
WHERE {" AND ".join(conditions)}
ORDER BY seq DESC
LIMIT ?""",
            params,
        )
        messages = [_scan_chat_message(row) for row in rows]
        messages.reverse()
        return messages

    def around(self, req: ChatHistoryAroundRequest) -> list[ChatMessage]:
        """取目标消息前后的上下文窗口（含目标本身）。目标不存在时抛 ErrNotFound。"""
        target = self.get_by_platform_message(req.platform, req.platform_scope_id, req.platform_message_id)
        previous = self._list_around_side(
            req.platform, req.platform_scope_id, target.seq, req.before, before=True
        )
        following = self._list_around_side(
            req.platform, req.platform_scope_id, target.seq, req.after, before=False
        )
        return [*previous, target, *following]

    def list_range(self, req: ChatHistoryRangeRequest) -> list[ChatMessage]:
        """按 ``seq`` 升序批量读取时间窗内的历史消息（``limit`` 上限 5000）。"""
        if not req.platform.strip() or not req.platform_scope_id.strip():
            raise InvalidError("chat history range platform and scope are required")
        limit = req.limit
        if limit <= 0 or limit > _MAX_RANGE_LIMIT:
            limit = _MAX_RANGE_LIMIT
        conditions = ["platform = ?", "platform_scope_id = ?", "(text != '' OR COALESCE(segments, '') != '')"]
        params: list[object] = [req.platform, req.platform_scope_id]
        if req.after_seq > 0:
            conditions.append("seq > ?")
            params.append(req.after_seq)
        if req.since is not None:
            conditions.append("created_at >= ?")
            params.append(format_time(req.since))
        if req.until is not None:
            conditions.append("created_at <= ?")
            params.append(format_time(req.until))
        params.append(limit)
        rows = self._db.query(
            f"""
SELECT {CHAT_MESSAGE_COLUMNS}
FROM chat_messages
WHERE {" AND ".join(conditions)}
ORDER BY seq ASC
LIMIT ?""",
            params,
        )
        return [_scan_chat_message(row) for row in rows]

    def delete_before(self, cutoff: datetime) -> int:
        """删除 ``created_at`` 早于 cutoff 的历史消息，返回删除行数。"""
        cursor = self._db.execute(
            "DELETE FROM chat_messages WHERE created_at < ?",
            (format_time(cutoff),),
        )
        return int(cursor.rowcount)

    def _list_around_side(
        self, platform: str, scope_id: str, seq: int, limit: int, before: bool
    ) -> list[ChatMessage]:
        if limit <= 0:
            return []
        operator = "<" if before else ">"
        order = "DESC" if before else "ASC"
        rows = self._db.query(
            f"""
SELECT {CHAT_MESSAGE_COLUMNS}
FROM chat_messages
WHERE platform = ? AND platform_scope_id = ? AND (text != '' OR COALESCE(segments, '') != '') AND seq {operator} ?
ORDER BY seq {order}
LIMIT ?""",
            (platform, scope_id, seq, limit),
        )
        messages = [_scan_chat_message(row) for row in rows]
        if before:
            messages.reverse()
        return messages


class OutboundMessageRepository:
    """已发送助手消息仓储。对应 Go ``sqlite.OutboundMessageRepository``。"""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._db = database

    def append(self, message: OutboundMessage) -> None:
        """写入一条已发送消息（按 ``id`` 冲突覆盖）。"""
        if not message.id:
            message.id = new_id()
        if is_zero_time(message.created_at):
            message.created_at = now()
        self._db.execute(
            """
INSERT INTO outbound_messages (
    id, platform, platform_scope_id, platform_message_id, text, segments, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    platform_message_id = excluded.platform_message_id,
    text = excluded.text,
    segments = excluded.segments,
    created_at = excluded.created_at""",
            (
                message.id,
                message.platform,
                message.platform_scope_id,
                message.platform_message_id,
                message.text,
                null_string(message.segments),
                format_time(message.created_at),
            ),
        )

    def list_range(self, req: OutboundMessageRangeRequest) -> list[OutboundMessage]:
        """按 ``seq`` 升序批量读取时间窗内的已发送消息（``limit`` 上限 5000）。"""
        conditions, params, limit = _outbound_range_query(req)
        params.append(limit)
        rows = self._db.query(
            f"""
SELECT {OUTBOUND_MESSAGE_COLUMNS}
FROM outbound_messages
WHERE {" AND ".join(conditions)}
ORDER BY seq ASC
LIMIT ?""",
            params,
        )
        messages: list[OutboundMessage] = []
        for row in rows:
            messages.append(
                OutboundMessage(
                    seq=int(row["seq"]),
                    id=row["id"],
                    platform=row["platform"],
                    platform_scope_id=row["platform_scope_id"],
                    platform_message_id=row["platform_message_id"],
                    text=row["text"],
                    segments=text_of(row["segments"]),
                    created_at=parse_time(row["created_at"]),
                )
            )
        return messages

    def count_range(self, req: OutboundMessageRangeRequest) -> int:
        """统计时间窗内的已发送消息条数（不加载整页行）。"""
        conditions, params, _ = _outbound_range_query(req)
        row = self._db.query_row(
            f"""
SELECT COUNT(*) AS count FROM outbound_messages
WHERE {" AND ".join(conditions)}""",
            params,
        )
        return int(row["count"]) if row is not None else 0

    def delete_before(self, cutoff: datetime) -> int:
        """删除 ``created_at`` 早于 cutoff 的已发送消息，返回删除行数。"""
        cursor = self._db.execute(
            "DELETE FROM outbound_messages WHERE created_at < ?",
            (format_time(cutoff),),
        )
        return int(cursor.rowcount)


def _outbound_range_query(req: OutboundMessageRangeRequest) -> tuple[list[str], list[object], int]:
    if not req.platform.strip() or not req.platform_scope_id.strip():
        raise InvalidError("outbound message range platform and scope are required")
    limit = req.limit
    if limit <= 0 or limit > _MAX_RANGE_LIMIT:
        limit = _MAX_RANGE_LIMIT
    conditions = ["platform = ?", "platform_scope_id = ?"]
    params: list[object] = [req.platform, req.platform_scope_id]
    if req.after_seq > 0:
        conditions.append("seq > ?")
        params.append(req.after_seq)
    if req.since is not None:
        conditions.append("created_at >= ?")
        params.append(format_time(req.since))
    if req.until is not None:
        conditions.append("created_at <= ?")
        params.append(format_time(req.until))
    return conditions, params, limit


def _scan_chat_message(row) -> ChatMessage:
    """历史消息行 → :class:`ChatMessage`（Go ``scanChatMessage``）。"""
    return ChatMessage(
        seq=int(row["seq"]),
        id=row["id"],
        platform=row["platform"],
        platform_scope_id=row["platform_scope_id"],
        scope_type=row["scope_type"],
        platform_message_id=row["platform_message_id"],
        sender_id=row["sender_id"],
        sender_name=text_of(row["sender_name"]),
        text=row["text"],
        raw=text_of(row["raw"]),
        segments=text_of(row["segments"]),
        reply_to_platform_message_id=text_of(row["reply_to_platform_message_id"]),
        metadata=text_of(row["metadata"]),
        created_at=parse_time(row["created_at"]),
    )
