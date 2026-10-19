"""``atri_x.agent.dsh`` 深度路由的离线测试（不依赖 Node / 真 dsh）。

覆盖四层：

1. :func:`atri_x.agent.dsh.router.score_task` —— 任务特征打分的样例标定；
2. :class:`atri_x.agent.dsh.router.DshRouter` —— 阈值、成本闸门、熔断、可用性缓存；
3. :mod:`atri_x.agent.dsh.handoff` —— 上下文交接 prompt 与 ACP 用量折算；
4. :class:`atri_x.agent.dsh.runtime.DshRuntime` 与
   :func:`atri_x.agent.dsh.route_call.maybe_route_to_dsh` —— 用
   ``tests/_dsh_fake_acp_server.py`` 真跑子进程，验证流式回传、预算记账、
   失败回退、部分输出收尾。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atri_x.agent.dsh.handoff import (
    build_handoff_prompt,
    latest_user_text,
    usage_from_acp,
)
from atri_x.agent.dsh.route_call import maybe_route_to_dsh, resolve_credential_env
from atri_x.agent.dsh.router import (
    DshRouteConfig,
    DshRouter,
    score_task,
)
from atri_x.agent.dsh.runtime import (
    DshRuntime,
    WorkspacePaths,
    child_environment,
    resolve_paths,
)
from atri_x.agent.dsh.types import AcpUsage
from atri_x.config import AgentDshConfig
from atri_x.errors import InvalidError
from atri_x.llm import LLMMessage, RoleSystem, RoleUser, text_segments
from atri_x.storage import SessionModeWork

FAKE_SERVER = Path(__file__).resolve().parent / "_dsh_fake_acp_server.py"
TMP_ROOT = Path(__file__).resolve().parent / "_tmp_dsh_route"


def _posix(path: Path | str) -> str:
    """Windows 路径转正斜杠（shlex 不把反斜杠当转义符）。"""
    return str(path).replace("\\", "/")


def _fake_command() -> str:
    """假 ACP 服务端的启动命令（字符串形态，走 shlex 拆分）。"""
    return f"{_posix(sys.executable)} -u {_posix(FAKE_SERVER)}"


def _config(**overrides: object) -> DshRouteConfig:
    """测试用路由配置：命令固定指向假服务端。"""
    values: dict[str, object] = {
        "enabled": True,
        "min_score": 0.0,
        "lambda_penalty": 0.0,
        "cost_ratio": 1.0,
        "command": _fake_command(),
        "timeout_seconds": 20.0,
        "max_history_messages": 8,
        "prompt_char_budget": 12000,
        "workspace": _posix(TMP_ROOT / "workspace"),
        "home": _posix(TMP_ROOT / "home"),
        "circuit_breaker_failures": 3,
        "circuit_breaker_cooldown_seconds": 300.0,
    }
    values.update(overrides)
    return DshRouteConfig(**values)  # type: ignore[arg-type]


def _messages(text: str) -> list[LLMMessage]:
    """一条用户消息 + 一条系统设定。"""
    return [
        LLMMessage(role=RoleSystem, segments=text_segments("你是 ATRI。")),
        LLMMessage(role=RoleUser, segments=text_segments(text)),
    ]


class ScoreTaskTest(unittest.TestCase):
    """任务特征打分：寒暄不路由、工程任务路由。"""

    def test_smalltalk_scores_zero(self) -> None:
        for text in ("你好", "在吗？", "谢谢啦", "晚上好"):
            with self.subTest(text=text):
                _features, score = score_task(text)
                self.assertLess(score, 0.1)

    def test_plain_question_scores_low(self) -> None:
        _features, score = score_task("今天天气怎么样")
        self.assertLess(score, 0.2)

    def test_code_task_scores_high(self) -> None:
        text = "写一个 Python 脚本，读取 data.json，统计每个用户的订单数，然后导出 CSV"
        features, score = score_task(text)
        self.assertGreaterEqual(score, 0.4)
        self.assertGreaterEqual(features.paths, 1)
        self.assertGreaterEqual(features.keywords, 2)

    def test_refactor_task_scores_high(self) -> None:
        _features, score = score_task("重构 utils.py 里的 parse 函数，抽出公共逻辑并补测试")
        self.assertGreaterEqual(score, 0.4)

    def test_error_traceback_scores(self) -> None:
        _features, score = score_task(
            '帮我看看这个报错：Traceback (most recent call last): File "main.py", line 12'
        )
        self.assertGreaterEqual(score, 0.35)

    def test_empty_text_scores_zero(self) -> None:
        features, score = score_task("   ")
        self.assertEqual(score, 0.0)
        self.assertEqual(features.chars, 0)

    def test_translation_task_stays_low(self) -> None:
        _features, score = score_task("把这句话翻译成英文：今天天气不错")
        self.assertLess(score, 0.3)


class RouterDecisionTest(unittest.TestCase):
    """阈值 / 成本闸门 / 熔断 / 可用性。"""

    def make_router(self, **overrides: object) -> DshRouter:
        return DshRouter(
            _config(**overrides), resolve=lambda _explicit: "python -m nothing"
        )

    def test_disabled_short_circuits(self) -> None:
        router = self.make_router(enabled=False, min_score=0.0)
        decision = router.decide("重构 utils.py 的函数并补测试")
        self.assertFalse(decision.use_dsh)
        self.assertEqual(decision.reason, "disabled")

    def test_unavailable_when_no_command(self) -> None:
        router = DshRouter(_config(), resolve=lambda _explicit: None)
        decision = router.decide("重构 utils.py 的函数并补测试")
        self.assertFalse(decision.use_dsh)
        self.assertEqual(decision.reason, "unavailable")

    def test_below_threshold(self) -> None:
        router = self.make_router(min_score=0.99)
        decision = router.decide("你好")
        self.assertFalse(decision.use_dsh)
        self.assertEqual(decision.reason, "below_threshold")

    def test_routed_above_threshold(self) -> None:
        router = self.make_router(min_score=0.1)
        decision = router.decide("重构 utils.py 里的 parse 函数并补测试")
        self.assertTrue(decision.use_dsh)
        self.assertEqual(decision.reason, "routed")
        self.assertGreaterEqual(decision.score, 0.1)

    def test_cost_gate_blocks_large_expensive_call(self) -> None:
        router = self.make_router(
            min_score=0.1, cost_ratio=4.0, cost_scale_tokens=1000, lambda_penalty=2.0
        )
        decision = router.decide("重构 utils.py 里的 parse 函数并补测试", estimated_tokens=50000)
        self.assertFalse(decision.use_dsh)
        self.assertEqual(decision.reason, "cost_gate")
        self.assertGreater(decision.cost_pressure, 0.5)

    def test_cost_gate_ignores_small_prompt(self) -> None:
        router = self.make_router(min_score=0.1, cost_ratio=4.0, lambda_penalty=2.0)
        decision = router.decide("重构 utils.py 里的 parse 函数并补测试", estimated_tokens=10)
        self.assertTrue(decision.use_dsh)

    def test_availability_cached_until_refresh(self) -> None:
        calls: list[str] = []

        def resolve(explicit: str) -> str:
            calls.append(explicit)
            return "python -m nothing"

        router = DshRouter(_config(), resolve=resolve)
        self.assertTrue(router.available())
        self.assertTrue(router.available())
        self.assertEqual(len(calls), 1)
        self.assertTrue(router.available(refresh=True))
        self.assertEqual(len(calls), 2)

    def test_breaker_opens_after_failures_and_recovers(self) -> None:
        clock = [1000.0]
        router = DshRouter(
            _config(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=10.0),
            resolve=lambda _explicit: "python -m nothing",
            clock=lambda: clock[0],
        )
        self.assertFalse(router.record_failure())
        self.assertTrue(router.record_failure())  # 打开熔断
        self.assertTrue(router.breaker_open())
        decision = router.decide("重构 utils.py 里的 parse 函数并补测试")
        self.assertEqual(decision.reason, "breaker_open")
        clock[0] += 11.0  # 冷却到期
        self.assertFalse(router.breaker_open())
        self.assertEqual(router.breaker_state()["failures"], 0)

    def test_success_resets_breaker(self) -> None:
        router = self.make_router(circuit_breaker_failures=2)
        router.record_failure()
        router.record_success()
        self.assertEqual(router.breaker_state()["failures"], 0)
        self.assertFalse(router.breaker_open())

    def test_notify_once_per_cooldown(self) -> None:
        router = self.make_router()
        self.assertTrue(router.note_notified())
        self.assertFalse(router.note_notified())
        router.record_success()
        self.assertTrue(router.note_notified())

    def test_config_from_section_reads_dataclass(self) -> None:
        config = DshRouteConfig.from_section(
            AgentDshConfig(
                enabled=False,
                min_score=0.7,
                model="m1",
                credential_provider="deepseek",
                credential_env="MY_DSH_KEY",
                credential_base_url_env="MY_DSH_BASE_URL",
            )
        )
        self.assertFalse(config.enabled)
        self.assertEqual(config.min_score, 0.7)
        self.assertEqual(config.model, "m1")
        self.assertEqual(config.credential_provider, "deepseek")
        self.assertEqual(config.credential_env, "MY_DSH_KEY")
        self.assertEqual(config.credential_base_url_env, "MY_DSH_BASE_URL")

    def test_config_credential_defaults(self) -> None:
        """空串仍是「内置默认」口径：变量名回落由 ``resolve_credential_env`` 兜底。"""
        config = DshRouteConfig.from_section(AgentDshConfig(credential_env=""))
        self.assertEqual(config.credential_provider, "")
        self.assertEqual(config.credential_env, "")
        self.assertEqual(DshRouteConfig.from_section(None).credential_env, "DEEPSEEK_API_KEY")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            env = resolve_credential_env(SimpleNamespace(providers={}), config)
        self.assertEqual(env, {})

    def test_config_from_missing_section_uses_defaults(self) -> None:
        defaults = DshRouteConfig.from_section(None)
        self.assertTrue(defaults.enabled)
        self.assertEqual(defaults.min_score, 0.40)

    def test_describe_reports_availability(self) -> None:
        router = self.make_router()
        snapshot = router.describe()
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["enabled"])
        self.assertIn("breaker", snapshot)


class HandoffTest(unittest.TestCase):
    """上下文交接 prompt 与用量折算。"""

    def test_prompt_contains_system_and_current_message(self) -> None:
        prompt = build_handoff_prompt(_messages("重构 utils.py 并补测试"))
        self.assertIn("[系统设定]", prompt)
        self.assertIn("你是 ATRI。", prompt)
        self.assertIn("[当前用户消息]", prompt)
        self.assertIn("重构 utils.py 并补测试", prompt)
        self.assertIn("不要复述本说明", prompt)

    def test_history_is_trimmed(self) -> None:
        messages = [
            LLMMessage(role=RoleUser, segments=text_segments("第 1 轮提问")),
            LLMMessage(role=RoleUser, segments=text_segments("第 2 轮提问")),
            LLMMessage(role=RoleUser, segments=text_segments("第 3 轮提问")),
            LLMMessage(role=RoleUser, segments=text_segments("当前问题")),
        ]
        prompt = build_handoff_prompt(
            messages, max_history_messages=1, char_budget=12000
        )
        self.assertIn("第 3 轮提问", prompt)
        self.assertNotIn("第 1 轮提问", prompt)
        self.assertNotIn("第 2 轮提问", prompt)
        self.assertIn("当前问题", prompt)

    def test_char_budget_drops_oldest_history(self) -> None:
        messages = [LLMMessage(role=RoleUser, segments=text_segments("旧" * 5000))]
        messages.append(LLMMessage(role=RoleUser, segments=text_segments("新问题")))
        prompt = build_handoff_prompt(
            messages, max_history_messages=8, char_budget=1200
        )
        self.assertIn("新问题", prompt)
        self.assertLessEqual(len(prompt), 1200)

    def test_latest_user_text(self) -> None:
        self.assertEqual(latest_user_text(_messages("最后一句")), "最后一句")
        self.assertEqual(latest_user_text([]), "")

    def test_usage_from_acp(self) -> None:
        usage = usage_from_acp(
            AcpUsage(total_tokens=123, input_tokens=100, output_tokens=23, cached_read_tokens=50)
        )
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.completion_tokens, 23)
        self.assertEqual(usage.total_tokens, 123)
        self.assertEqual(usage.cache_hit_tokens, 50)

    def test_usage_from_acp_falls_back_to_sum(self) -> None:
        usage = usage_from_acp(AcpUsage(input_tokens=10, output_tokens=5))
        self.assertEqual(usage.total_tokens, 15)

    def test_resolve_paths_relative_to_config_dir(self) -> None:
        import os

        agent = SimpleNamespace(state_path=str(TMP_ROOT / "state.toml"))
        paths = resolve_paths(agent, DshRouteConfig())
        self.assertTrue(paths.workspace.endswith(os.path.join("dsh", "workspace")))
        self.assertIn("home", paths.home)


class CredentialEnvTest(unittest.TestCase):
    """凭据注入与环境收敛（P0：DSH 侧 ``MISSING_CREDENTIAL``）。"""

    def _agent(self, providers: dict[str, object], work: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            providers=providers,
            mode_models={SessionModeWork: SimpleNamespace(provider=work)} if work else {},
        )

    def test_child_environment_drops_sensitive_and_injects(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ATRIX_TEST_SESSION_TOKEN": "leak", "ATRIX_TEST_PLAIN": "keep"},
            clear=False,
        ):
            env = child_environment({"DEEPSEEK_API_KEY": "sk-live", "EMPTY_ONE": ""})
        self.assertNotIn("ATRIX_TEST_SESSION_TOKEN", env)
        self.assertEqual(env.get("ATRIX_TEST_PLAIN"), "keep")
        self.assertEqual(env.get("DEEPSEEK_API_KEY"), "sk-live")
        self.assertNotIn("EMPTY_ONE", env)
        self.assertIn("PATH", env)

    def test_auto_prefers_provider_named_deepseek(self) -> None:
        agent = self._agent(
            {
                "deepseek": SimpleNamespace(api_key="k-deepseek", base_url="https://api.deepseek.com"),
                "openai": SimpleNamespace(api_key="k-openai", base_url="https://api.openai.com/v1"),
            },
            work="openai",
        )
        env = resolve_credential_env(agent, DshRouteConfig())
        # 官方 base_url 不额外下发覆盖变量。
        self.assertEqual(env, {"DEEPSEEK_API_KEY": "k-deepseek"})

    def test_auto_falls_back_to_work_provider(self) -> None:
        agent = self._agent(
            {"openai": SimpleNamespace(api_key="k-openai", base_url="https://api.openai.com/v1")},
            work="openai",
        )
        env = resolve_credential_env(agent, DshRouteConfig())
        self.assertEqual(env["DEEPSEEK_API_KEY"], "k-openai")
        self.assertEqual(env["DEEPSEEK_BASE_URL"], "https://api.openai.com/v1")

    def test_named_provider_and_custom_env_names(self) -> None:
        agent = self._agent(
            {
                "proxy": SimpleNamespace(api_key="k-proxy", base_url="https://gw.example.com/v1"),
                "deepseek": SimpleNamespace(api_key="k-deepseek", base_url=""),
            }
        )
        config = DshRouteConfig(
            credential_provider="proxy",
            credential_env="MY_KEY",
            credential_base_url_env="MY_BASE_URL",
        )
        env = resolve_credential_env(agent, config)
        self.assertEqual(env, {"MY_KEY": "k-proxy", "MY_BASE_URL": "https://gw.example.com/v1"})

    def test_empty_key_does_not_inject_blank(self) -> None:
        agent = self._agent({"deepseek": SimpleNamespace(api_key="", base_url="")})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            self.assertEqual(resolve_credential_env(agent, DshRouteConfig()), {})

    def test_empty_key_falls_back_to_inherited_value(self) -> None:
        agent = self._agent({"deepseek": SimpleNamespace(api_key="", base_url="")})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "inherited"}, clear=False):
            self.assertEqual(
                resolve_credential_env(agent, DshRouteConfig()),
                {"DEEPSEEK_API_KEY": "inherited"},
            )

    def test_api_key_env_is_a_fallback(self) -> None:
        """只声明了 ``api_key_env`` 的 provider 也能供上密钥。"""
        agent = self._agent(
            {
                "deepseek": SimpleNamespace(
                    api_key="", api_key_env="ATRIX_TEST_PROVIDER_KEY", base_url=""
                )
            }
        )
        with mock.patch.dict(
            os.environ, {"ATRIX_TEST_PROVIDER_KEY": "from-env"}, clear=False
        ):
            self.assertEqual(
                resolve_credential_env(agent, DshRouteConfig()),
                {"DEEPSEEK_API_KEY": "from-env"},
            )


class CredentialWiringTest(unittest.TestCase):
    """``DshRuntime`` 真的把收敛后的环境与 ``isolate_env`` 交给客户端。"""

    def test_ensure_client_passes_isolated_env(self) -> None:
        calls: list[dict[str, object]] = []

        def factory(command: object, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(alive=False, start=lambda: None)

        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        runtime = DshRuntime(
            _config(),
            command=_fake_command(),
            paths=WorkspacePaths(
                workspace=str(TMP_ROOT / "env-ws"), home=str(TMP_ROOT / "env-home")
            ),
            client_factory=factory,
            extra_env={"DEEPSEEK_API_KEY": "sk-live"},
        )
        with mock.patch.dict(os.environ, {"ATRIX_TEST_SESSION_TOKEN": "leak"}, clear=False):
            runtime._ensure_client()
        self.assertEqual(len(calls), 1)
        kwargs = calls[0]
        self.assertTrue(kwargs["isolate_env"])
        env = kwargs["env"]
        assert isinstance(env, dict)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-live")
        self.assertNotIn("ATRIX_TEST_SESSION_TOKEN", env)
        self.assertEqual(env["DSH_HOME"], str(TMP_ROOT / "env-home"))


class RuntimeTest(unittest.TestCase):
    """真子进程（假 ACP 服务端）跑通一轮 prompt。"""

    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.paths = WorkspacePaths(
            workspace=str(TMP_ROOT / "runtime-ws"), home=str(TMP_ROOT / "runtime-home")
        )
        self.runtime = DshRuntime(
            _config(), command=_fake_command(), paths=self.paths
        )

    def tearDown(self) -> None:
        self.runtime.close()

    def test_run_streams_text_and_usage(self) -> None:
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        async def go() -> object:
            return await self.runtime.run("普通提问", on_chunk=on_chunk)

        result = asyncio.run(go())
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "你好，世界")
        self.assertEqual(chunks, ["你好，", "世界"])
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertEqual(result.usage.output_tokens, 23)
        self.assertIn("fake-dsh-acp/0.0.1", result.server)
        self.assertTrue(any("bash" in title for title in result.tool_titles))

    def test_run_reuses_process_across_sessions(self) -> None:
        async def go() -> tuple[str, str]:
            first = await self.runtime.run("第一轮")
            second = await self.runtime.run("第二轮")
            return first.session_id, second.session_id

        first_id, second_id = asyncio.run(go())
        self.assertEqual(first_id, "sess-1")
        self.assertEqual(second_id, "sess-2")

    def test_describe_reports_running(self) -> None:
        asyncio.run(self.runtime.run("跑一下"))
        snapshot = self.runtime.describe()
        self.assertTrue(snapshot["running"])
        self.assertEqual(snapshot["workspace"], self.paths.workspace)
        self.assertIn("extraEnvKeys", snapshot)

    def test_model_override_rejected_degrades_to_kernel_default(self) -> None:
        """``[agent.dsh].model`` 填裸模型 id：本轮降级为内核默认，不再每轮硬失败。"""
        runtime = DshRuntime(
            _config(model="deepseek-v4-flash"),
            command=_fake_command(),
            paths=self.paths,
            extra_env={"FAKE_ACP_STRICT_MODEL": "1"},
        )
        try:
            first = asyncio.run(runtime.run("普通提问"))
            second = asyncio.run(runtime.run("普通提问"))
        finally:
            runtime.close()
        self.assertTrue(first.ok)
        self.assertEqual(first.text, "你好，世界")
        self.assertTrue(any("model override rejected" in item for item in first.warnings))
        self.assertEqual(second.warnings, [])  # 同一进程内不再重复尝试
        self.assertTrue(second.ok)

    def test_reasoning_effort_override_rejected_independently(self) -> None:
        runtime = DshRuntime(
            _config(model="fake-model", reasoning_effort="ultra"),
            command=_fake_command(),
            paths=self.paths,
            extra_env={"FAKE_ACP_STRICT_MODEL": "1"},
        )
        try:
            result = asyncio.run(runtime.run("普通提问"))
        finally:
            runtime.close()
        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("reasoning_effort override rejected", result.warnings[0])


class _FakeStream:
    """记录 stream_append 的最小替身。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def append(self, ctx: object, text: str) -> None:
        self.chunks.append(text)


class _FakeOut:
    """记录通知/思考的最小替身。"""

    def __init__(self) -> None:
        self.notices: list[tuple[int, str]] = []
        self.reasoning: list[str] = []

    async def send_notice(self, ctx: object, level: int, text: str) -> None:
        self.notices.append((level, text))

    async def send_reasoning(self, ctx: object, text: str) -> None:
        self.reasoning.append(text)


class _FakeAgent:
    """``maybe_route_to_dsh`` 需要的最小 Agent 面。"""

    def __init__(
        self,
        config: DshRouteConfig,
        *,
        deny_budget: bool = False,
        no_reservation: bool = False,
    ) -> None:
        self.dsh_config = SimpleNamespace(**config.__dict__)
        self.services: dict[str, object] = {}
        self.state_path = str(TMP_ROOT / "state.toml")
        self.deny_budget = deny_budget
        self.no_reservation = no_reservation
        self.budget_calls: list[tuple[str, int]] = []
        self.settled: list[tuple[object, object, bool]] = []
        self.recorded: list[tuple[str, object, str]] = []
        self.audits: list[tuple[str, tuple[object, ...]]] = []
        self.allow_output = True

    # -- Agent 面 ---------------------------------------------------------
    def begin_chat_budget(self, ctx, selection, messages, tools, max_output, call_id=""):
        if self.deny_budget:
            raise InvalidError("额度不足")
        self.budget_calls.append((selection.model, max_output))
        return None if self.no_reservation else "reservation"

    def settle_chat_budget(self, ctx, reservation, usage, release=False):
        self.settled.append((reservation, usage, release))

    def record_chat_usage(self, ctx, model, usage, call_id=""):
        self.recorded.append((model, usage, call_id))

    def turn_output_allowed(self, ctx) -> bool:
        return self.allow_output

    def audit(self, event: str, *attrs: object) -> None:
        self.audits.append((event, attrs))

    def audit_debug(self, event: str, *attrs: object) -> None:
        self.audits.append((event, attrs))

    def has_audit(self, name: str) -> bool:
        return any(event == name for event, _attrs in self.audits)

    def audit_attrs(self, name: str) -> dict[str, object]:
        """把某条审计的扁平 ``(key, value, ...)`` 属性还原成字典。"""
        for event, attrs in self.audits:
            if event == name:
                return {
                    str(attrs[index]): attrs[index + 1]
                    for index in range(0, len(attrs) - 1, 2)
                }
        return {}


class RouteCallTest(unittest.TestCase):
    """``maybe_route_to_dsh``：预算、流式、记账、失败回退。"""

    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def _selection(self) -> SimpleNamespace:
        return SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")

    def _run(self, agent: _FakeAgent, text: str = "重构 utils.py 里的 parse 函数并补测试") -> object:
        stream = _FakeStream()
        out = _FakeOut()
        return asyncio.run(
            maybe_route_to_dsh(
                agent,
                None,
                "sess-1",
                self._selection(),
                _messages(text),
                stream,
                out,
                max_output=256,
            )
        ), stream, out

    def test_routed_result_and_budget(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0))
        result, stream, out = self._run(agent)
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "你好，世界")
        self.assertEqual(stream.chunks, ["你好，", "世界"])
        self.assertEqual(agent.budget_calls, [("deepseek-v4-flash", 256)])
        self.assertEqual(len(agent.settled), 1)
        self.assertEqual(agent.settled[0][0], "reservation")
        # 内核回报了 usage：按真实值结算，且不再走软账本（已有预占）。
        self.assertEqual(agent.settled[0][1].prompt_tokens, 100)
        self.assertEqual(agent.settled[0][1].completion_tokens, 23)
        self.assertEqual(agent.recorded, [])
        self.assertTrue(agent.has_audit("dsh_route_succeeded"))
        self.assertTrue(agent.has_audit("dsh_route_decision"))
        self.assertTrue(agent.audit_attrs("dsh_route_succeeded")["usage_reported"])
        self.assertEqual(out.notices, [])

    def test_missing_usage_settles_as_uncertain(self) -> None:
        """内核不回 usage 时：保留预占并标 uncertain，而不是记 0。"""
        agent = _FakeAgent(_config(min_score=0.0))
        result, _stream, _out = self._run(
            agent, "__NO_USAGE__ 重构 utils.py 里的 parse 函数并补测试"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "没有 usage 的回答")
        self.assertEqual(len(agent.settled), 1)
        self.assertIsNone(agent.settled[0][1])  # → budget_ledger 走 uncertain 分支
        self.assertFalse(agent.settled[0][2])  # 不是 release
        self.assertEqual(agent.recorded, [])
        self.assertFalse(agent.audit_attrs("dsh_route_succeeded")["usage_reported"])

    def test_soft_ledger_records_real_usage_only(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0), no_reservation=True)
        result, _stream, _out = self._run(agent)
        self.assertIsNotNone(result)
        self.assertEqual(len(agent.recorded), 1)
        self.assertEqual(agent.recorded[0][1].prompt_tokens, 100)
        self.assertEqual(agent.settled, [])

    def test_soft_ledger_skips_without_usage(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0), no_reservation=True)
        result, _stream, _out = self._run(
            agent, "__NO_USAGE__ 重构 utils.py 里的 parse 函数并补测试"
        )
        self.assertIsNotNone(result)
        self.assertEqual(agent.recorded, [])
        self.assertEqual(agent.settled, [])

    def test_below_threshold_skips_routing(self) -> None:
        agent = _FakeAgent(_config(min_score=0.99))
        result, stream, _out = self._run(agent)
        self.assertIsNone(result)
        self.assertEqual(stream.chunks, [])
        self.assertEqual(agent.budget_calls, [])

    def test_budget_denied_skips_routing(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0), deny_budget=True)
        result, _stream, _out = self._run(agent)
        self.assertIsNone(result)
        self.assertTrue(agent.has_audit("dsh_route_budget_denied"))

    def test_crash_falls_back_without_output(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0))
        stream = _FakeStream()
        result = asyncio.run(
            maybe_route_to_dsh(
                agent,
                None,
                "sess-1",
                self._selection(),
                _messages("__CRASH__ 重构 utils.py"),
                stream,
                _FakeOut(),
            )
        )
        self.assertIsNone(result)
        self.assertEqual(stream.chunks, [])
        self.assertTrue(agent.has_audit("dsh_route_failed"))
        # 预占在"尚未受理"路径上释放
        self.assertEqual(agent.settled[0][0], "reservation")
        self.assertTrue(agent.settled[0][2])

    def test_crash_after_chunk_keeps_partial_output(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0))
        stream = _FakeStream()
        out = _FakeOut()
        result = asyncio.run(
            maybe_route_to_dsh(
                agent,
                None,
                "sess-1",
                self._selection(),
                _messages("__CRASH_AFTER_CHUNK__ 重构 utils.py"),
                stream,
                out,
            )
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "部分输出后崩溃")
        self.assertEqual(stream.chunks, ["部分输出后崩溃"])
        self.assertEqual(len(out.notices), 1)
        self.assertEqual(out.notices[0][0], 4)  # WARNING
        self.assertTrue(agent.has_audit("dsh_route_failed"))

    def test_output_gate_stops_streaming(self) -> None:
        agent = _FakeAgent(_config(min_score=0.0))
        agent.allow_output = False
        result, stream, _out = self._run(agent)
        # 被判定为"不该继续输出"：不记成功、不重放，返回空的部分结果。
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "")
        self.assertEqual(stream.chunks, [])
        self.assertTrue(agent.has_audit("dsh_route_stopped"))
        self.assertFalse(agent.has_audit("dsh_route_succeeded"))

    def test_unavailable_command_is_not_attempted(self) -> None:
        # 可用性判定在 router 层单测（``test_unavailable_when_no_command``）；
        # 这里再确认"不可用"不会进入预算预占，也就不会拉起任何子进程。
        router = DshRouter(_config(min_score=0.0), resolve=lambda _explicit: None)
        decision = router.decide("重构 utils.py 里的 parse 函数并补测试")
        self.assertFalse(decision.use_dsh)
        self.assertEqual(decision.reason, "unavailable")


class StatusTest(unittest.TestCase):
    """``python -m atri_x.agent.dsh.status`` 的状态采集。"""

    def test_collect_status_with_fake_config(self) -> None:
        from unittest import mock

        from atri_x.agent.dsh.status import collect_status

        config = SimpleNamespace(
            config_path=str(TMP_ROOT / "app.toml"),
            agent=SimpleNamespace(dsh=AgentDshConfig(enabled=False, min_score=0.5)),
        )
        with mock.patch("atri_x.config.load", return_value=config):
            report = collect_status("", ["重构 utils.py 的函数并补测试"])
        self.assertTrue(report["loaded"])
        self.assertFalse(report["enabled"])
        self.assertEqual(report["config"]["minScore"], 0.5)
        self.assertEqual(len(report["scores"]), 1)
        self.assertIn("features", report["scores"][0])

    def test_collect_status_reports_config_error(self) -> None:
        from atri_x.agent.dsh.status import collect_status

        report = collect_status(str(TMP_ROOT / "missing" / "app.toml"))
        self.assertFalse(report["loaded"])
        self.assertIn("error", report)

    def test_main_returns_nonzero_when_unavailable(self) -> None:
        import contextlib
        import io
        import os
        from unittest import mock

        from atri_x.agent.dsh.status import main

        config = SimpleNamespace(
            config_path=str(TMP_ROOT / "app.toml"),
            agent=SimpleNamespace(dsh=AgentDshConfig(enabled=True)),
        )
        saved = os.environ.pop("DSH_ACP_COMMAND", None)
        try:
            with mock.patch("atri_x.config.load", return_value=config):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--json"])
        finally:
            if saved is not None:
                os.environ["DSH_ACP_COMMAND"] = saved
        self.assertIn(code, (0, 1))


if __name__ == "__main__":  # pragma: no cover - 手工调试入口
    unittest.main()
