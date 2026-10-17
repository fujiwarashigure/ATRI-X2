"""入站消息总入口：命令派发与聊天。

对应 Go：``internal/agent/message.go``。

它是平台适配器唯一需要调用的聊天入口：先做群运行态闸门、唤醒判定、Hook 链
（``platform.message.received``）与媒体物化，再交给命令执行器或聊天路径。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info
from atri_x.hook import (
    Event,
    MessagePayload,
    PointAgentInputPrepared,
    PointPlatformMessageReceived,
)
from atri_x.llm import RoleUser, segments_text_only, text_segments
from atri_x.logging import ResultFailed
from atri_x.security import with_actor, with_policy

from atri_x.agent.errors import (
    UserNotifiedError,
    is_cancellation,
    new_user_error_details,
    should_notify_user_error,
)
from atri_x.agent.logging import audit_error
from atri_x.agent.segments import (
    inbound_context_segments,
    inbound_segments,
    replace_inbound_text_segments,
    with_inbound_segments,
)
from atri_x.agent.wakeup import with_message_wakeup

__all__ = [
    "CommandInfos",
    "command_infos",
    "handle_message",
    "is_cancellation",
    "should_notify_user_error",
    "text_segments",
    "user_notified_error",
]


def command_infos(agent: Any) -> list[Info]:
    """当前可用命令清单。对应 Go ``Agent.CommandInfos``。"""
    if agent is None or getattr(agent, "commands", None) is None:
        return []
    return agent.commands.commands()


#: Go 的 ``CommandInfos`` 是 Agent 的方法；这里保留 PascalCase 别名便于交叉检索。
CommandInfos = command_infos


def user_notified_error(err: BaseException) -> UserNotifiedError:
    """把错误包成"已通知用户"。对应 Go ``userNotifiedError`` 构造。"""
    return UserNotifiedError(err)


async def handle_message(agent: Any, ctx: Any, text: str) -> None:
    """分派命令与聊天消息。对应 Go ``Agent.HandleMessage``。"""
    if agent is None:
        return
    if agent.group_runtime_blocked(ctx):
        return
    current = agent.actor(ctx)
    ctx = with_policy(with_actor(ctx, current), agent.security_policy)
    segments = inbound_segments(ctx, text)
    try:
        await _handle_message_inner(agent, ctx, text, current, segments)
    except BaseException as exc:  # noqa: BLE001 - 统一错误呈现
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        details = new_user_error_details("请求失败", exc)
        audit_error(
            agent,
            "message_error",
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
                point=PointAgentInputPrepared,
                actor=agent.actor_context(current),
                message=MessagePayload(role=RoleUser, segments=segments),
            ),
            Exception(details.safe),
        )
        if should_notify_user_error(exc):
            await agent.send_chat(ctx, details.text)
        raise


async def _handle_message_inner(
    agent: Any, ctx: Any, text: str, current: Any, segments: list[Any]
) -> None:
    woken = agent.message_wakeup(ctx, segments_text_only(segments))
    ctx = with_message_wakeup(ctx, woken)
    if agent.is_cancel_command(segments_text_only(segments)):
        cancel_event = agent.fill_hook_context(
            ctx,
            Event(
                point=PointPlatformMessageReceived,
                actor=agent.actor_context(current),
                message=MessagePayload(role=RoleUser, segments=list(segments)),
            ),
        )
        if agent.cancel_hook_route(cancel_event):
            await agent.send_chat(ctx, "已取消当前 Hook 会话。")
            return
    event = agent.fill_hook_context(
        ctx,
        Event(
            point=PointPlatformMessageReceived,
            actor=agent.actor_context(current),
            message=MessagePayload(role=RoleUser, segments=list(segments)),
        ),
    )
    router = getattr(agent, "hook_runtime", None)
    waiting = router is not None and bool(router.route_hook_id(event))
    if woken or waiting:
        ctx = await agent.materialize_platform_media(ctx)
        ctx = await agent.transcribe_platform_audio(ctx)
        segments = inbound_segments(ctx, text)
        event = agent.fill_hook_context(
            ctx,
            Event(
                point=PointPlatformMessageReceived,
                actor=agent.actor_context(current),
                message=MessagePayload(role=RoleUser, segments=list(segments)),
            ),
        )
    event, routed, route_err = await agent.route_hook(ctx, event)
    if route_err is not None:
        raise route_err
    if not routed or not event.control.stop_propagation:
        event = await agent.run_hook(ctx, event)
    if event.outputs:
        await agent.send_outputs(ctx, event.outputs)
    if event.control.consume:
        return
    hook_changed_segments = not _segments_equal(event.message.segments, segments)
    if hook_changed_segments:
        segments = list(event.message.segments)
    else:
        segments = inbound_context_segments(ctx, text)
    ctx = with_inbound_segments(ctx, segments)
    text = segments_text_only(segments)
    if agent.is_cancel_command(text) and agent.cancel_hook_route(event):
        await agent.send_chat(ctx, "已取消当前 Hook 会话。")
        return
    if not woken:
        return
    text = agent.strip_wakeup_prefix(ctx, text)
    segments = replace_inbound_text_segments(ctx, text)
    ctx = with_inbound_segments(ctx, segments)
    from atri_x.agent.input import has_fork_from_message

    if not has_fork_from_message(ctx):
        await agent.expire_idle_current_session(ctx)
    handled, err = await agent.command_executor.handle(ctx, text)
    if handled:
        if err is not None:
            raise err
        return
    await agent.handle_input(ctx, text)


def _segments_equal(left: list[Any], right: list[Any]) -> bool:
    """段列表的值相等判定（对应 Go 的 ``reflect.DeepEqual``）。"""
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a is b:
            continue
        if a.type != b.type or a.text != b.text or a.url != b.url:
            return False
        if getattr(a, "media", "") != getattr(b, "media", ""):
            return False
        if getattr(a, "mime_type", "") != getattr(b, "mime_type", ""):
            return False
        if getattr(a, "name", "") != getattr(b, "name", ""):
            return False
    return True
