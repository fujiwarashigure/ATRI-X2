"""QQ 官方入站消息 → 平台中立 ``MessageContext``，以及 Chat History 落库。

对应 Go：``internal/platform/qqofficial/message.go``。

要点（上游语义）：

* 群消息的作者身份是 ``author.member_openid``，单聊是 ``author.user_openid``；
  缺任一路由字段就记 WARN 并丢弃（没有可回复的落点）。
* ``<faceType=...>`` 一律从正文里剔除；只有 ``GROUP_AT_MESSAGE_CREATE``
  才会剥掉开头的 ``@某人`` 前缀。
* 引用兜底用的是**消息自带的** ``message_reference.content``，不发网络请求，
  因此 :func:`inbound_reference_fetcher` 是同步回调。

形式差异：``time.Parse(time.RFC3339Nano, ...)`` → :func:`parse_rfc3339_nano`
（Python ``fromisoformat`` 只吃 6 位小数，这里先把纳秒截到微秒）；
Go 的 ``(*Adapter)`` 方法 → 模块级协程函数（首参是适配器），由
:class:`atri_x.platform.qqofficial.adapter.Adapter` 的同名方法委托。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from atri_x.platform import refcontext
from atri_x.platform.media import marshal_chat_segments
from atri_x.platform.platform import (
    ConversationKind,
    Identity,
    MessageContext,
    MessageSegment,
    MessageSegmentType,
    Mention,
    ReplyContext,
    with_message_context,
)
from atri_x.platform.qqofficial.attachment import inbound_attachment_segments
from atri_x.platform.qqofficial.target import (
    TARGET_C2C,
    TARGET_GROUP,
    SendTarget,
    with_target,
)
from atri_x.platform.qqofficial.types import InboundMessage, Payload
from atri_x.security import ActorID as security_actor_id
from atri_x.storage.models import ChatMessage
from atri_x.storage.times import now as storage_now

#: ``MessageContext.meta`` 的键（Go 常量）。
META_MSG_ID = "qqofficial.msg_id"
META_EVENT_ID = "qqofficial.event_id"
META_EVENT_TYPE = "qqofficial.event_type"
META_GROUP_ID = "qqofficial.group_openid"
META_MEMBER_ID = "qqofficial.member_openid"

#: ``<faceType=...>`` 一律剔除。对应 Go ``qqOfficialFaceFallbackPattern``。
FACE_FALLBACK_PATTERN = re.compile(r"<faceType=[^>]*>")

#: 群 @ 前缀（``@某人`` 或 ``<@!123>``）。对应 Go ``qqOfficialGroupAtPrefixPattern``。
GROUP_AT_PREFIX_PATTERN = re.compile(r"^\s*(?:@\S+\s*|<@!?[^>]+>\s*)")

#: RFC3339（含纳秒）。对应 Go ``time.RFC3339Nano`` 的可接受形态。
RFC3339_PATTERN = re.compile(
    r"^(?P<body>\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2})$"
)


async def handle_c2c_message(
    adapter: Any, ctx: Any, handler: Any, p: Payload, msg: InboundMessage
) -> None:
    """对应 Go ``(*Adapter).handleC2CMessage``。"""
    await handle_inbound_message(
        adapter, ctx, handler, p, msg, ConversationKind.PRIVATE, False
    )


async def handle_group_message(
    adapter: Any, ctx: Any, handler: Any, p: Payload, msg: InboundMessage
) -> None:
    """对应 Go ``(*Adapter).handleGroupMessage``。"""
    mentioned_bot = p.type == "GROUP_AT_MESSAGE_CREATE"
    await handle_inbound_message(
        adapter, ctx, handler, p, msg, ConversationKind.GROUP, mentioned_bot
    )


async def handle_inbound_message(
    adapter: Any,
    ctx: Any,
    handler: Any,
    p: Payload,
    msg: InboundMessage,
    conversation: str,
    mentioned_bot: bool,
) -> None:
    """一条入站消息的完整处理。对应 Go ``(*Adapter).handleInboundMessage``。"""
    sender_id, scope, target_kind, target_id = inbound_route(msg, conversation)
    if sender_id == "":
        label = "member_openid" if conversation == ConversationKind.GROUP else "user_openid"
        adapter.log_warn(
            ctx,
            "qqofficial message missing sender",
            field=label,
            message_id=msg.id,
            event=p.type,
        )
        return
    if target_id == "":
        adapter.log_warn(
            ctx, "qqofficial message missing target", message_id=msg.id, event=p.type
        )
        return

    text = normalized_inbound_text(msg, mentioned_bot)
    reply_id = inbound_reply_id(msg)
    segments = inbound_segments(text, inbound_attachment_segments(msg.attachments))
    if text == "" and not segments:
        return

    actor_id = security_actor_id(adapter.name(), sender_id)
    bot = Identity()
    mentions: list[Mention] = []
    if conversation == ConversationKind.GROUP:
        bot.user_id = (adapter.cfg.app_id or "").strip()
        if mentioned_bot and bot.user_id != "":
            mentions = [Mention(user_id=bot.user_id)]
    message_ctx = MessageContext(
        platform=adapter.name(),
        actor_id=actor_id,
        platform_user_id=sender_id,
        scope_id=scope,
        conversation_kind=conversation,
        platform_message_id=(msg.id or "").strip(),
        reply_to_message_id=reply_id,
        sender=adapter,
        buffer_assistant_output=True,
        segments=segments,
        raw_text=text,
        platform_message=raw_json(p.data),
        bot=bot,
        mentions=mentions,
        trigger_keywords=list(adapter.cfg.trigger_keywords),
        meta={
            META_MSG_ID: (msg.id or "").strip(),
            META_EVENT_ID: (p.id or "").strip(),
            META_EVENT_TYPE: p.type,
            META_GROUP_ID: (msg.group_openid or "").strip(),
            META_MEMBER_ID: (msg.author.member_openid or "").strip(),
        },
    )
    target = SendTarget(kind=target_kind, openid=target_id, msg_id=msg.id)
    msg_ctx = with_target(with_message_context(ctx, message_ctx), target)
    if reply_id != "":
        result = refcontext.apply(
            refcontext.Options(
                store=adapter.store,
                chat_history=adapter.chat_history,
                platform=adapter.name(),
                scope_id=message_ctx.scope_id,
                actor_id=actor_id,
                is_superadmin=is_configured_superadmin(adapter.cfg.superadmins, sender_id),
                reply_id=reply_id,
                text=text,
                command_prefixes=adapter.cfg.command_prefixes,
                fetch=inbound_reference_fetcher(msg),
            )
        )
        message_ctx.fork_from_message_id = result.fork_from_message_id
        message_ctx.resume_session_id = result.resume_session_id
        message_ctx.context_text = result.text
        message_ctx.reply = result.reply
        if (result.text or "").strip() != "" or result.reference_segments:
            message_ctx.context_segments = final_message_segments(
                result.text, segments, result.reference_segments
            )
        message_ctx.segments = final_message_segments(text, segments, None)
        msg_ctx = with_target(with_message_context(ctx, message_ctx), target)
    record_chat_message(
        adapter, ctx, msg, conversation, sender_id, scope, text, reply_id, message_ctx.reply
    )
    try:
        outcome = handler.handle_message(msg_ctx, text)
        if hasattr(outcome, "__await__"):
            await outcome
    except Exception as exc:  # noqa: BLE001 - 与 Go 一致：处理失败只记日志
        adapter.log_warn(
            ctx, "handle qqofficial message failed", error=exc, message_id=msg.id
        )


def inbound_route(
    msg: InboundMessage, conversation: str
) -> tuple[str, str, str, str]:
    """入站消息 → ``(sender_id, scope_id, target_kind, target_id)``。

    对应 Go ``inboundRoute``。
    """
    if conversation == ConversationKind.GROUP:
        sender_id = (msg.author.member_openid or "").strip()
        target_id = (msg.group_openid or "").strip()
        return sender_id, "group:" + target_id, TARGET_GROUP, target_id
    sender_id = (msg.author.user_openid or "").strip()
    return sender_id, "c2c:" + sender_id, TARGET_C2C, sender_id


def normalized_inbound_text(msg: InboundMessage, mentioned_bot: bool) -> str:
    """剔除 ``<faceType=...>``（群 @ 消息再剥 ``@`` 前缀）。对应 Go ``normalizedInboundText``。"""
    text = FACE_FALLBACK_PATTERN.sub("", msg.content or "")
    if mentioned_bot:
        text = GROUP_AT_PREFIX_PATTERN.sub("", text)
    return text.strip()


def inbound_reply_id(msg: InboundMessage) -> str:
    """对应 Go ``inboundReplyID``。"""
    if msg.message_reference is None:
        return ""
    return (msg.message_reference.message_id or "").strip()


def raw_json(value: Any) -> str:
    """``json.RawMessage`` 的 Python 形态：原始 JSON 文本（``None`` → 空串）。

    对应 Go ``append(json.RawMessage(nil), p.Data...)``；
    :class:`atri_x.platform.platform.MessageContext` 的 ``platform_message``
    字段在 Python 侧声明为 ``str``，故落成 JSON 文本。
    """
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def inbound_reference_fetcher(
    msg: InboundMessage,
) -> Callable[[Any, str], tuple[refcontext.ReferencedMessage, bool]]:
    """平台兜底引用读取（只读消息自带内容）。对应 Go ``inboundReferenceFetcher``。"""

    def fetch(_ctx: Any, reply_id: str) -> tuple[refcontext.ReferencedMessage, bool]:
        reference = msg.message_reference
        if reference is None:
            return refcontext.ReferencedMessage(), False
        if (reference.message_id or "").strip() != (reply_id or "").strip():
            return refcontext.ReferencedMessage(), False
        text = (reference.content or "").strip()
        if text == "":
            return refcontext.ReferencedMessage(), False
        return (
            refcontext.ReferencedMessage(
                label="引用",
                text=text,
                segments=[MessageSegment(type=MessageSegmentType.TEXT, text=text)],
            ),
            True,
        )

    return fetch


def record_chat_message(
    adapter: Any,
    ctx: Any,
    msg: InboundMessage,
    conversation: str,
    sender_id: str,
    scope: str,
    text: str,
    reply_id: str,
    reply: ReplyContext,
) -> None:
    """把入站消息写进 Chat History。对应 Go ``(*Adapter).recordChatMessage``。"""
    if adapter.chat_history is None:
        return
    if (text or "").strip() == "" and not msg.attachments:
        return
    if (msg.id or "").strip() == "":
        return
    created_at = storage_now()
    value = (msg.timestamp or "").strip()
    if value != "":
        parsed = parse_rfc3339_nano(value)
        if parsed is not None:
            created_at = parsed
    scope_type = "private"
    if conversation == ConversationKind.GROUP:
        scope_type = "group"
    row = ChatMessage(
        platform=adapter.name(),
        platform_scope_id=scope,
        scope_type=scope_type,
        platform_message_id=(msg.id or "").strip(),
        sender_id=sender_id,
        text=(text or "").strip(),
        raw=msg.content,
        segments=marshal_chat_segments(
            inbound_segments(text, inbound_attachment_segments(msg.attachments))
        ),
        reply_to_platform_message_id=(reply_id or "").strip(),
        metadata=refcontext.marshal_chat_metadata(reply),
        created_at=created_at,
    )
    try:
        adapter.chat_history.append(row)
    except Exception as exc:  # noqa: BLE001 - 写历史失败不影响本轮
        adapter.log_warn(
            ctx, "record qqofficial chat message failed", error=exc, message_id=msg.id
        )


def parse_rfc3339_nano(value: str) -> datetime | None:
    """解析 RFC3339/RFC3339Nano；失败返回 ``None``（Go 的 ``err != nil``）。"""
    match = RFC3339_PATTERN.match((value or "").strip())
    if match is None:
        return None
    fraction = (match.group("frac") or "")[:6]
    zone = match.group("tz")
    if zone in ("Z", "z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    text = match.group("body")
    if fraction:
        text += "." + fraction
    text += zone
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def inbound_segments(text: str, attachments: Sequence[MessageSegment]) -> list[MessageSegment]:
    """文本头 + 附件段。对应 Go ``inboundSegments``。"""
    segments: list[MessageSegment] = []
    if (text or "").strip() != "":
        segments.append(MessageSegment(type=MessageSegmentType.TEXT, text=text))
    segments.extend(attachments)
    return segments


def final_message_segments(
    text: str,
    current: Sequence[MessageSegment] | None,
    referenced: Sequence[MessageSegment] | None,
) -> list[MessageSegment]:
    """文本头 + 当前媒体段 + 引用媒体段。对应 Go ``finalMessageSegments``。"""
    out: list[MessageSegment] = []
    if (text or "").strip() != "":
        out.append(MessageSegment(type=MessageSegmentType.TEXT, text=text))
    out = append_non_text_segments(out, current)
    return append_non_text_segments(out, referenced)


def append_non_text_segments(
    out: list[MessageSegment], segments: Sequence[MessageSegment] | None
) -> list[MessageSegment]:
    """只追加非文本段。对应 Go ``appendNonTextSegments``。"""
    for segment in segments or []:
        if segment.type != MessageSegmentType.TEXT:
            out.append(segment)
    return out


def is_configured_superadmin(superadmins: Sequence[str] | None, identifier: str) -> bool:
    """该平台用户 ID 是否在超管名单里。对应 Go ``isConfiguredSuperadmin``。"""
    identifier = (identifier or "").strip()
    if identifier == "":
        return False
    for candidate in superadmins or []:
        candidate = (candidate or "").strip()
        if candidate.startswith("qqofficial:"):
            candidate = candidate[len("qqofficial:") :]
        if candidate == identifier:
            return True
    return False


__all__ = [name for name in dir() if not name.startswith("_")]
