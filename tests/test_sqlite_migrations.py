"""``atri_x/storage/sqlite/migrations.py`` 的 unittest。

全部用**真实 SQLite 文件**：不打内存库，因为要验的正是"落到磁盘的 schema 长什么样"
（表/索引/触发器/视图）与 migration 的幂等性、批次原子性。
临时目录用仓库根下的 ``tmp_sqlite_test/``（``.gitignore`` 已忽略）——
不要用 ``tempfile.TemporaryDirectory()``：本机沙箱下它的 0o700 ACL 会让后续写入被拒。
"""

from __future__ import annotations

import os
import shutil
import unittest

from atri_x.errors import InvalidError
from atri_x.storage.sqlite import (
    MIGRATIONS,
    MigrationError,
    SQLiteDatabase,
    applied_versions,
    apply_migration,
    migrate,
    new,
    pending_versions,
)
from atri_x.storage.sqlite.migrations import SCHEMA_MIGRATIONS_DDL, split_statements

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_ROOT = os.path.join(_REPO_ROOT, "tmp_sqlite_test")

# 从 sqlite_master 实测导出（见本次移植报告的清单）。
EXPECTED_TABLES = {
    "schema_migrations",
    "sessions",
    "messages",
    "platform_message_map",
    "context_summaries",
    "tool_call_records",
    "cron_jobs",
    "atrinis_events",
    "atrinis_report_deliveries",
    "media",
    "media_references",
    "media_outputs",
    "media_history",
}
EXPECTED_INDEXES = {
    "idx_sessions_scope_updated_at",
    "idx_sessions_parent",
    "idx_sessions_fork_from",
    "idx_messages_session_created_at",
    "idx_context_summaries_session_created_at",
    "idx_context_summaries_to_message",
    "idx_tool_call_records_session_tool",
    "idx_tool_call_records_session_created_at",
    "idx_cron_jobs_enabled_next_run",
    "idx_atrinis_events_status_updated_at",
    "idx_atrinis_events_atriwisp_received_at",
    "idx_atrinis_report_deliveries_event_status_ordinal",
    "idx_media_references_owner",
    "idx_media_references_session",
    "idx_media_outputs_expiry",
}
EXPECTED_TRIGGERS = {
    "release_message_media_references",
    "release_session_media_references",
    "media_initial_orphan",
    "media_reference_guard",
    "media_reference_added",
    "media_reference_removed",
    "media_message_updated",
    "media_fork_created",
    "media_cron_insert",
    "media_cron_update",
    "media_cron_delete",
    "media_output_added",
    "media_output_removed",
    "media_report_added",
    "media_report_deleted",
    "media_report_completed",
    "media_event_finished",
    "media_event_deleted",
    "media_history_added",
    "media_history_removed",
}
EXPECTED_VIEWS = {"media_fork_history"}


def schema_objects(database: SQLiteDatabase) -> tuple[set[str], set[str], set[str], set[str]]:
    """按类型取 ``sqlite_master`` 里的对象名（过滤 SQLite 自动索引）。"""
    tables: set[str] = set()
    indexes: set[str] = set()
    triggers: set[str] = set()
    views: set[str] = set()
    for row in database.query("SELECT type, name FROM sqlite_master"):
        name = row["name"]
        if name.startswith("sqlite_"):
            continue
        bucket = {"table": tables, "index": indexes, "trigger": triggers, "view": views}.get(row["type"])
        if bucket is not None:
            bucket.add(name)
    return tables, indexes, triggers, views


class SqliteMigrationTest(unittest.TestCase):
    """真实文件库上的 migration 行为。"""

    def setUp(self) -> None:
        self.directory = os.path.join(_TMP_ROOT, "migrations", self._testMethodName)
        shutil.rmtree(self.directory, ignore_errors=True)
        os.makedirs(self.directory, exist_ok=True)
        self.path = os.path.join(self.directory, "atrix_sessions.db")

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def _open(self, name: str = "atrix_sessions.db") -> SQLiteDatabase:
        return SQLiteDatabase(os.path.join(self.directory, name))

    # -- 全新库 -------------------------------------------------------------

    def test_fresh_database_creates_every_table_index_trigger_and_view(self) -> None:
        store = new(self.path)
        try:
            tables, indexes, triggers, views = schema_objects(store.database)
        finally:
            store.close()
        self.assertEqual(EXPECTED_TABLES, tables)
        self.assertEqual(EXPECTED_INDEXES, indexes)
        self.assertEqual(EXPECTED_TRIGGERS, triggers)
        self.assertEqual(EXPECTED_VIEWS, views)

    def test_applied_versions_covers_v1_to_v17(self) -> None:
        store = new(self.path)
        try:
            versions = applied_versions(store.database)
            self.assertEqual([migration.version for migration in MIGRATIONS], versions)
            self.assertEqual(list(range(1, 18)), versions)
            self.assertEqual([], pending_versions(store.database))
            rows = store.database.query("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
            self.assertEqual([migration.name for migration in MIGRATIONS], [row["name"] for row in rows])
            for row in rows:
                self.assertTrue(row["applied_at"].endswith("Z"), row["applied_at"])
        finally:
            store.close()

    def test_pending_versions_on_raw_database_without_migration_table(self) -> None:
        database = self._open("raw.db")
        try:
            self.assertEqual([], applied_versions(database))
            self.assertEqual(list(range(1, 18)), pending_versions(database))
        finally:
            database.close()

    def test_migrate_is_idempotent_and_reopen_keeps_schema(self) -> None:
        store = new(self.path)
        try:
            before = schema_objects(store.database)
            self.assertEqual([], migrate(store.database))
            self.assertEqual(before, schema_objects(store.database))
            self.assertEqual(list(range(1, 18)), applied_versions(store.database))
        finally:
            store.close()

        again = new(self.path)
        try:
            self.assertEqual(list(range(1, 18)), applied_versions(again.database))
            self.assertEqual(before, schema_objects(again.database))
            self.assertEqual(
                list(range(1, 18)),
                [int(row["version"]) for row in again.database.query("SELECT version FROM schema_migrations")],
            )
        finally:
            again.close()

    # -- 失败路径 -----------------------------------------------------------

    def test_failed_migration_reports_version_and_rolls_back_whole_batch(self) -> None:
        database = self._open()
        try:
            # v1 的第四条语句是 CREATE TABLE messages；先占住这个名字让它失败。
            database.execute("CREATE TABLE messages (placeholder TEXT)")
            with self.assertRaises(MigrationError) as caught:
                migrate(database)
            self.assertEqual(1, caught.exception.version)
            self.assertEqual("create_sessions_messages", caught.exception.name)
            self.assertIn("apply migration 1 create_sessions_messages", str(caught.exception))

            # 同一个 DDL 批次里先建出来的 sessions 也必须回滚。
            tables, _, _, _ = schema_objects(database)
            self.assertNotIn("sessions", tables)
            self.assertIn("messages", tables)  # 这是我们自己占位建的那张
            self.assertEqual([], applied_versions(database))
            self.assertEqual(list(range(1, 18)), pending_versions(database))
        finally:
            database.close()

    def test_failed_migration_keeps_earlier_versions_applied(self) -> None:
        database = self._open()
        try:
            database.execute("CREATE TABLE context_summaries (placeholder TEXT)")
            with self.assertRaises(MigrationError) as caught:
                migrate(database)
            self.assertEqual(2, caught.exception.version)
            self.assertEqual("create_context_summaries", caught.exception.name)
            self.assertEqual([1], applied_versions(database))
            self.assertEqual(list(range(2, 18)), pending_versions(database))
            tables, _, _, _ = schema_objects(database)
            self.assertIn("sessions", tables)
            self.assertIn("messages", tables)
            self.assertIn("context_summaries", tables)  # 仍是占位表，没被半途改建
            self.assertNotIn("idx_context_summaries_session_created_at", schema_objects(database)[1])
        finally:
            database.close()

    def test_failed_migration_is_resumable_after_fixing_the_cause(self) -> None:
        database = self._open()
        try:
            database.execute("CREATE TABLE context_summaries (placeholder TEXT)")
            with self.assertRaises(MigrationError):
                migrate(database)
            database.execute("DROP TABLE context_summaries")
            self.assertEqual(list(range(2, 18)), migrate(database))
            self.assertEqual(list(range(1, 18)), applied_versions(database))
            self.assertEqual([], pending_versions(database))
        finally:
            database.close()

    # -- 数据迁移 -----------------------------------------------------------

    def test_v6_renames_qq_platform_and_owner_prefix(self) -> None:
        database = self._open()
        try:
            database.execute(SCHEMA_MIGRATIONS_DDL)
            for migration in MIGRATIONS[:5]:
                apply_migration(database, migration)
            database.execute(
                """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('s1', 'qq:1234', 'qq', 'group:1', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
            )
            database.execute(
                """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('s2', 'qqofficial:9', 'qqofficial', 'group:2', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
            )
            database.execute(
                """
INSERT INTO messages (id, session_id, role, content, created_at)
VALUES ('m1', 's1', 'user', 'hi', '2026-01-01T00:00:00Z')"""
            )
            database.execute(
                """
INSERT INTO platform_message_map (id, platform, platform_scope_id, platform_message_id, message_id, session_id, created_at)
VALUES ('p1', 'qq', 'group:1', 'pm1', 'm1', 's1', '2026-01-01T00:00:00Z')"""
            )

            apply_migration(database, MIGRATIONS[5])

            self.assertEqual("qqonebot", database.query_row("SELECT platform FROM sessions WHERE id='s1'")["platform"])
            self.assertEqual("qqonebot:1234", database.query_row("SELECT owner_id FROM sessions WHERE id='s1'")["owner_id"])
            self.assertEqual("qqofficial", database.query_row("SELECT platform FROM sessions WHERE id='s2'")["platform"])
            self.assertEqual("qqofficial:9", database.query_row("SELECT owner_id FROM sessions WHERE id='s2'")["owner_id"])
            self.assertEqual(
                "qqonebot",
                database.query_row("SELECT platform FROM platform_message_map WHERE id='p1'")["platform"],
            )
        finally:
            database.close()

    def test_v11_moves_user_segments_out_of_metadata(self) -> None:
        database = self._open()
        try:
            database.execute(SCHEMA_MIGRATIONS_DDL)
            for migration in MIGRATIONS[:10]:
                apply_migration(database, migration)
            database.execute(
                """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('s1', 'u1', 'cli', 'local', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
            )
            legacy = '{"segments":[{"type":"text","text":"看图"},{"type":"image","url":"https://example.com/a.png"}],"custom":true}'
            database.execute(
                """
INSERT INTO messages (id, session_id, role, content, metadata, created_at)
VALUES ('m1', 's1', 'user', '看图', ?, '2026-01-01T00:00:00Z')""",
                (legacy,),
            )
            # 助手消息的 metadata 里即使有 segments 也不动它（Go 限定 role='user'）。
            database.execute(
                """
INSERT INTO messages (id, session_id, role, content, metadata, created_at)
VALUES ('m2', 's1', 'assistant', 'ok', ?, '2026-01-01T00:00:01Z')""",
                (legacy,),
            )

            apply_migration(database, MIGRATIONS[10])

            migrated = database.query_row("SELECT segments, metadata FROM messages WHERE id='m1'")
            self.assertIn('"type":"image"', migrated["segments"])
            self.assertEqual('{"custom":true}', migrated["metadata"])
            untouched = database.query_row("SELECT segments, metadata FROM messages WHERE id='m2'")
            self.assertIsNone(untouched["segments"])
            self.assertEqual(legacy, untouched["metadata"])
        finally:
            database.close()

    # -- helper -------------------------------------------------------------

    def test_split_statements_keeps_trigger_bodies_intact(self) -> None:
        statements = split_statements(MIGRATIONS[12].sql)  # v13：两条 release 触发器
        self.assertEqual(2, len(statements))
        for statement in statements:
            self.assertTrue(statement.startswith("CREATE TRIGGER"))
            self.assertTrue(statement.rstrip().endswith("END;"))
        # 朴素按 ';' 切会把触发器体切开 —— 这里必须不是那样。
        self.assertGreater(len(statements), 0)
        self.assertLess(len(statements), MIGRATIONS[12].sql.count(";"))

    def test_migrate_rejects_empty_path(self) -> None:
        with self.assertRaises(InvalidError):
            new("")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
