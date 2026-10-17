"""回合输出端口：前台流式输出与后台静默输出。

对应 Go：``internal/agent/turn_output.go``。

Go 的 ``turnOutput`` 接口 → :class:`TurnOutput` 协议。前台实现把消息发给平台，
后台实现只记录运行状态——后台回合不得产生任何用户可见输出。
"""

from __future__ import annotations

import logging as std_logging
from typing import Any, Protocol, runtime_checkable

from atri_x.delivery import (
    DELIVERY_AFTER_ASSISTANT,
    MessageStream,
    Notice,
    Output,
    Receipt,
    StreamingMessageSender,
    TOOL_PREVIEW_PREFIX,
    split_by_delivery_timing,
    text as text_output,
)
from atri_x.hook import (
    Event,
    MessagePayload,
    PointAgentOutputPrepared,
    PointPlatformMessageSent,
)
from atri_x.llm import RoleAssistant, text_segments
from atri_x.llm import segments_text_only as llm_segments_text_only
from atri_x.platform import message_context_from
from atri_x.runtime import Snapshot

__all__ = [
    "BackgroundTurnOutput",
    "ForegroundTurnOutput",
    "TurnOutput",
    "buffer_assistant_output",
    "finish_intermediate_output",
    "format_tool_preview",
    "replace_and_finish_stream",
    "replace_stream_output",
    "send_preview",
    "send_text_notice",
    "start_message_stream",
]


@runtime_checkable
class TurnOutput(Protocol):
    """一个回合的输出端口。对应 Go ``turnOutput``。"""

    def start_stream(self, ctx: Any) -> MessageStream | None: ...
    async def finish_intermediate(
        self, ctx: Any, stream_ctx: Any, stream: MessageStream | None, text: str, streaming: bool
    ) -> None: ...
    async def replace_and_finish_stream(
        self, ctx: Any, stream_ctx: Any, stream: MessageStream | None, text: str
    ) -> Receipt: ...
    async def send_assistant(self, ctx: Any, text: str) -> Receipt: ...
    async def send_outputs(self, ctx: Any, outputs: list[Output] | None) -> None: ...
    async def send_notice(self, ctx: Any, level: int, text: str) -> None: ...
    async def send_preview(self, ctx: Any, text: str) -> None: ...
    async def send_reasoning(self, ctx: Any, text: str) -> None: ...
    async def publish_runtime_status(self, ctx: Any, snapshot: Snapshot) -> None: ...


class ForegroundTurnOutput:
    """前台回合输出：真正发消息。对应 Go ``foregroundTurnOutput``。"""

    __slots__ = ("agent",)

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def start_stream(self, ctx: Any) -> MessageStream | None:
        return start_message_stream(self.agent, ctx)

    async def finish_intermediate(
        self, ctx: Any, stream_ctx: Any, stream: MessageStream | None, text: str, streaming: bool
    ) -> None:
        if not self.agent.turn_output_allowed(stream_ctx):
            return
        await finish_intermediate_output(self.agent, ctx, stream_ctx, stream, text, streaming)

    async def replace_and_finish_stream(
        self, ctx: Any, stream_ctx: Any, stream: MessageStream | None, text: str
    ) -> Receipt:
        if not self.agent.turn_output_allowed(stream_ctx):
            return Receipt()
        return await replace_and_finish_stream(self.agent, ctx, stream_ctx, stream, text)

    async def send_assistant(self, ctx: Any, text: str) -> Receipt:
        return await self.agent.send_chat_with_receipt(ctx, text)

    async def send_outputs(self, ctx: Any, outputs: list[Output] | None) -> None:
        await self.agent.send_outputs(ctx, outputs)

    async def send_notice(self, ctx: Any, level: int, text: str) -> None:
        await send_text_notice(self.agent, ctx, level, text)

    async def send_preview(self, ctx: Any, text: str) -> None:
        await send_preview(self.agent, ctx, text)

    async def send_reasoning(self, ctx: Any, text: str) -> None:
        await self.agent.send_cli_reasoning(ctx, text)

    async def publish_runtime_status(self, ctx: Any, snapshot: Snapshot) -> None:
        self.agent.publish_runtime_status(ctx, snapshot)


class BackgroundTurnOutput:
    """后台回合输出：全部丢弃，只留运行状态。对应 Go ``backgroundTurnOutput``。"""

    __slots__ = ("agent",)

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def start_stream(self, ctx: Any) -> MessageStream | None:
        return None

    async def finish_intermediate(
        self, ctx: Any, stream_ctx: Any, stream: MessageStream | None, text: str, streaming: bool
    ) -> None:
        return None

    async def replace_and_finish_stream(
        self, ctx: Any, stream_ctx: Any, stream: MessageStream | None, text: str
    ) -> Receipt:
        return Receipt()

    async def send_assistant(self, ctx: Any, text: str) -> Receipt:
        return Receipt()

    async def send_outputs(self, ctx: Any, outputs: list[Output] | None) -> None:
        return None

    async def send_notice(self, ctx: Any, level: int, text: str) -> None:
        return None

    async def send_preview(self, ctx: Any, text: str) -> None:
        return None

    async def send_reasoning(self, ctx: Any, text: str) -> None:
        return None

    async def publish_runtime_status(self, ctx: Any, snapshot: Snapshot) -> None:
        self.agent.record_runtime_status(snapshot)


async def send_text_notice(agent: Any, ctx: Any, level: int, text: str) -> None:
    """发一条纯文本通知。对应 Go ``Agent.sendTextNotice``。"""
    body = (text or "").strip()
    if not body:
        return
    await agent.send_notice(ctx, Notice(outputs=[text_output(body)], level=level))


async def send_preview(agent: Any, ctx: Any, text: str) -> None:
    """发一条工具进度预览（带 ``[tool] `` 前缀）。对应 Go ``Agent.sendPreview``。"""
    body = (text or "").strip()
    if not body:
        return
    event = await agent.run_hook(
        ctx,
        Event(
            point=PointAgentOutputPrepared,
            message=MessagePayload(role=RoleAssistant, segments=text_segments(body)),
        ),
    )
    rendered = llm_segments_text_only(event.message.segments).strip()
    if not rendered:
        return
    preview = format_tool_preview(rendered)
    await agent.send_notice(
        ctx, Notice(outputs=[text_output(preview)], level=std_logging.DEBUG)
    )
    await agent.notify_hook(
        ctx,
        Event(
            point=PointPlatformMessageSent,
            message=MessagePayload(role=RoleAssistant, segments=text_segments(preview)),
        ),
    )


def format_tool_preview(text: str) -> str:
    """给预览正文逐行加 ``[tool] `` 前缀。对应 Go ``formatToolPreview``。"""
    lines = (text or "").strip().split("\n")
    return "\n".join(TOOL_PREVIEW_PREFIX + line.strip() for line in lines)


async def finish_intermediate_output(
    agent: Any,
    ctx: Any,
    stream_ctx: Any,
    stream: MessageStream | None,
    text: str,
    streaming: bool,
) -> None:
    """结束一次"中途"（后面还有工具调用）的输出。对应 Go ``finishIntermediateOutput``。"""
    if streaming and stream is not None:
        if (text or "").strip():
            await replace_stream_output(agent, ctx, stream_ctx, stream, text)
        await stream.finish(stream_ctx)
        return
    if not (text or "").strip():
        return
    await agent.send_chat_with_receipt(ctx, text)


async def replace_and_finish_stream(
    agent: Any,
    ctx: Any,
    stream_ctx: Any,
    stream: MessageStream | None,
    text: str,
) -> Receipt:
    """把流式消息整体替换成终态文本并收尾。对应 Go ``replaceAndFinishStream``。"""
    if stream is None:
        return await agent.send_chat_with_receipt(ctx, text)
    prepared = await agent.prepare_assistant_output(ctx, PointAgentOutputPrepared, text)
    receipt = await stream.replace(stream_ctx, prepared)
    finish_receipt = await stream.finish(stream_ctx)
    if not receipt.platform_message_ids:
        receipt = finish_receipt
    return receipt


async def replace_stream_output(
    agent: Any,
    ctx: Any,
    stream_ctx: Any,
    stream: MessageStream,
    text: str,
) -> None:
    """只替换流式正文（不收尾）。对应 Go ``replaceStreamOutput``。"""
    prepared = await agent.prepare_assistant_output(ctx, PointAgentOutputPrepared, text)
    await stream.replace(stream_ctx, prepared)


def start_message_stream(agent: Any, ctx: Any) -> MessageStream | None:
    """开一条流式消息（平台不支持则返回 ``None``）。对应 Go ``startMessageStream``。"""
    if buffer_assistant_output(ctx):
        return None
    msg, ok = message_context_from(ctx)
    if ok and msg.sender is not None and isinstance(msg.sender, StreamingMessageSender):
        return _try_start_stream(msg.sender, ctx)
    platform = getattr(agent, "platform", None)
    if platform is not None and isinstance(platform, StreamingMessageSender):
        return _try_start_stream(platform, ctx)
    return None


def _try_start_stream(sender: Any, ctx: Any) -> MessageStream | None:
    try:
        return sender.start_stream(ctx)
    except Exception:  # noqa: BLE001 - 平台不支持流式就退回整条发送
        return None


def buffer_assistant_output(ctx: Any) -> bool:
    """当前会话是否要求"先落库再发送"。对应 Go ``bufferAssistantOutput``。"""
    msg, ok = message_context_from(ctx)
    return bool(ok and msg.buffer_assistant_output)


# 便于交叉检索：``split_by_delivery_timing`` / ``DELIVERY_AFTER_ASSISTANT`` 是
# chat.py 组织"延后输出"时用的交付层契约，这里再导出一次。
__all__ += ["DELIVERY_AFTER_ASSISTANT", "Output", "split_by_delivery_timing"]
