"""``atri_x/storage/sqlite`` 主库门面与 session/message/summary/tool_call 仓储的 unittest。

全部跑在**真实 SQLite 文件 + 真实 migration + 真实触发器**上（不打内存库、不 mock），
临时目录用仓库根下的 ``tmp_sqlite_test/``。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import unittest
import uuid
from datetime import timedelta

from atri_x.errors import (
    ClosedError,
    InvalidError,
    NotFoundError,
)
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText
from atri_x.storage.models import (
    ContextSummary,
    ListSessionsRequest,
    Message,
    PlatformMessageMap,
    RoleAssistant,
    RoleUser,
    Session,
    SessionStatusClosed,
    ToolCallRecord,
    segments_json,
)
from atri_x.storage.repositories import (
    AtrinisEventRepository,
    ContextSummaryRepository,
    CronJobRepository,
    MediaReferenceRepository,
    MediaRepository,
    MessageRepository,
    SessionRepository,
    Store,
    ToolCallRepository,
)
from atri_x.storage.sqlite import SQLiteError, new
from atri_x.storage.times import parse_time, now

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_ROOT = os.path.join(_REPO_ROOT, "tmp_sqlite_test")


class SqliteStoreTestCase(unittest.TestCase):
    """每个测试一个干净的真实库文件。"""

    def setUp(self) -> None:
        self.directory = os.path.join(_TMP_ROOT, "store", self._testMethodName)
        shutil.rmtree(self.directory, ignore_errors=True)
        os.makedirs(self.directory, exist_ok=True)
        self.path = os.path.join(self.directory, "atrix_sessions.db")
        self.store = new(self.path)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001 - 测试已自行关库时忽略
            pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def new_session(self, **overrides) -> Session:
        session = Session(owner_id="u1", platform="cli", platform_scope_id="local", title="test")
        for key, value in overrides.items():
            setattr(session, key, value)
        self.store.sessions().create(session)
        return session

    def append_message(self, session_id: str, content: str, role: str = RoleUser, **overrides) -> Message:
        message = Message(session_id=session_id, role=role, content=content)
        for key, value in overrides.items():
            setattr(message, key, value)
        self.store.messages().append(message)
        return message


class TestStoreLifecycle(SqliteStoreTestCase):
    def test_open_creates_file_and_runs_migrations(self) -> None:
        self.assertTrue(os.path.exists(self.path))
        tables = {
            row["name"]
            for row in self.store.database.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertIn("schema_migrations", tables)
        self.assertIn("media_history", tables)

    def test_open_can_run_twice_on_same_file(self) -> None:
        self.store.sessions().create(Session(owner_id="u", platform="p", platform_scope_id="s"))
        self.store.close()
        again = new(self.path)
        try:
            self.assertEqual(1, len(again.sessions().list(ListSessionsRequest(include_all_platforms=True))))
        finally:
            again.close()

    def test_default_pragmas_match_go_upstream(self) -> None:
        database = self.store.database
        self.assertEqual(1, int(database.query_row("PRAGMA foreign_keys")[0]))
        self.assertEqual("delete", database.query_row("PRAGMA journal_mode")[0])
        self.assertEqual(2, int(database.query_row("PRAGMA synchronous")[0]))
        # 上游 Go 没有 busy timeout；本仓显式抵消 CPython 隐式的 5000ms。
        self.assertEqual(0, int(database.query_row("PRAGMA busy_timeout")[0]))

    def test_tuning_switches_are_opt_in(self) -> None:
        tuned = new(
            os.path.join(self.directory, "tuned.db"),
            journal_mode="WAL",
            synchronous="NORMAL",
            busy_timeout_ms=2500,
        )
        try:
            self.assertEqual("wal", tuned.database.query_row("PRAGMA journal_mode")[0])
            self.assertEqual(1, int(tuned.database.query_row("PRAGMA synchronous")[0]))
            self.assertEqual(2500, int(tuned.database.query_row("PRAGMA busy_timeout")[0]))
        finally:
            tuned.close()
        with self.assertRaises(InvalidError):
            new(os.path.join(self.directory, "bad.db"), journal_mode="WAL; DROP TABLE sessions")

    def test_empty_path_is_rejected(self) -> None:
        with self.assertRaises(InvalidError):
            new("")

    def test_closed_database_rejects_work(self) -> None:
        self.store.close()
        with self.assertRaises(ClosedError):
            self.store.sessions().create(Session(owner_id="u", platform="p", platform_scope_id="s"))
        self.store.close()  # 幂等

    def test_store_and_repositories_satisfy_the_protocols(self) -> None:
        self.assertIsInstance(self.store, Store)
        self.assertIsInstance(self.store.sessions(), SessionRepository)
        self.assertIsInstance(self.store.messages(), MessageRepository)
        self.assertIsInstance(self.store.media(), MediaRepository)
        self.assertIsInstance(self.store.media_references(), MediaReferenceRepository)
        self.assertIsInstance(self.store.context_summaries(), ContextSummaryRepository)
        self.assertIsInstance(self.store.tool_calls(), ToolCallRepository)
        self.assertIsInstance(self.store.cron_jobs(), CronJobRepository)
        self.assertIsInstance(self.store.atrinis_events(), AtrinisEventRepository)

    def test_with_transaction_commits_and_rolls_back(self) -> None:
        with self.store.database.with_transaction() as tx:
            tx.execute(
                """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('tx-1', 'u', 'p', 's', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
            )
        self.assertEqual("tx-1", self.store.sessions().get("tx-1").id)

        with self.assertRaises(RuntimeError):
            with self.store.database.with_transaction() as tx:
                tx.execute(
                    """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('tx-2', 'u', 'p', 's', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
                )
                raise RuntimeError("boom")
        with self.assertRaises(NotFoundError):
            self.store.sessions().get("tx-2")

    def test_nested_transaction_joins_the_outer_one(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.store.database.with_transaction() as tx:
                tx.execute(
                    """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('outer', 'u', 'p', 's', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
                )
                with self.store.database.with_transaction() as inner:
                    inner.execute(
                        """
INSERT INTO sessions (id, owner_id, platform, platform_scope_id, mode, status, created_at, updated_at)
VALUES ('inner', 'u', 'p', 's', 'work', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
                    )
                raise RuntimeError("boom")
        for session_id in ("outer", "inner"):
            with self.assertRaises(NotFoundError):
                self.store.sessions().get(session_id)

    def test_sql_errors_are_wrapped(self) -> None:
        with self.assertRaises(SQLiteError) as caught:
            self.store.database.execute("SELECT * FROM does_not_exist")
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)

    def test_generated_ids_are_hyphenated_lowercase_uuid4(self) -> None:
        session = self.new_session()
        parsed = uuid.UUID(session.id)
        self.assertEqual(4, parsed.version)
        self.assertEqual(session.id, str(parsed))


class TestSessionRepository(SqliteStoreTestCase):
    def test_create_fills_defaults_and_get_round_trips(self) -> None:
        session = self.new_session(metadata='{"workspace_dir":"D:\\\\work"}')
        loaded = self.store.sessions().get(session.id)
        self.assertEqual("work", loaded.mode)
        self.assertEqual("active", loaded.status)
        self.assertEqual("test", loaded.title)
        self.assertEqual('{"workspace_dir":"D:\\\\work"}', loaded.metadata)
        self.assertIsNotNone(loaded.created_at)
        self.assertEqual(loaded.created_at, loaded.updated_at)
        # 读回的时间必须是 aware 且归一化到 UTC（PORTING.md §1.7）。
        self.assertIsNotNone(loaded.created_at.tzinfo)
        self.assertEqual(timedelta(0), loaded.created_at.utcoffset())

    def test_get_missing_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError) as caught:
            self.store.sessions().get("missing")
        self.assertIn("not found", str(caught.exception))

    def test_update_overwrites_the_row_and_delete_removes_it(self) -> None:
        session = self.new_session()
        session.title = "renamed"
        session.status = SessionStatusClosed
        session.archived_at = now()
        self.store.sessions().update(session)
        loaded = self.store.sessions().get(session.id)
        self.assertEqual("renamed", loaded.title)
        self.assertEqual(SessionStatusClosed, loaded.status)
        self.assertIsNotNone(loaded.archived_at)

        self.store.sessions().delete(session.id)
        with self.assertRaises(NotFoundError):
            self.store.sessions().get(session.id)
        with self.assertRaises(NotFoundError):
            self.store.sessions().delete(session.id)
        with self.assertRaises(NotFoundError):
            self.store.sessions().update(session)

    def test_list_filters_by_owner_platform_and_scope(self) -> None:
        s1 = self.new_session(title="cli session")
        s2 = self.new_session(platform="tg", platform_scope_id="chat:1", title="tg session")
        self.new_session(owner_id="u2", platform_scope_id="group:2", title="other owner")
        self.new_session(platform_scope_id="cron:1", title="cron session")

        listed = self.store.sessions().list(
            ListSessionsRequest(actor_id="u1", platform="cli", platform_scope_id="local")
        )
        self.assertEqual([s1.id], [row.id for row in listed])

        with_cron = self.store.sessions().list(
            ListSessionsRequest(
                actor_id="u1", platform="cli", platform_scope_id="local", include_same_platform_cron=True
            )
        )
        self.assertEqual(2, len(with_cron))

        every_platform = self.store.sessions().list(
            ListSessionsRequest(include_all_platforms=True, exclude_session_id=s2.id)
        )
        self.assertEqual(3, len(every_platform))

        by_query = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True, query="tg"))
        self.assertEqual([s2.id], [row.id for row in by_query])

        limited = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True, limit=1))
        self.assertEqual(1, len(limited))

    def test_archived_filtering(self) -> None:
        archived = self.new_session(title="archived")
        self.new_session(title="active one")
        archived.archived_at = now()
        self.store.sessions().update(archived)

        default = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True))
        self.assertEqual(["active one"], [row.title for row in default])
        only = self.store.sessions().list(
            ListSessionsRequest(include_all_platforms=True, archived_only=True)
        )
        self.assertEqual(["archived"], [row.title for row in only])
        both = self.store.sessions().list(
            ListSessionsRequest(include_all_platforms=True, include_archived=True)
        )
        self.assertEqual(2, len(both))

    def test_pinned_sessions_come_first(self) -> None:
        old = self.new_session(title="old")
        newer = self.new_session(title="newer")
        old.pinned_at = now()
        self.store.sessions().update(old)
        listed = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True))
        self.assertEqual([old.id, newer.id], [row.id for row in listed])
        by_updated = self.store.sessions().list(
            ListSessionsRequest(include_all_platforms=True, order_by_updated_at=True)
        )
        self.assertEqual([newer.id, old.id], [row.id for row in by_updated])

    def test_list_preview_skips_blank_messages(self) -> None:
        session = self.new_session()
        self.append_message(session.id, "", RoleUser)
        self.append_message(session.id, "hello", RoleUser)
        self.append_message(session.id, "  \n\t  ", RoleAssistant)
        self.append_message(session.id, "hi", RoleAssistant)

        summaries = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True))
        self.assertEqual(1, len(summaries))
        self.assertEqual("u: hello / b: hi / ...", summaries[0].message_preview)
        self.assertEqual(4, summaries[0].message_count)
        self.assertEqual("hello", summaries[0].last_user_preview)
        self.assertEqual("hi", summaries[0].last_bot_preview)

    def test_list_preview_uses_insertion_order_for_equal_timestamps(self) -> None:
        session = self.new_session()
        stamp = now()
        for role, content in (
            (RoleUser, "first"),
            (RoleUser, "second"),
            (RoleAssistant, "third"),
            (RoleAssistant, "fourth"),
        ):
            self.append_message(session.id, content, role, created_at=stamp)

        summaries = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True))
        self.assertEqual("second", summaries[0].last_user_preview)
        self.assertEqual("fourth", summaries[0].last_bot_preview)
        self.assertEqual("u: first / u: second / b: third / b: fourth", summaries[0].message_preview)

    def test_preview_truncates_by_code_point(self) -> None:
        session = self.new_session()
        self.append_message(session.id, "一" * 30, RoleUser)
        summaries = self.store.sessions().list(ListSessionsRequest(include_all_platforms=True))
        self.assertEqual("u: " + "一" * 24 + "...", summaries[0].message_preview)

    def test_delete_expired_skips_archived_and_pinned(self) -> None:
        stale = self.new_session(title="stale")
        pinned = self.new_session(title="pinned")
        archived = self.new_session(title="archived")
        fresh = self.new_session(title="fresh")
        old_stamp = now() - timedelta(days=30)
        for session in (stale, pinned, archived):
            session.updated_at = old_stamp
            self.store.sessions().update(session)
        pinned.pinned_at = now()
        self.store.sessions().update(pinned)
        archived.archived_at = now()
        self.store.sessions().update(archived)

        removed = self.store.sessions().delete_expired(now() - timedelta(days=1))
        self.assertEqual(1, removed)
        remaining = {
            row.id
            for row in self.store.sessions().list(
                ListSessionsRequest(include_all_platforms=True, include_archived=True, limit=50)
            )
        }
        self.assertEqual({pinned.id, archived.id, fresh.id}, remaining)


class TestSessionMutate(SqliteStoreTestCase):
    def test_mutate_reads_latest_row_inside_the_transaction(self) -> None:
        session = self.new_session(metadata='{"workspace_dir":"D:\\\\work"}')
        stale = self.store.sessions().get(session.id)

        self.store.sessions().mutate(
            session.id, lambda current: setattr(current, "title", "renamed by concurrent writer")
        )

        seen: list[Session] = []

        def update(current: Session) -> None:
            seen.append(current)
            current.metadata = '{"workspace_dir":"D:\\\\work","tool_cache":["web_search"]}'

        updated = self.store.sessions().mutate(session.id, update)
        self.assertEqual(1, len(seen))
        self.assertEqual("renamed by concurrent writer", seen[0].title)
        self.assertEqual("renamed by concurrent writer", updated.title)
        self.assertEqual('{"workspace_dir":"D:\\\\work","tool_cache":["web_search"]}', updated.metadata)

        persisted = self.store.sessions().get(session.id)
        self.assertEqual("renamed by concurrent writer", persisted.title)
        self.assertEqual(["web_search"], json.loads(persisted.metadata)["tool_cache"])
        self.assertEqual("test", stale.title)  # 陈旧快照是另一个对象，不许被改写

    def test_mutate_propagates_callback_error_without_writing(self) -> None:
        session = self.new_session()

        class Rejected(Exception):
            pass

        def update(current: Session) -> None:
            current.title = "must not be written"
            raise Rejected("reject mutation")

        with self.assertRaises(Rejected):
            self.store.sessions().mutate(session.id, update)
        self.assertEqual("test", self.store.sessions().get(session.id).title)

    def test_mutate_missing_session_and_none_callback(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.sessions().mutate("missing", lambda current: None)
        session = self.new_session()
        with self.assertRaises(InvalidError):
            self.store.sessions().mutate(session.id, None)

    def test_mutate_fills_updated_at_when_callback_leaves_it_zero(self) -> None:
        session = self.new_session()
        before = self.store.sessions().get(session.id).updated_at

        def update(current: Session) -> None:
            current.title = "touched"
            current.updated_at = None

        updated = self.store.sessions().mutate(session.id, update)
        self.assertIsNotNone(updated.updated_at)
        self.assertGreaterEqual(updated.updated_at, before)

    def test_mutate_serializes_concurrent_writers_without_lost_updates(self) -> None:
        session = self.new_session(metadata='{"workspace_dir":"D:\\\\work"}')
        writers = 8
        barrier = threading.Barrier(writers)
        failures: list[BaseException] = []

        def writer(index: int) -> None:
            name = f"tool_{index:02d}"

            def update(current: Session) -> None:
                names = json.loads(current.metadata).get("tool_cache", [])
                names.append(name)
                names.sort()
                current.metadata = json.dumps(
                    {"workspace_dir": "D:\\work", "tool_cache": names},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            try:
                barrier.wait()
                self.store.sessions().mutate(session.id, update)
            except BaseException as exc:  # noqa: BLE001 - 汇总到主线程再断言
                failures.append(exc)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures)
        persisted = self.store.sessions().get(session.id)
        names = json.loads(persisted.metadata)["tool_cache"]
        self.assertEqual(writers, len(names), names)
        self.assertEqual([f"tool_{index:02d}" for index in range(writers)], names)


class TestMessageRepository(SqliteStoreTestCase):
    def test_append_get_and_list_ordered(self) -> None:
        session = self.new_session()
        first = self.append_message(session.id, "hello")
        second = self.append_message(session.id, "hi", RoleAssistant)
        self.assertNotEqual(first.id, second.id)
        self.assertIsNotNone(first.created_at)

        loaded = self.store.messages().get(first.id)
        self.assertEqual(session.id, loaded.session_id)
        self.assertEqual("hello", loaded.content)
        self.assertEqual("", loaded.segments)
        self.assertEqual("", loaded.metadata)
        self.assertEqual([first.id, second.id], [m.id for m in self.store.messages().list_by_session(session.id)])

    def test_list_by_session_orders_same_second_variable_precision(self) -> None:
        """同一秒内、小数位长度不同的 ``created_at`` 必须按真实时间排序（1.1.4 回归）。

        历史 bug：``format_time`` 按 Go ``RFC3339Nano`` 去掉小数秒末尾的 0，于是
        ``…:45.7835Z``（783500µs）与 ``…:45.783545Z``（783545µs）按**字符串**比较时，
        前者的 ``Z``（U+005A）大于后者的 ``4``，导致较早的消息排到后面——
        1.1.4 稳定性测试里实测到同一会话的 user/assistant 次序偶发颠倒。
        修复方式：查询侧用 ``rtrim(created_at, 'Z')`` 归一化比较。
        """
        session = self.new_session()
        earlier = self.append_message(
            session.id,
            "earlier",
            created_at=parse_time("2026-10-29T16:38:45.783500Z"),
        )
        later = self.append_message(
            session.id,
            "later",
            RoleAssistant,
            created_at=parse_time("2026-10-29T16:38:45.783545Z"),
        )
        # 直接按字符串比会得到 [later, earlier]；修复后必须是真实时间顺序。
        ordered = self.store.messages().list_by_session(session.id)
        self.assertEqual([earlier.id, later.id], [m.id for m in ordered])

    def test_segments_round_trip_and_empty_segments_are_null(self) -> None:
        session = self.new_session()
        text_only = self.append_message(session.id, "plain")
        raw = self.store.database.query_row("SELECT segments FROM messages WHERE id = ?", (text_only.id,))
        self.assertIsNone(raw["segments"])

        segments = segments_json(
            [
                MessageSegment(type=SegmentText, text="看图"),
                MessageSegment(type=SegmentImage, url="https://example.com/a.png", name="a.png"),
            ]
        )
        with_segments = self.append_message(session.id, "看图", segments=segments)
        loaded = self.store.messages().get(with_segments.id)
        self.assertEqual(segments, loaded.segments)
        self.assertIn('"type":"image"', loaded.segments)
        self.assertNotIn("media_id", loaded.segments)

    def test_append_rolls_back_when_segments_cannot_be_decoded(self) -> None:
        session = self.new_session()
        broken = Message(session_id=session.id, role=RoleUser, content="x", segments="{not json")
        with self.assertRaises(InvalidError):
            self.store.messages().append(broken)
        with self.assertRaises(NotFoundError):
            self.store.messages().get(broken.id)

    def test_checkpoint_windows(self) -> None:
        session = self.new_session()
        first = self.append_message(session.id, "first")
        second = self.append_message(session.id, "second", RoleAssistant)
        third = self.append_message(session.id, "third")

        up_to = self.store.messages().list_by_session_up_to(session.id, second.id)
        self.assertEqual([first.id, second.id], [m.id for m in up_to])
        after = self.store.messages().list_by_session_after(session.id, second.id)
        self.assertEqual([third.id], [m.id for m in after])
        between = self.store.messages().list_by_session_after_up_to(session.id, first.id, second.id)
        self.assertEqual([second.id], [m.id for m in between])
        with self.assertRaises(NotFoundError):
            self.store.messages().list_by_session_up_to(session.id, "missing")

    def test_checkpoints_are_scoped_to_the_session(self) -> None:
        left = self.new_session()
        right = self.new_session(platform_scope_id="local2")
        message = self.append_message(left.id, "hello")
        with self.assertRaises(NotFoundError):
            self.store.messages().list_by_session_after(right.id, message.id)

    def test_platform_message_map_round_trip_and_cascade(self) -> None:
        session = self.new_session()
        message = self.append_message(session.id, "hello")
        self.store.messages().map_platform_message(
            PlatformMessageMap(
                platform="cli",
                platform_scope_id="local",
                platform_message_id="p1",
                message_id=message.id,
                session_id=session.id,
            )
        )
        mapped = self.store.messages().find_by_platform_message("cli", "local", "p1")
        self.assertEqual(message.id, mapped.id)
        with self.assertRaises(NotFoundError):
            self.store.messages().find_by_platform_message("cli", "local", "nope")

        self.store.sessions().delete(session.id)
        with self.assertRaises(NotFoundError):
            self.store.messages().get(message.id)
        count = self.store.database.query_row(
            "SELECT COUNT(*) AS count FROM platform_message_map WHERE session_id = ?", (session.id,)
        )
        self.assertEqual(0, int(count["count"]))


class TestContextSummaryRepository(SqliteStoreTestCase):
    def test_create_latest_and_latest_up_to(self) -> None:
        session = self.new_session()
        first = self.append_message(session.id, "first")
        second = self.append_message(session.id, "second", RoleAssistant)
        third = self.append_message(session.id, "third")

        summary = ContextSummary(
            session_id=session.id,
            from_message_id=first.id,
            to_message_id=second.id,
            summary="first two messages",
            provider="zhipu",
            model="glm-4-flash",
            source_tokens=10,
            summary_tokens=3,
            total_tokens=13,
            cache_hit_tokens=2,
            trigger_reason="manual",
            metadata='{"auto":true}',
        )
        self.store.context_summaries().create(summary)
        self.assertTrue(summary.id)

        latest = self.store.context_summaries().latest_by_session(session.id)
        self.assertEqual("first two messages", latest.summary)
        self.assertEqual(13, latest.total_tokens)
        self.assertEqual(2, latest.cache_hit_tokens)
        self.assertEqual(first.id, latest.from_message_id)
        self.assertEqual('{"auto":true}', latest.metadata)

        up_to = self.store.context_summaries().latest_by_session_up_to(session.id, second.id)
        self.assertEqual(latest.id, up_to.id)
        with self.assertRaises(NotFoundError):
            self.store.context_summaries().latest_by_session_up_to(session.id, first.id)
        with self.assertRaises(NotFoundError):
            self.store.context_summaries().latest_by_session_up_to(session.id, "missing")
        self.assertEqual(third.id, third.id)

    def test_latest_by_session_after_session_delete(self) -> None:
        session = self.new_session()
        message = self.append_message(session.id, "first")
        self.store.context_summaries().create(
            ContextSummary(
                session_id=session.id,
                to_message_id=message.id,
                summary="s",
                provider="p",
                model="m",
                trigger_reason="manual",
            )
        )
        self.store.sessions().delete(session.id)
        with self.assertRaises(NotFoundError):
            self.store.context_summaries().latest_by_session(session.id)

    def test_latest_by_session_missing_raises(self) -> None:
        session = self.new_session()
        with self.assertRaises(NotFoundError):
            self.store.context_summaries().latest_by_session(session.id)


class TestToolCallRepository(SqliteStoreTestCase):
    def test_create_successful_ids_and_usage(self) -> None:
        session = self.new_session()
        self.store.tool_calls().create(
            ToolCallRecord(
                session_id=session.id,
                tool_call_id="call-1",
                tool_name="web_search",
                actor_id="u1",
                risk_level="low",
                success=True,
                result_preview="ok",
            )
        )
        self.store.tool_calls().create(
            ToolCallRecord(
                session_id=session.id,
                tool_call_id="call-2",
                tool_name="web_search",
                actor_id="u1",
                risk_level="low",
                success=False,
                error="boom",
            )
        )
        self.store.tool_calls().create(
            ToolCallRecord(
                session_id=session.id,
                tool_call_id="call-3",
                tool_name="file_read",
                actor_id="u1",
                risk_level="medium",
                success=True,
            )
        )

        successful = self.store.tool_calls().successful_ids(["call-1", " call-2 ", "call-2", "", "call-3", "nope"])
        self.assertEqual({"call-1": True, "call-3": True}, successful)
        self.assertEqual({}, self.store.tool_calls().successful_ids([]))
        self.assertEqual({}, self.store.tool_calls().successful_ids(["  "]))

        usage = self.store.tool_calls().usage_by_session(session.id)
        self.assertEqual([("file_read", 1), ("web_search", 2)], [(row.tool_name, row.count) for row in usage])

    def test_records_are_deleted_with_the_session(self) -> None:
        session = self.new_session()
        self.store.tool_calls().create(
            ToolCallRecord(session_id=session.id, tool_call_id="c", tool_name="t", actor_id="a", risk_level="low")
        )
        self.store.sessions().delete(session.id)
        self.assertEqual([], self.store.tool_calls().usage_by_session(session.id))

    def test_successful_ids_batches_over_500(self) -> None:
        session = self.new_session()
        ids = [f"call-{index:04d}" for index in range(1100)]
        for tool_call_id in ids:
            self.store.tool_calls().create(
                ToolCallRecord(
                    session_id=session.id,
                    tool_call_id=tool_call_id,
                    tool_name="t",
                    actor_id="a",
                    risk_level="low",
                    success=True,
                )
            )
        successful = self.store.tool_calls().successful_ids(ids)
        self.assertEqual(set(ids), set(successful))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
