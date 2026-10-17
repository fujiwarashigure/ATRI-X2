"""``atri_x.hook`` 核心类型的单元测试。

对应上游 ``internal/hook/event.go``。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from atri_x.hook import event as ev
from atri_x.llm.llm import LLMMessage, MessageSegment, SegmentType, ToolCallRequest


class TestPoints(unittest.TestCase):
    def test_known_points(self) -> None:
        for point in ev.Point.ALL:
            self.assertTrue(ev.known_point(point), point)

    def test_point_values_match_upstream_wire_format(self) -> None:
        # 这些字符串是 Hook 配置里的字面量，不能改
        self.assertEqual("platform.connected", ev.Point.PLATFORM_CONNECTED)
        self.assertEqual("agent.input.prepared", ev.Point.AGENT_INPUT_PREPARED)
        self.assertEqual("llm.turn.prepared", ev.Point.LLM_TURN_PREPARED)
        self.assertEqual("llm.request.prepared", ev.Point.LLM_REQUEST_PREPARED)
        self.assertEqual("tool.call.completed", ev.Point.TOOL_CALL_COMPLETED)
        self.assertEqual("agent.output.prepared", ev.Point.AGENT_OUTPUT_PREPARED)
        self.assertEqual("agent.turn.output.prepared", ev.Point.AGENT_TURN_OUTPUT_PREPARED)
        self.assertEqual("error.occurred", ev.Point.ERROR_OCCURRED)

    def test_unknown_point(self) -> None:
        self.assertFalse(ev.known_point("nope"))
        self.assertFalse(ev.known_point(""))

    def test_frame_limit(self) -> None:
        self.assertEqual(16 * 1024 * 1024, ev.MAX_PROTOCOL_FRAME_BYTES)


class TestPrepareEvent(unittest.TestCase):
    def test_fills_time_and_metadata(self) -> None:
        event = ev.Event(point=ev.Point.ERROR_OCCURRED)
        before = datetime.now(timezone.utc)
        ev.prepare_event(event)
        self.assertIsNotNone(event.time)
        self.assertEqual(timezone.utc, event.time.tzinfo)  # type: ignore[union-attr]
        self.assertGreaterEqual(event.time, before)  # type: ignore[operator]
        self.assertEqual({}, event.metadata)

    def test_derives_error_info(self) -> None:
        event = ev.Event(error=ValueError("boom"))
        ev.prepare_event(event)
        self.assertIsNotNone(event.error_info)
        self.assertEqual("boom", event.error_info.message)  # type: ignore[union-attr]
        self.assertEqual("boom", event.error_message())

    def test_keeps_explicit_error_info(self) -> None:
        event = ev.Event(error=ValueError("boom"), error_info=ev.ErrorPayload(message="given"))
        ev.prepare_event(event)
        self.assertEqual("given", event.error_message())

    def test_error_message_empty(self) -> None:
        self.assertEqual("", ev.Event().error_message())


class TestMessageIntentText(unittest.TestCase):
    def test_prefers_intent_text(self) -> None:
        event = ev.Event(
            message=ev.MessagePayload(
                intent_text="  明确意图  ",
                segments=[MessageSegment(type=SegmentType.TEXT, text="原始文本")],
            )
        )
        self.assertEqual("明确意图", event.message_intent_text())

    def test_falls_back_to_segments_text(self) -> None:
        event = ev.Event(
            message=ev.MessagePayload(
                segments=[
                    MessageSegment(type=SegmentType.TEXT, text="前半"),
                    MessageSegment(type=SegmentType.IMAGE, media="m1"),
                    MessageSegment(type=SegmentType.TEXT, text="后半"),
                ]
            )
        )
        self.assertEqual("前半后半", event.message_intent_text())

    def test_empty_when_nothing(self) -> None:
        self.assertEqual("", ev.Event().message_intent_text())

    def test_whitespace_only_intent_text_falls_back(self) -> None:
        event = ev.Event(
            message=ev.MessagePayload(
                intent_text="   ",
                segments=[MessageSegment(type=SegmentType.TEXT, text=" fallback ")],
            )
        )
        self.assertEqual("fallback", event.message_intent_text())


class TestClone(unittest.TestCase):
    def _sample(self) -> ev.Event:
        return ev.Event(
            id="e1",
            point=ev.Point.LLM_TURN_PREPARED,
            metadata={"a": 1},
            message=ev.MessagePayload(
                id="m1",
                segments=[MessageSegment(type=SegmentType.TEXT, text="hi")],
                messages=[
                    LLMMessage(
                        role="user",
                        segments=[MessageSegment(type=SegmentType.TEXT, text="hi")],
                        tool_calls=[ToolCallRequest(id="t1", name="x", arguments="{}")],
                    )
                ],
                reply=ev.MessageReplyPayload(message_id="r1", segments=[MessageSegment()]),
            ),
            llm=ev.LLMPayload(
                provider="p",
                model="m",
                messages=[LLMMessage(role="user")],
                tool_calls=[ToolCallRequest(id="t2")],
                system_append=["s"],
                extra_body={"k": "v"},
            ),
        )

    def test_clone_isolates_nested_state(self) -> None:
        original = self._sample()
        copy = original.clone()

        copy.metadata["a"] = 99
        copy.message.segments[0].text = "changed"
        copy.message.messages[0].segments[0].text = "changed"
        copy.message.messages[0].tool_calls[0].name = "changed"
        copy.message.reply.segments[0].text = "changed"  # type: ignore[union-attr]
        copy.llm.system_append.append("extra")
        copy.llm.extra_body["k"] = "changed"
        copy.llm.tool_calls[0].id = "changed"
        copy.control.consume = True

        self.assertEqual(1, original.metadata["a"])
        self.assertEqual("hi", original.message.segments[0].text)
        self.assertEqual("hi", original.message.messages[0].segments[0].text)
        self.assertEqual("x", original.message.messages[0].tool_calls[0].name)
        self.assertEqual("", original.message.reply.segments[0].text)  # type: ignore[union-attr]
        self.assertEqual(["s"], original.llm.system_append)
        self.assertEqual("v", original.llm.extra_body["k"])
        self.assertEqual("t2", original.llm.tool_calls[0].id)
        self.assertFalse(original.control.consume)

    def test_clone_keeps_scalars(self) -> None:
        original = self._sample()
        copy = original.clone()
        self.assertEqual(original.id, copy.id)
        self.assertEqual(original.point, copy.point)
        self.assertEqual(original.llm.provider, copy.llm.provider)


class TestHandlerFunc(unittest.TestCase):
    def test_sync_handler(self) -> None:
        async def run() -> None:
            handler = ev.HandlerFunc(lambda ctx, e: e)
            out = await handler.handle_hook(None, ev.Event(id="x"))
            self.assertEqual("x", out.id)

        asyncio.run(run())

    def test_async_handler(self) -> None:
        async def fn(ctx: object, e: ev.Event) -> ev.Event:
            e.id = "async"
            return e

        async def run() -> None:
            handler = ev.HandlerFunc(fn)
            out = await handler.handle_hook(None, ev.Event(id="x"))
            self.assertEqual("async", out.id)

        asyncio.run(run())

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(ev.HandlerFunc(lambda c, e: e), ev.Handler)


if __name__ == "__main__":
    unittest.main()
