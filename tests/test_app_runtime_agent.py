"""``atri_x.app.runtime_agent.build_agent`` 的真实装配测试。

对应 Go：``internal/app/runtime.go`` 的 ``buildAgent``（含 ``core.go:428-434``
要求的工具运行时装配）。

这一组用例刻意**不走假 Agent / 假门面**：``build_agent`` 造的是真实
:class:`atri_x.agent.core.Agent`，``install_agent_services`` 是真实 installer，
``tool_runtime.registry`` 是 ``new_registry_with_builtin`` 造的真实
:class:`atri_x.tool.tool.Registry`（11 个内置工具）。只有「假存储」是假的。

覆盖两个第二轮发现的接线缺口：

* **P0-4（app 半边）**：``build_agent`` 把 ``tool_registry`` 传给 ``Options``，
  但 ``install_agent_services(agent)`` 没带 ``opts``，于是
  ``bootstrap._wire_extra_host_fields`` 里 ``options.get("registry")`` 恒为
  ``None`` 直接 return —— 工具注册表静默丢掉。修复后必须能在
  ``agent.tool_runtime`` 里看到真实注册表与工具。
* **运行期挂起**：``runtime_agent`` 曾 ``await maybe_await(start_group_services(ctx))``，
  而 ``start_group_services`` 返回常驻 Task（``while True: sleep``，群服务默认
  ``enabled=None`` → 启用），真实 ``build_agent`` 会永久停在 runtime 阶段。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from types import SimpleNamespace
from typing import Any

from atri_x.app.ops_health import VisionMetricsState
from atri_x.app.runtime_agent import build_agent
from atri_x.app.tool_runtime import ToolRuntime
from atri_x.config import FileDeliveryConfig, ModelSelection, ToolsConfig
from atri_x.security import default_policy
from atri_x.tool.builtin.file_manager import new_file_manager_with_media
from atri_x.tool.builtin.register import RegisterOptions, new_registry_with_builtin
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo

#: 临时目录放仓库根（PORTING §7.2：禁用 tempfile.mkdtemp）。
TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_app_runtime_agent_test")


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


def _config(root: str) -> SimpleNamespace:
    """一份能让 ``validate_options`` 通过的最小配置（字段与 ``Runtime`` 的读取点对齐）。"""
    return SimpleNamespace(
        config_path=os.path.join(root, "app.toml"),
        providers={"stub": SimpleNamespace(name="stub")},
        model_profiles={},
        tool_profiles={},
        mode_models={"work": ModelSelection(provider="stub", model="stub-model")},
        tools=ToolsConfig(),
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


class _Fixture:
    """真实 ``build_agent`` 的最小可运行环境（假存储 + 一个 stub provider 客户端）。"""

    def __init__(self) -> None:
        self.root = TMP_DIR
        os.makedirs(os.path.join(self.root, "sandbox"), exist_ok=True)
        self.config = _config(self.root)
        self.logger = _Logger()
        self.foundation = SimpleNamespace(
            config=self.config, logger=self.logger, logs=_Logs(), store=SimpleNamespace()
        )
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
            None,
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


if __name__ == "__main__":
    unittest.main()
