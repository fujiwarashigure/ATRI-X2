"""回合主流程：从入站文本到助手消息落库。

对应 Go：``internal/agent/chat.go``。

Go 的 ``(T, error)`` 在这里是"返回 T、失败抛异常"，因此
:func:`run_chat_turn_with_output` 返回 ``(session, pending)``，
:func:`run_chat` 返回"工具阶段合并进来、需要续跑的下一条输入"。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from atri_x.delivery import ctx_value
from atri_x.llm import segments_content_text
from atri_x.logging import ResultFailed
from atri_x.request import KindTurn, StartRequest
from atri_x.runtime import PhaseError
from atri_x.safecontext import DeadlineExceededError
from atri_x.storage import (
    ContextSummary,
    Message,
    RoleUser as StorageRoleUser,
    SessionModeWork,
    new_id,
    now,
)
from atri_x.turn import Input as TurnInput

from atri_x.agent.chat_llm import LEVEL_WARN
from atri_x.agent.chat_tools import (
    attach_speaker_marker,
    input_speaker_metadata,
    with_tool_capabilities,
)
from atri_x.agent.events import (
    register_message_work,
    unregister_message_work,
    without_message_work_keys,
)
from atri_x.agent.request_context import with_turn_request_id
from atri_x.agent.segments import (
    inbound_reply_message_id,
    inbound_segments,
    materialize_media,
    with_inbound_turn_input,
)
from atri_x.agent.status import runtime_done_status
from atri_x.agent.turn_output import (
    BackgroundTurnOutput,
    ForegroundTurnOutput,
    TurnOutput,
)
from atri_x.agent.turn_override import turn_model_override

__all__ = [
    "audit_partial_platform_send",
    "has_storage_user_message",
    "handle_chat",
    "handle_turn_context_done",
    "model_selection_for_turn",
    "pending_context_compact",
    "run_chat",
    "run_chat_turn_with_output",
    "start_background_chat",
    "start_chat",
    "start_chat_with_output",
]

#: 空回答的兜底文案。对应 Go 里的字面量。
EMPTY_ASSISTANT_TEXT = "模型这次没有返回可见内容。"
#: 超时停止的提示。对应 Go ``handleTurnContextDone``。
TURN_TIMEOUT_TEXT = "本轮处理已超时停止，可继续发送消息恢复或重试。"
#: cron 在 ctx 上放的模型选择键。对应 Go ``cronModelSelectionKey{}``。
CRON_MODEL_SELECTION_KEY = "atri_x.agent.cron_model_selection"


async def handle_chat(agent: Any, ctx: Any, text: str) -> None:
    """取当前会话并开一轮对话。对应 Go ``Agent.handleChat``。"""
    session = agent.sessions.get_or_create_current(agent.scope(ctx), text)
    await start_chat(agent, ctx, session, text)


async def start_chat(agent: Any, ctx: Any, session: Any, text: str) -> None:
    """前台开一轮对话。对应 Go ``Agent.startChat``。"""
    await start_chat_with_output(agent, ctx, session, text, ForegroundTurnOutput(agent))


async def start_background_chat(agent: Any, ctx: Any, session: Any, text: str) -> None:
    """后台开一轮对话（不产生用户可见输出）。对应 Go ``Agent.startBackgroundChat``。"""
    await start_chat_with_output(agent, ctx, session, text, BackgroundTurnOutput(agent))


async def start_chat_with_output(
    agent: Any, ctx: Any, session: Any, text: str, out: TurnOutput
) -> None:
    """开一轮对话，工具阶段合并进来的输入会被续跑。对应 Go ``Agent.startChatWithOutput``。"""
    while True:
        session, pending = await run_chat_turn_with_output(agent, ctx, session, text, out)
        if not pending.text and not pending.segments:
            return
        text = pending.text
        ctx = without_message_work_keys(ctx)
        ctx = with_inbound_turn_input(ctx, pending)


async def run_chat_turn_with_output(
    agent: Any, ctx: Any, session: Any, text: str, out: TurnOutput
) -> tuple[Any, TurnInput]:
    """跑一轮（含自动压缩与首轮错误状态发布）。

    对应 Go ``Agent.runChatTurnWithOutput``。
    """
    selection = model_selection_for_turn(agent, ctx, session)
    agent.authorize_execution_model_selection(ctx, selection)
    if agent.should_compact(ctx, session, selection):
        session, content = await agent.compact_session(ctx, session, "auto", selection)
        await out.send_assistant(ctx, content)
    if not agent.turns.start_llm_input(
        session.id, agent.turn_input_for_message(ctx, text)
    ):
        return session, TurnInput()
    try:
        pending = await run_chat(agent, ctx, session, text, out, selection)
    except BaseException as err:  # noqa: BLE001 - 与 Go 的 err != nil 分支对应
        agent.turns.stop_session(session.id)
        if _is_background_taken_over(err):
            # 前台接管不算失败：不发布 error 运行状态。
            raise
        status = agent.runtime_status_for_session(session.id)
        status.phase = PhaseError
        status.finished_at = now()
        status.error = str(err)
        await out.publish_runtime_status(ctx, status)
        raise
    else:
        status = agent.runtime_status_for_session(session.id)
        if _running(status):
            await out.publish_runtime_status(ctx, runtime_done_status(status, now()))
        return session, pending
    finally:
        agent.turns.finish_request(session.id)


async def handle_turn_context_done(
    agent: Any, ctx: Any, session_id: str, err: BaseException, out: TurnOutput
) -> None:
    """本轮 ctx 被取消/超时后的收尾。对应 Go ``Agent.handleTurnContextDone``。"""
    if not isinstance(err, DeadlineExceededError):
        return
    if getattr(agent, "logger", None) is not None:
        agent.logger.log(
            "warn", "turn response timeout", "session_id", session_id, "error", str(err)
        )
    agent.audit(
        "turn_response_timeout",
        "session_id",
        session_id,
        "error",
        str(err),
        "result",
        ResultFailed,
    )
    await out.send_notice(ctx, LEVEL_WARN, TURN_TIMEOUT_TEXT)


async def run_chat(
    agent: Any,
    ctx: Any,
    session: Any,
    text: str,
    out: TurnOutput,
    selection: Any,
) -> TurnInput:
    """一轮对话的正文流程。对应 Go ``Agent.runChat``。"""
    ctx = with_tool_capabilities(agent, ctx, session)
    inbound = agent.turn_input_for_message(ctx, text)
    user_segments = materialize_media(agent, ctx, inbound_segments(ctx, text))
    user_segments = attach_speaker_marker(agent, ctx, inbound, user_segments)
    user_content = segments_content_text(user_segments)
    user_message = Message(
        id=new_id(),
        session_id=session.id,
        role=StorageRoleUser,
        content=user_content,
        segments=_stored_segments(user_segments),
        metadata=input_speaker_metadata(inbound),
        reply_to_platform_message_id=inbound_reply_message_id(ctx),
    )
    logger = getattr(agent, "logger", None)
    if logger is not None:
        if agent.history_enabled(ctx):
            from atri_x.agent.chat_tools import preview_log_text

            logger.log(
                "info",
                "user input",
                "event",
                "user_message",
                "session_id",
                session.id,
                "text",
                preview_log_text(user_content),
            )
        else:
            logger.log(
                "info",
                "user input",
                "event",
                "user_message",
                "session_id",
                session.id,
                "history",
                "off",
            )

    loaded = await _load_context(agent, ctx, session.id)
    has_user_history = has_storage_user_message(loaded.messages)
    compact_seed_on_current_user = False
    seed = pending_context_compact(session)
    if seed is not None:
        if not has_user_history:
            loaded.summary = ContextSummary(summary=seed.summary)
            compact_seed_on_current_user = True
        else:
            await consume_context_compact_seed(agent, ctx, session)
    summary_on_current_user = (
        loaded.summary is not None and not has_user_history and not compact_seed_on_current_user
    )
    messages = list(loaded.messages) + [user_message]

    info, req_ctx, done = await agent.requests.start(
        _request_parent(ctx),
        StartRequest(
            session_id=session.id,
            kind=KindTurn,
            label="chat",
            fair_key=agent.request_fair_key(ctx),
            scope_key=agent.request_scope_key(ctx),
            timeout=agent.response_timeout,
        ),
    )
    req_ctx = with_turn_request_id(req_ctx, info.id)
    try:
        if register_message_work(agent, ctx, session.id, info.id):
            return TurnInput()
        return await _run_chat_body(
            agent,
            ctx,
            req_ctx,
            info,
            session,
            user_message,
            inbound,
            user_segments,
            messages,
            loaded,
            selection,
            out,
            compact_seed_on_current_user,
            summary_on_current_user,
        )
    finally:
        done()
        unregister_message_work(agent, info.id)


def model_selection_for_turn(agent: Any, ctx: Any, session: Any) -> Any:
    """这一轮该用哪个模型。对应 Go ``Agent.modelSelectionForTurn``。"""
    mode = session.mode if session is not None and session.mode else SessionModeWork
    selection = replace(agent.model_for_mode(mode))
    group_model, has_group = agent.group_default_model_selection(ctx)
    if has_group and group_model is not None:
        selection = replace(group_model)
    override, has_override = turn_model_override(ctx)
    if has_override and override is not None:
        if getattr(override, "provider", ""):
            selection.provider = override.provider
        if getattr(override, "model", ""):
            selection.model = override.model
    cron_override = ctx_value(ctx, CRON_MODEL_SELECTION_KEY)
    if isinstance(cron_override, dict):
        if cron_override.get("provider"):
            selection.provider = cron_override["provider"]
        if cron_override.get("model"):
            selection.model = cron_override["model"]
    return selection


def audit_partial_platform_send(
    agent: Any, session_id: str, operation: str, receipt: Any, err: BaseException | None
) -> None:
    """部分送达的发送要在审计里留痕。对应 Go ``Agent.auditPartialPlatformSend``。"""
    if agent is None or err is None:
        return
    ids = list(getattr(receipt, "platform_message_ids", None) or [])
    if not ids:
        return
    agent.audit(
        "platform_send_partial",
        "session_id",
        session_id,
        "operation",
        operation,
        "platform_message_count",
        len(ids),
        "error",
        str(err),
        "result",
        ResultFailed,
    )


def has_storage_user_message(messages: list[Message]) -> bool:
    """历史里是否已有用户消息。对应 Go ``hasStorageUserMessage``。"""
    return any(message.role == StorageRoleUser for message in messages or [])


def pending_context_compact(session: Any) -> Any:
    """待生效的压缩种子。对应 Go ``pendingContextCompact``。"""
    if session is None:
        return None
    from atri_x.agent.session_metadata import decode_session_metadata

    compact = decode_session_metadata(getattr(session, "metadata", "") or "").context_compact
    if compact is None or not compact.pending or not compact.summary:
        return None
    return compact


async def consume_context_compact_seed(agent: Any, ctx: Any, session: Any) -> None:
    """把压缩种子标记为已消费。对应 Go ``Agent.consumeContextCompactSeed``。"""
    if session is None:
        return

    def _mutate(metadata: Any) -> bool:
        compact = metadata.context_compact
        if compact is None or not compact.pending:
            return False
        compact.pending = False
        return True

    try:
        await _mutate_metadata(agent, ctx, session, _mutate)
    except BaseException as exc:  # noqa: BLE001 - 种子消费失败只记日志
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.log(
                "warn",
                "consume compact context failed",
                "session_id",
                getattr(session, "id", ""),
                "error",
                str(exc),
            )


def summary_user_prefix(summary: str) -> str:
    """摘要注入用户输入时的前缀。对应 Go ``summaryUserPrefix``。"""
    try:
        from atri_x.agent.prompt import summary_user_prefix as shared

        return shared(summary)
    except ImportError:  # pragma: no cover - 同僚模块缺失
        return f"[以下是本会话此前的摘要]\n{summary}\n[摘要结束]\n\n"


def _request_parent(ctx: Any) -> Any:
    """``requests.start`` 的父 token。ctx 不是 CancelToken 时不挂父级。"""
    from atri_x.safecontext import CancelToken

    return ctx if isinstance(ctx, CancelToken) else None


def _running(status: Any) -> bool:
    running = getattr(status, "running", None)
    if callable(running):
        return bool(running())
    return bool(getattr(status, "phase", ""))


def _is_prompt_overflow(err: BaseException) -> bool:
    from atri_x.errors import PromptOverflowError

    return _error_chain_has(err, PromptOverflowError)


def _is_background_taken_over(err: BaseException) -> bool:
    from atri_x.errors import BackgroundTakenOverError

    return _error_chain_has(err, BackgroundTakenOverError)


def _error_chain_has(err: BaseException | None, kind: type) -> bool:
    """沿 ``__cause__`` 链判定错误类型（对应 Go 的 ``errors.Is`` 包装穿透）。"""
    seen = 0
    current: BaseException | None = err
    while current is not None and seen < 16:
        if isinstance(current, kind):
            return True
        current = current.__cause__
        seen += 1
    return False


def _taken_over() -> BaseException:
    from atri_x.errors import BackgroundTakenOverError

    return BackgroundTakenOverError("background taken over")

# 这些名字的实现搬到了 atri_x.agent.chat_turn，这里保持同样的导入路径。
from atri_x.agent.chat_turn import (  # noqa: E402,F401
    _FallbackPromptBuildRequest,
    _assistant_metadata,
    _load_context,
    _mutate_metadata,
    _prompt_build_request,
    _receipt_of,
    _run_chat_body,
    _stored_segments,
)
