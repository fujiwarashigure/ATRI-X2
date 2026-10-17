"""``atri_x/storage/sqlite`` 的 cron / atrinis outbox / 聊天历史 / fork 边界 unittest。

真实 SQLite 文件 + 真实 migration（含触发器）；临时目录用仓库根下的 ``tmp_sqlite_test/``。
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import timedelta

from atri_x.errors import ConflictError, InvalidError, NotFoundError
from atri_x.storage.models import (
    AtrinisReportDeliveryDelivered,
    AtrinisReportDeliveryFailed,
    AtrinisReportDeliveryPending,
    ChatHistoryAroundRequest,
    ChatHistoryRangeRequest,
    ChatHistorySearchRequest,
    ChatMessage,
    CreateAtrinisEventRequest,
    CreateAtrinisReportDeliveryRequest,
    CronJobRunState,
    ListSessionsRequest,
    Media,
    Message,
    OutboundMessage,
    OutboundMessageRangeRequest,
    PrepareAtrinisReportRequest,
    RoleAssistant,
    RoleUser,
    Session,
    UpdateAtrinisEventRequest,
    UpsertCronJobRequest,
    segments_json,
)
from atri_x.llm.llm import MessageSegment, SegmentImage
from atri_x.storage.repositories import ChatHistoryRangeRepository, OutboundMessageCounter
from atri_x.storage.sqlite import SQLiteError, new, new_chat_history
from atri_x.storage.times import now

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_ROOT = os.path.join(_REPO_ROOT, "tmp_sqlite_test")

RESULT_READY = "result_ready"
DELIVERING = "delivering"
COMPLETED = "completed"


class SqliteRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = os.path.join(_TMP_ROOT, "repositories", self._testMethodName)
        shutil.rmtree(self.directory, ignore_errors=True)
        os.makedirs(self.directory, exist_ok=True)
        self.path = os.path.join(self.directory, "atrix_sessions.db")
        self.store = new(self.path)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def media_exists(self, media_id: str) -> None:
        self.store.media().upsert(Media(id=media_id, mime_type="image/png", backend="local"))


class TestCronJobRepository(SqliteRepositoryTestCase):
    def test_upsert_creates_then_updates_configuration(self) -> None:
        initial_next_run = now() + timedelta(minutes=30)
        job = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name="system.maintenance.cleanup",
                handler="maintenance.cleanup",
                schedule="0 3 * * *",
                enabled=True,
                metadata='{"system":true}',
                next_run_at=initial_next_run,
            )
        )
        self.assertTrue(job.id)
        self.assertTrue(job.enabled)
        self.assertEqual(0, job.run_count)
        self.assertEqual("", job.delivery_state)
        self.assertEqual("", job.delivery_token)
        self.assertEqual(initial_next_run, job.next_run_at)

        updated = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name=job.name,
                handler=job.handler,
                schedule="0 4 * * *",
                enabled=True,
                metadata='{"system":true,"updated":true}',
                next_run_at=initial_next_run + timedelta(hours=1),
            )
        )
        self.assertEqual(job.id, updated.id)
        self.assertEqual("0 4 * * *", updated.schedule)
        self.assertEqual('{"system":true,"updated":true}', updated.metadata)
        self.assertGreaterEqual(updated.updated_at, job.updated_at)

        enabled = self.store.cron_jobs().list_enabled()
        self.assertEqual([job.name], [row.name for row in enabled])
        self.assertEqual(1, len(self.store.cron_jobs().list(True)))
        self.assertEqual([job.name], [row.name for row in self.store.cron_jobs().list(False)])

    def test_upsert_skips_write_when_nothing_changed(self) -> None:
        req = UpsertCronJobRequest(
            name="system.maintenance.cleanup",
            handler="maintenance.cleanup",
            schedule="0 3 * * *",
            enabled=True,
            metadata='{"system":true}',
        )
        job = self.store.cron_jobs().upsert(req)
        unchanged = self.store.cron_jobs().upsert(req)
        self.assertEqual(job.id, unchanged.id)
        self.assertEqual(job.updated_at, unchanged.updated_at)

        changed = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name=req.name,
                handler=req.handler,
                schedule="0 4 * * *",
                enabled=True,
                metadata=req.metadata,
            )
        )
        self.assertGreater(changed.updated_at, job.updated_at)

    def test_run_state_disable_and_delete(self) -> None:
        job = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name="user.cron", handler="builtin.cron", schedule="0 3 * * *", enabled=True
            )
        )
        last_run = now()
        next_run = last_run + timedelta(hours=1)
        self.store.cron_jobs().update_run_state(
            job.id,
            CronJobRunState(
                last_run_at=last_run,
                next_run_at=next_run,
                run_count=1,
                last_error="boom",
                enabled=True,
                updated_at=last_run,
            ),
        )
        got = self.store.cron_jobs().get_by_name("user.cron")
        self.assertEqual(1, got.run_count)
        self.assertEqual("boom", got.last_error)
        self.assertEqual(last_run, got.last_run_at)
        self.assertEqual(next_run, got.next_run_at)

        self.store.cron_jobs().update_next_run_at(job.id, None, now())
        self.assertIsNone(self.store.cron_jobs().get_by_name("user.cron").next_run_at)
        with self.assertRaises(NotFoundError):
            self.store.cron_jobs().update_next_run_at("missing", None, now())

        self.store.cron_jobs().disable_by_name(job.name)
        disabled = self.store.cron_jobs().get_by_name(job.name)
        self.assertFalse(disabled.enabled)
        self.assertIsNone(disabled.next_run_at)
        self.assertEqual([], self.store.cron_jobs().list_enabled())
        with self.assertRaises(NotFoundError):
            self.store.cron_jobs().disable_by_name("missing")
        with self.assertRaises(NotFoundError):
            self.store.cron_jobs().update_run_state("missing", CronJobRunState())

        self.store.cron_jobs().delete_by_name(job.name)
        with self.assertRaises(NotFoundError):
            self.store.cron_jobs().get_by_name(job.name)
        with self.assertRaises(NotFoundError):
            self.store.cron_jobs().delete_by_name(job.name)

    def test_delivery_is_separate_from_configuration(self) -> None:
        req = UpsertCronJobRequest(
            name="user.cron.delivery",
            handler="builtin.cron",
            schedule="0 3 * * *",
            enabled=True,
            metadata='{"title":"old"}',
        )
        job = self.store.cron_jobs().upsert(req)

        self.assertTrue(self.store.cron_jobs().compare_and_swap_delivery(job.id, "", "session-1", '{"report_ready":true}'))
        self.assertFalse(self.store.cron_jobs().compare_and_swap_delivery(job.id, "wrong", "session-2", "{}"))

        req.metadata = '{"title":"new"}'
        preserved = self.store.cron_jobs().upsert(req)
        self.assertEqual("session-1", preserved.delivery_token)
        self.assertEqual('{"report_ready":true}', preserved.delivery_state)
        self.assertEqual('{"title":"new"}', preserved.metadata)

        req.reset_delivery = True
        reset = self.store.cron_jobs().upsert(req)
        self.assertEqual("", reset.delivery_token)
        self.assertEqual("", reset.delivery_state)

        self.assertFalse(self.store.cron_jobs().disable_by_name_if_delivery_token(job.name, "session-1"))
        self.assertTrue(self.store.cron_jobs().get_by_name(job.name).enabled)

    def test_delivery_cas_never_revives_a_disabled_job(self) -> None:
        job = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(name="user.cron", handler="h", schedule="* * * * *", enabled=True)
        )
        self.assertTrue(self.store.cron_jobs().compare_and_swap_delivery(job.id, "", "run-1", "{}"))
        self.assertTrue(self.store.cron_jobs().disable_by_name_if_delivery_token(job.name, "run-1"))
        self.assertFalse(self.store.cron_jobs().get_by_name(job.name).enabled)

        # 停用之后再来的同轮次结果不许写回去。
        self.assertFalse(self.store.cron_jobs().compare_and_swap_delivery(job.id, "run-1", "run-2", "{}"))
        self.assertFalse(self.store.cron_jobs().disable_by_name_if_delivery_token(job.name, "run-1"))
        untouched = self.store.cron_jobs().get_by_name(job.name)
        self.assertEqual("run-1", untouched.delivery_token)
        self.assertFalse(untouched.enabled)


class TestAtrinisEventRepository(SqliteRepositoryTestCase):
    def create_event(self, **overrides):
        req = CreateAtrinisEventRequest(
            event_key="key-1",
            token_name="token",
            atriwisp_name="demo",
            source="qqonebot",
            source_id="msg-1",
            mode="direct",
            content_hash="hash",
            status="accepted",
        )
        for key, value in overrides.items():
            setattr(req, key, value)
        return self.store.atrinis_events().create(req)

    def test_create_dedup_key_and_input_media_references(self) -> None:
        media_id = "media:" + "a" * 64
        self.media_exists(media_id)
        event = self.create_event(media_ids=[media_id], tags='["t"]')
        self.assertEqual("accepted", event.status)
        self.assertEqual("", event.session_id)
        self.assertIsNotNone(event.received_at)
        self.assertEqual(event.received_at, event.created_at)

        refs = self.store.media_references().list_by_owner("atrinis_event", event.id)
        self.assertEqual([media_id], [row.media_id for row in refs])
        self.assertEqual("input", refs[0].purpose)

        loaded = self.store.atrinis_events().get(event.id)
        self.assertEqual('["t"]', loaded.tags)
        self.assertEqual(event.id, self.store.atrinis_events().get_by_key("demo", "qqonebot", "msg-1").id)

        # 去重键是表上的 UNIQUE(atriwisp_name, source, source_id)。
        with self.assertRaises(SQLiteError):
            self.create_event(event_key="key-2")
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().get("missing")
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().get_by_key("demo", "qqonebot", "other")

    def test_update_and_delete_releases_event_references(self) -> None:
        media_id = "media:" + "b" * 64
        self.media_exists(media_id)
        event = self.create_event(media_ids=[media_id])

        self.store.atrinis_events().update(
            UpdateAtrinisEventRequest(
                id=event.id,
                resolved_targets='["t1"]',
                status="queued",
                session_id="session-1",
                result='{"ok":true}',
            )
        )
        updated = self.store.atrinis_events().get(event.id)
        self.assertEqual("queued", updated.status)
        self.assertEqual('["t1"]', updated.resolved_targets)
        self.assertEqual("session-1", updated.session_id)
        self.assertEqual('{"ok":true}', updated.result)
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().update(UpdateAtrinisEventRequest(id="missing", status="queued"))

        self.store.database.execute("DELETE FROM atrinis_events WHERE id = ?", (event.id,))
        self.assertEqual([], self.store.media_references().list_media_ids(media_id))

    def test_prepare_report_uses_zero_based_ordinals_and_is_atomic(self) -> None:
        event = self.create_event()
        with self.assertRaises(InvalidError):
            self.store.atrinis_events().prepare_report(PrepareAtrinisReportRequest(event_id=event.id))

        self.store.atrinis_events().prepare_report(
            PrepareAtrinisReportRequest(
                event_id=event.id,
                resolved_targets='["target"]',
                session_id="session-1",
                result='{"text":"hi"}',
                result_ready_status=RESULT_READY,
                deliveries=[
                    CreateAtrinisReportDeliveryRequest(target="t1", output='{"a":1}'),
                    CreateAtrinisReportDeliveryRequest(target="t2", output='{"a":2}', message_id="m-2"),
                ],
            )
        )
        prepared = self.store.atrinis_events().get(event.id)
        self.assertEqual(RESULT_READY, prepared.status)
        self.assertEqual('{"text":"hi"}', prepared.result)
        self.assertEqual("", prepared.error)

        deliveries = self.store.atrinis_events().list_report_deliveries(event.id)
        self.assertEqual([0, 1], [row.ordinal for row in deliveries])  # Go 的 range 从 0 起
        self.assertEqual([AtrinisReportDeliveryPending, AtrinisReportDeliveryPending], [r.status for r in deliveries])
        self.assertEqual(["t1", "t2"], [row.target for row in deliveries])
        self.assertEqual("m-2", deliveries[1].message_id)
        self.assertEqual([0, 0], [row.attempts for row in deliveries])

        before = len(deliveries)
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().prepare_report(
                PrepareAtrinisReportRequest(
                    event_id="missing",
                    result_ready_status=RESULT_READY,
                    deliveries=[CreateAtrinisReportDeliveryRequest(target="t", output="{}")],
                )
            )
        self.assertEqual(before, len(self.store.atrinis_events().list_report_deliveries(event.id)))

    def test_claim_release_and_restart_recovery(self) -> None:
        event = self.create_event()
        self.store.atrinis_events().prepare_report(
            PrepareAtrinisReportRequest(
                event_id=event.id,
                result_ready_status=RESULT_READY,
                deliveries=[CreateAtrinisReportDeliveryRequest(target="t", output="{}")],
            )
        )
        self.assertEqual([event.id], self.store.atrinis_events().list_result_ready_report_ids(RESULT_READY))
        self.assertTrue(self.store.atrinis_events().claim_report(event.id, RESULT_READY, DELIVERING))
        self.assertFalse(self.store.atrinis_events().claim_report(event.id, RESULT_READY, DELIVERING))
        self.assertEqual(DELIVERING, self.store.atrinis_events().get(event.id).status)
        self.assertFalse(self.store.atrinis_events().claim_report("missing", RESULT_READY, DELIVERING))

        self.store.cron_jobs()  # 只是确认门面可用，不参与本用例
        reopened = new(self.path)
        try:
            reopened.atrinis_events().reset_delivering_reports(DELIVERING, RESULT_READY)
            self.assertEqual(RESULT_READY, reopened.atrinis_events().get(event.id).status)
            self.assertEqual([event.id], reopened.atrinis_events().list_result_ready_report_ids(RESULT_READY))
        finally:
            reopened.close()

        self.store.atrinis_events().claim_report(event.id, RESULT_READY, DELIVERING)
        self.store.atrinis_events().release_report(event.id, DELIVERING, RESULT_READY, "timeout")
        released = self.store.atrinis_events().get(event.id)
        self.assertEqual(RESULT_READY, released.status)
        self.assertEqual("timeout", released.error)
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().release_report(event.id, DELIVERING, RESULT_READY, "again")

    def test_only_all_delivered_receipts_complete_the_report(self) -> None:
        event = self.create_event()
        self.store.atrinis_events().prepare_report(
            PrepareAtrinisReportRequest(
                event_id=event.id,
                result_ready_status=RESULT_READY,
                deliveries=[
                    CreateAtrinisReportDeliveryRequest(target="t1", output="{}"),
                    CreateAtrinisReportDeliveryRequest(target="t2", output="{}"),
                ],
            )
        )
        self.store.atrinis_events().claim_report(event.id, RESULT_READY, DELIVERING)
        deliveries = self.store.atrinis_events().list_report_deliveries(event.id)

        self.store.atrinis_events().start_report_delivery(deliveries[0].id)
        self.assertEqual(1, self.store.atrinis_events().list_report_deliveries(event.id)[0].attempts)
        self.store.atrinis_events().mark_report_delivery_delivered(deliveries[0].id, '{"message_id":"m1"}')

        # 还有一条没投完 → 不许 completed。
        with self.assertRaises(ConflictError):
            self.store.atrinis_events().complete_report(event.id, DELIVERING, COMPLETED)
        self.assertEqual(DELIVERING, self.store.atrinis_events().get(event.id).status)

        # 第二条先失败，事件被放回待投递。
        self.store.atrinis_events().start_report_delivery(deliveries[1].id)
        self.store.atrinis_events().mark_report_delivery_failed(event.id, deliveries[1].id, RESULT_READY, "boom")
        failed = self.store.atrinis_events().list_report_deliveries(event.id)[1]
        self.assertEqual(AtrinisReportDeliveryFailed, failed.status)
        self.assertEqual("boom", failed.error)
        self.assertEqual(1, failed.attempts)
        self.assertEqual(RESULT_READY, self.store.atrinis_events().get(event.id).status)

        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().mark_report_delivery_failed(event.id, "missing", RESULT_READY, "boom")

        # 重投：失败的那条最终成功 → 全部 delivered 才 completed。
        self.store.atrinis_events().claim_report(event.id, RESULT_READY, DELIVERING)
        self.store.atrinis_events().mark_report_delivery_delivered(deliveries[1].id, '{"message_id":"m2"}')
        self.store.atrinis_events().complete_report(event.id, DELIVERING, COMPLETED)
        completed = self.store.atrinis_events().get(event.id)
        self.assertEqual(COMPLETED, completed.status)
        self.assertEqual("", completed.error)

        delivered = self.store.atrinis_events().list_report_deliveries(event.id)
        self.assertEqual([AtrinisReportDeliveryDelivered, AtrinisReportDeliveryDelivered], [r.status for r in delivered])
        self.assertEqual('{"message_id":"m2"}', delivered[1].receipt)

        # 投递完成之后再标一次 delivered 是幂等拒绝（Go 的 status != delivered 条件）。
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().mark_report_delivery_delivered(deliveries[1].id, "again")
        with self.assertRaises(NotFoundError):
            self.store.atrinis_events().start_report_delivery(deliveries[0].id)
        with self.assertRaises(ConflictError):
            self.store.atrinis_events().complete_report(event.id, DELIVERING, COMPLETED)

    def test_report_media_reference_follows_delivery_lifecycle(self) -> None:
        media_id = "media:" + "c" * 64
        self.media_exists(media_id)
        event = self.create_event()
        output = '{"Source":{"media":"' + media_id + '"},"Segments":[]}'
        self.store.atrinis_events().prepare_report(
            PrepareAtrinisReportRequest(
                event_id=event.id,
                result_ready_status=RESULT_READY,
                deliveries=[CreateAtrinisReportDeliveryRequest(target="t1", output=output)],
            )
        )
        deliveries = self.store.atrinis_events().list_report_deliveries(event.id)
        refs = self.store.media_references().list_by_owner("atrinis_report", deliveries[0].id)
        self.assertEqual([media_id], [row.media_id for row in refs])
        self.assertEqual("delivery", refs[0].purpose)
        self.assertEqual([], self.store.media().check_references())

        self.store.atrinis_events().claim_report(event.id, RESULT_READY, DELIVERING)
        self.store.atrinis_events().mark_report_delivery_delivered(deliveries[0].id, "{}")
        self.store.atrinis_events().complete_report(event.id, DELIVERING, COMPLETED)
        self.assertEqual([], self.store.media_references().list_media_ids(media_id))


class TestChatHistoryRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = os.path.join(_TMP_ROOT, "chat_history", self._testMethodName)
        shutil.rmtree(self.directory, ignore_errors=True)
        os.makedirs(self.directory, exist_ok=True)
        self.path = os.path.join(self.directory, "history.db")
        self.history = new_chat_history(self.path)
        self.repository = self.history.repository()
        self.outbound = self.history.outbound()

    def tearDown(self) -> None:
        try:
            self.history.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def append(self, message_id: str, text: str, *, at=None, segments: str = "", sender: str = "user") -> ChatMessage:
        message = ChatMessage(
            platform="qqonebot",
            platform_scope_id="group:1",
            scope_type="group",
            platform_message_id=message_id,
            sender_id=sender,
            sender_name="阿特莉",
            text=text,
            segments=segments,
        )
        if at is not None:
            message.created_at = at
        self.repository.append(message)
        return message

    def test_new_chat_history_creates_tables_and_is_idempotent(self) -> None:
        tables = {
            row["name"]
            for row in self.history.database.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertIn("chat_messages", tables)
        self.assertIn("outbound_messages", tables)
        self.assertIn("sqlite_sequence", tables)  # AUTOINCREMENT
        indexes = {
            row["name"]
            for row in self.history.database.query("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        for name in (
            "idx_chat_messages_scope_seq",
            "idx_chat_messages_scope_created_at",
            "idx_chat_messages_sender",
            "idx_chat_messages_created_at",
            "idx_outbound_messages_scope_seq",
            "idx_outbound_messages_scope_created_at",
            "idx_outbound_messages_created_at",
        ):
            self.assertIn(name, indexes)

        self.history.close()
        self.history = new_chat_history(self.path)
        self.repository = self.history.repository()
        self.outbound = self.history.outbound()
        self.append("keep", "kept")
        self.assertEqual("kept", self.repository.get_by_platform_message("qqonebot", "group:1", "keep").text)

    def test_append_upserts_by_platform_message_id_and_keeps_seq(self) -> None:
        first = self.append("m1", "hello")
        self.append("m1", "hello again", sender="user2")
        rows = self.history.database.query("SELECT seq, sender_id, text FROM chat_messages")
        self.assertEqual(1, len(rows))
        self.assertEqual("user2", rows[0]["sender_id"])
        self.assertEqual("hello again", rows[0]["text"])

        loaded = self.repository.get_by_platform_message("qqonebot", "group:1", "m1")
        self.assertEqual(first.id, loaded.id)  # 冲突时保留原 id
        self.assertEqual("hello again", loaded.text)
        self.assertEqual("阿特莉", loaded.sender_name)
        self.assertEqual("group", loaded.scope_type)
        self.assertIsNotNone(loaded.created_at)
        self.assertGreaterEqual(loaded.seq, 1)
        with self.assertRaises(NotFoundError):
            self.repository.get_by_platform_message("qqonebot", "group:1", "missing")

    def test_search_includes_media_only_rows_and_supports_filters(self) -> None:
        base = now()
        self.append("text", "hello world", at=base, sender="u1")
        self.append(
            "media",
            "",
            at=base + timedelta(minutes=1),
            segments=segments_json([MessageSegment(type=SegmentImage, name="sticker.jpg")]),
            sender="u1",
        )
        self.append("empty", "", at=base + timedelta(minutes=2), sender="u2")
        self.append("later", "hello there", at=base + timedelta(hours=3), sender="u2")

        found = self.repository.search(ChatHistorySearchRequest(platform="qqonebot", platform_scope_id="group:1"))
        self.assertEqual(["text", "media", "later"], [row.platform_message_id for row in found])

        or_mode = self.repository.search(
            ChatHistorySearchRequest(
                platform="qqonebot",
                platform_scope_id="group:1",
                query_terms=["hello", "there"],
                query_mode="or",
            )
        )
        self.assertEqual(["text", "later"], [row.platform_message_id for row in or_mode])

        and_mode = self.repository.search(
            ChatHistorySearchRequest(
                platform="qqonebot",
                platform_scope_id="group:1",
                query_terms=["hello", "there"],
                query_mode="and",
            )
        )
        self.assertEqual(["later"], [row.platform_message_id for row in and_mode])

        sender = self.repository.search(
            ChatHistorySearchRequest(platform="qqonebot", platform_scope_id="group:1", sender_id="u1")
        )
        self.assertEqual(["text", "media"], [row.platform_message_id for row in sender])

        by_name = self.repository.search(
            ChatHistorySearchRequest(platform="qqonebot", platform_scope_id="group:1", sender_name_query="阿特")
        )
        self.assertEqual(3, len(by_name))

        windowed = self.repository.search(
            ChatHistorySearchRequest(
                platform="qqonebot",
                platform_scope_id="group:1",
                since=base + timedelta(seconds=30),
                until=base + timedelta(hours=1),
            )
        )
        self.assertEqual(["media"], [row.platform_message_id for row in windowed])

        self.assertEqual([], self.repository.search(
            ChatHistorySearchRequest(platform="qqonebot", platform_scope_id="group:2")
        ))

    def test_search_limit_applies_to_the_newest_rows(self) -> None:
        for index in range(5):
            self.append(f"m{index}", f"text {index}")
        found = self.repository.search(
            ChatHistorySearchRequest(platform="qqonebot", platform_scope_id="group:1", limit=2)
        )
        self.assertEqual(["m3", "m4"], [row.platform_message_id for row in found])

    def test_around_returns_neighbours_in_seq_order(self) -> None:
        for message_id in ("m1", "m2", "m3", "m4", "m5"):
            self.append(message_id, f"text {message_id}")
        self.append("empty", "")
        window = self.repository.around(
            ChatHistoryAroundRequest(
                platform="qqonebot", platform_scope_id="group:1", platform_message_id="m3", before=1, after=2
            )
        )
        self.assertEqual(["m2", "m3", "m4", "m5"], [row.platform_message_id for row in window])
        only_target = self.repository.around(
            ChatHistoryAroundRequest(
                platform="qqonebot", platform_scope_id="group:1", platform_message_id="m1"
            )
        )
        self.assertEqual(["m1"], [row.platform_message_id for row in only_target])
        with self.assertRaises(NotFoundError):
            self.repository.around(
                ChatHistoryAroundRequest(
                    platform="qqonebot", platform_scope_id="group:1", platform_message_id="missing"
                )
            )

    def test_list_range_requires_scope_and_clamps_limit(self) -> None:
        base = now()
        for index in range(3):
            self.append(f"m{index}", f"text {index}", at=base + timedelta(minutes=index))
        self.assertIsInstance(self.repository, ChatHistoryRangeRepository)

        with self.assertRaises(InvalidError):
            self.repository.list_range(ChatHistoryRangeRequest(platform="", platform_scope_id="group:1"))
        with self.assertRaises(InvalidError):
            self.repository.list_range(ChatHistoryRangeRequest(platform="qqonebot", platform_scope_id=""))

        everything = self.repository.list_range(
            ChatHistoryRangeRequest(platform="qqonebot", platform_scope_id="group:1", limit=99999)
        )
        self.assertEqual(["m0", "m1", "m2"], [row.platform_message_id for row in everything])
        tail = self.repository.list_range(
            ChatHistoryRangeRequest(
                platform="qqonebot",
                platform_scope_id="group:1",
                after_seq=everything[0].seq,
                limit=10,
            )
        )
        self.assertEqual(["m1", "m2"], [row.platform_message_id for row in tail])
        windowed = self.repository.list_range(
            ChatHistoryRangeRequest(
                platform="qqonebot",
                platform_scope_id="group:1",
                since=base + timedelta(seconds=30),
                until=base + timedelta(minutes=1, seconds=30),
            )
        )
        self.assertEqual(["m1"], [row.platform_message_id for row in windowed])
        self.assertEqual(
            ["m0", "m1", "m2"],
            [
                row.platform_message_id
                for row in self.repository.list_range(
                    ChatHistoryRangeRequest(platform="qqonebot", platform_scope_id="group:1", limit=0)
                )
            ],
        )

    def test_delete_before_counts_removed_rows(self) -> None:
        base = now() - timedelta(days=10)
        self.append("old", "old text", at=base)
        self.append("new", "new text", at=now())
        self.assertEqual(1, self.repository.delete_before(now() - timedelta(days=1)))
        self.assertEqual(0, self.repository.delete_before(now() - timedelta(days=1)))
        self.assertEqual(
            ["new"],
            [
                row.platform_message_id
                for row in self.repository.search(
                    ChatHistorySearchRequest(platform="qqonebot", platform_scope_id="group:1")
                )
            ],
        )

    def test_outbound_messages_range_count_and_delete(self) -> None:
        self.assertIsInstance(self.outbound, OutboundMessageCounter)
        with self.assertRaises(InvalidError):
            self.outbound.list_range(OutboundMessageRangeRequest(platform="", platform_scope_id="group:1"))
        with self.assertRaises(InvalidError):
            self.outbound.count_range(OutboundMessageRangeRequest(platform="qqonebot", platform_scope_id=""))

        base = now()
        for index in range(3):
            self.outbound.append(
                OutboundMessage(
                    platform="qqonebot",
                    platform_scope_id="group:1",
                    platform_message_id=f"p{index}",
                    text=f"收到 {index}",
                    segments=segments_json([MessageSegment(type=SegmentImage, name=f"{index}.png")]),
                    created_at=base + timedelta(minutes=index),
                )
            )
        self.outbound.append(
            OutboundMessage(platform="qqonebot", platform_scope_id="group:2", text="别的群", created_at=base)
        )
        listed = self.outbound.list_range(
            OutboundMessageRangeRequest(platform="qqonebot", platform_scope_id="group:1", limit=10)
        )
        self.assertEqual(["收到 0", "收到 1", "收到 2"], [row.text for row in listed])
        self.assertEqual('[{"type":"image","name":"0.png"}]', listed[0].segments)
        self.assertEqual("p0", listed[0].platform_message_id)

        self.assertEqual(
            2,
            self.outbound.count_range(
                OutboundMessageRangeRequest(
                    platform="qqonebot",
                    platform_scope_id="group:1",
                    since=base + timedelta(seconds=30),
                )
            ),
        )
        self.assertEqual(
            1,
            len(
                self.outbound.list_range(
                    OutboundMessageRangeRequest(
                        platform="qqonebot", platform_scope_id="group:1", after_seq=listed[0].seq, limit=1
                    )
                )
            ),
        )

        # 同 id 覆盖：文本/回执列更新，seq 不变。
        again = OutboundMessage(
            id=listed[0].id,
            platform="qqonebot",
            platform_scope_id="group:1",
            platform_message_id="p0",
            text="改过了",
            created_at=base,
        )
        self.outbound.append(again)
        replayed = self.outbound.list_range(
            OutboundMessageRangeRequest(platform="qqonebot", platform_scope_id="group:1", limit=10)
        )
        self.assertEqual(3, len(replayed))
        self.assertEqual("改过了", replayed[0].text)
        self.assertEqual(listed[0].seq, replayed[0].seq)

        # 两条 base 时刻的行（group:1 的被覆盖行 + group:2 的那条）都过期。
        self.assertEqual(2, self.outbound.delete_before(base + timedelta(seconds=1)))
        self.assertEqual(
            2,
            self.outbound.count_range(
                OutboundMessageRangeRequest(platform="qqonebot", platform_scope_id="group:1")
            ),
        )


class TestForkBoundaries(SqliteRepositoryTestCase):
    def test_fork_session_sees_parent_messages_up_to_the_checkpoint(self) -> None:
        parent = Session(owner_id="u1", platform="cli", platform_scope_id="local", title="parent")
        self.store.sessions().create(parent)
        messages = []
        for content in ("first", "second", "third"):
            message = Message(session_id=parent.id, role=RoleUser, content=content)
            self.store.messages().append(message)
            messages.append(message)

        fork = Session(
            owner_id="u1",
            platform="cli",
            platform_scope_id="local",
            parent_session_id=parent.id,
            fork_from_message_id=messages[1].id,
        )
        self.store.sessions().create(fork)
        loaded = self.store.sessions().get(fork.id)
        self.assertEqual(parent.id, loaded.parent_session_id)
        self.assertEqual(messages[1].id, loaded.fork_from_message_id)

        visible = self.store.messages().list_by_session_up_to(parent.id, loaded.fork_from_message_id)
        self.assertEqual([messages[0].id, messages[1].id], [row.id for row in visible])
        inherited = self.store.messages().list_by_session_up_to(parent.id, loaded.fork_from_message_id)
        self.assertEqual(
            self.store.messages().list_by_session_after_up_to(parent.id, messages[0].id, messages[1].id),
            inherited[1:],
        )
        self.assertEqual([], self.store.messages().list_by_session(fork.id))

        # 父会话删除时 fork 的 parent_session_id 由外键置空（ON DELETE SET NULL）。
        self.store.messages().append(Message(session_id=fork.id, role=RoleAssistant, content="own"))
        self.store.sessions().delete(parent.id)
        orphaned = self.store.sessions().get(fork.id)
        self.assertEqual("", orphaned.parent_session_id)
        self.assertEqual(1, len(self.store.messages().list_by_session(fork.id)))
        self.assertEqual(
            1, len(self.store.sessions().list(ListSessionsRequest(include_all_platforms=True)))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
