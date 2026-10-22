"""``atri_x.agent.risk_confirmation`` / ``tool_auth`` / ``toolrun_adapter`` 的测试。

覆盖：确认期指令表与文案、参数渲染（含块标量与 ``json.Number`` 语义）、
群工具白名单展开、额度种类判定、适配层的确认分支与工具缓存重建。
"""

from __future__ import annotations

import json
import unittest

from atri_x.agent import risk_confirmation as rc
from atri_x.agent import tool_auth
from atri_x.agent import toolrun_adapter as adapter
from atri_x.agent.tools import compact_arguments, preview_arguments, short_hash
from atri_x.config import GroupPolicyConfig
from atri_x.llm import ToolCallRequest
from atri_x.security import Actor, GroupRoleAdmin, GroupRoleMember, RoleSuperadmin, RoleUser
from atri_x.session import Scope
from atri_x.storage import Session
from atri_x.toolrun.toolrun import ResolvedTool
from atri_x.toolrun import CachedTool


class FakePolicy:
    """只实现测试需要的方法的最小策略替身。"""

    def __init__(self) -> None:
        self.allowed = True

    def can_use_tool(self, actor, risk, owner_scoped=False):  # noqa: ANN001, ARG002
        return self.allowed


class FakeAgent:
    """鸭子类型的 Agent 替身（只带被测路径需要的属性）。"""

    def __init__(self, *, group: bool = True, policy: GroupPolicyConfig | None = None) -> None:
        self.platform = object()
        self.security_policy = FakePolicy()
        self._group = group
        self._policy = policy or GroupPolicyConfig()
        self.tool_aliases = {"safe": "readonly"}
        self.tool_profiles = {"readonly": ["read_file", "web_search"]}
        self.audit_calls: list[tuple] = []
        self.sent: list[str] = []

    # -- scope / actor ------------------------------------------------------
    def scope(self, ctx):  # noqa: ANN001, ARG002
        return Scope(platform="qq", platform_scope_id="group:42", actor_id="u1")

    def is_group_scope(self, ctx):  # noqa: ANN001, ARG002
        return self._group

    def group_policy_for_scope(self, scope):  # noqa: ANN001, ARG002
        return self._policy

    def actor(self, ctx):  # noqa: ANN001, ARG002
        return Actor(id="u1", group_role=GroupRoleMember)

    def audit(self, *args):
        self.audit_calls.append(args)

    async def send_chat(self, ctx, text):  # noqa: ANN001, ARG002
        self.sent.append(text)


class ConfirmationChannelTests(unittest.TestCase):
    """研究报告 §5.3：确认通道缺失时必须 fail-closed（旧实现返回 allowed=True）。"""

    def test_missing_confirmation_channel_denies(self) -> None:
        import asyncio

        from atri_x.tool.tool import RiskAssessment

        agent = FakeAgent()
        deps = adapter.AgentToolRunDeps(agent=agent, output=agent)
        call = ToolCallRequest(
            id="c1", name="shell", arguments=json.dumps({"cmd": "rm -rf /"})
        )
        result = asyncio.run(
            deps.confirm_tool_call(
                None, "s1", call, RiskAssessment(level="high"), "detail"
            )
        )
        self.assertFalse(result.allowed)
        self.assertIsNotNone(result.error)
        self.assertIn("tool_confirmation_unavailable", str(result.error))

    def test_auto_confirmed_session_still_allowed(self) -> None:
        import asyncio

        from atri_x.tool.tool import RiskAssessment

        class ConfirmableAgent(FakeAgent):
            def __init__(self) -> None:
                super().__init__()
                self.auto_confirm_session: dict[str, bool] = {}

            def is_session_auto_confirmed(self, session_id: str) -> bool:
                return bool(self.auto_confirm_session.get(session_id))

            def set_session_auto_confirmed(self, session_id: str) -> None:
                self.auto_confirm_session[session_id] = True

        agent = ConfirmableAgent()
        agent.set_session_auto_confirmed("s1")
        deps = adapter.AgentToolRunDeps(agent=agent, output=agent)
        call = ToolCallRequest(
            id="c1", name="shell", arguments=json.dumps({"cmd": "rm -rf /"})
        )
        result = asyncio.run(
            deps.confirm_tool_call(
                None, "s1", call, RiskAssessment(level="high"), "detail"
            )
        )
        self.assertTrue(result.allowed)


class RiskConfirmationCommandTests(unittest.TestCase):
    def test_command_table_matches_go(self) -> None:
        commands = rc.risk_confirmation_commands("/")
        self.assertEqual(
            [cmd.name for cmd in commands],
            ["detail", "confirm", "confirmtool", "confirmall", "reject", "stop"],
        )
        self.assertEqual(commands[0].aliases, ["details"])
        self.assertEqual(commands[1].aliases, ["c"])
        self.assertEqual(commands[2].usage, "/confirmtool")

    def test_names_include_aliases(self) -> None:
        names = rc.risk_confirmation_command_names()
        self.assertIn("ca", names)
        self.assertTrue(rc.is_risk_confirmation_command_name(" confirmall "))
        self.assertFalse(rc.is_risk_confirmation_command_name("help"))

    def test_prompt_text_with_and_without_timeout(self) -> None:
        self.assertEqual(
            rc.risk_confirmation_prompt_text("/", 0).startswith("可用指令：/detail 查看完整参数"),
            True,
        )
        with_timeout = rc.risk_confirmation_prompt_text("/", 90)
        self.assertIn("超过 1m30s 没有执行有效操作将自动停止", with_timeout)
        self.assertIn("/detail 可重新计时", with_timeout)

    def test_waiting_text(self) -> None:
        self.assertTrue(rc.risk_confirmation_waiting_text("!").startswith("正在等待高风险工具调用确认，只接受："))
        self.assertIn("!stop 停止当前请求", rc.risk_confirmation_waiting_text("!"))

    def test_duration_text(self) -> None:
        self.assertEqual(rc.confirmation_wait_duration_text(45), "45s")
        self.assertEqual(rc.confirmation_wait_duration_text(3661), "1h1m1s")
        self.assertEqual(rc.confirmation_wait_duration_text(0), "0s")

    def test_router_based_detection(self) -> None:
        class Parsed:
            ok = True
            name = "confirm"

        class Router:
            def parse(self, text):  # noqa: ANN001, ARG002
                return Parsed()

        self.assertTrue(rc.is_risk_confirmation_command("/confirm", Router()))
        self.assertFalse(rc.is_risk_confirmation_command("/confirm", None))

    def test_detail_text_falls_back_to_rendered_arguments(self) -> None:
        confirmation = type(
            "Confirmation",
            (),
            {"tool_name": "shell", "risk": "high", "arguments": '{"cmd":"ls"}', "detail": ""},
        )()
        self.assertEqual(
            rc.risk_confirmation_detail_text(confirmation),
            "高风险工具调用详情\n工具：shell\n风险：high\n参数：\ncmd: ls\n",
        )


class RiskArgumentRenderTests(unittest.TestCase):
    def test_empty_and_invalid(self) -> None:
        self.assertEqual(rc.format_risk_confirmation_arguments(""), "{}")
        self.assertEqual(rc.format_risk_confirmation_arguments("{not json"), "{not json")

    def test_trailing_garbage_rejected(self) -> None:
        # Go 用第二次 Decode 必须得到 io.EOF 来拒绝尾部垃圾。
        self.assertEqual(rc.format_risk_confirmation_arguments("{} {}"), "{} {}")

    def test_scalars_and_keys(self) -> None:
        text = rc.format_risk_confirmation_arguments(
            json.dumps({"b": 1, "a": True, "c": None, "d": 1.5, "e f": "x"}, ensure_ascii=False)
        )
        self.assertEqual(
            text.splitlines(),
            ['a: true', 'b: 1', 'c: null', 'd: 1.5', '"e f": x'],
        )

    def test_nested_and_block_string(self) -> None:
        text = rc.format_risk_confirmation_arguments(
            json.dumps({"outer": {"inner": [1, 2]}, "text": "l1\nl2"}, ensure_ascii=False)
        )
        self.assertEqual(
            text.splitlines(),
            ["outer:", "  inner:", "    - 1", "    - 2", "text: |", "  l1", "  l2"],
        )

    def test_number_is_not_reformatted(self) -> None:
        # json.Number 语义：整数保持原样，不变成 1e+21。
        text = rc.format_risk_confirmation_arguments('{"n": 1000000000000000000000}')
        self.assertEqual(text, "n: 1000000000000000000000")


class ToolAuthTests(unittest.TestCase):
    def test_allowlist_unrestricted_returns_none(self) -> None:
        agent = FakeAgent(policy=GroupPolicyConfig())
        self.assertIsNone(tool_auth.group_tool_allowlist(agent, None))

    def test_allowlist_none_denies_everything(self) -> None:
        policy = GroupPolicyConfig(tool_allowlist=[], tool_allowlist_set=True).normalize()
        agent = FakeAgent(policy=policy)
        allowlist = tool_auth.group_tool_allowlist(agent, None)
        self.assertEqual(allowlist, {})

    def test_allowlist_expands_profiles(self) -> None:
        policy = GroupPolicyConfig(
            tool_allowlist=["safe", "shell"], tool_allowlist_set=True
        ).normalize()
        agent = FakeAgent(policy=policy)
        allowlist = tool_auth.group_tool_allowlist(agent, None)
        self.assertEqual(
            sorted(allowlist.keys()), ["read_file", "shell", "web_search"]
        )

    def test_allowlist_star_inherits(self) -> None:
        policy = GroupPolicyConfig(tool_allowlist=["*"], tool_allowlist_set=True).normalize()
        agent = FakeAgent(policy=policy)
        self.assertIsNone(tool_auth.group_tool_allowlist(agent, None))

    def test_allowlist_uses_session_mode(self) -> None:
        # 本仓扩展：chat / work 各一份白名单，未配置的模式回退通用。
        policy = GroupPolicyConfig(
            tool_allowlist=["web_extract"],
            tool_allowlist_set=True,
            tool_allowlist_chat=["web_search"],
            tool_allowlist_chat_set=True,
            tool_allowlist_work=[],
            tool_allowlist_work_set=True,
        ).normalize()
        agent = FakeAgent(policy=policy)
        chat = Session(id="s1", mode="chat")
        work = Session(id="s2", mode="work")
        self.assertEqual(
            sorted(tool_auth.group_tool_allowlist(agent, None, chat).keys()),
            ["web_search"],
        )
        # work 显式空列表 = 禁止全部（空 dict 而不是 None）。
        self.assertEqual(tool_auth.group_tool_allowlist(agent, None, work), {})
        # 不带 session（纯展示等调用点）只按通用策略判断。
        self.assertEqual(
            sorted(tool_auth.group_tool_allowlist(agent, None).keys()),
            ["web_extract"],
        )

    def test_authorize_uses_session_mode(self) -> None:
        policy = GroupPolicyConfig(
            tool_allowlist_chat=["web_search"],
            tool_allowlist_chat_set=True,
            tool_allowlist_work=["read_file"],
            tool_allowlist_work_set=True,
        ).normalize()
        agent = FakeAgent(policy=policy)
        chat = Session(id="s1", mode="chat")
        read_call = ToolCallRequest(id="c1", name="read_file", arguments="{}")
        allowed, _reason = tool_auth.authorize_tool_call(
            agent, None, read_call, ResolvedTool(name="read_file"), chat
        )
        self.assertFalse(allowed)
        search_call = ToolCallRequest(id="c2", name="web_search", arguments="{}")
        allowed, _reason = tool_auth.authorize_tool_call(
            agent, None, search_call, ResolvedTool(name="web_search"), chat
        )
        self.assertTrue(allowed)

    def test_authorize_uses_cached_alias(self) -> None:
        policy = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        ).normalize()
        agent = FakeAgent(policy=policy)
        call = ToolCallRequest(id="c1", name="alias", arguments="{}")
        resolved = ResolvedTool(
            name="alias",
            cached=CachedTool(name="alias", canonical_name="read_file"),
        )
        allowed, reason = tool_auth.authorize_tool_call(agent, None, call, resolved)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_authorize_denies_unknown_tool(self) -> None:
        policy = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        ).normalize()
        agent = FakeAgent(policy=policy)
        call = ToolCallRequest(id="c1", name="shell", arguments="{}")
        resolved = ResolvedTool(name="shell")
        allowed, reason = tool_auth.authorize_tool_call(agent, None, call, resolved)
        self.assertFalse(allowed)
        self.assertEqual(reason, "tool is not in this group's allowlist")

    def test_non_group_scope_never_restricted(self) -> None:
        policy = GroupPolicyConfig(tool_allowlist=[], tool_allowlist_set=True).normalize()
        agent = FakeAgent(group=False, policy=policy)
        self.assertIsNone(tool_auth.group_tool_allowlist(agent, None))

    def test_quota_kind(self) -> None:
        call = ToolCallRequest(name="image_generate", arguments='{"dry_run": true}')
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "")
        call = ToolCallRequest(name="image_generate", arguments='{"dry_run": false}')
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "image")
        call = ToolCallRequest(name="image_generate", arguments="{bad")
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "image")
        call = ToolCallRequest(name="image_to_prompt", arguments="{}")
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "vision")
        call = ToolCallRequest(name="artist_extract", arguments='{"visual_fallback": true}')
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "vision")
        call = ToolCallRequest(name="artist_extract", arguments='{"visual_fallback": false}')
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "")
        call = ToolCallRequest(name="shell", arguments="{}")
        self.assertEqual(tool_auth.tool_execution_quota_kind(call, None), "")

    def test_is_history_tool_name(self) -> None:
        self.assertTrue(tool_auth.is_history_tool_name("Search_Chat_History"))
        self.assertFalse(tool_auth.is_history_tool_name("history"))
        self.assertTrue(
            tool_auth.tool_names_include(
                ToolCallRequest(name="search_chat_history"), None, "shell", "search_chat_history"
            )
        )

    def test_filter_group_feature_schemas(self) -> None:
        policy = GroupPolicyConfig(
            group_analysis=False, history=False, learning=False
        ).normalize()
        agent = FakeAgent(policy=policy)

        def schema(name: str) -> object:
            return type("S", (), {"function": type("F", (), {"name": name})()})()

        kept = tool_auth.filter_group_feature_schemas(
            agent,
            None,
            [
                schema("group_analysis"),
                schema("search_chat_history"),
                schema("self_learning_review"),
                schema("shell"),
            ],
        )
        self.assertEqual([item.function.name for item in kept], ["shell"])

    def test_before_execute_blocks_disabled_history(self) -> None:
        policy = GroupPolicyConfig(
            history=False, tool_allowlist=["*"], tool_allowlist_set=True
        ).normalize()
        agent = FakeAgent(policy=policy)
        with self.assertRaises(ValueError) as caught:
            tool_auth.before_execute_tool_call(
                agent, None, ToolCallRequest(name="search_chat_history"), ResolvedTool()
            )
        self.assertEqual(str(caught.exception), "本群已关闭历史记录")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self) -> FakeAgent:
        return FakeAgent()

    async def test_cached_tools_drops_discover_for_background(self) -> None:
        agent = self._agent()
        session = Session(
            id="s1",
            mode="work",
            metadata=json.dumps(
                {
                    "background_kind": "cron",
                    "tool_cache": [
                        {"name": "discover_tool", "source": "native"},
                        {"name": "shell", "source": "native"},
                    ],
                    "discovered_tools": ["discover_tool", "web_search"],
                }
            ),
        )
        names = [item.name for item in adapter.cached_tools_for_session(agent, session)]
        self.assertEqual(names, ["shell"])

    async def test_cached_tools_keeps_foreground_only_flag(self) -> None:
        class FakeInfo:
            description = "d"
            foreground_only = True

        class FakeTool:
            def info(self):  # noqa: ANN201
                return FakeInfo()

            def schema(self):  # noqa: ANN201
                return "schema"

        class FakeRegistry:
            def get(self, name):  # noqa: ANN001, ARG002
                return FakeTool(), True

        class FakeState:
            registry = FakeRegistry()

        agent = self._agent()
        agent._tool_runtime_state = FakeState()  # noqa: SLF001 - 测试夹具
        session = Session(id="s1", mode="work", metadata='{"discovered_tools":["x"]}')
        cached = adapter.cached_tools_for_session(agent, session)
        self.assertEqual(len(cached), 1)
        self.assertTrue(cached[0].foreground_only)

    async def test_should_send_preview(self) -> None:
        deps = adapter.AgentToolRunDeps(agent=self._agent(), output=None)
        self.assertTrue(await deps.should_send_preview(None, None, None, "  "))
        self.assertFalse(await deps.should_send_preview(None, None, None, "hi"))

    async def test_confirm_tool_call_auto_confirmed(self) -> None:
        agent = self._agent()
        agent.is_session_auto_confirmed = lambda session_id: True  # noqa: ARG005
        deps = adapter.AgentToolRunDeps(agent=agent, output=None)
        result = await deps.confirm_tool_call(
            None, "s1", ToolCallRequest(id="c1", name="shell"), _assessment(), ""
        )
        self.assertTrue(result.allowed)
        self.assertEqual(agent.sent, [])

    async def test_confirm_tool_call_reject(self) -> None:
        agent = self._agent()
        agent.is_session_auto_confirmed = lambda session_id: False  # noqa: ARG005
        agent.is_tool_auto_confirmed = lambda session_id, name: False  # noqa: ARG005
        agent.confirmation_wait_timeout = lambda ctx: 0  # noqa: ARG005
        agent.command_prefix = lambda: "/"

        class Response:
            expired = False
            stopped = False
            confirm_tool = False
            confirm_all = False
            rejected = True
            reason = ""
            extra = "note"

        class Turns:
            def await_risk_confirmation_context(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return Response()

        agent.turns = Turns()
        deps = adapter.AgentToolRunDeps(agent=agent, output=None)
        result = await deps.confirm_tool_call(
            None, "s1", ToolCallRequest(id="c1", name="shell"), _assessment(), "detail"
        )
        self.assertFalse(result.allowed)
        self.assertFalse(result.stopped)
        self.assertEqual(result.extra, "note")
        self.assertEqual(
            result.message.segments[0].text,
            "tool call shell rejected by user: user rejected",
        )
        self.assertTrue(any("高风险工具调用等待确认" in text for text in agent.sent))

    async def test_confirm_tool_call_expired_stops(self) -> None:
        agent = self._agent()
        agent.is_session_auto_confirmed = lambda session_id: False  # noqa: ARG005
        agent.is_tool_auto_confirmed = lambda session_id, name: False  # noqa: ARG005
        agent.confirmation_wait_timeout = lambda ctx: 30  # noqa: ARG005
        agent.command_prefix = lambda: "/"

        class Response:
            expired = True
            stopped = False
            confirm_tool = False
            confirm_all = False
            rejected = False
            extra = ""

        class Turns:
            def await_risk_confirmation_context(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return Response()

        agent.turns = Turns()
        deps = adapter.AgentToolRunDeps(agent=agent, output=None)
        result = await deps.confirm_tool_call(
            None, "s1", ToolCallRequest(id="c1", name="shell"), _assessment(), ""
        )
        self.assertFalse(result.allowed)
        self.assertTrue(result.stopped)
        self.assertIn("高风险工具确认已过期，当前处理已停止。", agent.sent)

    async def test_confirm_background_tool_handled(self) -> None:
        agent = self._agent()
        decision = type("D", (), {"confirm": True, "handled": True})()

        agent.confirm_background_sandbox_shell = (  # noqa: SLF001
            lambda ctx, session_id, call, level, message: decision  # noqa: ARG005
        )
        deps = adapter.AgentToolRunDeps(agent=agent, output=None)
        result, handled = await deps.confirm_background_tool(
            None, "s1", ToolCallRequest(id="c1", name="shell"), ResolvedTool(), _assessment()
        )
        self.assertTrue(handled)
        self.assertTrue(result.allowed)

    async def test_tool_result_and_call_messages(self) -> None:
        from atri_x.llm import LLMMessage

        deps = adapter.AgentToolRunDeps(agent=self._agent(), output=None)
        message = LLMMessage(role="tool", name="shell", tool_call_id="c1")
        from atri_x.llm import text_segments

        message.segments = text_segments("ok")
        stored = deps.tool_result_message("s1", message)
        self.assertEqual(stored.role, "tool")
        self.assertEqual(stored.tool_call_id, "c1")
        self.assertEqual(json.loads(stored.metadata), {"name": "shell"})
        call_message = deps.tool_call_message("s1", "text", "raw", [])
        self.assertEqual(call_message.role, "assistant")
        self.assertEqual(json.loads(call_message.metadata), {"raw_text": "raw"})


class ToolsHelperTests(unittest.TestCase):
    def test_short_hash_is_fnv32a(self) -> None:
        # FNV-1a 32 位："a" → 0xe40c292c
        self.assertEqual(short_hash("a"), "e40c292c")

    def test_compact_and_preview_arguments(self) -> None:
        self.assertEqual(compact_arguments(""), "{}")
        self.assertEqual(compact_arguments('{ "a" : 1 }'), '{"a":1}')
        self.assertEqual(compact_arguments("{bad"), "{bad")
        long = json.dumps({"a": "x" * 400})
        preview = preview_arguments(long)
        self.assertTrue(preview.endswith("..."))
        self.assertEqual(len(preview), 259)

    def test_group_role_superadmin_bypass(self) -> None:
        policy = GroupPolicyConfig(tool_allowlist=[], tool_allowlist_set=True).normalize()
        agent = FakeAgent(policy=policy)
        agent.actor = lambda ctx: Actor(id="u1", role=RoleSuperadmin, group_role=GroupRoleAdmin)  # noqa: ARG005
        # 白名单是**服务端**闸门，超级管理员也不能绕过：空名单就是禁止全部群工具。
        self.assertEqual(tool_auth.group_tool_allowlist(agent, None), {})
        self.assertFalse(
            tool_auth.authorize_tool_call(
                agent, None, ToolCallRequest(name="shell"), ResolvedTool(name="shell")
            )[0]
        )

    def test_normalize_keeps_nil_allowlist_and_is_idempotent(self) -> None:
        """``Normalize`` 必须保留 nil / 空切片二态，并且幂等。

        真机事故（0.9.11）：旧实现把 ``None`` 归一成 ``[]``，于是第二次
        ``tool_allowlist_restricted()`` 把"这个群没配过工具策略"读成"显式禁止
        全部工具"，任何没配置群策略的群所有工具调用都被拒
        （``tool is not in this group's allowlist``）。nil 表示继承全局，空列表
        才表示禁止全部。
        """
        raw = GroupPolicyConfig(history=False)
        self.assertFalse(raw.tool_allowlist_restricted())
        once = raw.normalize()
        self.assertIsNone(once.tool_allowlist)
        self.assertFalse(once.tool_allowlist_restricted())
        self.assertFalse(once.normalize().tool_allowlist_restricted())

        explicit = GroupPolicyConfig(tool_allowlist=[]).normalize()
        self.assertEqual(explicit.tool_allowlist, [])
        self.assertTrue(explicit.tool_allowlist_restricted())
        self.assertTrue(explicit.normalize().tool_allowlist_restricted())


def _assessment() -> object:
    from atri_x.tool import RiskAssessment

    return RiskAssessment(level="high", reasons=["sensitive"])


if __name__ == "__main__":
    unittest.main()
