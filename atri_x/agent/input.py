"""入站输入的准入、确认与回合切换。

对应 Go：``internal/agent/input.go``。

本模块是"一条入站消息该走哪条路"的唯一裁决处：限流 → 群知识直答 → 会话选择 →
收件箱/直连 → 回合相位分派（风险确认 / 追加确认 / LLM 中打断 / 工具中追加 /
压缩中）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_x.command import Continuation
from atri_x.errors import NotFoundError
from atri_x.hook import Event, MessagePayload, PointAgentInputPrepared
from atri_x.llm import RoleUser, segments_text_only
from atri_x.logging import ResultSkipped, ResultSucceeded
from atri_x.platform import message_context_from
from atri_x.security import RoleSuperadmin
from atri_x.session import ExpireIdleRequest, IdleExpirationConfig, scope_ttl_minutes
from atri_x.storage import Session
from atri_x.turn import (
    PhaseAwaitAppendConfirm,
    PhaseAwaitRiskConfirm,
    PhaseCompact,
    PhaseIdle,
    PhaseLLM,
    PhaseTool,
    RiskConfirmation,
    RiskConfirmationResponse,
    is_cancel,
    is_confirm,
)

from atri_x.agent.logging import audit
from atri_x.agent.segments import (
    has_inbound_non_text_segment,
    inbound_segments,
    replace_inbound_text_segments,
    with_inbound_segments,
    with_inbound_turn_input,
)

__all__ = [
    "DEFAULT_USER_CONFIRMATION_TIMEOUT",
    "active_turn_command_blocked_text",
    "append_confirm_prompt_text",
    "compact_command_blocked_text",
    "confirmation_wait_duration_text",
    "confirmation_wait_timeout",
    "continue_command_input",
    "expire_idle_current_session",
    "handle_append_confirmation_input",
    "handle_input",
    "handle_risk_confirmation_input",
    "handle_session_input",
    "has_fork_from_message",
    "risk_confirmation_command_names",
    "risk_confirmation_confirm_all_blocked_text",
    "risk_confirmation_detail_text",
    "risk_confirmation_foreign_actor_text",
    "risk_confirmation_waiting_text",
    "session_for_input",
]

#: 普通用户等待确认的默认上限。对应 Go ``defaultUserConfirmationTimeout``。
DEFAULT_USER_CONFIRMATION_TIMEOUT = 600.0

#: 风险确认等待期间允许的命令名（对齐 Go ``riskConfirmationCommandNames``）。
_RISK_COMMAND_NAMES = (
    "detail",
    "details",
    "confirm",
    "c",
    "confirmtool",
    "ct",
    "confirmall",
    "ca",
    "reject",
    "stop",
)


def risk_confirmation_command_names() -> list[str]:
    """风险确认期间允许的命令名。对应 Go ``riskConfirmationCommandNames``。"""
    return list(_RISK_COMMAND_NAMES)


def risk_confirmation_waiting_text(prefix: str, allow_confirm_all: bool = True) -> str:
    """风险确认等待提示。对应 Go ``riskConfirmationWaitingText``。

    ``allow_confirm_all=False``（群聊 / 频道）时不提 ``confirmall``：那条指令
    会被 :func:`handle_risk_confirmation_input` 拒绝。
    """
    text = (
        "当前有高风险工具调用正在等待确认，只能发送确认类命令：\n"
        f"{prefix}detail 查看详情；{prefix}confirm 确认；{prefix}confirmtool 本次会话内始终允许该工具；"
    )
    if allow_confirm_all:
        text += f"{prefix}confirmall 本次会话内始终允许所有工具；"
    text += f"{prefix}reject 拒绝；{prefix}stop 停止本轮。"
    return text


def risk_confirmation_foreign_actor_text() -> str:
    """确认请求不属于当前发言者时的拒绝文案。"""
    return (
        "这条高风险确认属于其他成员，你不能代为确认。"
        "请让发起人本人确认或拒绝；超级管理员除外。"
    )


def risk_confirmation_confirm_all_blocked_text(prefix: str) -> str:
    """群聊里禁用 ``confirmall`` 的说明文案。"""
    return (
        "群聊里不能一次性放行本会话的全部高风险工具：请用 "
        f"{prefix}confirmtool（只放行这一个工具）或 {prefix}confirm（只放行这一次），"
        f"也可以 {prefix}reject 拒绝。"
    )


def risk_confirmation_detail_text(confirmation: RiskConfirmation | None) -> str:
    """风险确认详情正文。对应 Go ``riskConfirmationDetailText``。"""
    if confirmation is None:
        return "当前没有等待确认的高风险工具调用。"
    lines = [
        "高风险工具调用待确认：",
        f"- 工具：{confirmation.tool_name}",
        f"- 风险：{confirmation.risk}",
    ]
    if getattr(confirmation, "redacted", False):
        from atri_x.agent.risk_confirmation import REDACTED_ARGUMENTS_TEXT

        lines.append("- " + REDACTED_ARGUMENTS_TEXT)
        if confirmation.summary:
            lines.append(f"- 摘要：{confirmation.summary}")
        return "\n".join(lines)
    if confirmation.summary:
        lines.append(f"- 摘要：{confirmation.summary}")
    if confirmation.arguments:
        lines.append(f"- 参数：{confirmation.arguments}")
    if confirmation.detail:
        lines.append("")
        lines.append(confirmation.detail)
    return "\n".join(lines)


def confirmation_wait_timeout(agent: Any, ctx: Any) -> float:
    """等待用户确认的秒数上限。对应 Go ``Agent.confirmationWaitTimeout``。"""
    current = agent.actor(ctx)
    is_superadmin = current.role == RoleSuperadmin
    ttl_minutes = scope_ttl_minutes(
        getattr(agent, "idle_expiration", None), agent.scope(ctx), is_superadmin
    )
    session_timeout = float(ttl_minutes) * 60.0 if ttl_minutes > 0 else 0.0
    if is_superadmin:
        return session_timeout
    user_timeout = float(getattr(agent, "user_confirmation_timeout", 0.0) or 0.0)
    if user_timeout <= 0:
        user_timeout = DEFAULT_USER_CONFIRMATION_TIMEOUT
    if session_timeout > 0 and session_timeout < user_timeout:
        return session_timeout
    return user_timeout


def confirmation_wait_duration_text(timeout: float) -> str:
    """确认等待时长的中文描述。对应 Go ``confirmationWaitDurationText``。"""
    if timeout <= 0:
        return ""
    if timeout % 60.0 == 0:
        return f"{int(timeout // 60)} 分钟"
    return f"{timeout:g} 秒"


def append_confirm_prompt_text(timeout: float) -> str:
    """追加确认提示。对应 Go ``appendConfirmPromptText``。"""
    text = (
        "已停止当前处理。是否追加这条消息并重新发送？\n"
        "发送 $ / 是 / y / yes 确认；发送 取消 / 否 / n / no 放弃。\n"
        "也可以继续发送内容，发送完后再确认。"
    )
    if timeout > 0:
        text += f"\n超过 {confirmation_wait_duration_text(timeout)} 没有继续发送内容或确认，将自动放弃。"
    return text


async def handle_append_confirmation_input(
    agent: Any, ctx: Any, session: Session, text: str
) -> None:
    """处理追加确认相位的输入。对应 Go ``Agent.handleAppendConfirmationInput``。"""
    if is_confirm(text):
        merged, ok = agent.turns.confirm_append_input(session.id)
        if not ok or (not merged.text and not merged.segments):
            return None
        ctx = with_inbound_turn_input(ctx, merged)
        return await agent.start_chat(ctx, session, merged.text)
    if is_cancel(text):
        agent.turns.cancel_append(session.id)
        await agent.send_chat(ctx, "已取消追加，本轮处理已停止。")
        return None
    agent.turns.append_pending_input(session.id, agent.turn_input_for_message(ctx, text))
    return None


def compact_command_blocked_text(agent: Any, command: str) -> str:
    """压缩中拒绝命令的提示。对应 Go ``commandExecutor.compactCommandBlockedText``。"""
    return (
        f"正在压缩当前会话，暂不执行 {command}。"
        f"请等待压缩完成，或先使用 {agent.command_prefix()}stop 取消。"
    )


def active_turn_command_blocked_text(agent: Any) -> str:
    """回合进行中拒绝切换的提示。对应 Go ``activeTurnCommandBlockedText``。"""
    return (
        "当前会话处理中，暂不支持切换。"
        f"如有必要，请先使用 {agent.command_prefix()}stop 结束当前处理。"
    )


async def handle_input(agent: Any, ctx: Any, text: str) -> None:
    """入站消息的准入与分派。对应 Go ``Agent.handleInput``。"""
    allowed, retry_after = agent.allow_inbound(ctx)
    if not allowed:
        await agent.send_chat(ctx, agent.rate_limit_retry_text(retry_after))
        return None
    if agent.try_group_knowledge_answer(ctx, text):
        return None
    session = await session_for_input(agent, ctx, text)
    if await agent.use_inbound_inbox(ctx):
        phase = agent.turns.snapshot(session.id).phase
        if phase in (PhaseAwaitRiskConfirm, PhaseAwaitAppendConfirm, PhaseCompact):
            # 确认与压缩提示必须立刻处理；把它们排在所回应的回合后面会死锁。
            return await agent.handle_session_input(ctx, session, text)
        if phase == PhaseTool and not agent.scope(ctx).shared:
            # 每条用户自己的工具回合可以安全吸收同发言人的待处理输入。共享会话必须
            # 排队，免得别人的文本在当前回合的权限下执行。
            return await agent.handle_session_input(ctx, session, text)
        return await agent.submit_inbound(ctx, session.id, text)
    return await agent.handle_session_input(ctx, session, text)


async def continue_command_input(agent: Any, ctx: Any, continuation: Continuation) -> None:
    """命令续链（如 ``/resume`` 之后继续聊天）。对应 Go ``continueCommandInput``。"""
    session = agent.sessions.resume(agent.scope(ctx), continuation.session_id)
    segments = replace_inbound_text_segments(ctx, continuation.text)
    ctx = with_inbound_segments(ctx, segments)
    return await agent.handle_session_input(ctx, session, continuation.text)


async def handle_session_input(agent: Any, ctx: Any, session: Session, text: str) -> None:
    """确定会话之后的总入口。对应 Go ``Agent.handleSessionInput``。"""
    ctx = agent.with_tool_capabilities(ctx, session)
    # 压缩准入在输入 Hook 与指令处理之前：会被拒绝的消息不得预载工具、也不得改写
    # 会话的工具缓存。
    if agent.compact_active(session.id):
        await agent.send_chat(
            ctx,
            f"正在压缩上下文，请稍后再发送。可使用 {agent.command_prefix()}stop 取消当前请求。",
        )
        return None
    event = await agent.run_hook(
        ctx,
        Event(
            point=PointAgentInputPrepared,
            session=agent.hook_session(session),
            message=MessagePayload(role=RoleUser, segments=inbound_segments(ctx, text)),
        ),
    )
    ctx = with_inbound_segments(ctx, event.message.segments)
    text = segments_text_only(event.message.segments)

    if session.archived_at is not None:
        await agent.send_chat(
            ctx,
            "当前会话已归档，不能继续聊天。若要继续，请先使用 "
            + agent.command_prefix()
            + "unarchive。",
        )
        return None

    snapshot = agent.turns.snapshot(session.id)
    if snapshot.phase != PhaseAwaitRiskConfirm:
        directives = agent.apply_tool_directives(ctx, session, text)
        if getattr(directives, "err", None) is not None:
            raise directives.err
        if _any_nonempty(
            directives, "injected", "existing", "invalid"
        ) or getattr(directives, "mode_blocked", False):
            await agent.notify_tool_directive_result(ctx, directives)
        text = getattr(directives, "text", text)
        skill_directives = agent.apply_skill_directives(ctx, session, text)
        if _any_nonempty(
            skill_directives, "skills", "injected_wrappers", "existing_wrappers", "invalid"
        ) or getattr(skill_directives, "mode_blocked", False):
            await agent.notify_skill_directive_result(ctx, skill_directives)
        text = getattr(skill_directives, "text", text)
        character_directives = agent.apply_character_directives(ctx, text)
        if getattr(character_directives, "applied", None) or getattr(
            character_directives, "invalid", None
        ):
            await agent.notify_character_directive_result(ctx, character_directives)
        if getattr(character_directives, "applied", None):
            from atri_x.character import with_active

            ctx = with_active(ctx, *character_directives.applied)
        text = getattr(character_directives, "text", text)
        ctx, turn_override = agent.apply_turn_overrides(ctx, text)
        if (
            getattr(turn_override, "applied", None)
            or getattr(turn_override, "invalid", None)
            or getattr(turn_override, "denied", None)
        ):
            await agent.notify_turn_override_result(ctx, turn_override)
        text = getattr(turn_override, "text", text)
        ctx = with_inbound_segments(ctx, replace_inbound_text_segments(ctx, text))
        if not (text or "").strip() and not has_inbound_non_text_segment(ctx):
            if getattr(directives, "injected", None) or getattr(
                skill_directives, "injected_wrappers", None
            ):
                store = getattr(agent, "store", None)
                if store is not None:
                    try:
                        latest = store.sessions().get(session.id)
                    except Exception:  # noqa: BLE001 - 会话被删就保持原对象
                        latest = None
                    if latest is not None:
                        _copy_session(session, latest)
            return None

    if snapshot.phase == PhaseAwaitRiskConfirm:
        return await handle_risk_confirmation_input(agent, ctx, session.id, text)
    if snapshot.phase == PhaseAwaitAppendConfirm:
        return await handle_append_confirmation_input(agent, ctx, session, text)
    if snapshot.phase == PhaseLLM:
        for request_id in agent.requests.session_ids(session.id):
            agent.mark_turn_canceled(request_id)
        agent.requests.cancel_session(session.id)
        agent.cancel_inbox_session(session.id)
        if not agent.turns.interrupt_llm_input(
            session.id, agent.turn_input_for_message(ctx, text)
        ):
            return None
        timeout = confirmation_wait_timeout(agent, ctx)
        await agent.send_chat(ctx, append_confirm_prompt_text(timeout))
        if timeout > 0:
            wait_ctx = _without_cancel(ctx)
            asyncio.get_running_loop().create_task(
                _await_append_expiration(agent, session.id, timeout, wait_ctx)
            )
        return None
    if snapshot.phase == PhaseTool:
        agent.turns.append_pending_input(
            session.id, agent.turn_input_for_message(ctx, text)
        )
        await agent.send_chat(
            ctx,
            "已追加，将在当前流程下一次模型调用时带上。发送 "
            + agent.command_prefix()
            + "stop 可打断当前流程。",
        )
        return None
    if snapshot.phase == PhaseCompact:
        await agent.send_chat(
            ctx,
            f"正在压缩上下文，请稍后再发送。可使用 {agent.command_prefix()}stop 取消当前请求。",
        )
        return None
    return await agent.start_chat(ctx, session, text)


async def _await_append_expiration(
    agent: Any, session_id: str, timeout: float, wait_ctx: Any
) -> None:
    try:
        expired = await agent.turns.await_append_expiration(session_id, timeout)
    except asyncio.CancelledError:  # pragma: no cover - 关闭期
        return
    if expired:
        await agent.send_chat(wait_ctx, "追加确认已过期，待追加内容已丢弃，本轮处理已停止。")


async def expire_idle_current_session(agent: Any, ctx: Any) -> None:
    """空闲超时的会话自动过期。对应 Go ``Agent.expireIdleCurrentSession``。"""
    try:
        current = agent.sessions.current(agent.scope(ctx))
    except NotFoundError:
        return None
    if agent.turns.snapshot(current.id).phase != PhaseIdle:
        return None
    current_actor = agent.actor(ctx)
    result = agent.sessions.expire_idle_current(
        ExpireIdleRequest(
            scope=agent.scope(ctx),
            is_superadmin=current_actor.role == RoleSuperadmin,
            config=getattr(agent, "idle_expiration", None) or IdleExpirationConfig(),
        )
    )
    if result.expired:
        audit(
            agent,
            "session_idle_expired",
            "session_id",
            result.session_id,
            "actor_id",
            current_actor.id,
            "ttl_minutes",
            result.ttl_minutes,
            "result",
            ResultSkipped,
        )
    return None


def has_fork_from_message(ctx: Any) -> bool:
    """对应 Go ``hasForkFromMessage``。"""
    msg, ok = message_context_from(ctx)
    return bool(ok and msg.fork_from_message_id)


async def session_for_input(agent: Any, ctx: Any, text: str) -> Session:
    """为这条入站消息选定 Session。对应 Go ``Agent.sessionForInput``。"""
    msg, ok = message_context_from(ctx)
    if ok:
        if msg.resume_session_id:
            try:
                return agent.sessions.resume(agent.scope(ctx), msg.resume_session_id)
            except Exception:  # noqa: BLE001 - 共享会话下旧回复仍应继续
                if not agent.scope(ctx).shared:
                    raise
        if msg.fork_from_message_id:
            return agent.sessions.fork(agent.scope(ctx), msg.fork_from_message_id)
    mode = agent.group_default_session_mode(ctx)
    return agent.sessions.get_or_create_current_with_mode(agent.scope(ctx), text, mode)


async def handle_risk_confirmation_input(agent: Any, ctx: Any, session_id: str, text: str) -> None:
    """风险确认相位的输入分派。对应 Go ``Agent.handleRiskConfirmationInput``。

    本仓扩展的两道闸门（都在解析出指令名之后、执行动作之前）：

    1. **确认人绑定**：只有发起该调用的成员本人（或超级管理员）能确认 / 拒绝 /
       查看详情。共享线程群（``thread_mode = "group"``）里所有人共用同一个
       Session，没有这道校验时任何群成员都能替发起人放行高风险调用。
    2. **群聊禁用 confirmall**：一次放行本会话全部工具只在单聊 / CLI 提供。
    """
    from atri_x.agent.risk_confirmation import confirmation_allows_confirm_all

    confirmation, has_confirmation = agent.turns.pending_risk_confirmation(session_id)
    allow_confirm_all = confirmation_allows_confirm_all(agent, ctx)
    # 风险确认等待期间只接受确认/拒绝/详情/停止类命令。普通文本不能混入当前 turn，
    # 免得被当作高风险工具的隐式确认或污染下一次 LLM 调用上下文。
    if not agent.commands.is_command(text):
        log_risk_confirmation_action(agent, session_id, "invalid_text", confirmation, "")
        await agent.send_chat(
            ctx, risk_confirmation_waiting_text(agent.command_prefix(), allow_confirm_all)
        )
        return None

    parsed = agent.commands.parse(text)
    name = parsed.name
    if has_confirmation and not _may_resolve_confirmation(
        agent, ctx, confirmation
    ):
        log_risk_confirmation_action(
            agent, session_id, "foreign_actor", confirmation, name
        )
        await agent.send_chat(ctx, risk_confirmation_foreign_actor_text())
        return None
    if name in ("detail", "details"):
        if not agent.turns.refresh_risk_confirmation(session_id):
            return None
        log_risk_confirmation_action(agent, session_id, "detail", confirmation, "")
        await agent.send_chat(ctx, risk_confirmation_detail_text(confirmation))
    elif name in ("confirm", "c"):
        log_risk_confirmation_action(agent, session_id, "confirm", confirmation, parsed.args)
        agent.turns.resolve_risk_confirmation(
            session_id, RiskConfirmationResponse(confirmed=True, extra=parsed.args)
        )
    elif name in ("confirmtool", "ct"):
        log_risk_confirmation_action(
            agent, session_id, "confirmtool", confirmation, parsed.args
        )
        agent.turns.resolve_risk_confirmation(
            session_id,
            RiskConfirmationResponse(confirmed=True, confirm_tool=True, extra=parsed.args),
        )
    elif name in ("confirmall", "ca"):
        if not allow_confirm_all:
            log_risk_confirmation_action(
                agent, session_id, "confirmall_blocked", confirmation, parsed.args
            )
            await agent.send_chat(
                ctx,
                risk_confirmation_confirm_all_blocked_text(agent.command_prefix()),
            )
            return None
        log_risk_confirmation_action(
            agent, session_id, "confirmall", confirmation, parsed.args
        )
        agent.turns.resolve_risk_confirmation(
            session_id,
            RiskConfirmationResponse(confirmed=True, confirm_all=True, extra=parsed.args),
        )
    elif name == "reject":
        log_risk_confirmation_action(agent, session_id, "reject", confirmation, parsed.args)
        agent.turns.resolve_risk_confirmation(
            session_id, RiskConfirmationResponse(rejected=True, reason=parsed.args)
        )
    elif name == "stop":
        log_risk_confirmation_action(agent, session_id, "stop", confirmation, "")
        agent.requests.cancel_session(session_id)
        agent.turns.resolve_risk_confirmation(
            session_id, RiskConfirmationResponse(stopped=True)
        )
        await agent.send_chat(ctx, "stopped")
    else:
        if has_confirmation:
            log_risk_confirmation_action(
                agent, session_id, "invalid_command", confirmation, name
            )
        await agent.send_chat(
            ctx, risk_confirmation_waiting_text(agent.command_prefix(), allow_confirm_all)
        )
    return None


def _may_resolve_confirmation(
    agent: Any, ctx: Any, confirmation: RiskConfirmation | None
) -> bool:
    """当前发言者能否处理这条待确认调用。

    空 ``actor_id`` 表示来源未知（历史数据 / 其它宿主），此时不拦截，只按旧行为继续；
    超级管理员可以作为例外代为确认（运维解锁，审计里记 ``foreign_actor``）。
    """
    owner = str(getattr(confirmation, "actor_id", "") or "").strip()
    if not owner:
        return True
    current = agent.actor(ctx)
    if getattr(current, "id", "") == owner:
        return True
    return getattr(current, "role", "") == RoleSuperadmin


def log_risk_confirmation_action(
    agent: Any, session_id: str, action: str, confirmation: RiskConfirmation | None, extra: str
) -> None:
    """记录一次风险确认交互。对应 Go ``Agent.logRiskConfirmationAction``。"""
    audit(
        agent,
        "risk_confirmation_command",
        "session_id",
        session_id,
        "action",
        action,
        "tool",
        getattr(confirmation, "tool_name", ""),
        "risk",
        getattr(confirmation, "risk", ""),
        "extra",
        extra,
        "result",
        ResultSucceeded,
    )


def _any_nonempty(obj: Any, *names: str) -> bool:
    return any(bool(getattr(obj, name, None)) for name in names)


def _copy_session(target: Session, source: Session) -> None:
    """``*session = *latest`` 的 Python 等价物（就地覆盖字段）。"""
    for field_name in vars(source):
        setattr(target, field_name, getattr(source, field_name))


def _without_cancel(ctx: Any) -> Any:
    """``context.WithoutCancel`` 的等价物：只保留值，不带取消。"""
    from atri_x.safecontext import CancelToken

    if not isinstance(ctx, CancelToken):
        return ctx
    fresh = CancelToken()
    fresh._values = dict(ctx._values)  # noqa: SLF001 - 见 safecontext 的值语义
    return fresh
