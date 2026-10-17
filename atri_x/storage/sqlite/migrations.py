"""SQLite schema migration。对应 Go：``internal/storage/sqlite/migrations.go``。

逐版本（v1..v17）顺序应用，``schema_migrations`` 记录已应用版本，因此可重复检测（幂等）。
**每个 migration 是一个独立事务**：失败时该版本的 DDL 全部回滚、且不会写入
``schema_migrations``，异常里带上是哪个版本（:class:`MigrationError`）。

DDL 文本与 Go 逐字一致 —— 包括 v13..v16 的 20 个触发器与 ``media_fork_history`` 视图。
触发器不是装饰：``release_*`` 负责 owner 删除时释放媒体引用，``media_*`` 负责
孤儿时间戳、删除中守卫、以及从 ``messages.segments`` / ``cron_jobs.delivery_state`` /
``atrinis_report_deliveries.output`` / ``media_history`` / ``media_outputs`` 这些 JSON
或关联行同步 ``media_references``。漏建会**静默丢引用**（媒体被提前清理），
所以这里不做任何"简化"。

Go 的 ``runMigrations`` 把整个脚本交给驱动的多语句 ``Exec``；本仓用
:func:`split_statements`（基于标准库 :func:`sqlite3.complete_statement`，能正确识别
``CREATE TRIGGER ... BEGIN ... END;`` 里的分号）逐条执行 —— 等价，且报错能定位到具体语句。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from atri_x.errors import AtriXError
from atri_x.storage.times import format_time, now

if TYPE_CHECKING:  # 只为注解；运行期不做 import，避免与 store.py 成环
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = [
    "MIGRATIONS",
    "SCHEMA_MIGRATIONS_DDL",
    "Migration",
    "MigrationError",
    "applied_versions",
    "apply_migration",
    "migrate",
    "pending_versions",
    "split_statements",
]


class MigrationError(AtriXError):
    """migration 失败。对应 Go 里被 ``fmt.Errorf("apply migration %d %s: %w", ...)`` 包装的错误。

    契约缺口记录：``atri_x/errors.py`` 里没有对应的迁移异常（它由别的模块负责，本次不改），
    因此在本模块定义，基类取全仓的 :class:`atri_x.errors.AtriXError`。
    ``version`` / ``name`` 直接带上出错的迁移版本，便于启动日志定位。
    """

    def __init__(self, message: str, version: int | None = None, name: str = "") -> None:
        super().__init__(message)
        self.version = version
        self.name = name


@dataclass(frozen=True)
class Migration:
    """一条 migration。对应 Go ``migration`` struct。"""

    version: int
    name: str
    sql: str


# Go 的 runMigrations 里内联的建表语句。
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


def split_statements(script: str) -> list[str]:
    """把含多条语句的 SQL 脚本切成单条语句。

    ``sqlite3.complete_statement`` 走 SQLite 自己的分词器，因此
    ``CREATE TRIGGER ... BEGIN ...; ...; END;`` 里的分号不会把它切碎。
    """
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement and not _is_comment_only(statement):
                statements.append(statement)
            pending = ""
    tail = pending.strip()
    if tail and not _is_comment_only(tail):
        statements.append(tail)
    return statements


def _is_comment_only(statement: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in statement.splitlines())


def applied_versions(database: "SQLiteDatabase") -> list[int]:
    """已应用的迁移版本（升序）。``schema_migrations`` 尚不存在时返回空列表。"""
    exists = database.query_row(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    )
    if exists is None:
        return []
    rows = database.query("SELECT version FROM schema_migrations ORDER BY version")
    return [int(row["version"]) for row in rows]


def pending_versions(database: "SQLiteDatabase") -> list[int]:
    """尚未应用的迁移版本（升序）。"""
    applied = set(applied_versions(database))
    return [migration.version for migration in MIGRATIONS if migration.version not in applied]


def apply_migration(database: "SQLiteDatabase", migration: Migration) -> None:
    """在**一个事务**里应用单条迁移并记账。

    对应 Go ``applyMigration``（Go 里未导出，测试同包直接调用；本仓同样把它作为公开 helper，
    ``tests/test_sqlite_migrations.py`` 用它复刻 Go 的 v11 backfill 用例）。
    """
    try:
        with database.with_transaction() as tx:
            for statement in split_statements(migration.sql):
                tx.execute(statement)
            tx.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, format_time(now())),
            )
    except (AtriXError, sqlite3.Error) as exc:
        raise MigrationError(
            f"apply migration {migration.version} {migration.name}: {exc}",
            version=migration.version,
            name=migration.name,
        ) from exc


def migrate(database: "SQLiteDatabase") -> list[int]:
    """建 ``schema_migrations`` 并按版本顺序补齐未应用的迁移。

    返回本次实际应用的版本列表（空列表表示已经是最新）。对应 Go ``runMigrations``。
    """
    database.execute(SCHEMA_MIGRATIONS_DDL)
    applied = set(applied_versions(database))
    done: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        apply_migration(database, migration)
        done.append(migration.version)
    return done


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="create_sessions_messages",
        sql="""
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT NULL,
    fork_from_message_id TEXT NULL,
    owner_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_scope_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    title TEXT NULL,
    status TEXT NOT NULL,
    metadata TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NULL,
    pinned_at TEXT NULL,
    FOREIGN KEY(parent_session_id) REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE INDEX idx_sessions_scope_updated_at
ON sessions(owner_id, platform, platform_scope_id, updated_at);

CREATE INDEX idx_sessions_parent
ON sessions(parent_session_id);

CREATE INDEX idx_sessions_fork_from
ON sessions(fork_from_message_id);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    parent_message_id TEXT NULL,
    reply_to_platform_message_id TEXT NULL,
    reply_to_message_id TEXT NULL,
    tool_call_id TEXT NULL,
    metadata TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_message_id) REFERENCES messages(id) ON DELETE SET NULL,
    FOREIGN KEY(reply_to_message_id) REFERENCES messages(id) ON DELETE SET NULL
);

CREATE INDEX idx_messages_session_created_at
ON messages(session_id, created_at);

CREATE TABLE platform_message_map (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_scope_id TEXT NOT NULL,
    platform_message_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE(platform, platform_scope_id, platform_message_id)
);
""",
    ),
    Migration(
        version=2,
        name="create_context_summaries",
        sql="""
CREATE TABLE context_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    from_message_id TEXT NULL,
    to_message_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source_tokens INTEGER NOT NULL,
    summary_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    cache_hit_tokens INTEGER NOT NULL,
    trigger_reason TEXT NOT NULL,
    metadata TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(from_message_id) REFERENCES messages(id) ON DELETE SET NULL,
    FOREIGN KEY(to_message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE INDEX idx_context_summaries_session_created_at
ON context_summaries(session_id, created_at);

CREATE INDEX idx_context_summaries_to_message
ON context_summaries(to_message_id);
""",
    ),
    Migration(
        version=3,
        name="add_fork_session_indexes",
        sql="""
CREATE INDEX IF NOT EXISTS idx_sessions_parent
ON sessions(parent_session_id);

CREATE INDEX IF NOT EXISTS idx_sessions_fork_from
ON sessions(fork_from_message_id);
""",
    ),
    Migration(
        version=4,
        name="create_tool_call_records",
        sql="""
CREATE TABLE tool_call_records (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    success INTEGER NOT NULL,
    error TEXT NULL,
    result_preview TEXT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_tool_call_records_session_tool
ON tool_call_records(session_id, tool_name);

CREATE INDEX idx_tool_call_records_session_created_at
ON tool_call_records(session_id, created_at);
""",
    ),
    Migration(
        version=5,
        name="create_cron_jobs",
        sql="""
CREATE TABLE cron_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    handler TEXT NOT NULL,
    schedule TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    metadata TEXT NULL,
    last_run_at TEXT NULL,
    next_run_at TEXT NULL,
    run_count INTEGER NOT NULL,
    last_error TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_cron_jobs_enabled_next_run
ON cron_jobs(enabled, next_run_at);
""",
    ),
    Migration(
        version=6,
        name="rename_qq_platform_to_qqonebot",
        sql="""
UPDATE sessions
SET platform = 'qqonebot'
WHERE platform = 'qq';

UPDATE sessions
SET owner_id = 'qqonebot:' || substr(owner_id, 4)
WHERE owner_id LIKE 'qq:%';

UPDATE platform_message_map
SET platform = 'qqonebot'
WHERE platform = 'qq';
""",
    ),
    Migration(
        version=7,
        name="create_atrinis_events",
        sql="""
CREATE TABLE atrinis_events (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    token_name TEXT NOT NULL,
    atriwisp_name TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tags TEXT NULL,
    mode TEXT NOT NULL,
    model_slot TEXT NULL,
    content_hash TEXT NOT NULL,
    requested_targets TEXT NULL,
    resolved_targets TEXT NULL,
    status TEXT NOT NULL,
    session_id TEXT NULL,
    result TEXT NULL,
    error TEXT NULL,
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(atriwisp_name, source, source_id)
);

CREATE INDEX idx_atrinis_events_status_updated_at
ON atrinis_events(status, updated_at);

CREATE INDEX idx_atrinis_events_atriwisp_received_at
ON atrinis_events(atriwisp_name, received_at);
""",
    ),
    Migration(
        version=8,
        name="add_atrinis_tool_declarations",
        sql="""
ALTER TABLE atrinis_events ADD COLUMN tool_declarations TEXT NULL;
ALTER TABLE atrinis_events ADD COLUMN tool_hash TEXT NULL;
""",
    ),
    Migration(
        version=9,
        name="create_atrinis_report_deliveries",
        sql="""
CREATE TABLE atrinis_report_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    target TEXT NOT NULL,
    output TEXT NOT NULL,
    message_id TEXT NULL,
    status TEXT NOT NULL,
    receipt TEXT NULL,
    error TEXT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES atrinis_events(id) ON DELETE CASCADE,
    UNIQUE(event_id, ordinal)
);

CREATE INDEX idx_atrinis_report_deliveries_event_status_ordinal
ON atrinis_report_deliveries(event_id, status, ordinal);
""",
    ),
    Migration(
        version=10,
        name="add_cron_delivery_state",
        sql="""
ALTER TABLE cron_jobs ADD COLUMN delivery_state TEXT NULL;
ALTER TABLE cron_jobs ADD COLUMN delivery_token TEXT NULL;
""",
    ),
    Migration(
        version=11,
        name="add_message_segments",
        sql="""
ALTER TABLE messages ADD COLUMN segments TEXT NULL;

UPDATE messages
SET segments = json_extract(metadata, '$.segments'),
    metadata = NULLIF(json_remove(metadata, '$.segments'), '{}')
WHERE role = 'user'
  AND metadata IS NOT NULL
  AND json_valid(metadata)
  AND json_type(CASE WHEN json_valid(metadata) THEN metadata END, '$.segments') = 'array';
""",
    ),
    Migration(
        version=12,
        name="create_media_and_references",
        sql="""
CREATE TABLE media (
    id TEXT PRIMARY KEY,
    name TEXT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    local_path TEXT NULL,
    backend TEXT NOT NULL,
    object_key TEXT NULL,
    source_platform TEXT NULL,
    source_url TEXT NULL,
    source_file_id TEXT NULL,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    expires_at TEXT NULL
);

CREATE TABLE media_references (
    media_id TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    session_id TEXT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(media_id, owner_type, owner_id, purpose),
    FOREIGN KEY(media_id) REFERENCES media(id) ON DELETE CASCADE
);

CREATE INDEX idx_media_references_owner ON media_references(owner_type, owner_id);
CREATE INDEX idx_media_references_session ON media_references(session_id);
""",
    ),
    Migration(
        version=13,
        name="release_message_media_references",
        sql="""
CREATE TRIGGER release_message_media_references AFTER DELETE ON messages BEGIN
    DELETE FROM media_references WHERE owner_id = OLD.id AND owner_type IN ('message', 'tool_result');
END;
CREATE TRIGGER release_session_media_references AFTER DELETE ON sessions BEGIN
    DELETE FROM media_references WHERE session_id = OLD.id;
END;
""",
    ),
    Migration(
        version=14,
        name="media_lifecycle",
        sql="""
ALTER TABLE media ADD COLUMN orphaned_at TEXT NULL;
ALTER TABLE media ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0;
UPDATE media SET orphaned_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE NOT EXISTS (SELECT 1 FROM media_references WHERE media_id=media.id);
CREATE TRIGGER media_initial_orphan AFTER INSERT ON media BEGIN
 UPDATE media SET orphaned_at=NEW.created_at WHERE id=NEW.id;
END;
CREATE TRIGGER media_reference_guard BEFORE INSERT ON media_references BEGIN
 SELECT RAISE(ABORT,'media is being deleted') WHERE EXISTS(SELECT 1 FROM media WHERE id=NEW.media_id AND deleting=1);
END;
CREATE TRIGGER media_reference_added AFTER INSERT ON media_references BEGIN
 UPDATE media SET orphaned_at=NULL WHERE id=NEW.media_id;
END;
CREATE TRIGGER media_reference_removed AFTER DELETE ON media_references BEGIN
 UPDATE media SET orphaned_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=OLD.media_id
 AND NOT EXISTS(SELECT 1 FROM media_references WHERE media_id=OLD.media_id);
END;
CREATE TRIGGER media_message_updated AFTER UPDATE OF segments ON messages BEGIN
 DELETE FROM media_references WHERE owner_type IN ('message','tool_result') AND owner_id=NEW.id;
 INSERT OR IGNORE INTO media_references(media_id,owner_type,owner_id,purpose,session_id,created_at)
 SELECT json_extract(value,'$.media'),CASE WHEN NEW.role='tool' THEN 'tool_result' ELSE 'message' END,NEW.id,'content',NEW.session_id,NEW.created_at
 FROM json_each(COALESCE(NEW.segments,'[]')) WHERE COALESCE(json_extract(value,'$.media'),'')<>'';
END;
CREATE VIEW media_fork_history AS
 SELECT DISTINCT s.id AS session_id,r.media_id FROM sessions s
 JOIN messages checkpoint ON checkpoint.id=s.fork_from_message_id AND checkpoint.session_id=s.parent_session_id
 JOIN media_references r ON r.session_id=s.parent_session_id
 LEFT JOIN messages m ON m.id=r.owner_id AND m.session_id=s.parent_session_id
 WHERE (r.owner_type='session_fork' AND r.owner_id=s.parent_session_id)
 OR (r.owner_type=CASE WHEN m.role='tool' THEN 'tool_result' ELSE 'message' END
 AND (m.created_at<checkpoint.created_at OR (m.created_at=checkpoint.created_at AND m.rowid<=checkpoint.rowid)));
CREATE TRIGGER media_fork_created AFTER INSERT ON sessions WHEN NEW.parent_session_id IS NOT NULL BEGIN
 INSERT OR IGNORE INTO media_references(media_id,owner_type,owner_id,purpose,session_id,created_at)
 SELECT media_id,'session_fork',NEW.id,'history',NEW.id,NEW.created_at FROM media_fork_history WHERE session_id=NEW.id;
END;
CREATE TRIGGER media_cron_insert AFTER INSERT ON cron_jobs BEGIN
 INSERT OR IGNORE INTO media_references(media_id,owner_type,owner_id,purpose,created_at)
 SELECT json_extract(value,'$.media'),'cron',NEW.id,'report',strftime('%Y-%m-%dT%H:%M:%fZ','now')
 FROM json_each(CASE WHEN json_valid(NEW.delivery_state) THEN NEW.delivery_state ELSE '{}' END,'$.report_segments') WHERE COALESCE(json_extract(value,'$.media'),'')<>'';
END;
CREATE TRIGGER media_cron_update AFTER UPDATE OF delivery_state ON cron_jobs BEGIN
 DELETE FROM media_references WHERE owner_type='cron' AND owner_id=NEW.id;
 INSERT OR IGNORE INTO media_references(media_id,owner_type,owner_id,purpose,created_at)
 SELECT json_extract(value,'$.media'),'cron',NEW.id,'report',strftime('%Y-%m-%dT%H:%M:%fZ','now')
 FROM json_each(CASE WHEN json_valid(NEW.delivery_state) THEN NEW.delivery_state ELSE '{}' END,'$.report_segments') WHERE COALESCE(json_extract(value,'$.media'),'')<>'';
END;
CREATE TRIGGER media_cron_delete AFTER DELETE ON cron_jobs BEGIN
 DELETE FROM media_references WHERE owner_type='cron' AND owner_id=OLD.id;
END;
CREATE TABLE media_outputs (
 platform TEXT NOT NULL, scope_id TEXT NOT NULL, message_id TEXT NOT NULL,
 media_id TEXT NOT NULL REFERENCES media(id), owner_id TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 PRIMARY KEY(platform,scope_id,message_id,media_id)
);
CREATE INDEX idx_media_outputs_expiry ON media_outputs(expires_at);
CREATE TRIGGER media_output_added AFTER INSERT ON media_outputs BEGIN
 INSERT INTO media_references(media_id,owner_type,owner_id,purpose,created_at)
 VALUES(NEW.media_id,'output',NEW.owner_id,'cache',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER media_output_removed AFTER DELETE ON media_outputs BEGIN
 DELETE FROM media_references WHERE media_id=OLD.media_id AND owner_type='output' AND owner_id=OLD.owner_id AND purpose='cache';
END;
CREATE TRIGGER media_report_added AFTER INSERT ON atrinis_report_deliveries
 WHEN COALESCE(json_extract(NEW.output,'$.Source.media'),'')<>'' BEGIN
 INSERT INTO media_references(media_id,owner_type,owner_id,purpose,created_at)
 VALUES(json_extract(NEW.output,'$.Source.media'),'atrinis_report',NEW.id,'delivery',NEW.created_at);
END;
CREATE TRIGGER media_report_deleted AFTER DELETE ON atrinis_report_deliveries BEGIN
 DELETE FROM media_references WHERE owner_type='atrinis_report' AND owner_id=OLD.id;
END;
CREATE TRIGGER media_report_completed AFTER UPDATE OF status ON atrinis_events WHEN NEW.status='completed' BEGIN
 DELETE FROM media_references WHERE owner_type='atrinis_report' AND owner_id IN(SELECT id FROM atrinis_report_deliveries WHERE event_id=NEW.id);
END;
CREATE TRIGGER media_event_finished AFTER UPDATE OF status ON atrinis_events WHEN NEW.status IN ('completed','failed','result_ready') BEGIN
 DELETE FROM media_references WHERE owner_type='atrinis_event' AND owner_id=NEW.id;
END;
CREATE TRIGGER media_event_deleted AFTER DELETE ON atrinis_events BEGIN
 DELETE FROM media_references WHERE owner_type='atrinis_event' AND owner_id=OLD.id;
END;
""",
    ),
    Migration(
        version=15,
        name="ordered_media_outputs",
        sql="""
DELETE FROM media_references WHERE owner_type='output';
DROP TRIGGER media_output_added;
DROP TRIGGER media_output_removed;
DROP TABLE media_outputs;
CREATE TABLE media_outputs (
 platform TEXT NOT NULL, scope_id TEXT NOT NULL, message_id TEXT NOT NULL,
 segment_index INTEGER NOT NULL, kind TEXT NOT NULL,
 media_id TEXT NOT NULL REFERENCES media(id), owner_id TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 PRIMARY KEY(platform,scope_id,message_id,segment_index)
);
CREATE INDEX idx_media_outputs_expiry ON media_outputs(expires_at);
CREATE TRIGGER media_output_added AFTER INSERT ON media_outputs BEGIN
 INSERT INTO media_references(media_id,owner_type,owner_id,purpose,created_at)
 VALUES(NEW.media_id,'output',NEW.owner_id,'cache',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER media_output_removed AFTER DELETE ON media_outputs BEGIN
 DELETE FROM media_references WHERE media_id=OLD.media_id AND owner_type='output' AND owner_id=OLD.owner_id AND purpose='cache';
END;
""",
    ),
    Migration(
        version=16,
        name="chat_history_media_references",
        sql="""
CREATE TABLE media_history (
 history_id TEXT NOT NULL, platform TEXT NOT NULL, scope_id TEXT NOT NULL, message_id TEXT NOT NULL,
 media_index INTEGER NOT NULL CHECK(media_index>0), kind TEXT NOT NULL CHECK(kind IN ('image','file','record')),
 media_id TEXT NOT NULL REFERENCES media(id), owner_id TEXT NOT NULL UNIQUE,
 PRIMARY KEY(platform,scope_id,message_id,media_index)
);
CREATE TRIGGER media_history_added AFTER INSERT ON media_history BEGIN
 INSERT INTO media_references(media_id,owner_type,owner_id,purpose,created_at)
 VALUES(NEW.media_id,'chat_history',NEW.owner_id,'content',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER media_history_removed AFTER DELETE ON media_history BEGIN
 DELETE FROM media_references WHERE media_id=OLD.media_id AND owner_type='chat_history' AND owner_id=OLD.owner_id AND purpose='content';
END;
""",
    ),
    Migration(
        version=17,
        name="session_tool_media_references",
        sql="""
DROP VIEW media_fork_history;
CREATE VIEW media_fork_history AS
 SELECT DISTINCT s.id AS session_id,r.media_id FROM sessions s
 JOIN messages checkpoint ON checkpoint.id=s.fork_from_message_id AND checkpoint.session_id=s.parent_session_id
 JOIN media_references r ON r.session_id=s.parent_session_id
 LEFT JOIN messages m ON m.id=r.owner_id AND m.session_id=s.parent_session_id
 WHERE (r.owner_type='session_fork' AND r.owner_id=s.parent_session_id)
 OR (r.owner_type='session_tool' AND r.owner_id=s.parent_session_id AND r.created_at<=checkpoint.created_at)
 OR (r.owner_type=CASE WHEN m.role='tool' THEN 'tool_result' ELSE 'message' END
 AND (m.created_at<checkpoint.created_at OR (m.created_at=checkpoint.created_at AND m.rowid<=checkpoint.rowid)));
""",
    ),
)
