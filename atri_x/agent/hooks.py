"""Hook 运行、路由与失败通知。

对应 Go：``internal/agent/hooks.go``。

Go 的 ``hookRunner`` / ``HookRouter`` 两个接口 → 本模块的同名
:class:`typing.Protocol`；Hook 管理器的唯一出处是 :mod:`atri_x.hook`
（``NoopManager`` / ``DefaultManager``），本模块只消费。
"""

from __future__ import annotations

import logging as std_logging
from typing import Any, Protocol, runtime_checkable

from atri_x.delivery import Notice, Output, Target, text as text_output
from atri_x.hook import (
    ActorContext,
    Event,
    LLMPayload,
    MessagePayload,
    MessageReplyPayload,
    PlatformContext,
    PointErrorOccurred,
    PointPlatformConnected,
    PointPlatformMessageReceived,
    SessionContext,
)
from atri_x.hook.manager import NoopManager
from atri_x.llm import RoleUser
from atri_x.llm import segments_text_only as llm_segments_text_only
from atri_x.platform import message_context_from
from atri_x.redact import error as redact_error
from atri_x.redact import summarize
from atri_x.request import KindHook, StartRequest
from atri_x.storage import Session
from atri_x.agent.errors import safe_error
from atri_x.agent.request_context import turn_request_id_from_context
from atri_x.agent.segments import platform_segments_to_llm

__all__ = [
    "HookRouter",
    "HookRunner",
    "actor_context",
    "cancel_hook_route",
    "fill_hook_context",
    "handle_platform_connected",
    "hook_failure_notice_text",
    "hook_session",
    "log_hook_error",
    "notify_hook",
    "notify_hook_error",
    "observe_hook_run",
    "route_hook",
    "run_hook",
    "send_hook_failure_notice",
    "set_hook_manager",
    "set_hook_runtime",
    "trim_hook_notice_text",
]

#: Hook 失败通知正文的最大 rune 数。对应 Go 里 ``redact.Summarize(text, 1200)``。
HOOK_NOTICE_MAX_RUNES = 1200


@runtime_checkable
class HookRunner(Protocol):
    """跑一个 Hook 事件。对应 Go ``hookRunner``。"""

    async def run(self, ctx: Any, event: Event) -> Event: ...
    async def notify(self, ctx: Any, event: Event) -> None: ...


@runtime_checkable
class HookRouter(Protocol):
    """有状态 Hook 续链路由。对应 Go ``HookRouter``。"""

    def cancel(self, event: Event) -> bool: ...
    async def route(self, ctx: Any, event: Event) -> tuple[Event, bool, BaseException | None]: ...
    def route_hook_id(self, event: Event) -> str: ...


def set_hook_manager(agent: Any, manager: Any) -> None:
    """对应 Go ``Agent.SetHookManager``。"""
    if manager is None:
        manager = NoopManager()
    if hasattr(manager, "set_wakeup_func"):
        manager.set_wakeup_func(agent.hook_wakeup)
    if hasattr(manager, "set_observer"):
        manager.set_observer(agent.observe_hook_run)
    agent.hooks = manager


def set_hook_runtime(agent: Any, router: Any) -> None:
    """挂上有状态 Hook 续链路由。对应 Go ``Agent.SetHookRuntime``。"""
    agent.hook_runtime = router


def cancel_hook_route(agent: Any, event: Event) -> bool:
    """对应 Go ``Agent.cancelHookRoute``。"""
    router = getattr(agent, "hook_runtime", None)
    return bool(router is not None and router.cancel(event))


async def route_hook(agent: Any, ctx: Any, event: Event) -> tuple[Event, bool, BaseException | None]:
    """把事件交给续链路由器（若已装配）。对应 Go ``Agent.routeHook``。"""
    router = getattr(agent, "hook_runtime", None)
    if router is None:
        return event, False, None
    route_id = router.route_hook_id(event)
    requests = getattr(agent, "requests", None)
    if route_id and requests is not None:
        try:
            _, request_ctx, done = await requests.start(
                ctx,
                StartRequest(
                    parent_id=turn_request_id_from_context(ctx),
                    kind=KindHook,
                    label=route_id + " continuation",
                    scope_key=agent.request_scope_key(ctx),
                ),
            )
        except BaseException:  # noqa: BLE001 - 续链跟踪失败不阻断路由
            pass
        else:
            try:
                return await router.route(request_ctx, event)
            finally:
                done()
    return await router.route(ctx, event)


async def run_hook(agent: Any, ctx: Any, event: Event) -> Event:
    """跑一次 Hook 事件。对应 Go ``Agent.runHook``。"""
    manager = getattr(agent, "hooks", None) or NoopManager()
    event = fill_hook_context(agent, ctx, event)
    try:
        updated = await manager.run(ctx, event)
    except BaseException as exc:  # noqa: BLE001 - 统一走失败通知
        if _is_cancelled(exc):
            raise
        await notify_hook_error(agent, ctx, event, exc)
        await send_hook_failure_notice(agent, ctx, event, exc)
        raise
    return updated


async def notify_hook(agent: Any, ctx: Any, event: Event) -> None:
    """通知类 Hook 点（返回值被忽略）。对应 Go ``Agent.notifyHook``。"""
    manager = getattr(agent, "hooks", None) or NoopManager()
    event = fill_hook_context(agent, ctx, event)
    try:
        await manager.notify(ctx, event)
    except BaseException as exc:  # noqa: BLE001
        # Hook 管理器已经带着规则上下文记过这次失败，这里再记一遍会重复。
        if _is_cancelled(exc):
            return
        if event.point != PointErrorOccurred:
            await notify_hook_error(agent, ctx, event, exc)
            await send_hook_failure_notice(agent, ctx, event, exc)


async def observe_hook_run(
    agent: Any, ctx: Any, event: Event, info: Any
) -> tuple[Any, Any]:
    """把一次 Hook 执行登记成 request。对应 Go ``Agent.observeHookRun``。"""
    requests = getattr(agent, "requests", None)
    if agent is None or requests is None:
        return ctx, (lambda: None)
    session_id = (getattr(event.session, "id", "") or "").strip()
    if not session_id:
        session_id = (getattr(event.request, "session_id", "") or "").strip()
    label = (getattr(info, "name", "") or "").strip()
    if not label:
        label = str(getattr(info, "point", "") or "").strip()
    try:
        _, request_ctx, done = await requests.start(
            ctx,
            StartRequest(
                parent_id=turn_request_id_from_context(ctx),
                session_id=session_id,
                kind=KindHook,
                label=label,
                scope_key=agent.request_scope_key(ctx),
                timeout=getattr(agent, "hook_timeout", 0.0) or 0.0,
            ),
        )
    except BaseException as exc:  # noqa: BLE001
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.warn(
                "hook request tracking failed",
                hook=label,
                point=str(getattr(info, "point", "")),
                error=str(exc),
            )
        return ctx, (lambda: None)
    return request_ctx, done


async def notify_hook_error(agent: Any, ctx: Any, source: Event, err: BaseException) -> None:
    """把一次 Hook 失败再作为 ``error.occurred`` 通知出去。

    对应 Go ``Agent.notifyHookError``。
    """
    if source.point == PointErrorOccurred:
        return
    event = source.clone()
    event.point = PointErrorOccurred
    event.error = safe_error(err)
    event.error_info = _error_payload(redact_error(err))
    await notify_hook(agent, ctx, event)


async def send_hook_failure_notice(agent: Any, ctx: Any, event: Event, err: BaseException) -> None:
    """把 Hook 失败直接告诉用户。对应 Go ``Agent.sendHookFailureNotice``。"""
    if err is None or event.point == PointErrorOccurred or _is_cancelled(err):
        return
    body = hook_failure_notice_text(event, err)
    if not body.strip():
        return
    target = Target()
    if not message_context_from(ctx)[1]:
        target.platform = event.platform.name
        target.scope_id = event.platform.scope_id
    try:
        await agent.send_notice(
            ctx,
            Notice(
                target=target,
                outputs=[text_output(body)],
                level=std_logging.ERROR,
            ),
        )
    except BaseException as exc:  # noqa: BLE001 - 通知失败只记日志
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.warn(
                "hook failure notice failed",
                point=str(event.point),
                error=str(exc),
            )


def hook_failure_notice_text(event: Event, err: BaseException) -> str:
    """对应 Go ``hookFailureNoticeText``。"""
    point = str(event.point or "").strip() or "unknown"
    return "Hook 执行失败（" + point + "）：\n" + trim_hook_notice_text(str(err))


def trim_hook_notice_text(value: str) -> str:
    """对应 Go ``trimHookNoticeText``。"""
    return summarize(value, HOOK_NOTICE_MAX_RUNES)


async def handle_platform_connected(agent: Any, ctx: Any, platform_name: str) -> None:
    """平台连接事实的实际执行体。

    对应 Go ``Agent.handlePlatformConnected``：它跑在 Agent 自己那条按平台的队列上
    （见 :mod:`atri_x.agent.platform_signals`），因此 hook 规则与 cron 补投递不会
    阻塞连接回调。
    """
    try:
        event = await run_hook(
            agent, ctx, Event(point=PointPlatformConnected, platform=PlatformContext(name=platform_name))
        )
    except BaseException:  # noqa: BLE001 - hook 管理器已经记过日志
        return
    if event.outputs:
        try:
            await agent.send_outputs(ctx, event.outputs)
        except BaseException as exc:  # noqa: BLE001
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.warn(
                    "hook notice send failed",
                    point=str(PointPlatformConnected),
                    error=str(exc),
                )


def hook_session(session: Session | None) -> SessionContext:
    """对应 Go ``Agent.hookSession``。"""
    if session is None:
        return SessionContext()
    return SessionContext(
        id=session.id, mode=session.mode, title=session.title, status=session.status
    )


def fill_hook_context(agent: Any, ctx: Any, event: Event) -> Event:
    """补齐 Hook 事件的平台/发言人/消息上下文。对应 Go ``Agent.fillHookContext``。"""
    media = getattr(agent, "media", None)
    if media is not None:
        event.media = media
    current = agent.actor(ctx)
    platform = getattr(agent, "platform", None)
    platform_name = platform.name() if platform is not None else ""
    scope_id = getattr(agent, "scope_id", "local")
    msg, ok = message_context_from(ctx)
    if ok:
        if msg.platform:
            platform_name = msg.platform
        if msg.scope_id:
            scope_id = msg.scope_id
        if not event.platform.message_id:
            event.platform.message_id = msg.platform_message_id
        if not event.platform.reply_to_message_id:
            event.platform.reply_to_message_id = msg.reply_to_message_id
        if not event.message.platform_text:
            event.message.platform_text = msg.raw_text
        if event.point == PointPlatformMessageReceived and not event.message.platform_message and msg.platform_message:
            event.message.platform_message = msg.platform_message
        if event.message.reply is None and msg.reply.message_id:
            reply_segments = platform_segments_to_llm(msg.reply.segments, msg.reply.text)
            event.message.reply = MessageReplyPayload(
                message_id=msg.reply.message_id,
                sender_id=msg.reply.sender_id,
                text=llm_segments_text_only(reply_segments),
                display_text=_segments_content_text(reply_segments),
                segments=reply_segments,
            )
    if not event.message.intent_text and event.message.role == RoleUser:
        event.message.intent_text = agent.strip_wakeup_prefix(
            ctx, llm_segments_text_only(event.message.segments)
        )
    if not event.platform.name:
        event.platform.name = platform_name
    if not event.platform.scope_id:
        event.platform.scope_id = scope_id
    if not event.platform.user_id:
        event.platform.user_id = current.platform_user_id
    if not event.actor.id:
        event.actor = actor_context(current)
    elif not event.actor.display_name:
        event.actor.display_name = current.display_name
    return event


def log_hook_error(agent: Any, point: str, err: BaseException | None) -> None:
    """对应 Go ``Agent.logHookError``。"""
    if err is None:
        return
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    if _is_cancelled(err):
        logger.info("hook canceled", point=str(point), error=str(err))
        return
    logger.warn("hook error", point=str(point), error=str(err))


def actor_context(current: Any) -> ActorContext:
    """``security.Actor`` → ``hook.ActorContext``。对应 Go ``actorContext``。"""
    return ActorContext(
        id=current.id,
        role=str(current.role),
        group_role=str(current.group_role),
        user_id=current.platform_user_id,
        nickname=current.nickname,
        group_card=current.group_card,
        display_name=current.display_name,
    )


def _error_payload(message: str) -> Any:
    from atri_x.hook import ErrorPayload

    return ErrorPayload(message=message)


def _segments_content_text(segments: list[Any]) -> str:
    from atri_x.llm import segments_content_text

    return segments_content_text(segments)


def _is_cancelled(err: BaseException) -> bool:
    from atri_x.agent.errors import is_cancellation

    return is_cancellation(err)


# 便于交叉检索：Go 的 ``LLMPayload`` / ``MessagePayload`` 在 agent.hooks 里被构造，
# 这里再导出一次，调用点不需要同时 import 两个包。
__all__ += ["LLMPayload", "MessagePayload", "Output"]
