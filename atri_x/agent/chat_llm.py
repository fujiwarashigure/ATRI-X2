"""单次 LLM 调用：请求构造、流式读取、用量与错误审计。

对应 Go：``internal/agent/chat_llm.go``。其中 ``llmRequestOptions`` /
``llmRequestOptionsFromPayload`` / ``mergeLLMRequestOptions`` 在 Go 里就在单独的
``internal/agent/llm_request_options.go``；Python 版把它们放在
:mod:`atri_x.agent.chat_request_options`（本模块末尾重新导出，导入路径不变）。

Go 的 ``(llmCallResult, error)`` 在 Python 里是"返回 :class:`LLMCallResult`，
失败抛异常"。两个刻意的例外与 Go 一致：``ctx`` 被取消/超时时**不抛**，而是带着
已累积的文本正常返回，让上层走"本轮已超时停止"的分支。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from atri_x.delivery import MessageStream, Output, ctx_error, ctx_with_value
from atri_x.hook import (
    Event,
    LLMPayload,
    MessagePayload,
    PointLLMRequestPrepared,
    PointLLMResponseReceived,
    SessionContext,
)
from atri_x.llm import (
    ChatRequest,
    LLMMessage,
    RoleUser,
    ToolCallRequest,
    ToolSchema,
    Usage,
    append_system_segment_text,
    clone_messages,
    segments_content_text,
)
from atri_x.logging import ResultFailed
from atri_x.storage import new_id

from atri_x.agent.errors import is_cancellation, mark_user_notified, new_user_error_details
from atri_x.agent.segments import (
    file_segment_text,
    materialize_media,
    platform_segments_to_llm,
)
from atri_x.agent.tool_transcript import stored_message_segments

__all__ = [
    "LEVEL_ERROR",
    "LEVEL_WARN",
    "LLMCallResult",
    "LLMRequestOptions",
    "VISION_FALLBACK_BLOCKED_NOTICE",
    "audit_usage",
    "call_llm",
    "elapsed_millis",
    "fallback_vision_messages",
    "file_segment_text",
    "is_cli_context",
    "llm_request_options_from_payload",
    "log_llm_output",
    "merge_llm_request_options",
    "platform_segments_to_llm",
    "send_cli_reasoning",
    "should_fallback_vision",
    "should_show_cli_reasoning",
    "user_message_segments",
    "vision_fallback_transparent",
]

#: ``delivery.Notice.level`` 用 int，对齐 Go 的 ``slog.Level``（Warn=4 / Error=8）。
LEVEL_WARN = 4
LEVEL_ERROR = 8


@dataclass
class LLMCallResult:
    """一次模型调用的结果。对应 Go ``llmCallResult``。"""

    text: str = ""
    raw_text: str = ""
    usage: Usage | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    outputs: list[Output] = field(default_factory=list)
    messages: list[LLMMessage] = field(default_factory=list)
    stream: MessageStream | None = None


async def _route_to_dsh(
    agent: Any,
    ctx: Any,
    session_id: str,
    selection: Any,
    messages: list[LLMMessage],
    stream: MessageStream | None,
    out: Any,
    *,
    max_output: int = 0,
) -> LLMCallResult | None:
    """尝试 DSH 深度路由；不可用/不划算/失败都返回 ``None``（继续原生链路）。"""
    try:
        from atri_x.agent.dsh.route_call import maybe_route_to_dsh
    except Exception:  # noqa: BLE001 - DSH 模块缺失（裁剪发行版）时静默跳过
        return None
    return await maybe_route_to_dsh(
        agent, ctx, session_id, selection, messages, stream, out, max_output=max_output
    )


async def call_llm(
    agent: Any,
    ctx: Any,
    session_id: str,
    tools_enabled: bool,
    selection: Any,
    messages: list[LLMMessage],
    tools: list[ToolSchema] | None,
    pending: Any,
    request_options: LLMRequestOptions | None,
    stream: MessageStream | None,
    out: Any,
    *,
    allow_route: bool = False,
) -> LLMCallResult:
    """调用一次模型（含请求/响应 hook、预算、限流与视觉兜底）。

    对应 Go ``Agent.callLLM``。``selection`` 在 Go 里按值传递，这里先复制，
    保证 hook 改的模型选择不会泄漏回调用方。

    ``allow_route`` 是 Python 版新增：**一轮对话的第一次调用**才允许把任务交给
    内置 DSH 内核（见 :mod:`atri_x.agent.dsh.route_call`）。工具阶段的多轮调用与
    视觉重试不允许再次路由，避免同一轮被两个后端交替接管。
    """
    started_at = time.monotonic()
    base_messages = list(clone_messages(messages) or [])
    selection = replace(selection)
    hook_message = MessagePayload()
    if pending is not None:
        index = int(getattr(pending, "message_index", 0) or 0)
        segments = _segments_at(base_messages, index)
        hook_message = MessagePayload(
            id=getattr(pending.message, "id", ""),
            role=RoleUser,
            platform_text=getattr(pending, "platform_text", ""),
            segments=segments,
        )
    request_options = request_options or LLMRequestOptions()
    try:
        event = await agent.run_hook(
            ctx,
            Event(
                point=PointLLMRequestPrepared,
                session=SessionContext(id=session_id),
                message=hook_message,
                llm=LLMPayload(
                    provider=selection.provider,
                    model=selection.model,
                    messages=clone_messages(base_messages),
                    tools=list(tools or []),
                ),
            ),
        )
        selection.provider = event.llm.provider
        selection.model = event.llm.model
        agent.authorize_execution_model_selection(ctx, selection)
    except BaseException as exc:  # noqa: BLE001 - 钩子失败也要先落待发用户消息
        await _persist_pending_lenient(agent, ctx, pending)
        raise RuntimeError(f"llm request hook: {exc}") from exc
    # 请求 hook 不能把工具塞给 chat 会话。
    tools = list(event.llm.tools or []) if tools_enabled else []
    request_options = merge_llm_request_options(
        request_options, llm_request_options_from_payload(event.llm)
    )
    if pending is not None:
        index = int(getattr(pending, "message_index", 0) or 0)
        segments = await materialize_media(agent, ctx, list(event.message.segments or []))
        if 0 <= index < len(base_messages):
            base_messages[index].segments = segments
        pending.message.content = segments_content_text(segments)
        pending.message.segments = stored_message_segments(segments)
        await agent.persist_turn_message(ctx, pending.message, "append_pending_user_message")

    releases: list[Any] = []
    cleanup = None
    request_messages = base_messages
    media = getattr(agent, "media", None)
    try:
        if media is not None:
            request_messages, cleanup = await _resolve_media_for_llm(
                media, ctx, base_messages, releases
            )
        for part in request_options.system_append:
            request_messages = append_system_segment_text(request_messages, part)
        if allow_route:
            routed = await _route_to_dsh(
                agent,
                ctx,
                session_id,
                selection,
                request_messages,
                stream,
                out,
                max_output=int(request_options.max_tokens or 0),
            )
            if routed is not None:
                return routed
        await agent.guard_prompt_before_call(
            ctx, session_id, selection, request_messages, tools, out
        )
        return await _stream_chat(
            agent,
            ctx,
            session_id,
            tools_enabled,
            selection,
            base_messages,
            request_messages,
            tools,
            request_options,
            stream,
            out,
            started_at,
        )
    finally:
        _release_all(releases, cleanup)


async def _stream_chat(
    agent: Any,
    ctx: Any,
    session_id: str,
    tools_enabled: bool,
    selection: Any,
    base_messages: list[LLMMessage],
    request_messages: list[LLMMessage],
    tools: list[ToolSchema] | None,
    request_options: LLMRequestOptions,
    stream: MessageStream | None,
    out: Any,
    started_at: float,
) -> LLMCallResult:
    """真正发起调用并读完流。对应 Go ``callLLM`` 里 ``ChatStream`` 起的那段。"""
    req = ChatRequest(
        model=selection.model,
        session_id=session_id,
        messages=request_messages,
        tools=list(tools or []),
    )
    if request_options.temperature is not None:
        req.temperature = float(request_options.temperature)
    if request_options.max_tokens is not None:
        req.max_tokens = int(request_options.max_tokens)
    if request_options.extra_body:
        req.extra_body = dict(request_options.extra_body)

    assistant: list[str] = []
    usage: Usage | None = None
    tool_calls: list[ToolCallRequest] = []
    show_reasoning = should_show_cli_reasoning(agent, ctx)
    reasoning_open = False

    usage_call_id = new_id()
    reservation = agent.begin_chat_budget(
        ctx, selection, request_messages, tools, int(request_options.max_tokens or 0), usage_call_id
    )
    settled = False
    release_provider = _noop
    try:
        release_provider = await _acquire_provider_slot(agent, ctx, selection.provider)
        try:
            iterator = await _open_stream(agent, ctx, selection, req)
        except BaseException as exc:  # noqa: BLE001 - 对应 Go 的 err != nil 分支
            if is_cancellation(ctx_error(ctx)):
                return _accumulated(assistant, usage, tool_calls, base_messages, stream)
            if _can_retry_without_vision(
                request_messages, exc, "".join(assistant), len(tool_calls), reasoning_open, ctx
            ):
                await _notify_vision_fallback_once(agent, ctx, session_id, out)
                _settle(agent, ctx, reservation, None, True)
                settled = True
                release_provider()
                return await _retry_for_vision(
                    agent,
                    ctx,
                    session_id,
                    tools_enabled,
                    selection,
                    base_messages,
                    tools,
                    request_options,
                    stream,
                    out,
                )
            _audit_llm_error(agent, session_id, selection, exc, started_at)
            await agent.notify_hook_error(
                ctx,
                Event(
                    point=PointLLMResponseReceived,
                    session=SessionContext(id=session_id),
                    llm=LLMPayload(
                        provider=selection.provider,
                        model=selection.model,
                        elapsed_ms=elapsed_millis(started_at),
                    ),
                ),
                exc,
            )
            raise RuntimeError(f"chat: {exc}") from exc
        async for chunk in iterator:
            if not agent.turn_output_allowed(ctx):
                return _accumulated(assistant, usage, tool_calls, base_messages, stream)
            error = getattr(chunk, "error", None)
            if error is not None:
                if is_cancellation(ctx_error(ctx)):
                    return _accumulated(assistant, usage, tool_calls, base_messages, stream)
                if should_fallback_vision(request_messages, error):
                    if _can_retry_without_vision(
                        request_messages,
                        error,
                        "".join(assistant),
                        len(tool_calls),
                        reasoning_open,
                        ctx,
                    ):
                        await _notify_vision_fallback_once(agent, ctx, session_id, out)
                        _settle(agent, ctx, reservation, None, True)
                        settled = True
                        return await _retry_for_vision(
                            agent,
                            ctx,
                            session_id,
                            tools_enabled,
                            selection,
                            base_messages,
                            tools,
                            request_options,
                            stream,
                            out,
                        )
                    # 图片拒绝发生在已经有用户可见输出、思维链或工具调用增量之后：
                    # 重放会输出第二遍答案，因此只提示不重试。
                    await out.send_notice(ctx, LEVEL_WARN, VISION_FALLBACK_BLOCKED_NOTICE)
                    raise mark_user_notified(
                        RuntimeError(f"chat stream: {error}")
                    ) from error
                details = new_user_error_details("LLM 响应中断", error)
                agent.audit(
                    "llm_error",
                    "session_id",
                    session_id,
                    "provider",
                    selection.provider,
                    "model",
                    selection.model,
                    "elapsed_ms",
                    elapsed_millis(started_at),
                    "error_id",
                    details.id,
                    "error",
                    details.safe,
                    "result",
                    ResultFailed,
                )
                await agent.notify_hook_error(
                    ctx,
                    Event(
                        point=PointLLMResponseReceived,
                        session=SessionContext(id=session_id),
                        llm=LLMPayload(
                            provider=selection.provider,
                            model=selection.model,
                            source_text="".join(assistant),
                            text="".join(assistant),
                            tool_calls=list(tool_calls),
                            usage=usage,
                            elapsed_ms=elapsed_millis(started_at),
                        ),
                    ),
                    RuntimeError(details.safe),
                )
                await out.send_notice(ctx, LEVEL_ERROR, details.text)
                raise mark_user_notified(RuntimeError(f"chat stream: {error}")) from error
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            delta_reasoning = getattr(chunk, "delta_reasoning_content", "") or ""
            if delta_reasoning and show_reasoning:
                if not reasoning_open:
                    await out.send_reasoning(ctx, "[thinking] ")
                    reasoning_open = True
                await out.send_reasoning(ctx, delta_reasoning)
            if tools_enabled:
                for delta in getattr(chunk, "tool_call_deltas", None) or []:
                    tool_calls.append(
                        ToolCallRequest(id=delta.id, name=delta.name, arguments=delta.args)
                    )
            delta = getattr(chunk, "delta_content", "") or ""
            assistant.append(delta)
            if stream is not None and delta:
                if not agent.turn_output_allowed(ctx):
                    return _accumulated(assistant, usage, tool_calls, base_messages, stream)
                try:
                    # ``MessageStream.append`` 是 async（Python 侧 I/O 走 asyncio）；
                    # Go 的 ``stream.Append(ctx, delta)`` 是同步调用，照抄形状会丢掉
                    # 协程 —— 增量静默不显示、只剩终态 ``replace``，且不报任何错。
                    await stream.append(ctx, delta)
                except BaseException as exc:  # noqa: BLE001 - 流式写入失败即本轮失败
                    raise RuntimeError(f"stream append: {exc}") from exc
        if not agent.turn_output_allowed(ctx):
            return _accumulated(assistant, usage, tool_calls, base_messages, stream)
        if reasoning_open:
            await out.send_reasoning(ctx, "[/thinking]\n\n")
        elapsed_ms = elapsed_millis(started_at)
        content = "".join(assistant)
        event = await agent.run_hook(
            ctx,
            Event(
                point=PointLLMResponseReceived,
                session=SessionContext(id=session_id),
                llm=LLMPayload(
                    provider=selection.provider,
                    model=selection.model,
                    usage=usage,
                    source_text=content,
                    text=content,
                    tool_calls=list(tool_calls),
                    elapsed_ms=elapsed_ms,
                ),
            ),
        )
        usage = event.llm.usage
        tool_calls = list(event.llm.tool_calls or []) if tools_enabled else []
        log_llm_output(
            agent,
            ctx,
            session_id,
            selection,
            event.llm.text,
            event.llm.source_text,
            len(tool_calls),
            elapsed_ms,
        )
        audit_usage(agent, session_id, selection, usage, elapsed_ms)
        if reservation is None:
            agent.record_chat_usage(ctx, selection.model, usage, usage_call_id)
        return LLMCallResult(
            text=event.llm.text,
            raw_text=content,
            usage=usage,
            tool_calls=tool_calls,
            outputs=list(event.outputs or []),
            messages=base_messages,
            stream=stream,
        )
    finally:
        if not settled:
            settled = True
            _settle(agent, ctx, reservation, usage, False)
        release_provider()


async def _retry_for_vision(
    agent: Any,
    ctx: Any,
    session_id: str,
    tools_enabled: bool,
    selection: Any,
    base_messages: list[LLMMessage],
    tools: list[ToolSchema] | None,
    request_options: LLMRequestOptions,
    stream: MessageStream | None,
    out: Any,
) -> LLMCallResult:
    """把图片换成文本描述后重发一次。对应 Go 的递归 ``callLLM`` 调用。"""
    return await call_llm(
        agent,
        ctx_with_value(ctx, _VISION_ATTEMPT_KEY, True),
        session_id,
        tools_enabled,
        selection,
        fallback_vision_messages(base_messages),
        tools,
        None,
        request_options,
        stream,
        out,
    )


def _accumulated(
    assistant: list[str],
    usage: Usage | None,
    tool_calls: list[ToolCallRequest],
    base_messages: list[LLMMessage],
    stream: MessageStream | None,
) -> LLMCallResult:
    content = "".join(assistant)
    return LLMCallResult(
        text=content,
        raw_text=content,
        usage=usage,
        tool_calls=list(tool_calls),
        messages=base_messages,
        stream=stream,
    )



























# 这些名字的实现搬到了 atri_x.agent.chat_request_options，这里保持导入路径。
from atri_x.agent.chat_request_options import (  # noqa: E402,F401
    LLMRequestOptions,
    llm_request_options_from_payload,
    merge_llm_request_options,
)

# 这些名字的实现搬到了 atri_x.agent.chat_observe，这里保持同样的导入路径。
from atri_x.agent.chat_observe import (  # noqa: E402,F401
    _audit_llm_error,
    audit_usage,
    elapsed_millis,
    is_cli_context,
    log_llm_output,
    send_cli_reasoning,
    should_show_cli_reasoning,
    user_message_segments,
)

# 这些名字的实现搬到了 atri_x.agent.chat_vision，这里保持同样的导入路径。
from atri_x.agent.chat_vision import (  # noqa: E402,F401
    VISION_FALLBACK_BLOCKED_NOTICE,
    _VISION_ATTEMPT_KEY,
    _notify_vision_fallback_once,
    _vision_attempted,
    fallback_vision_messages,
    should_fallback_vision,
    vision_fallback_transparent,
)

# 这些名字的实现搬到了 atri_x.agent.chat_stream，这里保持同样的导入路径。
from atri_x.agent.chat_stream import (  # noqa: E402,F401
    _acquire_provider_slot,
    _can_retry_without_vision,
    _noop,
    _open_stream,
    _persist_pending_lenient,
    _release_all,
    _resolve_media_for_llm,
    _segments_at,
    _settle,
)
