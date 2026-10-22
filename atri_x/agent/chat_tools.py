"""回合里的工具批次：待发用户消息、工具执行、自动确认与 schema 过滤。

对应 Go：``internal/agent/chat_tools.go``。

两处与 Go 文件边界有关的说明：

* ``attachSpeakerMarker`` / ``inputSpeakerMetadata`` 在 Go 里位于
  ``internal/agent/inbound_speaker.go``（不在本组文件清单内，Python 侧也没有对应
  模块），这里按 Go 原样实现，供 :func:`drain_pending_user_input` 使用；
* ``tool_call_risk`` / ``record_tool_call`` 依赖 :mod:`atri_x.tool`、存储与日志，
  在 Go 里同属本文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.llm import LLMMessage, RoleUser, ToolCallRequest, text_segments
from atri_x.llm import segments_content_text
from atri_x.logging import (
    FieldRunSource,
    ResultCanceled,
    ResultFailed,
    ResultRejected,
    ResultSucceeded,
    RunSourceAtrinis,
    RunSourceCron,
    RunSourceForeground,
)
from atri_x.storage import Message, RoleUser as StorageRoleUser, new_id, now
from atri_x.turn import Input as TurnInput

from atri_x.agent.tool_transcript import stored_message_segments

__all__ = [
    "PREVIEW_TEXT_RUNES",
    "TOOL_LOG_ACTION_LIMIT",
    "TOOL_LOG_ARGUMENT_LIMIT",
    "TOOL_LOG_QUERY_LIMIT",
    "PendingUserMessage",
    "attach_speaker_marker",
    "compact_arguments",
    "drain_pending_user_input",
    "execute_tool_calls",
    "filter_tool_schemas_by_allowlist",
    "input_speaker_metadata",
    "is_background_session",
    "is_session_auto_confirmed",
    "is_tool_auto_confirmed",
    "join_assistant_text",
    "join_tool_names",
    "log_risk_confirmation_result",
    "log_risk_confirmation_wait",
    "max_tool_rounds_per_turn",
    "merge_tool_schemas",
    "preview_arguments",
    "preview_log_text",
    "preview_runes",
    "record_tool_call",
    "result_from_success",
    "risk_confirmation_result",
    "risk_reasons_text",
    "session_tools_enabled",
    "set_session_auto_confirmed",
    "set_tool_auto_confirmed",
    "skipped_tool_messages",
    "speaker_display_name",
    "tool_call_risk",
    "tool_log_result_limit",
    "tool_run_source",
    "tools_for_session",
    "unique_input_speakers",
    "with_model_capabilities",
    "with_tool_capabilities",
]


@dataclass
class PendingUserMessage:
    """工具阶段合并进来的用户消息。对应 Go ``pendingUserMessage``。"""

    message: Any = None
    message_index: int = 0
    platform_text: str = ""


# ---------------------------------------------------------------------------
# 待发用户输入
# ---------------------------------------------------------------------------


def drain_pending_user_input(
    agent: Any, ctx: Any, session_id: str, messages: list[LLMMessage]
) -> tuple[list[LLMMessage], PendingUserMessage | None]:
    """把工具阶段合并进来的用户输入接在消息列表尾部。对应 Go ``drainPendingUserInput``。"""
    pending = agent.turns.drain_merged_input(session_id)
    if not pending.text and not pending.segments:
        return messages, None
    segments = list(pending.segments or [])
    if not segments:
        segments = text_segments(pending.text)
    segments = attach_speaker_marker(agent, ctx, pending, segments)
    message = Message(
        id=new_id(),
        session_id=session_id,
        role=StorageRoleUser,
        content=segments_content_text(segments),
        segments=stored_message_segments(segments),
        metadata=input_speaker_metadata(pending),
    )
    binding = PendingUserMessage(
        message=message, message_index=len(messages), platform_text=pending.platform_text
    )
    return list(messages) + [LLMMessage(role=RoleUser, segments=segments)], binding


def unique_input_speakers(input_value: TurnInput | None) -> list[Any]:
    """回合输入里出现过的发言人（去重）。对应 Go ``uniqueInputSpeakers``。"""
    seen: set[str] = set()
    out: list[Any] = []
    if input_value is None:
        return out

    def add(speaker: Any) -> None:
        if speaker is None:
            return
        actor_id = getattr(speaker, "actor_id", "") or ""
        user_id = getattr(speaker, "user_id", "") or ""
        name = getattr(speaker, "name", "") or ""
        if not actor_id and not user_id and not name:
            return
        key = "\x00".join([actor_id, user_id, name])
        if key in seen:
            return
        seen.add(key)
        out.append(speaker)

    for part in getattr(input_value, "parts", None) or []:
        add(getattr(part, "speaker", None))
    add(getattr(input_value, "speaker", None))
    return out


def speaker_display_name(speaker: Any) -> str:
    """发言人显示名。对应 Go ``speakerDisplayName``。"""
    name = " ".join(str(getattr(speaker, "name", "") or "").split())
    name = name.replace("]", "）").replace("[", "（")
    if not name:
        name = "未知成员"
    user_id = str(getattr(speaker, "user_id", "") or "").strip()
    if not user_id:
        return name
    return f"{name}(id:{user_id})"


def attach_speaker_marker(
    agent: Any, ctx: Any, input_value: TurnInput, segments: list[Any]
) -> list[Any]:
    """共享线程里给用户内容加一行服务端生成的发言人标记。

    对应 Go ``Agent.attachSpeakerMarker``：标记由服务端生成，用户正文永远不会被
    当成指令处理。
    """
    from atri_x.llm import prepend_segment_text

    if agent is None:
        return segments
    scope = agent.scope(ctx)
    if not getattr(scope, "shared", False):
        return segments
    speakers = unique_input_speakers(input_value)
    if not speakers:
        return segments
    names = "、".join(speaker_display_name(speaker) for speaker in speakers)
    return prepend_segment_text(segments, f"[发言成员：{names}]")


def input_speaker_metadata(input_value: TurnInput | None) -> str:
    """把发言人写进消息 metadata。对应 Go ``inputSpeakerMetadata``。"""
    speakers = unique_input_speakers(input_value)
    if not speakers:
        return ""
    records = []
    for speaker in speakers:
        record: dict[str, Any] = {}
        for key, value in (
            ("actor_id", getattr(speaker, "actor_id", "") or ""),
            ("user_id", getattr(speaker, "user_id", "") or ""),
            ("name", getattr(speaker, "name", "") or ""),
            ("role", getattr(speaker, "role", "") or ""),
        ):
            if value:
                record[key] = value
        records.append(record)
    return json.dumps({"speakers": records}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------


async def execute_tool_calls(
    agent: Any,
    ctx: Any,
    session: Any,
    calls: list[ToolCallRequest],
    assistant_text: str,
    assistant_raw_text: str,
    out: Any,
    *,
    reasoning: str = "",
) -> Any:
    """跑一批工具调用。对应 Go ``Agent.executeToolCalls``。

    ``reasoning`` 是发起这批调用的那一轮 assistant 思维链，随工具调用记录落库，
    供后续请求按思考模式的协议原样回传。
    """
    from atri_x.toolrun import RunRequest
    from atri_x.toolrun import run as run_tool_calls

    from atri_x.agent.toolrun_adapter import AgentToolRunDeps, tool_run_manager

    if not session_tools_enabled(session):
        return _empty_run_result()
    authorize, before = _tool_hooks(agent, session)
    return await run_tool_calls(
        tool_run_manager(agent),
        ctx,
        AgentToolRunDeps(agent=agent, output=out),
        RunRequest(
            session=session,
            calls=list(calls),
            assistant_text=assistant_text,
            assistant_raw_text=assistant_raw_text,
            assistant_reasoning=reasoning,
            cached_tools=agent.cached_tools_for_session(session),
            actor=agent.actor(ctx),
            authorize_tool=authorize,
            before_execute=before,
        ),
    )


def _empty_run_result() -> Any:
    from atri_x.toolrun import RunResult

    return RunResult()


def _tool_hooks(agent: Any, session: Any = None) -> tuple[Any, Any]:
    """工具授权与执行前钩子。

    这里**不再**吞 ``ImportError``：``atri_x.agent.tool_auth`` 是硬依赖，
    静默降级会返回 ``(None, None)``，而 ``RunRequest.authorize_tool=None`` 过去等于
    "权威授权检查不存在" → 群工具白名单/超管门槛被悄悄绕过。模块缺失时应当直接
    失败（fail-closed），而不是让每次调用都失去授权。

    ``session`` 用于分模式白名单（``chat`` / ``work`` 各一份）：执行闸门要按
    **发起这次调用的会话**的模式取策略，而不是按"当前时刻"猜。
    """
    from atri_x.agent.tool_auth import authorize_tool_call, before_execute_tool_call

    def authorize(ctx: Any, call: Any, resolved: Any) -> Any:
        return authorize_tool_call(agent, ctx, call, resolved, session)

    def before(ctx: Any, call: Any, resolved: Any) -> Any:
        return before_execute_tool_call(agent, ctx, call, resolved, session)

    return authorize, before


def tool_call_risk(agent: Any, ctx: Any, call: ToolCallRequest) -> str:
    """估一次工具调用的风险等级。对应 Go ``Agent.toolCallRisk``。"""
    from atri_x.tool import CallRequest, assess_risk

    registry = getattr(getattr(agent, "tool_runtime", None), "registry", None)
    if registry is None:
        return "unknown"
    target, ok = registry.get(call.name)
    if not ok or target is None:
        return "unknown"
    try:
        assessment = assess_risk(
            ctx, target, CallRequest(id=call.id, name=call.name, arguments=call.arguments)
        )
    except BaseException:  # noqa: BLE001 - 风险评估失败按 unknown 记账
        return "unknown"
    return str(getattr(assessment, "level", "") or "unknown")


def record_tool_call(
    agent: Any,
    ctx: Any,
    session_id: str,
    call: ToolCallRequest,
    risk: str,
    started_at: Any,
    result: str,
    call_err: BaseException | None,
) -> None:
    """记一条工具调用（存储 + 日志 + 审计）。对应 Go ``Agent.recordToolCall``。"""
    from atri_x.storage import ToolCallRecord

    history = agent.history_enabled(ctx)
    result_preview = preview_runes(result, tool_log_result_limit(call.name)) if history else ""
    arguments_preview = preview_arguments(call.arguments) if history else ""
    run_source = tool_run_source(ctx)
    finished_at = now()
    record = ToolCallRecord(
        session_id=session_id,
        tool_call_id=call.id,
        tool_name=call.name,
        actor_id=agent.actor(ctx).id,
        risk_level=risk,
        success=call_err is None,
        result_preview=result_preview,
        started_at=started_at,
        finished_at=finished_at,
    )
    if call_err is not None:
        record.error = str(call_err)
    store = getattr(agent, "store", None)
    if store is not None:
        try:
            store.tool_calls().create(record)
        except BaseException as exc:  # noqa: BLE001 - 记账失败不影响回合
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.log(
                    "warn",
                    "record tool call failed",
                    "session_id",
                    session_id,
                    "tool",
                    call.name,
                    "error",
                    str(exc),
                )
    elapsed_ms = _elapsed_ms(record.started_at, record.finished_at)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.log(
            "info",
            "tool call",
            "event",
            "tool_call",
            "session_id",
            session_id,
            "arguments",
            arguments_preview,
            "result",
            result_preview,
            "tool",
            call.name,
            FieldRunSource,
            run_source,
            "tool_call_id",
            call.id,
            "actor_id",
            record.actor_id,
            "risk",
            risk,
            "success",
            record.success,
            "elapsed_ms",
            elapsed_ms,
            "error",
            record.error,
        )
    agent.audit(
        "tool_call",
        "session_id",
        session_id,
        "arguments",
        arguments_preview,
        "tool",
        call.name,
        FieldRunSource,
        run_source,
        "tool_call_id",
        call.id,
        "actor_id",
        record.actor_id,
        "risk",
        risk,
        "success",
        record.success,
        "elapsed_ms",
        elapsed_ms,
        "error",
        record.error,
        "result",
        result_from_success(record.success),
    )


def _elapsed_ms(started_at: Any, finished_at: Any) -> int:
    try:
        return int((finished_at - started_at).total_seconds() * 1000)
    except BaseException:  # noqa: BLE001 - 时间不可比时不写耗时
        return 0


def tool_run_source(ctx: Any) -> str:
    """这次工具调用的运行来源。对应 Go ``toolRunSource``。"""
    from atri_x.tool import BackgroundKindAtrinis, BackgroundKindCron, sandbox_context_from_context

    sandbox, ok = sandbox_context_from_context(ctx)
    if not ok or sandbox is None:
        return RunSourceForeground
    if not getattr(sandbox, "background", False) and not str(
        getattr(sandbox, "background_kind", "") or ""
    ).strip():
        return RunSourceForeground
    kind = getattr(sandbox, "background_kind", "")
    if kind == BackgroundKindCron:
        return RunSourceCron
    if kind == BackgroundKindAtrinis:
        return RunSourceAtrinis
    # 后台运行但没有可辨识来源：宁可不写，也不要假装是前台。
    return ""


def result_from_success(success: bool) -> str:
    """工具执行的布尔结果 → 契约里的操作结果。对应 Go ``resultFromSuccess``。"""
    return ResultSucceeded if success else ResultFailed


def risk_confirmation_result(action: str) -> str:
    """用户的确认动作 → 操作结果。对应 Go ``riskConfirmationResult``。"""
    normalized = str(action or "").strip().lower()
    if normalized in ("reject", "rejected"):
        return ResultRejected
    if normalized in ("expire", "expired", "stop", "stopped"):
        return ResultCanceled
    return ResultSucceeded


def log_risk_confirmation_wait(
    agent: Any, session_id: str, call: ToolCallRequest, risk: str, reasons: list[str] | None
) -> None:
    """记一条风险确认等待。对应 Go ``Agent.logRiskConfirmationWait``。"""
    attrs: list[Any] = [
        "session_id",
        session_id,
        "tool",
        call.name,
        "risk",
        risk,
        "arguments",
        preview_arguments(call.arguments),
    ]
    if reasons:
        attrs.extend(["risk_reasons", "; ".join(reasons)])
    attrs.extend(["result", ResultSucceeded])
    agent.audit("risk_confirmation_wait", *attrs)


def log_risk_confirmation_result(
    agent: Any,
    session_id: str,
    call: ToolCallRequest,
    risk: str,
    action: str,
    extra: str,
    reason: str,
) -> None:
    """记一条风险确认结果。对应 Go ``Agent.logRiskConfirmationResult``。"""
    agent.audit(
        "risk_confirmation_result",
        "session_id",
        session_id,
        "tool",
        call.name,
        "risk",
        risk,
        "action",
        action,
        "extra",
        extra,
        "reason",
        reason,
        "result",
        risk_confirmation_result(action),
    )


def risk_reasons_text(reasons: list[str] | None) -> str:
    """风险原因的多行文本。对应 Go ``riskReasonsText``。"""
    if not reasons:
        return ""
    lines = ["\n风险原因："]
    for reason in reasons:
        text = str(reason or "").strip()
        if text:
            lines.append(f"\n- {text}")
    return "".join(lines)


# ---------------------------------------------------------------------------
# 自动确认
# ---------------------------------------------------------------------------


def _auto_confirm_lock(agent: Any) -> Any:
    lock = getattr(agent, "auto_confirm_mu", None)
    if lock is None:
        import threading

        lock = threading.Lock()
        agent.auto_confirm_mu = lock
    return lock


def is_session_auto_confirmed(agent: Any, session_id: str) -> bool:
    """该会话是否已"全部放行"。对应 Go ``Agent.isSessionAutoConfirmed``。"""
    with _auto_confirm_lock(agent):
        return bool((getattr(agent, "auto_confirm_session", None) or {}).get(session_id))


def set_session_auto_confirmed(agent: Any, session_id: str) -> None:
    """对应 Go ``Agent.setSessionAutoConfirmed``。"""
    with _auto_confirm_lock(agent):
        if getattr(agent, "auto_confirm_session", None) is None:
            agent.auto_confirm_session = {}
        agent.auto_confirm_session[session_id] = True


def is_tool_auto_confirmed(agent: Any, session_id: str, tool_name: str) -> bool:
    """该工具在这个会话里是否已放行。对应 Go ``Agent.isToolAutoConfirmed``。"""
    with _auto_confirm_lock(agent):
        tools = (getattr(agent, "auto_confirm_tools", None) or {}).get(session_id)
        return bool(tools and tools.get(tool_name))


def set_tool_auto_confirmed(agent: Any, session_id: str, tool_name: str) -> None:
    """对应 Go ``Agent.setToolAutoConfirmed``。"""
    with _auto_confirm_lock(agent):
        if getattr(agent, "auto_confirm_tools", None) is None:
            agent.auto_confirm_tools = {}
        agent.auto_confirm_tools.setdefault(session_id, {})[tool_name] = True


# ---------------------------------------------------------------------------
# schema 装配
# ---------------------------------------------------------------------------
























# ---------------------------------------------------------------------------
# 日志正文预览
# ---------------------------------------------------------------------------

# 这些名字的实现搬到了 atri_x.agent.tool_schemas，这里保持同样的导入路径。
from atri_x.agent.tool_schemas import (  # noqa: E402,F401
    PREVIEW_TEXT_RUNES,
    TOOL_LOG_ACTION_LIMIT,
    TOOL_LOG_ARGUMENT_LIMIT,
    TOOL_LOG_QUERY_LIMIT,
    TOOL_LOG_QUERY_TOOLS,
    _maybe_await,
    _toolrun_view,
    compact_arguments,
    filter_tool_schemas_by_allowlist,
    is_background_session,
    join_assistant_text,
    join_tool_names,
    max_tool_rounds_per_turn,
    merge_tool_schemas,
    preview_arguments,
    preview_log_text,
    preview_runes,
    session_tools_enabled,
    skipped_tool_messages,
    tool_log_result_limit,
    tools_for_session,
    with_model_capabilities,
    with_tool_capabilities,
)
