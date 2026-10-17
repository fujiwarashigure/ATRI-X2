"""``atri_x.app`` 启动基础层测试。

覆盖：``app.go``（RunMode/Options）、``environment.go``（模式解析与剖析器）、
``service_marker.py``（跨平台文件锁标记）、``foundation.py``（配置校验与
foundation 生命周期）、``cron.py``（平台启用判定与异步启动）。

临时目录按 PORTING §7.2 建在仓库根下（``tmp_app_bootstrap_test/``），
tearDown 里 ``shutil.rmtree(ignore_errors=True)``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path

from atri_x.app import app as app_module
from atri_x.app.app import (
    RUN_MODE_AUTO,
    RUN_MODE_CLI_ONLY,
    RUN_MODE_FULL,
    RUN_MODE_SERVICE,
    Options,
    RunMode,
    run_sync,
)
from atri_x.app.cron import (
    enabled_cron_platforms,
    platform_config_enabled,
    start_cron_async,
)
from atri_x.app.environment import (
    DefaultEnvironment,
    StartupProfiler,
    new_startup_profiler,
    platform_mode,
    resolve_run_mode,
    should_start_cron,
    startup_profile_enabled,
)
from atri_x.app.foundation import (
    DefaultFoundationFactory,
    FoundationLifecycle,
    log_startup_configuration,
    validate_work_model,
)
from atri_x.app.service_marker import (
    ServiceMarker,
    claim_service_marker,
    read_service_marker,
    service_marker_path,
    service_marker_running,
)
from atri_x.errors import InvalidError

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp_app_bootstrap_test"
#: 隔离的服务标记运行目录（``TestResolveRunMode`` 专用，避免依赖全局 XDG_RUNTIME_DIR）。
TMP_BOOTSTRAP_RUNTIME = TMP / "runtime"


class RecordingLogger:
    """最小 logger：记录 ``info``/``warn``/``error`` 调用。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def _emit(self, level: str, message: str, **fields):
        self.records.append((level, message, fields))

    def debug(self, message, **fields):
        self._emit("debug", message, **fields)

    def info(self, message, **fields):
        self._emit("info", message, **fields)

    def warn(self, message, **fields):
        self._emit("warn", message, **fields)

    def error(self, message, **fields):
        self._emit("error", message, **fields)

    def with_(self, *args, **fields):
        return self


class TestRunModeAndOptions(unittest.TestCase):
    def test_constants_match_go(self):
        self.assertEqual(str(RUN_MODE_AUTO), "auto")
        self.assertEqual(str(RUN_MODE_FULL), "full")
        self.assertEqual(str(RUN_MODE_CLI_ONLY), "cli")
        self.assertEqual(str(RUN_MODE_SERVICE), "service")

    def test_run_mode_is_str_subclass(self):
        mode = RunMode("full")
        self.assertIsInstance(mode, str)
        self.assertEqual(mode, "full")

    def test_options_defaults(self):
        opts = Options()
        self.assertEqual(opts.config_path, "")
        self.assertEqual(opts.version, "")
        self.assertEqual(opts.mode, RUN_MODE_AUTO)
        self.assertIsInstance(opts.started_at, datetime)
        self.assertIsNotNone(opts.started_at.tzinfo)

    def test_options_started_at_is_utc_aware(self):
        opts = Options(started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(opts.started_at.tzinfo, timezone.utc)

    def test_run_module_has_callable_entry_points(self):
        self.assertTrue(callable(app_module.run))
        self.assertTrue(callable(app_module.run_sync))
        self.assertIs(app_module.Run, app_module.run)


class TestResolveRunMode(unittest.TestCase):
    """``auto`` 模式依赖**服务标记**（``$XDG_RUNTIME_DIR/atri-x/atri-x.pid``）。

    真实事故（第三轮验证）：本机同时有人跑着 ``python -m atri_x service run`` 时，
    ``auto`` 会正确地退到 CLI-only，于是"无服务时回 full"这条用例在本机**变红**——
    用例依赖了全局运行目录。这里把运行目录隔离到仓库内的临时目录，并且在隔离目录里
    把"已有实例持标记"这一分支也真正测一遍。
    """

    def setUp(self) -> None:
        self._old_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        shutil.rmtree(TMP_BOOTSTRAP_RUNTIME, ignore_errors=True)
        TMP_BOOTSTRAP_RUNTIME.mkdir(parents=True, exist_ok=True)
        os.environ["XDG_RUNTIME_DIR"] = str(TMP_BOOTSTRAP_RUNTIME)

    def tearDown(self) -> None:
        if self._old_runtime_dir is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._old_runtime_dir
        shutil.rmtree(TMP_BOOTSTRAP_RUNTIME, ignore_errors=True)

    def test_explicit_modes_pass_through(self):
        self.assertEqual(resolve_run_mode(RUN_MODE_FULL), RUN_MODE_FULL)
        self.assertEqual(resolve_run_mode(RUN_MODE_CLI_ONLY), RUN_MODE_CLI_ONLY)
        self.assertEqual(resolve_run_mode(RUN_MODE_SERVICE), RUN_MODE_SERVICE)

    def test_auto_without_service_falls_back_to_full(self):
        self.assertEqual(resolve_run_mode(RUN_MODE_AUTO), RUN_MODE_FULL)
        self.assertEqual(resolve_run_mode(""), RUN_MODE_FULL)

    def test_auto_with_running_service_falls_back_to_cli_only(self):
        marker = claim_service_marker()
        try:
            self.assertEqual(resolve_run_mode(RUN_MODE_AUTO), RUN_MODE_CLI_ONLY)
        finally:
            marker.close()

    def test_unknown_mode_raises(self):
        with self.assertRaises(InvalidError):
            resolve_run_mode("banana")

    def test_platform_mode_mapping(self):
        self.assertEqual(platform_mode(RUN_MODE_CLI_ONLY), "cli")
        self.assertEqual(platform_mode(RUN_MODE_SERVICE), "service")
        self.assertEqual(platform_mode(RUN_MODE_FULL), "full")
        self.assertEqual(platform_mode("whatever"), "full")

    def test_should_start_cron_skips_cli_only(self):
        self.assertFalse(should_start_cron(RUN_MODE_CLI_ONLY))
        self.assertTrue(should_start_cron(RUN_MODE_FULL))
        self.assertTrue(should_start_cron(RUN_MODE_SERVICE))


class TestStartupProfiler(unittest.TestCase):
    def test_profile_enabled_only_on_debug(self):
        self.assertTrue(startup_profile_enabled("debug"))
        self.assertTrue(startup_profile_enabled(" DEBUG "))
        self.assertFalse(startup_profile_enabled("info"))
        self.assertFalse(startup_profile_enabled(""))

    def test_entries_only_recorded_when_enabled(self):
        profiler = new_startup_profiler(datetime.now(timezone.utc))
        profiler.mark("disabled")
        self.assertEqual(profiler.entries, [])
        profiler.set_enabled(True)
        profiler.mark("enabled")
        self.assertEqual([entry.name for entry in profiler.entries], ["enabled"])
        total = profiler.flush()
        self.assertGreaterEqual(total, 0.0)

    def test_zero_started_at_defaults_to_now(self):
        profiler = new_startup_profiler(None)
        self.assertIsNotNone(profiler.started_at.tzinfo)

    def test_go_named_aliases_exist(self):
        profiler = StartupProfiler()
        for name in ("SetEnabled", "Mark", "Flush"):
            self.assertTrue(hasattr(profiler, name))


class TestDefaultEnvironment(unittest.TestCase):
    def test_resolve_mode_is_awaitable_result(self):
        env = DefaultEnvironment()
        mode = asyncio.run(env.resolve_mode(RUN_MODE_FULL))
        self.assertEqual(mode, RUN_MODE_FULL)

    def test_claim_service_marker_skipped_outside_service_mode(self):
        env = DefaultEnvironment()

        async def run():
            return await env.claim_service_marker(RUN_MODE_FULL)

        self.assertIsNone(asyncio.run(run()))

    def test_startup_profiler_factory(self):
        env = DefaultEnvironment()
        profiler = env.new_startup_profiler(None)
        self.assertIsInstance(profiler, StartupProfiler)


class TestServiceMarker(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)
        self._saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = str(TMP)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._saved
        shutil.rmtree(TMP, ignore_errors=True)

    def test_path_matches_go_layout(self):
        expected = os.path.join(str(TMP), "atri-x", "atri-x.pid")
        self.assertEqual(service_marker_path(), expected)

    def test_claim_writes_pid_and_blocks_second_claim(self):
        marker = claim_service_marker()
        try:
            self.assertIsInstance(marker, ServiceMarker)
            self.assertEqual(read_service_marker(service_marker_path()), os.getpid())
            self.assertTrue(service_marker_running())
            with self.assertRaises(Exception):
                claim_service_marker()
        finally:
            marker.close()

    def test_close_is_idempotent(self):
        marker = claim_service_marker()
        marker.close()
        marker.close()
        self.assertTrue(marker.closed)

    def test_running_is_false_when_no_marker(self):
        self.assertFalse(service_marker_running())

    def test_read_invalid_marker_returns_zero(self):
        path = Path(service_marker_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-a-pid\n", encoding="utf-8")
        self.assertEqual(read_service_marker(str(path)), 0)


class FakeConfig:
    """最小配置替身：只提供 foundation 校验用到的字段。"""

    def __init__(self, work_provider="openai", work_model="gpt-4o"):
        self.config_path = str(TMP / "app.toml")
        self.services_config_path = ""
        self.providers_config_path = str(TMP / "providers.toml")
        self.state_config_path = str(TMP / "state.toml")
        self.atrinis_config_path = ""
        self.mode_models = {
            "work": _Selection(work_provider, work_model),
            "chat": _Selection("openai", "gpt-4o-mini"),
        }
        self.providers = {"openai": object()}
        self.soul = _Soul()
        self.storage = _Storage()
        self.runtime = _RuntimeConfig()


class _Selection:
    def __init__(self, provider, model):
        self.provider = provider
        self.model = model


class _Soul:
    path = "/tmp/soul.md"


class _Storage:
    sessions_sqlite_path = str(TMP / "sessions.db")
    chat_history_sqlite_path = str(TMP / "history.db")


class _RuntimeConfig:
    log_level = "info"
    log_retention_days = 30


class TestValidateWorkModel(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_valid_config_passes(self):
        validate_work_model(FakeConfig())

    def test_missing_work_model_raises(self):
        with self.assertRaises(InvalidError):
            validate_work_model(FakeConfig(work_provider="", work_model=""))

    def test_unknown_provider_raises(self):
        with self.assertRaises(InvalidError):
            validate_work_model(FakeConfig(work_provider="nope"))


class TestLogStartupConfiguration(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_keys_match_go_struct(self):
        logger = RecordingLogger()
        log_startup_configuration(logger, Options(version="1.2.3"), FakeConfig())
        self.assertEqual(len(logger.records), 1)
        level, message, fields = logger.records[0]
        self.assertEqual(level, "info")
        self.assertEqual(message, "atri-x started")
        self.assertEqual(fields["version"], "1.2.3")
        self.assertEqual(fields["work_provider"], "openai")
        self.assertEqual(fields["work_model"], "gpt-4o")
        self.assertEqual(fields["chat_provider"], "openai")
        self.assertEqual(fields["soul_path"], "/tmp/soul.md")
        self.assertEqual(fields["sessions_sqlite_path"], _Storage.sessions_sqlite_path)
        self.assertIn("state_config_path", fields)


class FakeCronManager:
    def __init__(self):
        self.stopped = asyncio.Event()

    async def stop(self):
        self.stopped.set()


class TestFoundationLifecycle(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_close_is_safe_without_components(self):
        lifecycle = FoundationLifecycle(FakeConfig(), None)
        asyncio.run(lifecycle.close(None))

    def test_close_stops_cron_manager(self):
        manager = FakeCronManager()
        lifecycle = FoundationLifecycle(FakeConfig(), None)
        lifecycle.cron_manager = manager

        async def run():
            await lifecycle.close(None)

        asyncio.run(run())
        self.assertTrue(manager.stopped.is_set())

    def test_start_cron_is_idempotent(self):
        lifecycle = FoundationLifecycle(FakeConfig(), RecordingLogger())
        lifecycle.cron_manager = FakeCronManager()

        async def fake_setup(ctx, manager, cfg):
            return None

        import atri_x.app.cron as cron_module

        original = cron_module.setup_cron
        cron_module.setup_cron = fake_setup
        try:

            async def run():
                await lifecycle.start_cron(None, object())
                first = lifecycle._cron_task
                await lifecycle.start_cron(None, object())
                self.assertIs(lifecycle._cron_task, first)
                if first is not None:
                    await first

            asyncio.run(run())
        finally:
            cron_module.setup_cron = original
        self.assertTrue(lifecycle.cron_scheduled)

    def test_start_cron_requires_manager_and_service(self):
        lifecycle = FoundationLifecycle(FakeConfig(), None)
        asyncio.run(lifecycle.start_cron(None, object()))
        self.assertFalse(lifecycle.cron_scheduled)


class TestCronHelpers(unittest.TestCase):
    def test_platform_config_enabled_only_accepts_true_bool(self):
        self.assertTrue(platform_config_enabled({"enabled": True}))
        self.assertFalse(platform_config_enabled({"enabled": False}))
        self.assertFalse(platform_config_enabled({"enabled": "true"}))
        self.assertFalse(platform_config_enabled({}))
        self.assertFalse(platform_config_enabled(None))

    def test_enabled_cron_platforms_skips_disabled(self):
        cfg = FakeConfig()
        cfg.platform = {
            "cli": {"enabled": True},
            "telegram": {"enabled": False},
            "qq": {},
        }
        cfg.security = _Security()
        targets = enabled_cron_platforms(cfg)
        self.assertEqual([target.name for target in targets], ["cli"])
        self.assertEqual(targets[0].superadmin_ids, ["u1"])

    def test_enabled_cron_platforms_handles_none(self):
        self.assertEqual(enabled_cron_platforms(None), [])

    def test_start_cron_async_signals_done(self):
        calls = []

        async def fake_setup(ctx, manager, cfg):
            calls.append((ctx, manager, cfg))

        import atri_x.app.cron as cron_module

        original = cron_module.setup_cron
        cron_module.setup_cron = fake_setup
        try:

            async def run():
                done = asyncio.Event()
                task = start_cron_async("ctx", "manager", "service", "cfg", None, done)
                await asyncio.wait_for(done.wait(), 5)
                await task

            asyncio.run(run())
        finally:
            cron_module.setup_cron = original
        self.assertEqual(calls, [("ctx", "manager", "cfg")])


class _Security:
    superadmins = {"cli": ["u1"]}


class TestDefaultFoundationFactoryShape(unittest.TestCase):
    def test_build_is_coroutine(self):
        import inspect

        factory = DefaultFoundationFactory()
        self.assertTrue(inspect.iscoroutinefunction(factory.build))
        self.assertTrue(hasattr(factory, "Build"))


if __name__ == "__main__":
    unittest.main()
