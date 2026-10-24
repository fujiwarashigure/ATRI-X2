"""回合主流程、模型调用与工具批次。

覆盖 ``atri_x.agent.chat`` / ``chat_llm`` / ``chat_tools`` 的对外行为：
一次完整回合（含流式文本、工具调用、超时与空回答）、视觉兜底判定、
请求参数合并，以及工具 schema 装配。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from atri_x.config import ProviderConfig, ToolsConfig
from atri_x.delivery import Receipt
from atri_x.errors import BackgroundTakenOverError, PromptOverflowError
from atri_x.hook import Event, PointLLMRequestPrepared, PointLLMResponseReceived
from atri_x.hook.manager import NoopManager
from atri_x.llm import (
    LLMMessage,
    MessageSegment,
    RoleUser,
    StreamChunk,
    ToolCallDelta,
    ToolCallRequest,
    Usage,
    text_segments,
)
from atri_x.llm.errors import APIError, ErrorCategoryVisionUnsupported
from atri_x.platform import MessageContext, with_message_context
from atri_x.runtime import PhaseDone, PhaseError, PhaseLLM
from atri_x.safecontext import CancelToken
from atri_x.storage import Message, Session, SessionModeWork

from atri_x.agent import chat, chat_llm, chat_tools, chat_turn
from tests.test_agent_core import make_agent, make_ctx


class _ScriptedClient:
    """按脚本逐次返回响应块的假客户端。"""

    def __init__(self, scripts: list[list[StreamChunk]] | None = None) -> None:
        self.scripts = list(scripts or [])
        self.requests: list[object] = []

    async def chat_stream(self, ctx, req):
        self.requests.append(req)
        chunks = self.scripts.pop(0) if self.scripts else []

        async def _gen():
            for chunk in chunks:
                yield chunk

        return _gen()


class _ErrorClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def chat_stream(self, ctx, req):
        self.calls += 1
        raise self.error


class _Out:
    """记录输出的假 turnOutput。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.notices: list[tuple[int, str]] = []
        self.previews: list[str] = []
        self.reasoning: list[str] = []
        self.statuses: list[object] = []
        self.outputs: list[list[object]] = []

    def start_stream(self, ctx):
        return None

    async def finish_intermediate(self, ctx, stream_ctx, stream, text, streaming):
        return None

    async def replace_and_finish_stream(self, ctx, stream_ctx, stream, text):
        self.sent.append(text)
        return Receipt(platform_message_ids=["m1"])

    async def send_assistant(self, ctx, text):
        self.sent.append(text)
        return Receipt(platform_message_ids=["m1"])

    async def send_outputs(self, ctx, outputs):
        self.outputs.append(list(outputs or []))

    async def send_notice(self, ctx, level, text):
        self.notices.append((level, text))

    async def send_preview(self, ctx, text):
        self.previews.append(text)

    async def send_reasoning(self, ctx, text):
        self.reasoning.append(text)

    async def publish_runtime_status(self, ctx, snapshot):
        self.statuses.append(snapshot)


class _ContextRuntime:
    """最小上下文加载服务（避免依赖同僚模块）。"""

    def __init__(self, messages: list[Message] | None = None, summary=None) -> None:
        from atri_x.contextmgr import LoadedContext

        self.loaded = LoadedContext(messages=list(messages or []), summary=summary)

    async def load(self, ctx, session_id):
        return self.loaded


def _agent_with_client(client, **overrides):
    agent = make_agent(**overrides)
    agent.clients["default"] = client
    agent.sessions.title_gen = None  # 不排期标题生成，保持用例确定性
    agent.hooks = NoopManager()
    agent.services["context_runtime"] = _ContextRuntime()
    return agent


def _chunk(text: str = "", **rest) -> StreamChunk:
    return StreamChunk(delta_content=text, **rest)


class PlainTurnTest(unittest.TestCase):
    def test_plain_answer_is_sent_and_persisted(self) -> None:
        client = _ScriptedClient(
            [[_chunk("你好"), _chunk("，世界", usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3))]]
        )
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()

        asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(out.sent, ["你好，世界"])
        rows = agent.store.messages().rows
        self.assertEqual([row.role for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0].content, "hi")
        self.assertEqual(rows[1].content, "你好，世界")
        self.assertEqual(rows[1].session_id, session.id)
        # 运行状态：准备 → LLM → 发送 → 完成。
        self.assertEqual(out.statuses[-1].phase, PhaseDone)
        self.assertIn(PhaseLLM, [status.phase for status in out.statuses])
        # 客户端收到的请求带上了模型名与会话 id。
        self.assertEqual(client.requests[0].model, "m1")
        self.assertEqual(client.requests[0].session_id, session.id)

    def test_empty_answer_gets_fallback_text(self) -> None:
        client = _ScriptedClient([[]])
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()

        asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(out.sent, [chat.EMPTY_ASSISTANT_TEXT])
        # 空回答不写助手消息，只留用户消息。
        self.assertEqual([row.role for row in agent.store.messages().rows], ["user"])

    def test_prompt_overflow_ends_turn_quietly(self) -> None:
        agent = _agent_with_client(_ErrorClient(PromptOverflowError("too big")))
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()

        asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(out.sent, [])
        self.assertEqual(out.notices, [])

    def test_llm_error_publishes_error_status(self) -> None:
        agent = _agent_with_client(_ErrorClient(RuntimeError("boom")))
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()

        with self.assertRaises(RuntimeError):
            asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(out.statuses[-1].phase, PhaseError)
        self.assertIn("boom", out.statuses[-1].error)


class ToolRoundTest(unittest.TestCase):
    def test_tool_call_is_answered_then_model_summarises(self) -> None:
        tool_calls = [ToolCallDelta(id="c1", name="read_file", args='{"path": "a.txt"}')]
        client = _ScriptedClient(
            [
                [StreamChunk(delta_content="", tool_call_deltas=tool_calls)],
                [_chunk("读完了。")],
            ]
        )
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        executed: list[list[ToolCallRequest]] = []

        async def fake_execute(agent_arg, ctx, session_arg, calls, text, raw, out_arg, *, reasoning=""):
            from atri_x.toolrun import RunResult

            executed.append(list(calls))
            return RunResult(
                messages=[
                    LLMMessage(role="tool", tool_call_id="c1", segments=text_segments("内容"))
                ],
                prepared_calls=list(calls),
                transcript=[],
            )

        # 工具批次的真正调用点在 chat_turn（回合体所在模块）里绑定，替换那里的全局名。
        with mock.patch.object(chat_turn, "execute_tool_calls", fake_execute):
            asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0][0].name, "read_file")
        self.assertEqual(out.sent, ["读完了。"])
        # 第二轮请求必须带上第一轮的 assistant 工具调用与 tool 结果。
        second = client.requests[1].messages
        self.assertEqual(second[-2].tool_calls[0].id, "c1")
        self.assertEqual(second[-1].role, "tool")

    def test_chat_session_runs_tool_round(self) -> None:
        """本仓有意偏离上游：chat 会话同样注入 schema 并跑工具轮。"""
        from atri_x.llm import ToolFunctionSchema, ToolSchema
        from atri_x.session import CreateRequest
        from atri_x.storage import SessionModeChat

        tool_calls = [ToolCallDelta(id="c1", name="read_file", args='{"path": "a.txt"}')]
        client = _ScriptedClient(
            [
                [StreamChunk(delta_content="", tool_call_deltas=tool_calls)],
                [_chunk("读完了。")],
            ]
        )
        agent = _agent_with_client(client)

        class _Provider:
            async def schemas(self, ctx, mode, session, scope):
                return [ToolSchema(function=ToolFunctionSchema(name="read_file"))]

        agent.tool_runtime.provider = _Provider()
        agent.tool_runtime.default_provider = False
        session = agent.sessions.create(
            agent.scope(make_ctx()),
            CreateRequest(title="hi", mode=SessionModeChat),
        )
        out = _Out()
        executed: list[list[ToolCallRequest]] = []

        async def fake_execute(agent_arg, ctx, session_arg, calls, text, raw, out_arg, *, reasoning=""):
            from atri_x.toolrun import RunResult

            executed.append(list(calls))
            return RunResult(
                messages=[
                    LLMMessage(role="tool", tool_call_id="c1", segments=text_segments("内容"))
                ],
                prepared_calls=list(calls),
                transcript=[],
            )

        with mock.patch.object(chat_turn, "execute_tool_calls", fake_execute):
            asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual([call.name for call in executed[0]], ["read_file"])
        self.assertEqual([tool.function.name for tool in client.requests[0].tools], ["read_file"])
        self.assertEqual(out.sent, ["读完了。"])

    def test_reasoning_content_is_passed_back_in_tool_loop(self) -> None:
        """思考模式：带 tool_calls 的 assistant 消息必须把思维链一起回传。"""
        tool_calls = [ToolCallDelta(id="c1", name="read_file", args='{"path": "a.txt"}')]
        client = _ScriptedClient(
            [
                [
                    StreamChunk(
                        delta_reasoning_content="先看看文件",
                        tool_call_deltas=tool_calls,
                    )
                ],
                [_chunk("读完了。", delta_reasoning_content="收尾思考")],
            ]
        )
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        captured: dict[str, str] = {}

        async def fake_execute(
            agent_arg, ctx, session_arg, calls, text, raw, out_arg, *, reasoning=""
        ):
            from atri_x.toolrun import RunResult

            captured["reasoning"] = reasoning
            return RunResult(
                messages=[
                    LLMMessage(role="tool", tool_call_id="c1", segments=text_segments("内容"))
                ],
                prepared_calls=list(calls),
                transcript=[],
            )

        with mock.patch.object(chat_turn, "execute_tool_calls", fake_execute):
            asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        # 1) 思维链被交给工具批次落库（下一轮对话还要用）。
        self.assertEqual(captured["reasoning"], "先看看文件")
        # 2) 续轮请求里那条 assistant 消息带上了 reasoning_content。
        second = client.requests[1].messages
        assistant_with_tools = second[-2]
        self.assertEqual(assistant_with_tools.tool_calls[0].id, "c1")
        self.assertEqual(assistant_with_tools.reasoning_content, "先看看文件")
        # 3) 工具结果之后模型才给出最终回答。
        self.assertEqual(out.sent, ["读完了。"])
        # 4) 最终回答这一轮的思维链也落库（下一轮带 tools 的请求要回传）。
        from atri_x.agent.tool_transcript import AssistantMetadata

        answer_rows = [row for row in agent.store.messages().rows if row.role == "assistant"]
        metadata = AssistantMetadata.from_json(answer_rows[-1].metadata)
        self.assertEqual(metadata.reasoning_content, "收尾思考")

    def test_max_tool_rounds_produces_summary_request(self) -> None:
        tool_calls = [ToolCallDelta(id="c1", name="read_file", args="{}")]
        client = _ScriptedClient(
            [
                [StreamChunk(tool_call_deltas=tool_calls)],
                [StreamChunk(tool_call_deltas=tool_calls)],
                [_chunk("总结：什么都没做。")],
            ]
        )
        agent = _agent_with_client(client)
        agent.tool_runtime.config = ToolsConfig(max_rounds_per_turn=1)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()

        async def fake_execute(*args, **kwargs):
            from atri_x.toolrun import RunResult

            return RunResult(prepared_calls=list(args[3]), messages=[])

        with mock.patch.object(chat_turn, "execute_tool_calls", fake_execute):
            asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(out.sent, ["总结：什么都没做。"])
        self.assertTrue(any("max_rounds_per_turn=1" in text for text in out.previews))
        # 总结请求不再带工具 schema。
        self.assertEqual(client.requests[2].tools, [])


class BackgroundTakeoverTest(unittest.TestCase):
    def test_taken_over_turn_stops_before_sending(self) -> None:
        client = _ScriptedClient([[_chunk("迟到的回答")]])
        agent = _agent_with_client(client)
        agent.services["foreground"] = _ForegroundTakesOver(agent)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()

        from atri_x.agent.turn_output import BackgroundTurnOutput

        with self.assertRaises(BackgroundTakenOverError):
            asyncio.run(
                chat.start_chat_with_output(agent, make_ctx(), session, "hi", BackgroundTurnOutput(agent))
            )
        self.assertEqual(out.sent, [])


class _ForegroundTakesOver:
    def __init__(self, agent) -> None:
        self.agent = agent
        self.taken = False

    def background_session_must_stop(self, ctx, session_id: str) -> bool:
        return True


class ModelSelectionTest(unittest.TestCase):
    def test_turn_override_wins_over_mode_model(self) -> None:
        from atri_x.agent.turn_override import with_turn_model_override
        from atri_x.config import ModelSelection

        agent = _agent_with_client(_ScriptedClient())
        session = Session(id="s1", mode=SessionModeWork)
        base = chat.model_selection_for_turn(agent, make_ctx(), session)
        self.assertEqual(base.model, "m1")
        ctx = with_turn_model_override(make_ctx(), ModelSelection(provider="default", model="m9"))
        overridden = chat.model_selection_for_turn(agent, ctx, session)
        self.assertEqual(overridden.model, "m9")

    def test_cron_selection_is_honoured(self) -> None:
        from atri_x.delivery import ctx_with_value

        agent = _agent_with_client(_ScriptedClient())
        session = Session(id="s1", mode=SessionModeWork)
        ctx = ctx_with_value(
            make_ctx(), chat.CRON_MODEL_SELECTION_KEY, {"provider": "default", "model": "cron-1"}
        )
        self.assertEqual(chat.model_selection_for_turn(agent, ctx, session).model, "cron-1")


class LlmRequestOptionsTest(unittest.TestCase):
    def test_from_payload_trims_and_clones(self) -> None:
        from atri_x.hook import LLMPayload

        payload = LLMPayload(
            system_append=[" a ", "", "  "],
            temperature=0.5,
            max_tokens=128,
            extra_body={"k": "v"},
        )
        options = chat_llm.llm_request_options_from_payload(payload)
        self.assertEqual(options.system_append, ["a"])
        self.assertEqual(options.temperature, 0.5)
        self.assertEqual(options.max_tokens, 128)
        self.assertEqual(options.extra_body, {"k": "v"})

    def test_merge_overrides_and_appends(self) -> None:
        base = chat_llm.LLMRequestOptions(system_append=["a"], temperature=0.1, extra_body={"x": 1})
        overlay = chat_llm.LLMRequestOptions(system_append=["b"], temperature=0.9, extra_body={"y": 2})
        merged = chat_llm.merge_llm_request_options(base, overlay)
        self.assertEqual(merged.system_append, ["a", "b"])
        self.assertEqual(merged.temperature, 0.9)
        self.assertEqual(merged.extra_body, {"x": 1, "y": 2})
        self.assertEqual(merged.max_tokens, None)
        # 未覆盖时保留基值。
        self.assertEqual(chat_llm.merge_llm_request_options(base, None).system_append, ["a"])


class VisionFallbackTest(unittest.TestCase):
    def _messages_with_image(self) -> list[LLMMessage]:
        return [
            LLMMessage(
                role=RoleUser,
                segments=[MessageSegment(type="image", media="m1", url="http://x/1.png")],
            )
        ]

    def test_should_fallback_only_for_capability_errors(self) -> None:
        messages = self._messages_with_image()
        err = APIError(status_code=400, category=ErrorCategoryVisionUnsupported)
        self.assertTrue(chat_llm.should_fallback_vision(messages, err))
        self.assertFalse(
            chat_llm.should_fallback_vision(messages, APIError(status_code=500, category=ErrorCategoryVisionUnsupported))
        )
        self.assertFalse(chat_llm.should_fallback_vision(messages, APIError(status_code=400, category="rate_limit")))
        self.assertFalse(chat_llm.should_fallback_vision([LLMMessage(role=RoleUser, segments=text_segments("hi"))], err))
        self.assertFalse(chat_llm.should_fallback_vision(messages, RuntimeError("x")))

    def test_transparency_and_rewrite(self) -> None:
        self.assertTrue(chat_llm.vision_fallback_transparent("", 0, False))
        self.assertFalse(chat_llm.vision_fallback_transparent("已输出", 0, False))
        self.assertFalse(chat_llm.vision_fallback_transparent("", 1, False))
        self.assertFalse(chat_llm.vision_fallback_transparent("", 0, True))
        rewritten = chat_llm.fallback_vision_messages(self._messages_with_image())
        self.assertEqual(rewritten[0].segments[0].type, "text")
        self.assertTrue(rewritten[0].segments[0].text)

    def test_cli_reasoning_only_in_cli_context(self) -> None:
        agent = _agent_with_client(_ScriptedClient())
        self.assertTrue(chat_llm.should_show_cli_reasoning(agent, make_ctx()))
        other = with_message_context(None, MessageContext(platform="qq"))
        self.assertFalse(chat_llm.should_show_cli_reasoning(agent, other))

    def test_user_message_segments_uses_platform_segments(self) -> None:
        ctx = make_ctx(segments=[MessageSegment(type="text", text="来自平台")])
        segments = chat_llm.user_message_segments(ctx, "fallback")
        self.assertEqual(segments[0].text, "来自平台")
        self.assertEqual(chat_llm.user_message_segments(None, "fallback")[0].text, "fallback")


class CallLlmTest(unittest.TestCase):
    def _run(self, client, chunks_agent=None, **kwargs):
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        options = kwargs.pop("request_options", chat_llm.LLMRequestOptions())
        result = asyncio.run(
            chat_llm.call_llm(
                agent,
                CancelToken(),
                session.id,
                True,
                agent.model_for_mode(SessionModeWork),
                [LLMMessage(role=RoleUser, segments=text_segments("hi"))],
                [],
                None,
                options,
                None,
                out,
            )
        )
        return agent, out, result

    def test_stream_error_marks_user_notified(self) -> None:
        from atri_x.agent.errors import is_user_notified

        client = _ScriptedClient([[StreamChunk(error=RuntimeError("boom"))]])
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        with self.assertRaises(BaseException) as caught:
            asyncio.run(
                chat_llm.call_llm(
                    agent,
                    CancelToken(),
                    session.id,
                    True,
                    agent.model_for_mode(SessionModeWork),
                    [LLMMessage(role=RoleUser, segments=text_segments("hi"))],
                    [],
                    None,
                    chat_llm.LLMRequestOptions(),
                    None,
                    out,
                )
            )
        self.assertTrue(is_user_notified(caught.exception))
        self.assertTrue(out.notices)
        self.assertEqual(out.notices[0][0], chat_llm.LEVEL_ERROR)

    def test_response_hook_can_rewrite_text_and_tool_calls(self) -> None:
        class _Hooks(NoopManager):
            def __init__(self) -> None:
                self.seen: list[str] = []

            async def run(self, ctx, event: Event) -> Event:
                self.seen.append(event.point)
                if event.point == PointLLMResponseReceived:
                    event.llm.text = "改写后的回答"
                return event

        hooks = _Hooks()
        client = _ScriptedClient([[_chunk("原始回答", tool_call_deltas=[ToolCallDelta(id="c1", name="t")])]])
        agent = _agent_with_client(client)
        agent.hooks = hooks
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        result = asyncio.run(
            chat_llm.call_llm(
                agent,
                CancelToken(),
                session.id,
                True,
                agent.model_for_mode(SessionModeWork),
                [LLMMessage(role=RoleUser, segments=text_segments("hi"))],
                [],
                None,
                chat_llm.LLMRequestOptions(),
                None,
                out,
            )
        )
        self.assertIn(PointLLMRequestPrepared, hooks.seen)
        self.assertIn(PointLLMResponseReceived, hooks.seen)
        self.assertEqual(result.text, "改写后的回答")
        self.assertEqual(result.raw_text, "原始回答")
        self.assertEqual([call.id for call in result.tool_calls], ["c1"])

    def test_tools_disabled_flag_strips_tool_calls(self) -> None:
        # call_llm 的 tools_enabled（第 4 个参数）为假时不把工具带回结果——与模式无关。
        client = _ScriptedClient([[_chunk("x", tool_call_deltas=[ToolCallDelta(id="c1", name="t")])]])
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        result = asyncio.run(
            chat_llm.call_llm(
                agent,
                CancelToken(),
                session.id,
                False,
                agent.model_for_mode(SessionModeWork),
                [LLMMessage(role=RoleUser, segments=text_segments("hi"))],
                [],
                None,
                chat_llm.LLMRequestOptions(),
                None,
                out,
            )
        )
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(client.requests[0].tools, [])

    def test_cancelled_context_returns_accumulated_text(self) -> None:
        token = CancelToken()

        class _CancelMidStream:
            """第一块正常产出后取消 ctx，第二块带错误。"""

            async def chat_stream(self, ctx, req):
                async def _gen():
                    yield _chunk("部分")
                    token.cancel(RuntimeError("canceled"))
                    yield StreamChunk(error=RuntimeError("canceled"))

                return _gen()

        agent = _agent_with_client(_CancelMidStream())
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _Out()
        result = asyncio.run(
            chat_llm.call_llm(
                agent,
                token,
                session.id,
                True,
                agent.model_for_mode(SessionModeWork),
                [LLMMessage(role=RoleUser, segments=text_segments("hi"))],
                [],
                None,
                chat_llm.LLMRequestOptions(),
                None,
                out,
            )
        )
        # 取消不算失败：带着已累积的文本正常返回，也不发错误通知。
        self.assertEqual(result.text, "部分")
        self.assertEqual(out.notices, [])


class ToolsForSessionTest(unittest.TestCase):
    def test_no_tool_manager_means_no_schemas(self) -> None:
        agent = _agent_with_client(_ScriptedClient())
        session = Session(id="s1", mode=SessionModeWork)
        self.assertEqual(asyncio.run(chat_tools.tools_for_session(agent, make_ctx(), session)), [])

    def test_chat_session_without_tool_manager_has_no_schemas(self) -> None:
        # 没有工具运行时（registry/provider 都为空）时，chat 会话也拿不到 schema；
        # "chat 模式本身是否有工具"由 test_chat_session_runs_tool_round 覆盖。
        agent = _agent_with_client(_ScriptedClient())
        session = Session(id="s1", mode="chat")
        self.assertEqual(asyncio.run(chat_tools.tools_for_session(agent, make_ctx(), session)), [])

    def test_group_allowlist_filters_schemas(self) -> None:
        from atri_x.config import GroupPolicyConfig
        from atri_x.llm import ToolFunctionSchema, ToolSchema

        class _Provider:
            async def schemas(self, ctx, mode, session, scope):
                return [
                    ToolSchema(function=ToolFunctionSchema(name="read_file")),
                    ToolSchema(function=ToolFunctionSchema(name="write_file")),
                ]

        agent = _agent_with_client(_ScriptedClient())
        agent.tool_runtime.provider = _Provider()
        agent.tool_runtime.default_provider = False
        # 群白名单来自群策略本身：Go 的 ``groupToolAllowlist`` 直接读
        # ``groupPolicyForScope``，不查 ``Deps.GroupPolicy``。
        agent.group_policy["cli:group:42"] = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        )
        session = Session(id="s1", mode=SessionModeWork)
        schemas = asyncio.run(
            chat_tools.tools_for_session(agent, make_ctx(scope_id="group:42"), session)
        )
        self.assertEqual([schema.function.name for schema in schemas], ["read_file"])


class ModelCapabilitiesTest(unittest.TestCase):
    def test_explicit_unsupported_vision_marks_context(self) -> None:
        from atri_x.tool import capabilities_from_context

        provider = ProviderConfig(vision=False)
        agent = _agent_with_client(_ScriptedClient(), providers={"default": provider})
        ctx = chat_tools.with_model_capabilities(agent, make_ctx(), "default", "m1")
        capabilities, ok = capabilities_from_context(ctx)
        self.assertTrue(ok)
        self.assertFalse(capabilities.vision)

    def test_unknown_provider_keeps_context(self) -> None:
        agent = _agent_with_client(_ScriptedClient())
        ctx = make_ctx()
        self.assertIs(chat_tools.with_model_capabilities(agent, ctx, "nope", "m1"), ctx)


class InboxIdleTest(unittest.TestCase):
    def test_wait_inbox_idle_returns_when_idle(self) -> None:
        agent = _agent_with_client(_ScriptedClient())
        asyncio.run(agent.wait_inbox_idle(make_ctx(), "missing-session"))

    def test_inbox_session_falls_back_to_store(self) -> None:
        agent = _agent_with_client(_ScriptedClient())
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        agent.sessions.clear_current_if(agent.scope(make_ctx()), session.id)
        found = asyncio.run(agent.inbox_session(make_ctx(), session.id))
        self.assertEqual(found.id, session.id)


class _RecordingStream:
    """记录流式增量写入的假 ``MessageStream``（三个方法都是 async，与契约一致）。"""

    def __init__(self) -> None:
        self.appends: list[str] = []
        self.replaced: list[str] = []
        self.finished = False

    async def append(self, ctx, value):
        self.appends.append(value)

    async def replace(self, ctx, value):
        self.replaced.append(value)
        return Receipt(platform_message_ids=["m1"])

    async def finish(self, ctx):
        self.finished = True
        return Receipt(platform_message_ids=["m1"])


class _StreamOut(_Out):
    """``start_stream`` 返回真实 ``MessageStream`` 的假 turnOutput（用于流式路径）。"""

    def __init__(self) -> None:
        super().__init__()
        self.stream = _RecordingStream()

    def start_stream(self, ctx):
        return self.stream


class StreamAppendAwaitTest(unittest.TestCase):
    """流式增量必须真的写进 ``MessageStream``。

    真实事故（第二轮对抗性验证后复查发现）：``chat_llm`` 照抄 Go 的
    ``stream.Append(ctx, delta)`` 同步形状，而本仓 ``MessageStream.append`` 是
    ``async``（Python 侧 I/O 走 asyncio）—— 协程被静默丢弃，CLI/TUI/Telegram 的
    流式增量永远不显示，只剩终态 ``replace``，而且不抛任何异常。
    这条用例走真实 chat 路径，是唯一能抓住它的形状。
    """

    def test_deltas_reach_the_stream(self) -> None:
        client = _ScriptedClient(
            [
                [
                    _chunk("你好"),
                    _chunk(
                        "，世界",
                        usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
                    ),
                ]
            ]
        )
        agent = _agent_with_client(client)
        session = agent.sessions.get_or_create_current(agent.scope(make_ctx()), "hi")
        out = _StreamOut()

        asyncio.run(chat.start_chat_with_output(agent, make_ctx(), session, "hi", out))

        self.assertEqual(out.stream.appends, ["你好", "，世界"])
        # 流式增量与终态投递不冲突：终态走 replace（或退回整条发送）。
        self.assertTrue(out.stream.replaced or out.sent)


class ReactionTurnTest(unittest.TestCase):
    """表情意图标记在真实回合路径上的行为。

    重点不是选图算法（那在 ``test_reaction.py``），而是：

    1. 标记**绝不能**出现在平台正文/落库内容里；
    2. 表情图片作为 ``after_assistant`` 的 deferred output 交付；
    3. 功能关闭时只剥标记、不发图。
    """

    def setUp(self) -> None:
        import json
        import os
        import shutil

        self.tmp = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tmp_agent_chat_reaction_test",
        )
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "assets"), exist_ok=True)
        with open(os.path.join(self.tmp, "assets", "a.png"), "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        with open(os.path.join(self.tmp, "catalog.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "items": [
                        {"id": "a", "emotion": "无语", "path": "assets/a.png"}
                    ],
                },
                handle,
                ensure_ascii=False,
            )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _service(self):
        from atri_x.agent.reactions import build_reaction_service
        from atri_x.config import ReactionsConfig

        return build_reaction_service(
            ReactionsConfig(
                enabled=True,
                gallery_dir=self.tmp,
                cooldown_text_replies=0,
                session_cooldown_seconds=0,
                group_cooldown_seconds=0,
                dedupe_recent=0,
                max_text_chars=0,
                suppress_keywords=[],
            )
        )

    def _run(self, text: str, *, enable_reaction: bool):
        client = _ScriptedClient([[_chunk(text)]])
        agent = _agent_with_client(client)
        if enable_reaction:
            agent.reaction_service = self._service()
        ctx = make_ctx(platform="qqonebot", scope_id="group:123")
        session = agent.sessions.get_or_create_current(agent.scope(ctx), "hi")
        out = _Out()
        asyncio.run(chat.start_chat_with_output(agent, ctx, session, "hi", out))
        return agent, out

    def test_marker_stripped_and_image_deferred(self) -> None:
        agent, out = self._run(
            "这个需求已经重写整个项目了吧！[[emo:无语]]", enable_reaction=True
        )
        self.assertEqual(out.sent, ["这个需求已经重写整个项目了吧！"])
        images = [
            output
            for batch in out.outputs
            for output in batch
            if getattr(output, "kind", "") == "image"
        ]
        self.assertEqual(1, len(images))
        from atri_x.delivery import delivery_timing

        self.assertEqual("after_assistant", delivery_timing(images[0]))
        rows = agent.store.messages().rows
        self.assertEqual("这个需求已经重写整个项目了吧！", rows[-1].content)
        self.assertNotIn("[[emo", rows[-1].content)

    def test_marker_stripped_without_image_when_disabled(self) -> None:
        _agent, out = self._run("好耶[[emo:得意]]", enable_reaction=False)
        self.assertEqual(out.sent, ["好耶"])
        self.assertEqual([], [item for batch in out.outputs for item in batch])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
