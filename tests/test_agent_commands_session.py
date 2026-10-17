"""会话类命令的端到端测试：``/new``、``/status``、``/sessions``、``/resume`` 等。

对应 Go：``internal/agent/commands/session_*.go``。

这一组用**真实的** :class:`atri_x.session.Service` 与内存 SQLite ``Store``
（``tests.test_session.new_test_service``），只有模型 / 上下文状态这类外部服务是替身。
因此断言针对"命令确实驱动了会话状态"，而不是复述模板字符串。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.errors import ConflictError
from atri_x.storage import Message, RoleAssistant, RoleUser
from tests.agent_commands_fakes import Harness

PREFIX = "/"


def run(coro):
    return asyncio.run(coro)


class StatusTests(unittest.TestCase):
    def test_empty_status_without_current_session(self) -> None:
        harness = Harness()
        content = run(harness.router.dispatch(harness.ctx, "/status")).content
        self.assertIn("current session: none\n", content)
        self.assertIn("default mode: work\n", content)
        self.assertIn("current mode model: openai/gpt-4o\n", content)
        self.assertIn("compact model: openai/gpt-4o-mini\n", content)
        self.assertIn("platform: qq/private:1\n", content)
        self.assertIn("turn phase: idle\n", content)
        self.assertIn("tools: available in work mode", content)

    def test_status_with_current_session(self) -> None:
        harness = Harness()
        session = harness.create_session(title="测试会话")
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="你好")
        )
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleAssistant, content="在的")
        )
        content = run(harness.router.dispatch(harness.ctx, "/status")).content
        self.assertIn(f"  id: {session.id}\n", content)
        self.assertIn("  title: 测试会话\n", content)
        self.assertIn("  mode: work\n", content)
        self.assertIn("  message count: 2\n", content)
        self.assertIn("  archived: no\n", content)
        self.assertIn("  pinned: no\n", content)
        self.assertIn("  last ask: 你好\n", content)
        self.assertIn("  last answer: 在的\n", content)
        self.assertIn("  turn phase: idle\n", content)
        self.assertIn("  pending input: none\n", content)
        self.assertIn("  tool usage: none\n", content)
        self.assertIn(harness.context_status.text, content)

    def test_status_reports_live_tool_counts(self) -> None:
        harness = Harness()
        session = harness.create_session()
        self.assertTrue(harness.turns.start_llm(session.id, "hi"))
        harness.turns.add_tool_use(session.id, "read_file")
        harness.turns.add_tool_use(session.id, "read_file")
        content = run(harness.router.dispatch(harness.ctx, "/status")).content
        self.assertIn("  tool usage: read_file x2\n", content)

    def test_status_counts_stored_tool_calls(self) -> None:
        from datetime import datetime, timezone

        from atri_x.storage import ToolCallRecord

        harness = Harness()
        session = harness.create_session()
        harness.store.tool_calls().create(
            ToolCallRecord(
                session_id=session.id,
                tool_name="read_file",
                success=True,
                created_at=datetime.now(timezone.utc),
            )
        )
        content = run(harness.router.dispatch(harness.ctx, "/status")).content
        self.assertIn("  tool usage: read_file x1\n", content)


class NewAndModeTests(unittest.TestCase):
    def test_new_resets_current(self) -> None:
        harness = Harness()
        harness.create_session()
        self.assertTrue(harness.sessions.current(harness.scope_value))
        content = run(harness.router.dispatch(harness.ctx, "/new")).content
        self.assertEqual(
            content,
            "new session ready; it will be created when you send the first message",
        )
        with self.assertRaises(Exception):
            harness.sessions.current(harness.scope_value)

    def test_work_mode_switches_and_continues(self) -> None:
        harness = Harness()
        session = harness.create_session(mode="chat")
        result = run(harness.router.dispatch(harness.ctx, "/work 继续干活"))
        self.assertIn("work mode active:\n", result.content)
        self.assertIn(f"  id: {session.id}\n", result.content)
        self.assertIn("  mode: work\n", result.content)
        self.assertIn("  tools: enabled", result.content)
        self.assertIsNotNone(result.continuation)
        self.assertEqual(result.continuation.text, "继续干活")
        self.assertEqual(result.continuation.session_id, session.id)

    def test_work_mode_is_idempotent(self) -> None:
        harness = Harness()
        harness.create_session(mode="work")
        content = run(harness.router.dispatch(harness.ctx, "/work")).content
        self.assertTrue(content.startswith("already in work mode:\n"), content)

    def test_chat_mode_on_clean_session(self) -> None:
        harness = Harness()
        harness.create_session(mode="work")
        content = run(harness.router.dispatch(harness.ctx, "/chat")).content
        self.assertIn("chat mode active:\n", content)
        self.assertIn("  mode: chat", content)
        self.assertEqual(harness.sessions.current(harness.scope_value).mode, "chat")

    def test_chat_mode_on_dirty_session_explains_how_to_proceed(self) -> None:
        harness = Harness()
        session = harness.create_session(mode="work")
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="hi")
        )
        content = run(harness.router.dispatch(harness.ctx, "/chat")).content
        self.assertEqual(
            content,
            "current work session has history; run /new then /chat to start a clean "
            "chat session",
        )


class SessionsListTests(unittest.TestCase):
    def test_sessions_without_any_session(self) -> None:
        harness = Harness()
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/sessions")).content,
            "no sessions found",
        )

    def test_sessions_lists_and_numbers(self) -> None:
        harness = Harness()
        first = harness.create_session(title="甲")
        second = harness.create_session(title="乙")
        content = run(harness.router.dispatch(harness.ctx, "/sessions")).content
        self.assertTrue(content.startswith("sessions:\n"), content)
        self.assertIn("[1] 乙 [current]", content)
        self.assertIn("[2] 甲", content)
        self.assertIn(f"      id: {second.id}\n", content)
        self.assertIn(f"      id: {first.id}\n", content)
        self.assertTrue(content.endswith("page: 1"), content)

    def test_sessions_search_filters(self) -> None:
        harness = Harness()
        harness.create_session(title="甲")
        harness.create_session(title="乙")
        content = run(harness.router.dispatch(harness.ctx, "/sessions 甲")).content
        self.assertIn("甲", content)
        self.assertNotIn("乙", content)

    def test_sessions_records_numbering_for_follow_up_commands(self) -> None:
        harness = Harness()
        harness.create_session(title="甲")
        run(harness.router.dispatch(harness.ctx, "/sessions"))
        newest = harness.create_session(title="丙")
        content = run(harness.router.dispatch(harness.ctx, "/sessions")).content
        self.assertIn("[1] 丙 [current]", content)
        # /sessions 之后编号 1 指向列表里的第一条（最近活跃的"丙"）。
        self.assertEqual(
            harness.deps.session_state.get(harness.scope_value)[0], newest.id
        )
        renamed = run(harness.router.dispatch(harness.ctx, "/rename 1 改名了")).content
        self.assertIn(f"  id: {newest.id}\n", renamed)
        self.assertIn("  title: 改名了", renamed)

    def test_archives_lists_only_archived(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")
        harness.sessions.archive(harness.scope_value, session.id)
        content = run(harness.router.dispatch(harness.ctx, "/archives")).content
        self.assertIn("[1] 甲 [current, archived]", content)
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/sessions")).content,
            "no sessions found",
        )

    def test_archives_empty_message(self) -> None:
        harness = Harness()
        harness.create_session()
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/archives")).content,
            "no archived sessions found",
        )


class SessionLifecycleTests(unittest.TestCase):
    def test_archive_and_unarchive_flow(self) -> None:
        harness = Harness()
        harness.create_session(title="甲")
        harness.create_session(title="乙")
        target = harness.sessions.list(harness.scope_value, "甲", 10)[0]
        harness.deps.session_state.set(harness.scope_value, [target])

        confirm = run(harness.router.dispatch(harness.ctx, "/archive 1")).content
        self.assertIn("archive will modify session:\n", confirm)
        self.assertIn("  title: 甲\n", confirm)
        self.assertTrue(
            confirm.endswith(f"Run /archive {target.id} --confirm to continue.")
        )

        result = run(harness.router.dispatch(harness.ctx, "/archive 1 --confirm")).content
        self.assertIn("archived session:\n", result)
        self.assertIn(f"  id: {target.id}\n", result)
        self.assertIn("  title: 甲", result)

        restored = run(
            harness.router.dispatch(harness.ctx, f"/unarchive {target.id}")
        ).content
        self.assertIn("unarchived session:\n", restored)

    def test_pin_and_unpin(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")
        pinned = run(
            harness.router.dispatch(harness.ctx, f"/pin {session.id}")
        ).content
        self.assertIn("pinned session:\n", pinned)
        self.assertIsNotNone(
            harness.store.sessions().get(session.id).pinned_at
        )
        unpinned = run(
            harness.router.dispatch(harness.ctx, f"/unpin {session.id}")
        ).content
        self.assertIn("unpinned session:\n", unpinned)
        self.assertIsNone(harness.store.sessions().get(session.id).pinned_at)

    def test_rename_with_explicit_id(self) -> None:
        harness = Harness()
        session = harness.create_session(title="旧名")
        content = run(
            harness.router.dispatch(harness.ctx, f"/rename {session.id} 新名字")
        ).content
        self.assertIn("renamed session:\n", content)
        self.assertIn("  title: 新名字", content)
        self.assertEqual(harness.store.sessions().get(session.id).title, "新名字")

    def test_rename_without_target_renames_current(self) -> None:
        harness = Harness()
        session = harness.create_session(title="旧名")
        content = run(harness.router.dispatch(harness.ctx, "/rename 只要标题")).content
        self.assertIn("  title: 只要标题", content)
        self.assertEqual(harness.store.sessions().get(session.id).title, "只要标题")

    def test_rename_without_any_argument_is_usage_error(self) -> None:
        harness = Harness()
        harness.create_session()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/rename"))

    def test_rename_requires_target_when_titles_are_ambiguous(self) -> None:
        harness = Harness()
        harness.create_session(title="同名")
        harness.create_session(title="同名")
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/rename 同名 新名"))

    def test_delete_with_confirm(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")
        harness.create_session(title="乙")
        content = run(
            harness.router.dispatch(harness.ctx, f"/delete {session.id} --confirm")
        ).content
        self.assertEqual(content, f"deleted session:\n  id: {session.id}")
        with self.assertRaises(Exception):
            harness.store.sessions().get(session.id)

    def test_delete_drops_memories_from_that_session(self) -> None:
        from atri_x.angelmemory import Memory

        harness = Harness()
        session = harness.create_session(title="甲")
        harness.angel_memory.memories = [
            Memory(
                id="m1",
                platform="qq",
                scope_id="private:1",
                content="x",
                source_session_id=session.id,
            ),
            Memory(
                id="m2",
                platform="qq",
                scope_id="private:1",
                content="y",
                source_session_id="other",
            ),
        ]
        run(harness.router.dispatch(harness.ctx, f"/delete {session.id} --confirm"))
        self.assertEqual([m.id for m in harness.angel_memory.memories], ["m2"])
        self.assertIn("angel_memory_delete_by_session", harness.audit_events())

    def test_clean_needs_confirm_then_reports_count(self) -> None:
        harness = Harness()
        harness.create_session()
        content = run(harness.router.dispatch(harness.ctx, "/clean")).content
        self.assertIn("Run /clean --confirm to continue.", content)
        cleaned = run(harness.router.dispatch(harness.ctx, "/clean --confirm")).content
        self.assertEqual(cleaned, "cleaned expired sessions: 0")

    def test_clean_removes_only_expired(self) -> None:
        harness = Harness()
        stale = harness.create_session(title="旧")
        fresh = harness.create_session(title="新")
        old = datetime.now(timezone.utc) - timedelta(days=90)
        harness.store.sessions().mutate(stale.id, _set_updated_at(old))
        content = run(harness.router.dispatch(harness.ctx, "/clean --confirm")).content
        self.assertEqual(content, "cleaned expired sessions: 1")
        self.assertIsNotNone(harness.store.sessions().get(fresh.id))

    def test_numbered_target_requires_a_listing(self) -> None:
        harness = Harness()
        harness.create_session()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/pin 2"))


def _set_updated_at(value: datetime):
    def update(session) -> None:
        session.updated_at = value

    return update


class ResumeAndForkTests(unittest.TestCase):
    def test_resume_by_id_switches_current(self) -> None:
        harness = Harness()
        first = harness.create_session(title="甲")
        harness.sessions.reset_current(harness.scope_value)
        content = run(
            harness.router.dispatch(harness.ctx, f"/resume {first.id}")
        ).content
        self.assertIn("resumed session:\n", content)
        self.assertIn(f"  id: {first.id}\n", content)
        self.assertIn("  title: 甲", content)
        self.assertEqual(harness.sessions.current(harness.scope_value).id, first.id)
        self.assertIn("session_resume", harness.audit_events())

    def test_resume_reports_recent_messages(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="问题")
        )
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleAssistant, content="回答")
        )
        harness.sessions.reset_current(harness.scope_value)
        content = run(
            harness.router.dispatch(harness.ctx, f"/resume {session.id}")
        ).content
        self.assertIn("recent messages:\n", content)
        self.assertIn("  user: 问题\n", content)
        self.assertIn("  assistant: 回答", content)

    def test_resume_page_without_sessions(self) -> None:
        harness = Harness()
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/resume")).content,
            "no sessions found",
        )

    def test_resume_page_lists_resumable_sessions(self) -> None:
        harness = Harness()
        older = harness.create_session(title="以前")
        current = harness.create_session(title="现在")
        harness.store.sessions().mutate(
            older.id,
            _set_updated_at(datetime.now(timezone.utc) - timedelta(hours=1)),
        )
        content = run(harness.router.dispatch(harness.ctx, "/resume --page 1")).content
        self.assertIn("[1] 以前", content)
        self.assertNotIn(current.id, content)

    def test_resume_by_index_uses_the_resumable_order(self) -> None:
        harness = Harness()
        older = harness.create_session(title="以前")
        harness.create_session(title="现在")
        harness.store.sessions().mutate(
            older.id,
            _set_updated_at(datetime.now(timezone.utc) - timedelta(hours=1)),
        )
        content = run(harness.router.dispatch(harness.ctx, "/resume 1")).content
        self.assertIn(f"  id: {older.id}\n", content)
        self.assertEqual(harness.sessions.current(harness.scope_value).id, older.id)

    def test_resume_index_out_of_range(self) -> None:
        harness = Harness()
        harness.create_session(title="现在")
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/resume 5"))

    def test_messages_lists_assistant_ids(self) -> None:
        harness = Harness()
        session = harness.create_session()
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="问题")
        )
        stored = Message(session_id=session.id, role=RoleAssistant, content="回答")
        harness.store.messages().append(stored)
        content = run(harness.router.dispatch(harness.ctx, "/messages")).content
        self.assertIn("messages page 1/1:\n", content)
        self.assertIn(f"  {stored.id}: 回答", content)
        self.assertNotIn("问题", content)

    def test_messages_without_assistant_replies(self) -> None:
        harness = Harness()
        session = harness.create_session()
        harness.store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="问题")
        )
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/messages")).content,
            "no assistant messages found",
        )

    def test_fork_creates_child_session(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")
        stored = Message(session_id=session.id, role=RoleAssistant, content="回答")
        harness.store.messages().append(stored)
        content = run(
            harness.router.dispatch(harness.ctx, f"/fork {stored.id}")
        ).content
        self.assertIn("forked session:\n", content)
        self.assertIn(f"  parent: {session.id}\n", content)
        self.assertIn(f"  from message: {stored.id}\n", content)
        self.assertIn("session_fork", harness.audit_events())

    def test_fork_rejects_unknown_message(self) -> None:
        harness = Harness()
        harness.create_session()
        with self.assertRaises(Exception):
            run(harness.router.dispatch(harness.ctx, "/fork nope"))


class RequestCommandTests(unittest.TestCase):
    def test_requests_empty(self) -> None:
        harness = Harness()
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/requests")).content,
            "no active requests",
        )

    def test_requests_lists_turn_tree(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")

        async def scenario() -> str:
            turn, _, _ = await start_request(harness, session.id, "turn", "chat")
            await start_request(harness, session.id, "tool", "read_file", parent=turn.id)
            return (await harness.router.dispatch(harness.ctx, "/requests")).content

        content = asyncio.run(scenario())
        self.assertIn("[1] chat turn request ", content)
        self.assertIn("[1.1] read_file tool request ", content)
        self.assertIn("      session: 甲\n", content)
        self.assertIn("      mode: work\n", content)

    def test_stop_cancels_the_current_session(self) -> None:
        harness = Harness()
        session = harness.create_session()

        async def scenario() -> str:
            await start_request(harness, session.id, "turn", "chat")
            return (await harness.router.dispatch(harness.ctx, "/stop")).content

        self.assertEqual(asyncio.run(scenario()), "stopped 2 requests")
        self.assertEqual(harness.cancelled_inbox, [session.id])

    def test_stop_by_request_id_cancels_that_request(self) -> None:
        harness = Harness()
        session = harness.create_session()

        async def scenario() -> str:
            record, _, _ = await start_request(harness, session.id, "llm", "llm")
            return (
                await harness.router.dispatch(harness.ctx, f"/stop {record.id}")
            ).content

        self.assertEqual(asyncio.run(scenario()), "stopped 1 request")

    def test_stop_accepts_the_tree_number_from_requests(self) -> None:
        harness = Harness()
        session = harness.create_session()

        async def scenario() -> str:
            await start_request(harness, session.id, "llm", "llm")
            await harness.router.dispatch(harness.ctx, "/requests")
            return (await harness.router.dispatch(harness.ctx, "/stop 1")).content

        self.assertEqual(asyncio.run(scenario()), "stopped 1 request")

    def test_stop_unknown_request(self) -> None:
        harness = Harness()
        harness.create_session()
        self.assertEqual(
            run(harness.router.dispatch(harness.ctx, "/stop nope")).content,
            "request not found: nope",
        )

    def test_stop_all(self) -> None:
        harness = Harness()
        session = harness.create_session()

        async def scenario() -> str:
            await start_request(harness, session.id, "turn", "chat")
            return (await harness.router.dispatch(harness.ctx, "/stopall")).content

        self.assertEqual(asyncio.run(scenario()), "stopped 1 request")

    def test_plain_user_only_stops_own_session(self) -> None:
        harness = Harness(admin=False)
        session = harness.create_session()

        async def scenario() -> str:
            await start_request(harness, session.id, "turn", "chat")
            other, _, _ = await start_request(
                harness, "someone-else", "turn", "chat"
            )
            content = (await harness.router.dispatch(harness.ctx, "/stop")).content
            # 别人的 request 不能被普通用户停掉。
            self.assertEqual([r.id for r in harness.requests.list()], [other.id])
            return content

        self.assertEqual(asyncio.run(scenario()), "stopped 2 requests")


async def start_request(
    harness: Harness,
    session_id: str,
    kind: str,
    label: str,
    parent: str = "",
):
    """在真实的 ``atri_x.request.Manager`` 上登记一个进行中的请求。"""
    from atri_x.request import StartRequest

    return await harness.requests.start(
        None,
        StartRequest(
            parent_id=parent,
            session_id=session_id,
            kind=kind,
            label=label,
            scope_key="qq:private:1",
        ),
    )


class SessionManipulationAccessTests(unittest.TestCase):
    """每个会话命令都必须真正改到存储（而不是只返回文案）。"""

    def test_full_lifecycle_round_trip(self) -> None:
        harness = Harness()
        session = harness.create_session(title="甲")

        run(harness.router.dispatch(harness.ctx, f"/pin {session.id}"))
        self.assertIsNotNone(harness.store.sessions().get(session.id).pinned_at)
        run(harness.router.dispatch(harness.ctx, f"/unpin {session.id}"))
        self.assertIsNone(harness.store.sessions().get(session.id).pinned_at)

        run(harness.router.dispatch(harness.ctx, f"/archive {session.id} --confirm"))
        self.assertIsNotNone(harness.store.sessions().get(session.id).archived_at)
        run(harness.router.dispatch(harness.ctx, f"/unarchive {session.id}"))
        self.assertIsNone(harness.store.sessions().get(session.id).archived_at)

        run(harness.router.dispatch(harness.ctx, f"/delete {session.id} --confirm"))
        with self.assertRaises(Exception):
            harness.store.sessions().get(session.id)


class SessionConflictTests(unittest.TestCase):
    def test_archiving_a_shared_session_conflict_surfaces(self) -> None:
        """服务抛出的错误（不是命令层的用法错误）必须原样冒泡。"""

        harness = Harness()
        session = harness.create_session(title="甲")

        class Boom:
            def archive(self, scope, session_id):
                raise ConflictError("nope")

        original = harness.sessions.archive
        harness.sessions.archive = Boom().archive
        try:
            with self.assertRaises(ConflictError):
                run(
                    harness.router.dispatch(
                        harness.ctx, f"/archive {session.id} --confirm"
                    )
                )
        finally:
            harness.sessions.archive = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
