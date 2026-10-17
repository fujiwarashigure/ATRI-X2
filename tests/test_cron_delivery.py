"""``atri_x.cron`` 的投递、补发与历史迁移测试。

对应 Go 测试：``internal/cron/service_test.go`` 的 delivery / recovery 部分
（``TestNotifyPlatformConnected*``、``TestMissedOnce*``、``TestMigrateLegacyDeliveryState*``、
``TestConcurrentPlatformConnectionsGenerateOnce``、``TestDisableDuringMissedDeliveryDoesNotReenableJob``、
``TestCronSendAuditIncludesPlatform``）。

fake 与临时目录 helper 复用 :mod:`tests.test_cron`（同一套 in-memory fake + 真实
``sqlite3`` 最小 Store），本文件只写用例。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import unittest

from atri_x.cron import (
    METADATA_KIND,
    USER_HANDLER_NAME,
    CronActor,
    CronDeliveryOutputState,
    CronDeliveryState,
    CronDeliveryTargetState,
    CronSchedule,
    CronTarget,
    CronTrigger,
    DeliveryDelivered,
    DeliveryFallbackDelivered,
    DeliveryFallbackPending,
    LegacyCronDeliveryMetadata,
    Metadata,
    PlatformTarget,
    ScheduleCron,
    ScheduleOnce,
    TriggerDirect,
    TriggerLLM,
    decode_delivery_state,
    decode_metadata,
    find_delivery_output_status,
    find_delivery_target_state,
    missed_once_report_text,
)
from atri_x.delivery.delivery import KindImage, KindText, Receipt
from atri_x.llm.llm import MessageSegment, SegmentType
from atri_x.storage.models import UpsertCronJobRequest

from tests.test_cron import (
    LOCATION,
    CronTestCase,
    FakeCronRepo,
    FakeRunner,
    FakeStore,
    SqliteCronStore,
    delivery_platform_done,
    fixed_time,
    must_decode_delivery,
    must_marshal_delivery,
    superadmin,
    test_atriyph_task,
    upsert_test_cron_job,
)


def legacy_once_metadata(
    *,
    title: str = "旧提醒",
    platform: str = "cli",
    platform_user_id: str = "",
    message: str = "旧报告",
    trigger: str = TriggerDirect,
    task: str = "legacy",
    broadcast: bool = False,
) -> Metadata:
    """一次性任务的测试 Metadata。

    ``platform_user_id`` 默认为空：这时收件目标 key 是 ``<platform>|superadmins``
    （与 Go 测试里不填 ``CreatedBy`` 的用例一致）；填了就变成
    ``<platform>|private|<id>``。
    """
    return Metadata(
        kind=METADATA_KIND,
        version=1,
        title=title,
        created_by=CronActor(platform=platform, platform_user_id=platform_user_id),
        schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
        trigger=CronTrigger(
            mode=trigger, message=message if trigger == TriggerDirect else test_atriyph_task(task)
        ),
        target=CronTarget(all_enabled_platforms=broadcast, source_platform=platform),
    )


def with_legacy_delivery_metadata(raw: str, legacy: LegacyCronDeliveryMetadata) -> str:
    """把历史投递段塞进 metadata（对应 Go 测试 helper ``withLegacyDeliveryMetadata``）。"""
    payload = json.loads(raw)
    payload["delivery"] = {
        "completed": legacy.completed,
        "report": legacy.report,
        "report_session_id": legacy.report_session_id,
        "report_message_id": legacy.report_message_id,
        "delivered_platforms": list(legacy.delivered_platforms),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class TestMissedOnce(CronTestCase):
    async def test_notify_platform_connected_delivers_missed_direct_cron_per_platform(self) -> None:
        repo = FakeCronRepo()
        sent: list[str] = []

        async def send_target(ctx, target, outputs):
            sent.append(f"{target.platform}:{outputs[0].text}")
            return Receipt()

        svc = self.make_service(
            store=FakeStore(repo),
            enabled_platforms=[PlatformTarget(name="qqonebot")],
            send_target=send_target,
        )
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo,
            legacy_once_metadata(
                title="提醒", message="该测试啦", trigger=TriggerDirect, broadcast=True
            ),
        )

        await svc.notify_platform_connected(None, "qqonebot")
        self.assertEqual(sent, ["qqonebot:提醒补发：\n\n该测试啦"])
        state = must_decode_delivery(repo.jobs[job.name].delivery_state)
        self.assertTrue(delivery_platform_done(state, "qqonebot"))
        self.assertFalse(delivery_platform_done(state, "cli"))
        self.assertTrue(repo.jobs[job.name].enabled)

        await svc.notify_platform_connected(None, "qqonebot")
        self.assertEqual(len(sent), 1, "重连不得重复投递")
        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(sent[1], "cli:提醒补发：\n\n该测试啦")
        self.assertFalse(repo.jobs[job.name].enabled)

    async def test_notify_platform_connected_generates_llm_report_for_first_connected_target(
        self,
    ) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":true,"need_report":true,"report":"报告内容"}')
        sent: list[str] = []

        async def send_target(ctx, target, outputs):
            sent.append(f"{target.platform}:{outputs[0].text}")
            return Receipt()

        svc = self.make_service(
            store=FakeStore(repo),
            runner=runner,
            enabled_platforms=[PlatformTarget(name="qqonebot")],
            send_target=send_target,
        )
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo,
            legacy_once_metadata(
                title="总结", trigger=TriggerLLM, task="test", broadcast=True
            ),
        )

        await svc.notify_platform_connected(None, "qqonebot")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(sent, ["qqonebot:总结补发：\n\n报告内容"])
        state = must_decode_delivery(repo.jobs[job.name].delivery_state)
        self.assertTrue(state.report_ready)
        self.assertEqual(state.report, "报告内容")
        self.assertTrue(delivery_platform_done(state, "qqonebot"))

        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(runner.calls, 1, "复用已就绪的报告，不得重跑 LLM")
        self.assertEqual(sent[1], "cli:总结补发：\n\n报告内容")
        await svc.notify_platform_connected(None, "qqonebot")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(len(sent), 2)

    async def test_missed_once_report_text_includes_platform(self) -> None:
        self.assertEqual(missed_once_report_text("日报", " 报告内容 "), "日报补发：\n\n报告内容")
        self.assertEqual(missed_once_report_text("日报", ""), "日报补发：")

    async def test_missed_delivery_only_runs_on_platform_connection(self) -> None:
        """没有平台连接就没有补发：Service 里没有任何周期性重试任务。"""
        repo = FakeCronRepo()
        sent: list[str] = []

        async def send_target(ctx, target, outputs):
            sent.append(outputs[0].text)
            return Receipt()

        svc = self.make_service(store=FakeStore(repo), send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        upsert_test_cron_job(
            repo, legacy_once_metadata(title="提醒", message="正文", trigger=TriggerDirect)
        )
        await svc.run_missed_once(None)
        self.assertEqual(sent, [])
        await asyncio.sleep(0.05)
        self.assertEqual(sent, [], "补发不做周期重试")

    async def test_missed_once_reuses_report_when_task_completed_is_false(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":false,"need_report":true,"report":"任务被阻塞"}')
        svc = self.make_service(
            store=FakeStore(repo),
            runner=runner,
            enabled_platforms=[PlatformTarget(name="qqonebot")],
            send_target=lambda ctx, target, outputs: _ok(),
        )
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo,
            legacy_once_metadata(
                title="阻塞任务", trigger=TriggerLLM, task="test", broadcast=True
            ),
        )
        await svc.notify_platform_connected(None, "qqonebot")
        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(runner.calls, 1)
        state = must_decode_delivery(repo.jobs[job.name].delivery_state)
        self.assertTrue(state.report_ready)
        self.assertFalse(state.task_completed, "TaskCompleted 只记录任务结论")
        self.assertFalse(repo.jobs[job.name].enabled, "completed=false 也要投完并条件禁用")

    async def test_notify_platform_connected_skips_already_delivered_platform(self) -> None:
        repo = FakeCronRepo()
        sent: list[str] = []

        async def send_target(ctx, target, outputs):
            sent.append(f"{target.platform}:{outputs[0].text}")
            return Receipt()

        svc = self.make_service(store=FakeStore(repo), send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="提醒", message="该测试啦", trigger=TriggerDirect)
        )
        state = CronDeliveryState(
            run_id="run",
            report_ready=True,
            task_completed=True,
            report="该测试啦",
            targets=[_target("cli|superadmins", [("text", DeliveryDelivered)])],
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(state)
        repo.jobs[job.name].delivery_token = "run"

        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(sent, [])

    async def test_missed_once_reads_latest_config_and_keeps_config_on_write(self) -> None:
        """补发读最新任务配置；投递状态的 CAS 写入不得覆盖配置。"""
        repo = FakeCronRepo()
        attempts: list[str] = []

        async def send_target(ctx, target, outputs):
            out = outputs[0]
            if out.text.startswith("cron 补跑失败："):
                return Receipt()
            attempts.append(out.text)
            if len(attempts) == 1:
                raise RuntimeError("platform down")
            return Receipt()

        svc = self.make_service(store=FakeStore(repo), send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="旧标题", message="正文", trigger=TriggerDirect)
        )
        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(attempts, ["旧标题补发：\n\n正文"])
        self.assertTrue(repo.jobs[job.name].enabled)

        updated = legacy_once_metadata(
            title="新标题", message="正文", trigger=TriggerDirect
        )
        repo.upsert(
            UpsertCronJobRequest(
                name=job.name,
                handler=USER_HANDLER_NAME,
                schedule=repo.jobs[job.name].schedule,
                enabled=True,
                metadata=updated.encode(),
            )
        )
        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(attempts[1], "新标题补发：\n\n正文")
        self.assertIn("新标题", repo.jobs[job.name].metadata, "投递状态写入覆盖了配置")
        self.assertFalse(repo.jobs[job.name].enabled)

    async def test_delivery_state_write_is_conditional(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="提醒", message="正文", trigger=TriggerDirect)
        )
        state = CronDeliveryState(run_id="run", report_ready=True, report="正文")
        stale = repo.jobs[job.name].id
        repo.jobs[job.name].delivery_token = "other-token"
        job = repo.get_by_name(job.name)
        job.delivery_token = "stale-token"
        active, err = await svc.persist_delivery_state(None, job, state)
        self.assertFalse(active, "令牌不匹配时不得写入")
        self.assertIsNone(err)
        self.assertEqual(repo.jobs[job.name].delivery_state, "")
        self.assertEqual(repo.jobs[job.name].delivery_token, "other-token")
        self.assertEqual(stale, repo.jobs[job.name].id)

        repo.disable_by_name(job.name)
        active, err = await svc.persist_delivery_state(None, job, state)
        self.assertFalse(active, "已禁用的任务不得被状态写入重新启用")
        self.assertIsNone(err)
        self.assertFalse(repo.jobs[job.name].enabled)

    async def test_missed_delivery_failure_notifies_cli_and_keeps_job_enabled(self) -> None:
        repo = FakeCronRepo()
        notifications: list[str] = []

        async def send_target(ctx, target, outputs):
            if outputs[0].text.startswith("cron 补跑失败："):
                notifications.append(outputs[0].text)
                return Receipt()
            raise RuntimeError("platform down")

        svc = self.make_service(store=FakeStore(repo), send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="提醒", message="正文", trigger=TriggerDirect)
        )
        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(len(notifications), 1)
        self.assertIn("cron 补跑失败：user.cron.test", notifications[0])
        self.assertIn("platform down", notifications[0])
        self.assertTrue(repo.jobs[job.name].enabled)


class TestReportSegments(CronTestCase):
    async def test_run_llm_sends_report_segments(self) -> None:
        repo = FakeCronRepo()
        root = os.path.join(self.temp_dir("sandbox"), "sandbox")
        os.makedirs(os.path.join(root, "cron", "test"), exist_ok=True)
        chart = os.path.join(root, "cron", "test", "chart.png")
        with open(chart, "wb") as handle:
            handle.write(b"png")
        runner = FakeRunner(
            text='{"completed":true,"need_report":true,"report":"见图",'
            '"report_segments":[{"type":"image","url":"chart.png"}]}'
        )
        kinds: list[str] = []

        async def send_target(ctx, target, outputs):
            kinds.append(outputs[0].kind)
            if outputs[0].kind == KindImage:
                self.assertEqual(outputs[0].source.path, chart)
            return Receipt()

        svc = self.make_service(
            store=FakeStore(repo), runner=runner, sandbox_root=root, send_target=send_target
        )
        job = upsert_test_cron_job(
            repo,
            legacy_once_metadata(title="LLM", trigger=TriggerLLM, task="test"),
        )
        err = await svc.run_llm(None, job, decode_metadata(job.metadata))
        self.assertIsNone(err)
        self.assertEqual(kinds, [KindText, KindImage])
        updated = must_decode_delivery(repo.jobs["user.cron.test"].delivery_state)
        self.assertEqual(len(updated.report_segments), 1)
        self.assertEqual(updated.report_segments[0].url, "chart.png")

    async def test_missed_once_falls_back_to_text_when_sandbox_attachment_is_missing(self) -> None:
        repo = FakeCronRepo()
        sent: list[object] = []

        async def send_target(ctx, target, outputs):
            sent.append(outputs[0])
            return Receipt()

        svc = self.make_service(
            store=FakeStore(repo), sandbox_root=self.temp_dir("sandbox"), send_target=send_target
        )
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="日报", trigger=TriggerLLM, task="test")
        )
        state = CronDeliveryState(
            run_id="run",
            report_ready=True,
            task_completed=True,
            report="正文",
            report_segments=[MessageSegment(type=SegmentType.IMAGE, url="missing.png")],
            report_session_id="session",
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(state)
        repo.jobs[job.name].delivery_token = "session"

        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].text, "日报补发：\n\n正文")
        self.assertEqual(sent[1].text, "路径 missing.png 附件发送失败")
        self.assertFalse(repo.jobs[job.name].enabled)
        got = must_decode_delivery(repo.jobs[job.name].delivery_state)
        target_state = find_delivery_target_state(got, "cli|superadmins")
        self.assertIsNotNone(target_state)
        self.assertEqual(
            find_delivery_output_status(target_state, "segment:0"), DeliveryFallbackDelivered
        )

    async def test_missed_once_falls_back_to_url_text_after_remote_media_send_fails(self) -> None:
        repo = FakeCronRepo()
        sent: list[object] = []

        async def send_target(ctx, target, outputs):
            sent.append(outputs[0])
            if outputs[0].kind == KindImage:
                raise RuntimeError("media rejected")
            return Receipt()

        svc = self.make_service(store=FakeStore(repo), send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="日报", trigger=TriggerLLM, task="test")
        )
        state = CronDeliveryState(
            run_id="run",
            report_ready=True,
            task_completed=True,
            report="正文",
            report_segments=[
                MessageSegment(type=SegmentType.IMAGE, url="https://example.com/chart.png")
            ],
            report_session_id="session",
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(state)
        repo.jobs[job.name].delivery_token = "session"

        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1].source.url, "https://example.com/chart.png")
        self.assertEqual(sent[2].text, "url https://example.com/chart.png 发送失败")
        self.assertFalse(repo.jobs[job.name].enabled)

    async def test_missed_once_reconnect_retries_only_pending_fallback_text(self) -> None:
        repo = FakeCronRepo()
        counters = {"report": 0, "media": 0, "fallback": 0}
        fallback = "url https://example.com/chart.png 发送失败"

        async def send_target(ctx, target, outputs):
            out = outputs[0]
            if out.kind == KindImage:
                counters["media"] += 1
                raise RuntimeError("media rejected")
            if out.text == fallback:
                counters["fallback"] += 1
                if counters["fallback"] == 1:
                    raise RuntimeError("fallback rejected")
            if out.text == "日报补发：\n\n正文":
                counters["report"] += 1
            return Receipt()

        svc = self.make_service(store=FakeStore(repo), send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo, legacy_once_metadata(title="日报", trigger=TriggerLLM, task="test")
        )
        state = CronDeliveryState(
            run_id="run",
            report_ready=True,
            task_completed=True,
            report="正文",
            report_segments=[
                MessageSegment(type=SegmentType.IMAGE, url="https://example.com/chart.png")
            ],
            report_session_id="session",
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(state)
        repo.jobs[job.name].delivery_token = "session"

        await svc.notify_platform_connected(None, "cli")
        first = must_decode_delivery(repo.jobs[job.name].delivery_state)
        target_state = find_delivery_target_state(first, "cli|superadmins")
        self.assertEqual(
            find_delivery_output_status(target_state, "segment:0"), DeliveryFallbackPending
        )
        self.assertTrue(repo.jobs[job.name].enabled)

        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(counters, {"report": 1, "media": 1, "fallback": 2})
        self.assertFalse(repo.jobs[job.name].enabled)
        final = must_decode_delivery(repo.jobs[job.name].delivery_state)
        target_state = find_delivery_target_state(final, "cli|superadmins")
        self.assertEqual(
            find_delivery_output_status(target_state, "segment:0"), DeliveryFallbackDelivered
        )

    async def test_partial_success_keeps_successful_outputs(self) -> None:
        """逐目标逐输出：成功项保留，失败项等下次连接，不重发已完成项。"""
        repo = FakeCronRepo()
        sends: list[str] = []
        fallback_attempts = {"count": 0}

        async def send_target(ctx, target, outputs):
            out = outputs[0]
            if out.text.startswith("cron 补跑失败："):
                # 补跑失败的超管通知（固定走 cli），不算目标投递。
                return Receipt()
            sends.append(f"{target.platform}|{out.kind}|{out.text}")
            if target.platform == "qqonebot" and out.kind == KindImage:
                raise RuntimeError("qq rejected media")
            if (
                target.platform == "qqonebot"
                and out.text == "url https://example.com/chart.png 发送失败"
            ):
                fallback_attempts["count"] += 1
                if fallback_attempts["count"] == 1:
                    raise RuntimeError("qq rejected fallback")
            return Receipt()

        svc = self.make_service(
            store=FakeStore(repo),
            enabled_platforms=[PlatformTarget(name="qqonebot")],
            send_target=send_target,
        )
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo,
            legacy_once_metadata(
                title="日报", trigger=TriggerLLM, task="test", broadcast=True
            ),
        )
        state = CronDeliveryState(
            run_id="run",
            report_ready=True,
            task_completed=True,
            report="正文",
            report_segments=[
                MessageSegment(type=SegmentType.IMAGE, url="https://example.com/chart.png")
            ],
            report_session_id="session",
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(state)
        repo.jobs[job.name].delivery_token = "session"

        await svc.notify_platform_connected(None, "cli")
        await svc.notify_platform_connected(None, "qqonebot")
        mid = must_decode_delivery(repo.jobs[job.name].delivery_state)
        cli_state = find_delivery_target_state(mid, "cli|private|local")
        qq_state = find_delivery_target_state(mid, "qqonebot|superadmins")
        self.assertEqual(
            find_delivery_output_status(cli_state, "segment:0"), DeliveryDelivered
        )
        self.assertEqual(
            find_delivery_output_status(qq_state, "segment:0"), DeliveryFallbackPending
        )
        self.assertTrue(repo.jobs[job.name].enabled)
        cli_sends = [item for item in sends if item.startswith("cli|")]
        self.assertEqual(len(cli_sends), 2)

        await svc.notify_platform_connected(None, "qqonebot")
        self.assertEqual(
            len([item for item in sends if item.startswith("cli|")]),
            2,
            "已完成的目标不得重发",
        )
        self.assertFalse(repo.jobs[job.name].enabled)
        final = must_decode_delivery(repo.jobs[job.name].delivery_state)
        qq_state = find_delivery_target_state(final, "qqonebot|superadmins")
        self.assertEqual(
            find_delivery_output_status(qq_state, "segment:0"), DeliveryFallbackDelivered
        )


class TestConcurrencyAndInterleaving(CronTestCase):
    def make_sqlite_store(self) -> SqliteCronStore:
        path = os.path.join(self.temp_dir("sqlite"), "test.db")
        store = SqliteCronStore(path)
        self.addCleanup(store.close)
        return store

    async def test_concurrent_platform_connections_generate_once(self) -> None:
        store = self.make_sqlite_store()
        runner = FakeRunner(text='{"completed":true,"need_report":true,"report":"报告"}')

        async def send_target(ctx, target, outputs):
            await asyncio.sleep(0)
            return Receipt()

        svc = self.make_service(
            store=store,
            runner=runner,
            enabled_platforms=[PlatformTarget(name="qqonebot")],
            send_target=send_target,
        )
        self.fixed("2026-01-02 03:05:00")
        meta = legacy_once_metadata(
            title="并发", trigger=TriggerLLM, task="test", broadcast=True
        )
        job = store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name="user.cron.test",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=meta.encode(),
            )
        )
        await asyncio.gather(
            svc.notify_platform_connected(None, "qqonebot"),
            svc.notify_platform_connected(None, "cli"),
        )
        self.assertEqual(runner.calls, 1)
        latest = store.cron_jobs().get_by_name(job.name)
        self.assertFalse(latest.enabled)
        state = decode_delivery_state(latest.delivery_state)
        self.assertTrue(delivery_platform_done(state, "cli"))
        self.assertTrue(delivery_platform_done(state, "qqonebot"))

    async def test_disable_during_missed_delivery_does_not_reenable_job(self) -> None:
        store = self.make_sqlite_store()
        started = asyncio.Event()
        release = asyncio.Event()

        async def send_target(ctx, target, outputs):
            if outputs[0].text == "提醒补发：\n\n正文":
                started.set()
                await release.wait()
            return Receipt()

        svc = self.make_service(store=store, send_target=send_target)
        self.fixed("2026-01-02 03:05:00")
        meta = legacy_once_metadata(title="提醒", message="正文", trigger=TriggerDirect)
        job = store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name="user.cron.test",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=meta.encode(),
            )
        )
        task = asyncio.create_task(svc.notify_platform_connected(None, "cli"))
        await asyncio.wait_for(started.wait(), timeout=5)
        await svc.disable(None, job.name, superadmin())
        release.set()
        await asyncio.wait_for(task, timeout=5)
        latest = store.cron_jobs().get_by_name(job.name)
        self.assertFalse(latest.enabled, "补发把已禁用的任务重新启用了")
        state = decode_delivery_state(latest.delivery_state)
        self.assertEqual(
            state.targets, [], "禁用之后的状态写入必须被 CAS 拒绝（没有任何目标状态落库）"
        )


class TestMigrateLegacyDeliveryState(CronTestCase):
    async def test_migrate_legacy_delivery_state_disables_completed_once_cron(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(
            store=FakeStore(repo),
            enabled_platforms=[PlatformTarget(name="qqonebot", superadmin_ids=["1001"])],
        )
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="旧提醒",
                created_by=CronActor(platform="qqonebot", platform_user_id="1001"),
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerDirect, message="旧报告"),
                target=CronTarget(source_platform="qqonebot"),
            ),
        )
        repo.jobs[job.name].run_count = 1
        repo.jobs[job.name].metadata = with_legacy_delivery_metadata(
            job.metadata,
            LegacyCronDeliveryMetadata(
                completed=True, report="旧报告", delivered_platforms=["qqonebot"]
            ),
        )

        await svc.migrate_legacy_delivery_state(None)
        migrated = repo.jobs[job.name]
        self.assertFalse(migrated.enabled)
        state = must_decode_delivery(migrated.delivery_state)
        self.assertTrue(state.report_ready)
        self.assertTrue(state.task_completed)
        self.assertEqual(state.report, "旧报告")
        self.assertTrue(delivery_platform_done(state, "qqonebot"))
        self.assertEqual(migrated.delivery_token, state.run_id)

        views = await svc.list(None, True, False, superadmin())
        self.assertEqual(views, [], "迁移后已完成的 cron 应默认隐藏")

        await svc.migrate_legacy_delivery_state(None)
        self.assertEqual(repo.jobs[job.name].delivery_token, state.run_id, "迁移必须幂等")

    async def test_migrate_legacy_delivery_state_sqlite(self) -> None:
        path = os.path.join(self.temp_dir("sqlite"), "test.db")
        store = SqliteCronStore(path)
        self.addCleanup(store.close)
        meta = Metadata(
            kind=METADATA_KIND,
            version=1,
            title="旧提醒",
            created_by=CronActor(platform="cli", platform_user_id="local"),
            schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
            trigger=CronTrigger(mode=TriggerDirect, message="旧报告"),
            target=CronTarget(source_platform="cli"),
        )
        job = store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name="user.cron.legacy_sqlite",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=with_legacy_delivery_metadata(
                    meta.encode(),
                    LegacyCronDeliveryMetadata(
                        completed=True, report="旧报告", delivered_platforms=["cli"]
                    ),
                ),
            )
        )
        from atri_x.storage.models import CronJobRunState
        from atri_x.storage.times import now as storage_now

        store.cron_jobs().update_run_state(
            job.id,
            CronJobRunState(
                last_run_at=storage_now(), run_count=1, enabled=True, updated_at=storage_now()
            ),
        )
        svc = self.make_service(store=store)
        await svc.migrate_legacy_delivery_state(None)
        migrated = store.cron_jobs().get_by_name(job.name)
        state = must_decode_delivery(migrated.delivery_state)
        self.assertFalse(migrated.enabled)
        self.assertEqual(migrated.delivery_token, state.run_id)
        self.assertTrue(state.report_ready)
        self.assertTrue(delivery_platform_done(state, "cli"))

    async def test_migrate_legacy_delivery_state_resumes_only_pending_platform(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":true,"need_report":true,"report":"不应执行"}')
        sent_platforms: list[str] = []

        async def send_target(ctx, target, outputs):
            sent_platforms.append(target.platform)
            return Receipt()

        svc = self.make_service(
            store=FakeStore(repo),
            runner=runner,
            enabled_platforms=[
                PlatformTarget(name="qqofficial", superadmin_ids=["1001"]),
                PlatformTarget(name="cli"),
            ],
            send_target=send_target,
        )
        self.fixed("2026-01-02 03:05:00")
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="旧广播",
                created_by=CronActor(platform="qqofficial", platform_user_id="1001"),
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("legacy")),
                target=CronTarget(all_enabled_platforms=True, source_platform="qqofficial"),
            ),
        )
        repo.jobs[job.name].run_count = 1
        repo.jobs[job.name].metadata = with_legacy_delivery_metadata(
            job.metadata,
            LegacyCronDeliveryMetadata(
                completed=True, report="旧报告", delivered_platforms=["qqofficial"]
            ),
        )

        await svc.migrate_legacy_delivery_state(None)
        self.assertTrue(repo.jobs[job.name].enabled, "部分投递的历史任务不得被禁用")
        state = must_decode_delivery(repo.jobs[job.name].delivery_state)
        self.assertTrue(delivery_platform_done(state, "qqofficial"))
        self.assertFalse(delivery_platform_done(state, "cli"))

        await svc.notify_platform_connected(None, "qqofficial")
        self.assertEqual(sent_platforms, [])
        await svc.notify_platform_connected(None, "cli")
        self.assertEqual(sent_platforms, ["cli"])
        self.assertEqual(runner.calls, 0, "历史任务不得重新执行 LLM")
        self.assertFalse(repo.jobs[job.name].enabled)

    async def test_migrate_legacy_delivery_state_skips_fresh_and_current_cycles(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        legacy = LegacyCronDeliveryMetadata(
            completed=True, report="旧报告", delivered_platforms=["cli"]
        )
        base = Metadata(
            kind=METADATA_KIND,
            version=1,
            title="skip",
            schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
            trigger=CronTrigger(mode=TriggerDirect, message="test"),
            target=CronTarget(source_platform="cli"),
        )
        future = repo.upsert(
            UpsertCronJobRequest(
                name="user.cron.future",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=with_legacy_delivery_metadata(base.encode(), legacy),
                next_run_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            )
        )
        repo.jobs[future.name].run_count = 1

        unrun = repo.upsert(
            UpsertCronJobRequest(
                name="user.cron.unrun",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=with_legacy_delivery_metadata(base.encode(), legacy),
            )
        )
        self.assertIsNotNone(unrun)
        periodic_meta = Metadata(
            kind=METADATA_KIND,
            version=1,
            title="skip",
            schedule=CronSchedule(mode=ScheduleCron, cron_expr="0 3 * * *"),
            trigger=CronTrigger(mode=TriggerDirect, message="test"),
            target=CronTarget(source_platform="cli"),
        )
        periodic = repo.upsert(
            UpsertCronJobRequest(
                name="user.cron.periodic",
                handler=USER_HANDLER_NAME,
                schedule="0 3 * * *",
                enabled=True,
                metadata=with_legacy_delivery_metadata(periodic_meta.encode(), legacy),
            )
        )
        repo.jobs[periodic.name].run_count = 1

        current = repo.upsert(
            UpsertCronJobRequest(
                name="user.cron.current",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=with_legacy_delivery_metadata(base.encode(), legacy),
            )
        )
        repo.jobs[current.name].run_count = 1
        repo.jobs[current.name].delivery_token = "current-run"
        repo.jobs[current.name].delivery_state = must_marshal_delivery(
            CronDeliveryState(run_id="current-run", report_ready=True, report="当前报告")
        )

        await svc.migrate_legacy_delivery_state(None)
        for name in (future.name, unrun.name, periodic.name):
            stored = repo.jobs[name]
            self.assertTrue(stored.enabled, f"{name} 不该被迁移")
            self.assertEqual(stored.delivery_state, "")
            self.assertEqual(stored.delivery_token, "")
        stored = repo.jobs[current.name]
        self.assertTrue(stored.enabled)
        self.assertEqual(stored.delivery_token, "current-run")
        self.assertEqual(
            must_decode_delivery(stored.delivery_state).report, "当前报告"
        )


class TestSendAudit(CronTestCase):
    async def test_cron_send_audit_includes_platform(self) -> None:
        events: list[str] = []

        def audit(event, **attrs):
            events.append(f"{event}:{attrs.get('platform', '')}")

        async def send_target(ctx, target, outputs):
            return Receipt()

        svc = self.make_service(
            store=FakeStore(), audit=audit, send_target=send_target
        )
        err = await svc.send_to_platforms(None, "user.cron.audit", ["cli"], "hello")
        self.assertIsNone(err)
        joined = "|".join(events)
        self.assertIn("cron.send_started:cli", joined)
        self.assertIn("cron.send_completed:cli", joined)

    async def test_send_requires_target_sender(self) -> None:
        svc = self.make_service(store=FakeStore())
        err = await svc.send_to_platforms(None, "user.cron.audit", ["cli"], "hello")
        self.assertIsNotNone(err)
        self.assertIn("target sender", str(err))


def _target(key: str, outputs: list[tuple[str, str]]):
    from atri_x.cron import CronDeliveryOutputState, CronDeliveryTargetState

    return CronDeliveryTargetState(
        key=key,
        outputs=[CronDeliveryOutputState(id=oid, status=status) for oid, status in outputs],
    )


async def _ok():
    return Receipt()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
