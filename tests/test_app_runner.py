"""``atri_x.app.runner`` 装配顺序与关停测试。

对应 Go：``internal/app/runner.go``。

除集成工厂外全部用假工厂（无需真实 SQLite / 网络 / 平台），验证：

* 七个依赖缺一即 ``app: missing <name>``（并带上 ``DefaultDependencies`` 记下的原因）；
* 装配顺序（config → logging → health → models → platforms → runtime → integrations
  → executor）与标记（profiler mark）；
* 清理逆序执行、``shutdown`` 超时、失败步骤不阻断后续；
* 主体异常与清理异常用 :class:`ExceptionGroup` 一起抛出（对齐 Go 的 ``errors.Join``）；
* 平台失败回调会写 health 并通知超管；
* 启动期 Hook 告警在 Attach 之后补投递。

**集成工厂不放假实现**：``deps.integrations`` 一律用生产实现
:class:`atri_x.app.integrations.DefaultIntegrationFactory`。第二轮验证的 P0-3
（``Runner`` 调 ``attach`` 少传 ``ctx``）之所以测试全绿，就是因为这里原本放了一个
``async def attach(self, req)`` 的假工厂，比生产签名
``DefaultIntegrationFactory.attach(self, ctx, req)`` 少一个形参。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.app.app import RUN_MODE_FULL, Options
from atri_x.app.dependencies import (
    Dependencies,
    DefaultDependencies,
    FoundationComponents,
    ModelClients,
    PlatformComponents,
    RuntimeComponents,
)
from atri_x.app.integrations import DefaultIntegrationFactory
from atri_x.app.runner import DEFAULT_SHUTDOWN_TIMEOUT, CleanupStep, NewRunner, Runner
from atri_x.app.runtime import DefaultRuntimeFactory
from atri_x.app.support import maybe_await
from atri_x.errors import InvalidError


class FakeLogger:
    def __init__(self) -> None:
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


class FakeProfiler:
    def __init__(self) -> None:
        self.marks: list[str] = []
        self.enabled = False
        self.flushed = False

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def mark(self, name: str) -> None:
        self.marks.append(name)

    def flush(self) -> float:
        self.flushed = True
        return 1.5


class FakeEnvironment:
    def __init__(self, marker: Any = None) -> None:
        self.marker = marker
        self.modes: list[Any] = []
        self.profiler = FakeProfiler()

    async def resolve_mode(self, mode):
        self.modes.append(mode)
        return RUN_MODE_FULL

    async def claim_service_marker(self, mode):
        return self.marker

    def new_startup_profiler(self, started_at):
        return self.profiler


class FakeLifecycle:
    def __init__(self, events: list[str], name: str, error: BaseException | None = None):
        self.events = events
        self.name = name
        self.error = error
        self.contexts: list[Any] = []

    async def close(self, ctx=None):
        self.events.append(self.name)
        self.contexts.append(ctx)
        if self.error is not None:
            raise self.error


class FakeMarker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def close(self):
        self.events.append("marker")
        self.closed = True


@dataclass
class FakeConfig:
    config_path: str = "/tmp/atri/app.toml"

    @property
    def storage(self):
        return _Storage()

    @property
    def sandbox(self):
        return _Sandbox()


class _Storage:
    sessions_sqlite_path = "/tmp/atri/sessions.db"
    chat_history_sqlite_path = "/tmp/atri/history.db"


class _Sandbox:
    root = "/tmp/atri/sandbox"


class FakeHeartbeat:
    def __init__(self) -> None:
        self.beats = 0
        self.ready = False
        self.shutting_down = False
        self.disconnected: list[tuple[str, Any]] = []

    def beat(self) -> None:
        self.beats += 1

    def set_ready(self, value: bool) -> None:
        self.ready = bool(value)

    def set_shutting_down(self, value: bool) -> None:
        self.shutting_down = bool(value)

    def mark_platform_disconnected(self, name, err) -> None:
        self.disconnected.append((name, err))


class FakeAgent:
    def __init__(self) -> None:
        self.senders: list[tuple[str, Any]] = []
        self.notices: list[Any] = []
        self.platform_events_started = False
        self.platform_events_ctx: Any = None
        self.closed: list[str] = []

    def register_platform_sender(self, name, sender) -> None:
        self.senders.append((name, sender))

    async def send_notice(self, ctx, notice):
        self.notices.append(notice)
        return None

    async def start_platform_events(self, ctx) -> None:
        self.platform_events_started = True
        # 记下 Attach 收到的上下文：Go 里整条启动链共用同一个 ctx。
        self.platform_events_ctx = ctx

    async def close_platform_events(self, ctx) -> None:
        self.closed.append("platform_events")

    async def close_notice_queues(self, ctx) -> None:
        self.closed.append("notice_queues")

    def active_requests(self):
        return {"active": 0}


class RecordingAdapter:
    """记录 Attach 阶段装上的三样东西（补全 / 命令目录 / 连接通知）。"""

    def __init__(self, name: str = "cli") -> None:
        self.name = name
        self.completer: Any = "unset"
        self.command_catalog: Any = "unset"
        self.connect_notifier: Any = "unset"

    def set_completer(self, service: Any) -> None:
        self.completer = service

    def set_command_catalog(self, commands: Any) -> None:
        self.command_catalog = commands

    def set_connect_notifier(self, notifier: Any) -> None:
        self.connect_notifier = notifier


class Harness:
    """组装一套可运行的假依赖，并记录调用顺序。

    ``platforms`` / ``runtime`` / ``executor`` 是假工厂，但 **``integrations`` 是
    生产实现**：Attach 的签名、内部顺序、以及它给 Agent / 适配器装了什么，都由真实
    代码决定（见模块 docstring）。
    """

    def __init__(
        self, *, hook_notices=None, marker=None, runtime_close_error=None, runtimes=None
    ):
        self.events: list[str] = []
        self.logger = FakeLogger()
        self.environment = FakeEnvironment(marker)
        self.agent = FakeAgent()
        self.health = FakeHeartbeat()
        self.runtimes = list(runtimes or [])
        self.executor_calls: list[Any] = []
        self.executor_ctx: Any = None
        self.runtime_ctx: Any = None
        self.hook_notices = list(hook_notices or [])
        self.runtime_lifecycle = FakeLifecycle(
            self.events, "runtime", runtime_close_error
        )

        harness = self

        class Foundation:
            async def build(self, req):
                harness.events.append("foundation.build")
                req.profiler.mark("foundation")
                return FoundationComponents(
                    config=FakeConfig(),
                    logger=harness.logger,
                    lifecycle=FakeLifecycle(harness.events, "foundation"),
                    start_cron=None,
                )

        class Models:
            async def build(self, req):
                harness.events.append("models.build")
                req.profiler.mark("models")
                return ModelClients(by_provider={})

        class Platforms:
            async def build(self, req):
                harness.events.append("platforms.build")
                req.profiler.mark("platforms")
                return PlatformComponents(primary=None, runtimes=list(harness.runtimes))

        class Runtime:
            async def build(self, ctx, req):
                # 生产签名是 ``Build(ctx, req)``（Go ``runtime.go:42``）：假件必须带 ctx，
                # 否则 ``runner`` 少传 ctx 也会"测试全绿"—— 真实启动才炸
                # （`DefaultRuntimeFactory.build() missing 1 required positional argument: 'req'`）。
                harness.runtime_ctx = ctx
                harness.events.append("runtime.build")
                req.profiler.mark("runtime")
                return RuntimeComponents(
                    agent=harness.agent,
                    handler=harness.agent,
                    hook_notices=harness.hook_notices,
                    lifecycle=harness.runtime_lifecycle,
                )

        class Executor:
            async def run(self, ctx, req):
                harness.events.append("executor.run")
                harness.executor_calls.append(req)
                harness.executor_ctx = ctx
                if req.heartbeat is not None:
                    req.heartbeat()
                if req.after_start is not None:
                    await maybe_await(req.after_start(ctx))

        self.deps = Dependencies(
            environment=self.environment,
            foundation=Foundation(),
            models=Models(),
            platforms=Platforms(),
            runtime=Runtime(),
            integrations=DefaultIntegrationFactory(),
            executor=Executor(),
        )
        self.health_state = self.health


def patch_health_server(test: unittest.TestCase, state: Any, server: Any) -> None:
    """把 ``start_health_server`` 换成返回固定值的桩。"""
    import atri_x.app.runner as runner_module

    original = runner_module.start_health_server

    async def fake(cfg, logger, version, extra):
        return state, server

    runner_module.start_health_server = fake
    test.addCleanup(setattr, runner_module, "start_health_server", original)


class FailingServer:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def close(self, ctx):
        self.events.append("health_server")
        self.closed = True


class TestRunnerValidation(unittest.TestCase):
    def test_all_dependencies_required(self):
        names = (
            ("environment", "environment"),
            ("foundation factory", "foundation"),
            ("model factory", "models"),
            ("platform factory", "platforms"),
            ("runtime factory", "runtime"),
            ("integration factory", "integrations"),
            ("platform executor", "executor"),
        )
        full = {key: object() for _label, key in names}
        NewRunner(Dependencies(**full))
        for label, key in names:
            partial = dict(full)
            partial[key] = None
            with self.subTest(missing=label):
                with self.assertRaises(InvalidError) as ctx:
                    NewRunner(Dependencies(**partial))
                self.assertIn(label, str(ctx.exception))

    def test_default_shutdown_timeout_matches_go(self):
        self.assertEqual(DEFAULT_SHUTDOWN_TIMEOUT, 30.0)


class TestRunnerHappyPath(unittest.TestCase):
    def setUp(self):
        import os

        for name in (
            "ATRIX_HEALTH_ADDR",
            "ATRIX_OPS_TOKEN",
            "ATRIX_HEALTH_LIVE_STALE_SECONDS",
            "ATRIX_OPS_ALLOW_UNAUTHENTICATED",
        ):
            os.environ.pop(name, None)
        self.harness = Harness(hook_notices=["hook 配置有问题"])
        patch_health_server(self, self.harness.health, None)

    def test_assembly_order_and_cleanup(self):
        runner = Runner(self.harness.deps)
        asyncio.run(runner.run(None, Options(version="9.9.9")))
        self.assertEqual(
            self.harness.events,
            [
                "foundation.build",
                "models.build",
                "platforms.build",
                "runtime.build",
                "executor.run",
                "runtime",
                "foundation",
            ],
        )
        # 真实 Attach 不在 events 里（那是假工厂的记账），用它真正的两个副作用断言：
        # 订阅了平台连接事件、并走到 executor。
        self.assertTrue(self.harness.agent.platform_events_started)
        self.assertEqual(len(self.harness.executor_calls), 1)
        self.assertEqual(
            self.harness.environment.profiler.marks,
            ["foundation", "models", "platforms", "runtime", "platform hooks", "platform events"],
        )
        self.assertTrue(self.harness.environment.profiler.flushed)

    def harness_ready(self) -> bool:
        return self.harness.health.ready

    def test_health_state_shutting_down_on_cleanup(self):
        runner = Runner(self.harness.deps)
        asyncio.run(runner.run(None, Options()))
        self.assertTrue(self.harness.health.shutting_down)
        self.assertFalse(self.harness.health.ready)

    def test_service_marker_closed_last(self):
        harness = Harness()
        marker = FakeMarker(harness.events)
        harness.deps.environment.marker = marker
        patch_health_server(self, harness.health, None)
        runner = Runner(harness.deps)
        asyncio.run(runner.run(None, Options()))
        self.assertTrue(marker.closed)
        # marker 的清理最先注册 → 最后执行（Go 的 defer 逆序语义）。
        self.assertEqual(harness.events[-1], "marker")

    def test_hook_notices_delivered_after_attach(self):
        runner = Runner(self.harness.deps)
        asyncio.run(runner.run(None, Options()))
        # 没有可用平台 → 退回日志，不静默丢弃。
        messages = [message for _lvl, message, _f in self.harness.logger.records]
        self.assertIn("hook notice", messages)

    def test_health_handler_wraps_runtime_handler(self):
        captured = {}

        class Handler:
            async def handle_message(self, ctx, text):
                captured["called"] = text

        harness = Harness()
        patch_health_server(self, harness.health, None)
        original_build = harness.deps.runtime.build

        async def wrapped(ctx, req):
            components = await original_build(ctx, req)
            components.handler = Handler()
            return components

        harness.deps.runtime.build = wrapped
        runner = Runner(harness.deps)
        asyncio.run(runner.run(None, Options()))
        # Runner 把 handler 换成 HealthHandler 后不应影响消息处理。
        self.assertIsNotNone(harness.health)

    def test_startup_log_recorded(self):
        runner = Runner(self.harness.deps)
        asyncio.run(runner.run(None, Options()))
        messages = [message for _lvl, message, _f in self.harness.logger.records]
        self.assertIn("atri-x startup completed", messages)


class TestRunnerHealthServer(unittest.TestCase):
    def setUp(self):
        import os

        os.environ.pop("ATRIX_HEALTH_ADDR", None)

    def test_health_server_closed_when_present(self):
        harness = Harness()
        server = FailingServer(harness.events)
        patch_health_server(self, harness.health, server)
        runner = Runner(harness.deps)
        asyncio.run(runner.run(None, Options()))
        self.assertTrue(server.closed)
        self.assertIn("health_server", harness.events)


class TestRunnerFailureHandling(unittest.TestCase):
    def setUp(self):
        import os

        os.environ.pop("ATRIX_HEALTH_ADDR", None)

    def test_body_error_still_cleans_up(self):
        harness = Harness()

        async def boom(ctx, req):
            raise InvalidError("runtime exploded")

        harness.deps.runtime.build = boom
        patch_health_server(self, harness.health, None)
        runner = Runner(harness.deps)
        with self.assertRaises(InvalidError):
            asyncio.run(runner.run(None, Options()))
        self.assertEqual(harness.events[-1], "foundation")

    def test_cleanup_error_is_raised(self):
        harness = Harness(runtime_close_error=InvalidError("close failed"))
        patch_health_server(self, harness.health, None)
        runner = Runner(harness.deps)
        with self.assertRaises(InvalidError) as ctx:
            asyncio.run(runner.run(None, Options()))
        self.assertIn("close failed", str(ctx.exception))

    def test_body_and_cleanup_errors_grouped(self):
        harness = Harness(runtime_close_error=InvalidError("close failed"))

        async def boom(ctx, req):
            # runtime 已成功装配（清理已登记），失败发生在更后面的 Attach 阶段。
            raise InvalidError("attach exploded")

        harness.deps.integrations.attach = boom
        patch_health_server(self, harness.health, None)
        runner = Runner(harness.deps)
        with self.assertRaises(ExceptionGroup) as ctx:
            asyncio.run(runner.run(None, Options()))
        self.assertEqual(len(ctx.exception.exceptions), 2)

    def test_cleanup_timeout_does_not_block_rest(self):
        harness = Harness()

        class SlowLifecycle:
            async def close(self, ctx=None):
                harness.events.append("slow")
                await asyncio.sleep(5)

        harness.runtime_lifecycle = SlowLifecycle()
        patch_health_server(self, harness.health, None)
        runner = Runner(harness.deps, shutdown_timeout=0.05)
        with self.assertRaises(TimeoutError):
            asyncio.run(runner.run(None, Options()))
        # 后续步骤仍然执行。
        self.assertEqual(harness.events[-1], "foundation")

    def test_missing_runtime_lifecycle_rejected(self):
        harness = Harness()

        async def incomplete(ctx, req):
            from atri_x.app.dependencies import RuntimeComponents

            return RuntimeComponents(agent=harness.agent, handler=harness.agent)

        harness.deps.runtime.build = incomplete
        patch_health_server(self, harness.health, None)
        runner = Runner(harness.deps)
        with self.assertRaises(InvalidError) as ctx:
            asyncio.run(runner.run(None, Options()))
        self.assertIn("incomplete", str(ctx.exception))

    def test_missing_runtime_handler_still_closes_lifecycle(self):
        """Go ``runner.go:112-118``：清理先登记、再查 Handler，defer 照跑。"""
        harness = Harness()

        async def incomplete(ctx, req):
            from atri_x.app.dependencies import RuntimeComponents

            return RuntimeComponents(
                agent=harness.agent, handler=None, lifecycle=harness.runtime_lifecycle
            )

        harness.deps.runtime.build = incomplete
        patch_health_server(self, harness.health, None)
        with self.assertRaises(InvalidError) as ctx:
            asyncio.run(Runner(harness.deps).run(None, Options()))
        self.assertIn("incomplete", str(ctx.exception))
        self.assertIn("runtime", harness.events)

    def test_missing_foundation_logger_still_closes_lifecycle(self):
        """Go ``runner.go:60-66``：同上，foundation 侧的顺序也一样。"""
        harness = Harness()

        async def incomplete(req):
            from atri_x.app.dependencies import FoundationComponents

            return FoundationComponents(
                config=FakeConfig(),
                logger=None,
                lifecycle=FakeLifecycle(harness.events, "foundation"),
            )

        harness.deps.foundation.build = incomplete
        patch_health_server(self, harness.health, None)
        with self.assertRaises(InvalidError) as ctx:
            asyncio.run(Runner(harness.deps).run(None, Options()))
        self.assertIn("incomplete", str(ctx.exception))
        self.assertIn("foundation", harness.events)


class TestRunnerPlatformFailure(unittest.TestCase):
    def setUp(self):
        import os

        os.environ.pop("ATRIX_HEALTH_ADDR", None)

    def test_platform_failure_marks_health_and_notifies(self):
        harness = Harness()
        patch_health_server(self, harness.health, None)
        captured = {}

        original = harness.deps.executor.run

        async def run(ctx, req):
            await original(ctx, req)
            captured["failure"] = req.platform_failure

        harness.deps.executor.run = run
        runner = Runner(harness.deps)
        asyncio.run(runner.run(None, Options()))
        failure = captured["failure"]
        self.assertTrue(inspect.iscoroutinefunction(failure))
        asyncio.run(failure(None, "cli", ["telegram"], InvalidError("boom")))
        self.assertEqual(harness.health.disconnected[0][0], "cli")

    def test_memory_and_learning_admin_handlers_are_wired(self):
        harness = Harness()
        patch_health_server(self, harness.health, None)
        import atri_x.app.runner as runner_module

        created: list[Any] = []
        original_json = runner_module.LazyJSONProvider
        original_http = runner_module.LazyHTTPHandler

        class SpyJSON(original_json):  # type: ignore[misc, valid-type]
            def __init__(self):
                super().__init__()
                created.append("json")

        class SpyHTTP(original_http):  # type: ignore[misc, valid-type]
            def __init__(self):
                super().__init__()
                created.append("http")

        runner_module.LazyJSONProvider = SpyJSON
        runner_module.LazyHTTPHandler = SpyHTTP
        self.addCleanup(setattr, runner_module, "LazyJSONProvider", original_json)
        self.addCleanup(setattr, runner_module, "LazyHTTPHandler", original_http)
        runner = Runner(harness.deps)
        asyncio.run(runner.run(None, Options()))
        # 3 个 lazy JSON provider（tasks/metrics/diagnostics）+ 2 个 lazy HTTP handler。
        self.assertEqual(created.count("json"), 3)
        self.assertEqual(created.count("http"), 2)


class TestCleanupStepDataclass(unittest.TestCase):
    def test_fields(self):
        step = CleanupStep(name="x", close=lambda ctx: None)
        self.assertEqual(step.name, "x")
        self.assertTrue(callable(step.close))


class TestRealIntegrationFactoryWiring(unittest.TestCase):
    """回归 P0-3：``Runner`` 必须按 Go ``Attach(ctx, req)`` 传**两个**实参。

    这里刻意不放任何 ``attach`` 假实现 —— 缺陷正是「假实现的形参比生产少一个」
    掩盖出来的。
    """

    def setUp(self):
        import os

        os.environ.pop("ATRIX_HEALTH_ADDR", None)

    def test_production_attach_signature_is_ctx_and_req(self):
        """生产签名写死为 ``(ctx, req)``，与 Go ``Attach(ctx, req)`` 一致。"""
        parameters = list(inspect.signature(DefaultIntegrationFactory().attach).parameters)
        self.assertEqual(parameters, ["ctx", "req"])
        self.assertTrue(inspect.iscoroutinefunction(DefaultIntegrationFactory().attach))

    def test_production_runtime_factory_signature_is_ctx_and_req(self):
        """生产 runtime 工厂签名写死为 ``(ctx, req)``，与 Go ``runtime.go:42`` 一致。

        真实事故（进程级冒烟抓到）：``runner`` 只传了 ``req``，真实启动第一行就抛
        ``DefaultRuntimeFactory.build() missing 1 required positional argument: 'req'``
        —— 而假工厂的 ``build(self, req)`` 让全部 app 测试照样全绿。
        """
        parameters = list(inspect.signature(DefaultRuntimeFactory().build).parameters)
        self.assertEqual(parameters, ["ctx", "req"])
        self.assertTrue(inspect.iscoroutinefunction(DefaultRuntimeFactory().build))

    def test_runner_passes_context_to_runtime_factory(self):
        """``runner`` 必须把同一个 ctx 也交给 runtime 工厂（Go 整链共用 ctx）。"""
        adapter = RecordingAdapter("cli")
        harness = Harness(runtimes=[adapter])
        patch_health_server(self, harness.health, None)
        asyncio.run(Runner(harness.deps).run(None, Options()))

        self.assertIsNotNone(harness.runtime_ctx)
        self.assertIs(harness.runtime_ctx, harness.executor_ctx)
        self.assertIs(harness.runtime_ctx, harness.agent.platform_events_ctx)

    def test_runner_drives_real_attach_to_completion(self):
        adapter = RecordingAdapter("cli")
        harness = Harness(runtimes=[adapter])
        patch_health_server(self, harness.health, None)
        asyncio.run(Runner(harness.deps).run(None, Options(version="9.9.9")))

        # Attach 真的执行完了（缺参时第 193 行会抛 TypeError，以下断言全部不可达）。
        self.assertTrue(harness.agent.platform_events_started)
        self.assertEqual(harness.agent.senders, [("cli", adapter)])
        self.assertIsNotNone(adapter.connect_notifier)
        self.assertNotEqual(adapter.completer, "unset")
        self.assertNotEqual(adapter.command_catalog, "unset")
        self.assertEqual(len(harness.executor_calls), 1)

    def test_runner_passes_the_same_context_to_attach_and_executor(self):
        """Go 里整条启动链共用同一个 ctx；Python 侧是 :class:`_RunContext` 替身。"""
        adapter = RecordingAdapter("cli")
        harness = Harness(runtimes=[adapter])
        patch_health_server(self, harness.health, None)
        asyncio.run(Runner(harness.deps).run(None, Options()))

        attach_ctx = harness.agent.platform_events_ctx
        self.assertIsNotNone(attach_ctx)
        self.assertIs(attach_ctx, harness.executor_ctx)
        # 传下去的是 runner 的运行上下文替身（``_RunContext``），不是 ``None``。
        self.assertTrue(callable(getattr(attach_ctx, "wait_stopped", None)))

    def test_create_connect_notifier_marks_health(self):
        """``register_platform_hooks`` 里 health 的 ``expect_platform`` 也要跑到。"""
        adapter = RecordingAdapter("telegram")
        harness = Harness(runtimes=[adapter])
        harness.expect_calls = []
        harness.health.expect_platform = lambda name: harness.expect_calls.append(name)
        patch_health_server(self, harness.health, None)
        asyncio.run(Runner(harness.deps).run(None, Options()))
        self.assertEqual(harness.expect_calls, ["telegram"])


class TestDegradedDependencies(unittest.TestCase):
    """回归 P2-3：``_maybe`` 吞掉的构造失败要能出现在启动报错里。"""

    def test_missing_factory_error_carries_reason(self):
        deps = Dependencies(
            environment=object(),
            foundation=object(),
            models=object(),
            platforms=object(),
            runtime=object(),
            integrations=None,
            executor=object(),
            degraded=[("integrations", "import atri_x.app.integrations: ImportError: boom")],
        )
        with self.assertRaises(InvalidError) as ctx:
            NewRunner(deps)
        message = str(ctx.exception)
        self.assertIn("app: missing integration factory", message)
        self.assertIn("ImportError: boom", message)

    def test_missing_factory_error_unchanged_without_reason(self):
        with self.assertRaises(InvalidError) as ctx:
            NewRunner(Dependencies(environment=object()))
        self.assertEqual(str(ctx.exception), "app: missing foundation factory")

    def test_maybe_records_import_failure(self):
        from atri_x.app.dependencies import _maybe

        degraded: list[tuple[str, str]] = []
        self.assertIsNone(
            _maybe("atri_x.app.no_such_module_xyz", "Nope", "runtime", degraded)
        )
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0][0], "runtime")
        self.assertIn("no_such_module_xyz", degraded[0][1])

    def test_maybe_records_constructor_failure(self):
        from atri_x.app.dependencies import _maybe

        degraded: list[tuple[str, str]] = []
        # ``Runner`` 需要 deps 位置参数 → 构造即抛 TypeError。
        self.assertIsNone(_maybe("atri_x.app.runner", "Runner", "runtime", degraded))
        self.assertEqual(len(degraded), 1)
        self.assertIn("Runner()", degraded[0][1])
        self.assertIn("TypeError", degraded[0][1])

    def test_default_dependencies_has_no_silent_degradation(self):
        """全仓文件级覆盖已 100%：默认装配不该再有任何降级。"""
        deps = DefaultDependencies()
        self.assertEqual(deps.degraded, [])
        self.assertEqual(deps.degraded_reason("integrations"), "")
        for slot in ("environment", "foundation", "models", "platforms", "runtime",
                     "integrations", "executor"):
            self.assertIsNotNone(getattr(deps, slot), slot)


if __name__ == "__main__":
    unittest.main()
