"""入站的网络型预处理：@ 解析、引用上下文、合并转发展开与 Chat History 落库。

对应 Go：``internal/platform/qq-onebot/inbound.go`` 的 ``prepareInbound`` 与
``internal/platform/qq-onebot/adapter.go`` 的 ``resolveForwardSegments`` /
``resolveAtSegments`` / ``replyToSenderID`` / ``referenceFetcher`` /
``recordChatMessage`` / ``finalMessageSegments``。

Go 侧这些是 ``*Adapter`` 方法；Python 里做成模块级协程函数（首个参数是适配器），
由 :class:`atri_x.platform.qq_onebot.adapter.Adapter` 的同名方法委托，以免
``adapter`` ↔ ``preprocess`` 循环导入。

形式差异：

* ``refcontext.Apply`` 是**同步**的（它由另一位实现者按 Go 的同步签名落地），而
  OneBot 的引用兜底要发 ``get_msg``。这里先按"无平台兜底"调一次，只有在
  「本地来源没有给出任何引用内容、且这次不是命令、也不是续聊自己的 Session」时
  才去 ``get_msg``，拿到结果后再调一次。两次调用都是纯函数式决策，第二次只是补上
  平台兜底分支；与 Go 的差别仅在 Go 会在上述被跳过的场景里多发一次 ``get_msg``
  （其结果不会改变输出）。
* ``context.WithTimeout`` → :func:`asyncio.wait_for`。
* ``json.RawMessage`` 的字节长度 → 重新序列化后的字节数（Go 直接量原始字节）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from atri_x.platform import refcontext
from atri_x.platform.config import has_command_prefix
from atri_x.platform.media import marshal_chat_segments
from atri_x.platform.platform import (
    ConversationKind,
    Identity,
    MessageContext,
    MessageSegment,
    MessageSegmentType,
    ReplyContext,
    with_message_context,
)
from atri_x.platform.qq_onebot.forward import (
    at_text,
    clean_text,
    new_forward_parse_state,
    render_forward_data,
)
from atri_x.platform.qq_onebot.message import (
    display_name,
    normalize_message_with_limits,
    sender_name,
)
from atri_x.platform.qq_onebot.target import (
    MESSAGE_TYPE_GROUP,
    MESSAGE_TYPE_PRIVATE,
    Target,
    scope_id,
    with_target,
)
from atri_x.platform.qq_onebot.types import (
    FORWARD_TRUNCATED_MARKER,
    FORWARD_TRUST_MARKER,
    Event,
    ForwardLimits,
    NormalizedMessage,
    parse_int64,
)
from atri_x.security import ActorID as security_actor_id
from atri_x.storage.models import ChatMessage
from atri_x.storage.times import now as storage_now


async def prepare_inbound(
    adapter: Any, prep_ctx: Any, base_ctx: Any, event: Event
) -> tuple[MessageContext, Any, str, bool]:
    """做完所有需要网络的预处理，返回 Agent 该处理的 ctx 与文本。

    对应 Go ``(*Adapter).prepareInbound``：返回
    ``(message_ctx, msg_ctx, text, ok)``；``ok`` 为假表示这条消息不需要处理
    （非私聊/群聊，或内容为空）。它不碰 Agent 队列，因此调用方可以决定同步跑
    （测试）还是交给请求管理器（读循环）。
    """
    normalized = normalize_message_with_limits(
        event.message, event.raw_message, event.self_id, adapter.forward_limits()
    )
    normalized = await resolve_at_segments(adapter, prep_ctx, event, normalized)
    normalized = await resolve_forward_segments(adapter, prep_ctx, event, normalized)
    if event.message_type not in (MESSAGE_TYPE_PRIVATE, MESSAGE_TYPE_GROUP):
        record_chat_message(adapter, base_ctx, event, normalized, ReplyContext())
        return MessageContext(), None, "", False
    text = normalized.text
    current_segments = normalized.segments
    message_ctx = MessageContext(
        platform=adapter.name(),
        platform_user_id=str(event.user_id),
        nickname=(event.sender.nickname or "").strip(),
        group_card=(event.sender.card or "").strip(),
        display_name=display_name(event.sender, event.user_id),
        group_role=adapter.group_role(event),
        scope_id=scope_id(event),
        conversation_kind=conversation_kind(event),
        platform_message_id=str(event.message_id),
        reply_to_message_id=normalized.reply_id,
        reply_to_sender_id=await reply_to_sender_id(adapter, prep_ctx, event, normalized.reply_id),
        media_resolver=adapter,
        sender=adapter,
        buffer_assistant_output=True,
        segments=final_message_segments(text, current_segments, None),
        raw_text=normalized.text,
        match_text=normalized.match_text,
        match_text_set=True,
        platform_message=raw_json(event.message),
        bot=Identity(user_id=str(event.self_id)),
        mentions=list(normalized.mentions),
        trigger_keywords=list(adapter.cfg.trigger_keywords),
        meta={
            "qq_onebot.message_id": str(event.message_id),
            "qq_onebot.message_type": event.message_type,
            "qq_onebot.group_id": str(event.group_id),
            "qq_onebot.user_id": str(event.user_id),
        },
    )
    msg_ctx = _with_outbound_target(with_message_context(base_ctx, message_ctx), event)

    reference_segments: list[MessageSegment] = []
    if normalized.reply_id != "":
        result = await apply_reference(adapter, prep_ctx, event, message_ctx, text)
        message_ctx.fork_from_message_id = result.fork_from_message_id
        message_ctx.resume_session_id = result.resume_session_id
        message_ctx.context_text = result.text
        message_ctx.reply = result.reply
        reference_segments = result.reference_segments
        if (result.text or "").strip() != "" or reference_segments:
            message_ctx.context_segments = final_message_segments(
                result.text, current_segments, reference_segments
            )
    record_chat_message(adapter, base_ctx, event, normalized, message_ctx.reply)
    message_ctx.segments = final_message_segments(text, current_segments, None)
    msg_ctx = _with_outbound_target(with_message_context(base_ctx, message_ctx), event)
    if text.strip() == "" and not current_segments:
        return message_ctx, msg_ctx, "", False
    return message_ctx, msg_ctx, text, True


def _with_outbound_target(ctx: Any, event: Event) -> Any:
    """把 ``targetKey{}`` 挂到 ctx 上。对应 Go 的 ``context.WithValue``。"""
    return with_target(
        ctx,
        Target(
            message_type=event.message_type, user_id=event.user_id, group_id=event.group_id
        ),
    )


async def apply_reference(
    adapter: Any, ctx: Any, event: Event, message_ctx: MessageContext, text: str
) -> Any:
    """引用上下文解析（含异步的平台兜底）。对应 Go ``refcontext.Apply`` 的调用点。"""
    options = refcontext.Options(
        store=adapter.store,
        chat_history=adapter.chat_history,
        platform=adapter.name(),
        scope_id=message_ctx.scope_id,
        actor_id=security_actor_id(adapter.name(), str(event.user_id)),
        is_superadmin=adapter.is_superadmin(str(event.user_id)),
        reply_id=message_ctx.reply_to_message_id,
        text=text,
        command_prefixes=adapter.cfg.command_prefixes,
        fetch=None,
    )
    result = refcontext.apply(options)
    if not _needs_platform_reference(adapter, result, text):
        return result
    referenced, ok = await fetch_referenced(adapter, ctx, event, message_ctx.reply_to_message_id)
    if not ok:
        return result
    options.fetch = _prefetched_fetcher(message_ctx.reply_to_message_id, referenced)
    return refcontext.apply(options)


def _needs_platform_reference(adapter: Any, result: Any, text: str) -> bool:
    """本地来源是否已经给出引用内容；没有才值得发 ``get_msg``。

    Go 的 fetcher 是惰性回调，只在本地来源全部落空时被调用。这里的等价判定：
    命令路径与"续聊自己的 Session"路径在 Go 里也不会调用 fetcher，直接跳过。
    """
    if has_command_prefix((text or "").strip(), adapter.cfg.command_prefixes):
        return False
    if result.resume_session_id and not result.fork_from_message_id:
        return False
    if (result.reply.text or "").strip() != "":
        return False
    return not result.reference_segments


def _prefetched_fetcher(reply_id: str, referenced: Any) -> Callable[[Any, str], Any]:
    """把已经取到的引用包成 ``refcontext`` 期望的同步回调。"""

    def fetch(_ctx: Any, asked_id: str) -> tuple[Any, bool]:
        if (asked_id or "").strip() != reply_id:
            return refcontext.ReferencedMessage(), False
        return referenced, True

    return fetch


async def fetch_referenced(
    adapter: Any, ctx: Any, event: Event, reply_id: str
) -> tuple[Any, bool]:
    """按消息 ID 从平台取被引用消息并归一化。对应 Go ``(*Adapter).referenceFetcher``。"""
    if adapter.transport is None:
        return refcontext.ReferencedMessage(), False
    try:
        data = await adapter.transport.get_message(ctx, reply_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 与 Go 一致：取不到就当没有引用
        return refcontext.ReferencedMessage(), False
    ref = normalize_message_with_limits(
        data.message, data.raw_message, event.self_id, adapter.forward_limits()
    )
    # 被引用的消息本身若是合并转发，也要展开 —— 只有这一层，避免敌意引用拉出无界转发树。
    if ref.forward_ids:
        ref = await resolve_forward_segments(adapter, ctx, event, ref)
    label = "引用"
    if data.user_id != 0:
        label = "引用：" + display_name(data.sender, data.user_id)
    return (
        refcontext.ReferencedMessage(
            sender_id=str(data.user_id),
            sender_name=display_name(data.sender, data.user_id),
            label=label,
            text=ref.text,
            segments=list(ref.segments),
        ),
        True,
    )


async def resolve_forward_segments(
    adapter: Any, ctx: Any, event: Event, msg: NormalizedMessage
) -> NormalizedMessage:
    """按 id 拉取并展开合并转发。对应 Go ``(*Adapter).resolveForwardSegments``。"""
    if not msg.forward_ids or adapter.transport is None:
        return msg
    state = new_forward_state(adapter.forward_limits())
    parts: list[str] = [msg.text]
    length = len(msg.text)
    seen: dict[str, bool] = {}
    for raw_id in msg.forward_ids:
        forward_id = (raw_id or "").strip()
        if forward_id == "":
            continue
        if seen.get(forward_id):
            length = _write_fallback(parts, length, "[重复转发引用 id:" + forward_id + "，已省略]")
            continue
        seen[forward_id] = True
        if state.fetches >= state.limits.max_fetches:
            state.truncated = True
            length = _write_fallback(
                parts, length, "[转发拉取次数达到上限，剩余转发已省略]"
            )
            break
        state.fetches += 1
        try:
            if state.limits.fetch_timeout > 0:
                raw = await asyncio.wait_for(
                    adapter.transport.get_forward_msg(ctx, forward_id),
                    state.limits.fetch_timeout,
                )
            else:
                raw = await adapter.transport.get_forward_msg(ctx, forward_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 拉取失败退化为标记文本
            adapter.log_warn("get qq forward message failed", message_id=forward_id, error=exc)
            length = _write_fallback(
                parts, length, "[转发消息 id:" + forward_id + " 拉取失败]"
            )
            continue
        if _raw_size(raw) > state.limits.max_result_bytes:
            state.truncated = True
            length = _write_fallback(
                parts, length, "[转发消息 id:" + forward_id + " 超过体积上限，已省略]"
            )
            continue
        body, parsed_id, _resolved = render_forward_data(raw, state)
        if body.strip() == "" and parsed_id != "":
            body = "[转发消息 id:" + parsed_id + "]"
        if body.strip() == "":
            length = _write_fallback(
                parts, length, "[转发消息 id:" + forward_id + " 无可展示文本]"
            )
            continue
        if length > 0:
            parts.append("\n")
            length += 1
        parts.append(FORWARD_TRUST_MARKER)
        parts.append("\n")
        parts.append(body)
        length += len(FORWARD_TRUST_MARKER) + 1 + len(body)
    if state.truncated:
        parts.append(FORWARD_TRUNCATED_MARKER)
    msg.text = clean_text("".join(parts))
    if not msg.segments and msg.text != "":
        msg.segments = [MessageSegment(type=MessageSegmentType.TEXT, text=msg.text)]
    return msg


def raw_json(value: Any) -> str:
    """``json.RawMessage`` 的 Python 形态：原始 JSON 文本（``None`` → 空串）。

    对应 Go 的 ``append(json.RawMessage(nil), value...)``：nil 保持 nil，
    否则是原始字节；:class:`atri_x.platform.platform.MessageContext` 的
    ``platform_message`` 字段在 Python 侧声明为 ``str``，故落成 JSON 文本。
    """
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def _write_fallback(parts: list[str], length: int, text: str) -> int:
    if (text or "").strip() == "":
        return length
    if length > 0:
        parts.append("\n")
        length += 1
    parts.append(text)
    return length + len(text)


def _raw_size(raw: Any) -> int:
    """对应 Go ``len(raw json.RawMessage)``：序列化后的字节数。"""
    try:
        return len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def new_forward_state(limits: ForwardLimits) -> Any:
    """建一个转发解析状态。对应 Go ``newForwardParseState``。"""
    return new_forward_parse_state(limits)


async def resolve_at_segments(
    adapter: Any, ctx: Any, event: Event, msg: NormalizedMessage
) -> NormalizedMessage:
    """把群里的 ``@`` 段补上成员昵称。对应 Go ``(*Adapter).resolveAtSegments``。"""
    if (
        event.message_type != MESSAGE_TYPE_GROUP
        or event.group_id == 0
        or adapter.transport is None
        or not msg.segments
    ):
        return msg
    updated = list(msg.segments)
    replacements: dict[str, str] = {}
    changed = False
    for index, segment in enumerate(updated):
        if segment.type != MessageSegmentType.AT or segment.user_id == "":
            continue
        user_id = parse_int64(segment.user_id)
        if user_id is None or user_id == event.self_id:
            continue
        try:
            sender = await adapter.transport.get_group_member_info(
                ctx, event.group_id, user_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个成员查询失败不影响其它段
            adapter.log_warn(
                "get qq group member info failed",
                group_id=event.group_id,
                user_id=user_id,
                error=exc,
            )
            continue
        text = at_text(segment.user_id, sender_name(sender))
        if text == segment.text:
            continue
        replacements[segment.text] = text
        updated[index] = dataclasses.replace(segment, text=text)
        changed = True
    if not changed:
        return msg
    for old_text, new_text in replacements.items():
        msg.text = msg.text.replace(old_text, new_text)
    msg.segments = updated
    return msg


async def reply_to_sender_id(adapter: Any, ctx: Any, event: Event, reply_id: str) -> str:
    """被引用消息的发送者。对应 Go ``(*Adapter).replyToSenderID``。

    顺序与 Go 一致：本地 Message 表 → 媒体输出 → Chat History → ``get_msg``；
    bot 自己的消息返回 ``self_id``。
    """
    reply_id = (reply_id or "").strip()
    if reply_id == "":
        return ""
    scope = scope_id(event)
    if adapter.store is not None:
        try:
            message = adapter.store.messages().find_by_platform_message(
                adapter.name(), scope, reply_id
            )
        except Exception:  # noqa: BLE001 - 上游把错误当未命中
            message = None
        if message is not None and getattr(message, "role", "") == "assistant":
            return str(event.self_id)
        try:
            outputs = adapter.store.media().find_outputs(
                adapter.name(), scope, reply_id, storage_now()
            )
        except Exception:  # noqa: BLE001
            outputs = []
        if outputs:
            return str(event.self_id)
    if adapter.chat_history is not None:
        try:
            row = adapter.chat_history.get_by_platform_message(adapter.name(), scope, reply_id)
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            return getattr(row, "sender_id", "") or ""
    if adapter.transport is None:
        return ""
    try:
        data = await adapter.transport.get_message(ctx, reply_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return ""
    if data.user_id == 0:
        return ""
    return str(data.user_id)


def record_chat_message(
    adapter: Any, ctx: Any, event: Event, normalized: NormalizedMessage, reply: ReplyContext
) -> None:
    """把入站消息写进 Chat History。对应 Go ``(*Adapter).recordChatMessage``。"""
    if adapter.chat_history is None:
        return
    if (normalized.text or "").strip() == "" and not normalized.segments:
        return
    if event.message_id == 0:
        return
    created_at = storage_now()
    if event.time > 0:
        created_at = datetime.fromtimestamp(event.time, timezone.utc)
    row = ChatMessage(
        platform=adapter.name(),
        platform_scope_id=scope_id(event),
        scope_type=event.message_type,
        platform_message_id=str(event.message_id),
        sender_id=str(event.user_id),
        sender_name=sender_name(event.sender),
        text=normalized.text,
        raw=normalized.text,
        segments=marshal_chat_segments(normalized.segments),
        reply_to_platform_message_id=normalized.reply_id,
        metadata=refcontext.marshal_chat_metadata(reply),
        created_at=created_at,
    )
    try:
        adapter.chat_history.append(row)
    except Exception as exc:  # noqa: BLE001 - 写历史失败不影响本轮
        adapter.log_warn("record qq chat message failed", error=exc, message_id=event.message_id)


def final_message_segments(
    text: str, current: Sequence[MessageSegment] | None, referenced: Sequence[MessageSegment] | None
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


def conversation_kind(event: Event) -> str:
    """对应 Go ``oneBotConversationKind``。"""
    if event.message_type == MESSAGE_TYPE_PRIVATE:
        return ConversationKind.PRIVATE
    if event.message_type == MESSAGE_TYPE_GROUP:
        return ConversationKind.GROUP
    return ConversationKind.UNKNOWN


def is_message_event(event: Event) -> bool:
    """私聊/群聊消息事件。对应 Go ``(*Adapter).isMessageEvent``。"""
    return event.post_type == "message" and event.message_type in (
        MESSAGE_TYPE_PRIVATE,
        MESSAGE_TYPE_GROUP,
    )


def first_non_zero(*values: int) -> int:
    """对应 Go ``firstNonZero``。"""
    for value in values:
        if value != 0:
            return value
    return 0


def is_configured_superadmin(superadmins: Sequence[str] | None, identifier: str) -> bool:
    """该平台用户 ID 是否在超管名单里。对应 Go ``isConfiguredSuperadmin``。"""
    identifier = (identifier or "").strip()
    if identifier == "":
        return False
    for candidate in superadmins or []:
        candidate = (candidate or "").strip()
        if candidate.startswith("qqonebot:"):
            candidate = candidate[len("qqonebot:"):]
        if candidate == identifier:
            return True
    return False


__all__ = [name for name in dir() if not name.startswith("_")]
