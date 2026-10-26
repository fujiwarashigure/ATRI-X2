"""``atri_x.agent.time_context`` 的单元测试：注入幂等、剥旧段、入站时间基准。"""

from __future__ import annotations

import datetime as dt
import json
import unittest

from atri_x.agent.time_context import (
    TIME_BLOCK_MARKER,
    build_time_block,
    inbound_times,
    inject_time_block,
    merge_message_metadata,
    message_time_metadata,
    received_at_from_context,
    strip_time_block,
    with_inbound_times,
    with_platform_sent_at,
)
from atri_x.llm import LLMMessage, RoleAssistant, RoleSystem, RoleUser, segments_text_only, text_segments
from atri_x.time_service import TimeService

_FIXED = dt.datetime(2026, 3, 1, 1, 12, 33, tzinfo=dt.timezone.utc)


def _service(state: str = "disabled") -> TimeService:
    service = TimeService(
        "Asia/Shanghai", now_fn=lambda: _FIXED, monotonic_fn=lambda: 10.0
    )
    service.set_verification_state(state)
    return service


def _messages() -> list[LLMMessage]:
    return [
        LLMMessage(role=RoleSystem, segments=text_segments("you are a bot")),
        LLMMessage(role=RoleUser, segments=text_segments("hi")),
    ]


class BuildBlockTest(unittest.TestCase):
    def test_block_layout(self) -> None:
        block = build_time_block(_service(), {})
        lines = block.splitlines()
        self.assertEqual(lines[0], TIME_BLOCK_MARKER)
        self.assertTrue(lines[1].startswith("timezone=Asia/Shanghai (UTC+08:00)"))
        self.assertIn("current=2026-03-01T09:12:33+08:00", block)
        self.assertIn("source=system_clock verification=disabled", block)
        self.assertIn("note=", block)
        # 永远不允许出现"已校准/准确"这种结论
        self.assertNotIn("已校准\n", block)
        self.assertNotIn("accuracy_guaranteed", block)

    def test_conflict_state_is_spelled_out(self) -> None:
        block = build_time_block(_service("conflict"), {})
        self.assertIn("verification=conflict", block)
        self.assertIn("可能不准确", block)

    def test_received_at_and_platform_sent_at(self) -> None:
        received = _FIXED - dt.timedelta(seconds=35)
        ctx = with_inbound_times({}, received_at=received)
        ctx = with_platform_sent_at(ctx, received - dt.timedelta(seconds=2))
        block = build_time_block(_service(), ctx)
        self.assertIn("message_received_at=2026-03-01T09:11:58+08:00 (age=35s)", block)
        self.assertIn("platform_sent_at=", block)

    def test_stale_message_adds_ambiguity_note(self) -> None:
        received = _FIXED - dt.timedelta(minutes=10)
        ctx = with_inbound_times({}, received_at=received)
        block = build_time_block(_service(), ctx)
        self.assertIn("先向用户确认", block)

    def test_inject_instant_false_writes_timezone_only(self) -> None:
        block = build_time_block(_service(), {}, inject_instant=False)
        self.assertIn("current=unknown", block)
        self.assertIn("timezone=Asia/Shanghai", block)


class InboundContextTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        moment = _FIXED - dt.timedelta(seconds=5)
        ctx = with_inbound_times({}, received_at=moment)
        received, sent = inbound_times(ctx)
        self.assertEqual(received, moment)
        self.assertIsNone(sent)
        self.assertEqual(received_at_from_context(ctx), moment)

    def test_naive_rejected(self) -> None:
        with self.assertRaises(Exception):
            with_inbound_times({}, received_at=dt.datetime(2026, 1, 1))

    def test_metadata_merge_keeps_speakers(self) -> None:
        received = _FIXED - dt.timedelta(seconds=1)
        ctx = with_inbound_times({}, received_at=received)
        base = json.dumps({"speakers": [{"actor_id": "cli:local"}]})
        merged = json.loads(merge_message_metadata(base, ctx))
        self.assertIn("speakers", merged)
        self.assertEqual(
            merged["received_at"], received.astimezone(dt.timezone.utc).isoformat()
        )
        self.assertEqual(message_time_metadata(ctx)["received_at"], merged["received_at"])

    def test_metadata_merge_tolerates_non_json(self) -> None:
        self.assertEqual(merge_message_metadata("not-json", {}), "not-json")


class InjectionTest(unittest.TestCase):
    def test_inject_once_and_idempotent(self) -> None:
        service = _service()
        once = inject_time_block(_messages(), service, {})
        twice = inject_time_block(once, service, {})
        text = segments_text_only(twice[0].segments)
        self.assertEqual(text.count(TIME_BLOCK_MARKER), 1)
        self.assertIn("you are a bot", text)

    def test_input_messages_are_not_mutated(self) -> None:
        messages = _messages()
        original = segments_text_only(messages[0].segments)
        inject_time_block(messages, _service(), {})
        self.assertEqual(segments_text_only(messages[0].segments), original)

    def test_tool_round_does_not_accumulate(self) -> None:
        """模拟 chat_llm 的工具续轮：同一份 base_messages 调两次，仍只有一份片段。"""
        service = _service()
        base = _messages()
        first = inject_time_block(base, service, {})
        # 续轮里 base 可能已被其它路径改写；这里直接用第一轮结果再注入一次
        second = inject_time_block(first, service, {})
        self.assertEqual(segments_text_only(second[0].segments).count(TIME_BLOCK_MARKER), 1)

    def test_strip_removes_block(self) -> None:
        service = _service()
        injected = inject_time_block(_messages(), service, {})
        stripped = strip_time_block(injected)
        self.assertNotIn(TIME_BLOCK_MARKER, segments_text_only(stripped[0].segments))
        self.assertIn("you are a bot", segments_text_only(stripped[0].segments))

    def test_no_service_returns_input(self) -> None:
        messages = _messages()
        self.assertEqual(inject_time_block(messages, None, {}), messages)

    def test_disabled_still_strips_history(self) -> None:
        service = _service()
        injected = inject_time_block(_messages(), service, {})
        without = inject_time_block(injected, service, {}, enabled=True, inject_instant=False)
        self.assertEqual(segments_text_only(without[0].segments).count(TIME_BLOCK_MARKER), 1)
        self.assertIn("current=unknown", segments_text_only(without[0].segments))

    def test_assistant_messages_untouched(self) -> None:
        """没有系统消息时插入一条新的 system 消息，原有消息内容不变。"""
        messages = [LLMMessage(role=RoleAssistant, segments=text_segments("ok"))]
        injected = inject_time_block(messages, _service(), {})
        self.assertEqual(segments_text_only(injected[0].segments).splitlines()[0], TIME_BLOCK_MARKER)
        self.assertEqual(segments_text_only(injected[1].segments), "ok")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
