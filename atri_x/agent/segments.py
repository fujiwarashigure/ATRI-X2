"""入站消息段在 ctx / LLM / 平台三种表示之间的搬运。

对应 Go：``internal/agent/segments.go``。

``ctx`` 是鸭子类型；本模块只在 ctx 上挂 ``turn.Input`` 与改写
:class:`atri_x.platform.MessageContext`，不碰别的键。
"""

from __future__ import annotations

from typing import Any

from atri_x.delivery import ctx_value, ctx_with_value
from atri_x.llm import (
    MessageSegment,
    SegmentFile,
    SegmentImage,
    SegmentText,
    segments_text_only,
    text_segments,
)
from atri_x.platform import (
    MessageSegment as PlatformMessageSegment,
)
from atri_x.platform import (
    SegmentFile as PlatformSegmentFile,
)
from atri_x.platform import (
    SegmentImage as PlatformSegmentImage,
)
from atri_x.platform import (
    SegmentText as PlatformSegmentText,
)
from atri_x.platform import message_context_from, with_message_context
from atri_x.turn import Input

__all__ = [
    "InboundTurnInput",
    "file_segment_text",
    "has_inbound_non_text_segment",
    "inbound_context_segments",
    "inbound_reply_message_id",
    "inbound_segments",
    "inbound_turn_input",
    "llm_segments_to_platform",
    "platform_segments_to_llm",
    "replace_inbound_text_segments",
    "with_inbound_segments",
    "with_inbound_turn_input",
]

#: Go 的 ``inboundTurnInputContextKey{}``。
InboundTurnInput = object()


def inbound_segments(ctx: Any, text: str) -> list[MessageSegment]:
    """当前入站消息的 LLM 段视图。对应 Go ``inboundSegments``。"""
    input_value = ctx_value(ctx, InboundTurnInput)
    if isinstance(input_value, Input) and input_value.segments:
        return [segment.clone() for segment in input_value.segments]
    msg, ok = message_context_from(ctx)
    if ok and msg.segments:
        return platform_segments_to_llm(msg.segments, text)
    return text_segments(text)


def inbound_turn_input(ctx: Any, text: str) -> Input:
    """把当前入站消息归一成 :class:`atri_x.turn.Input`。对应 Go ``inboundTurnInput``。"""
    input_value = ctx_value(ctx, InboundTurnInput)
    if isinstance(input_value, Input):
        return Input(
            text=input_value.text,
            platform_text=input_value.platform_text,
            segments=[segment.clone() for segment in input_value.segments],
            speaker=input_value.speaker,
            parts=list(input_value.parts),
        )
    platform_text = text
    msg, ok = message_context_from(ctx)
    if ok and (msg.raw_text or "").strip():
        platform_text = msg.raw_text
    segments = inbound_segments(ctx, text)
    return Input(
        text=segments_text_only(segments), platform_text=platform_text, segments=segments
    )


def with_inbound_turn_input(ctx: Any, input_value: Input) -> Any:
    """把 ``turn.Input`` 写回 ctx，并同步平台消息视图。对应 Go ``withInboundTurnInput``。"""
    stored = Input(
        text=input_value.text,
        platform_text=input_value.platform_text,
        segments=[segment.clone() for segment in input_value.segments],
        speaker=input_value.speaker,
        parts=list(input_value.parts),
    )
    ctx = ctx_with_value(ctx, InboundTurnInput, stored)
    msg, ok = message_context_from(ctx)
    if not ok:
        return ctx
    msg.raw_text = stored.platform_text
    msg.segments = llm_segments_to_platform(stored.segments)
    msg.context_text = ""
    msg.context_segments = []
    return with_message_context(ctx, msg)


def inbound_context_segments(ctx: Any, text: str) -> list[MessageSegment]:
    """Hook 可改写的上下文段视图。对应 Go ``inboundContextSegments``。"""
    msg, ok = message_context_from(ctx)
    if ok:
        if msg.context_segments:
            return platform_segments_to_llm(msg.context_segments, msg.context_text)
        if (msg.context_text or "").strip():
            return text_segments(msg.context_text)
    return inbound_segments(ctx, text)


def inbound_reply_message_id(ctx: Any) -> str:
    """被回复的平台消息 id。对应 Go ``inboundReplyMessageID``。"""
    msg, ok = message_context_from(ctx)
    if not ok:
        return ""
    reply_id = (msg.reply.message_id or "").strip()
    if reply_id:
        return reply_id
    return (msg.reply_to_message_id or "").strip()


def with_inbound_segments(ctx: Any, segments: list[MessageSegment]) -> Any:
    """按给定段重写 ctx 的入站视图。对应 Go ``withInboundSegments``。"""
    input_value = inbound_turn_input(ctx, segments_text_only(segments))
    input_value.text = segments_text_only(segments)
    input_value.segments = [segment.clone() for segment in segments]
    ctx = ctx_with_value(ctx, InboundTurnInput, input_value)
    msg, ok = message_context_from(ctx)
    if not ok:
        return ctx
    msg.segments = llm_segments_to_platform(segments)
    msg.context_text = ""
    msg.context_segments = []
    return with_message_context(ctx, msg)


def replace_inbound_text_segments(ctx: Any, text: str) -> list[MessageSegment]:
    """把可能被改写的正文贴回入站段，只改真正变化的那一段文本。

    对应 Go ``replaceInboundTextSegments``：唤醒词、工具/技能指令、命令前缀的剥离
    不能让被引用的图片/文件挪位。
    """
    segments = inbound_segments(ctx, text)
    if segments_text_only(segments) == text:
        return segments
    original = "".join(
        segment.text for segment in segments if segment.type == SegmentText
    )
    before, after = list(original), list(text)
    if not before:
        if text:
            return [MessageSegment(type=SegmentText, text=text), *segments]
        return segments
    start = 0
    while start < len(before) and start < len(after) and before[start] == after[start]:
        start += 1
    end, new_end = len(before), len(after)
    while end > start and new_end > start and before[end - 1] == after[new_end - 1]:
        end -= 1
        new_end -= 1
    replacement = "".join(after[start:new_end])
    out: list[MessageSegment] = []
    offset = 0
    inserted = False
    for segment in segments:
        if segment.type != SegmentText:
            out.append(segment)
            continue
        runes = list(segment.text)
        left = min(max(start - offset, 0), len(runes))
        right = min(max(end - offset, 0), len(runes))
        updated = "".join(runes[:left])
        if not inserted and offset + len(runes) >= start:
            updated += replacement
            inserted = True
        updated += "".join(runes[right:])
        offset += len(runes)
        if updated:
            copy = segment.clone()
            copy.text = updated
            out.append(copy)
    return out


def has_inbound_non_text_segment(ctx: Any) -> bool:
    """当前入站消息里是否有非文本段。对应 Go ``hasInboundNonTextSegment``。"""
    for segment in inbound_segments(ctx, ""):
        if segment.type != SegmentText:
            return True
    return False


async def materialize_media(
    agent: Any, ctx: Any, segments: list[MessageSegment]
) -> list[MessageSegment]:
    """把段里的远端媒体本地化。对应 Go ``Agent.materializeMedia``。

    Go 侧 :meth:`media.Manager.Materialize` 是同步的；Python 版要下远端媒体，
    因此是 ``async``——这条链路必须 ``await``，否则调用方拿到的是 coroutine
    （迭代时报 ``'coroutine' object is not iterable``）。
    """
    copies = [segment.clone() for segment in segments]
    media = getattr(agent, "media", None)
    if media is None:
        return copies
    outcome = media.materialize(ctx, copies)
    if hasattr(outcome, "__await__"):
        outcome = await outcome
    return list(outcome or copies)


def llm_segments_to_platform(segments: list[MessageSegment]) -> list[PlatformMessageSegment]:
    """LLM 段 → 平台段。对应 Go ``llmSegmentsToPlatform``。"""
    out: list[PlatformMessageSegment] = []
    for segment in segments:
        if segment.type == SegmentText:
            out.append(PlatformMessageSegment(type=PlatformSegmentText, text=segment.text))
        elif segment.type == SegmentImage:
            out.append(
                PlatformMessageSegment(
                    type=PlatformSegmentImage,
                    text=segment.text,
                    url=segment.url,
                    media_id=segment.media,
                    mime_type=segment.mime_type,
                    name=segment.name,
                )
            )
        elif segment.type == SegmentFile:
            out.append(
                PlatformMessageSegment(
                    type=PlatformSegmentFile,
                    text=segment.text,
                    url=segment.url,
                    media_id=segment.media,
                    mime_type=segment.mime_type,
                    name=segment.name,
                )
            )
    return out


def platform_segments_to_llm(
    segments: list[PlatformMessageSegment], fallback_text: str
) -> list[MessageSegment]:
    """平台段 → LLM 段。对应 Go ``platformSegmentsToLLM``。"""
    out: list[MessageSegment] = []
    for segment in segments:
        if segment.type == PlatformSegmentText:
            if segment.text:
                out.append(MessageSegment(type=SegmentText, text=segment.text))
        elif segment.type == PlatformSegmentImage:
            if segment.url or segment.media_id:
                out.append(
                    MessageSegment(
                        type=SegmentImage,
                        media=segment.media_id,
                        url=segment.url,
                        mime_type=segment.mime_type,
                        name=segment.name,
                    )
                )
            else:
                out.append(
                    MessageSegment(type=SegmentText, text=file_segment_text(segment.name, "图片"))
                )
        elif segment.type == PlatformSegmentFile:
            # 语音段在入站路径上已被 ASR 文本段替换；视频与普通文件这里退化成文本
            # 引用，作为真正的模型输入是另一项能力。
            out.append(
                MessageSegment(
                    type=SegmentFile,
                    media=segment.media_id,
                    url=segment.url,
                    text=file_segment_text(segment.name, segment.text),
                    mime_type=segment.mime_type,
                    name=segment.name,
                )
            )
    if not out:
        return text_segments(fallback_text)
    return out


def file_segment_text(name: str, fallback: str) -> str:
    """非文本段的文本引用。对应 Go ``fileSegmentText``。"""
    label = (fallback or "").strip() or "文件"
    trimmed = (name or "").strip()
    if not trimmed:
        return "[" + label + "]"
    return f"[{label}: {trimmed}]"
