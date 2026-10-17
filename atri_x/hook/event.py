"""Hook 事件点、事件 payload 与 Handler 基础类型。

对应 Go：``internal/hook/event.go``。

一个 ``Event`` 会在某个 :class:`Point` 上传给 Hook handler；哪些字段被填充取决于
该事件点，handler 只应依赖与当前点相关的字段。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from atri_x.llm.llm import (
    LLMMessage,
    MessageSegment,
    ToolCallRequest,
    ToolSchema,
    Usage,
)
from atri_x.llm.segment import segments_text_only


class Point:
    """稳定的 Hook 事件点。对应 Go ``Point``。

    用字符串常量而非 ``Enum``：既是 JSON 值，也要能直接和配置里的字符串比较。
    """

    PLATFORM_CONNECTED = "platform.connected"
    PLATFORM_MESSAGE_RECEIVED = "platform.message.received"
    AGENT_INPUT_PREPARED = "agent.input.prepared"
    LLM_TURN_PREPARED = "llm.turn.prepared"
    LLM_REQUEST_PREPARED = "llm.request.prepared"
    LLM_RESPONSE_RECEIVED = "llm.response.received"
    TOOL_CALL_PREPARED = "tool.call.prepared"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    AGENT_OUTPUT_PREPARED = "agent.output.prepared"
    AGENT_TURN_OUTPUT_PREPARED = "agent.turn.output.prepared"
    PLATFORM_MESSAGE_SENT = "platform.message.sent"
    ERROR_OCCURRED = "error.occurred"

    ALL = (
        PLATFORM_CONNECTED,
        PLATFORM_MESSAGE_RECEIVED,
        AGENT_INPUT_PREPARED,
        LLM_TURN_PREPARED,
        LLM_REQUEST_PREPARED,
        LLM_RESPONSE_RECEIVED,
        TOOL_CALL_PREPARED,
        TOOL_CALL_COMPLETED,
        AGENT_OUTPUT_PREPARED,
        AGENT_TURN_OUTPUT_PREPARED,
        PLATFORM_MESSAGE_SENT,
        ERROR_OCCURRED,
    )


# Go 侧常量名（保留以便交叉检索）
PointPlatformConnected = Point.PLATFORM_CONNECTED
PointPlatformMessageReceived = Point.PLATFORM_MESSAGE_RECEIVED
PointAgentInputPrepared = Point.AGENT_INPUT_PREPARED
PointLLMTurnPrepared = Point.LLM_TURN_PREPARED
PointLLMRequestPrepared = Point.LLM_REQUEST_PREPARED
PointLLMResponseReceived = Point.LLM_RESPONSE_RECEIVED
PointToolCallPrepared = Point.TOOL_CALL_PREPARED
PointToolCallCompleted = Point.TOOL_CALL_COMPLETED
PointAgentOutputPrepared = Point.AGENT_OUTPUT_PREPARED
PointAgentTurnOutputPrepared = Point.AGENT_TURN_OUTPUT_PREPARED
PointPlatformMessageSent = Point.PLATFORM_MESSAGE_SENT
PointErrorOccurred = Point.ERROR_OCCURRED

#: 一个 hook.v2 JSON Lines 帧的最大字节数。对应 Go ``MaxProtocolFrameBytes``。
MAX_PROTOCOL_FRAME_BYTES = 16 * 1024 * 1024


def known_point(point: str) -> bool:
    """对应 Go ``KnownPoint``。"""
    return point in Point.ALL


@dataclass
class Control:
    """对应 Go ``Control``。"""

    consume: bool = False
    stop_propagation: bool = False


@dataclass
class ErrorPayload:
    """对应 Go ``ErrorPayload``。"""

    message: str = ""


@dataclass
class PlatformContext:
    """对应 Go ``PlatformContext``。"""

    name: str = ""
    scope_id: str = ""
    user_id: str = ""
    conversation_id: str = ""
    message_id: str = ""
    reply_to_message_id: str = ""


@dataclass
class ActorContext:
    """对应 Go ``ActorContext``。"""

    id: str = ""
    role: str = ""
    group_role: str = ""
    user_id: str = ""
    nickname: str = ""
    group_card: str = ""
    display_name: str = ""


@dataclass
class SessionContext:
    """对应 Go ``SessionContext``。"""

    id: str = ""
    mode: str = ""
    title: str = ""
    status: str = ""


@dataclass
class RequestContext:
    """对应 Go ``RequestContext``。"""

    id: str = ""
    kind: str = ""
    session_id: str = ""
    phase: str = ""


@dataclass
class MessageReplyPayload:
    """对应 Go ``MessageReplyPayload``。"""

    message_id: str = ""
    sender_id: str = ""
    text: str = ""
    display_text: str = ""
    segments: list[MessageSegment] = field(default_factory=list)


@dataclass
class MessagePayload:
    """对应 Go ``MessagePayload``。"""

    id: str = ""
    role: str = ""
    platform_text: str = ""
    #: Go 是 ``json.RawMessage``：原样透传平台消息体，不做解释。
    platform_message: Any = None
    intent_text: str = ""
    reply: MessageReplyPayload | None = None
    segments: list[MessageSegment] = field(default_factory=list)
    messages: list[LLMMessage] = field(default_factory=list)


@dataclass
class LLMPayload:
    """对应 Go ``LLMPayload``。

    ``system_append`` / ``temperature`` / ``max_tokens`` / ``extra_body`` 只会在
    ``llm.turn.prepared`` 与 ``llm.request.prepared`` 被消费，**永不写入会话历史**。
    """

    provider: str = ""
    model: str = ""
    messages: list[LLMMessage] = field(default_factory=list)
    tools: list[ToolSchema] = field(default_factory=list)
    usage: Usage | None = None
    source_text: str = ""
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    elapsed_ms: int = 0
    system_append: list[str] = field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolPayload:
    """对应 Go ``ToolPayload``。"""

    id: str = ""
    name: str = ""
    arguments: str = ""
    risk: str = ""
    result: str = ""
    error: BaseException | None = None


@dataclass
class Event:
    """Hook 点上的上下文。对应 Go ``Event``。

    ``media`` 与 ``error`` 在 Go 侧标记 ``json:"-"``，序列化时**不**输出
    （``error`` 会投影成 ``error_info``）。
    """

    #: 宿主 Media API（进程内 Go Hook 用）。不参与 JSON。
    media: Any = None
    id: str = ""
    point: str = ""
    time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    control: Control = field(default_factory=Control)

    platform: PlatformContext = field(default_factory=PlatformContext)
    actor: ActorContext = field(default_factory=ActorContext)
    session: SessionContext = field(default_factory=SessionContext)
    request: RequestContext = field(default_factory=RequestContext)
    message: MessagePayload = field(default_factory=MessagePayload)
    llm: LLMPayload = field(default_factory=LLMPayload)
    tool: ToolPayload = field(default_factory=ToolPayload)
    outputs: list[Any] = field(default_factory=list)  # list[delivery.Output]
    error: BaseException | None = None
    error_info: ErrorPayload | None = None

    # -- 便捷 ---------------------------------------------------------------
    def error_message(self) -> str:
        """对应 Go ``EventErrorMessage``。"""
        if self.error_info is not None:
            return self.error_info.message
        if self.error is not None:
            return str(self.error)
        return ""

    def message_intent_text(self) -> str:
        """对应 Go ``MessageIntentText``。"""
        trimmed = (self.message.intent_text or "").strip()
        if trimmed:
            return trimmed
        return segments_text_only(self.message.segments)

    def clone(self) -> "Event":
        """深拷贝：Hook 对 ``llm.messages`` 只能读，宿主交给 handler 的必须是副本。"""
        return dataclasses.replace(
            self,
            metadata=dict(self.metadata),
            control=dataclasses.replace(self.control),
            platform=dataclasses.replace(self.platform),
            actor=dataclasses.replace(self.actor),
            session=dataclasses.replace(self.session),
            request=dataclasses.replace(self.request),
            message=dataclasses.replace(
                self.message,
                reply=(
                    dataclasses.replace(
                        self.message.reply,
                        segments=[s.clone() for s in self.message.reply.segments],
                    )
                    if self.message.reply
                    else None
                ),
                segments=[s.clone() for s in self.message.segments],
                messages=_clone_messages(self.message.messages),
            ),
            llm=dataclasses.replace(
                self.llm,
                messages=_clone_messages(self.llm.messages),
                tools=list(self.llm.tools),
                usage=dataclasses.replace(self.llm.usage) if self.llm.usage else None,
                tool_calls=[dataclasses.replace(tc) for tc in self.llm.tool_calls],
                system_append=list(self.llm.system_append),
                extra_body=dict(self.llm.extra_body),
            ),
            tool=dataclasses.replace(self.tool),
            outputs=list(self.outputs),
            error_info=(
                dataclasses.replace(self.error_info) if self.error_info else None
            ),
        )


def _clone_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    return [
        LLMMessage(
            role=m.role,
            segments=[s.clone() for s in m.segments],
            name=m.name,
            tool_call_id=m.tool_call_id,
            tool_calls=[dataclasses.replace(tc) for tc in m.tool_calls],
        )
        for m in messages
    ]


def prepare_event(event: Event) -> Event:
    """补全 ``time`` / ``metadata`` / ``error_info``。对应 Go ``prepareEvent``。

    Go 是值语义（就地改副本）；Python 就地改传入对象并返回，调用方应传入副本。
    """
    if event.time is None:
        event.time = datetime.now(timezone.utc)
    if event.metadata is None:
        event.metadata = {}
    if event.error_info is None and event.error is not None:
        event.error_info = ErrorPayload(message=str(event.error))
    return event


@runtime_checkable
class Handler(Protocol):
    """处理一个 Hook 事件、可返回更新后的事件。对应 Go ``Handler``。"""

    async def handle_hook(self, ctx: Any, event: Event) -> Event: ...


class HandlerFunc:
    """把一个普通函数适配成 :class:`Handler`。对应 Go ``HandlerFunc``。

    支持同步与异步函数：
    ``HandlerFunc(lambda ctx, ev: ev)`` 与 ``HandlerFunc(async_fn)`` 都可以。
    """

    __slots__ = ("_fn",)

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def handle_hook(self, ctx: Any, event: Event) -> Event:
        import inspect

        result = self._fn(ctx, event)
        if inspect.isawaitable(result):
            result = await result
        return result

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        name = getattr(self._fn, "__name__", repr(self._fn))
        return f"HandlerFunc({name})"


__all__ = [name for name in dir() if not name.startswith("_")]
