"""入站平台媒体的本地化。

对应 Go：``internal/agent/inbound_media.go``。

职责：``materializePlatformMedia`` / ``materializePlatformSegment`` /
``associateInboundHistory`` / ``unavailablePlatformSegment``。

与 Go 的差异（形式，不是简化）：

* 段缓存键：Go 用可比较的 ``platform.MessageSegment`` 当 map key；Python 的
  dataclass 不可哈希，因此用 ``(type, url, media, platform_file_id, name)`` 元组
  （Go 的 struct 相等语义覆盖的正是这几个字段）。
* ``a.media.ImportPlatform`` / ``AssociateHistory`` 在 Python 侧是 async，
  因此整条链路是 async（``AgentSeams.materialize_platform_media`` 已按 async 调用）。
* 失败的段降级成 ``[媒体不可用；<名字>]`` 文本段，与 Go 逐字一致。
"""

from __future__ import annotations

from typing import Any

from atri_x.media import sanitize_name
from atri_x.platform import (
    SegmentFile,
    SegmentImage,
    SegmentText,
    MessageSegment,
    message_context_from,
    with_message_context,
)

from atri_x.agent.tools import service_host

__all__ = [
    "InboundMediaMixin",
    "associate_inbound_history",
    "materialize_platform_media",
    "materialize_platform_segment",
    "unavailable_platform_segment",
]


async def materialize_platform_media(agent: Any, ctx: Any) -> Any:
    """把入站消息（含引用/上下文）里的媒体段本地化。对应 Go ``materializePlatformMedia``。"""
    host = service_host(agent)
    msg, ok = message_context_from(ctx)
    media = getattr(host, "media", None)
    if not ok or media is None:
        return ctx
    cache: dict[tuple, MessageSegment] = {}

    async def _resolve(segments: list[Any]) -> list[Any]:
        out = [segment.clone() if hasattr(segment, "clone") else segment for segment in segments]
        for index, segment in enumerate(out):
            if getattr(segment, "type", "") not in (SegmentImage, SegmentFile):
                continue
            key = _segment_key(segment)
            cached = cache.get(key)
            if cached is not None:
                out[index] = cached
                continue
            resolved = await materialize_platform_segment(host, ctx, msg, segment)
            cache[key] = resolved
            out[index] = resolved
        return out

    original = msg
    msg.segments = await _resolve(msg.segments)
    msg.context_segments = await _resolve(msg.context_segments)
    if msg.reply is not None:
        msg.reply.segments = await _resolve(msg.reply.segments)
    await associate_inbound_history(
        host, ctx, msg, msg.platform_message_id, original.segments, msg.segments
    )
    if msg.reply is not None:
        await associate_inbound_history(
            host, ctx, msg, msg.reply.message_id, original.reply.segments, msg.reply.segments
        )
    return with_message_context(ctx, msg)


async def materialize_platform_segment(
    agent: Any, ctx: Any, msg: Any, segment: Any
) -> Any:
    """导入一个媒体段；失败时降级成"媒体不可用"。对应 Go ``materializePlatformSegment``。"""
    try:
        item = await agent.media.import_platform(
            ctx, getattr(msg, "platform", ""), getattr(msg, "media_resolver", None), segment
        )
    except BaseException:  # noqa: BLE001 - 与 Go 相同：失败的段不阻断消息
        return unavailable_platform_segment(segment)
    out = segment.clone() if hasattr(segment, "clone") else MessageSegment.from_json(
        segment.to_json() if hasattr(segment, "to_json") else {}
    )
    out.media_id = item.id
    out.name = item.name
    out.mime_type = item.mime_type
    out.size = int(item.size or 0)
    out.url = ""
    out.platform_file_id = ""
    return out


async def associate_inbound_history(
    agent: Any,
    ctx: Any,
    msg: Any,
    message_id: str,
    original: list[Any],
    resolved: list[Any],
) -> None:
    """把本地媒体 ID 关联到历史消息位序。对应 Go ``associateInboundHistory``。"""
    history = getattr(getattr(agent, "media", None), "history", None)
    if history is None or not message_id:
        return
    try:
        row = await _history_lookup(agent, ctx, msg, message_id)
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：查不到就跳过
        _warn(agent, "associate inbound history media failed", exc)
        return
    if row is None:
        return
    index = 0
    for position, raw in enumerate(original):
        if getattr(raw, "type", "") not in (SegmentImage, SegmentFile):
            continue
        index += 1
        if position >= len(resolved):
            continue
        media_id = str(getattr(resolved[position], "media_id", "") or "")
        if not media_id:
            continue
        try:
            await agent.media.associate_history(
                ctx, row, index, getattr(raw, "type", ""), media_id
            )
        except BaseException as exc:  # noqa: BLE001
            _warn(agent, "associate inbound history media failed", exc)


async def _history_lookup(agent: Any, ctx: Any, msg: Any, message_id: str) -> Any:
    """按平台消息 ID 查历史行。对应 Go ``a.media.History.GetByPlatformMessage``。"""
    history = getattr(agent.media, "history", None)
    for name in ("get_by_platform_message", "get_by_platform"):
        getter = getattr(history, name, None)
        if getter is None:
            continue
        result = getter(ctx, getattr(msg, "platform", ""), getattr(msg, "scope_id", ""), message_id)
        if hasattr(result, "__await__"):
            result = await result
        return result
    return None


def unavailable_platform_segment(segment: Any) -> Any:
    """降级段：``[媒体不可用；<名字>]``。对应 Go ``unavailablePlatformSegment``。"""
    label = str(getattr(segment, "name", "") or "").strip()
    if not label:
        label = "未知媒体"
    else:
        label = sanitize_name(label)
    return MessageSegment(type=SegmentText, text="[媒体不可用；" + label + "]")


class InboundMediaMixin:
    """Go ``*Agent`` 的入站媒体方法组。"""

    async def materialize_platform_media(self, ctx: Any) -> Any:
        return await materialize_platform_media(self, ctx)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _segment_key(segment: Any) -> tuple:
    return (
        getattr(segment, "type", ""),
        getattr(segment, "url", ""),
        getattr(segment, "media_id", ""),
        getattr(segment, "platform_file_id", ""),
        getattr(segment, "name", ""),
    )


def _warn(agent: Any, message: str, exc: BaseException) -> None:
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    warn = getattr(logger, "warn", None)
    if warn is not None:
        warn(message, error=str(exc))
