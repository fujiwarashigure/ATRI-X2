"""``atri_x.app.runtime_agent.build_agent`` 的真实装配测试。

对应 Go：``internal/app/runtime.go`` 的 ``buildAgent``（含 ``core.go:428-434``
要求的工具运行时装配）。

这一组用例刻意**不走假 Agent / 假门面**：``build_agent`` 造的是真实
:class:`atri_x.agent.core.Agent`，``install_agent_services`` 是真实 installer，
``tool_runtime.registry`` 是 ``new_registry_with_builtin`` 造的真实
:class:`atri_x.tool.tool.Registry`（11 个内置工具）。只有「假存储」是假的。

覆盖四类接线缺口（前三类是第二轮审计的遗留，第四类是静默降级审计的 P1）：

* **P0-4（app 半边）**：``build_agent`` 把 ``tool_registry`` 传给 ``Options``，
  但 ``install_agent_services(agent)`` 没带 ``opts``，于是
  ``bootstrap._wire_extra_host_fields`` 里 ``options.get("registry")`` 恒为
  ``None`` 直接 return —— 工具注册表静默丢掉。修复后必须能在
  ``agent.tool_runtime`` 里看到真实注册表与工具。
* **运行期挂起**：``runtime_agent`` 曾 ``await maybe_await(start_group_services(ctx))``，
  而 ``start_group_services`` 返回常驻 Task（``while True: sleep``，群服务默认
  ``enabled=None`` → 启用），真实 ``build_agent`` 会永久停在 runtime 阶段。
* **``hook_service`` 未接线**：``runtime_agent`` 曾把 ``hook_service`` 只放进
  ``AgentOptions``，而 ``Agent.__init__`` 不落这个字段，bootstrap 只认 ``opts``
  里的键 → ``deps.hooks`` 恒为 ``None``、``/hooks`` 恒回 "not configured"。
* **``rate_limit_*`` 未接线**：``[ops]`` 的 per-minute/burst 从没有构造点，
  ``/diagnostics`` 于是把"没有限流"显示成"0 次拒绝"。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from types import SimpleNamespace
from typing import Any

from atri_x.app.ops_health import VisionMetricsState, collect_ops_diagnostics
from atri_x.app.runtime_agent import build_agent
from atri_x.app.tool_runtime import ToolRuntime
from atri_x.config import FileDeliveryConfig, ModelSelection, ToolsConfig
from atri_x.security import Actor, RoleSuperadmin, default_policy, with_actor
from atri_x.tool.builtin.file_manager import new_file_manager_with_media
from atri_x.tool.builtin.register import RegisterOptions, new_registry_with_builtin
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo
from tests.test_agent_core import make_ctx

#: 临时目录放仓库根（PORTING §7.2：禁用 tempfile.mkdtemp）。
TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_app_runtime_agent_test")


def _superadmin_ctx():
    """``/hooks`` 等控制面命令按超管身份跑，避免被角色过滤掉。"""
    return with_actor(make_ctx(), Actor(id="root", role=RoleSuperadmin))


class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def _emit(self, level: str, message: str, **fields: Any) -> None:
        self.records.append((level, message, fields))

    def debug(self, message, **fields):
        self._emit("debug", message, **fields)

    def info(self, message, **fields):
        self._emit("info", message, **fields)

    def warn(self, message, **fields):
        self._emit("warn", message, **fields)

    def error(self, message, **fields):
        self._emit("error", message, **fields)


class _Logs:
    def audit(self):
        return _Logger()

    def runtime(self):
        return _Logger()


def _config(
    root: str,
    *,
    ops: Any = None,
    runtime: Any = None,
) -> SimpleNamespace:
    """一份能让 ``validate_options`` 通过的最小配置（字段与 ``Runtime`` 的读取点对齐）。"""
    return SimpleNamespace(
        config_path=os.path.join(root, "app.toml"),
        providers={"stub": SimpleNamespace(name="stub")},
        model_profiles={},
        tool_profiles={},
        mode_models={"work": ModelSelection(provider="stub", model="stub-model")},
        tools=ToolsConfig(),
        ops=ops,
        runtime=runtime,
        state_config_path="",
        command_prefixes=["/"],
        sandbox=SimpleNamespace(root=os.path.join(root, "sandbox")),
        storage=SimpleNamespace(
            sessions_sqlite_path=os.path.join(root, "sessions.db"),
            chat_history_sqlite_path=os.path.join(root, "history.db"),
        ),
        maintenance=SimpleNamespace(
            daily_report=None,
            session_cleanup=SimpleNamespace(retention_days=7),
            sandbox_cleanup=SimpleNamespace(retention_days=7),
        ),
        session=SimpleNamespace(naming=None, idle_expiration=None, default_mode="work"),
        commands=SimpleNamespace(prefixes=["/"]),
        soul=SimpleNamespace(path=""),
        view=SimpleNamespace(session_list_page_size=20),
    )


class _HookService:
    """最小 hook 控制面替身：``/hooks`` 只要求 ``hook_list`` / ``stateful_hooks``。"""

    def hook_list(self) -> list[Any]:
        return []

    def stateful_hooks(self) -> list[Any]:
        return []


class _Fixture:
    """真实 ``build_agent`` 的最小可运行环境（假存储 + 一个 stub provider 客户端）。"""

    def __init__(
        self,
        *,
        hook_service: Any = None,
        ops: Any = None,
        runtime: Any = None,
    ) -> None:
        self.root = TMP_DIR
        os.makedirs(os.path.join(self.root, "sandbox"), exist_ok=True)
        self.config = _config(self.root, ops=ops, runtime=runtime)
        self.logger = _Logger()
        self.foundation = SimpleNamespace(
            config=self.config, logger=self.logger, logs=_Logs(), store=SimpleNamespace()
        )
        self.hook_service = hook_service
        self.models = SimpleNamespace(
            by_provider={"stub": SimpleNamespace(name="stub-client")}
        )
        self.platforms = SimpleNamespace(primary=None)
        self.registry = new_registry_with_builtin(
            RegisterOptions(runtime_info=RuntimeInfo(config_dir=self.root))
        )
        self.tool_runtime = ToolRuntime(
            registry=self.registry,
            file_manager=new_file_manager_with_media(
                self.config.sandbox.root, FileDeliveryConfig(), None
            ),
            skill_manager=None,
            image_profiles={},
        )
        self.vision_metrics = VisionMetricsState()

    def build(self) -> Any:
        return build_agent(
            None,
            self.foundation,
            self.models,
            self.platforms,
            self.tool_runtime,
            default_policy(),
            None,
            None,
            self.hook_service,
            self.vision_metrics,
            None,
        )

    async def build_and_quiesce(self, timeout: float = 10.0) -> tuple[Any, Any]:
        """跑 ``build_agent``，并把群服务后台循环收干净（避免测试间串扰）。

        ``wait_for`` 同时就是挂起回归的断言：老代码会一直等在
        ``start_group_services`` 返回的常驻 Task 上，永远不返回。
        """
        agent = await asyncio.wait_for(self.build(), timeout)
        task = getattr(agent, "group_services_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return agent, task


class TestBuildAgentToolRuntime(unittest.TestCase):
    """P0-4（app 半边）：装配后 Agent 的工具运行时里必须真的有工具。"""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_agent_tool_runtime_receives_the_real_registry(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        runtime = agent.tool_runtime
        self.assertIs(runtime.registry, self.fixture.registry)
        self.assertIsNotNone(runtime.manager)
        self.assertIsNotNone(runtime.provider)
        self.assertTrue(runtime.default_provider)

    def test_registry_really_contains_builtin_tools(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        infos = agent.tool_runtime.registry.list()
        self.assertGreaterEqual(len(infos), 1)
        names = [info.name for info in infos]
        self.assertIn("discover_tool", names)

    def test_model_facing_schemas_are_not_empty(self):
        """模型这一轮的 schema 出口：装配前这里是空的（``Manager(None, ...)``）。"""
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())

        async def collect() -> list[str]:
            from atri_x.toolrun.toolrun import Context as ToolRunContext

            manager = agent.tool_run_manager()
            self.assertIs(manager.native.registry, self.fixture.registry)
            view = ToolRunContext(
                mode="work", session=None, scope="local", actor=None, disable_base_tools=False
            )
            schemas = await manager.base_schemas(None, view)
            return [getattr(getattr(item, "function", None), "name", "") for item in schemas]

        names = asyncio.run(collect())
        self.assertIn("discover_tool", names)

    def test_tools_facade_registered_for_deps(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        self.assertIsNotNone(agent.services.get("tools"))


class TestBuildAgentBootstrapOptions(unittest.TestCase):
    """P0-4：``install_agent_services`` 必须收到 ``opts["registry"]``。"""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_app_passes_registry_and_skills_to_bootstrap(self):
        import atri_x.agent.bootstrap as bootstrap_module

        seen: list[dict[str, Any]] = []
        original = bootstrap_module.install_agent_services

        def spy(agent, *, deps=None, opts=None):
            seen.append(dict(opts or {}))
            return original(agent, deps=deps, opts=opts)

        bootstrap_module.install_agent_services = spy
        self.addCleanup(setattr, bootstrap_module, "install_agent_services", original)
        asyncio.run(self.fixture.build_and_quiesce())

        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0].get("registry"), self.fixture.registry)
        self.assertIs(seen[0].get("skills"), self.fixture.tool_runtime.skill_manager)


class TestBuildAgentBackgroundLoops(unittest.TestCase):
    """``StartGroupServices`` / ``StartRuntimeStateWatch`` 是同步调用、不等待。"""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_build_agent_does_not_await_the_group_services_task(self):
        agent, task = asyncio.run(self.fixture.build_and_quiesce(timeout=10.0))
        # 老代码在这里等不到返回值：``start_group_services`` 返回的是常驻 Task，
        # 群服务默认 enabled（enabled=None → True，与 Go 的
        # ``Enabled == nil || *Enabled`` 一致），await 它等于等进程结束。
        self.assertIsInstance(task, asyncio.Task)
        self.assertIs(agent.group_services_task, task)

    def test_runtime_state_watch_is_started_synchronously(self):
        """Go ``StartRuntimeStateWatch(ctx)``：同步返回，不返回值。"""
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        # state_path 为空 → 监视器按契约直接返回，不建 task（见 state_watch.py:59）。
        self.assertFalse(getattr(agent, "state_watch_started", False))
        self.assertIsNone(getattr(agent, "state_watch_task", None))


#: 一份"配置了入站限流"的 ``[ops]`` 段（12/3 用户、60/10 群）。
_OPS_WITH_RATE_LIMIT = SimpleNamespace(
    user_messages_per_minute=12,
    user_burst=3,
    group_messages_per_minute=60,
    group_burst=10,
    rate_limit_idle_ttl_seconds=600,
)


class TestBuildAgentRateLimitWiring(unittest.TestCase):
    """P1：``[ops]`` 的入站限流必须真的构造出限流器并挂到宿主上。"""

    def setUp(self):
        self.fixture = _Fixture(ops=_OPS_WITH_RATE_LIMIT)
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_limiters_are_built_from_ops_config(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        user = getattr(agent, "rate_limit_user", None)
        group = getattr(agent, "rate_limit_group", None)
        self.assertIsNotNone(user, "rate_limit_user 没有构造：入站限流仍然不存在")
        self.assertIsNotNone(group, "rate_limit_group 没有构造：入站限流仍然不存在")
        # 12/min → 0.2/s；burst 按配置（不是 Limiter 的 1 兜底）。
        self.assertAlmostEqual(user.limit.rate_per_second, 0.2)
        self.assertEqual(user.limit.burst, 3)
        self.assertAlmostEqual(group.limit.rate_per_second, 1.0)
        self.assertEqual(group.limit.burst, 10)
        self.assertTrue(agent.agent_capabilities["rate_limit_configured"])

    def test_status_reports_configured_thresholds(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        status = agent.rate_limit_status()
        self.assertEqual(status.user_messages_per_minute, 12)
        self.assertEqual(status.user_burst, 3)
        self.assertEqual(status.group_messages_per_minute, 60)
        self.assertEqual(status.group_burst, 10)
        self.assertEqual(status.rejected, 0)

    def test_diagnostics_marks_rate_limit_as_configured(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        report = collect_ops_diagnostics(None, agent)
        self.assertTrue(report.rate_limit.configured)
        self.assertEqual(report.rate_limit.disabled_reason, "")
        # 阈值不再因为读错字段名而恒为 0。
        self.assertEqual(report.rate_limit.user_messages_per_minute, 12)
        self.assertEqual(report.rate_limit.group_messages_per_minute, 60)

    def test_zero_thresholds_stay_explicitly_unconfigured(self):
        fixture = _Fixture(
            ops=SimpleNamespace(
                user_messages_per_minute=0,
                user_burst=0,
                group_messages_per_minute=0,
                group_burst=0,
                rate_limit_idle_ttl_seconds=0,
            )
        )
        agent, _task = asyncio.run(fixture.build_and_quiesce())
        self.assertIsNone(getattr(agent, "rate_limit_user", None))
        self.assertIsNone(getattr(agent, "rate_limit_group", None))
        report = collect_ops_diagnostics(None, agent)
        self.assertFalse(report.rate_limit.configured)
        self.assertIn("not configured", report.rate_limit.disabled_reason)


class TestBuildAgentHookServiceWiring(unittest.TestCase):
    """P1：hook 控制面必须在**生产装配路径**上接进 ``deps.hooks``。"""

    def setUp(self):
        self.hook_service = _HookService()
        self.fixture = _Fixture(hook_service=self.hook_service)
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_hook_service_lands_on_the_agent_and_deps(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        self.assertIs(agent.hook_service, self.hook_service)
        self.assertIs(agent.services.get("hooks"), self.hook_service)
        self.assertIs(agent.command_deps.hooks, self.hook_service)
        self.assertTrue(agent.agent_capabilities["hook_service"])

    def test_hooks_command_no_longer_reports_not_configured(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        result = asyncio.run(agent.commands.dispatch(_superadmin_ctx(), "/hooks"))
        self.assertIsNotNone(result)
        self.assertNotIn("not configured", result.content)
        self.assertIn("hooks: none", result.content)

    def test_installer_receives_hook_service_in_opts(self):
        import atri_x.agent.bootstrap as bootstrap_module

        seen: list[dict[str, Any]] = []
        original = bootstrap_module.install_agent_services

        def spy(agent, *, deps=None, opts=None):
            seen.append(dict(opts or {}))
            return original(agent, deps=deps, opts=opts)

        bootstrap_module.install_agent_services = spy
        self.addCleanup(setattr, bootstrap_module, "install_agent_services", original)
        asyncio.run(self.fixture.build_and_quiesce())
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0].get("hook_service"), self.hook_service)


class TestBuildAgentServicesReport(unittest.TestCase):
    """P1：装配报告必须有出口（结构化日志 + ``/diagnostics``），不能再静默。"""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_report_is_published_on_the_host(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        payload = agent.agent_services_report
        self.assertIsInstance(payload, dict)
        for key in ("installed", "skipped", "failed", "warnings", "ok"):
            self.assertIn(key, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["installed"]), 9)

    def test_startup_logs_all_four_sections(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())  # noqa: F841 - 触发装配
        records = [
            record
            for record in self.fixture.logger.records
            if record[1] == "agent services assembled"
        ]
        self.assertEqual(len(records), 1)
        level, _message, fields = records[0]
        self.assertEqual(level, "info")
        for key in ("installed", "skipped", "failed", "warnings"):
            self.assertIn(key, fields)
        self.assertEqual(fields["skipped"], [])
        self.assertEqual(fields["failed"], [])
        self.assertTrue(fields["installed"])

    def test_historical_silent_skips_are_logged_and_queryable(self):
        """旧代码里只在 ``skipped`` 里的补充装配失败：0 行日志、无人可查。"""
        import atri_x.agent.bootstrap as bootstrap_module

        original = bootstrap_module._SUPPLEMENTARY_INSTALLERS
        bootstrap_module._SUPPLEMENTARY_INSTALLERS = original + (
            ("broken_extra", "atri_x.agent.does_not_exist", "install_broken"),
        )
        try:
            agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        finally:
            bootstrap_module._SUPPLEMENTARY_INSTALLERS = original

        payload = agent.agent_services_report
        skipped_names = [name for name, _detail in payload["skipped"]]
        self.assertEqual(skipped_names, ["broken_extra"])
        # 原因必须一起落盘（不能只有名字）。
        self.assertIn("does_not_exist", dict(payload["skipped"])["broken_extra"])

        warnings = [
            record
            for record in self.fixture.logger.records
            if record[0] == "warn" and record[1].startswith("agent services")
        ]
        self.assertEqual(len(warnings), 1, self.fixture.logger.records)
        fields = warnings[0][2]
        self.assertEqual([name for name, _detail in fields["skipped"]], ["broken_extra"])
        self.assertTrue(fields["warnings"])

        diagnostics = collect_ops_diagnostics(None, agent)
        self.assertIn(
            "broken_extra", [name for name, _detail in diagnostics.agent_services["skipped"]]
        )

    def test_diagnostics_carries_services_and_redaction(self):
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        report = collect_ops_diagnostics(None, agent)
        self.assertTrue(report.agent_services["ok"])
        self.assertIsInstance(report.redaction, dict)
        self.assertIn("available", report.redaction)

    def test_capability_snapshot_is_bound_to_doctor(self):
        """``/doctor`` 拿不到 Agent，只能复用启动期快照；快照必须真的绑上去。"""
        agent, _task = asyncio.run(self.fixture.build_and_quiesce())
        capabilities = agent.agent_capabilities
        self.assertIsInstance(capabilities, dict)
        self.assertEqual(capabilities["agent_services"], agent.agent_services_report)
        self.assertIn("redaction", capabilities)
        self.assertEqual(agent.doctor.capabilities, capabilities)

    def test_report_line_is_structured_and_parseable(self):
        """真实 ``Logger`` 写出的那一行必须能被日志读取侧解析回键值。"""
        import io

        from atri_x.logging.logging import Logger
        from atri_x.logging.reader import parse_text_attrs

        output = io.StringIO()
        self.fixture.foundation.logger = Logger("info", output)
        asyncio.run(self.fixture.build_and_quiesce())
        lines = [
            line
            for line in output.getvalue().splitlines()
            if "agent services assembled" in line
        ]
        self.assertEqual(len(lines), 1, output.getvalue())
        parsed = parse_text_attrs(lines[0])
        self.assertEqual(parsed["event"], "agent_service_install")
        self.assertEqual(parsed["ok"], "true")
        self.assertEqual(parsed["level"], "INFO")
        # 四类结果都在同一行里，且每项带原因（``installed`` 的说明来自 installer 返回值）。
        for key in ("installed", "skipped", "failed", "warnings"):
            self.assertIn(key, parsed)
        self.assertIn("models", parsed["installed"])


class TestBuildAgentFailFast(unittest.TestCase):
    """可选开关：``[runtime] fail_fast_agent_services``（默认关闭）。"""

    def setUp(self):
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def _break_core_installer(self):
        import atri_x.agent.bootstrap as bootstrap_module

        original = bootstrap_module._CORE_INSTALLERS
        bootstrap_module._CORE_INSTALLERS = tuple(
            (
                ("ratelimit", "atri_x.agent.does_not_exist", "install_ratelimit_services")
                if item[0] == "ratelimit"
                else item
            )
            for item in original
        )
        return original

    def test_fail_fast_is_off_by_default_and_reports_degradation(self):
        fixture = _Fixture()
        original = self._break_core_installer()
        try:
            agent, _task = asyncio.run(fixture.build_and_quiesce())
        finally:
            import atri_x.agent.bootstrap as bootstrap_module

            bootstrap_module._CORE_INSTALLERS = original
        self.assertFalse(agent.agent_services_report["ok"])

    def test_fail_fast_aborts_startup(self):
        fixture = _Fixture(
            runtime=SimpleNamespace(fail_fast_agent_services=True)
        )
        original = self._break_core_installer()
        try:
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(fixture.build())
        finally:
            import atri_x.agent.bootstrap as bootstrap_module

            bootstrap_module._CORE_INSTALLERS = original
        self.assertIn("fail_fast_agent_services", str(caught.exception))
        self.assertIn("ratelimit", str(caught.exception))


class TestDoctorCommandUsesLiveCapabilities(unittest.TestCase):
    """``/doctor``（service 模式）必须复用启动期快照，而不是伪造通过。"""

    def setUp(self):
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)

    def test_missing_hook_service_is_reported(self):
        fixture = _Fixture()  # 没有 hook 控制面 = 接线缺失
        agent, _task = asyncio.run(fixture.build_and_quiesce())
        result = asyncio.run(agent.commands.dispatch(_superadmin_ctx(), "/doctor"))
        text = result.content
        self.assertIn("hook_service", text)
        self.assertIn("not wired", text)

    def test_wired_capabilities_are_not_reported_as_problems(self):
        fixture = _Fixture(hook_service=_HookService(), ops=_OPS_WITH_RATE_LIMIT)
        agent, _task = asyncio.run(fixture.build_and_quiesce())
        result = asyncio.run(agent.commands.dispatch(_superadmin_ctx(), "/doctor"))
        text = result.content
        self.assertNotIn("hook_service", text)
        self.assertNotIn("inbound_rate_limit", text)
        self.assertNotIn("redaction", text)


if __name__ == "__main__":
    unittest.main()
