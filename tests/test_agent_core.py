"""``atri_x.agent`` 核心：构造、身份、状态与纯逻辑助手。

对应 Go ``internal/agent`` 的 core / identity / prefix / status / turn_gate /
session_metadata / segments / notice_queue / options / ops 等文件。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from atri_x.config import ProviderConfig, ToolsConfig, TurnDirectivesConfig
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm import MessageSegment
from atri_x.platform import MessageContext, with_message_context
from atri_x.runtime import PhaseDone, PhaseError, PhaseIdle, Snapshot
from atri_x.security import default_policy
from atri_x.session import Config as SessionConfig
from atri_x.storage import Message, Session, SessionModeWork
from atri_x.turn import Input as TurnInput, Speaker

from atri_x.agent import chat_tools
from atri_x.agent.core import Agent, StaticSoulProvider, new_options
from atri_x.agent.options import Options, validate_options
from atri_x.agent.session_metadata import (
    ContextCompactState,
    LLMOriginState,
    decode_session_metadata,
    encode_session_metadata,
    encode_session_metadata_into,
)


class _Sessions:
    def __init__(self) -> None:
        self.rows: dict[str, Session] = {}

    def create(self, session: Session) -> Session:
        if not getattr(session, "id", ""):
            # 真实存储会在 create 里分配 ID（契约：带连字符的小写 UUIDv4）。
            from atri_x.storage import new_id

            session.id = new_id()
        self.rows[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        if session_id not in self.rows:
            raise NotFoundError(session_id)
        return self.rows[session_id]

    def mutate(self, session_id: str, update) -> Session:
        row = self.get(session_id)
        update(row)
        return row

    def update(self, session: Session) -> Session:
        self.rows[session.id] = session
        return session


class _Messages:
    """``storage.MessageRepository`` 的替身。

    真实仓储的写入方法是 ``append``（见 ``atri_x/storage/repositories.py`` 的
    ``MessageRepository`` 协议）；``create`` 是给旧调用点留的同义别名。
    """

    def __init__(self) -> None:
        self.rows: list[Message] = []

    def append(self, message: Message) -> Message:
        self.rows.append(message)
        return message

    def create(self, message: Message) -> Message:
        return self.append(message)

    def list_by_session(self, session_id: str) -> list[Message]:
        return [row for row in self.rows if row.session_id == session_id]


class _ToolCalls:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def create(self, record: object) -> None:
        self.rows.append(record)


class _Store:
    def __init__(self) -> None:
        self._sessions = _Sessions()
        self._messages = _Messages()
        self._tool_calls = _ToolCalls()

    def sessions(self) -> _Sessions:
        return self._sessions

    def messages(self) -> _Messages:
        return self._messages

    def tool_calls(self) -> _ToolCalls:
        return self._tool_calls

    def media(self) -> None:
        return None


class _Client:
    """最小 LLM 客户端（本文件的用例不会真的调用它）。"""

    async def chat_stream(self, ctx, req):  # pragma: no cover - 只占位
        raise AssertionError("chat_stream must not be called in this test")


class _Platform:
    def name(self) -> str:
        return "cli"


def make_agent(**overrides) -> Agent:
    """构造一个可用的最小 Agent（真实 Store 替身 + 假客户端）。"""
    opts = Options(
        platform=overrides.pop("platform", _Platform()),
        clients={"default": _Client()},
        mode_models={"work": overrides.pop("work_model", None) or _model("m1")},
        providers={"default": ProviderConfig()},
        store=overrides.pop("store", None) or _Store(),
        command_prefixes=["/"],
        session_config=SessionConfig(default_mode="work"),
        session_list_page_size=10,
        cleanup_retention_days=30,
        sandbox_root=".",
        tools_config=ToolsConfig(max_rounds_per_turn=2),
        turn_directives=TurnDirectivesConfig(),
        security_policy=default_policy(),
        ops=overrides.pop("ops", None),
    )
    for key, value in overrides.items():
        setattr(opts, key, value)
    return new_options(opts)


def _model(name: str):
    from atri_x.config import ModelSelection

    return ModelSelection(provider="default", model=name)


def make_ctx(**overrides) -> object:
    fields = {
        "platform": "cli",
        "actor_id": "u1",
        "platform_user_id": "10001",
        "nickname": "nick",
        "display_name": "显示名",
        "scope_id": "local",
        "platform_message_id": "pm1",
        "segments": [MessageSegment(type="text", text="hi")],
    }
    fields.update(overrides)
    return with_message_context(None, MessageContext(**fields))


class OptionsTest(unittest.TestCase):
    def test_validate_options_rejects_missing_work_model(self) -> None:
        with self.assertRaises(InvalidError):
            validate_options(Options(store=_Store(), providers={"default": ProviderConfig()}))

    def test_validate_options_rejects_missing_store(self) -> None:
        with self.assertRaises(InvalidError):
            validate_options(
                Options(
                    mode_models={"work": _model("m1")},
                    providers={"default": ProviderConfig()},
                    clients={"default": _Client()},
                    session_config=SessionConfig(default_mode="work"),
                    session_list_page_size=10,
                    cleanup_retention_days=30,
                    sandbox_root=".",
                    tools_config=ToolsConfig(max_rounds_per_turn=2),
                    security_policy=default_policy(),
                )
            )


class AgentConstructionTest(unittest.TestCase):
    def test_new_options_wires_core_state(self) -> None:
        agent = make_agent()
        self.assertIsInstance(agent, Agent)
        self.assertEqual(agent.actor_id, "cli:local")
        self.assertEqual(agent.scope_id, "local")
        self.assertIsInstance(agent.soul, StaticSoulProvider)
        self.assertEqual(agent.max_tool_rounds_per_turn(), 2)
        self.assertEqual(agent.command_prefix(), "/")
        self.assertIsNotNone(agent.sessions)
        self.assertIsNotNone(agent.turns)
        self.assertIsNotNone(agent.requests)

    def test_validation_runs_before_state_is_built(self) -> None:
        with self.assertRaises(InvalidError):
            new_options(Options())

    def test_session_metadata_roundtrip(self) -> None:
        agent = make_agent()
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        metadata = agent.session_metadata(session)
        metadata.llm_origin = LLMOriginState(provider="default", model="m1")
        metadata.context_compact = ContextCompactState(pending=True, summary="摘要")
        agent.store_session_metadata(session, metadata)
        decoded = decode_session_metadata(session.metadata)
        self.assertEqual(decoded.llm_origin.provider, "default")
        self.assertTrue(decoded.context_compact.pending)
        self.assertEqual(decoded.context_compact.summary, "摘要")
        # 再编码一次必须幂等，且不丢别的字段。
        again = encode_session_metadata_into(session.metadata, decoded)
        self.assertEqual(decode_session_metadata(again).llm_origin.model, "m1")

    def test_encode_session_metadata_omits_empty_fields(self) -> None:
        raw = encode_session_metadata(decode_session_metadata(""))
        self.assertIn(raw, ("", "{}"))


class IdentityAndPrefixTest(unittest.TestCase):
    def test_scope_and_actor_come_from_message_context(self) -> None:
        agent = make_agent()
        ctx = make_ctx(scope_id="group:42")
        scope = agent.scope(ctx)
        self.assertEqual(scope.platform, "cli")
        self.assertEqual(scope.platform_scope_id, "group:42")
        actor = agent.actor(ctx)
        self.assertTrue(actor.id)
        self.assertEqual(actor.platform_user_id, "10001")

    def test_scope_falls_back_to_agent_defaults(self) -> None:
        agent = make_agent()
        scope = agent.scope(None)
        self.assertEqual(scope.platform_scope_id, agent.scope_id)

    def test_is_cancel_command(self) -> None:
        agent = make_agent()
        self.assertTrue(agent.is_cancel_command("/cancel"))
        self.assertFalse(agent.is_cancel_command("继续"))


class TurnGateAndStatusTest(unittest.TestCase):
    def test_turn_output_allowed_defaults_true_and_false_after_cancel(self) -> None:
        agent = make_agent()
        ctx = make_ctx()
        turn_request_ctx = agent and None  # 语义占位：无明显 ctx 时默认允许
        self.assertTrue(agent.turn_output_allowed(ctx))
        from atri_x.agent.request_context import with_turn_request_id

        request_ctx = with_turn_request_id(None, "r1")
        self.assertTrue(agent.turn_output_allowed(request_ctx))
        agent.mark_turn_canceled("r1")
        self.assertFalse(agent.turn_output_allowed(request_ctx))
        agent.clear_turn_state("r1")
        self.assertTrue(agent.turn_output_allowed(request_ctx))
        self.assertIsNone(turn_request_ctx)

    def test_runtime_status_records_and_finishes(self) -> None:
        agent = make_agent()
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        snapshot = Snapshot(session_id="s1", phase=PhaseIdle, turn_started_at=now)
        agent.record_runtime_status(snapshot)
        self.assertEqual(agent.runtime_status_for_session("s1").phase, PhaseIdle)
        agent.record_runtime_status(
            Snapshot(session_id="s1", phase=PhaseDone, turn_started_at=now)
        )
        self.assertEqual(agent.runtime_status_for_session("s1").phase, PhaseDone)

    def test_runtime_status_keeps_error_phase(self) -> None:
        agent = make_agent()
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        agent.record_runtime_status(Snapshot(session_id="s1", phase=PhaseError, turn_started_at=now))
        self.assertEqual(agent.runtime_status_for_session("s1").phase, PhaseError)


class SessionMetadataHelpersTest(unittest.TestCase):
    def test_pending_context_compact_requires_pending_and_summary(self) -> None:
        from atri_x.agent.chat import pending_context_compact

        session = Session(id="s1", metadata="")
        self.assertIsNone(pending_context_compact(session))
        session.metadata = encode_session_metadata(
            decode_session_metadata("").__class__(
                context_compact=ContextCompactState(pending=True, summary="")
            )
        )
        self.assertIsNone(pending_context_compact(session))
        session.metadata = encode_session_metadata(
            decode_session_metadata("").__class__(
                context_compact=ContextCompactState(pending=True, summary="摘要")
            )
        )
        seed = pending_context_compact(session)
        self.assertIsNotNone(seed)
        self.assertEqual(seed.summary, "摘要")


class ChatToolsHelpersTest(unittest.TestCase):
    def test_preview_runes_and_log_limits(self) -> None:
        self.assertEqual(chat_tools.preview_runes("  你好世界  ", 3), "你好世...")
        self.assertEqual(chat_tools.preview_runes("", 10), "")
        self.assertEqual(chat_tools.tool_log_result_limit("read_file"), 128)
        self.assertEqual(chat_tools.tool_log_result_limit("write_file"), 256)
        self.assertEqual(chat_tools.preview_log_text("x" * 200).endswith("..."), True)

    def test_compact_and_preview_arguments(self) -> None:
        self.assertEqual(chat_tools.compact_arguments("  "), "{}")
        self.assertEqual(chat_tools.compact_arguments('{ "a" : 1 }'), '{"a":1}')
        self.assertEqual(chat_tools.compact_arguments("not json"), "not json")
        self.assertEqual(chat_tools.preview_arguments(""), "{}")

    def test_join_assistant_text(self) -> None:
        self.assertEqual(chat_tools.join_assistant_text("", "b"), "b")
        self.assertEqual(chat_tools.join_assistant_text("a", ""), "a")
        self.assertEqual(chat_tools.join_assistant_text(" a ", " b "), "a\n\nb")

    def test_skipped_tool_messages_keep_call_identity(self) -> None:
        from atri_x.llm import ToolCallRequest

        calls = [
            ToolCallRequest(id="c1", name="read_file", arguments="{}"),
            ToolCallRequest(id="c2", name="write_file", arguments="{}"),
        ]
        messages = chat_tools.skipped_tool_messages(calls, 3)
        self.assertEqual([m.tool_call_id for m in messages], ["c1", "c2"])
        self.assertEqual([m.name for m in messages], ["read_file", "write_file"])
        self.assertIn("max_rounds_per_turn=3", messages[0].segments[0].text)

    def test_filter_and_merge_schemas(self) -> None:
        from atri_x.llm import ToolFunctionSchema, ToolSchema

        schemas = [
            ToolSchema(function=ToolFunctionSchema(name="Read_File")),
            ToolSchema(function=ToolFunctionSchema(name="write_file")),
        ]
        filtered = chat_tools.filter_tool_schemas_by_allowlist(schemas, {"read_file": True})
        self.assertEqual([s.function.name for s in filtered], ["Read_File"])
        self.assertEqual(chat_tools.filter_tool_schemas_by_allowlist(schemas, None), schemas)
        merged = chat_tools.merge_tool_schemas(
            schemas, [ToolSchema(function=ToolFunctionSchema(name="write_file"))]
        )
        self.assertEqual(len(merged), 2)

    def test_session_tools_enabled_only_for_work(self) -> None:
        work = Session(id="s1", mode=SessionModeWork)
        chat = Session(id="s2", mode="chat")
        self.assertTrue(chat_tools.session_tools_enabled(work))
        self.assertFalse(chat_tools.session_tools_enabled(chat))
        self.assertFalse(chat_tools.session_tools_enabled(None))

    def test_risk_texts_and_results(self) -> None:
        self.assertEqual(chat_tools.risk_reasons_text([]), "")
        self.assertEqual(chat_tools.risk_reasons_text(["a", " b "]), "\n风险原因：\n- a\n- b")
        self.assertEqual(chat_tools.risk_confirmation_result("reject"), "rejected")
        self.assertEqual(chat_tools.risk_confirmation_result("stop"), "canceled")
        self.assertEqual(chat_tools.risk_confirmation_result("confirmall"), "succeeded")

    def test_auto_confirm_flags(self) -> None:
        agent = make_agent()
        self.assertFalse(chat_tools.is_session_auto_confirmed(agent, "s1"))
        chat_tools.set_session_auto_confirmed(agent, "s1")
        self.assertTrue(chat_tools.is_session_auto_confirmed(agent, "s1"))
        self.assertFalse(chat_tools.is_tool_auto_confirmed(agent, "s1", "read_file"))
        chat_tools.set_tool_auto_confirmed(agent, "s1", "read_file")
        self.assertTrue(chat_tools.is_tool_auto_confirmed(agent, "s1", "read_file"))

    def test_is_background_session_reads_metadata(self) -> None:
        from atri_x.agent.session_metadata import SessionMetadata

        session = Session(id="s1", metadata="")
        self.assertFalse(chat_tools.is_background_session(session))
        session.metadata = encode_session_metadata(SessionMetadata(background_kind="cron"))
        self.assertTrue(chat_tools.is_background_session(session))

    def test_drain_pending_user_input_adds_message(self) -> None:
        agent = make_agent()
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        agent.turns.start_llm_input(session.id, TurnInput(text="hi"))
        self.assertTrue(agent.turns.interrupt_llm_input(session.id, TurnInput(text="再说一句")))
        messages, pending = chat_tools.drain_pending_user_input(
            agent, make_ctx(), session.id, []
        )
        self.assertIsNotNone(pending)
        self.assertEqual(pending.message_index, 0)
        self.assertEqual(len(messages), 1)
        self.assertIn("再说一句", pending.message.content)
        # 抽干之后第二次调用不再产生 pending。
        again, second = chat_tools.drain_pending_user_input(agent, make_ctx(), session.id, messages)
        self.assertIsNone(second)
        self.assertEqual(again, messages)

    def test_speaker_marker_only_for_shared_scope(self) -> None:
        agent = make_agent()
        input_value = TurnInput(
            text="hi", speaker=Speaker(actor_id="u1", user_id="10001", name="nick", role="member")
        )
        plain = chat_tools.attach_speaker_marker(agent, make_ctx(), input_value, [])
        self.assertEqual(plain, [])
        self.assertIn("nick", chat_tools.input_speaker_metadata(input_value))
        self.assertEqual(chat_tools.speaker_display_name(Speaker(name="a[b]c")), "a（b）c")

    def test_max_tool_rounds_defaults_to_two(self) -> None:
        agent = make_agent()
        agent.tool_runtime.config = None
        self.assertEqual(chat_tools.max_tool_rounds_per_turn(agent), 2)


class NoticeQueueTest(unittest.TestCase):
    def test_notice_target_key_is_stable(self) -> None:
        from atri_x.agent.notice_queue import notice_target_key
        from atri_x.delivery import Notice, Target

        notice = Notice(target=Target(platform="qq", scope_id="group:1"))
        same = Notice(target=Target(platform="qq", scope_id="group:1"))
        other = Notice(target=Target(platform="qq", scope_id="group:2"))
        self.assertEqual(notice_target_key(notice), notice_target_key(same))
        self.assertNotEqual(notice_target_key(notice), notice_target_key(other))
        self.assertEqual(notice_target_key(Notice()), "context")

    def test_notice_queues_close_is_idempotent(self) -> None:
        from atri_x.agent.notice_queue import NoticeQueues

        queues = NoticeQueues()
        asyncio.run(queues.close(None))
        asyncio.run(queues.close(None))


class ActiveRequestsTest(unittest.TestCase):
    def test_active_requests_snapshot(self) -> None:
        agent = make_agent()
        snapshot = agent.active_requests()
        self.assertIsNotNone(snapshot)


class GroupServicesConfigSeedTest(unittest.TestCase):
    """构造期必须把 ``GroupServicesConfig`` 落到宿主的 ``group_services_cfg``。

    真实事故：``opts.group_services`` 只被塞进 ``services["group_services"]``，而读侧
    :func:`atri_x.agent.group_services_state.group_services_config` 取的是
    ``group_services_cfg`` 属性（或 ``services["group_services_config"]``）—— 两条都不是
    前者。于是 ``[group_services] enabled = false`` **完全无效**：提醒派发循环照起、
    各项上限退回默认值，且不报任何错。
    对应 Go ``internal/agent/core.go:343`` ``groupServicesCfg: opts.GroupServices.Normalized()``。
    """

    def test_disabled_config_reaches_the_read_side(self) -> None:
        from atri_x.agent.group_services_state import group_services_config
        from atri_x.config.app_sections import GroupServicesConfig

        agent = make_agent(group_services=GroupServicesConfig(enabled=False))

        self.assertFalse(group_services_config(agent).is_enabled())

    def test_default_config_enabled_and_limits_normalized(self) -> None:
        from atri_x.agent.group_services_state import group_services_config
        from atri_x.config.app_sections import GroupServicesConfig

        agent = make_agent(group_services=GroupServicesConfig(max_reminders_per_scope=0))
        config = group_services_config(agent)

        self.assertTrue(config.is_enabled())
        # ``normalized()`` 把非法上限回落成默认值（对应 Go ``Normalized()``）。
        self.assertEqual(config.max_reminders_per_scope, 50)

    def test_seed_is_a_copy(self) -> None:
        from atri_x.agent.group_services_state import group_services_config
        from atri_x.config.app_sections import GroupServicesConfig

        passed = GroupServicesConfig(enabled=False)
        agent = make_agent(group_services=passed)

        group_services_config(agent).enabled = True

        self.assertFalse(passed.enabled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
