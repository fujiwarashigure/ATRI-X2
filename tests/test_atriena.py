"""``atri_x.atriena`` 的单元测试。

覆盖协议类型的 JSON 口径、来源校验、平台调用映射（capability → 原生 API）、
目标/段的投递换算，以及分发总线。全部用标准库 ``unittest``。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.atriena import (
    Bus,
    Call,
    CallExecutionError,
    CallKindCapability,
    CallResult,
    CapabilityChatLeave,
    CapabilityMemberMute,
    CapabilityMessageRecall,
    DirectText,
    DispatcherNotConfiguredMessage,
    ModeRecord,
    Origin,
    OriginHook,
    OriginHTTPToken,
    OriginInternal,
    Request,
    Segment,
    SegmentKindFile,
    SegmentKindImage,
    SegmentKindText,
    StatusCompleted,
    Target,
    TargetScopeID,
    VersionV2,
    VersionV3,
    build_direct_outputs,
    execute_call,
    execute_calls,
    map_chat_leave,
    map_member_mute,
    map_message_recall,
    resolve_raw_call,
    segment_key,
    segments_outputs,
    target_scope_id,
    to_int64,
)
from atri_x.delivery import Kind as DeliveryKind
from atri_x.errors import AtriXError, InvalidError
from atri_x.media import ValidID

MEDIA_ID = "media:" + "a" * 64


class TestRequestWire(unittest.TestCase):
    """Request 的 JSON 键名与 omitempty 口径。"""

    def test_to_json_uses_go_tag_names(self) -> None:
        req = Request(
            version=VersionV2,
            source="src",
            id="evt-1",
            mode=ModeRecord,
            content="hello",
            tool_list_names=["shell"],
            targets=[Target(platform="cli")],
        )
        payload = req.to_json()
        self.assertEqual(payload["version"], "atriena.v2")
        self.assertEqual(payload["model_slot"], "")
        self.assertEqual(payload["session_mode"], "")
        self.assertEqual(payload["tool_list_names"], ["shell"])
        self.assertEqual(payload["targets"], [{"platform": "cli"}])
        # omitempty：空 segments / calls 整个键不出现。
        self.assertNotIn("segments", payload)
        self.assertNotIn("calls", payload)
        # 无 omitempty 的切片字段 nil → null（Go 的 nil 切片）。
        self.assertIsNone(payload["tools"])
        self.assertIsNone(payload["meta"])

    def test_to_json_keeps_empty_slice_as_array(self) -> None:
        req = Request(targets=[], tool_list_names=[])
        payload = req.to_json()
        self.assertEqual(payload["targets"], [])
        self.assertEqual(payload["tool_list_names"], [])

    def test_segment_omitempty(self) -> None:
        seg = Segment(kind=SegmentKindImage, url="chart.png")
        self.assertEqual(seg.to_json(), {"kind": "image", "url": "chart.png"})
        self.assertEqual(Segment(kind=SegmentKindText, text="hi").to_json(), {"kind": "text", "text": "hi"})

    def test_target_omitempty(self) -> None:
        self.assertEqual(Target(platform="telegram").to_json(), {"platform": "telegram"})
        self.assertEqual(
            Target(platform="telegram", type="private", id="1").to_json(),
            {"platform": "telegram", "type": "private", "id": "1"},
        )

    def test_round_trip_ignores_unknown_fields(self) -> None:
        raw = {
            "version": VersionV3,
            "atriwisp": {"name": "watcher", "tags": ["t"]},
            "source": "src",
            "id": "evt-1",
            "mode": "record",
            "targets": [{"platform": "cli"}],
            "future_field": {"enabled": True},
        }
        req = Request.from_json(raw)
        self.assertEqual(req.version, VersionV3)
        self.assertEqual(req.atriwisp.name, "watcher")
        self.assertEqual(req.atriwisp.tags, ["t"])
        self.assertEqual([target.platform for target in req.targets or []], ["cli"])
        self.assertIsNone(req.segments)
        self.assertIsNone(req.calls)

    def test_from_json_rejects_type_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            Request.from_json({"content": 42})
        with self.assertRaises(ValueError):
            Request.from_json({"targets": {"platform": "cli"}})
        with self.assertRaises(ValueError):
            Request.from_json({"meta": []})
        with self.assertRaises(ValueError):
            Request.from_json([])

    def test_from_json_parses_segments_and_calls(self) -> None:
        raw = {
            "segments": [{"kind": "image", "url": MEDIA_ID, "mime_type": "image/png"}],
            "calls": [{"kind": "raw", "api": "delete_msg", "params": {"message_id": 1}}],
        }
        req = Request.from_json(raw)
        self.assertEqual(len(req.segments or []), 1)
        self.assertEqual(req.segments[0].mime_type, "image/png")  # type: ignore[index]
        self.assertEqual(req.calls[0].api, "delete_msg")  # type: ignore[index]

    def test_tool_declaration_wire(self) -> None:
        raw = {
            "version": VersionV2,
            "tools": [
                {
                    "name": "server_status",
                    "description": "d",
                    "endpoint": "http://127.0.0.1/tools/x",
                    "schema": {"type": "object"},
                    "timeout_seconds": 5,
                    "foreground_only": True,
                }
            ],
        }
        req = Request.from_json(raw)
        declared = (req.tools or [None])[0]
        assert declared is not None
        self.assertEqual(declared.name, "server_status")
        self.assertEqual(declared.timeout_seconds, 5)
        self.assertTrue(declared.foreground_only)
        payload = Request.from_json(raw).to_json()
        self.assertEqual(payload["tools"][0]["name"], "server_status")
        self.assertTrue(payload["tools"][0]["foreground_only"])


class TestOrigin(unittest.TestCase):
    def test_validate_accepts_known_kinds(self) -> None:
        for kind in (OriginHTTPToken, OriginHook, OriginInternal):
            Origin(kind=kind, name="home").validate()

    def test_validate_rejects_unknown_kind_and_empty_name(self) -> None:
        with self.assertRaises(InvalidError):
            Origin(kind="carrier-pigeon", name="home").validate()
        with self.assertRaises(InvalidError):
            Origin(kind=OriginHTTPToken, name="   ").validate()

    def test_label(self) -> None:
        self.assertEqual(Origin(kind=OriginHTTPToken, name=" home ").label(), "http_token:home")
        self.assertEqual(Origin(kind=OriginHook, name="").label(), "hook")


class TestTargetDelivery(unittest.TestCase):
    def test_to_delivery_target(self) -> None:
        private = Target(platform="telegram", type="private", id="42").to_delivery_target()
        self.assertEqual((private.platform, private.private_user_id), ("telegram", "42"))
        group = Target(platform="qqonebot", type="group", id="9").to_delivery_target()
        self.assertEqual((group.platform, group.group_id), ("qqonebot", "9"))
        admin = Target(platform="cli").to_delivery_target()
        self.assertTrue(admin.superadmins)

    def test_target_scope_id(self) -> None:
        self.assertEqual(TargetScopeID(Target(platform="telegram", type="private", id="1")), "private:1")
        self.assertEqual(
            TargetScopeID(Target(platform="qqofficial", type="private", id="1")), "c2c:1"
        )
        self.assertEqual(TargetScopeID(Target(platform="cli", type="group", id="2")), "group:2")
        self.assertEqual(TargetScopeID(Target(platform="cli")), "")
        self.assertEqual(TargetScopeID(Target(platform="cli", type="private", id=" ")), "")
        self.assertEqual(TargetScopeID(None), "")
        self.assertEqual(target_scope_id(Target(platform="cli", type="group", id="3")), "group:3")


class TestSegmentsDelivery(unittest.TestCase):
    def test_direct_text_joins_title_and_content(self) -> None:
        self.assertEqual(DirectText(Request(title="警告", content=" 异常 ")), "警告\n异常")
        self.assertEqual(DirectText(Request(content="只有内容")), "只有内容")

    def test_build_direct_outputs_without_segments(self) -> None:
        outputs = build_direct_outputs(Request(title="t", content="c"), None)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, DeliveryKind.TEXT)
        self.assertEqual(outputs[0].text, "t\nc")

    def test_segments_outputs_media_and_paths(self) -> None:
        segments = [
            Segment(kind=SegmentKindText, text="看这个"),
            Segment(kind=SegmentKindImage, url="chart.png", name="图"),
            Segment(kind=SegmentKindFile, url=MEDIA_ID, name="报告.pdf", mime_type="application/pdf"),
        ]
        paths = {segment_key(1, segments[1]): "/tmp/materialized/chart.png"}
        outputs = segments_outputs(segments, paths)
        self.assertEqual(outputs[0].kind, DeliveryKind.TEXT)
        self.assertEqual(outputs[0].text, "看这个")
        self.assertEqual(outputs[1].kind, DeliveryKind.IMAGE)
        self.assertEqual(outputs[1].source.path, "/tmp/materialized/chart.png")
        self.assertEqual(outputs[1].name, "图")
        self.assertEqual(outputs[2].kind, DeliveryKind.FILE)
        self.assertTrue(ValidID(outputs[2].source.media_id))
        self.assertEqual(outputs[2].source.path, "")

    def test_segment_key_shape(self) -> None:
        self.assertEqual(segment_key(0, Segment(kind=SegmentKindText)), "0:text")


class TestRawCalls(unittest.TestCase):
    def test_kind_is_inferred(self) -> None:
        self.assertEqual(resolve_raw_call(Call(api=" delete_msg ", params={"a": 1})).api, "delete_msg")
        raw = resolve_raw_call(Call(name=CapabilityMessageRecall, platform="qqonebot", params={"message_id": 7}))
        self.assertEqual(raw.api, "delete_msg")
        self.assertEqual(raw.params, {"message_id": 7})

    def test_unsupported_kind_and_capability(self) -> None:
        with self.assertRaises(InvalidError):
            resolve_raw_call(Call())
        with self.assertRaises(InvalidError):
            resolve_raw_call(Call(kind="telepathy", name="x"))
        with self.assertRaises(InvalidError):
            resolve_raw_call(Call(kind=CallKindCapability, name="message.pin", platform="cli"))

    def test_raw_call_requires_api_and_clones_params(self) -> None:
        with self.assertRaises(InvalidError):
            resolve_raw_call(Call(kind="raw", api="  "))
        params = {"message_id": 1}
        raw = resolve_raw_call(Call(kind="raw", api="x", params=params))
        raw.params["message_id"] = 99
        self.assertEqual(params["message_id"], 1)

    def test_message_recall_telegram_uses_target_id(self) -> None:
        raw = map_message_recall(
            Call(
                name=CapabilityMessageRecall,
                platform="telegram",
                target=Target(platform="telegram", type="group", id="555"),
                params={"message_id": 3},
            )
        )
        self.assertEqual(raw.api, "deleteMessage")
        self.assertEqual(raw.params, {"chat_id": 555, "message_id": 3})

    def test_message_recall_unmapped_platform(self) -> None:
        with self.assertRaises(InvalidError):
            map_message_recall(Call(name=CapabilityMessageRecall, platform="cli", params={"message_id": 1}))
        with self.assertRaises(InvalidError):
            map_message_recall(Call(name=CapabilityMessageRecall, platform="cli"))

    def test_member_mute_onebot_and_telegram(self) -> None:
        onebot = map_member_mute(
            Call(
                name=CapabilityMemberMute,
                platform="qq-onebot",
                params={"group_id": 10, "user_id": 20, "duration_seconds": 60},
            )
        )
        self.assertEqual(onebot.api, "set_group_ban")
        self.assertEqual(
            onebot.params, {"group_id": 10, "user_id": 20, "duration": 60}
        )
        telegram = map_member_mute(
            Call(
                name=CapabilityMemberMute,
                platform="telegram",
                target=Target(platform="telegram", type="group", id="77"),
                params={"user_id": 20, "duration_seconds": 60},
            )
        )
        self.assertEqual(telegram.api, "restrictChatMember")
        self.assertEqual(telegram.params["chat_id"], 77)
        self.assertFalse(telegram.params["permissions"]["can_send_messages"])
        self.assertGreater(telegram.params["until_date"], 0)

    def test_member_mute_rejects_non_positive_duration(self) -> None:
        with self.assertRaises(InvalidError):
            map_member_mute(
                Call(
                    name=CapabilityMemberMute,
                    platform="qqonebot",
                    params={"group_id": 1, "user_id": 2, "duration_seconds": 0},
                )
            )

    def test_chat_leave_passes_is_dismiss_through(self) -> None:
        raw = map_chat_leave(
            Call(
                name=CapabilityChatLeave,
                platform="qqonebot",
                params={"group_id": 5, "is_dismiss": True},
            )
        )
        self.assertEqual(raw.api, "set_group_leave")
        self.assertEqual(raw.params, {"group_id": 5, "is_dismiss": True})
        plain = map_chat_leave(Call(name=CapabilityChatLeave, platform="qqonebot", params={"group_id": 5}))
        self.assertEqual(plain.params, {"group_id": 5})
        telegram = map_chat_leave(
            Call(name=CapabilityChatLeave, platform="telegram", params={"chat_id": 6})
        )
        self.assertEqual(telegram.api, "leaveChat")

    def test_to_int64_semantics(self) -> None:
        self.assertEqual(to_int64(3), 3)
        self.assertEqual(to_int64(3.0), 3)
        self.assertEqual(to_int64(" 12 "), 12)
        self.assertIsNone(to_int64(3.5))
        self.assertIsNone(to_int64(True))
        self.assertIsNone(to_int64("abc"))
        self.assertIsNone(to_int64(None))
        self.assertIsNone(to_int64([]))


class _FakeCaller:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call_platform_api(self, ctx, api, params):  # noqa: ANN001
        self.calls.append((api, dict(params or {})))
        if self.fail:
            raise AtriXError("boom")
        return '{"ok":true}'


class _FakeResolver:
    def __init__(self, callers: dict[str, _FakeCaller]) -> None:
        self.callers = callers

    def platform_caller(self, platform: str):
        caller = self.callers.get(platform)
        return caller, caller is not None


class TestExecuteCalls(unittest.IsolatedAsyncioTestCase):
    async def test_execute_call_requires_resolver_and_platform(self) -> None:
        with self.assertRaises(InvalidError):
            await execute_call(None, None, Call(api="x"))
        resolver = _FakeResolver({})
        with self.assertRaises(InvalidError):
            await execute_call(None, resolver, Call(kind="raw", api="x"))
        with self.assertRaises(InvalidError):
            await execute_call(None, resolver, Call(kind="raw", api="x", platform="cli"))

    async def test_execute_calls_collects_results(self) -> None:
        caller = _FakeCaller()
        resolver = _FakeResolver({"cli": caller})
        results = await execute_calls(
            None, resolver, [Call(kind="raw", api="a", platform="cli"), Call(kind="raw", api="b", platform="cli")]
        )
        self.assertEqual([result.response for result in results], ['{"ok":true}', '{"ok":true}'])
        self.assertEqual([api for api, _ in caller.calls], ["a", "b"])

    async def test_execute_calls_carries_partial_results_on_failure(self) -> None:
        caller = _FakeCaller(fail=True)
        resolver = _FakeResolver({"cli": caller})
        with self.assertRaises(CallExecutionError) as caught:
            await execute_calls(None, resolver, [Call(kind="raw", api="a", platform="cli")])
        self.assertEqual(len(caught.exception.results), 1)
        self.assertEqual(caught.exception.results[0].error, "boom")
        self.assertIsInstance(caught.exception.results[0], CallResult)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.seen: list[tuple[Origin, Request]] = []

    async def dispatch_atriena(self, ctx, origin, req):  # noqa: ANN001
        self.seen.append((origin, req))
        from atri_x.atriena import Response

        return Response(accepted=True, status=StatusCompleted, mode=req.mode)


class TestBus(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_without_dispatcher_raises(self) -> None:
        bus = Bus()
        with self.assertRaises(InvalidError) as caught:
            await bus.dispatch_atriena(None, Origin(kind=OriginInternal, name="home"), Request())
        self.assertEqual(str(caught.exception), DispatcherNotConfiguredMessage)

    async def test_dispatch_delegates(self) -> None:
        bus = Bus()
        dispatcher = _FakeDispatcher()
        bus.set_dispatcher(dispatcher)
        origin = Origin(kind=OriginInternal, name="home")
        response = await bus.dispatch_atriena(None, origin, Request(mode=ModeRecord))
        self.assertTrue(response.accepted)
        self.assertEqual(response.status, StatusCompleted)
        self.assertEqual(dispatcher.seen[0][0].label(), "internal:home")

    async def test_dispatch_is_concurrency_safe(self) -> None:
        bus = Bus()
        bus.set_dispatcher(_FakeDispatcher())
        origin = Origin(kind=OriginInternal, name="home")
        responses = await asyncio.gather(
            *(bus.dispatch_atriena(None, origin, Request()) for _ in range(5))
        )
        self.assertEqual(len(responses), 5)
        self.assertTrue(all(item.accepted for item in responses))


if __name__ == "__main__":
    unittest.main()
