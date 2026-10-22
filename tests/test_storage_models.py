"""存储领域模型测试。

覆盖：ID 生成、RFC3339 编解码（含 Go 的纳秒/偏移量写法）、领域模型字段与
``internal/storage/sqlite/migrations.go`` 的列名一致性、segments JSON 往返、
repository Protocol 的方法名。
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import MessageSegment
from atri_x.storage import ids, times
from atri_x.storage.models import (
    AtrinisEvent,
    AtrinisReportDelivery,
    AtrinisReportDeliveryDelivered,
    AtrinisReportDeliveryFailed,
    AtrinisReportDeliveryPending,
    ChatMessage,
    ContextSummary,
    CronJob,
    HistoryMedia,
    Media,
    MediaOutput,
    MediaReference,
    Message,
    OutboundMessage,
    PlatformMessageMap,
    RoleAssistant,
    RoleSystem,
    RoleTool,
    RoleUser,
    Session,
    SessionModeChat,
    SessionModeWork,
    SessionStatusActive,
    SessionStatusClosed,
    SessionStatusPaused,
    ToolCallRecord,
    parse_segments,
    segments_json,
    session_mode_supports_tools,
)
from atri_x.storage.repositories import (
    AtrinisEventRepository,
    ChatHistoryRangeRepository,
    ChatHistoryRepository,
    ContextSummaryRepository,
    CronJobRepository,
    MediaReferenceRepository,
    MediaRepository,
    MessageRepository,
    OutboundMessageCounter,
    OutboundMessageRepository,
    SessionRepository,
    Store,
    ToolCallRepository,
)
from atri_x.storage.storage import ErrNotFound
from atri_x.storage.storage import Session as SessionFromReexport


def field_names(model: type) -> set[str]:
    return {item.name for item in dataclasses.fields(model)}


class TestNewID(unittest.TestCase):
    """对应 Go ``internal/storage/id.go``。"""

    def test_new_id_is_hyphenated_uuid4(self) -> None:
        # Go 的 NewID 用 "%08x-%04x-%04x-%04x-%012x" 输出，即带连字符的小写 UUIDv4。
        value = ids.new_id()
        self.assertEqual(36, len(value))
        self.assertTrue(
            re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                value,
            ),
            value,
        )
        parsed = ids.parse_id(value)
        self.assertEqual(4, parsed.version)
        # 变体位由上面的正则 [89ab] 覆盖（Python 3.14 的 UUID.variant 是 str，
        # 不是枚举，所以不在这里断言它的 name）。
        # 与标准库等价（Go 手工拼的位与 uuid4 一致）
        self.assertEqual(str(parsed), value)

    def test_is_valid_id(self) -> None:
        self.assertTrue(ids.is_valid_id(ids.new_id()))
        self.assertFalse(ids.is_valid_id("not-an-id"))
        self.assertFalse(ids.is_valid_id(""))

    def test_new_id_is_unique(self) -> None:
        values = {ids.new_id() for _ in range(200)}
        self.assertEqual(200, len(values))


class TestTimes(unittest.TestCase):
    """对应 Go ``internal/storage/time.go``。"""

    def test_format_time_uses_rfc3339_utc(self) -> None:
        moment = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual("2026-10-13T12:00:00Z", times.format_time(moment))

    def test_format_time_trims_fraction_zeros_like_rfc3339_nano(self) -> None:
        self.assertEqual(
            "2026-10-13T12:00:00.1Z",
            times.format_time(datetime(2026, 10, 13, 12, 0, 0, 100000, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            "2026-10-13T12:00:00.123456Z",
            times.format_time(datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=timezone.utc)),
        )

    def test_format_time_normalizes_offset_to_utc(self) -> None:
        moment = datetime(2026, 10, 13, 20, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual("2026-10-13T12:00:00Z", times.format_time(moment))

    def test_format_time_zero_value_is_empty_string(self) -> None:
        # Go 的 FormatTime 对零值时间返回空串。
        self.assertEqual("", times.format_time(None))
        self.assertEqual("", times.format_time(times.ZERO_TIME))
        self.assertTrue(times.is_zero_time(None))
        self.assertTrue(times.is_zero_time(times.ZERO_TIME))
        self.assertFalse(times.is_zero_time(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    def test_format_time_rejects_naive_datetime(self) -> None:
        with self.assertRaises(InvalidError):
            times.format_time(datetime(2026, 10, 13, 12, 0, 0))

    def test_parse_time_accepts_go_rfc3339_nano_forms(self) -> None:
        expected = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
        for text in (
            "2026-10-13T12:00:00Z",
            "2026-10-13T12:00:00.000000000Z",  # 9 位纳秒（Go 侧最常见）
            "2026-10-13T12:00:00.000Z",
            "2026-10-13T12:00:00,000Z",  # Go 的 Parse 接受逗号小数分隔符
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, times.parse_time(text))

    def test_parse_time_keeps_fraction_and_normalizes_offset(self) -> None:
        parsed = times.parse_time("2026-10-13T20:00:00.123456789+08:00")
        self.assertEqual(datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=timezone.utc), parsed)
        self.assertEqual(timezone.utc, parsed.tzinfo)

    def test_parse_time_truncates_beyond_microseconds(self) -> None:
        parsed = times.parse_time("2026-10-13T12:00:00.1234567Z")
        self.assertEqual(123456, parsed.microsecond)

    def test_parse_time_round_trip(self) -> None:
        for text in (
            "2026-10-13T12:00:00Z",
            "2026-10-13T12:00:00.5Z",
            "1999-01-01T00:00:00.000001Z",
        ):
            with self.subTest(text=text):
                self.assertEqual(text, times.format_time(times.parse_time(text)))

    def test_parse_time_rejects_bad_input(self) -> None:
        for text in ("", "2026-10-13", "2026-10-13T12:00:00", "not-a-time", "2026-13-01T00:00:00Z"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    times.parse_time(text)

    def test_parse_optional_time(self) -> None:
        self.assertIsNone(times.parse_optional_time(""))
        self.assertEqual(
            datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc),
            times.parse_optional_time("2026-10-13T12:00:00Z"),
        )
        with self.assertRaises(ValueError):
            times.parse_optional_time("2026-10-13")

    def test_now_is_utc_aware(self) -> None:
        moment = times.now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(timedelta(0), moment.utcoffset())


class TestModelsMatchMigrations(unittest.TestCase):
    """领域模型字段必须覆盖 ``migrations.go``（以及 chat_history 建表）里的列。"""

    def assert_columns(self, model: type, *columns: str) -> None:
        available = field_names(model)
        missing = [column for column in columns if column not in available]
        self.assertEqual([], missing, f"{model.__name__} 缺少列：{missing}")

    def test_sessions_columns(self) -> None:
        self.assert_columns(
            Session,
            "id",
            "parent_session_id",
            "fork_from_message_id",
            "owner_id",
            "platform",
            "platform_scope_id",
            "mode",
            "title",
            "status",
            "metadata",
            "created_at",
            "updated_at",
            "archived_at",
            "pinned_at",
        )

    def test_messages_columns(self) -> None:
        self.assert_columns(
            Message,
            "id",
            "session_id",
            "role",
            "content",
            "parent_message_id",
            "reply_to_platform_message_id",
            "reply_to_message_id",
            "tool_call_id",
            "metadata",
            "created_at",
            "segments",  # v11
        )

    def test_context_summaries_columns(self) -> None:
        self.assert_columns(
            ContextSummary,
            "id",
            "session_id",
            "from_message_id",
            "to_message_id",
            "summary",
            "provider",
            "model",
            "source_tokens",
            "summary_tokens",
            "total_tokens",
            "cache_hit_tokens",
            "trigger_reason",
            "metadata",
            "created_at",
        )

    def test_tool_call_records_columns(self) -> None:
        self.assert_columns(
            ToolCallRecord,
            "id",
            "session_id",
            "tool_call_id",
            "tool_name",
            "actor_id",
            "risk_level",
            "success",
            "error",
            "result_preview",
            "started_at",
            "finished_at",
            "created_at",
        )

    def test_cron_jobs_columns(self) -> None:
        self.assert_columns(
            CronJob,
            "id",
            "name",
            "handler",
            "schedule",
            "enabled",
            "metadata",
            "last_run_at",
            "next_run_at",
            "run_count",
            "last_error",
            "created_at",
            "updated_at",
            "delivery_state",  # v10
            "delivery_token",  # v10
        )

    def test_atrinis_events_columns(self) -> None:
        self.assert_columns(
            AtrinisEvent,
            "id",
            "event_key",
            "token_name",
            "atriwisp_name",
            "source",
            "source_id",
            "tags",
            "mode",
            "model_slot",
            "content_hash",
            "requested_targets",
            "resolved_targets",
            "status",
            "session_id",
            "result",
            "error",
            "received_at",
            "created_at",
            "updated_at",
            "tool_declarations",  # v8
            "tool_hash",  # v8
        )

    def test_atrinis_report_deliveries_columns(self) -> None:
        self.assert_columns(
            AtrinisReportDelivery,
            "id",
            "event_id",
            "ordinal",
            "target",
            "output",
            "message_id",
            "status",
            "receipt",
            "error",
            "attempts",
            "created_at",
            "updated_at",
        )

    def test_media_columns(self) -> None:
        self.assert_columns(
            Media,
            "id",
            "name",
            "mime_type",
            "size",
            "local_path",
            "backend",
            "object_key",
            "source_platform",
            "source_url",
            "source_file_id",
            "created_at",
            "last_accessed_at",
            "expires_at",
            "deleting",  # v14
        )
        # v14 的 orphaned_at 由 SQLite 触发器维护，Go 领域模型不含该字段，本仓同样不暴露。
        self.assertNotIn("orphaned_at", field_names(Media))

    def test_media_references_columns(self) -> None:
        self.assert_columns(
            MediaReference,
            "media_id",
            "owner_type",
            "owner_id",
            "purpose",
            "session_id",
            "created_at",
        )

    def test_media_outputs_columns(self) -> None:
        self.assert_columns(
            MediaOutput,
            "platform",
            "scope_id",
            "message_id",
            "segment_index",
            "kind",
            "media_id",
            "owner_id",
            "expires_at",
        )

    def test_media_history_columns(self) -> None:
        self.assert_columns(
            HistoryMedia,
            "history_id",
            "platform",
            "scope_id",
            "message_id",
            "media_index",
            "kind",
            "media_id",
            "owner_id",
        )

    def test_platform_message_map_columns(self) -> None:
        self.assert_columns(
            PlatformMessageMap,
            "id",
            "platform",
            "platform_scope_id",
            "platform_message_id",
            "message_id",
            "session_id",
            "created_at",
        )

    def test_chat_messages_columns(self) -> None:
        self.assert_columns(
            ChatMessage,
            "seq",
            "id",
            "platform",
            "platform_scope_id",
            "scope_type",
            "platform_message_id",
            "sender_id",
            "sender_name",
            "text",
            "raw",
            "segments",
            "reply_to_platform_message_id",
            "metadata",
            "created_at",
        )

    def test_outbound_messages_columns(self) -> None:
        self.assert_columns(
            OutboundMessage,
            "seq",
            "id",
            "platform",
            "platform_scope_id",
            "platform_message_id",
            "text",
            "segments",
            "created_at",
        )

    def test_nullable_columns_default_to_null_semantics(self) -> None:
        message = Message(session_id="s1", role=RoleUser, content="hi")
        self.assertEqual("", message.segments)  # 空串 = NULL = 无多模态正文
        self.assertEqual("", message.metadata)
        self.assertIsNone(message.created_at)  # None = Go 零值时间
        session = Session(owner_id="u1", platform="cli", platform_scope_id="c1")
        self.assertIsNone(session.archived_at)
        self.assertIsNone(session.pinned_at)
        self.assertFalse(Media().deleting)
        self.assertFalse(CronJob().enabled)
        self.assertFalse(ToolCallRecord().success)


class TestConstants(unittest.TestCase):
    def test_session_constants_match_go(self) -> None:
        self.assertEqual("work", SessionModeWork)
        self.assertEqual("chat", SessionModeChat)
        self.assertEqual("active", SessionStatusActive)
        self.assertEqual("paused", SessionStatusPaused)
        self.assertEqual("closed", SessionStatusClosed)

    def test_role_constants_match_go(self) -> None:
        self.assertEqual("system", RoleSystem)
        self.assertEqual("user", RoleUser)
        self.assertEqual("assistant", RoleAssistant)
        self.assertEqual("tool", RoleTool)

    def test_session_mode_supports_tools(self) -> None:
        # 本仓有意偏离上游：work / chat 都支持工具；未知模式不支持（安全默认）。
        self.assertTrue(session_mode_supports_tools(SessionModeWork))
        self.assertTrue(session_mode_supports_tools(SessionModeChat))
        self.assertTrue(session_mode_supports_tools(" WORK "))
        self.assertFalse(session_mode_supports_tools("background"))
        self.assertFalse(session_mode_supports_tools(""))

    def test_atrinis_delivery_constants_match_go(self) -> None:
        self.assertEqual("pending", AtrinisReportDeliveryPending)
        self.assertEqual("failed", AtrinisReportDeliveryFailed)
        self.assertEqual("delivered", AtrinisReportDeliveryDelivered)


class TestSegmentsJson(unittest.TestCase):
    def test_round_trip(self) -> None:
        segments = [
            MessageSegment(type="text", text="看图"),
            MessageSegment(type="image", media="abc123", mime_type="image/png", name="cat.png"),
        ]
        raw = segments_json(segments)
        self.assertEqual(
            '[{"type":"text","text":"看图"},'
            '{"type":"image","media":"abc123","mime_type":"image/png","name":"cat.png"}]',
            raw,
        )
        parsed = parse_segments(raw)
        self.assertEqual(segments, parsed)

    def test_empty_is_null_semantics(self) -> None:
        self.assertEqual("", segments_json([]))
        self.assertEqual("", segments_json(None))
        self.assertEqual([], parse_segments(""))
        self.assertEqual([], parse_segments("   "))
        self.assertEqual([], parse_segments("not json"))
        self.assertEqual([], parse_segments('{"type":"text"}'))


class TestRepositoryProtocols(unittest.TestCase):
    """repository 抽象的方法名要能按 Go 名字交叉检索（snake_case）。"""

    def test_session_repository_methods(self) -> None:
        for name in ("create", "get", "update", "mutate", "list", "delete", "delete_expired"):
            self.assertTrue(hasattr(SessionRepository, name), name)

    def test_message_repository_methods(self) -> None:
        for name in (
            "append",
            "get",
            "list_by_session",
            "list_by_session_up_to",
            "list_by_session_after",
            "list_by_session_after_up_to",
            "map_platform_message",
            "find_by_platform_message",
        ):
            self.assertTrue(hasattr(MessageRepository, name), name)

    def test_other_repository_methods(self) -> None:
        expectations = {
            MediaRepository: ("save_history", "find_history", "list_history", "delete_history", "upsert"),
            MediaReferenceRepository: ("add", "add_all", "remove", "list_by_owner", "list_media_ids"),
            ToolCallRepository: ("create", "successful_ids", "usage_by_session"),
            CronJobRepository: (
                "upsert",
                "get_by_name",
                "list",
                "list_enabled",
                "update_next_run_at",
                "update_run_state",
                "compare_and_swap_delivery",
                "disable_by_name",
                "disable_by_name_if_delivery_token",
                "delete_by_name",
            ),
            AtrinisEventRepository: (
                "create",
                "get",
                "get_by_key",
                "update",
                "prepare_report",
                "reset_delivering_reports",
                "list_result_ready_report_ids",
                "claim_report",
                "release_report",
                "list_report_deliveries",
                "start_report_delivery",
                "mark_report_delivery_failed",
                "mark_report_delivery_delivered",
                "complete_report",
            ),
            ChatHistoryRepository: (
                "append",
                "get_by_platform_message",
                "search",
                "around",
                "delete_before",
            ),
            ChatHistoryRangeRepository: ("list_range",),
            OutboundMessageRepository: ("append", "list_range", "delete_before"),
            OutboundMessageCounter: ("count_range",),
            ContextSummaryRepository: ("create", "latest_by_session", "latest_by_session_up_to"),
            Store: (
                "sessions",
                "messages",
                "media",
                "media_references",
                "context_summaries",
                "tool_calls",
                "cron_jobs",
                "atrinis_events",
                "close",
            ),
        }
        for protocol, names in expectations.items():
            for name in names:
                with self.subTest(protocol=protocol.__name__, method=name):
                    self.assertTrue(hasattr(protocol, name), f"{protocol.__name__}.{name}")

    def test_storage_module_reexports_contract(self) -> None:
        # PORTING.md §3 把 atri_x.storage.storage 定为共享契约路径；拆分成
        # models/repositories 之后必须仍然能从这里 import。
        self.assertIs(SessionFromReexport, Session)
        self.assertIsInstance(ErrNotFound, NotFoundError)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
