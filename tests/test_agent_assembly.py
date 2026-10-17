"""装配**之后**的真实 :class:`~atri_x.agent.core.Agent` 的行为回归。

对应 Go：``internal/agent/core.go`` 的 ``NewWithOptions`` 装配段。

第二轮对抗性验证发现的两个 P0 之所以能躲过 4800 条用例，是因为相关测试全程用
``_DirectiveHost`` 之类的**假宿主**直接调模块级函数（见
``verification/round2-agent.md`` 的 P0-1 / P0-2）。本文件因此只做一件事：
**一律经过真实 ``Agent``**（真实构造、真实方法解析、真实 ``Deps`` 接线），
把"装配后能不能聊天"钉死。

覆盖：

* P0-1 ``@char:`` / ``@turn:`` 的方法必须真的在 ``Agent`` 上（并且能一路走到模型）；
* P0-2 ``deps.group_policy is agent`` 时群策略接缝不得自递归；
* P0-4 ``Options.tool_registry`` 必须在构造期接进 ``tool_runtime``；
* P1-3 ``Options.naming_notifier`` 必须真的交给 ``SessionService``；
* P1-2 ``set_runtime_status`` 的 sync / async 两种实现都要被正确消费；
* P2-6 ``Options.context_overflow`` 必须在构造期 seed。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest

from atri_x.agent.bootstrap import install_agent_services
from atri_x.agent.character_directive import CHARACTER_DIRECTIVE_METHODS
from atri_x.agent.commands.deps import Deps
from atri_x.agent.core import Agent
from atri_x.agent.turn_override import TURN_OVERRIDE_METHODS
from atri_x.character import NewStore as new_character_store
from atri_x.character import Viewer, VisibilityPublic, WriteRequest
from atri_x.config import ContextOverflowConfig, GroupPolicyConfig, ModelSelection
from atri_x.delivery import Receipt
from atri_x.llm import MessageSegment, ToolFunctionSchema, ToolSchema
from atri_x.runtime import PhaseLLM, Snapshot
from atri_x.security import Actor, RoleSuperadmin, with_actor
from atri_x.session import NamingCompletedEvent, NamingScheduledEvent
from atri_x.storage import Session, SessionModeWork
from atri_x.tool.tool import Registry
from atri_x.toolrun import Manager

from tests.test_agent_core import _Store as _CoreStore
from tests.test_agent_core import make_agent, make_ctx

#: 临时角色库根目录（仓库根，``.gitignore`` 已覆盖 ``tmp_*``）。
TMP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_agent_assembly_test"
)


def superadmin_ctx(**overrides):
    """``make_ctx`` 的默认 Actor 是普通用户；单回合声明要看超管。"""
    return with_actor(make_ctx(**overrides), Actor(id="root", role=RoleSuperadmin))


class _Summaries:
    """摘要仓储替身：没有任何摘要（走"无摘要"分支）。"""

    def latest_by_session(self, session_id):
        from atri_x.errors import NotFoundError

        raise NotFoundError(session_id)

    def latest_by_session_up_to(self, session_id, message_id):
        from atri_x.errors import NotFoundError

        raise NotFoundError(session_id)


class _Store(_CoreStore):
    """``make_agent`` 的默认存储替身 + 装配后回合真正会读的摘要仓储。

    跑过 ``install_agent_services`` 之后上下文加载器是真的（``_configure_context``
    会把它接上），所以存储必须提供 ``context_summaries()``。
    """

    def __init__(self) -> None:
        super().__init__()
        self._summaries = _Summaries()

    def context_summaries(self) -> _Summaries:
        return self._summaries


class _Tool:
    """最小真实工具（``Registry.register`` 只要求 ``name()`` / ``info()`` / ``schema()``）。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name

    def info(self):
        from atri_x.tool import Info

        return Info(name=self._name, description="d")

    def schema(self) -> ToolSchema:
        return ToolSchema(function=ToolFunctionSchema(name=self._name))


class _Client:
    """脚本化 LLM 客户端：每次调用回一段固定文本。"""

    def __init__(self, text: str = "收到") -> None:
        self.requests: list[object] = []
        self._text = text

    async def chat_stream(self, ctx, req):
        self.requests.append(req)

        async def _gen():
            from atri_x.llm import StreamChunk

            yield StreamChunk(delta_content=self._text)

        return _gen()


class _ContextRuntime:
    """最小上下文加载服务（``tools_for_session`` 与 prompt 都只要 ``load``）。"""

    async def load(self, ctx, session_id):
        from atri_x.contextmgr import LoadedContext

        return LoadedContext(messages=[], summary=None)


class _OutSpy:
    """替换发送边界：只记录，不碰真实平台。"""

    def __init__(self, agent: Agent) -> None:
        self.texts: list[str] = []
        agent.send_chat = self.send_chat
        agent.send_chat_with_receipt = self.send_chat_with_receipt

    async def send_chat(self, ctx, text: str) -> None:
        self.texts.append(text)

    async def send_chat_with_receipt(self, ctx, text: str) -> Receipt:
        self.texts.append(text)
        return Receipt()


def _assembled_agent(case: unittest.TestCase, **overrides) -> tuple[Agent, Deps]:
    """建一个真实 Agent，并用**真实 Deps** 跑一遍 ``install_agent_services``。

    这正是生产里 ``atri_x/app/runtime_agent.py`` 的装配形状（含 ``Deps.GroupPolicy``
    按 Go ``core.go:454`` 接成 ``Agent`` 自己）。把 ``deps`` 同时挂到
    ``agent.command_deps``，``AgentSeams._svc`` 才会像生产那样回退到它 ——
    自递归风险正是从这条回退上来的。
    """
    agent = make_agent(**overrides)
    agent.sessions.title_gen = None  # 不排期标题生成，保持用例确定性
    agent.services["context_runtime"] = _ContextRuntime()
    deps = Deps(
        router=agent.commands,
        sessions=agent.sessions,
        turns=agent.turns,
        scope=agent.scope,
    )
    agent.command_deps = deps
    case.addCleanup(install_agent_services, agent, deps=deps)
    install_agent_services(agent, deps=deps)
    return agent, deps


def _character_store(case: unittest.TestCase, root_name: str):
    """真实角色库 + 一个公开角色（断言走真实可见性判定）。"""
    root = os.path.join(TMP_ROOT, root_name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    case.addCleanup(shutil.rmtree, root, ignore_errors=True)
    store = new_character_store(root)
    store.write(
        None,
        WriteRequest(id="assistant", name="公共助手", visibility=VisibilityPublic),
        Viewer(platform="cli", actor_id="root", superadmin=True),
    )
    return store


def _request_text(req) -> str:
    return "\n".join(
        segment.text or ""
        for message in req.messages
        for segment in message.segments
    )


class CharacterAndTurnDirectiveTest(unittest.TestCase):
    """P0-1：``@char:`` / ``@turn:`` 必须在真实 ``Agent`` 上有实现。"""

    def test_methods_exist_on_the_agent_class(self) -> None:
        for name in CHARACTER_DIRECTIVE_METHODS + TURN_OVERRIDE_METHODS:
            self.assertTrue(hasattr(Agent, name), name)

    def test_character_directive_reaches_the_model_via_handle_session_input(self) -> None:
        """走 ``Agent.handle_session_input``（普通聊天消息的主路径）。

        正文来自 ctx 的入站段（与 Go ``input.go:143`` 一致），所以指令要放进段里。
        """
        client = _Client()
        store = _character_store(self, "char_flow")
        agent, _deps = _assembled_agent(self, character_store=store, store=_Store())
        agent.clients["default"] = client
        sent = _OutSpy(agent)
        ctx = make_ctx(segments=[MessageSegment(type="text", text="@c:assistant 你好")])
        session = agent.sessions.get_or_create_current(agent.scope(ctx), "@c:assistant 你好")

        asyncio.run(Agent.handle_session_input(agent, ctx, session, "@c:assistant 你好"))

        self.assertTrue(client.requests, "回合没有走到模型")
        prompt = _request_text(client.requests[0])
        self.assertIn("公共助手", prompt)
        self.assertNotIn("@c:assistant", prompt)
        self.assertTrue(
            any("已启用角色" in text for text in sent.texts),
            sent.texts,
        )

    def test_turn_override_runs_on_the_real_agent(self) -> None:
        agent, _deps = _assembled_agent(self)
        agent.model_profiles["m1"] = ModelSelection(provider="default", model="m1")
        ctx = superadmin_ctx()

        new_ctx, result = agent.apply_turn_overrides(ctx, "@m:m1 你好")

        self.assertEqual(result.applied, ["model:m1"])
        self.assertEqual(result.text, "你好")
        from atri_x.agent.turn_override import turn_model_override

        selection, ok = turn_model_override(new_ctx)
        self.assertTrue(ok)
        self.assertEqual(selection.model, "m1")

    def test_turn_override_denied_for_normal_actor(self) -> None:
        agent, _deps = _assembled_agent(self)
        _ctx, result = agent.apply_turn_overrides(make_ctx(), "@m:m1 你好")
        self.assertEqual(result.denied, ["model:m1"])
        self.assertEqual(result.text, "你好")


class GroupPolicySeamTest(unittest.TestCase):
    """P0-2：``Deps.GroupPolicy`` 接 ``Agent`` 自己时不得自递归。"""

    def test_installed_deps_point_at_the_agent_without_recursing(self) -> None:
        agent, deps = _assembled_agent(self)
        self.assertIs(deps.group_policy, agent)
        agent.group_policy["cli:group:42"] = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        )
        ctx = make_ctx(scope_id="group:42")
        # 修复前这三条都会 ``RecursionError``：分派拿到的"服务"就是宿主自己。
        self.assertEqual(agent.group_tool_allowlist(ctx), {"read_file": True})
        self.assertEqual(agent.filter_group_feature_schemas(ctx, []), [])
        self.assertEqual(agent.group_default_model_selection(ctx), (None, False))
        self.assertEqual(agent.group_merge_window(ctx), 0.0)
        self.assertTrue(agent.history_enabled(ctx))
        # 根治点：宿主自己不算"已装配的服务"。
        self.assertIsNone(agent._svc("group_policy"))

    def test_tools_for_session_survives_the_assembled_deps(self) -> None:
        """真实回合的取 schema 入口（``chat_turn`` 每回合都会走）。"""
        from atri_x.agent.tool_schemas import tools_for_session

        registry = Registry()
        registry.register(_Tool("discover_tool"))
        agent, _deps = _assembled_agent(self, tool_registry=registry)
        session = Session(id="s1", mode=SessionModeWork)

        schemas = asyncio.run(tools_for_session(agent, make_ctx(), session))

        self.assertEqual([schema.function.name for schema in schemas], ["discover_tool"])


class ToolRegistryWiringTest(unittest.TestCase):
    """P0-4：``Options.tool_registry`` 必须在**构造期**接进工具运行时。"""

    def test_registry_from_options_is_wired(self) -> None:
        registry = Registry()
        registry.register(_Tool("discover_tool"))
        agent = make_agent(tool_registry=registry)

        self.assertIs(agent.tool_runtime.registry, registry)
        self.assertIsInstance(agent.tool_runtime.manager, Manager)
        self.assertIsNotNone(agent.tool_run_manager())
        # 对应 Go ``SetToolRuntime``：注册表在就换成 toolRunPromptProvider。
        self.assertTrue(agent.tool_runtime.default_provider)
        self.assertTrue(hasattr(agent.tool_runtime.provider, "tool_names"))

    def test_tool_config_and_tag_config_follow_go(self) -> None:
        from atri_x.agent.tools import tool_runtime_state
        from atri_x.config import ToolsConfig

        agent = make_agent(tools_config=ToolsConfig(max_rounds_per_turn=5))
        self.assertEqual(agent.tool_runtime.config.max_rounds_per_turn, 5)
        self.assertEqual(agent.max_tool_rounds_per_turn(), 5)
        self.assertIs(agent.tools_config, agent.tool_runtime.config)
        # ``SetToolTagConfig`` 无条件执行（Go ``core.go:434``）。
        self.assertIsNotNone(tool_runtime_state(agent).tool_tags)

    def test_tool_provider_is_used_when_no_registry_or_skills(self) -> None:
        class _Provider:
            async def schemas(self, ctx, mode, session, scope):
                return [ToolSchema(function=ToolFunctionSchema(name="only"))]

        provider = _Provider()
        agent = make_agent(tool_provider=provider)
        self.assertIs(agent.tool_runtime.provider, provider)
        self.assertFalse(agent.tool_runtime.default_provider)

    def test_skills_alone_also_select_the_runtime_branch(self) -> None:
        """Go 的分支条件是 ``ToolRegistry != nil || Skills != nil``。"""
        agent = make_agent(skills=object())
        self.assertIsNone(agent.tool_runtime.registry)
        self.assertIsInstance(agent.tool_runtime.manager, Manager)


class NamingNotifierWiringTest(unittest.TestCase):
    """P1-3：``Options.naming_notifier`` 必须交给 ``SessionService``。"""

    def test_notifier_from_options_reaches_the_session_service(self) -> None:
        events: list[object] = []

        class _Notifier:
            def notify_naming_scheduled(self, event):  # noqa: D401 - 协议实现
                events.append(event)

            def notify_naming_completed(self, event):
                events.append(event)

            def notify_naming_failed(self, event):
                events.append(event)

        notifier = _Notifier()
        agent = make_agent(naming_notifier=notifier)
        self.assertIs(agent.sessions.notifier, notifier)

        agent.sessions.notify_naming_scheduled(NamingScheduledEvent(session_id="s1"))
        agent.sessions.notify_naming_completed(NamingCompletedEvent(session_id="s1", title="t"))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].title, "t")

    def test_naming_model_is_wired_like_go(self) -> None:
        """Go ``core.go:298`` 的 ``naming`` / ``namingModel`` / ``namingSource``。"""
        agent = make_agent(
            naming_selection=ModelSelection(provider="default", model="naming-model")
        )
        self.assertIs(agent.title_gen.naming, agent.clients["default"])
        self.assertEqual(agent.title_gen.naming_model, "naming-model")
        self.assertEqual(agent.title_gen.naming_source, "default")

    def test_without_naming_selection_the_primary_model_is_used(self) -> None:
        agent = make_agent()
        self.assertIsNone(agent.title_gen.naming)
        self.assertEqual(agent.title_gen.naming_model, "")
        self.assertIs(agent.title_gen.primary, agent.clients["default"])


class RuntimeStatusDispatchTest(unittest.TestCase):
    """P1-2：``set_runtime_status`` 的 sync / async 实现都要真的生效。"""

    def _publish(self, agent: Agent, sender) -> list[tuple]:
        agent.platform = sender
        status_module_publish(agent, make_ctx(), Snapshot(session_id="s1", phase=PhaseLLM))
        return list(getattr(sender, "calls", []))

    def test_sync_sender_is_called(self) -> None:
        agent = make_agent()

        class _Sync:
            def __init__(self):
                self.calls = []

            def set_runtime_status(self, ctx, snapshot):
                self.calls.append(snapshot.session_id)

        sender = _Sync()
        self._publish(agent, sender)
        self.assertEqual(sender.calls, ["s1"])

    def test_async_sender_is_consumed(self) -> None:
        agent = make_agent()

        class _Async:
            def __init__(self):
                self.calls = []

            async def set_runtime_status(self, ctx, snapshot):
                self.calls.append(snapshot.session_id)

        sender = _Async()

        async def _run():
            self._publish(agent, sender)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_run())
        self.assertEqual(sender.calls, ["s1"])

    def test_async_sender_failure_is_reported_not_swallowed(self) -> None:
        agent = make_agent()
        warnings: list[tuple] = []
        agent.audit_warn = lambda *args: warnings.append(args)

        class _Boom:
            async def set_runtime_status(self, ctx, snapshot):
                raise RuntimeError("push failed")

        async def _run():
            self._publish(agent, _Boom())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_run())
        self.assertTrue(warnings, "异步推送失败必须留痕，不能只丢在 task 里")
        self.assertEqual(warnings[0][0], "runtime_status_publish_failed")
        self.assertIn("push failed", warnings[0][-1])

    def test_async_sender_without_loop_is_reported(self) -> None:
        agent = make_agent()
        warnings: list[tuple] = []
        agent.audit_warn = lambda *args: warnings.append(args)

        class _Async:
            async def set_runtime_status(self, ctx, snapshot):  # pragma: no cover - 不该被 await
                raise AssertionError("must not run without a loop")

        self._publish(agent, _Async())
        self.assertTrue(warnings, "没有事件循环时也必须留痕，而不是静默丢协程")


def status_module_publish(agent: Agent, ctx, snapshot) -> None:
    from atri_x.agent import status as status_module

    status_module.publish_runtime_status(agent, ctx, snapshot)


class ContextOverflowSeedTest(unittest.TestCase):
    """P2-6：``Options.context_overflow`` 在构造期就要进溢出覆盖表。"""

    def test_context_overflow_seeded_at_construction(self) -> None:
        overflow = {"cli:local": ContextOverflowConfig(chat="truncate", work="reject")}
        agent = make_agent(context_overflow=overflow)
        snapshot = agent.context_overflow_snapshot()
        self.assertIn("cli:local", snapshot)
        self.assertEqual(snapshot["cli:local"].work, "reject")
        # 覆盖表是副本，改配置不影响宿主（Go ``cloneContextOverflow``）。
        overflow["cli:local"].work = "summarize"
        self.assertEqual(agent.context_overflow_snapshot()["cli:local"].work, "reject")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
