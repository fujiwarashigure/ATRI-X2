"""平台装配、集成助手与诊断面测试。

覆盖：``atri_x/app/platforms.py``（``runPlatforms`` 的三种模式、Hook 告警目标、
超管失败通知、注册助手）、``atri_x/app/integrations.py``（令牌解析）、
``atri_x/app/platform_caller.py``、``atri_x/app/config_check.py``、
``atri_x/app/doctor.py`` + ``doctor_checks.py``、``atri_x/app/image_rewriter.py``、
``atri_x/app/cli_client.py`` 的纯函数部分。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

from atri_x.app.cli_client import is_local_cli_url
from atri_x.app.config_check import check_config_warnings
from atri_x.app.doctor import (
    DoctorCheck,
    DoctorOptions,
    DoctorReport,
    format_doctor_report,
)
from atri_x.app.doctor_checks import (
    check_writable_dirs,
    doctor_platform_connection,
    enabled_platform_names,
    first_line,
    resolve_doctor_api_key,
    snapshot_has_platform,
)
from atri_x.app.image_rewriter import (
    IMAGE_REWRITE_SYSTEM_NATURAL,
    IMAGE_REWRITE_SYSTEM_TAGS,
    ImagePromptRewriter,
    build_rewrite_prompt,
    dialect_label,
    image_rewrite_system_for,
    image_rewrite_target_line,
)
from atri_x.app.integrations import DefaultIntegrationFactory, resolve_atrinis_tokens
from atri_x.app.platform_caller import (
    HookPlatformCallerResolver,
    PlatformCallerResolver,
    resolve_platform_caller,
)
from atri_x.app.platforms import (
    AtrinisRuntimeAdapter,
    DefaultPlatformExecutor,
    deliver_hook_notices,
    hook_notice_target,
    notify_platform_failure,
    platform_stops_app_on_exit,
    register_command_catalogs,
    register_completion_platforms,
    register_platform_hooks,
    run_platforms,
    runtime_names,
)
from atri_x.app.service_types import ImagePromptRewriteRequest
from atri_x.errors import ErrCLIClientFallback, InvalidError

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp_app_platforms_test"


class RecordingLogger:
    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def _emit(self, level, message, **fields):
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


class FakeRuntime:
    def __init__(self, name, run=None, stop_on_exit=False, connect_notifier=True):
        self._name = name
        self._run = run
        self._stop_on_exit = stop_on_exit
        self.calls = 0
        self.notifier = None
        self.completer = None
        self.catalog = None
        self.caller = True

    def name(self):
        return self._name

    async def run(self, ctx, handler):
        self.calls += 1
        if self._run is not None:
            await self._run(ctx, handler)

    def stop_app_on_exit(self):
        return self._stop_on_exit

    def set_connect_notifier(self, fn):
        self.notifier = fn

    def set_completer(self, service):
        self.completer = service

    def set_command_catalog(self, infos):
        self.catalog = infos


class FakeAgent:
    def __init__(self):
        self.senders = []
        self.notices = []
        self.platform_events = 0

    def register_platform_sender(self, name, sender):
        self.senders.append((name, sender))

    async def send_notice(self, ctx, notice):
        self.notices.append(notice)

    def completion_service(self):
        return "completer"

    def command_infos(self):
        return ["info"]

    async def start_platform_events(self, ctx):
        self.platform_events += 1


class TestRunPlatforms(unittest.TestCase):
    def test_returns_when_all_runtimes_exit_in_full_mode(self):
        runtimes = [FakeRuntime("cli")]
        asyncio.run(
            run_platforms(None, None, RecordingLogger(), runtimes, mode="full")
        )
        self.assertEqual(runtimes[0].calls, 1)

    def test_unexpected_failure_invokes_callback(self):
        async def boom(ctx, handler):
            # 让其它运行时先登记进 running 集合（task 首次调度）。
            await asyncio.sleep(0)
            raise InvalidError("platform died")

        failures = []

        async def on_failure(ctx, name, still_running, err):
            failures.append((name, list(still_running), err))

        runtimes = [FakeRuntime("cli", boom), FakeRuntime("telegram", self._wait)]
        asyncio.run(
            run_platforms(
                None, None, RecordingLogger(), runtimes, mode="full", failure=on_failure
            )
        )
        self.assertEqual(failures[0][0], "cli")
        self.assertEqual(failures[0][1], ["telegram"])

    @staticmethod
    async def _wait(ctx, handler):
        await asyncio.sleep(0.05)

    def test_cli_exit_with_stop_app_on_exit_cancels_run(self):
        runtimes = [FakeRuntime("cli", stop_on_exit=True), FakeRuntime("telegram", self._wait)]
        asyncio.run(run_platforms(None, None, RecordingLogger(), runtimes, mode="full"))

    def test_no_platform_keeps_alive_until_stop(self):
        stop = asyncio.Event()
        logger = RecordingLogger()

        async def run():
            task = asyncio.ensure_future(
                run_platforms(stop, None, logger, [], mode="service")
            )
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            stop.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        messages = [message for _lvl, message, _f in logger.records]
        self.assertIn(
            "no platform is enabled; service stays alive so health endpoints remain "
            "available until a platform is configured",
            messages,
        )

    def test_service_mode_survives_all_runtimes_exiting(self):
        stop = asyncio.Event()

        async def run():
            task = asyncio.ensure_future(
                run_platforms(stop, None, RecordingLogger(), [FakeRuntime("cli")], mode="service")
            )
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "service 模式不能因平台退出而结束进程")
            stop.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())

    def test_heartbeat_and_after_start_called(self):
        beats = []

        async def after_start(ctx):
            beats.append("after_start")

        runtimes = [FakeRuntime("cli")]
        asyncio.run(
            run_platforms(
                None,
                None,
                RecordingLogger(),
                runtimes,
                after_start=after_start,
                heartbeat=lambda: beats.append("beat"),
                mode="full",
            )
        )
        self.assertIn("after_start", beats)
        self.assertIn("beat", beats)


class TestPlatformHelpers(unittest.TestCase):
    def test_runtime_names_skips_atrinis_and_blanks(self):
        runtimes = [FakeRuntime("cli"), FakeRuntime("atrinis"), FakeRuntime(""), None]
        self.assertEqual(runtime_names(runtimes), ["cli"])

    def test_hook_notice_target_prefers_primary(self):
        runtimes = [FakeRuntime("cli"), FakeRuntime("telegram")]
        self.assertEqual(hook_notice_target(FakeRuntime("telegram"), runtimes), "telegram")

    def test_hook_notice_target_falls_back_to_first(self):
        runtimes = [FakeRuntime("cli"), FakeRuntime("telegram")]
        self.assertEqual(hook_notice_target(FakeRuntime("headless"), runtimes), "cli")

    def test_hook_notice_target_empty(self):
        self.assertEqual(hook_notice_target(None, []), "")

    def test_platform_stops_app_on_exit(self):
        self.assertTrue(platform_stops_app_on_exit(FakeRuntime("cli", stop_on_exit=True)))
        self.assertFalse(platform_stops_app_on_exit(FakeRuntime("cli")))
        self.assertFalse(platform_stops_app_on_exit(object()))

    def test_deliver_hook_notices_falls_back_to_log(self):
        logger = RecordingLogger()
        asyncio.run(deliver_hook_notices(None, logger, None, "", ["问题"]))
        self.assertEqual(logger.records[0][1], "hook notice")

    def test_deliver_hook_notices_sends_to_superadmins(self):
        agent = FakeAgent()
        asyncio.run(deliver_hook_notices(None, None, agent, "cli", ["  问题  "]))
        self.assertEqual(len(agent.notices), 1)
        self.assertTrue(agent.notices[0].target.superadmins)
        self.assertEqual(agent.notices[0].target.platform, "cli")
        self.assertEqual(agent.notices[0].outputs[0].text, "问题")

    def test_notify_platform_failure_targets_other_platforms(self):
        cfg = SimpleNamespace(
            security=SimpleNamespace(superadmins={"telegram": ["u1", "u1", ""], "cli": ["x"]})
        )
        agent = FakeAgent()
        asyncio.run(
            notify_platform_failure(None, cfg, agent, "cli", ["telegram", "atrinis", "cli"], InvalidError("boom"))
        )
        self.assertEqual(len(agent.notices), 1)
        self.assertEqual(agent.notices[0].target.platform, "telegram")
        self.assertEqual(agent.notices[0].target.private_user_id, "u1")
        self.assertIn("平台 cli 异常退出", agent.notices[0].outputs[0].text)

    def test_notify_platform_failure_ignores_missing_sender(self):
        asyncio.run(notify_platform_failure(None, None, None, "cli", ["telegram"], InvalidError("x")))

    def test_register_platform_hooks_wires_sender_and_health(self):
        state = SimpleNamespace(
            expected=[], connected=[],
            expect_platform=lambda name: state.expected.append(name),
            mark_platform_connected=lambda name: state.connected.append(name),
        )
        agent = FakeAgent()
        runtime = FakeRuntime("cli")
        register_platform_hooks(agent, [runtime], state)
        self.assertEqual(agent.senders[0][0], "cli")
        self.assertEqual(state.expected, ["cli"])
        runtime.notifier(None, "")
        self.assertEqual(state.connected, ["cli"])
        self.assertTrue(callable(runtime.notifier))

    def test_register_completion_and_catalogs(self):
        agent = FakeAgent()
        runtime = FakeRuntime("cli")
        register_completion_platforms(agent, [runtime])
        register_command_catalogs(agent, [runtime])
        self.assertEqual(runtime.completer, "completer")
        self.assertEqual(runtime.catalog, ["info"])

    def test_register_helpers_tolerate_none(self):
        register_completion_platforms(None, [FakeRuntime("cli")])
        register_command_catalogs(None, [])
        register_platform_hooks(None, [], None)

    def test_atrinis_adapter_rejects_sends(self):
        adapter = AtrinisRuntimeAdapter(SimpleNamespace(run=self._noop))
        self.assertEqual(adapter.name(), "atrinis")
        with self.assertRaises(InvalidError):
            asyncio.run(adapter.send_chat(None, []))
        with self.assertRaises(InvalidError):
            asyncio.run(adapter.send_notice(None, None))

    @staticmethod
    async def _noop(ctx):
        return None

    def test_executor_delegates(self):
        executor = DefaultPlatformExecutor()
        calls = []

        async def run(ctx, handler):
            calls.append(ctx)

        from atri_x.app.dependencies import PlatformRunRequest

        asyncio.run(
            executor.run(
                "ctx",
                PlatformRunRequest(
                    handler=None, logger=None, runtimes=[FakeRuntime("cli", run)], mode="full"
                ),
            )
        )
        # 运行时收到的是可查询取消状态的运行标志（Go 的 runCtx 对等物）。
        self.assertEqual(len(calls), 1)
        self.assertTrue(hasattr(calls[0], "is_set"))


class TestPlatformCaller(unittest.TestCase):
    def test_resolves_by_name(self):
        runtime = FakeRuntime("cli")
        self.assertEqual(resolve_platform_caller([runtime], "cli"), (runtime, True))
        self.assertEqual(resolve_platform_caller([runtime], "qq"), (None, False))
        self.assertEqual(resolve_platform_caller([runtime], "  "), (None, False))
        self.assertEqual(resolve_platform_caller(None, "cli"), (None, False))

    def test_resolvers_share_behaviour(self):
        runtime = FakeRuntime("cli")
        self.assertEqual(PlatformCallerResolver([runtime]).platform_caller("cli"), (runtime, True))
        self.assertEqual(
            HookPlatformCallerResolver([runtime]).PlatformCaller("cli"), (runtime, True)
        )
        self.assertEqual(
            HookPlatformCallerResolver([runtime]).platform_caller("nope"), (None, False)
        )


class TestIntegrations(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)
        self._saved = os.environ.get("ATRIX_TEST_ATRINIS_TOKEN")
        os.environ["ATRIX_TEST_ATRINIS_TOKEN"] = " tok "

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ATRIX_TEST_ATRINIS_TOKEN", None)
        else:
            os.environ["ATRIX_TEST_ATRINIS_TOKEN"] = self._saved
        shutil.rmtree(TMP, ignore_errors=True)

    def _cfg(self, token_env):
        return SimpleNamespace(
            config_path=str(TMP / "app.toml"),
            atrinis=SimpleNamespace(
                tokens={"edge": SimpleNamespace(token_env=token_env)}
            ),
        )

    def test_resolve_tokens_first_non_empty_wins(self):
        envs = os.environ.copy()
        envs["ATRIX_TEST_ATRINIS_TOKEN"] = " tok "
        tokens = resolve_atrinis_tokens(self._cfg(["ATRIX_MISSING_TOKEN", "ATRIX_TEST_ATRINIS_TOKEN"]))
        self.assertEqual(tokens, {"edge": "tok"})
        self.assertTrue(envs)

    def test_resolve_tokens_skips_blank_names(self):
        cfg = self._cfg([])
        cfg.atrinis.tokens = {"": SimpleNamespace(token_env=["X"]), "edge": SimpleNamespace(token_env=[""])}
        self.assertEqual(resolve_atrinis_tokens(cfg), {})

    def test_attach_requires_foundation_and_runtime(self):
        factory = DefaultIntegrationFactory()
        with self.assertRaises(InvalidError):
            asyncio.run(factory.attach(None, SimpleNamespace(foundation=None, runtime=None)))

    def test_attach_rejects_runtime_without_agent(self):
        from atri_x.app.dependencies import IntegrationRequest, RuntimeComponents

        factory = DefaultIntegrationFactory()
        request = IntegrationRequest(
            foundation=SimpleNamespace(config=SimpleNamespace(atrinis=SimpleNamespace(enabled=False))),
            runtime=RuntimeComponents(agent=None),
        )
        with self.assertRaises(InvalidError):
            asyncio.run(factory.attach(None, request))

    def test_attach_subscribes_platform_events(self):
        from atri_x.app.dependencies import (
            IntegrationRequest,
            PlatformComponents,
            RuntimeComponents,
        )

        agent = FakeAgent()
        request = IntegrationRequest(
            foundation=SimpleNamespace(config=SimpleNamespace(atrinis=SimpleNamespace(enabled=False))),
            runtime=RuntimeComponents(agent=agent, atriena_bus=None),
            platforms=PlatformComponents(primary=None, runtimes=[FakeRuntime("cli")]),
        )
        result = asyncio.run(DefaultIntegrationFactory().attach(None, request))
        self.assertEqual(agent.platform_events, 1)
        self.assertEqual(len(result.runtimes), 1)
        self.assertEqual(agent.senders[0][0], "cli")


class TestConfigCheckWarnings(unittest.TestCase):
    def test_unknown_model_profile_provider_warned(self):
        cfg = SimpleNamespace(
            model_profiles={"fast": SimpleNamespace(provider="ghost")},
            providers={},
            image_generation=SimpleNamespace(enabled=False, profiles={"p": {}}, base_url="", endpoint=""),
            maintenance=SimpleNamespace(daily_report=SimpleNamespace(enabled=False, prices={})),
            security=SimpleNamespace(superadmins={}),
        )
        warnings = check_config_warnings(cfg)
        self.assertTrue(any("ghost" in item for item in warnings))
        self.assertTrue(any("enabled=false" in item for item in warnings))

    def test_daily_report_warnings(self):
        cfg = SimpleNamespace(
            model_profiles={},
            providers={},
            image_generation=SimpleNamespace(enabled=True, profiles={}, base_url="u", endpoint=""),
            maintenance=SimpleNamespace(daily_report=SimpleNamespace(enabled=True, prices={})),
            security=SimpleNamespace(superadmins={}),
        )
        warnings = check_config_warnings(cfg)
        self.assertTrue(any("superadmins" in item for item in warnings))
        self.assertTrue(any("prices" in item for item in warnings))

    def test_missing_image_endpoint_warned(self):
        cfg = SimpleNamespace(
            model_profiles={},
            providers={},
            image_generation=SimpleNamespace(enabled=True, profiles={}, base_url="", endpoint=""),
            maintenance=SimpleNamespace(daily_report=SimpleNamespace(enabled=False, prices={})),
            security=SimpleNamespace(superadmins={}),
        )
        self.assertTrue(
            any("base_url/endpoint" in item for item in check_config_warnings(cfg))
        )

    def _config_with_backend(self, value):
        return SimpleNamespace(
            model_profiles={},
            providers={},
            image_generation=SimpleNamespace(enabled=False, profiles={}, base_url="", endpoint=""),
            maintenance=SimpleNamespace(daily_report=SimpleNamespace(enabled=False, prices={})),
            security=SimpleNamespace(superadmins={}),
            platform={"qqonebot": {"protocol_backend": value}},
        )

    def test_invalid_protocol_backend_warned(self):
        for raw in ("llonebot", 123):
            with self.subTest(raw=raw):
                warnings = check_config_warnings(self._config_with_backend(raw))
                self.assertTrue(any("protocol_backend" in item for item in warnings))

    def test_valid_or_missing_protocol_backend_is_quiet(self):
        for value in ("snowluma", "napcat", ""):
            with self.subTest(value=value):
                warnings = check_config_warnings(self._config_with_backend(value))
                self.assertFalse(any("protocol_backend" in item for item in warnings))
        missing = self._config_with_backend(None)
        missing.platform = {"qqonebot": {"enabled": True}}
        self.assertFalse(
            any("protocol_backend" in item for item in check_config_warnings(missing))
        )


class TestDoctorHelpers(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_format_report_all_ok(self):
        report = DoctorReport(checks=[DoctorCheck(category="config", name="load", status="passed")])
        self.assertEqual(format_doctor_report(report), "Everything is OK")

    def test_format_report_lists_failures(self):
        report = DoctorReport(
            checks=[
                DoctorCheck(category="config", name="load", status="failed", detail="d", error="e"),
                DoctorCheck(category="model", name="chat_call", status="passed"),
            ]
        )
        text = format_doctor_report(report)
        self.assertIn("配置检查发现 1 个问题", text)
        self.assertIn("[config] load：d：e", text)
        self.assertIn("本命令只读", text)

    def test_first_line(self):
        self.assertEqual(first_line("a\nb"), "a")
        self.assertEqual(first_line("  a  "), "a")

    def test_doctor_options_defaults(self):
        opts = DoctorOptions()
        self.assertFalse(opts.e2e)
        self.assertEqual(opts.timeout, 0.0)

    def test_enabled_platform_names_defaults_to_enabled(self):
        cfg = SimpleNamespace(platform={"a": {}, "b": {"enabled": False}, "c": {"enabled": True}})
        self.assertEqual(sorted(enabled_platform_names(cfg)), ["a", "c"])

    def test_check_writable_dirs_creates_and_cleans(self):
        cfg = SimpleNamespace(
            storage=SimpleNamespace(
                sessions_sqlite_path=str(TMP / "s" / "s.db"),
                chat_history_sqlite_path=str(TMP / "h" / "h.db"),
            )
        )
        check_writable_dirs(cfg)
        self.assertTrue((TMP / "s").is_dir())
        self.assertEqual(list((TMP / "s").iterdir()), [])

    def test_check_writable_dirs_rejects_empty(self):
        cfg = SimpleNamespace(
            storage=SimpleNamespace(sessions_sqlite_path="s.db", chat_history_sqlite_path="")
        )
        with self.assertRaises(InvalidError):
            check_writable_dirs(cfg)

    def test_resolve_api_key_prefers_env(self):
        os.environ["ATRIX_TEST_DOCTOR_KEY"] = " from-env "
        self.addCleanup(os.environ.pop, "ATRIX_TEST_DOCTOR_KEY", None)
        provider = SimpleNamespace(api_key_env="ATRIX_TEST_DOCTOR_KEY", api_key="inline")
        self.assertEqual(resolve_doctor_api_key(provider, str(TMP)), "from-env")

    def test_resolve_api_key_falls_back_to_inline(self):
        provider = SimpleNamespace(api_key_env="ATRIX_MISSING_DOCTOR_KEY", api_key="inline")
        self.assertEqual(resolve_doctor_api_key(provider, str(TMP)), "inline")

    def test_resolve_api_key_errors(self):
        with self.assertRaises(InvalidError):
            resolve_doctor_api_key(SimpleNamespace(api_key_env="", api_key=""), str(TMP))

    def test_snapshot_has_platform(self):
        snapshot = SimpleNamespace(platforms=[SimpleNamespace(name="cli")])
        self.assertTrue(snapshot_has_platform(snapshot, "cli"))
        self.assertFalse(snapshot_has_platform(snapshot, "qq"))

    def test_platform_connection_no_platforms(self):
        status, detail, err = doctor_platform_connection(
            SimpleNamespace(status="ok", platforms=[]), [], False
        )
        self.assertEqual(status, "passed")
        self.assertIn("no platform is enabled", detail)
        self.assertIsNone(err)

    def test_platform_connection_require_without_platforms(self):
        status, _detail, err = doctor_platform_connection(
            SimpleNamespace(status="ok", platforms=[]), [], True
        )
        self.assertEqual(status, "failed")
        self.assertIsInstance(err, InvalidError)

    def test_platform_connection_disconnected(self):
        snapshot = SimpleNamespace(
            status="degraded", platforms=[SimpleNamespace(name="cli", connected=False)]
        )
        status, detail, err = doctor_platform_connection(snapshot, ["cli"], False)
        self.assertEqual(status, "failed")
        self.assertIn("cli=disconnected", detail)
        self.assertIsInstance(err, InvalidError)

    def test_platform_connection_missing_only_fails_when_required(self):
        snapshot = SimpleNamespace(status="ok", platforms=[])
        self.assertEqual(
            doctor_platform_connection(snapshot, ["cli"], False)[0], "passed"
        )
        self.assertEqual(
            doctor_platform_connection(snapshot, ["cli"], True)[0], "failed"
        )


class TestImageRewriter(unittest.TestCase):
    def test_natural_default(self):
        self.assertEqual(image_rewrite_system_for("", ""), IMAGE_REWRITE_SYSTEM_NATURAL)
        self.assertEqual(image_rewrite_system_for("auto", "flux"), IMAGE_REWRITE_SYSTEM_NATURAL)

    def test_tags_for_sd_families(self):
        for family in ("sdxl", "sd", "stable_diffusion", "novelai", "nai", "SDXL"):
            with self.subTest(family=family):
                self.assertEqual(
                    image_rewrite_system_for("auto", family), IMAGE_REWRITE_SYSTEM_TAGS
                )

    def test_tags_dialect_forced(self):
        self.assertEqual(image_rewrite_system_for("tags", ""), IMAGE_REWRITE_SYSTEM_TAGS)
        self.assertEqual(image_rewrite_system_for("sd_weighted", ""), IMAGE_REWRITE_SYSTEM_TAGS)
        self.assertEqual(image_rewrite_system_for("natural", "sdxl"), IMAGE_REWRITE_SYSTEM_NATURAL)

    def test_dialect_labels_match_go(self):
        self.assertEqual(dialect_label("natural"), "自然语言")
        self.assertEqual(dialect_label("sd_weighted"), "SD 加权标签")
        self.assertEqual(dialect_label("nai"), "NovelAI 加权标签")
        self.assertEqual(dialect_label("tags"), "纯标签")
        self.assertEqual(dialect_label("auto"), "跟随 profile")

    def test_target_line(self):
        self.assertEqual(
            image_rewrite_target_line(ImagePromptRewriteRequest(target_model="flux")),
            "目标模型：flux",
        )
        self.assertEqual(
            image_rewrite_target_line(
                ImagePromptRewriteRequest(target_dialect="tags", target_model="nai")
            ),
            "目标模型：nai / 纯标签",
        )
        self.assertEqual(image_rewrite_target_line(ImagePromptRewriteRequest()), "")

    def test_build_prompt_sections(self):
        req = ImagePromptRewriteRequest(
            scene="  一只猫  ",
            character_name="阿特丽",
            character_prompt="银发",
            context_text="用户说想要猫",
            library_hints="cat, silver hair",
            target_model="flux",
            target_dialect="natural",
            protected_blocks=["(cat:1.2)"],
            max_runes=0,
        )
        text = build_rewrite_prompt(req, 4000)
        self.assertIn("目标模型：flux / 自然语言", text)
        self.assertIn("[场景描述]\n一只猫", text)
        self.assertIn("[角色]\n阿特丽\n银发", text)
        self.assertIn("用户说想要猫", text)
        self.assertIn("[TAG 库参考]", text)
        self.assertIn("[已单独处理的片段：不要输出它们，也不要改写]\n(cat:1.2)", text)
        self.assertIn("不超过 4000 个字符", text)

    def test_build_prompt_minimal(self):
        text = build_rewrite_prompt(ImagePromptRewriteRequest(scene="s"), 100)
        self.assertNotIn("[角色]", text)
        self.assertNotIn("[TAG 库参考]", text)

    def test_rewrite_without_model_raises(self):
        with self.assertRaises(InvalidError):
            asyncio.run(
                ImagePromptRewriter(None, "").rewrite_image_prompt(
                    None, ImagePromptRewriteRequest(scene="s")
                )
            )

    def test_rewrite_collects_stream(self):
        from atri_x.llm import StreamChunk

        class Client:
            def __init__(self):
                self.requests = []

            def chat_stream(self, ctx, req):
                self.requests.append(req)
                return self._gen()

            async def _gen(self):
                yield StreamChunk(delta_content=" a")
                yield StreamChunk(delta_content="b ")

        client = Client()
        result = asyncio.run(
            ImagePromptRewriter(client, "rewrite-model").rewrite_image_prompt(
                None, ImagePromptRewriteRequest(scene="猫", max_runes=10)
            )
        )
        self.assertEqual(result, "ab")
        self.assertEqual(client.requests[0].model, "rewrite-model")
        self.assertEqual(client.requests[0].max_tokens, 700)

    def test_rewrite_propagates_error_chunk(self):
        from atri_x.llm import StreamChunk

        boom = InvalidError("bad")

        class Client:
            def chat_stream(self, ctx, req):
                return self._gen()

            async def _gen(self):
                yield StreamChunk(error=boom)

        with self.assertRaises(InvalidError):
            asyncio.run(
                ImagePromptRewriter(Client(), "m").rewrite_image_prompt(
                    None, ImagePromptRewriteRequest(scene="s")
                )
            )

    def test_rewrite_empty_result_raises(self):
        from atri_x.llm import StreamChunk

        class Client:
            def chat_stream(self, ctx, req):
                return self._gen()

            async def _gen(self):
                yield StreamChunk(delta_content="   ")

        with self.assertRaises(InvalidError):
            asyncio.run(
                ImagePromptRewriter(Client(), "m").rewrite_image_prompt(
                    None, ImagePromptRewriteRequest(scene="s")
                )
            )

    def test_request_from_any_accepts_dataclass(self):
        req = ImagePromptRewriteRequest(scene="s")
        self.assertIs(ImagePromptRewriteRequest.from_any(req), req)
        self.assertEqual(ImagePromptRewriteRequest.from_any(None).scene, "")
        self.assertEqual(
            ImagePromptRewriteRequest.from_any(SimpleNamespace(scene="x", max_runes=3)).scene, "x"
        )


class TestCliClientHelpers(unittest.TestCase):
    def test_local_urls(self):
        for url in ("ws://localhost:8080", "ws://127.0.0.1:1/ws", "ws://[::1]:9", "ws:///x"):
            with self.subTest(url=url):
                self.assertTrue(is_local_cli_url(url))

    def test_remote_urls(self):
        for url in ("wss://example.com/ws", "ws://10.0.0.1:80"):
            with self.subTest(url=url):
                self.assertFalse(is_local_cli_url(url))

    def test_fallback_sentinel_identity(self):
        from atri_x.app.cli_client import run_cli_client

        self.assertTrue(callable(run_cli_client))
        self.assertIsInstance(ErrCLIClientFallback, BaseException)


class TestConnectNoticeHook(unittest.TestCase):
    """``register_connect_notice_hook``：私聊/群聊上线通知互相独立（1.1.3 修订）。

    上线通知从 ``hooks.toml`` 规则搬到内置 Hook，就是为了两个落点能各自开关：
    这里直接喂一个 ``platform.connected`` 事件，断言追加的 ``event.outputs``
    里的 Target（超管私聊 / 指定群）与开关的组合。
    """

    class _Registrar:
        def __init__(self) -> None:
            self.registrations: list[object] = []

        def register(self, registration: object) -> None:
            self.registrations.append(registration)

    @staticmethod
    def _config(
        *,
        private: bool = True,
        group: bool = True,
        groups: tuple[str, ...] = (),
        text: str = "",
        superadmins: dict[str, list[str]] | None = None,
        with_section: bool = True,
    ):
        if superadmins is None:
            superadmins = {"qqonebot": ["10001"]}
        cfg = SimpleNamespace(
            security=SimpleNamespace(superadmins=superadmins),
        )
        if with_section:
            cfg.connect_notice = SimpleNamespace(
                private=private, group=group, groups=list(groups), text=text
            )
        return cfg

    def _register(self, **kwargs):
        from atri_x.app.platforms import register_connect_notice_hook

        registrar = self._Registrar()
        register_connect_notice_hook(registrar, self._config(**kwargs))
        return registrar

    @staticmethod
    def _run(registrar, platform: str = "qqonebot"):
        from atri_x.hook.event import Event, PlatformContext, Point

        registration = registrar.registrations[0]
        event = Event(
            point=Point.PLATFORM_CONNECTED, platform=PlatformContext(name=platform)
        )
        return asyncio.run(registration.handler.handle_hook(None, event))

    def test_private_and_group_are_independent_outputs(self) -> None:
        from atri_x.delivery import Output

        event = self._run(self._register(groups=("123456789",)))
        self.assertEqual(len(event.outputs), 2)
        self.assertIsInstance(event.outputs[0], Output)
        self.assertTrue(event.outputs[0].target.superadmins)
        self.assertEqual(event.outputs[0].text, "ATRI-X 已连接 QQ OneBot。")
        self.assertEqual(event.outputs[1].target.group_id, "123456789")
        self.assertEqual(event.outputs[1].target.platform, "qqonebot")

    def test_private_off_keeps_group_only(self) -> None:
        event = self._run(self._register(private=False, groups=("1", "2")))
        self.assertEqual(
            [item.target.group_id for item in event.outputs], ["1", "2"]
        )
        self.assertFalse(any(item.target.superadmins for item in event.outputs))

    def test_group_off_keeps_private_only(self) -> None:
        event = self._run(self._register(group=False, groups=("1",)))
        self.assertEqual(len(event.outputs), 1)
        self.assertTrue(event.outputs[0].target.superadmins)

    def test_group_on_without_group_ids_sends_nothing(self) -> None:
        registrar = self._register(private=False, groups=())
        self.assertEqual(registrar.registrations, [])

    def test_missing_superadmins_does_not_block_group_notice(self) -> None:
        event = self._run(
            self._register(groups=("123456789",), superadmins={"qqonebot": []})
        )
        self.assertEqual(len(event.outputs), 1)
        self.assertEqual(event.outputs[0].target.group_id, "123456789")

    def test_other_platform_is_ignored(self) -> None:
        event = self._run(self._register(groups=("123456789",)), platform="telegram")
        self.assertEqual(event.outputs, [])

    def test_custom_text_and_missing_section(self) -> None:
        event = self._run(self._register(groups=("1",), text="我醒了"))
        self.assertEqual(event.outputs[0].text, "我醒了")
        registrar = self._Registrar()
        from atri_x.app.platforms import register_connect_notice_hook

        register_connect_notice_hook(registrar, self._config(with_section=False))
        self.assertEqual(registrar.registrations, [])


if __name__ == "__main__":
    unittest.main()
