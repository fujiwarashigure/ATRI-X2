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
    def _ctx(self, kind: str = KindCron, background: bool = True) -> Any:
        sandbox = SandboxContext(
            root="data/sandbox",
            dir="data/sandbox/%s" % kind,
            background=background,
            background_kind=kind,
        )
        return with_sandbox_context({}, sandbox)

    def test_auto_confirm_non_critical(self) -> None:
        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="shell", arguments='{"command":"ls"}')
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "high"
        )
        self.assertTrue(decision.confirm)
        self.assertTrue(decision.handled)
        confirm, handled = decision
        self.assertTrue(confirm and handled)
        events = [item[0] for item in agent.audit_calls]
        self.assertIn(BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, events)

    def test_reject_critical_and_message(self) -> None:
        from atri_x.llm import LLMMessage

        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="shell", arguments='{"command":"rm -rf /"}')
        message = LLMMessage(role="tool", name="shell", tool_call_id="c1")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(), "s1", call, "critical", message
        )
        self.assertFalse(decision.confirm)
        self.assertTrue(decision.handled)
        self.assertEqual(message.segments[0].text, BACKGROUND_SHELL_CRITICAL_MESSAGE)
        events = [item[0] for item in agent.audit_calls]
        self.assertIn(BACKGROUND_SHELL_AUDIT_REJECTED, events)

    def test_not_handled_for_foreground(self) -> None:
        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="shell", arguments="{}")
        decision = cron_tools.confirm_background_sandbox_shell(
            agent, self._ctx(background=False), "s1", call, "high"
        )
        self.assertFalse(decision.handled)
        self.assertEqual(agent.audit_calls, [])

    def test_not_handled_for_unknown_kind(self) -> None:
        agent = FakeAgent()
        call = ToolCallRequest(id="c1", name="shell", arguments="{}")
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
        decision = cron_tools.background_shell_decision(sandbox, "shell", "critical")
        self.assertTrue(decision.handled)
        self.assertFalse(decision.confirm)


if __name__ == "__main__":
    unittest.main()
