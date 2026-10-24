"""授权生命周期（评估 §B1 / §B2）的行为契约测试。

覆盖三块：

* :class:`atri_x.security.grants.GrantRegistry`：TTL、次数上限、scope/actor 绑定、
  撤销（按 id 前缀 / 作用域 / Session / 主体 / 全清）；
* ``chat_tools`` 的放行：``/confirmall`` / ``/confirmtool`` 产生带绑定的凭据，
  命中即消耗次数并写 ``grant_used`` 审计；换个人 / 换个会话不生效；
* ``/grant`` 与 ``/panic`` 命令：列表、按 id 撤销、群里需要二次确认、panic 的
  清空与停止动作。
"""

import asyncio
import unittest
from types import SimpleNamespace

from atri_x.agent.chat_tools import (
    auto_confirm_allowed,
    grant_registry,
    grant_session_auto_confirm,
    grant_tool_auto_confirm,
    is_session_auto_confirmed,
    is_tool_auto_confirmed,
    revoke_all_grants,
    revoke_scope_grants,
    revoke_session_grants,
    set_session_auto_confirmed,
    set_tool_auto_confirmed,
)
from atri_x.agent.commands.approval import format_grant_list, new_grant, new_panic
from atri_x.agent.commands.deps import Deps
from atri_x.command import Request
from atri_x.security import (
    ACTOR_CONTEXT_KEY,
    GRANT_KIND_SESSION,
    GRANT_KIND_TOOL,
    Actor,
    GrantRegistry,
    RoleSuperadmin,
)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value


class _AuditLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, level: str, message: str, *attrs) -> None:  # noqa: ANN001
        pairs = {}
        for index in range(0, len(attrs) - 1, 2):
            pairs[attrs[index]] = attrs[index + 1]
        self.events.append((str(pairs.get("event", "")), pairs))


def _ctx(actor_id: str = "qq_onebot:10000", role: str = RoleSuperadmin):
    actor = Actor(id=actor_id, platform="qq_onebot", platform_user_id="10000", role=role)
    return {ACTOR_CONTEXT_KEY: actor}


def _scope(scope_id: str = "group:1"):
    return SimpleNamespace(platform="qq_onebot", platform_scope_id=scope_id)


def _agent(registry=None, logger=None, scope_id: str = "group:1"):
    agent = SimpleNamespace(
        grants=registry,
        audit_logger=logger,
        _scope_id=scope_id,
    )
    agent.scope = lambda ctx: _scope(agent._scope_id)  # noqa: ARG005
    return agent


class GrantRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.registry = GrantRegistry(ttl_seconds=600.0, clock=self.clock)

    def test_grant_and_find(self) -> None:
        grant = self.registry.grant(
            GRANT_KIND_SESSION, session_id="s1", scope_key="qq:group:1", actor_id="u1"
        )
        found = self.registry.find(
            GRANT_KIND_SESSION, session_id="s1", scope_key="qq:group:1", actor_id="u1"
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.id, grant.id)
        self.assertEqual(self.registry.count(), 1)

    def test_binding_rejects_other_actor_and_scope(self) -> None:
        self.registry.grant(
            GRANT_KIND_SESSION, session_id="s1", scope_key="qq:group:1", actor_id="u1"
        )
        self.assertIsNone(
            self.registry.find(
                GRANT_KIND_SESSION, session_id="s1", scope_key="qq:group:1", actor_id="u2"
            )
        )
        self.assertIsNone(
            self.registry.find(
                GRANT_KIND_SESSION, session_id="s1", scope_key="qq:group:2", actor_id="u1"
            )
        )

    def test_tool_grant_only_covers_its_tool(self) -> None:
        self.registry.grant(GRANT_KIND_TOOL, tool="shell", session_id="s1")
        self.assertIsNotNone(
            self.registry.find(GRANT_KIND_TOOL, tool="shell", session_id="s1")
        )
        self.assertIsNone(
            self.registry.find(GRANT_KIND_TOOL, tool="write_file", session_id="s1")
        )

    def test_expiry(self) -> None:
        grant = self.registry.grant(GRANT_KIND_SESSION, session_id="s1", ttl_seconds=60)
        self.clock.value += 59
        self.assertIsNotNone(self.registry.find(GRANT_KIND_SESSION, session_id="s1"))
        self.clock.value += 2
        self.assertIsNone(self.registry.find(GRANT_KIND_SESSION, session_id="s1"))
        self.assertEqual(self.registry.count(), 0)
        self.assertEqual(grant.remaining_seconds(self.clock.value), 0.0)

    def test_no_expiry_grant(self) -> None:
        self.registry.grant(GRANT_KIND_SESSION, session_id="s1", ttl_seconds=0)
        self.clock.value += 10**6
        grant = self.registry.find(GRANT_KIND_SESSION, session_id="s1")
        self.assertIsNotNone(grant)
        self.assertEqual(grant.remaining_seconds(self.clock.value), -1.0)

    def test_consume_uses_limit(self) -> None:
        self.registry.grant(GRANT_KIND_TOOL, tool="shell", session_id="s1", max_uses=2)
        self.assertIsNotNone(self.registry.consume(GRANT_KIND_TOOL, tool="shell", session_id="s1"))
        self.assertIsNotNone(self.registry.consume(GRANT_KIND_TOOL, tool="shell", session_id="s1"))
        self.assertIsNone(self.registry.consume(GRANT_KIND_TOOL, tool="shell", session_id="s1"))
        self.assertEqual(self.registry.count(), 0)

    def test_revoke_by_id_and_prefix(self) -> None:
        first = self.registry.grant(GRANT_KIND_SESSION, session_id="s1")
        second = self.registry.grant(GRANT_KIND_SESSION, session_id="s2")
        self.assertIsNotNone(self.registry.revoke(first.id[:5]))
        self.assertEqual(self.registry.count(), 1)
        self.assertIsNotNone(self.registry.revoke(second.id))
        self.assertIsNone(self.registry.revoke("nope"))

    def test_revoke_scope_session_actor(self) -> None:
        self.registry.grant(GRANT_KIND_SESSION, session_id="s1", scope_key="qq:group:1", actor_id="u1")
        self.registry.grant(GRANT_KIND_SESSION, session_id="s2", scope_key="qq:group:2", actor_id="u1")
        self.registry.grant(GRANT_KIND_SESSION, session_id="s3", scope_key="qq:group:2", actor_id="u2")
        self.assertEqual(self.registry.revoke_scope("qq:group:2"), 2)
        self.assertEqual(self.registry.count(), 1)
        self.assertEqual(self.registry.revoke_actor("u1"), 1)
        self.assertEqual(self.registry.count(), 0)

    def test_revoke_session_and_clear(self) -> None:
        self.registry.grant(GRANT_KIND_SESSION, session_id="s1")
        self.registry.grant(GRANT_KIND_SESSION, session_id="s1")
        self.assertEqual(self.registry.revoke_session("s1"), 2)
        self.registry.grant(GRANT_KIND_SESSION, session_id="s2")
        self.assertEqual(self.registry.clear(), 1)
        self.assertEqual(self.registry.count(), 0)


class ChatToolsGrantTest(unittest.TestCase):
    def test_session_grant_binds_scope_and_actor(self) -> None:
        logger = _AuditLogger()
        agent = _agent(registry=GrantRegistry(ttl_seconds=600.0), logger=logger)
        grant_session_auto_confirm(agent, _ctx(), "s1")
        self.assertTrue(auto_confirm_allowed(agent, _ctx(), "s1", "shell"))
        events = [event for event, _ in logger.events]
        self.assertIn("grant_created", events)
        self.assertIn("grant_used", events)
        # 换个主体（同名会话）不能复用
        other_actor = _ctx(actor_id="qq_onebot:20000")
        self.assertFalse(auto_confirm_allowed(agent, other_actor, "s1", "shell"))

    def test_session_grant_not_usable_in_other_scope(self) -> None:
        agent = _agent(registry=GrantRegistry(ttl_seconds=600.0))
        grant_session_auto_confirm(agent, _ctx(), "s1")
        agent._scope_id = "group:2"
        self.assertFalse(auto_confirm_allowed(agent, _ctx(), "s1", "shell"))

    def test_tool_grant_and_consume(self) -> None:
        agent = _agent(registry=GrantRegistry(ttl_seconds=600.0))
        grant_tool_auto_confirm(agent, _ctx(), "s1", "shell")
        self.assertTrue(auto_confirm_allowed(agent, _ctx(), "s1", "shell"))
        self.assertFalse(auto_confirm_allowed(agent, _ctx(), "s1", "write_file"))

    def test_tool_grant_expires(self) -> None:
        clock = _Clock()
        agent = _agent(registry=GrantRegistry(ttl_seconds=60.0, clock=clock))
        grant_tool_auto_confirm(agent, _ctx(), "s1", "shell")
        clock.value += 61
        self.assertFalse(auto_confirm_allowed(agent, _ctx(), "s1", "shell"))

    def test_legacy_shims(self) -> None:
        agent = _agent(registry=GrantRegistry(ttl_seconds=600.0))
        self.assertFalse(is_session_auto_confirmed(agent, "s1"))
        set_tool_auto_confirmed(agent, "s1", "shell")
        self.assertTrue(is_tool_auto_confirmed(agent, "s1", "shell"))
        self.assertFalse(is_session_auto_confirmed(agent, "s1"))
        set_session_auto_confirmed(agent, "s1")
        self.assertTrue(is_session_auto_confirmed(agent, "s1"))
        self.assertEqual(grant_registry(agent).count(), 2)

    def test_revoke_helpers(self) -> None:
        agent = _agent(registry=GrantRegistry(ttl_seconds=600.0))
        grant_session_auto_confirm(agent, _ctx(), "s1")
        grant_tool_auto_confirm(agent, _ctx(), "s2", "shell")
        self.assertEqual(revoke_session_grants(agent, "s1"), 1)
        self.assertEqual(revoke_scope_grants(agent, "qq_onebot:group:1"), 1)
        grant_session_auto_confirm(agent, _ctx(), "s3")
        self.assertEqual(revoke_all_grants(agent), 1)


class ApprovalCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = GrantRegistry(ttl_seconds=600.0)
        self.audited: list[tuple] = []
        self.stopped = 0
        self.cancelled = 0
        self.inbox_cleared = 0
        self.deps = Deps(
            grants=self.registry,
            scope=lambda ctx: _scope("private:10000"),  # noqa: ARG005
            turns=SimpleNamespace(
                snapshot_all=lambda: [SimpleNamespace(session_id="s1")],
                stop_all=self._stop_all,
            ),
            requests=SimpleNamespace(cancel_all=self._cancel_all),
            cancel_session_inbox=self._clear_inbox,
            audit=lambda event, *attrs: self.audited.append((event, attrs)),
        )

    def _stop_all(self) -> None:
        self.stopped += 1

    def _cancel_all(self) -> int:
        self.cancelled += 2
        return 2

    def _clear_inbox(self, session_id: str) -> int:
        self.inbox_cleared += 1
        return 1

    def _run(self, handler, args: str):
        return asyncio.run(handler.handle(None, Request(name="x", args=args)))

    def test_grant_list_and_revoke(self) -> None:
        grant = self.registry.grant(GRANT_KIND_SESSION, session_id="s1")
        handler = new_grant(self.deps)
        listed = self._run(handler, "")
        self.assertIn(grant.id, listed.content)
        self.assertIn("当前生效的授权", listed.content)
        revoked = self._run(handler, f"revoke {grant.id}")
        self.assertIn("已撤销授权", revoked.content)
        self.assertEqual(self.registry.count(), 0)
        missing = self._run(handler, "revoke not-there")
        self.assertIn("没有找到", missing.content)

    def test_grant_revoke_requires_id(self) -> None:
        handler = new_grant(self.deps)
        result = self._run(handler, "revoke")
        self.assertIn("/grant revoke", result.content)

    def test_grant_list_empty(self) -> None:
        self.assertIn("当前没有生效中的授权", format_grant_list([], 0.0))

    def test_panic_in_private(self) -> None:
        self.registry.grant(GRANT_KIND_SESSION, session_id="s1")
        handler = new_panic(self.deps)
        result = self._run(handler, "")
        self.assertEqual(self.registry.count(), 0)
        self.assertEqual(self.stopped, 1)
        self.assertEqual(self.cancelled, 2)
        self.assertEqual(self.inbox_cleared, 1)
        self.assertIn("已执行紧急停止", result.content)
        self.assertEqual(self.audited[-1][0], "panic")

    def test_panic_in_group_needs_confirm(self) -> None:
        self.deps.scope = lambda ctx: _scope("group:1")  # noqa: ARG005
        self.registry.grant(GRANT_KIND_SESSION, session_id="s1")
        handler = new_panic(self.deps)
        result = self._run(handler, "")
        self.assertIn("/panic", result.content)
        self.assertEqual(self.registry.count(), 1)
        confirmed = self._run(handler, "confirm")
        self.assertIn("已执行紧急停止", confirmed.content)
        self.assertEqual(self.registry.count(), 0)


if __name__ == "__main__":
    unittest.main()
