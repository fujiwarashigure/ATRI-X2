"""``atri_x.hook.protocol`` 的单元测试。

对应上游 ``internal/hook/protocol/protocol.go`` 与 ``event.go`` 的 struct tag
投影。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from atri_x.delivery.delivery import Output, Source, Target as DeliveryTarget
from atri_x.hook.event import Control, Event, Point
from atri_x.hook.match import MatchContext, RegexMatch
from atri_x.hook.output import MessageSegment as WireMessageSegment
from atri_x.hook.protocol import (
    EventHandleParams,
    EventResultBase,
    Frame,
    LLMResult,
    LineReader,
    MessageResult,
    apply_llm_result,
    decode_frame,
    encode_frame,
    event_from_json,
    event_to_json,
    match_context_to_json,
    new_request,
    output_to_json,
    time_to_json,
    validate_id,
)
from atri_x.llm.llm import LLMMessage, MessageSegment, ToolCallRequest, Usage
from atri_x.hook.output import Segment, Target


class TestTimeJSON(unittest.TestCase):
    def test_utc(self) -> None:
        moment = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual("2026-10-13T12:00:00Z", time_to_json(moment))

    def test_microseconds_trimmed(self) -> None:
        moment = datetime(2026, 10, 13, 12, 0, 0, 100000, tzinfo=timezone.utc)
        self.assertEqual("2026-10-13T12:00:00.1Z", time_to_json(moment))

    def test_zero_value(self) -> None:
        self.assertEqual("0001-01-01T00:00:00Z", time_to_json(None))


class TestFrame(unittest.TestCase):
    def test_request_frame_omitempty(self) -> None:
        frame = new_request("host:1", "system.init", {"version": "hook.v2"})
        data = json.loads(encode_frame(frame).decode("utf-8"))
        self.assertEqual("request", data["type"])
        self.assertEqual("host:1", data["id"])
        self.assertEqual("system.init", data["method"])
        self.assertEqual({"version": "hook.v2"}, data["params"])
        self.assertNotIn("ok", data)
        self.assertNotIn("result", data)
        self.assertNotIn("error", data)

    def test_response_ok_false(self) -> None:
        frame = Frame(type="response", id="plugin:1", ok=False, error="boom")
        data = json.loads(encode_frame(frame).decode("utf-8"))
        self.assertIs(False, data["ok"])
        self.assertEqual("boom", data["error"])

    def test_decode_ignores_unknown_fields(self) -> None:
        frame = decode_frame('{"type":"event","method":"hook.log","extra":1}')
        self.assertEqual("event", frame.type)
        self.assertEqual("hook.log", frame.method)

    def test_decode_rejects_non_object(self) -> None:
        with self.assertRaises(ValueError):
            decode_frame("[1,2,3]")

    def test_validate_id(self) -> None:
        validate_id("host:abc", "host:")
        with self.assertRaises(ValueError):
            validate_id("plugin:abc", "host:")


class TestEventJSON(unittest.TestCase):
    def make_event(self) -> Event:
        event = Event(
            id="e1",
            point=Point.AGENT_INPUT_PREPARED,
            time=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        event.platform.name = "cli"
        event.platform.scope_id = "s1"
        event.actor.id = "cli:u1"
        event.actor.user_id = "u1"
        event.message.id = "m1"
        event.message.segments = [MessageSegment(type="text", text="hello")]
        event.llm.provider = "openai"
        event.llm.model = "gpt-x"
        event.llm.usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        event.llm.tool_calls = [ToolCallRequest(id="c1", name="t", arguments="{}")]
        event.tool.id = "t1"
        event.tool.name = "shell"
        return event

    def test_required_keys_present(self) -> None:
        data = event_to_json(self.make_event())
        for key in (
            "id",
            "point",
            "time",
            "control",
            "platform",
            "actor",
            "session",
            "request",
            "message",
            "llm",
            "tool",
        ):
            self.assertIn(key, data)
        self.assertEqual({"consume": False, "stop_propagation": False}, data["control"])
        self.assertEqual(
            {
                "name": "cli",
                "scope_id": "s1",
                "user_id": "",
                "conversation_id": "",
                "message_id": "",
                "reply_to_message_id": "",
            },
            data["platform"],
        )

    def test_omitempty_fields(self) -> None:
        data = event_to_json(self.make_event())
        self.assertNotIn("metadata", data)
        self.assertNotIn("outputs", data)
        self.assertNotIn("source_text", data)
        self.assertNotIn("text", data)
        self.assertNotIn("risk", data)
        self.assertEqual(
            {"type": "text", "text": "hello"}, data["message"]["segments"][0]
        )

    def test_llm_keys_follow_go_names(self) -> None:
        data = event_to_json(self.make_event())
        self.assertEqual(
            {
                "PromptTokens": 1,
                "CompletionTokens": 2,
                "TotalTokens": 3,
                "CacheHitTokens": 0,
            },
            data["llm"]["usage"],
        )
        self.assertEqual(
            [{"ID": "c1", "Name": "t", "Arguments": "{}"}], data["llm"]["tool_calls"]
        )

    def test_outputs_projection(self) -> None:
        event = self.make_event()
        event.outputs.append(
            Output(
                kind="image",
                text="cap",
                source=Source(media_id="media:" + "c" * 64),
                target=DeliveryTarget(platform="cli", scope_id="s"),
            )
        )
        data = event_to_json(event)
        out = data["outputs"][0]
        self.assertEqual("image", out["Kind"])
        self.assertEqual("media:" + "c" * 64, out["Source"]["media"])
        self.assertEqual("cli", out["Target"]["Platform"])
        self.assertIsNone(out["Meta"])

    def test_tool_error_is_empty_object(self) -> None:
        event = self.make_event()
        event.tool.error = RuntimeError("boom")
        data = event_to_json(event)
        self.assertEqual({}, data["tool"]["error"])

    def test_match_metadata_is_serializable(self) -> None:
        event = self.make_event()
        event.metadata["match"] = MatchContext(
            regex=[RegexMatch(field="message.text", value="a", text="a", groups=["a"])]
        )
        data = event_to_json(event)
        self.assertEqual("a", data["metadata"]["match"]["regex"][0]["text"])

    def test_round_trip(self) -> None:
        original = self.make_event()
        restored = event_from_json(json.loads(json.dumps(event_to_json(original))))
        self.assertEqual(original.id, restored.id)
        self.assertEqual(original.point, restored.point)
        self.assertEqual("hello", restored.message.segments[0].text)
        self.assertEqual("u1", restored.actor.user_id)
        self.assertEqual(3, restored.llm.usage.total_tokens)
        self.assertEqual(original.time, restored.time)

    def test_match_context_omitempty(self) -> None:
        self.assertEqual({}, match_context_to_json(MatchContext()))


class TestEventParamsAndResults(unittest.TestCase):
    def test_event_handle_params(self) -> None:
        params = EventHandleParams(event=Event(id="e", point=Point.ERROR_OCCURRED))
        data = params.to_json()
        self.assertEqual("e", data["event"]["id"])
        self.assertEqual({}, data["match"])

    def test_result_base_keys(self) -> None:
        result = EventResultBase(
            status="completed",
            outputs=[Segment(kind="text", text="x")],
            target=Target(platform="cli"),
        )
        data = result.to_json()
        self.assertEqual("completed", data["status"])
        self.assertEqual({"platform": "cli"}, data["target"])
        self.assertEqual("text", data["outputs"][0]["kind"])

    def test_result_strict_unknown_field(self) -> None:
        with self.assertRaises(Exception):
            EventResultBase.from_object({"status": "completed", "nope": 1})

    def test_result_with_message_and_llm(self) -> None:
        result = EventResultBase.from_object(
            {
                "status": "waiting",
                "outputs": [],
                "target": {},
                "pass_through": True,
                "message": {"text": "hi"},
                "llm": {"system_append": ["a"], "temperature": 0.5},
            }
        )
        self.assertEqual("waiting", result.status)
        self.assertIs(True, result.pass_through)
        self.assertEqual("hi", result.message.text)
        self.assertEqual(["a"], result.llm.system_append)
        self.assertEqual(0.5, result.llm.temperature)

    def test_message_result_strict(self) -> None:
        with self.assertRaises(Exception):
            MessageResult.from_object({"text": "a", "bogus": 1})

    def test_llm_result_null(self) -> None:
        self.assertIsNone(LLMResult.from_object(None))


class TestApplyLLMResult(unittest.TestCase):
    def test_merges(self) -> None:
        event = Event()
        event.llm.extra_body = {"keep": 1}
        result = LLMResult(
            system_append=["a", "  ", "b"],
            temperature=0.3,
            max_tokens=64,
            extra_body={"x": 2},
        )
        apply_llm_result(event, result)
        self.assertEqual(["a", "b"], event.llm.system_append)
        self.assertEqual(0.3, event.llm.temperature)
        self.assertEqual(64, event.llm.max_tokens)
        self.assertEqual({"keep": 1, "x": 2}, event.llm.extra_body)

    def test_none_is_noop(self) -> None:
        event = Event()
        self.assertIs(event, apply_llm_result(event, None))


class FakeStream:
    """把预置字节按块吐出的假流（模拟 ``asyncio.StreamReader``）。"""

    def __init__(self, data: bytes, chunk: int = 3) -> None:
        self._data = data
        self._chunk = chunk
        self._pos = 0

    async def read(self, _n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos : self._pos + self._chunk]
        self._pos += len(chunk)
        return chunk


class TestLineReader(unittest.TestCase):
    def read_all(self, data: bytes, chunk: int = 3) -> list[bytes | None]:
        async def run() -> list[bytes | None]:
            reader = LineReader(FakeStream(data, chunk))
            lines: list[bytes | None] = []
            while True:
                line = await reader.read_line()
                if line is None:
                    break
                lines.append(line)
            return lines

        return asyncio.run(run())

    def test_splits_lines(self) -> None:
        self.assertEqual(
            [b"a\n", b"bb\n", b"ccc"],
            self.read_all(b"a\nbb\nccc"),
        )

    def test_partial_line_at_eof(self) -> None:
        self.assertEqual([b"tail"], self.read_all(b"tail"))

    def test_empty_stream(self) -> None:
        self.assertEqual([], self.read_all(b""))

    def test_oversize_frame(self) -> None:
        async def run() -> None:
            reader = LineReader(FakeStream(b"x" * 50), max_bytes=10)
            await reader.read_line()

        with self.assertRaises(Exception) as ctx:
            asyncio.run(run())
        self.assertIn("exceeds", str(ctx.exception))


class TestWireSegmentCompatibility(unittest.TestCase):
    def test_wire_and_llm_segment_names(self) -> None:
        # 进程 Hook 的 wire 段与 llm 段字段名不同（media vs media_id 的历史差异）。
        wire = WireMessageSegment.from_json('{"type":"image","path":"media:' + "d" * 64 + '"}')
        self.assertEqual("image", wire.type)
        self.assertEqual(
            {"type": "image", "path": "media:" + "d" * 64}, wire.to_json()
        )
        llm_segment = MessageSegment(type="image", media="media:" + "d" * 64)
        self.assertEqual("media:" + "d" * 64, llm_segment.to_json()["media"])
        self.assertEqual(
            "hello", LLMMessage(segments=[MessageSegment(text="hello")]).content_text()
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
