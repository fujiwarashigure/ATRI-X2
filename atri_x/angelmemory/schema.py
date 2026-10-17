"""angelmemory 的建表、迁移与文本 helper。

对应 Go：``internal/angelmemory/store.go`` 的 ``migrate`` / ``columnExists`` /
``normalizeTags`` / ``truncateSourceText`` / ``likePrefixPattern``，以及常量
``defaultMaxContentRunes`` / ``defaultMaxPerScope`` / ``defaultRecallLimit``。

拆出本模块只为满足 ``PORTING.md`` §2 的单文件行数约束；行为与 Go 逐条一致。

**SQLite 表结构**（与 Go 的 ``migrate`` 逐列对应）::

    CREATE TABLE memories (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        content TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '',
        strength INTEGER NOT NULL DEFAULT 50,
        source TEXT NOT NULL DEFAULT '',
        source_kind TEXT NOT NULL DEFAULT '',
        source_actor_id TEXT NOT NULL DEFAULT '',
        source_message_id TEXT NOT NULL DEFAULT '',
        source_session_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_accessed_at TEXT NOT NULL
    )
    CREATE UNIQUE INDEX idx_angel_memories_scope_content ON memories(platform, scope_id, content)
    CREATE INDEX idx_angel_memories_scope_strength ON memories(platform, scope_id, strength)
    CREATE INDEX idx_angel_memories_source_message ON memories(platform, scope_id, source_message_id)
    CREATE INDEX idx_angel_memories_source_actor ON memories(platform, scope_id, source_actor_id)

四个 ``source_*`` 列是后加的，所以 ``migrate`` 先 ``CREATE TABLE IF NOT EXISTS``
再用 ``PRAGMA table_info`` 判存在后逐列 ``ALTER TABLE ADD COLUMN`` —— 旧库因此
可以原地升级（对应 Go ``TestOpenMigratesLegacyMemorySchema``）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from atri_x.angelmemory.models import Memory

__all__ = [
    "DEFAULT_MAX_CONTENT_RUNES",
    "DEFAULT_MAX_PER_SCOPE",
    "DEFAULT_RECALL_LIMIT",
    "CREATE_MEMORIES_TABLE",
    "LEGACY_TOOL_SOURCE_LABEL",
    "SELECT_MEMORY_COLUMNS",
    "column_exists",
    "like_prefix_pattern",
    "migrate",
    "normalize_tags",
    "row_to_memory",
    "source_filter_clause",
    "truncate_source_text",
]

#: 单条记忆的 rune 上限。对应 Go ``defaultMaxContentRunes``。
DEFAULT_MAX_CONTENT_RUNES = 1000
#: 单 scope 的记忆条数上限。对应 Go ``defaultMaxPerScope``。
DEFAULT_MAX_PER_SCOPE = 1000
#: 召回默认条数。对应 Go ``defaultRecallLimit``。
DEFAULT_RECALL_LIMIT = 5

#: 旧版自由文本来源标签：结构化来源出现之前的**唯一**生产写入方写的就是它。
#: 对应 Go ``legacyToolSourceLabel``。
LEGACY_TOOL_SOURCE_LABEL = "tool"

SELECT_MEMORY_COLUMNS = (
    "SELECT id, platform, scope_id, content, tags, strength, source, source_kind,"
    " source_actor_id, source_message_id, source_session_id, created_at, updated_at,"
    " last_accessed_at FROM memories"
)

#: ``SELECT_MEMORY_COLUMNS`` 的列顺序（``row_to_memory`` 的 ``row[i]`` 依赖它）。
CREATE_MEMORIES_TABLE = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    strength INTEGER NOT NULL DEFAULT 50,
    source TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_actor_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
)"""

#: 后续追加的结构化来源列（Go 的 ``migrate`` 里的 ALTER 列表）。
SOURCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_kind", "source_kind TEXT NOT NULL DEFAULT ''"),
    ("source_actor_id", "source_actor_id TEXT NOT NULL DEFAULT ''"),
    ("source_message_id", "source_message_id TEXT NOT NULL DEFAULT ''"),
    ("source_session_id", "source_session_id TEXT NOT NULL DEFAULT ''"),
)

INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_angel_memories_scope_content"
    " ON memories(platform, scope_id, content)",
    "CREATE INDEX IF NOT EXISTS idx_angel_memories_scope_strength"
    " ON memories(platform, scope_id, strength)",
    "CREATE INDEX IF NOT EXISTS idx_angel_memories_source_message"
    " ON memories(platform, scope_id, source_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_angel_memories_source_actor"
    " ON memories(platform, scope_id, source_actor_id)",
)


def migrate(connection: sqlite3.Connection) -> None:
    """建表 + 补列 + 建索引。对应 Go ``migrate``。"""
    connection.execute(CREATE_MEMORIES_TABLE)
    for name, ddl in SOURCE_COLUMNS:
        if column_exists(connection, "memories", name):
            continue
        connection.execute(f"ALTER TABLE memories ADD COLUMN {ddl}")
    for statement in INDEX_STATEMENTS:
        connection.execute(statement)


def column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    """该表是否已有该列（大小写与空白不敏感）。对应 Go ``columnExists``。"""
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    target = column.strip().lower()
    for row in rows:
        # PRAGMA table_info 的列：cid, name, type, notnull, dflt_value, pk
        name = row[1]
        if isinstance(name, str) and name.strip().lower() == target:
            return True
    return False


def row_to_memory(row: Any) -> Memory:
    """把一行 ``SELECT_MEMORY_COLUMNS`` 的结果映射成 :class:`Memory`。"""
    memory = Memory(
        id=row[0],
        platform=row[1],
        scope_id=row[2],
        content=row[3],
        tags=row[4],
        strength=int(row[5] or 0),
        source=row[6] or "",
        source_kind=row[7] or "",
        source_actor_id=row[8] or "",
        source_message_id=row[9] or "",
        source_session_id=row[10] or "",
    )
    memory.created_at = _parse(row[11])
    memory.updated_at = _parse(row[12])
    memory.last_accessed_at = _parse(row[13])
    return memory


def _parse(raw: Any):
    from atri_x.storage.times import parse_time

    try:
        return parse_time(str(raw))
    except ValueError:
        return None


def source_filter_clause(filter: Any) -> tuple[str, list[Any]]:
    """把 :class:`SourceFilter` 编译成 SQL 片段 + 参数。对应 Go ``sourceFilterClause``。

    返回 ``("", [])`` 表示过滤器为空（匹配整个 scope）；否则返回形如
    ``" AND source_actor_id = ?"`` 的片段。
    """
    parts: list[str] = []
    params: list[Any] = []
    actor_id = _trim(getattr(filter, "actor_id", "") or "")
    if actor_id != "":
        parts.append("source_actor_id = ?")
        params.append(actor_id)
    message_id = _trim(getattr(filter, "message_id", "") or "")
    if message_id != "":
        parts.append("source_message_id = ?")
        params.append(message_id)
    session_id = _trim(getattr(filter, "session_id", "") or "")
    if session_id != "":
        parts.append("source_session_id = ?")
        params.append(session_id)
    if not parts:
        return "", params
    return " AND " + " AND ".join(parts), params


def like_prefix_pattern(prefix: str) -> str:
    """转义 LIKE 通配符并包成前缀匹配；调用方必须写 ``LIKE ? ESCAPE '\\\\'``。"""
    value = _trim(prefix)
    value = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return value + "%"


def truncate_source_text(value: str, max_runes: int) -> str:
    """trim 后按 rune 截断来源字段。对应 Go ``truncateSourceText``。"""
    value = _trim(value)
    if max_runes <= 0:
        return ""
    if len(value) <= max_runes:
        return value
    return value[:max_runes]


def normalize_tags(tags: str) -> str:
    """把标签规范成去重后的逗号分隔串。对应 Go ``normalizeTags``。

    分隔符与 Go 完全一致：``','`` / ``'，'`` / 空格 / ``'\\n'`` / ``'\\t'``；
    其他空白（``\\r`` 等）不属于分隔符，只会在 trim 时被去掉。
    """
    fields: list[str] = []
    current: list[str] = []
    for ch in tags or "":
        if ch in ",， \n\t":
            fields.append("".join(current))
            current = []
            continue
        current.append(ch)
    fields.append("".join(current))
    out: list[str] = []
    seen: set[str] = set()
    for field in fields:
        field = _trim(field)
        if field == "" or field in seen:
            continue
        seen.add(field)
        out.append(field)
    return ",".join(out)


def _trim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
