"""``atri_x.maintenance`` 的清理任务与 cron 装配测试。

覆盖：沙盒清理（真实文件系统）、各项 ``run_*`` 的前置条件与错误吞并语义、
cron 装配（7 个 handler + upsert/disable 分支）。

**为什么不 import atri_x.cron**：``atri_x.cron`` 目前 import 即抛
``NameError``（``cron/model.py`` 的既有问题，不属于本模块）。``setup_cron`` 在
**调用点**才 import ``UpsertJobRequest``，因此测试用
``sys.modules["atri_x.cron"]`` 注入替身来验证装配逻辑——这也顺带证明
``atri_x.maintenance`` 不依赖那个坏模块就能工作。

共享夹具见 ``tests/_maintenance_support.py``。
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from atri_x.config import (
    AppConfig,
    ChatHistoryCleanupConfig,
    CronTaskConfig,
    DailyReportConfig,
    MaintenanceCleanupConfig,
)
from atri_x.errors import NotFoundError
from atri_x.maintenance import (
    Service,
    cleanup_sandbox,
    new_service,
    new_service_with_config,
    remove_empty_dirs,
    setup_cron,
    upsert_or_disable,
)

from tests._maintenance_support import _AsyncCaseWithTmp, _CaseWithTmp


class TestSandboxCleanup(_AsyncCaseWithTmp):
    async def test_removes_only_expired_files_and_empty_dirs(self) -> None:
        root = self.path("sandbox")
        old = self.write_file(self.path("sandbox", "old", "a.txt"), b"a", age_days=10)
        fresh = self.write_file(self.path("sandbox", "fresh", "b.txt"), b"b", age_days=1)
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        deleted = await cleanup_sandbox(None, root, cutoff)
        self.assertEqual(deleted, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        # old/ 变成空目录 → 被删；fresh/ 仍非空 → 保留。
        self.assertFalse(os.path.exists(os.path.join(root, "old")))
        self.assertTrue(os.path.exists(os.path.join(root, "fresh")))

    async def test_missing_dir_is_not_an_error(self) -> None:
        self.assertEqual(
            await cleanup_sandbox(None, self.path("nope"), datetime.now(timezone.utc)), 0
        )
        self.assertEqual(await cleanup_sandbox(None, "", datetime.now(timezone.utc)), 0)

    async def test_cancelled_ctx_raises(self) -> None:
        root = self.path("sandbox")
        self.write_file(os.path.join(root, "x.txt"), b"x", age_days=10)
        from atri_x.safecontext import CancelledError, with_cancel

        token = with_cancel(None)
        token.cancel("test")
        with self.assertRaises(CancelledError):
            await cleanup_sandbox(token, root, datetime.now(timezone.utc))

    async def test_remove_empty_dirs_ignores_failures(self) -> None:
        root = self.path("tree")
        self.write_file(os.path.join(root, "a", "b", "keep.txt"), b"k")
        remove_empty_dirs(root)
        # 有文件的目录链必须保留。
        self.assertTrue(os.path.exists(os.path.join(root, "a", "b", "keep.txt")))
        os.remove(os.path.join(root, "a", "b", "keep.txt"))
        os.rmdir(os.path.join(root, "a", "b"))
        os.rmdir(os.path.join(root, "a"))
        os.makedirs(os.path.join(root, "c", "d"))
        remove_empty_dirs(root)
        self.assertFalse(os.path.exists(os.path.join(root, "c")))


class _FakeLogs:
    def __init__(self, log_dir: str, retention: int = 30, fail: bool = False) -> None:
        self._dir = log_dir
        self._retention = retention
        self.cleaned = 0
        self.fail = fail

    def log_dir(self) -> str:
        return self._dir

    def retention_days(self) -> int:
        return self._retention

    def cleanup_old_logs(self) -> None:
        if self.fail:
            raise OSError("cannot clean logs")
        self.cleaned += 1


class _FakeDeleteBefore:
    def __init__(self, deleted: int = 0, fail: bool = False) -> None:
        self.deleted = deleted
        self.calls: list[datetime] = []
        self.fail = fail

    def delete_before(self, cutoff: datetime) -> int:
        self.calls.append(cutoff)
        if self.fail:
            raise OSError("delete failed")
        return self.deleted


class _FakeMedia:
    def __init__(self) -> None:
        self.cleaned = 0
        self.reconciled = 0

    async def cleanup(self, ctx=None) -> None:  # noqa: ANN001
        self.cleaned += 1

    async def reconcile_history(self, ctx=None) -> None:  # noqa: ANN001
        self.reconciled += 1


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, tuple]] = []

    def info(self, msg: str, *attrs) -> None:  # noqa: ANN001
        self.records.append(("info", msg, attrs))

    def warn(self, msg: str, *attrs) -> None:  # noqa: ANN001
        self.records.append(("warn", msg, attrs))

    def messages(self) -> list[str]:
        return [msg for _level, msg, _attrs in self.records]


class TestServiceCleanups(_AsyncCaseWithTmp):
    def _service(self, **kwargs) -> Service:
        logger = _RecordingLogger()
        service = new_service(
            kwargs.get("logs", _FakeLogs(self.path("logs"))),
            kwargs.get("store", None),
            kwargs.get("session_cleanup", MaintenanceCleanupConfig()),
            logger,
        )
        service.logger = logger
        return service

    def test_new_service_without_config_uses_defaults(self) -> None:
        service = new_service_with_config(None, None, None, None, None, None)
        self.assertEqual(service.session_cleanup.retention_days, 0)
        self.assertFalse(service.session_cleanup.enabled)
        self.assertEqual(service.report.window_hours, 12)

    def test_new_service_with_config_reads_all_sections(self) -> None:
        cfg = AppConfig()
        cfg.maintenance.session_cleanup = MaintenanceCleanupConfig(
            enabled=True, schedule="1 2 * * *", retention_days=7
        )
        cfg.maintenance.sandbox_cleanup = MaintenanceCleanupConfig(
            enabled=True, retention_days=3
        )
        cfg.maintenance.chat_history_cleanup = ChatHistoryCleanupConfig(
            enabled=True, retention_days=90
        )
        cfg.maintenance.privacy_cleanup = MaintenanceCleanupConfig(enabled=True)
        cfg.sandbox.root = self.path("sandbox")
        cfg.angel_memory.retention_days = 11
        cfg.self_learning.retention_days = 22
        service = new_service_with_config(
            _FakeLogs(self.path("logs")), object(), object(), object(), cfg, None
        )
        self.assertEqual(service.session_cleanup.retention_days, 7)
        self.assertEqual(service.sandbox_root, self.path("sandbox"))
        self.assertEqual(service.sandbox_cleanup.retention_days, 3)
        self.assertEqual(service.chat_history_cleanup.retention_days, 90)
        self.assertTrue(service.privacy_cleanup.enabled)
        self.assertEqual(service.angel_memory_retention_days, 11)
        self.assertEqual(service.self_learning_retention_days, 22)

    async def test_log_cleanup(self) -> None:
        logs = _FakeLogs(self.path("logs"))
        service = self._service(logs=logs)
        await service.run_log_cleanup(None)
        self.assertEqual(logs.cleaned, 1)
        self.assertIn("maintenance log cleanup completed", service.logger.messages())

    async def test_log_cleanup_failure_propagates(self) -> None:
        service = self._service(logs=_FakeLogs(self.path("logs"), fail=True))
        with self.assertRaises(OSError):
            await service.run_log_cleanup(None)
        self.assertIn("maintenance log cleanup failed", service.logger.messages())

    async def test_session_cleanup_skipped_when_disabled(self) -> None:
        service = self._service()
        await service.run_session_cleanup(None)
        self.assertNotIn("maintenance session cleanup completed", service.logger.messages())

    async def test_session_cleanup_runs_when_enabled(self) -> None:
        from atri_x.storage import sqlite
        from atri_x.storage import Session, SessionStatusActive

        store = sqlite.new(self.path("sessions.db"))
        self.addCleanup(store.close)
        store.sessions().create(
            Session(
                id="s1",
                owner_id="cli:1",
                platform="cli",
                platform_scope_id="private:1",
                mode="work",
                status=SessionStatusActive,
                title="t",
                updated_at=datetime.now(timezone.utc) - timedelta(days=100),
            )
        )
        service = self._service(
            store=store,
            session_cleanup=MaintenanceCleanupConfig(enabled=True, retention_days=7),
        )
        await service.run_session_cleanup(None)
        self.assertIn("maintenance session cleanup completed", service.logger.messages())
        self.assertTrue(
            any(name == "deleted" and value == 1 for _level, _msg, attrs in service.logger.records for name, value in zip(attrs[0::2], attrs[1::2]))
        )

    async def test_chat_history_cleanup_skips_without_config(self) -> None:
        service = self._service()
        service.chat_history = _FakeDeleteBefore(5)
        await service.run_chat_history_cleanup(None)
        self.assertEqual(service.chat_history.calls, [])

    async def test_chat_history_cleanup_deletes_and_reconciles(self) -> None:
        service = self._service()
        service.chat_history_cleanup = ChatHistoryCleanupConfig(enabled=True, retention_days=30)
        history = _FakeDeleteBefore(4)
        outbound = _FakeDeleteBefore(2)
        media = _FakeMedia()
        service.chat_history = history
        service.outbound_messages = outbound
        service.media = media
        await service.run_chat_history_cleanup(None)
        self.assertEqual(len(history.calls), 1)
        self.assertEqual(history.calls[0], outbound.calls[0])
        self.assertEqual(media.reconciled, 1)
        self.assertIn("maintenance chat history cleanup completed", service.logger.messages())

    async def test_chat_history_cleanup_propagates_history_failure(self) -> None:
        service = self._service()
        service.chat_history_cleanup = ChatHistoryCleanupConfig(enabled=True, retention_days=30)
        service.chat_history = _FakeDeleteBefore(fail=True)
        with self.assertRaises(OSError):
            await service.run_chat_history_cleanup(None)
        self.assertIn("maintenance chat history cleanup failed", service.logger.messages())

    async def test_privacy_cleanup_selects_cutoff_by_name(self) -> None:
        service = self._service()
        service.angel_memory_retention_days = 5
        service.self_learning_retention_days = 0  # 0 → 跳过 learning
        seen: dict[str, datetime] = {}

        async def _angel(ctx, cutoff):  # noqa: ANN001
            seen["angel"] = cutoff

        def _learning(ctx, cutoff):  # noqa: ANN001
            seen["learning"] = cutoff

        async def _other(ctx, cutoff):  # noqa: ANN001
            seen["other"] = cutoff

        service.register_retention_cleanup("angel-memory", _angel)
        service.register_retention_cleanup("self-learning", _learning)
        service.register_retention_cleanup("group-kb", _other)
        await service.run_privacy_cleanup(None)
        self.assertIn("angel", seen)
        self.assertNotIn("learning", seen)
        self.assertIn("other", seen)
        now = datetime.now(timezone.utc)
        self.assertLess((now - seen["angel"]).days, 6)
        self.assertGreater((now - seen["other"]).days, 179)
        # features 只数成功项（angel + other）。
        attrs = dict(
            zip(
                service.logger.records[-1][2][0::2],
                service.logger.records[-1][2][1::2],
            )
        )
        self.assertEqual(attrs["features"], 2)

    async def test_privacy_cleanup_continues_after_failure(self) -> None:
        service = self._service()
        called: list[str] = []

        async def _boom(ctx, cutoff):  # noqa: ANN001
            called.append("boom")
            raise OSError("feature exploded")

        async def _ok(ctx, cutoff):  # noqa: ANN001
            called.append("ok")

        service.register_retention_cleanup("boom", _boom)
        service.register_retention_cleanup("ok", _ok)
        await service.run_privacy_cleanup(None)
        self.assertEqual(called, ["boom", "ok"])
        self.assertIn("maintenance privacy cleanup failed", service.logger.messages())

    async def test_privacy_cleanup_noop_without_callbacks(self) -> None:
        service = self._service()
        await service.run_privacy_cleanup(None)
        self.assertIn("maintenance privacy cleanup completed", service.logger.messages())

    async def test_register_retention_cleanup_ignores_none(self) -> None:
        service = self._service()
        service.register_retention_cleanup("x", None)
        self.assertEqual(service.retention_cleanups, [])

    async def test_sandbox_cleanup_skipped_without_root_or_retention(self) -> None:
        service = self._service()
        await service.run_sandbox_cleanup(None)
        service.sandbox_root = self.path("sandbox")
        await service.run_sandbox_cleanup(None)
        self.assertNotIn("maintenance sandbox cleanup completed", service.logger.messages())

    async def test_sandbox_cleanup_runs(self) -> None:
        root = self.path("sandbox")
        self.write_file(os.path.join(root, "stale.txt"), b"s", age_days=30)
        self.write_file(os.path.join(root, "new.txt"), b"n")
        service = self._service()
        service.sandbox_root = root
        service.sandbox_cleanup = MaintenanceCleanupConfig(enabled=True, retention_days=7)
        await service.run_sandbox_cleanup(None)
        self.assertFalse(os.path.exists(os.path.join(root, "stale.txt")))
        self.assertTrue(os.path.exists(os.path.join(root, "new.txt")))
        self.assertIn("maintenance sandbox cleanup completed", service.logger.messages())


@dataclass
class _FakeUpsertJobRequest:
    name: str = ""
    handler: str = ""
    schedule: str = ""
    enabled: bool = False


class _FakeCronManager:
    def __init__(self, disable_error: BaseException | None = None) -> None:
        self.handlers: dict[str, object] = {}
        self.registered: list[str] = []
        self.upserted: list[_FakeUpsertJobRequest] = []
        self.disabled: list[str] = []
        self.started: list[object] = []
        self.disable_error = disable_error

    async def register_handler(self, name: str, handler) -> None:  # noqa: ANN001
        self.handlers[name] = handler
        self.registered.append(name)

    async def upsert_job(self, req):  # noqa: ANN001
        self.upserted.append(req)
        return req

    async def disable_job(self, name: str) -> None:
        if self.disable_error is not None:
            raise self.disable_error
        self.disabled.append(name)

    async def start(self, ctx=None) -> None:  # noqa: ANN001
        self.started.append(ctx)


class TestCronAssembly(_AsyncCaseWithTmp):
    def setUp(self) -> None:
        super().setUp()
        # 只用字段名与 Go 的 UpsertJobRequest 对齐的最小替身。
        fake = types.ModuleType("atri_x.cron")
        fake.UpsertJobRequest = _FakeUpsertJobRequest  # type: ignore[attr-defined]
        self._previous = sys.modules.get("atri_x.cron")
        sys.modules["atri_x.cron"] = fake
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._previous is None:
            sys.modules.pop("atri_x.cron", None)
        else:
            sys.modules["atri_x.cron"] = self._previous

    async def test_registers_seven_handlers(self) -> None:
        service = new_service(None, None, None, None)
        manager = _FakeCronManager()
        await service.register_cron_handlers(manager)
        self.assertEqual(
            manager.registered,
            [
                "maintenance.log_cleanup",
                "maintenance.session_cleanup",
                "maintenance.sandbox_cleanup",
                "maintenance.media_cleanup",
                "maintenance.chat_history_cleanup",
                "maintenance.privacy_cleanup",
                "maintenance.daily_report",
            ],
        )

    async def test_registered_handlers_delegate(self) -> None:
        service = new_service(None, None, None, None)
        calls: list[str] = []

        async def _fake_log_cleanup(ctx):  # noqa: ANN001
            calls.append("log")

        service.run_log_cleanup = _fake_log_cleanup  # type: ignore[method-assign]
        manager = _FakeCronManager()
        await service.register_cron_handlers(manager)
        # cron 的 Handler 约定：``handler(ctx, job)`` 可返回 awaitable，Manager 会 await。
        await manager.handlers["maintenance.log_cleanup"](None, None)  # type: ignore[operator]
        self.assertEqual(calls, ["log"])

    async def test_media_handler_noop_without_media(self) -> None:
        service = new_service(None, None, None, None)
        manager = _FakeCronManager()
        await service.register_cron_handlers(manager)
        await manager.handlers["maintenance.media_cleanup"](None, None)  # type: ignore[operator]

    async def test_media_handler_calls_cleanup(self) -> None:
        service = new_service(None, None, None, None)
        media = _FakeMedia()
        service.media = media
        manager = _FakeCronManager()
        await service.register_cron_handlers(manager)
        await manager.handlers["maintenance.media_cleanup"](None, None)  # type: ignore[operator]
        self.assertEqual(media.cleaned, 1)

    async def test_register_without_manager_is_noop(self) -> None:
        service = new_service(None, None, None, None)
        await service.register_cron_handlers(None)

    async def test_setup_cron_upserts_enabled_and_disables_rest(self) -> None:
        cfg = AppConfig()
        cfg.maintenance.log_cleanup = CronTaskConfig(enabled=True, schedule="0 3 * * *")
        cfg.maintenance.session_cleanup = MaintenanceCleanupConfig(
            enabled=False, schedule="15 3 * * *", retention_days=30
        )
        cfg.maintenance.sandbox_cleanup = MaintenanceCleanupConfig(
            enabled=True, schedule="0 4 * * *", retention_days=7
        )
        cfg.maintenance.chat_history_cleanup = ChatHistoryCleanupConfig(
            enabled=False, schedule="35 4 * * *", retention_days=180
        )
        cfg.maintenance.privacy_cleanup = MaintenanceCleanupConfig(
            enabled=False, schedule="", retention_days=0
        )
        cfg.maintenance.daily_report = DailyReportConfig(enabled=False, schedule="0 9 * * *")
        manager = _FakeCronManager()
        await setup_cron(None, manager, cfg)
        self.assertEqual(
            [(job.name, job.handler, job.schedule) for job in manager.upserted],
            [
                ("system.maintenance.log_cleanup", "maintenance.log_cleanup", "0 3 * * *"),
                ("system.maintenance.sandbox_cleanup", "maintenance.sandbox_cleanup", "0 4 * * *"),
                # media_cleanup 硬编码启用，schedule 沿用 sandbox cleanup。
                ("system.maintenance.media_cleanup", "maintenance.media_cleanup", "0 4 * * *"),
            ],
        )
        self.assertTrue(all(job.enabled for job in manager.upserted))
        self.assertEqual(
            manager.disabled,
            [
                "system.maintenance.session_cleanup",
                "system.maintenance.chat_history_cleanup",
                "system.maintenance.privacy_cleanup",
                "system.maintenance.daily_report",
            ],
        )
        self.assertEqual(len(manager.started), 1)

    async def test_setup_cron_ignores_missing_job_on_disable(self) -> None:
        cfg = AppConfig()
        manager = _FakeCronManager(disable_error=NotFoundError("not found"))
        await setup_cron(None, manager, cfg)
        self.assertEqual(len(manager.disabled), 0)
        self.assertEqual(len(manager.started), 1)

    async def test_setup_cron_propagates_other_disable_errors(self) -> None:
        cfg = AppConfig()
        manager = _FakeCronManager(disable_error=OSError("cron db locked"))
        with self.assertRaises(OSError):
            await setup_cron(None, manager, cfg)

    async def test_setup_cron_without_manager_or_config(self) -> None:
        await setup_cron(None, None, AppConfig())
        await setup_cron(None, _FakeCronManager(), None)

    async def test_upsert_or_disable_direct_call(self) -> None:
        manager = _FakeCronManager()
        await upsert_or_disable(None, manager, True, "n", "h", "s")
        self.assertEqual(manager.upserted[0].name, "n")
        await upsert_or_disable(None, manager, False, "n", "h", "s")
        self.assertEqual(manager.disabled, ["n"])


if __name__ == "__main__":
    unittest.main()
