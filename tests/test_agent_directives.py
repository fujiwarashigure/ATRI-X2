"""``@tool:`` / ``@skill:`` 指令、入站限流、会话工作区与工具标签的测试。

对应 Go：``internal/agent/tool_directive_test.go`` / ``ratelimit_test.go`` /
``workspace_test.go`` / ``tool_tag_config_test.go`` 的核心路径。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.agent.ratelimit import (
    RateLimitService,
    RateLimitState,
    RateLimitStatus,
    allow_inbound_detailed,
    ensure_rate_limit_state,
    install_ratelimit_services,
    is_group_context,
    rate_limit_retry_text,
)
from atri_x.agent.tool_directive import (
    apply_tool_directives,
    install_tool_directive_services,
    notify_tool_directive_result,
    preload_root_reason,
    preload_skip_result,
)
from atri_x.agent.tool_directive_skill import (
    TOOL_REASON_NOT_SKILL_LIKE,
    TOOL_REASON_SKILL_HAS_NO_SCHEMA,
    apply_skill_directives,
    notify_skill_directive_result,
    preload_skill_root_reason,
    preload_skill_wrapper_reason,
)
from atri_x.agent.tool_tag_config import (
    ToolTagConfigSource,
    completion_tool_names_by_tag,
    names_by_tool_tag,
    normalize_tool_tags_config,
)
from atri_x.agent.workspace import SessionWorkspaceStore, new_session_workspace_store
from atri_x.config import ToolTagConfig, ToolTagsConfig
from atri_x.logging import ResultRejected, ResultSkipped
from atri_x.ops.ratelimit import Limit, Limiter
from atri_x.security import Actor, RoleSuperadmin, default_policy
from atri_x.tool import Info, ToolReasonHidden, ToolReasonNotFound, ToolReasonSuperadminOnly
from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.storage.models import Session


class _Tool:
    """普通工具（**不**实现 detail/activate_tools，因此不是详情提供者）。"""

    def __init__(
        self,
        name: str,
        *,
        hidden: bool = False,
        superadmin_only: bool = False,
    ) -> None:
        self._name = name
        self._hidden = hidden
        self._superadmin_only = superadmin_only

    def info(self) -> Info:
        return Info(
            name=self._name,
            description="d",
            hidden=self._hidden,
            superadmin_only=self._superadmin_only,
        )

    def schema(self) -> ToolSchema:
        return ToolSchema(function=ToolFunctionSchema(name=self._name))


class _DetailTool(_Tool):
    """详情提供者（Skill 本体）。对应 Go ``tool.DetailProvider``。"""

    def __init__(self, name: str, *, activates: list[str] | None = None, **kwargs) -> None:
        super().__init__(name, **kwargs)
        self._activates = list(activates or [])

    def detail(self) -> str:
        return "detail"

    def activate_tools(self) -> list[str]:
        return list(self._activates)


class _Registry:
    def __init__(self, tools: dict[str, _Tool], tags: dict[str, list[str]] | None = None) -> None:
        self.tools = tools
        self.tag_map = dict(tags or {})

    def get(self, name: str) -> _Tool | None:
        return self.tools.get(name)

    def names_by_tag(self, tag: str, allowed=None) -> list[str]:
        names = [names for names in self.tag_map.get(tag, []) if names in self.tools]
        if allowed is None:
            return names
        return [name for name in names if allowed(self.tools[name])]

    def tags(self) -> list[str]:
        return sorted(self.tag_map)


class _ToolRuntime:
    def __init__(self, registry: _Registry | None, source: ToolTagConfigSource | None = None) -> None:
        self.registry = registry
        self.tool_tags = source


class _Actor:
    def __init__(self, actor_id: str = "u1") -> None:
        self.id = actor_id
        self.platform_user_id = ""

    def role(self) -> str:  # pragma: no cover - 兼容按方法访问
        return "user"


class _Scope:
    platform = "qq"
    platform_scope_id = "group:1"
    actor_id = "u1"


class _DirectiveHost:
    def __init__(self, registry: _Registry | None, source: ToolTagConfigSource | None = None) -> None:
        self.tool_runtime = _ToolRuntime(registry, source)
        self.security_policy = default_policy()
        self.discovered_tools: dict[str, dict] = {}
        self.auto_confirm_mu = __import__("threading").Lock()
        self.services: dict[str, object] = {}
        self.sent: list[str] = []
        self.audit_events: list[tuple] = []

    def scope(self, ctx) -> _Scope:
        return _Scope()

    def actor(self, ctx):
        from atri_x.security import Actor as _A

        return _A(id="u1", platform="qq", platform_user_id="u1")

    def command_prefix(self) -> str:
        return "/"

    async def send_chat(self, ctx, text: str) -> None:
        self.sent.append(text)

    def audit(self, event: str, **attrs) -> None:
        self.audit_events.append((event, attrs))


class TestToolDirectiveReasons(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = default_policy()
        self.actor = Actor(id="u1", platform="qq", platform_user_id="u1")

    def test_preload_root_reason(self) -> None:
        self.assertEqual(preload_root_reason(self.actor, self.policy, _Tool("t")), "")
        self.assertEqual(
            preload_root_reason(self.actor, self.policy, _Tool("discover_tool")),
            ToolReasonHidden,
        )
        self.assertEqual(
            preload_root_reason(self.actor, self.policy, _Tool("h", hidden=True)),
            ToolReasonHidden,
        )
        self.assertEqual(
            preload_root_reason(self.actor, self.policy, _DetailTool("s")),
            ToolReasonHidden,
        )
        self.assertEqual(
            preload_root_reason(
                self.actor, self.policy, _Tool("admin", superadmin_only=True)
            ),
            ToolReasonSuperadminOnly,
        )

    def test_preload_skill_reasons(self) -> None:
        self.assertEqual(
            preload_skill_root_reason(self.actor, self.policy, _Tool("plain")),
            TOOL_REASON_NOT_SKILL_LIKE,
        )
        self.assertEqual(
            preload_skill_root_reason(self.actor, self.policy, _DetailTool("skill")),
            "",
        )
        self.assertEqual(
            preload_skill_root_reason(
                self.actor, self.policy, _DetailTool("skill", hidden=True)
            ),
            ToolReasonHidden,
        )
        # 包装工具：隐藏是常态，不因 hidden 拒绝；详情提供者不能当包装工具
        self.assertEqual(
            preload_skill_wrapper_reason(self.actor, self.policy, _Tool("w", hidden=True)), ""
        )
        self.assertEqual(
            preload_skill_wrapper_reason(self.actor, self.policy, _DetailTool("s")),
            TOOL_REASON_SKILL_HAS_NO_SCHEMA,
        )

    def test_preload_skip_result(self) -> None:
        from atri_x.tool import TOOL_REASON_RISK_DENIED, TOOL_REASON_SUPERADMIN_ONLY

        self.assertEqual(preload_skip_result(TOOL_REASON_RISK_DENIED), ResultRejected)
        self.assertEqual(preload_skip_result(TOOL_REASON_SUPERADMIN_ONLY), ResultRejected)
        self.assertEqual(preload_skip_result("hidden"), ResultSkipped)
        self.assertEqual(preload_skip_result(ToolReasonNotFound), ResultSkipped)


class TestApplyToolDirectives(unittest.TestCase):
    def test_chat_mode_preloads_tools(self) -> None:
        # 本仓有意偏离上游：chat 模式也允许 @tool: 预载。
        registry = _Registry({"t": _Tool("t")})
        host = _DirectiveHost(registry)
        result = apply_tool_directives(host, None, Session(id="s", mode="chat"), "@tool:t hello")
        self.assertFalse(result.mode_blocked)
        self.assertEqual(result.injected, ["t"])
        self.assertEqual(result.invalid, [])
        self.assertEqual(result.text, "hello")

    def test_unknown_mode_blocks_tools(self) -> None:
        registry = _Registry({"t": _Tool("t")})
        host = _DirectiveHost(registry)
        result = apply_tool_directives(
            host, None, Session(id="s", mode="background"), "@tool:t hello"
        )
        self.assertTrue(result.mode_blocked)
        self.assertEqual(result.invalid, ["t"])
        self.assertEqual(result.text, "hello")

    def test_unknown_tool_is_invalid(self) -> None:
        registry = _Registry({"t": _Tool("t")})
        host = _DirectiveHost(registry)
        result = apply_tool_directives(host, None, Session(id="s", mode="work"), "@tool:nope hi")
        self.assertEqual(result.invalid, ["nope"])
        self.assertEqual(result.injected, [])
        self.assertEqual(result.text, "@tool:nope hi")

    def test_injected_tool_strips_directive(self) -> None:
        registry = _Registry({"t": _Tool("t")})
        host = _DirectiveHost(registry)
        result = apply_tool_directives(host, None, Session(id="s", mode="work"), "@tool:t hi")
        self.assertEqual(result.injected, ["t"])
        self.assertEqual(result.text, "hi")

    def test_existing_tool_is_reported(self) -> None:
        registry = _Registry({"t": _Tool("t")})
        host = _DirectiveHost(registry)
        host.discovered_tools["s"] = {"t": object()}
        result = apply_tool_directives(host, None, Session(id="s", mode="work"), "@tool:t hi")
        self.assertEqual(result.existing, ["t"])
        self.assertEqual(result.injected, [])

    def test_tag_expands_to_tools(self) -> None:
        registry = _Registry({"a": _Tool("a"), "b": _Tool("b")}, {"media": ["a", "b"]})
        host = _DirectiveHost(registry)
        result = apply_tool_directives(host, None, Session(id="s", mode="work"), "@tool:media hi")
        self.assertEqual(result.injected, ["a", "b"])
        self.assertEqual(result.text, "hi")

    def test_text_without_directive_is_untouched(self) -> None:
        host = _DirectiveHost(_Registry({"t": _Tool("t")}))
        result = apply_tool_directives(host, None, Session(id="s", mode="work"), "plain")
        self.assertEqual(result.text, "plain")
        self.assertEqual(result.injected, [])

    def test_no_registry_returns_text(self) -> None:
        host = _DirectiveHost(None)
        result = apply_tool_directives(host, None, Session(id="s", mode="work"), "@tool:t hi")
        self.assertEqual(result.text, "@tool:t hi")

    def test_skill_directives_inject_wrappers(self) -> None:
        registry = _Registry(
            {"sk": _DetailTool("sk", activates=["w1"]), "w1": _Tool("w1")}
        )
        host = _DirectiveHost(registry)
        result = apply_skill_directives(host, None, Session(id="s", mode="work"), "@skill:sk hi")
        self.assertEqual(result.skills, ["sk"])
        self.assertEqual(result.injected_wrappers, ["w1"])
        self.assertEqual(result.text, "hi")

    def test_skill_directives_in_chat_mode(self) -> None:
        # 本仓有意偏离上游：chat 模式也允许 @skill: 预载。
        registry = _Registry({"sk": _DetailTool("sk")})
        host = _DirectiveHost(registry)
        result = apply_skill_directives(host, None, Session(id="s", mode="chat"), "@skill:sk hi")
        self.assertFalse(result.mode_blocked)
        self.assertEqual(result.skills, ["sk"])
        self.assertEqual(result.text, "hi")

    def test_skill_directives_reject_non_skill(self) -> None:
        registry = _Registry({"plain": _Tool("plain")})
        host = _DirectiveHost(registry)
        result = apply_skill_directives(host, None, Session(id="s", mode="work"), "@skill:plain hi")
        self.assertEqual(result.invalid, ["plain"])
        self.assertEqual(result.skills, [])


class TestDirectiveNotifications(unittest.IsolatedAsyncioTestCase):
    async def test_tool_notification_lines(self) -> None:
        host = _DirectiveHost(_Registry({"t": _Tool("t")}))
        # work / chat 都允许预载，用未知模式覆盖"模式阻止"兜底分支。
        result = apply_tool_directives(host, None, Session(id="s", mode="background"), "@tool:t hi")
        await notify_tool_directive_result(host, None, result)
        self.assertEqual(len(host.sent), 1)
        self.assertIn("当前会话模式不支持工具预载", host.sent[0])
        self.assertNotIn("未找到", host.sent[0])

        host.sent.clear()
        unknown = apply_tool_directives(host, None, Session(id="s", mode="work"), "@tool:nope hi")
        await notify_tool_directive_result(host, None, unknown)
        self.assertIn("未找到或不可用的工具：nope", host.sent[0])

        class _Result:
            injected = ["b", "a"]
            existing = ["c"]
            invalid = []
            mode_blocked = False

        host.sent.clear()
        await notify_tool_directive_result(host, None, _Result())
        self.assertEqual(host.sent[0], "已注入工具：a, b\n已存在工具：c")

    async def test_skill_notification_lines(self) -> None:
        class _Result:
            skills = ["sk"]
            injected_wrappers = ["w"]
            existing_wrappers = []
            invalid = ["bad"]
            mode_blocked = False

        host = _DirectiveHost(None)
        await notify_skill_directive_result(host, None, _Result())
        self.assertEqual(host.sent[0], "已注入 Skill：sk\n已注入 Skill 工具：w\n未找到或不可用的 Skill：bad")

        empty = type("E", (), {"skills": [], "injected_wrappers": [], "existing_wrappers": [], "invalid": [], "mode_blocked": False})()
        host.sent.clear()
        await notify_skill_directive_result(host, None, empty)
        self.assertEqual(host.sent, [])

    def test_install_services(self) -> None:
        host = _DirectiveHost(_Registry({"t": _Tool("t")}))
        tool_service, skill_service = install_tool_directive_services(host)
        self.assertIs(host.services["tool_directive"], tool_service)
        self.assertIs(host.services["skill_directive"], skill_service)
        self.assertTrue(callable(host.apply_tool_directives))
        result = host.apply_tool_directives(None, Session(id="s", mode="work"), "@tool:t hi")
        self.assertEqual(result.text, "hi")


class TestRateLimit(unittest.TestCase):
    def _host(
        self,
        user: Limiter | None = None,
        group: Limiter | None = None,
        *,
        group_context: bool = True,
        role: str = "user",
    ):
        class _RateHost:
            pass

        host = _RateHost()
        host.rate_limit_user = user
        host.rate_limit_group = group
        host.rate_limit_state = RateLimitState()
        host.rate_limit_user_per_minute = 10
        host.rate_limit_user_burst = 5
        host.rate_limit_group_per_minute = 20
        host.rate_limit_group_burst = 7
        host._role = role
        host._group = group_context

        host.scope = lambda ctx: _Scope()
        host.actor = lambda ctx: Actor(
            id="u1", platform="qq", platform_user_id="u1", role=role
        )
        return host

    def test_no_limiters_allows(self) -> None:
        host = self._host()
        self.assertEqual(allow_inbound_detailed(host, None), (True, 0.0, ""))

    def test_superadmin_bypasses(self) -> None:
        limiter = Limiter(Limit(rate_per_second=0.0001, burst=1))
        host = self._host(user=limiter, role=RoleSuperadmin)
        self.assertEqual(allow_inbound_detailed(host, None), (True, 0.0, ""))
        self.assertEqual(limiter.keys(), 0)

    def test_user_then_group(self) -> None:
        user = Limiter(Limit(rate_per_second=0.0001, burst=1))
        group = Limiter(Limit(rate_per_second=0.0001, burst=1))
        host = self._host(user=user, group=group)
        # 第一次：用户桶放行，群桶放行
        self.assertEqual(allow_inbound_detailed(host, None), (True, 0.0, ""))
        # 第二次：用户桶先拒绝，原因是 user（不再消耗群桶）
        allowed, retry_after, reason = allow_inbound_detailed(host, None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "user")
        self.assertGreater(retry_after, 0)
        state = ensure_rate_limit_state(host)
        self.assertEqual(state.allowed, 1)
        self.assertEqual(state.rejected, 1)
        self.assertEqual(state.user_rejected, 1)
        self.assertEqual(state.group_rejected, 0)
        self.assertEqual(state.last_reason, "user")
        self.assertIsNotNone(state.last_rejected_at)

    def test_group_rejection_reason(self) -> None:
        group = Limiter(Limit(rate_per_second=0.0001, burst=1))
        host = self._host(group=group)
        self.assertTrue(allow_inbound_detailed(host, None)[0])
        allowed, _, reason = allow_inbound_detailed(host, None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "group")
        self.assertEqual(ensure_rate_limit_state(host).group_rejected, 1)

    def test_is_group_context_from_scope(self) -> None:
        host = self._host()
        self.assertTrue(is_group_context(host, None))

        class _Private:
            platform = "qq"
            platform_scope_id = "private:1"

        host.scope = lambda ctx: _Private()
        self.assertFalse(is_group_context(host, None))

    def test_retry_text(self) -> None:
        self.assertEqual(rate_limit_retry_text(0), "消息发送过于频繁，请稍后再试。")
        self.assertEqual(rate_limit_retry_text(2.4), "消息发送过于频繁，请在 2 秒后重试。")
        self.assertEqual(rate_limit_retry_text(0.1), "消息发送过于频繁，请在 1 秒后重试。")

    def test_mixin_and_status(self) -> None:
        from atri_x.agent.ratelimit import RateLimitMixin

        class _MixinHost(RateLimitMixin):
            pass

        host = _MixinHost()
        host.rate_limit_user = Limiter(Limit(rate_per_second=0.0001, burst=1))
        host.rate_limit_group = None
        host.rate_limit_state = RateLimitState()
        host.rate_limit_user_per_minute = 10
        host.rate_limit_user_burst = 5
        host.rate_limit_group_per_minute = 0
        host.rate_limit_group_burst = 0
        host.scope = lambda ctx: _Scope()
        host.actor = lambda ctx: Actor(id="u1", platform="qq", platform_user_id="u1")
        allowed, retry_after = host.allow_inbound(None)
        self.assertTrue(allowed)
        self.assertFalse(host.allow_inbound(None)[0])
        status = host.rate_limit_status()
        self.assertEqual(status.allowed, 1)
        self.assertEqual(status.rejected, 1)
        self.assertEqual(status.user_messages_per_minute, 10)
        self.assertEqual(status.keys, 1)
        self.assertIn("last_reason", status.to_dict())
        allowed_, rejected, keys = host.rate_limit_stats()
        self.assertEqual((status.allowed, status.rejected, status.keys), (allowed_, rejected, keys))
        self.assertEqual(host.rate_limit_retry_text(3), "消息发送过于频繁，请在 3 秒后重试。")

        host.services = {}
        service = install_ratelimit_services(host)
        self.assertIsInstance(service, RateLimitService)
        self.assertIs(host.services["ratelimit"], service)

    def test_to_dict_omits_empty_last(self) -> None:
        status = RateLimitStatus(allowed=1)
        data = status.to_dict()
        self.assertEqual(data["allowed"], 1)
        self.assertNotIn("last_reason", data)
        self.assertNotIn("last_rejected_at", data)


class TestWorkspaceStore(unittest.IsolatedAsyncioTestCase):
    def _host(self) -> object:
        class _SessionsRepo:
            def __init__(self) -> None:
                self.rows: dict[str, Session] = {"s1": Session(id="s1")}

            def get(self, session_id: str) -> Session:
                return self.rows[session_id]

            def mutate(self, session_id: str, apply) -> Session:
                row = self.rows[session_id]
                apply(row)
                return row

        class _Store:
            def __init__(self) -> None:
                self._sessions = _SessionsRepo()

            def sessions(self):
                return self._sessions

        class _Host:
            pass

        host = _Host()
        host.store = _Store()
        return host

    async def test_set_get_clear(self) -> None:
        host = self._host()
        session = host.store.sessions().get("s1")
        store = new_session_workspace_store(host, session)
        self.assertEqual(await store.get_workspace_dir(None), "")
        await store.set_workspace_dir(None, "/tmp/ws")
        self.assertEqual(await store.get_workspace_dir(None), "/tmp/ws")
        await store.clear_workspace_dir(None)
        self.assertEqual(await store.get_workspace_dir(None), "")

    async def test_notice_dirs(self) -> None:
        host = self._host()
        session = host.store.sessions().get("s1")
        store = SessionWorkspaceStore(host, session)
        self.assertFalse(await store.has_workspace_agent_notice_dir(None, "/tmp/ws"))
        await store.set_workspace_dir_with_agent_notice(None, "/tmp/ws", True)
        self.assertTrue(await store.has_workspace_agent_notice_dir(None, "/tmp/ws"))
        await store.mark_workspace_agent_notice_dir(None, "/tmp/ws")
        await store.clear_workspace_dir_with_agent_notice(None, True)
        self.assertEqual(await store.get_workspace_dir(None), "")

    async def test_unavailable_host_is_noop(self) -> None:
        store = SessionWorkspaceStore(None, None)
        self.assertEqual(await store.get_workspace_dir(None), "")
        await store.set_workspace_dir(None, "/tmp/ws")
        self.assertFalse(await store.has_workspace_agent_notice_dir(None, "/tmp/ws"))


class TestToolTagHelpers(unittest.IsolatedAsyncioTestCase):
    def _host(self, registry: _Registry, source: ToolTagConfigSource) -> object:
        host = _DirectiveHost(registry, source)
        return host

    async def test_names_by_tool_tag_merges_registry_and_config(self) -> None:
        registry = _Registry({"a": _Tool("a")}, {"media": ["a"]})
        source = ToolTagConfigSource(
            "", ToolTagsConfig(tags={"media": ToolTagConfig(tools=["b", "missing"])})
        )
        registry.tools["b"] = _Tool("b")
        host = self._host(registry, source)
        self.assertEqual(await names_by_tool_tag(host, None, "media"), ["a", "b"])
        self.assertEqual(
            await names_by_tool_tag(host, None, "media", lambda tool: tool.info().name == "b"),
            ["b"],
        )
        self.assertEqual(await names_by_tool_tag(host, None, "unknown"), [])
        self.assertEqual(await names_by_tool_tag(_DirectiveHost(None), None, "media"), [])

    async def test_completion_tool_names_by_tag(self) -> None:
        registry = _Registry({"a": _Tool("a")}, {"media": ["a"]})
        source = ToolTagConfigSource(
            "", ToolTagsConfig(tags={"media": ToolTagConfig(tools=["a"])})
        )
        host = self._host(registry, source)
        names = await completion_tool_names_by_tag(host, None, registry, "media", None)
        self.assertEqual(names, ["a"])
        self.assertEqual(
            await completion_tool_names_by_tag(host, None, registry, "media", lambda tool: False),
            [],
        )
        self.assertEqual(await completion_tool_names_by_tag(host, None, None, "media", None), [])

    def test_normalize_config_keeps_prompt_only_tags(self) -> None:
        cfg = normalize_tool_tags_config(
            ToolTagsConfig(tags={"a": ToolTagConfig(prompt="p"), "b": ToolTagConfig()})
        )
        self.assertEqual(list(cfg.tags), ["a"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
