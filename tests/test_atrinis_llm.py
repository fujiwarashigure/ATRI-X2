"""``atri_x.atrinis`` 的 llm 模式、汇报出箱与恢复测试。

覆盖 ``internal/atrinis/llm.go`` / ``outbox.go`` 的关键边界语义（前台接管、
JSON 格式重试、状态机 ``result_ready`` → ``delivering`` → ``completed``、
只重投未完成的投递）。校验/鉴权/目标解析见 ``tests/test_atrinis.py``，
HTTP 层见 ``tests/test_atrinis_http.py``，共享夹具见 ``tests/_atrinis_support.py``。
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

class TestLLMEvent(_ServiceCase):
    async def test_queues_and_persists_queued_status(self) -> None:
        req = test_request(ModeLLM)
        response, queued = await self._queue_llm(req)
        self.assertEqual(response.status, StatusQueued)
        self.assertTrue(queued.event_id)
        self.assertTrue(queued.event.event_key)
        record = self.store.atrinis_events().get_by_key("watcher", "source", "event-1")
        self.assertEqual(record.status, StatusQueued)

    async def test_enqueue_failure_marks_event_failed(self) -> None:
        async def _full(ctx, item):  # noqa: ANN001
            raise InvalidError("queue full")

        self.service.set_llm_enqueuer(_full)
        response = await self.service.handle(None, "secret", test_request(ModeLLM))
        self.assertEqual(response.status, StatusFailed)
        self.assertEqual(response.error, "queue full")
        record = self.store.atrinis_events().get_by_key("watcher", "source", "event-1")
        self.assertEqual(record.status, StatusFailed)

    async def test_uses_model_slot(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":false,"report":""}')
        service = self._make_service(runner=runner)
        req = test_request(ModeLLM)
        req.model_slot = "atriwisp2"
        _, queued = await self._queue_llm(req)
        await service.run_llm_event(None, queued.event, queued.event_id)
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(runner.requests[0].model_provider, "provider-atriwisp2")
        self.assertEqual(runner.requests[0].model, "model-atriwisp2")
        self.assertEqual(runner.requests[0].kind, "atrinis")
        self.assertEqual(runner.requests[0].sandbox_subdir, "atrinis/watcher")

    async def test_defaults_to_work_slot(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":false,"report":""}')
        service = self._make_service(runner=runner)
        _, queued = await self._queue_llm(test_request(ModeLLM))
        await service.run_llm_event(None, queued.event, queued.event_id)
        self.assertEqual(runner.requests[0].model_provider, "provider-work")

    async def test_falls_back_when_slot_unconfigured(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":false,"report":""}')
        service = self._make_service(runner=runner)
        service.resolve_model = lambda slot: (
            ModelSelection(provider="provider-work", model="model-work")
            if slot == "work"
            else ModelSelection()
        )
        req = test_request(ModeLLM)
        req.model_slot = "atriwisp3"
        _, queued = await self._queue_llm(req)
        await service.run_llm_event(None, queued.event, queued.event_id)
        self.assertEqual(runner.requests[0].model_provider, "provider-work")

    async def test_missing_runner_marks_failed(self) -> None:
        service = self._make_service(runner=None)
        _, queued = await self._queue_llm(test_request(ModeLLM))
        with self.assertRaises(InvalidError):
            await service.run_llm_event(None, queued.event, queued.event_id)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusFailed)

    async def test_completes_and_reports(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')
        service = self._make_service(runner=runner)
        req = test_request(ModeLLM)
        req.targets = [Target(platform="cli")]
        _, queued = await self._queue_llm(req)
        await service.run_llm_event(None, queued.event, queued.event_id)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(record.session_id, "bg-session")
        self.assertIn("处理完成", record.result)
        self.assertEqual(
            [f"{platform}:{outputs[0].text}" for platform, outputs in self.sent],
            ["cli:处理完成"],
        )
        deliveries = self.store.atrinis_events().list_report_deliveries(queued.event_id)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].status, AtrinisReportDeliveryDelivered)

    async def test_completes_without_report_when_targets_empty_reports(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":true,"report":""}')
        service = self._make_service(runner=runner)
        req = test_request(ModeLLM)
        req.targets = [Target(platform="cli")]
        _, queued = await self._queue_llm(req)
        await service.run_llm_event(None, queued.event, queued.event_id)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(self.store.atrinis_events().list_report_deliveries(queued.event_id), [])

    async def test_status_is_delivering_while_sending(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _send(ctx, target, outputs):  # noqa: ANN001
            entered.set()
            await release.wait()
            from atri_x.delivery import Receipt

            return Receipt(platform_message_ids=["notice-1"])

        service = self._make_service(runner=runner, send=_send)
        _, queued = await self._queue_llm(test_request(ModeLLM))
        task = asyncio.create_task(service.run_llm_event(None, queued.event, queued.event_id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusDelivering)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        deliveries = self.store.atrinis_events().list_report_deliveries(queued.event_id)
        self.assertEqual(deliveries[0].status, AtrinisReportDeliveryDelivered)
        self.assertIn("notice-1", deliveries[0].receipt)

    async def test_failed_result_is_reported_when_requested(self) -> None:
        runner = _FakeRunner(
            '{"completed":false,"need_report":true,"report":"创建失败：工具权限不足"}'
        )
        service = self._make_service(runner=runner)
        _, queued = await self._queue_llm(test_request(ModeLLM))
        await service.run_llm_event(None, queued.event, queued.event_id)
        self.assertEqual(self.sent[0][1][0].text, "创建失败：工具权限不足")

    async def test_taken_over_completes_without_report(self) -> None:
        class _TakenOver:
            async def run_background(self, ctx, req):  # noqa: ANN001
                from atri_x.background import RunResult

                return RunResult(session_id="bg-session", taken_over=True, text="")

        service = self._make_service(runner=_TakenOver())
        _, queued = await self._queue_llm(test_request(ModeLLM))
        await service.run_llm_event(None, queued.event, queued.event_id)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(self.sent, [])

    async def test_json_parse_failure_retries_then_fails(self) -> None:
        class _BadTwice:
            def __init__(self) -> None:
                self.requests: list = []

            async def run_background(self, ctx, req):  # noqa: ANN001
                from atri_x.background import RunResult

                self.requests.append(req)
                return RunResult(session_id="bg-session", text="not json at all")

        runner = _BadTwice()
        service = self._make_service(runner=runner)
        _, queued = await self._queue_llm(test_request(ModeLLM))
        with self.assertRaises(ValueError):
            await service.run_llm_event(None, queued.event, queued.event_id)
        self.assertEqual(len(runner.requests), 2)
        self.assertTrue(runner.requests[1].session_id == "bg-session")
        self.assertTrue(runner.requests[1].prompt)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusFailed)
        self.assertIn("解析格式失败", record.result)

    async def test_json_parse_failure_recovers_on_retry(self) -> None:
        class _Flaky:
            def __init__(self) -> None:
                self.count = 0

            async def run_background(self, ctx, req):  # noqa: ANN001
                from atri_x.background import RunResult

                self.count += 1
                if self.count == 1:
                    return RunResult(session_id="bg-session", text="```json broken")
                return RunResult(
                    session_id="bg-session", text='{"completed":true,"need_report":false,"report":""}'
                )

        service = self._make_service(runner=_Flaky())
        _, queued = await self._queue_llm(test_request(ModeLLM))
        await service.run_llm_event(None, queued.event, queued.event_id)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)

    async def test_llm_prompt_shape(self) -> None:
        event = self.service.prepare_event(
            Origin(kind=OriginInternal, name="home"), test_request(ModeLLM)
        )
        prompt = self.service.llm_prompt(event)
        self.assertTrue(prompt.startswith("[系统 Atrinis 后台事件任务]\n"))
        self.assertIn("** 最终回复必须是严格 JSON", prompt)
        self.assertIn("事件标题：", prompt)
        self.assertIn("事件内容：\nhello", prompt)
        self.assertNotIn("atriyph", prompt)

    async def test_llm_prompt_includes_atriyph_rule_card(self) -> None:
        req = test_request(ModeLLM)
        req.format = "atriyph"
        event = self.service.prepare_event(Origin(kind=OriginInternal, name="home"), req)
        prompt = self.service.llm_prompt(event)
        self.assertIn("事件格式：atriyph", prompt)
        self.assertGreater(len(prompt), len(self.service.llm_prompt(
            self.service.prepare_event(
                Origin(kind=OriginInternal, name="home"), test_request(ModeLLM)
            )
        )))

    async def test_report_maps_notice_back_to_background_message(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')

        async def _send(ctx, target, outputs):  # noqa: ANN001
            from atri_x.delivery import Receipt

            return Receipt(platform_message_ids=["notice-1"])

        service = self._make_service(runner=runner, send=_send)
        self.store.sessions().create(
            Session(
                id="bg-session",
                owner_id="atrinis:home",
                platform="qq-onebot",
                platform_scope_id="atrinis:watcher:source:event-1",
                mode=SessionModeWork,
                status=SessionStatusActive,
                title="atrinis",
            )
        )
        self.store.messages().append(
            Message(id="bg-message", session_id="bg-session", role=RoleAssistant, content="处理完成")
        )
        req = test_request(ModeLLM)
        req.targets = [Target(platform="qq-onebot", type="private", id="1001")]
        _, queued = await self._queue_llm(req)
        await service.run_llm_event(None, queued.event, queued.event_id)
        found = self.store.messages().find_by_platform_message(
            "qq-onebot", "private:1001", "notice-1"
        )
        self.assertEqual(found.id, "bg-message")
        self.assertEqual(found.session_id, "bg-session")


class TestOutboxRecovery(_ServiceCase):
    async def _prepare(self, service: Service, targets: list[Target]):
        req = test_request(ModeLLM)
        req.targets = targets
        _, queued = await self._queue_llm(req)
        event = queued.event
        parsed = JSONResult(completed=True, need_report=True, report="处理完成")
        result_json = json.dumps(parsed.to_json(), ensure_ascii=False)
        return queued, parsed, result_json

    async def test_retries_only_incomplete_deliveries(self) -> None:
        calls = {"n": 0}

        async def _send(ctx, target, outputs):  # noqa: ANN001
            from atri_x.delivery import Receipt

            calls["n"] += 1
            if calls["n"] == 2:
                raise InvalidError("temporary delivery failure")
            return Receipt(platform_message_ids=["notice-" + target.platform])

        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')
        service = self._make_service(runner=runner, send=_send)
        queued, _, _ = await self._prepare(
            service, [Target(platform="cli"), Target(platform="qqonebot")]
        )
        with self.assertRaises(InvalidError):
            await service.run_llm_event(None, queued.event, queued.event_id)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusResultReady)
        self.assertIn("temporary delivery failure", record.error)
        deliveries = self.store.atrinis_events().list_report_deliveries(queued.event_id)
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(deliveries[0].status, AtrinisReportDeliveryDelivered)
        self.assertEqual(deliveries[1].status, AtrinisReportDeliveryFailed)

        await service.recover_reports(None, False)
        record = self.store.atrinis_events().get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(calls["n"], 3)
        deliveries = self.store.atrinis_events().list_report_deliveries(queued.event_id)
        self.assertEqual(deliveries[0].attempts, 1)
        self.assertEqual(deliveries[1].attempts, 2)

    async def test_receipt_persistence_failure_retries(self) -> None:
        calls = {"n": 0}

        async def _send(ctx, target, outputs):  # noqa: ANN001
            from atri_x.delivery import Receipt

            calls["n"] += 1
            return Receipt(platform_message_ids=["notice-1"])

        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')
        service = self._make_service(runner=runner, send=_send)
        queued, _, _ = await self._prepare(service, [Target(platform="cli")])

        real_repo = self.store.atrinis_events()
        state = {"failed": False}

        class _FailOnce:
            def __getattr__(self, name):
                return getattr(real_repo, name)

            def mark_report_delivery_delivered(self, delivery_id, receipt):
                if not state["failed"]:
                    state["failed"] = True
                    raise InvalidError("receipt persistence failed")
                return real_repo.mark_report_delivery_delivered(delivery_id, receipt)

        self.store.atrinis_events = lambda: _FailOnce()  # type: ignore[method-assign]
        with self.assertRaises(InvalidError):
            await service.run_llm_event(None, queued.event, queued.event_id)
        record = real_repo.get(queued.event_id)
        self.assertEqual(record.status, StatusResultReady)
        self.assertEqual(calls["n"], 1)

        self.store.atrinis_events = lambda: real_repo  # type: ignore[method-assign]
        await service.recover_reports(None, False)
        record = real_repo.get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(calls["n"], 2)
        deliveries = real_repo.list_report_deliveries(queued.event_id)
        self.assertEqual(deliveries[0].attempts, 2)
        self.assertEqual(deliveries[0].status, AtrinisReportDeliveryDelivered)

    async def test_reset_interrupted_delivering(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')
        service = self._make_service(runner=runner)
        queued, parsed, result_json = await self._prepare(service, [Target(platform="cli")])
        prepared = await service.prepare_report(
            None, queued.event, queued.event_id, result_json, "bg-session", "bg-message", parsed
        )
        self.assertTrue(prepared)
        repo = self.store.atrinis_events()
        self.assertTrue(repo.claim_report(queued.event_id, StatusResultReady, StatusDelivering))
        await service.recover_reports(None, True)
        record = repo.get(queued.event_id)
        self.assertEqual(record.status, StatusCompleted)
        self.assertEqual(len(self.sent), 1)

    async def test_does_not_resend_persisted_receipt(self) -> None:
        runner = _FakeRunner('{"completed":true,"need_report":true,"report":"处理完成"}')
        service = self._make_service(runner=runner)
        queued, parsed, result_json = await self._prepare(service, [Target(platform="cli")])
        await service.prepare_report(
            None, queued.event, queued.event_id, result_json, "bg-session", "bg-message", parsed
        )
        repo = self.store.atrinis_events()
        repo.claim_report(queued.event_id, StatusResultReady, StatusDelivering)
        deliveries = repo.list_report_deliveries(queued.event_id)
        repo.start_report_delivery(deliveries[0].id)
        repo.mark_report_delivery_delivered(deliveries[0].id, '{"PlatformMessageIDs":["notice-1"]}')
        await service.recover_reports(None, True)
        self.assertEqual(repo.get(queued.event_id).status, StatusCompleted)
        self.assertEqual(self.sent, [])

    async def test_report_without_targets_completes_immediately(self) -> None:
        service = self.service
        # 目标在预处理阶段就被配置禁用 → resolved_targets 落库为空。
        service.cfg.delivery_disabled.targets = [AtrinisTargetConfig(platform="cli")]
        req = test_request(ModeLLM)
        req.targets = [Target(platform="cli")]
        _, queued = await self._queue_llm(req)
        self.assertEqual(queued.event.resolved_targets, "[]")
        parsed = JSONResult(completed=True, need_report=True, report="处理完成")
        prepared = await service.prepare_report(
            None,
            queued.event,
            queued.event_id,
            json.dumps(parsed.to_json(), ensure_ascii=False),
            "bg-session",
            "bg-message",
            parsed,
        )
        self.assertFalse(prepared)
        self.assertEqual(
            self.store.atrinis_events().get(queued.event_id).status, StatusCompleted
        )

    async def test_report_segments_relative_path(self) -> None:
        runner = _FakeRunner(
            '{"completed":true,"need_report":true,"report":"见图",'
            '"report_segments":[{"type":"image","url":"chart.png"}]}'
        )
        service = self._make_service(runner=runner)
        workspace = os.path.join(self.case_dir, "sandbox", "atrinis", "watcher")
        os.makedirs(workspace, exist_ok=True)
        with open(os.path.join(workspace, "chart.png"), "wb") as handle:
            handle.write(b"png")
        _, queued = await self._queue_llm(test_request(ModeLLM))
        await service.run_llm_event(None, queued.event, queued.event_id)
        # media 未配置 → 段按相对路径解析成 Source.path。
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1][0].text, "见图")

    async def test_report_segment_absolute_path_rejected(self) -> None:
        runner = _FakeRunner(
            '{"completed":true,"need_report":true,"report":"见图",'
            '"report_segments":[{"type":"image","url":"/tmp/chart.png"}]}'
        )
        service = self._make_service(runner=runner)
        _, queued = await self._queue_llm(test_request(ModeLLM))
        with self.assertRaises(ValueError):
            await service.run_llm_event(None, queued.event, queued.event_id)


if __name__ == "__main__":
    unittest.main()
