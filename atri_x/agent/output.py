"""出站：发送管理器的装配、部分失败记账与平台消息映射。

对应 Go：``internal/agent/output.go``。

Go 的 ``(Receipt, error)`` → Python 返回 :class:`~atri_x.delivery.delivery.Receipt`，
失败时抛 :class:`~atri_x.delivery.delivery.DeliveryError`，它带着"已经成功投递的
那一部分"（``err.receipt``），调用方不会丢失 `哪些已经发出去了`。
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.delivery import (
    ContextSender,
    DeliveryError,
    MessageSender,
    Notice,
    Output,
    Receipt,
    Target,
    text as text_output,
)
from atri_x.hook import Event, MessagePayload, PointAgentOutputPrepared, PointPlatformMessageSent
from atri_x.llm import RoleAssistant, text_segments
from atri_x.llm import segments_text_only as llm_segments_text_only
from atri_x.logging import ResultFailed
from atri_x.platform import message_context_from
from atri_x.storage import PlatformMessageMap

from atri_x.agent.logging import audit
from atri_x.agent.turn_output import buffer_assistant_output

__all__ = [
    "AgentOutputSender",
    "ContextTextSender",
    "NoticeDispatch",
    "map_sent_assistant_message",
    "notice_dispatch",
    "notice_platform_name",
    "prepare_assistant_output",
    "register_platform_sender",
    "send_chat",
    "send_chat_with_receipt",
    "send_notice",
    "send_notice_public",
    "send_outputs",
    "set_output_manager",
]


class NoticeDispatch:
    """一次通知的落点：队列按平台分条，串行键按目标分。

    对应 Go ``noticeDispatch``。``send`` 是 ``async (ctx, notice) -> Receipt``。
    """

    __slots__ = ("platform", "send")

    def __init__(self, platform: str, send: Callable[..., Any]) -> None:
        self.platform = platform
        self.send = send


class AgentOutputSender:
    """把交付层的发送请求转成 Agent 的实际发送。对应 Go ``agentOutputSender``。"""

    __slots__ = ("agent", "ctx")

    def __init__(self, agent: Any, ctx: Any) -> None:
        self.agent = agent
        self.ctx = ctx

    async def send_chat(self, ctx: Any, outputs: list[Output]) -> Receipt:
        if self.agent is None:
            raise RuntimeError("agent output sender is not configured")

        async def _send(resolved: list[Output]) -> Receipt:
            msg, ok = message_context_from(self.ctx)
            if ok and msg.sender is not None:
                return await msg.sender.send_chat(self.ctx, resolved)
            platform = getattr(self.agent, "platform", None)
            if platform is None:
                raise RuntimeError("chat output sender is not configured")
            return await platform.send_chat(self.ctx, resolved)

        return await self.agent.send_prepared_media(self.ctx, outputs, _send)

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        outputs = list(notice.outputs)
        if self.agent is None:
            raise RuntimeError("agent output sender is not configured")
        dispatch = notice_dispatch(self.agent, self.ctx, notice.target)

        async def _run(run_ctx: Any) -> Receipt:
            async def _send(resolved: list[Output]) -> Receipt:
                out = Notice(target=notice.target, outputs=resolved, level=notice.level)
                return await dispatch.send(run_ctx, out)

            return await self.agent.send_prepared_media(run_ctx, outputs, _send)

        try:
            return await self.agent.notice_queues.send(
                self.ctx, dispatch.platform, notice, _run
            )
        except Exception as exc:  # noqa: BLE001 - 队列满是背压，不是故障
            from atri_x.signal import ErrQueueFull

            logger = getattr(self.agent, "logger", None)
            if exc is ErrQueueFull and logger is not None:
                logger.warn(
                    "notice queue full", platform=dispatch.platform, error=str(exc)
                )
            raise


class ContextTextSender:
    """把"当前会话的 sender"固定成契约里的 ``ContextSender``。

    对应 Go ``contextTextSender``。
    """

    __slots__ = ("ctx", "sender")

    def __init__(self, ctx: Any, sender: ContextSender) -> None:
        self.ctx = ctx
        self.sender = sender

    async def send_chat(self, ctx: Any, outputs: list[Output]) -> Receipt:
        return await self.sender.send_chat(self.ctx, outputs)

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        return await self.sender.send_notice(self.ctx, notice)


def set_output_manager(agent: Any, manager: Any) -> None:
    """对应 Go ``Agent.SetOutputManager``。"""
    agent.outputs = manager


def notice_platform_name(name: str) -> str:
    """给"没有平台名的当前会话发送"一条稳定的队列名。对应 Go ``noticePlatformName``。"""
    return (name or "").strip() or "context"


def notice_dispatch(agent: Any, ctx: Any, target: Target) -> NoticeDispatch:
    """解析这次通知该发给谁。对应 Go ``Agent.noticeDispatch``。

    解析放在入队之前：平台没配好就不必占队列。
    """
    if target.empty():
        msg, ok = message_context_from(ctx)
        if ok and msg.sender is not None:
            return NoticeDispatch(
                platform=notice_platform_name(msg.platform), send=msg.sender.send_notice
            )
    platform_name = (target.platform or "").strip()
    if not platform_name:
        msg, ok = message_context_from(ctx)
        if ok:
            platform_name = msg.platform
    if not platform_name:
        platform = getattr(agent, "platform", None)
        if platform is not None:
            platform_name = platform.name()
    if not platform_name:
        raise RuntimeError("notice target platform is not configured")
    sender = getattr(agent, "platform_senders", {}).get(platform_name)
    if sender is None:
        raise RuntimeError(f"target platform {platform_name!r} is not configured")

    async def _send(send_ctx: Any, notice: Notice) -> Receipt:
        notice.target.platform = platform_name
        return await sender.send_notice(send_ctx, notice)

    return NoticeDispatch(platform=platform_name, send=_send)


async def send_outputs(agent: Any, ctx: Any, outputs: list[Output] | None) -> None:
    """发送一批输出意图。对应 Go ``Agent.sendOutputs``。"""
    if not agent.turn_output_allowed(ctx):
        return
    if not outputs:
        return
    manager = agent.outputs
    manager.sender = AgentOutputSender(agent, ctx)
    if manager.logger is None:
        manager.logger = getattr(agent, "logger", None)
    try:
        await manager.send_notices_with_receipt(ctx, outputs)
    except DeliveryError as exc:
        receipt = exc.receipt
        if receipt.platform_message_ids:
            audit(
                agent,
                "platform_send_partial",
                "operation",
                "send_outputs",
                "platform_message_count",
                len(receipt.platform_message_ids),
                "error",
                str(exc),
                "result",
                ResultFailed,
            )
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.warn(
                    "output send failed with partial receipt",
                    error=str(exc),
                    platform_message_count=len(receipt.platform_message_ids),
                )
        raise


async def send_chat(agent: Any, ctx: Any, text: str) -> None:
    """发一条聊天消息，忽略凭据。对应 Go ``Agent.sendChat``。"""
    try:
        await send_chat_with_receipt(agent, ctx, text)
    except DeliveryError:
        pass


async def send_notice(agent: Any, ctx: Any, notice: Notice) -> None:
    """对应 Go ``Agent.sendNotice``。"""
    await agent.send_notice(ctx, notice)


async def prepare_assistant_output(agent: Any, ctx: Any, point: str, text: str) -> str:
    """让输出 Hook 改写助手正文。对应 Go ``Agent.prepareAssistantOutput``。"""
    event = await agent.run_hook(
        ctx,
        Event(
            point=point,
            message=MessagePayload(role=RoleAssistant, segments=text_segments(text)),
        ),
    )
    return llm_segments_text_only(event.message.segments)


async def send_chat_with_receipt(agent: Any, ctx: Any, text: str) -> Receipt:
    """发一条聊天消息并返回凭据。对应 Go ``Agent.sendChatWithReceipt``。

    失败时抛 :class:`DeliveryError`，其 ``receipt`` 是已成功投递的部分。
    """
    if not agent.turn_output_allowed(ctx):
        return Receipt()
    if not (text or "").strip() and buffer_assistant_output(ctx):
        return Receipt()
    prepared = await prepare_assistant_output(agent, ctx, PointAgentOutputPrepared, text)
    manager = agent.outputs
    manager.sender = AgentOutputSender(agent, ctx)
    if manager.logger is None:
        manager.logger = getattr(agent, "logger", None)
    try:
        receipt = await manager.send_chat(ctx, [text_output(prepared)])
    except DeliveryError as exc:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            partial = "yes" if exc.receipt.platform_message_ids else ""
            logger.warn("chat send failed", error=str(exc), partial_receipt=partial)
        raise DeliveryError(str(exc), exc.receipt.mark_partial_failure(exc)) from exc
    await agent.notify_hook(
        ctx,
        Event(
            point=PointPlatformMessageSent,
            message=MessagePayload(role=RoleAssistant, segments=text_segments(prepared)),
        ),
    )
    return receipt


def map_sent_assistant_message(
    agent: Any, ctx: Any, session_id: str, message_id: str, receipt: Receipt
) -> None:
    """把发送出去的平台消息 id 映射回本地消息。对应 Go ``Agent.mapSentAssistantMessage``。"""
    store = getattr(agent, "store", None)
    if not receipt.platform_message_ids or store is None or store.messages() is None:
        return
    scope_value = agent.scope(ctx)
    for platform_message_id in receipt.platform_message_ids:
        trimmed = (platform_message_id or "").strip()
        if not trimmed:
            continue
        mapping = PlatformMessageMap(
            platform=scope_value.platform,
            platform_scope_id=scope_value.platform_scope_id,
            platform_message_id=trimmed,
            message_id=message_id,
            session_id=session_id,
        )
        try:
            store.messages().map_platform_message(mapping)
        except Exception as exc:  # noqa: BLE001 - 映射失败不能让回合失败
            audit(
                agent,
                "persistence_error",
                "session_id",
                session_id,
                "operation",
                "map_platform_message",
                "platform_message_id",
                trimmed,
                "error",
                str(exc),
                "result",
                ResultFailed,
            )
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.warn(
                    "map platform message failed",
                    session_id=session_id,
                    platform_message_id=trimmed,
                    error=str(exc),
                )


def register_platform_sender(agent: Any, name: str, sender: MessageSender | None) -> None:
    """注册一个平台的发送者。对应 Go ``Agent.RegisterPlatformSender``。"""
    trimmed = (name or "").strip()
    if not trimmed or sender is None:
        return
    if getattr(agent, "platform_senders", None) is None:
        agent.platform_senders = {}
    agent.platform_senders[trimmed] = sender


async def send_notice_public(agent: Any, ctx: Any, notice: Notice) -> Receipt:
    """``Agent.SendNotice`` 的自由函数实现（供 core.py 的薄转发用）。"""
    if not agent.turn_output_allowed(ctx):
        return Receipt()
    if not agent.group_runtime_notice_bypass(ctx) and agent.notice_target_blocked(
        notice.target
    ):
        return Receipt()
    manager = agent.outputs
    manager.sender = AgentOutputSender(agent, ctx)
    if manager.logger is None:
        manager.logger = getattr(agent, "logger", None)
    return await manager.send_notice(ctx, notice)
