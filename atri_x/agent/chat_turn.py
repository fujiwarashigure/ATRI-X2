"""一轮对话的主体循环。

对应 Go：``internal/agent/chat.go`` 里 ``Agent.runChat`` 从拿到请求槽位之后到
助手消息落库的全部逻辑（含工具轮次、轮次上限总结、接管检查与最终发送）。
拆出来的原因是单文件行数上限；``chat.py`` 在文件末尾再导出，导入路径不变。
"""

from __future__ import annotations

import asyncio
from typing import Any
from atri_x.agent.turn_output import (
    BackgroundTurnOutput,
    TurnOutput,
    buffer_assistant_output,
)
from atri_x.hook import (
    Event,
    LLMPayload,
    MessagePayload,
    PointAgentTurnOutputPrepared,
    PointLLMTurnPrepared,
    SessionContext,
)
from atri_x.request import KindTurn
from atri_x.llm import (
    LLMMessage,
    RoleAssistant,
    RoleUser,
    clone_messages,
    prepend_segment_text,
    segments_content_text,
    set_latest_user_segments,
    text_segments,
)
from atri_x.storage import Message, RoleAssistant as StorageRoleAssistant, now
from atri_x.runtime import PhaseLLM, PhasePreparing, PhaseSending, Snapshot
from atri_x.delivery import Receipt, ctx_error, split_by_delivery_timing
from atri_x.logging import ResultFailed
from atri_x.turn import Input as TurnInput
from atri_x.agent.chat_llm import call_llm, llm_request_options_from_payload
from dataclasses import dataclass
from atri_x.agent.chat_tools import (
    drain_pending_user_input,
    execute_tool_calls,
    join_assistant_text,
    max_tool_rounds_per_turn,
    session_tools_enabled,
    skipped_tool_messages,
    tools_for_session,
    with_model_capabilities,
)
from atri_x.agent.segments import materialize_media
from atri_x.agent.status import runtime_done_status
# 这五个模块在 ``atri_x/agent/core.py`` 里已经是模块级硬导入，原来包在这里的
# ``except ImportError`` 兜底永远不可达，只会把"模块坏了"变成静默降级（研究报告 §5.1）。
from atri_x.agent.context_runtime import ensure_context_runtime
from atri_x.agent.prompt import PromptBuildRequest
from atri_x.agent.tool_cache import mutate_session_metadata
from atri_x.agent.tool_transcript import (
    assistant_raw_text_metadata,
    stored_message_segments,
)

from atri_x.agent.chat import (
    EMPTY_ASSISTANT_TEXT,
    _is_prompt_overflow,
    _taken_over,
    audit_partial_platform_send,
    consume_context_compact_seed,
    handle_turn_context_done,
    model_selection_for_turn,
    summary_user_prefix,
)


async def _run_chat_body(
    agent: Any,
    ctx: Any,
    req_ctx: Any,
    info: Any,
    session: Any,
    user_message: Message,
    inbound: TurnInput,
    user_segments: list[Any],
    messages: list[Message],
    loaded: Any,
    selection: Any,
    out: TurnOutput,
    compact_seed_on_current_user: bool,
    summary_on_current_user: bool,
) -> TurnInput:
    """``runChat`` 里取得请求槽位之后的主体。"""
    turn_started_at = now()
    await out.publish_runtime_status(
        ctx,
        Snapshot(
            session_id=session.id,
            phase=PhasePreparing,
            provider=selection.provider,
            model=selection.model,
            mode=session.mode,
            turn_started_at=turn_started_at,
            stage_started_at=turn_started_at,
        ),
    )
    scope = agent.scope(ctx)
    llm_messages = await agent.build_prompt(
        ctx,
        _prompt_build_request(agent, ctx, session, scope, messages, loaded.summary),
    )
    llm_messages = list(llm_messages or [])
    tools = await tools_for_session(agent, ctx, session)
    try:
        turn_event = await agent.run_hook(
            ctx,
            Event(
                point=PointLLMTurnPrepared,
                session=SessionContext(id=session.id),
                message=MessagePayload(
                    id=user_message.id,
                    role=RoleUser,
                    platform_text=inbound.platform_text,
                    segments=list(user_segments),
                ),
                llm=LLMPayload(
                    provider=selection.provider,
                    model=selection.model,
                    messages=clone_messages(llm_messages),
                    tools=tools,
                ),
            ),
        )
    except asyncio.CancelledError:
        # 取消不是"钩子失败"：转成 RuntimeError 会让上层把它当业务错误上报。
        raise
    except BaseException as exc:  # noqa: BLE001 - 钩子失败即本轮失败
        raise RuntimeError(f"llm turn hook: {exc}") from exc
    selection.provider = turn_event.llm.provider
    selection.model = turn_event.llm.model
    agent.authorize_execution_model_selection(ctx, selection)
    # turn hook 可能换了模型，工具能力要跟着真正会被调用的模型走。
    ctx = with_model_capabilities(agent, ctx, selection.provider, selection.model)
    # chat 会话不吃工具 schema，包括 turn hook 硬塞进来的。
    tools = list(turn_event.llm.tools or []) if session_tools_enabled(session) else []
    request_options = llm_request_options_from_payload(turn_event.llm)
    await out.publish_runtime_status(
        ctx,
        Snapshot(
            session_id=session.id,
            phase=PhasePreparing,
            provider=selection.provider,
            model=selection.model,
            mode=session.mode,
            turn_started_at=turn_started_at,
            stage_started_at=turn_started_at,
        ),
    )
    canonical_user_segments = materialize_media(
        agent, ctx, list(turn_event.message.segments or [])
    )
    prompt_user_segments = canonical_user_segments
    if compact_seed_on_current_user or summary_on_current_user:
        prompt_user_segments = prepend_segment_text(
            prompt_user_segments, summary_user_prefix(loaded.summary.summary)
        )
    llm_messages = set_latest_user_segments(llm_messages, prompt_user_segments)
    continued, overflow_adjusted = await agent.handle_initial_prompt_overflow(
        ctx, session, selection, llm_messages, tools, out
    )
    if not continued:
        return TurnInput()
    if overflow_adjusted:
        prompt_user_segments = llm_messages[-1].segments
    if compact_seed_on_current_user or overflow_adjusted:
        user_message.content = segments_content_text(prompt_user_segments)
        user_message.segments = _stored_segments(prompt_user_segments)
    else:
        user_message.content = segments_content_text(canonical_user_segments)
        user_message.segments = _stored_segments(canonical_user_segments)
    await agent.persist_turn_message(ctx, user_message, "append_user_message")
    if compact_seed_on_current_user:
        await consume_context_compact_seed(agent, ctx, session)

    buffer_output = buffer_assistant_output(ctx)
    is_background_output = isinstance(out, BackgroundTurnOutput)
    final_text = ""
    final_raw_text = ""
    platform_final_text = ""
    final_stream = None
    final_receipt = Receipt()
    deferred_outputs: list[Any] = []
    usage = None
    tool_rounds = 0
    in_tool_phase = False
    while True:
        if is_background_output and agent.background_session_must_stop(ctx, session.id):
            raise _taken_over()
        pending = None
        if in_tool_phase:
            llm_messages, pending = drain_pending_user_input(
                agent, ctx, session.id, llm_messages
            )
        stream = out.start_stream(req_ctx)
        llm_stage_started_at = now()
        await out.publish_runtime_status(
            ctx,
            Snapshot(
                session_id=session.id,
                phase=PhaseLLM,
                provider=selection.provider,
                model=selection.model,
                mode=session.mode,
                request_id=info.id,
                kind=KindTurn,
                label="chat",
                turn_started_at=turn_started_at,
                stage_started_at=llm_stage_started_at,
                usage=usage,
            ),
        )
        try:
            result = await call_llm(
                agent,
                req_ctx,
                session.id,
                session_tools_enabled(session),
                selection,
                llm_messages,
                tools,
                pending,
                request_options,
                stream,
                out,
                # 只有一轮的第一次调用允许交给 DSH：工具轮次与视觉重试不再换后端。
                allow_route=tool_rounds == 0 and not in_tool_phase and pending is None,
            )
        except BaseException as exc:  # noqa: BLE001 - 溢出是"正常结束这一轮"
            if _is_prompt_overflow(exc):
                return TurnInput()
            raise
        if result.messages:
            llm_messages = result.messages
        streaming = result.stream is not None
        req_error = ctx_error(req_ctx)
        if req_error is not None:
            await handle_turn_context_done(agent, ctx, session.id, req_error, out)
            return TurnInput()
        assistant_text = result.text
        assistant_raw_text = result.raw_text
        if result.usage is not None:
            usage = result.usage
        await out.publish_runtime_status(
            ctx,
            Snapshot(
                session_id=session.id,
                phase=PhaseLLM,
                provider=selection.provider,
                model=selection.model,
                mode=session.mode,
                request_id=info.id,
                kind=KindTurn,
                label="chat",
                turn_started_at=turn_started_at,
                stage_started_at=llm_stage_started_at,
                usage=usage,
            ),
        )
        immediate_outputs, later_outputs = split_by_delivery_timing(result.outputs)
        if not result.tool_calls:
            deferred_outputs.extend(later_outputs)
        await out.send_outputs(req_ctx, immediate_outputs)
        if not result.tool_calls:
            final_text = join_assistant_text(final_text, assistant_raw_text)
            final_raw_text = join_assistant_text(final_raw_text, assistant_raw_text)
            platform_final_text = join_assistant_text(platform_final_text, assistant_text)
            final_stream = result.stream
            break
        await out.finish_intermediate(ctx, req_ctx, result.stream, assistant_text, streaming)
        await out.send_outputs(req_ctx, later_outputs)
        if not in_tool_phase:
            if not agent.turns.start_tool_phase(session.id):
                return TurnInput()
            in_tool_phase = True
        assistant_tool_call_index = len(llm_messages)
        llm_messages.append(
            LLMMessage(
                role=RoleAssistant,
                segments=text_segments(assistant_raw_text),
                tool_calls=list(result.tool_calls),
            )
        )
        max_rounds = max_tool_rounds_per_turn(agent)
        if tool_rounds >= max_rounds:
            await out.send_preview(
                req_ctx,
                f"已达到 max_rounds_per_turn={max_rounds}，后续工具调用未执行，"
                "正在请求模型总结当前进度。",
            )
            llm_messages.extend(skipped_tool_messages(result.tool_calls, max_rounds))
            llm_messages, _summary_pending = drain_pending_user_input(
                agent, ctx, session.id, llm_messages
            )
            llm_messages.append(
                LLMMessage(
                    role=RoleUser,
                    segments=text_segments(
                        "工具调用轮次已达到上限，可以询问用户是否继续或者基于已有工具结果"
                        "和当前上下文总结当前进度。"
                    ),
                )
            )
            tools = []
            summary_stream = out.start_stream(req_ctx)
            try:
                summary = await call_llm(
                    agent,
                    req_ctx,
                    session.id,
                    session_tools_enabled(session),
                    selection,
                    llm_messages,
                    tools,
                    _summary_pending,
                    request_options,
                    summary_stream,
                    out,
                )
            except BaseException as exc:  # noqa: BLE001
                if _is_prompt_overflow(exc):
                    return TurnInput()
                raise
            if summary.tool_calls:
                # 总结请求已经不传 tools；仍返回工具调用就忽略。
                await out.send_preview(req_ctx, "总结请求仍返回了工具调用，已忽略。")
            immediate_outputs, later_outputs = split_by_delivery_timing(summary.outputs)
            deferred_outputs.extend(later_outputs)
            await out.send_outputs(req_ctx, immediate_outputs)
            summary_text = summary.text
            summary_raw_text = summary.raw_text
            if not summary_text:
                summary_text = "工具调用轮次已达到上限，当前流程已停止。"
                if not summary_raw_text:
                    summary_raw_text = summary_text
            if summary.usage is not None:
                usage = summary.usage
            final_text = join_assistant_text(final_text, summary_raw_text)
            final_raw_text = join_assistant_text(final_raw_text, summary_raw_text)
            platform_final_text = join_assistant_text(platform_final_text, summary_text)
            final_stream = summary.stream
            break
        tool_rounds += 1
        execution = await execute_tool_calls(
            agent, req_ctx, session, result.tool_calls, assistant_raw_text, assistant_raw_text, out
        )
        if execution.stopped:
            req_error = ctx_error(req_ctx)
            if req_error is not None:
                await handle_turn_context_done(agent, ctx, session.id, req_error, out)
            return TurnInput()
        llm_messages[assistant_tool_call_index].tool_calls = list(execution.prepared_calls)
        llm_messages.extend(execution.messages)
        await agent.persist_turn_messages(
            ctx, session.id, "append_tool_transcript", execution.transcript
        )
        if is_background_output and agent.background_session_must_stop(ctx, session.id):
            # 工具批次是安全的停止点：这一批已经跑完并写入转录。
            raise _taken_over()
        tools = await tools_for_session(agent, ctx, session)
        if execution.confirmation_extra:
            llm_messages.append(
                LLMMessage(
                    role=RoleUser,
                    segments=text_segments("补充：" + execution.confirmation_extra),
                )
            )

    if is_background_output and agent.background_session_must_stop(ctx, session.id):
        raise _taken_over()
    platform_output_text = platform_final_text
    empty_assistant_response = (
        not platform_output_text.strip()
        and not final_text.strip()
        and not deferred_outputs
    )
    if empty_assistant_response and not is_background_output:
        platform_output_text = EMPTY_ASSISTANT_TEXT
    await out.publish_runtime_status(
        ctx,
        Snapshot(
            session_id=session.id,
            phase=PhaseSending,
            provider=selection.provider,
            model=selection.model,
            mode=session.mode,
            request_id=info.id,
            kind=KindTurn,
            label="chat",
            turn_started_at=turn_started_at,
            stage_started_at=now(),
        ),
    )
    if platform_output_text.strip():
        try:
            platform_output_text = await agent.prepare_assistant_output(
                ctx, PointAgentTurnOutputPrepared, platform_output_text
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 钩子失败即本轮失败
            raise RuntimeError(f"turn output hook: {exc}") from exc
        if not buffer_output:
            if final_stream is not None:
                try:
                    final_receipt = await out.replace_and_finish_stream(
                        ctx, req_ctx, final_stream, platform_output_text
                    )
                except BaseException as exc:  # noqa: BLE001
                    audit_partial_platform_send(
                        agent, session.id, "replace_and_finish_stream", _receipt_of(exc), exc
                    )
                    raise
            else:
                try:
                    final_receipt = await out.send_assistant(req_ctx, platform_output_text)
                except BaseException as exc:  # noqa: BLE001
                    audit_partial_platform_send(
                        agent, session.id, "send_assistant_message", _receipt_of(exc), exc
                    )
                    raise
    if not buffer_output:
        await out.send_outputs(req_ctx, deferred_outputs)
    # 输出 hook 之后仍要再确认一次接管：终态文本已经在内存里，也不能写进前台会话。
    if is_background_output and agent.background_session_must_stop(ctx, session.id):
        raise _taken_over()
    assistant_message = Message(
        session_id=session.id,
        role=StorageRoleAssistant,
        content=final_text,
        metadata=_assistant_metadata(final_text, final_raw_text),
    )
    pending, completed = agent.turns.complete_llm_input(session.id)
    if not completed:
        return TurnInput()
    persisted_assistant = False
    if not empty_assistant_response:
        await agent.persist_turn_message(ctx, assistant_message, "append_assistant_message")
        persisted_assistant = agent.history_enabled(ctx)
    if buffer_output:
        if platform_output_text.strip():
            try:
                receipt = await out.send_assistant(req_ctx, platform_output_text)
            except BaseException as exc:  # noqa: BLE001
                if persisted_assistant:
                    agent.map_sent_assistant_message(
                        ctx, session.id, assistant_message.id, _receipt_of(exc)
                    )
                audit_partial_platform_send(
                    agent, session.id, "send_assistant_message", _receipt_of(exc), exc
                )
                agent.audit(
                    "platform_send_error",
                    "session_id",
                    session.id,
                    "operation",
                    "send_assistant_message",
                    "error",
                    str(exc),
                    "result",
                    ResultFailed,
                )
                raise
            if persisted_assistant:
                agent.map_sent_assistant_message(
                    ctx, session.id, assistant_message.id, receipt
                )
        await out.send_outputs(req_ctx, deferred_outputs)
    elif persisted_assistant:
        agent.map_sent_assistant_message(
            ctx, session.id, assistant_message.id, final_receipt
        )
    try:
        agent.sessions.touch(session)
    except BaseException as exc:  # noqa: BLE001 - 触碰失败必须冒泡
        agent.audit(
            "persistence_error",
            "session_id",
            session.id,
            "operation",
            "touch_session",
            "error",
            str(exc),
            "result",
            ResultFailed,
        )
        raise
    agent.record_usage(session.id, usage)
    agent.record_llm_origin(ctx, session, selection)
    done_status = runtime_done_status(
        Snapshot(
            session_id=session.id,
            provider=selection.provider,
            model=selection.model,
            mode=session.mode,
            turn_started_at=turn_started_at,
            stage_started_at=turn_started_at,
            usage=usage,
        ),
        now(),
    )
    await out.publish_runtime_status(ctx, done_status)
    next_selection = model_selection_for_turn(agent, ctx, session)
    if agent.should_compact(ctx, session, next_selection):
        await out.send_assistant(ctx, "compact status: will compact before next request")
    if agent.history_enabled(ctx):
        agent.sessions.maybe_schedule_naming(session.id)
    return pending


async def _load_context(agent: Any, ctx: Any, session_id: str) -> Any:
    """取会话上下文（摘要 + 历史）。对应 Go ``a.contextRuntime.load``。"""
    service = agent.services.get("context_runtime") if isinstance(agent.services, dict) else None
    if service is not None and hasattr(service, "load"):
        result = service.load(ctx, session_id)
        if hasattr(result, "__await__"):
            result = await result
        return result
    return await ensure_context_runtime(agent).load(ctx, session_id)


def _prompt_build_request(
    agent: Any, ctx: Any, session: Any, scope: Any, messages: list[Message], summary: Any
) -> Any:
    """构造 ``PromptBuildRequest``。对应 Go ``PromptBuildRequest`` 字面量。"""
    return PromptBuildRequest(
        session=session,
        scope=scope,
        meta=agent.conversation_meta(ctx, scope),
        messages=messages,
        summary=summary,
    )


def _receipt_of(err: BaseException) -> Any:
    """从发送异常里取部分回执（``DeliveryError.receipt``）。"""
    receipt = getattr(err, "receipt", None)
    return receipt if receipt is not None else Receipt()


def _stored_segments(segments: list[Any]) -> str:
    """把分段序列化成落库形态。对应 Go ``storedMessageSegments``。"""
    return stored_message_segments(segments)


def _assistant_metadata(content: str, raw_text: str) -> str:
    """助手消息的 metadata（含原始文本）。"""
    return assistant_raw_text_metadata(content, raw_text)


async def _mutate_metadata(
    agent: Any, ctx: Any, session: Any, mutate: Any
) -> None:
    """改会话 metadata。对应 Go ``mutateSessionMetadata``。"""
    await mutate_session_metadata(agent, ctx, session, mutate)
