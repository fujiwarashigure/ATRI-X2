"""``atri_x.agent.cron`` / ``cron_tools`` 的测试。

覆盖：作用域/标题/元数据构造、缓存工具过滤、Skill 预载提示词拼接、
后台运行态检查、后台 shell 的风险裁决与审计。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from atri_x.agent import cron as cron_module
from atri_x.agent import cron_tools
from atri_x.background import (
    BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED,
    BACKGROUND_SHELL_AUDIT_REJECTED,
    BACKGROUND_SHELL_CRITICAL_MESSAGE,
    KindAtrinis,
    KindCron,
    RunRequest,
    background_tool_allowed,
)
from atri_x.llm import ToolCallRequest, text_segments
from atri_x.security import RoleSuperadmin
from atri_x.session import Scope, promote_to_foreground, was_promoted
from atri_x.storage import Session
from atri_x.tool.sandbox import SandboxContext, with_sandbox_context
from atri_x.toolrun import CachedTool
from tests.agent_fakes import FakeAgent


class HelperTests(unittest.TestCase):
    def test_scope_id_and_title(self) -> None:
        self.assertEqual(cron_module.background_scope_id("cron", "daily"), "cron:daily")
        self.assertEqual(cron_module.background_scope_id("", ""), "background:default")
        self.assertEqual(cron_module.background_title("cron", "daily"), "Cron: daily")
        self.assertEqual(cron_module.background_title("", ""), "Background")
        self.assertEqual(cron_module.background_title("atrinis", ""), "Atrinis")

    def test_session_mode(self) -> None:
        self.assertEqual(cron_module.normalize_background_session_mode("chat"), "chat")
        self.assertEqual(cron_module.normalize_background_session_mode("work"), "work")
        self.assertEqual(cron_module.normalize_background_session_mode(""), "work")
        self.assertEqual(cron_module.normalize_background_session_mode("bogus"), "work")

    def test_tool_list_names(self) -> None:
        self.assertEqual(
            cron_module.background_tool_list_names(
                [" a ", "", "discover_tool", "a", "b"]
            ),
            ["a", "b"],
        )
        self.assertEqual(cron_module.background_tool_list_names(None), [])

    def test_tool_background_kind(self) -> None:
        self.assertEqual(cron_module.tool_background_kind(KindCron), "cron")
        self.assertEqual(cron_module.tool_background_kind(KindAtrinis), "atrinis")
        self.assertEqual(cron_module.tool_background_kind("custom"), "custom")

    def test_prompt_with_skills(self) -> None:
        self.assertEqual(cron_module.background_prompt_with_skills("p", "  "), "p")
        merged = cron_module.background_prompt_with_skills("任务", "## Skill: x")
        self.assertTrue(merged.startswith("[系统预加载 Skill]"))
        self.assertIn("## Skill: x", merged)
        self.assertTrue(merged.endswith("[后台任务]\n\n任务"))

    def test_prompt_segments(self) -> None:
        from atri_x.llm import MessageSegment

        llm_segments = [
            MessageSegment(type="text", text="hi"),
            MessageSegment(type="image", url="http://x", media="m1", name="a.png"),
            MessageSegment(type="file", url="http://y", media="m2", name="b.txt"),
        ]
        platform_segments = cron_module.background_prompt_segments(llm_segments)
        self.assertEqual([item.type for item in platform_segments], ["text", "image", "file"])
        self.assertEqual(platform_segments[1].media_id, "m1")
        self.assertEqual(platform_segments[2].name, "b.txt")

    def test_metadata_map_and_merge(self) -> None:
        req = RunRequest(
            kind=KindCron,
            name="daily",
            metadata={"cron_job_name": "daily"},
            cached_tools=[CachedTool(name="shell"), CachedTool(name="discover_tool")],
        )
        data = json.loads(cron_module.background_session_metadata(req))
        self.assertEqual(data["background_kind"], "cron")
        self.assertEqual(data["background_name"], "daily")
        self.assertEqual(data["cron_job_name"], "daily")
        self.assertEqual([item["name"] for item in data["tool_cache"]], ["shell"])

        merged = json.loads(
            cron_module.merge_background_session_metadata(
                json.dumps({"keep": 1, "title_renamed": True}), req
            )
        )
        self.assertEqual(merged["keep"], 1)
        self.assertEqual(merged["background_name"], "daily")

        broken = json.loads(cron_module.merge_background_session_metadata("{bad", req))
        self.assertEqual(broken["background_kind"], "cron")

    def test_background_cached_tools_drops_denied(self) -> None:
        cached = [CachedTool(name="discover_tool"), CachedTool(name="workspace"), CachedTool(name="shell")]
        kept = [item.name for item in cron_module.background_cached_tools(cached)]
        self.assertEqual(kept, ["shell"])
        self.assertTrue(background_tool_allowed("shell"))
        self.assertFalse(background_tool_allowed("discover_tool"))

    def test_cron_session_metadata(self) -> None:
        data = json.loads(cron_module.cron_session_metadata("daily", "s1", True))
        self.assertEqual(data["cron_source_session_id"], "s1")
        self.assertTrue(data["cron_broadcast_copy"])


class SessionTests(unittest.TestCase):
    def test_taken_over_reads_store(self) -> None:
        agent = FakeAgent()
        agent.store.sessions().create(Session(id="bg1", metadata=""))
        taken, err = cron_module.background_session_taken_over(agent, None, "bg1")
        self.assertFalse(taken)
        self.assertIsNone(err)
        taken, err = cron_module.background_session_taken_over(agent, None, "missing")
        self.assertFalse(taken)
        self.assertIsNone(err)

    def test_taken_over_true_when_promoted(self) -> None:
        agent = FakeAgent()
        # 用真实提升助手造标记：手写 {"foreground_origin": "resume"} 不是接管标记
        # （``was_promoted`` 要求它是对象且至少一个字段非空），会让本用例静默 skip。
        promoted = Session(id="bg2", platform_scope_id="cron:daily")
        promote_to_foreground(promoted, Scope(actor_id="u1", platform="qq"))
        self.assertTrue(was_promoted(promoted))
        agent.store.sessions().create(promoted)

        taken, _ = cron_module.background_session_taken_over(agent, None, "bg2")
        self.assertTrue(taken)

    def test_must_stop_is_conservative_on_error(self) -> None:
        agent = FakeAgent()

        class Broken:
            def get(self, session_id: str) -> None:  # noqa: ARG002
                raise RuntimeError("boom")

        agent.store.sessions = lambda: Broken()  # type: ignore[assignment]
        self.assertTrue(cron_module.background_session_must_stop(agent, None, "x"))
        self.assertTrue(
            any(call[0] == "background_takeover_check_failed" for call in agent.audit_calls)
        )

    def test_create_background_session(self) -> None:
        import asyncio

        agent = FakeAgent()
        scope = agent.scope(None)
        req = RunRequest(kind=KindCron, name="daily", session_mode="work", prompt="p")
        session = asyncio.run(cron_module.background_session(agent, None, req, scope))
        self.assertEqual(session.title, "Cron: daily")
        self.assertEqual(session.mode, "work")
        self.assertEqual(json.loads(session.metadata)["background_name"], "daily")
        self.assertIs(agent.store.sessions().get(session.id), session)

    def test_ensure_background_session_rejects_promoted(self) -> None:
        import asyncio

        from atri_x.errors import BackgroundTakenOverError
        from atri_x.session import Scope, promote_to_foreground, was_promoted

        agent = FakeAgent()
        existing = Session(id="bg3", platform_scope_id="cron:daily")
        promote_to_foreground(existing, Scope(actor_id="u1", platform="qq"))
        self.assertTrue(was_promoted(existing))
        agent.store.sessions().create(existing)
        with self.assertRaises(BackgroundTakenOverError):
            asyncio.run(
                cron_module.background_session(
                    agent, None, RunRequest(kind=KindCron, session_id="bg3"), agent.scope(None)
                )
            )

    def test_ensure_background_session_merges_metadata(self) -> None:
        import asyncio

        agent = FakeAgent()
        existing = Session(id="bg4", metadata=json.dumps({"keep": 1}), mode="chat")
        agent.store.sessions().create(existing)
        req = RunRequest(kind=KindCron, name="daily", session_id="bg4", session_mode="work")
        session = asyncio.run(
            cron_module.ensure_background_session(agent, None, existing, req)
        )
        self.assertEqual(session.mode, "work")
        self.assertEqual(json.loads(session.metadata)["keep"], 1)

    def test_latest_assistant_message(self) -> None:
        import asyncio

        from atri_x.storage import Message

        agent = FakeAgent()
        agent.store.messages().append(Message(session_id="s1", role="user", content="hi"))
        agent.store.messages().append(
            Message(
                session_id="s1",
                role="assistant",
                content="done",
                metadata=json.dumps({"raw_text": "raw"}),
            )
        )
        message = asyncio.run(cron_module.latest_assistant_message(agent, None, "s1"))
        self.assertEqual(message.content, "done")
        self.assertEqual(cron_module.assistant_raw_text_from_metadata(message.metadata), "raw")
        with self.assertRaises(ValueError):
            asyncio.run(cron_module.latest_assistant_message(agent, None, "other"))
        self.assertEqual(cron_module.assistant_raw_text_from_metadata("{bad"), "")

    def test_preload_tool_list(self) -> None:
        import asyncio

        from tests.agent_fakes import FakeRegistry, FakeTool

        registry = FakeRegistry({"shell": FakeTool("shell")})
        agent = FakeAgent(registry=registry)
        agent.group_policy_calls = []
        session = Session(id="s1", mode="work")
        result = asyncio.run(
            cron_module.preload_background_resources(
                agent, None, session, ["shell", "discover_tool", "", "missing"]
            )
        )
        self.assertEqual(result.tools, ["shell"])
        self.assertEqual(result.skills, [])
        self.assertIn("shell", agent.discovered_tools["s1"])

    def test_preload_skip_result(self) -> None:
        from atri_x.tool import ToolReasonNotFound, ToolReasonRiskDenied, ToolReasonSuperadminOnly

        self.assertEqual(cron_module.preload_skip_result(ToolReasonRiskDenied), "rejected")
        self.assertEqual(cron_module.preload_skip_result(ToolReasonSuperadminOnly), "rejected")
        self.assertEqual(cron_module.preload_skip_result(ToolReasonNotFound), "skipped")


class RunBackgroundTests(unittest.TestCase):
    def test_run_background_happy_path(self) -> None:
        import asyncio

        from atri_x.storage import Message

        agent = FakeAgent()
        started: list[str] = []

        async def _start_background_chat(ctx: Any, session: Any, prompt: str) -> None:
            started.append(prompt)
            agent.store.messages().append(
                Message(session_id=session.id, role="assistant", content="report")
            )

        agent.start_background_chat = _start_background_chat
        req = RunRequest(kind=KindCron, name="daily", prompt="do it")
        result = asyncio.run(cron_module.run_background(agent, None, req))
        self.assertEqual(result.text, "report")
        self.assertFalse(result.taken_over)
        self.assertEqual(started, ["do it"])
        self.assertEqual(result.session_id, agent.store.sessions().rows[result.session_id].id)

    def test_run_cron_message_wraps_result(self) -> None:
        import asyncio

        from atri_x.cron.service import RunCronMessageRequest
        from atri_x.security import Actor
        from atri_x.storage import Message

        agent = FakeAgent()

        async def _start_background_chat(ctx: Any, session: Any, prompt: str) -> None:
            del ctx, prompt
            agent.store.messages().append(
                Message(session_id=session.id, role="assistant", content="ok")
            )

        agent.start_background_chat = _start_background_chat
        req = RunCronMessageRequest(
            job_name="daily", actor=Actor(id="u1", role=RoleSuperadmin), prompt="p"
        )
        result = asyncio.run(cron_module.run_cron_message(agent, None, req))
        self.assertEqual(result.text, "ok")
        self.assertFalse(result.taken_over)

    def test_audit_fields_for_preload(self) -> None:
        import asyncio

        from atri_x.storage import Message
        from tests.agent_fakes import FakeRegistry, FakeTool

        registry = FakeRegistry({"shell": FakeTool("shell")})
        agent = FakeAgent(registry=registry)

        async def _start_background_chat(ctx: Any, session: Any, prompt: str) -> None:
            del ctx, prompt
            agent.store.messages().append(
                Message(session_id=session.id, role="assistant", content="x")
            )

        agent.start_background_chat = _start_background_chat
        req = RunRequest(kind=KindCron, name="daily", prompt="p", tool_list_names=["shell"])
        asyncio.run(cron_module.run_background(agent, None, req))
        events = [call[0] for call in agent.audit_calls]
        self.assertIn("background_tool_preloaded", events)


class CronToolsTests(unittest.TestCase):
    """``cron_tools.confirm_background_sandbox_shell`` 的裁决 + 审计。

    本机 ``is_power_shell_env()`` 恒为 True（真实环境就是 PowerShell）：白名单 / 结构
    这些 bash 口径的分支必须显式构造 bash 主机；PowerShell 分支单独用 patch 构造。
    """

    def setUp(self) -> None:
        from unittest import mock

        self._risk_patch = mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=False
        )
        self._sandbox_patch = mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=False
        )
        self._risk_patch.start()
        self._sandbox_patch.start()

    def tearDown(self) -> None:
        self._risk_patch.stop()
        self._sandbox_patch.stop()

    def _ctx(self, kind: str = KindCron, background: bool = True) -> Any:
        sandbox = SandboxContext(
            root="data/sandbox",
            dir="data/sandbox/%s" % kind,
            background=background,
            background_kind=kind,
        )
        return with_sandbox_context({}, sandbox)

    def _call(self, cmd: str = "ls -la") -> ToolCallRequest:
        return ToolCallRequest(id="c1", name="shell", arguments=json.dumps({"cmd": cmd}))

    def audit_fields(self, agent: FakeAgent, event: str) -> list[dict]:
        out = []
        for call in agent.audit_calls:
            if not call or call[0] != event:
                continue
            out.append(dict(zip(call[1::2], call[2::2])))
        return out

    def test_auto_confirm_low_risk_allowlisted(self) -> None:
        agent = FakeAgent()
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", self._call("ls -la"), "low"
        )
        self.assertTrue(decision.confirm)
        self.assertTrue(decision.handled)
        confirm, handled = decision
        self.assertTrue(confirm and handled)
        events = [item[0] for item in agent.audit_calls]
        self.assertIn(BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, events)
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED)[0]
        self.assertIn("白名单", fields["reason"], "审计必须带可区分的原因")

    def test_high_risk_is_rejected(self) -> None:
        agent = FakeAgent()
        call = self._call("git clean -fd")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "high"
        )
        self.assertFalse(decision.confirm)
        self.assertTrue(decision.handled)
        events = [item[0] for item in agent.audit_calls]
        self.assertIn(BACKGROUND_SHELL_AUDIT_REJECTED, events)
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_REJECTED)[0]
        self.assertIn("低风险", fields["reason"])

    def test_reject_critical_and_message(self) -> None:
        from atri_x.llm import LLMMessage

        agent = FakeAgent()
        call = self._call("rm -rf /")
        message = LLMMessage(role="tool", name="shell", tool_call_id="c1")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "critical", message
        )
        self.assertFalse(decision.confirm)
        self.assertTrue(decision.handled)
        self.assertEqual(message.segments[0].text, BACKGROUND_SHELL_CRITICAL_MESSAGE)
        events = [item[0] for item in agent.audit_calls]
        self.assertIn(BACKGROUND_SHELL_AUDIT_REJECTED, events)
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_REJECTED)[0]
        self.assertTrue(fields["reason"])

    def test_missing_cmd_text_is_rejected(self) -> None:
        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="shell", arguments="{}")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "low"
        )
        self.assertTrue(decision.handled)
        self.assertFalse(decision.confirm, "拿不到命令文本必须 fail-closed")
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_REJECTED)[0]
        self.assertIn("命令文本", fields["reason"])

    def test_unparsable_arguments_are_rejected(self) -> None:
        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="shell", arguments="{not json")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "low"
        )
        self.assertFalse(decision.confirm)

    def test_allowlist_miss_reason_is_audited(self) -> None:
        agent = FakeAgent()
        # ``uname`` 是低风险只读命令，但不在内置白名单里 —— 这样才会走到
        # "白名单未命中"那条拒绝分支（``python tool.py`` 会先被判 medium 风险，
        # 命中的是风险超限分支，拿不到白名单原因）。
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(kind=KindAtrinis), "s1", self._call("uname -a"), "low"
        )
        self.assertFalse(decision.confirm)
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_REJECTED)[0]
        self.assertIn("白名单", fields["reason"])
        self.assertIn("uname", fields["reason"])
        self.assertEqual("shell", fields["tool"])
        self.assertEqual("s1", fields["session_id"])
        self.assertEqual(KindAtrinis, fields["kind"])
        self.assertEqual("data/sandbox/atrinis", fields["sandbox_dir"])

    def test_structure_violation_reason_is_audited(self) -> None:
        agent = FakeAgent()
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", self._call("ls | grep TODO"), "low"
        )
        self.assertFalse(decision.confirm)
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_REJECTED)[0]
        self.assertIn("管道", fields["reason"])

    def test_power_shell_host_reason_is_audited(self) -> None:
        from unittest import mock

        agent = FakeAgent()
        with mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=True
        ), mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=True
        ):
            decision = cron_tools.confirm_background_sandbox_shell(
                agent, self._ctx(), "s1", self._call("ls -la"), "low"
            )
        self.assertFalse(decision.confirm)
        fields = self.audit_fields(agent, BACKGROUND_SHELL_AUDIT_REJECTED)[0]
        self.assertIn("PowerShell", fields["reason"])

    def test_policy_from_registry_shell_tool_is_used(self) -> None:
        """确认阶段必须用装配层配到 ``ShellTool`` 上的策略，否则显式放行会失效。"""
        from tests.agent_fakes import FakeRegistry

        class _Policy:
            allow: frozenset[str] = frozenset()
            allow_all = True

        class _ShellTool:
            background_shell_policy = _Policy()

        registry = FakeRegistry({"shell": _ShellTool()})
        agent = FakeAgent(registry=registry)
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", self._call("python tool.py"), "high"
        )
        self.assertTrue(decision.confirm, "allow_all 必须能从注册表里的 shell 工具读到")

    def test_not_handled_for_foreground(self) -> None:
        agent = FakeAgent()
        call = self._call("ls -la")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(background=False), "s1", call, "high"
        )
        self.assertFalse(decision.handled)
        self.assertEqual(agent.audit_calls, [])

    def test_not_handled_for_unknown_kind(self) -> None:
        agent = FakeAgent()
        call = self._call("ls -la")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(kind="other"), "s1", call, "high"
        )
        self.assertFalse(decision.handled)

    def test_not_handled_for_other_tool(self) -> None:
        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="read_file", arguments="{}")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "high"
        )
        self.assertFalse(decision.handled)

    def test_pure_decision_helper(self) -> None:
        sandbox = SandboxContext(background=True, background_kind=KindAtrinis)
        decision = cron_tools.background_shell_decision(
            sandbox, "shell", "critical", cmd_text="rm -rf /"
        )
        self.assertTrue(decision.handled)
        self.assertFalse(decision.confirm)
        allowed = cron_tools.background_shell_decision(
            sandbox, "shell", "low", cmd_text="ls -la"
        )
        self.assertTrue(allowed.confirm)

    def test_shell_command_text_reads_only_cmd_key(self) -> None:
        self.assertEqual(
            "ls -la",
            cron_tools.shell_command_text(
                ToolCallRequest(id="c1", name="shell", arguments='{"cmd":" ls -la "}')
            ),
        )
        for arguments in ("{}", "", "{not json", '{"cmd":3}', '["cmd"]'):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    "",
                    cron_tools.shell_command_text(
                        ToolCallRequest(id="c1", name="shell", arguments=arguments)
                    ),
                )


class BackgroundConfirmationOrderTests(unittest.TestCase):
    """后台 shell 的裁决必须先于前台的"本会话自动确认"（runner.py 的 ``_confirm``）。

    runner.py 属 Lead 的写范围，这里只钉行为：``deps.confirm_background_tool`` 返回
    ``handled=True`` 时，``needs_tool_confirmation`` / ``confirm_tool_call`` 都不该被
    调用 —— 也就永远走不到 ``toolrun_adapter`` 里的 ``_auto_confirmed``。
    """

    def _confirm(self, background_result, *, strict=True):
        """跑一次 ``runner._confirm``。

        ``strict=True`` 时前台确认路径（``risk_detail`` / ``confirm_tool_call``）被调用
        即断言失败 —— 用来证明后台已裁决的调用**不会**走到前台的"本会话自动确认"。
        ``strict=False`` 让前台路径正常返回，用来证明 ``handled=False`` 时会正常回落。
        """
        import asyncio

        from atri_x.errors import ForbiddenError
        from atri_x.llm import ToolCallRequest as Call
        from atri_x.llm import ToolFunctionSchema, ToolSchema
        from atri_x.security import Actor, Policy, RoleSuperadmin, RiskHigh
        from atri_x.tool.tool import Info, RiskAssessment
        from atri_x.toolrun import ConfirmResult
        from atri_x.toolrun import runner as runner_mod
        from atri_x.toolrun.toolrun import ResolvedTool

        seen: list[str] = []

        class _Native:
            def info(self) -> Info:
                return Info(name="shell", description="shell", risk=RiskHigh)

            def schema(self) -> ToolSchema:
                return ToolSchema(function=ToolFunctionSchema(name="shell"))

        class _Manager:
            policy = Policy()

            async def risk_detail(self, ctx, resolved, call):
                seen.append("risk_detail")
                if strict:
                    raise AssertionError("后台已裁决，不该再走前台确认细节")
                return "detail"

        class _Deps:
            async def confirm_background_tool(self, ctx, session_id, call, resolved, assessment):
                seen.append("confirm_background_tool")
                return background_result

            async def confirm_tool_call(self, ctx, session_id, call, assessment, detail):
                seen.append("confirm_tool_call")
                if strict:
                    raise AssertionError("后台已裁决，不该再问用户（前台自动确认路径）")
                return ConfirmResult(allowed=True, extra=detail)

            class _Policy:
                def can_use_tool(self, actor, risk, owner_scoped=False):
                    return True

                def needs_tool_confirmation(self, actor, risk):
                    seen.append("needs_tool_confirmation")
                    return True

        deps = _Deps()
        call = Call(id="1", name="shell", arguments='{"cmd":"ls -la"}')
        resolved = ResolvedTool(
            name="shell", available=True, source="native", native=_Native()
        )
        decision = asyncio.run(
            runner_mod._confirm(
                _Manager(),
                deps,
                {},
                Actor(role=RoleSuperadmin),
                "s1",
                call,
                resolved,
                RiskAssessment(level=RiskHigh),
            )
        )
        del ForbiddenError
        return decision, seen

    def test_background_handled_denial_never_reaches_foreground_confirm(self) -> None:
        from atri_x.toolrun import ConfirmResult

        decision, seen = self._confirm((ConfirmResult(allowed=False), True))
        self.assertFalse(decision.allowed)
        self.assertEqual(["confirm_background_tool"], seen)

    def test_background_handled_allowance_never_reaches_foreground_confirm(self) -> None:
        from atri_x.toolrun import ConfirmResult

        decision, seen = self._confirm((ConfirmResult(allowed=True), True))
        self.assertTrue(decision.allowed)
        self.assertEqual(["confirm_background_tool"], seen)

    def test_unhandled_background_falls_through_to_foreground_confirm(self) -> None:
        from atri_x.toolrun import ConfirmResult

        decision, seen = self._confirm((ConfirmResult(), False), strict=False)
        self.assertTrue(decision.allowed)
        # ``needs_tool_confirmation`` 走的是 ``_Manager.policy`` 的真实 Policy（不会记账），
        # 所以这里只看前台确认真的被走到了。
        self.assertEqual(
            ["confirm_background_tool", "risk_detail", "confirm_tool_call"],
            seen,
        )


if __name__ == "__main__":
    unittest.main()
