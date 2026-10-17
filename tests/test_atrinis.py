"""``atri_x.atrinis`` 核心行为的单元测试：校验、鉴权、目标解析与 direct 模式。

用真实的 SQLite 存储（``atri_x.storage.sqlite``）与假发送器/假 runner，覆盖
``internal/atrinis/service_test.go`` 里的关键边界语义。llm 模式与汇报出箱见
``tests/test_atrinis_llm.py``，HTTP 层与 wire 编码见
``tests/test_atrinis_http.py``，共享夹具见 ``tests/_atrinis_support.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.atriena import (
    Atriwisp,
    Call,
    CallKindCapability,
    CapabilityMessageRecall,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Origin,
    OriginInternal,
    Request,
    Segment,
    SegmentKindImage,
    SegmentKindText,
    Target,
    VersionV2,
)
from atri_x.atrinis import (
    ATRIWISP_NAME_PATTERN,
    Options,
    Service,
    StatusCompleted,
    StatusDelivering,
    StatusDuplicate,
    StatusFailed,
    StatusQueued,
    StatusResultReady,
    UNAUTHORIZED_MESSAGE,
    atrinis_sandbox_subdir,
    background_tool_names,
    content_hash,
    decode_resolved_targets,
    first_non_empty,
    first_platform,
    hash_text,
    is_atrinis_model_slot,
    is_atrinis_session_mode,
    normalize_targets,
    segments_content_text,
    set_from_strings,
    trim_strings,
    unique_sorted,
    unique_targets,
    validate_media_segment,
    validate_segment_url,
)
from atri_x.background import JSONResult
from atri_x.config import (
    AtrinisAtriwispConfig,
    AtrinisConfig,
    AtrinisTargetConfig,
    ModelSelection,
)
from atri_x.errors import InvalidError
from atri_x.storage import (
    AtrinisReportDeliveryDelivered,
    AtrinisReportDeliveryFailed,
    Message,
    RoleAssistant,
    Session,
    SessionModeWork,
    SessionStatusActive,
    sqlite,
)
from atri_x.toolrun import AtriwispToolDeclaration

from tests._atrinis_support import (
    TMP_ROOT,
    _FakePlatformCaller,
    _FakePlatformCallerResolver,
    _FakeRunner,
    _ServiceCase,
    external_tool,
    test_request,
)

class TestConstruction(_ServiceCase):
    async def test_requires_store(self) -> None:
        with self.assertRaises(InvalidError):
            Service(Options(config=AtrinisConfig()))

    async def test_enabled_requires_tokens(self) -> None:
        with self.assertRaises(InvalidError):
            Service(Options(config=AtrinisConfig(enabled=True), store=self.store))

    async def test_default_sandbox_root(self) -> None:
        service = Service(Options(store=self.store, tokens={"a": "b"}))
        self.assertEqual(service.sandbox_root, os.path.join("data", "sandbox"))

    async def test_enabled_platforms_are_normalized(self) -> None:
        service = Service(
            Options(
                store=self.store,
                tokens={"a": "b"},
                config=AtrinisConfig(enabled=True),
                enabled_platforms=[" qqonebot ", "cli", "cli", ""],
            )
        )
        self.assertEqual(service.enabled_platforms, ["cli", "qqonebot"])


class TestRecordAndDuplicate(_ServiceCase):
    async def test_record_completes_then_duplicates(self) -> None:
        response = await self.service.handle(None, "secret", test_request(ModeRecord))
        self.assertTrue(response.accepted)
        self.assertFalse(response.duplicate)
        self.assertEqual(response.status, StatusCompleted)

        again = await self.service.handle(None, "secret", test_request(ModeRecord))
        self.assertTrue(again.accepted)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.status, StatusDuplicate)

    async def test_record_does_not_persist_media_ids(self) -> None:
        req = test_request(ModeRecord)
        req.segments = [Segment(kind=SegmentKindImage, url="media:" + "a" * 64)]
        response = await self.service.handle(None, "secret", req)
        self.assertEqual(response.status, StatusCompleted)
        record = self.store.atrinis_events().get_by_key("watcher", "source", "event-1")
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(record.tags, '["test"]')

    async def test_unauthorized(self) -> None:
        response = await self.service.handle(None, "wrong", test_request(ModeRecord))
        self.assertFalse(response.accepted)
        self.assertEqual(response.status, StatusFailed)
        self.assertEqual(response.error, UNAUTHORIZED_MESSAGE)

    async def test_event_attrs_include_tags(self) -> None:
        event = self.service.prepare_event(
            Origin(kind=OriginInternal, name="home"), test_request(ModeRecord)
        )
        attrs = self.service.event_attrs(event)
        pairs = dict(zip(attrs[0::2], attrs[1::2]))
        self.assertEqual(pairs["tags"], '["test"]')
        self.assertEqual(pairs["origin"], "internal:home")


class TestAtriwispAuthorization(_ServiceCase):
    async def _record(self, name: str, token: str = "secret"):
        req = test_request(ModeRecord)
        req.atriwisp.name = name
        return await self.service.handle(None, token, req)

    async def test_unconfigured_atriwisp_allowed(self) -> None:
        response = await self._record("unknown")
        self.assertTrue(response.accepted)
        self.assertEqual(response.status, StatusCompleted)

    async def test_configured_without_enabled_allowed(self) -> None:
        self.assertEqual((await self._record("configured")).status, StatusCompleted)

    async def test_explicitly_enabled_allowed(self) -> None:
        self.assertEqual((await self._record("enabled")).status, StatusCompleted)

    async def test_disabled_rejected(self) -> None:
        response = await self._record("disabled")
        self.assertFalse(response.accepted)
        self.assertIn("disabled", response.error)

    async def test_token_not_allowed_for_atriwisp(self) -> None:
        response = await self._record("watcher", token="alt-secret")
        self.assertFalse(response.accepted)
        self.assertIn("not allowed", response.error)

    async def test_token_restriction_skipped_for_non_http_origin(self) -> None:
        req = test_request(ModeRecord)
        req.atriwisp.name = "watcher"
        response = await self._dispatch(req, Origin(kind=OriginInternal, name="alt"))
        self.assertTrue(response.accepted)


class TestInternalTools(_ServiceCase):
    async def test_allowed_tool(self) -> None:
        req = test_request(ModeLLM)
        req.tool_list_names = ["shell"]
        response, queued = await self._queue_llm(req)
        self.assertEqual(response.status, StatusQueued)
        self.assertIsNotNone(queued)

    async def test_disallowed_tool(self) -> None:
        req = test_request(ModeLLM)
        req.tool_list_names = ["cron"]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn("not allowed", response.error)

    async def test_discover_tool_is_exempt(self) -> None:
        req = test_request(ModeLLM)
        req.tool_list_names = ["discover_tool", "shell", "shell"]
        response, _ = await self._queue_llm(req)
        self.assertEqual(response.status, StatusQueued)

    async def test_global_allowlist_used_when_atriwisp_has_none(self) -> None:
        req = test_request(ModeLLM)
        req.atriwisp.name = "configured"
        req.tool_list_names = ["web_search"]
        response, _ = await self._queue_llm(req)
        self.assertEqual(response.status, StatusQueued)

    async def test_disallowed_global_tool(self) -> None:
        req = test_request(ModeLLM)
        req.atriwisp.name = "configured"
        req.tool_list_names = ["shell"]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)


class TestExternalTools(_ServiceCase):
    async def test_default_allowed_and_persisted(self) -> None:
        req = test_request(ModeLLM)
        req.tools = [external_tool("server_status")]
        response, _ = await self._queue_llm(req)
        self.assertEqual(response.status, StatusQueued)
        record = self.store.atrinis_events().get_by_key("watcher", "source", "event-1")
        self.assertTrue(record.tool_declarations)
        self.assertTrue(record.tool_hash)

    async def test_disabled_tool_rejected(self) -> None:
        req = test_request(ModeLLM)
        req.tools = [external_tool("danger_tool")]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn("disabled", response.error)

    async def _reject(self, declared: AtriwispToolDeclaration, needle: str) -> None:
        req = test_request(ModeLLM)
        req.tools = [declared]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn(needle, response.error)

    async def test_invalid_declarations(self) -> None:
        blank = external_tool("  ")
        await self._reject(blank, "name is required")
        dotted = external_tool("foo.bar")
        await self._reject(dotted, "invalid name")
        no_endpoint = external_tool("ok")
        no_endpoint.endpoint = "   "
        await self._reject(no_endpoint, "endpoint is required")
        bad_scheme = external_tool("ok")
        bad_scheme.endpoint = "ftp://example.com/x"
        await self._reject(bad_scheme, "http or https")
        bad_timeout = external_tool("ok")
        bad_timeout.timeout_seconds = 61
        await self._reject(bad_timeout, "timeout_seconds")
        no_schema = external_tool("ok")
        no_schema.schema = {}
        await self._reject(no_schema, "schema is required")
        bad_schema = external_tool("ok")
        bad_schema.schema = {"type": "array"}
        await self._reject(bad_schema, "schema.type must be object")

    async def test_duplicate_tool_rejected(self) -> None:
        req = test_request(ModeLLM)
        req.tools = [external_tool("dup"), external_tool("dup")]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn("duplicated", response.error)


class TestPrepareValidation(_ServiceCase):
    async def _reject(self, mutate, needle: str) -> None:
        req = test_request(ModeRecord)
        mutate(req)
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn(needle, response.error)

    async def test_version(self) -> None:
        await self._reject(lambda r: setattr(r, "version", "atriena.v1"), "unsupported Atriena version")

    async def test_atriwisp_name_required_and_pattern(self) -> None:
        await self._reject(lambda r: setattr(r.atriwisp, "name", "  "), "atriwisp.name is required")
        await self._reject(lambda r: setattr(r.atriwisp, "name", "foo.bar"), "atriwisp.name")

    async def test_atriwisp_name_pattern_is_anchored(self) -> None:
        # Python 的 ``$`` 会在结尾换行前匹配，因此实现必须用 fullmatch；
        # prepare_event 会先 strip，所以这里直接验正则本身。
        self.assertIsNotNone(ATRIWISP_NAME_PATTERN.fullmatch("watcher"))
        self.assertIsNotNone(ATRIWISP_NAME_PATTERN.fullmatch("a-1_B"))
        self.assertIsNone(ATRIWISP_NAME_PATTERN.fullmatch("watcher\n"))
        self.assertIsNone(ATRIWISP_NAME_PATTERN.fullmatch("foo.bar"))
        self.assertIsNone(ATRIWISP_NAME_PATTERN.fullmatch(""))

    async def test_required_fields(self) -> None:
        await self._reject(lambda r: setattr(r, "source", " "), "source is required")
        await self._reject(lambda r: setattr(r, "id", " "), "id is required")
        await self._reject(lambda r: setattr(r, "content", " "), "content, segments or calls is required")

    async def test_format_and_mode(self) -> None:
        await self._reject(lambda r: setattr(r, "format", "markdown"), "unsupported format")
        await self._reject(lambda r: setattr(r, "mode", "chat"), "unsupported mode")

    async def test_llm_requires_content(self) -> None:
        # 有段时第一条"content, segments or calls"检查通过，才会命中 llm 专属检查。
        await self._reject(
            lambda r: (
                setattr(r, "mode", ModeLLM),
                setattr(r, "content", ""),
                setattr(r, "segments", [Segment(kind=SegmentKindText, text="x")]),
            ),
            "content is required for llm mode",
        )

    async def test_model_slot_and_session_mode(self) -> None:
        await self._reject(lambda r: setattr(r, "model_slot", "chat"), "unsupported model_slot")
        await self._reject(lambda r: setattr(r, "session_mode", "bot"), "unsupported session_mode")

    async def test_segment_validation(self) -> None:
        await self._reject(
            lambda r: setattr(r, "segments", [Segment(kind=SegmentKindText, url="http://x/")]),
            "segment 0",
        )

    async def test_created_at_must_be_rfc3339(self) -> None:
        await self._reject(lambda r: setattr(r, "created_at", "2026-13-45"), "created_at must be RFC3339")

    async def test_created_at_is_parsed(self) -> None:
        req = test_request(ModeRecord)
        req.created_at = "2026-10-13T12:00:00Z"
        event = self.service.prepare_event(Origin(kind=OriginInternal, name="home"), req)
        self.assertEqual(event.created_at.year, 2026)
        self.assertEqual(event.created_at.month, 10)

    async def test_prepare_does_not_mutate_caller_request(self) -> None:
        req = test_request(ModeRecord)
        req.content = "  padded  "
        req.atriwisp.name = " watcher "
        event = self.service.prepare_event(Origin(kind=OriginInternal, name="home"), req)
        self.assertEqual(event.request.content, "padded")
        self.assertEqual(event.request.atriwisp.name, "watcher")
        # Go 的值语义：调用方对象不变。
        self.assertEqual(req.content, "  padded  ")
        self.assertEqual(req.atriwisp.name, " watcher ")

    async def test_event_key_and_hash(self) -> None:
        event = self.service.prepare_event(
            Origin(kind=OriginInternal, name="home"), test_request(ModeRecord)
        )
        self.assertEqual(event.event_key, "watcher/source/event-1")
        self.assertEqual(len(event.content_hash), 64)
        self.assertEqual(event.tool_hash, "")
        self.assertEqual(event.tags_json, '["test"]')
        self.assertEqual(event.requested_targets, '[{"platform":"cli"}]')


class TestTargets(_ServiceCase):
    async def test_all_expands_to_enabled_platforms(self) -> None:
        req = test_request(ModeDirect)
        req.targets = [Target(platform="all")]
        resolved = self.service.resolve_targets(req)
        self.assertEqual([t.platform for t in resolved], ["cli", "qqonebot"])

    async def test_missing_targets(self) -> None:
        req = test_request(ModeDirect)
        req.targets = []
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn("targets is required", response.error)

    async def test_all_cannot_set_type_or_id(self) -> None:
        req = test_request(ModeDirect)
        req.targets = [Target(platform="all", type="group", id="123")]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn("platform all", response.error)

    async def test_disabled_platform_blocks_every_form(self) -> None:
        self.service.cfg.delivery_disabled.targets = [AtrinisTargetConfig(platform="telegram")]
        req = test_request(ModeLLM)
        req.targets = [
            Target(platform="telegram"),
            Target(platform="telegram", type="private", id="1"),
            Target(platform="telegram", type="group", id="2"),
            Target(platform="cli"),
        ]
        resolved = self.service.resolve_targets(req)
        self.assertEqual([t.platform for t in resolved], ["cli"])

    async def test_atriwisp_disabled_private_target(self) -> None:
        policy = self.service.cfg.atriwisps["watcher"]
        policy.disabled_targets = [AtrinisTargetConfig(platform="telegram", type="private", id="1")]
        self.service.cfg.atriwisps["watcher"] = policy
        req = test_request(ModeLLM)
        req.targets = [
            Target(platform="telegram", type="private", id="1"),
            Target(platform="telegram", type="group", id="2"),
        ]
        resolved = self.service.resolve_targets(req)
        self.assertEqual([(t.platform, t.type, t.id) for t in resolved], [("telegram", "group", "2")])

    async def test_direct_requires_a_target(self) -> None:
        req = test_request(ModeDirect)
        req.targets = [Target(platform="telegram")]
        self.service.cfg.delivery_disabled.targets = [AtrinisTargetConfig(platform="telegram")]
        response = await self.service.handle(None, "secret", req)
        self.assertFalse(response.accepted)
        self.assertIn("no allowed delivery targets", response.error)

    async def test_decode_resolved_targets(self) -> None:
        self.assertEqual(decode_resolved_targets("[]"), [])
        self.assertEqual(decode_resolved_targets("null"), [])
        decoded = decode_resolved_targets('[{"platform":"qqonebot","type":"group","id":"2"}]')
        self.assertEqual((decoded[0].platform, decoded[0].type, decoded[0].id), ("qqonebot", "group", "2"))
        with self.assertRaises(ValueError):
            decode_resolved_targets('{"platform":"cli"}')


class TestDirect(_ServiceCase):
    async def test_sends_to_private_and_group_targets(self) -> None:
        req = test_request(ModeDirect)
        req.title = "警告"
        req.content = "服务器异常"
        req.targets = [
            Target(platform="telegram", type="private", id="123"),
            Target(platform="qqonebot", type="group", id="456"),
        ]
        response = await self.service.handle(None, "secret", req)
        self.assertEqual(response.status, StatusCompleted)
        got = [f"{platform}:{outputs[0].text}" for platform, outputs in self.sent]
        self.assertEqual(got, ["qqonebot:警告\n服务器异常", "telegram:警告\n服务器异常"])

    async def test_superadmin_targets(self) -> None:
        req = test_request(ModeDirect)
        req.title = "警告"
        req.content = "服务器异常"
        req.targets = [Target(platform="cli"), Target(platform="qqofficial")]
        response = await self.service.handle(None, "secret", req)
        self.assertEqual(response.status, StatusCompleted)
        self.assertTrue(all(outputs[0].text == "警告\n服务器异常" for _, outputs in self.sent))
        self.assertEqual([platform for platform, _ in self.sent], ["cli", "qqofficial"])

    async def test_calls_only_does_not_send_outputs(self) -> None:
        caller = _FakePlatformCaller()
        self.service.platform_callers = _FakePlatformCallerResolver({"qqonebot": caller})
        req = test_request(ModeDirect)
        req.content = ""
        req.title = ""
        req.calls = [
            Call(
                kind=CallKindCapability,
                name=CapabilityMessageRecall,
                platform="qqonebot",
                params={"message_id": 42},
            )
        ]
        response = await self.service.handle(None, "secret", req)
        self.assertEqual(response.status, StatusCompleted)
        self.assertEqual(self.sent, [])
        self.assertEqual(caller.calls, [("delete_msg", {"message_id": 42})])

    async def test_call_platform_falls_back_to_first_target(self) -> None:
        caller = _FakePlatformCaller()
        self.service.platform_callers = _FakePlatformCallerResolver({"cli": caller})
        req = test_request(ModeDirect)
        req.content = ""
        req.title = ""
        req.calls = [Call(kind="raw", api="ping")]
        response = await self.service.handle(None, "secret", req)
        self.assertEqual(response.status, StatusCompleted)
        self.assertEqual(caller.calls, [("ping", {})])

    async def test_calls_without_resolver_fail(self) -> None:
        req = test_request(ModeDirect)
        req.content = ""
        req.title = ""
        req.calls = [Call(kind="raw", api="ping", platform="cli")]
        response = await self.service.handle(None, "secret", req)
        self.assertEqual(response.status, StatusFailed)
        self.assertIn("platform api callers are not configured", response.error)
        record = self.store.atrinis_events().get_by_key("watcher", "source", "event-1")
        self.assertEqual(record.status, StatusFailed)

    async def test_sender_failure_is_recorded(self) -> None:
        async def _boom(ctx, target, outputs):  # noqa: ANN001
            raise InvalidError("send exploded")

        service = self._make_service(send=_boom)
        req = test_request(ModeDirect)
        response = await service.dispatch_atriena(
            None, Origin(kind=OriginInternal, name="home"), req
        )
        self.assertTrue(response.accepted)
        self.assertEqual(response.status, StatusFailed)
        self.assertIn("send exploded", response.error)
        record = self.store.atrinis_events().get_by_key("watcher", "source", "event-1")
        self.assertEqual(record.status, StatusFailed)

    async def test_missing_sender(self) -> None:
        service = self._make_service(send=None)
        service.send = None
        req = test_request(ModeDirect)
        response = await service.dispatch_atriena(
            None, Origin(kind=OriginInternal, name="home"), req
        )
        self.assertEqual(response.status, StatusFailed)
        self.assertIn("sender is not configured", response.error)


