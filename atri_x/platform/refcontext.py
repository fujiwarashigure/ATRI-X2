"""引用的恢复与续聊/Fork 判定。

对应 Go：``internal/platform/refcontext/refcontext.go``。

上游语义（来自 ``devdocs/architecture.md`` 的 ``locator:platform``）：

- 引用按「输出索引 → Chat History → 平台兜底」的顺序恢复有序媒体；
- **自己 Session 的最后一条 assistant 自动 Resume**（续聊同一 Session），
  **较早的 assistant 自动 Fork**（并从该消息处切开上下文）；
- 超级管理员回复**后台 Session**（scope 前缀 ``cron:`` / ``atrinis:``）时
  直接 Resume 该后台 Session。

``Apply`` 是唯一的入口：返回文本、Fork 起点、Resume 目标、引用段与
``ReplyContext``。平台适配器只负责把入站引用喂进来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from atri_x.platform.config import command_name, has_command_prefix, primary_command_prefix
from atri_x.platform.media import unmarshal_chat_segments
from atri_x.platform.platform import (
    MessageSegment,
    MessageSegmentType,
    ReplyContext,
)
from atri_x.storage.models import (
    ChatMessage,
    HistoryMedia,
    Message,
    RoleAssistant,
    Session,
)

#: 兜底标签。对应 Go 的字面量。
DEFAULT_REFERENCE_LABEL = "引用"
BOT_REFERENCE_LABEL = "引用：bot"
MEDIA_ONLY_TEXT = "[媒体消息]"


@dataclass
class ReferencedMessage:
    """被引用消息的可读形态。对应 Go ``ReferencedMessage``。"""

    sender_id: str = ""
    sender_name: str = ""
    label: str = ""
    text: str = ""
    segments: list[MessageSegment] = field(default_factory=list)


@dataclass
class Options:
    """``apply`` 的输入。对应 Go ``Options``。

    ``store`` / ``chat_history`` 用鸭子类型（对应 Go 的 ``storage.Store`` /
    ``storage.ChatHistoryRepository`` 接口）；``fetch`` 是平台兜底回调。
    """

    store: Any = None
    chat_history: Any = None
    platform: str = ""
    scope_id: str = ""
    actor_id: str = ""
    is_superadmin: bool = False
    reply_id: str = ""
    text: str = ""
    command_prefixes: Sequence[str] = field(default_factory=list)
    fetch: Callable[[Any, str], Any] | None = None


@dataclass
class Result:
    """``apply`` 的输出。对应 Go ``Result``。"""

    text: str = ""
    fork_from_message_id: str = ""
    resume_session_id: str = ""
    reference_segments: list[MessageSegment] = field(default_factory=list)
    reply: ReplyContext = field(default_factory=ReplyContext)


def apply(opts: Options) -> Result:  # noqa: A001 - 对齐 Go 的 Apply
    """主入口。对应 Go ``Apply``。"""
    result = Result(text=opts.text)
    reply_id = (opts.reply_id or "").strip()
    if not reply_id:
        return result
    result.reply.message_id = reply_id

    stored, has_stored = _referenced_message(opts, reply_id)
    if stored is not None:
        result.reply.text = (stored.content or "").strip()
        if result.reply.text:
            result.reply.segments = [
                MessageSegment(type=MessageSegmentType.TEXT, text=result.reply.text)
            ]

    trimmed = (opts.text or "").strip()
    if has_command_prefix(trimmed, opts.command_prefixes):
        # 命令路径：只处理 /fork 的重写，其余原样返回
        if stored is not None and stored.role == RoleAssistant:
            name, ok = command_name(trimmed, opts.command_prefixes)
            if ok and name == "fork":
                result.text = (
                    primary_command_prefix(opts.command_prefixes) + "fork " + stored.id
                )
        return result

    if stored is not None and stored.role == RoleAssistant:
        session, has_session = _referenced_session(opts, stored)
        if has_session and session is not None and opts.is_superadmin and _is_background_session(session):
            # 超管回复后台 Session：直接续聊它
            result.resume_session_id = session.id
            _, result.reference_segments, result.reply = _fallback_reference_text(
                opts, reply_id, stored, has_stored
            )
            return result
        if has_session and session is not None and _is_own_platform_session(opts, session):
            if _is_latest_assistant(opts.store, stored):
                # 自己 Session 的最后一条 assistant —— 续聊，不重复注入引用内容
                result.resume_session_id = session.id
                result.reply = ReplyContext(message_id=reply_id)
                return result
            # 较早的 assistant —— Fork，并保留引用媒体
            result.fork_from_message_id = stored.id
            _, result.reference_segments, result.reply = _fallback_reference_text(
                opts, reply_id, stored, has_stored
            )
            return result

    text, segments, reply = _fallback_reference_text(opts, reply_id, stored, has_stored)
    if reply.message_id:
        result.reply = reply
    result.text = text
    result.reference_segments = segments
    return result


Apply = apply


# ---------------------------------------------------------------------------
# 内部：定位被引用对象
# ---------------------------------------------------------------------------
def _referenced_message(opts: Options, reply_id: str) -> tuple[Message | None, bool]:
    """按平台消息 ID 找已落库的 Message。对应 Go ``referencedMessage``。"""
    if opts.store is None:
        return None, False
    try:
        message = opts.store.messages().find_by_platform_message(
            (opts.platform or "").strip(), (opts.scope_id or "").strip(), reply_id
        )
    except Exception:  # noqa: BLE001 - 上游同样把错误吞成"没找到"
        return None, False
    return message, True


def _referenced_session(opts: Options, message: Message) -> tuple[Session | None, bool]:
    """对应 Go ``referencedSession``。"""
    if opts.store is None or opts.store.sessions() is None or message is None:
        return None, False
    try:
        session = opts.store.sessions().get(message.session_id)
    except Exception:  # noqa: BLE001
        return None, False
    return session, True


def _is_own_platform_session(opts: Options, session: Session | None) -> bool:
    """对应 Go ``isOwnPlatformSession``（后台 Session 永远不算"自己的"）。"""
    if session is None or _is_background_session(session):
        return False
    return (
        session.owner_id == (opts.actor_id or "").strip()
        and session.platform == (opts.platform or "").strip()
        and session.platform_scope_id == (opts.scope_id or "").strip()
    )


def _is_background_session(session: Session | None) -> bool:
    """对应 Go ``refcontext.isBackgroundSession``：只看 scope 前缀。

    注意：这与 ``atri_x.session`` 的 ``is_background``（还看 metadata
    ``background_kind``）**不是同一个判定**，不要合并。
    """
    if session is None:
        return False
    scope_id = (session.platform_scope_id or "").strip()
    return scope_id.startswith("cron:") or scope_id.startswith("atrinis:")


def _is_latest_assistant(store: Any, message: Message | None) -> bool:
    """该消息是否是 Session 里最后一条 assistant。对应 Go ``isLatestAssistant``。

    读不到消息列表时**保守返回 True**（与 Go 一致）—— 即倾向"续聊"而不是 Fork。
    """
    if store is None or message is None:
        return True
    try:
        messages = store.messages().list_by_session(message.session_id)
    except Exception:  # noqa: BLE001
        return True
    for item in reversed(list(messages)):
        if item.role == RoleAssistant:
            return item.id == message.id
    return True


# ---------------------------------------------------------------------------
# 内部：引用文本与媒体恢复
# ---------------------------------------------------------------------------
def _fallback_reference_text(
    opts: Options,
    reply_id: str,
    stored: Message | None,
    has_stored: bool,
) -> tuple[str, list[MessageSegment], ReplyContext]:
    """对应 Go ``fallbackReferenceText``。返回 ``(最终文本, 引用段, ReplyContext)``。"""
    label = DEFAULT_REFERENCE_LABEL
    content = ""
    segments: list[MessageSegment] = []
    reply = ReplyContext(message_id=reply_id)

    ref, found = _reference_source(opts, reply_id)
    if found:
        if (ref.label or "").strip():
            label = ref.label.strip()
        reply.sender_id = (ref.sender_id or "").strip()
        reply.sender_name = (ref.sender_name or "").strip()
        content = ref.text
        segments = list(ref.segments)

    if has_stored and stored is not None:
        if stored.role == RoleAssistant and label == DEFAULT_REFERENCE_LABEL:
            label = BOT_REFERENCE_LABEL
        if (stored.content or "").strip():
            content = stored.content

    if not reply.sender_name:
        reply.sender_name = _reference_sender_name(label)

    content = (content or "").strip()
    reply.text = content
    reply.segments = list(segments)
    if not reply.segments and content:
        reply.segments = [MessageSegment(type=MessageSegmentType.TEXT, text=content)]
    if not content and segments:
        content = MEDIA_ONLY_TEXT
        reply.text = content
    if not content:
        # 一点内容都没有：原样返回用户输入，但仍带上引用段
        return opts.text, segments, reply
    return format_reference_text(opts.platform, reply, opts.text), segments, reply


def format_reference_text(platform_name: str, reply: ReplyContext, current_text: str) -> str:
    """把引用块拼到当前输入前面。对应 Go ``FormatReferenceText``。"""
    header = "引用#" + (reply.message_id or "").strip()
    name = (reply.sender_name or "").strip()
    ident = (reply.sender_id or "").strip()
    if ident:
        kind = _reference_id_kind(platform_name)
        if name:
            name += f"({kind}:{ident})"
        else:
            name = f"{kind}:{ident}"
    if name:
        header += "：" + name
    content = (reply.text or "").strip()
    if content:
        header += ":" + content
    formatted = "[" + header + "]"
    current = (current_text or "").strip()
    if current:
        formatted += "\n\n" + current
    return formatted


FormatReferenceText = format_reference_text


def _reference_source(opts: Options, reply_id: str) -> tuple[ReferencedMessage, bool]:
    """按「输出索引 → Chat History → 平台兜底」恢复引用。对应 Go ``referenceSource``。"""
    if opts.store is not None and opts.store.media() is not None:
        try:
            import datetime as _dt

            outputs = opts.store.media().find_outputs(
                opts.platform, opts.scope_id, reply_id, _dt.datetime.now(_dt.timezone.utc)
            )
        except Exception:  # noqa: BLE001
            outputs = []
        if outputs:
            ref = ReferencedMessage(label=BOT_REFERENCE_LABEL)
            for output in outputs:
                kind = MessageSegmentType.FILE
                if output.kind == "image":
                    kind = MessageSegmentType.IMAGE
                ref.segments.append(MessageSegment(type=kind, media_id=output.media_id))
            return ref, True

    if opts.chat_history is not None:
        try:
            row = opts.chat_history.get_by_platform_message(
                opts.platform, opts.scope_id, reply_id
            )
        except Exception:  # noqa: BLE001 - 上游把 ErrNotFound 视作未命中
            row = None
        if row is not None:
            segments = _restore_history_media(
                opts, row, unmarshal_chat_segments(row.segments)
            )
            if segments or (row.text or "").strip():
                label = DEFAULT_REFERENCE_LABEL
                if row.sender_name:
                    label += "：" + row.sender_name
                return (
                    ReferencedMessage(
                        sender_id=row.sender_id,
                        sender_name=row.sender_name,
                        label=label,
                        text=row.text,
                        segments=segments,
                    ),
                    True,
                )

    if opts.fetch is not None:
        return opts.fetch(None, reply_id)
    return ReferencedMessage(), False


def _restore_history_media(
    opts: Options, row: ChatMessage, segments: list[MessageSegment]
) -> list[MessageSegment]:
    """按历史媒体位置恢复**仍然有效**的媒体 ID（不下载、不重新解析来源）。

    对应 Go ``restoreHistoryMedia``。
    """
    out = list(segments)
    if opts.store is None or opts.store.media() is None:
        return out
    try:
        associations = opts.store.media().find_history(
            row.platform, row.platform_scope_id, row.platform_message_id
        )
    except Exception:  # noqa: BLE001
        return out

    by_index: dict[int, HistoryMedia] = {}
    for association in associations:
        if association.history_id == row.id:
            by_index[association.media_index] = association

    media_index = 0
    for segment in out:
        if segment.type not in (MessageSegmentType.IMAGE, MessageSegmentType.FILE):
            continue
        media_index += 1
        association = by_index.get(media_index)
        if association is None or association.kind != segment.type:
            continue
        try:
            item = opts.store.media().get(association.media_id)
        except Exception:  # noqa: BLE001
            continue
        if item is None or item.deleting:
            continue
        segment.media_id = association.media_id
    return out


# ---------------------------------------------------------------------------
# Chat History 元数据里的引用（reply）序列化
# ---------------------------------------------------------------------------
def marshal_chat_metadata(reply: ReplyContext) -> str:
    """把 ``ReplyContext`` 编码进 Chat History 的 metadata。对应 Go ``MarshalChatMetadata``。"""
    if not (reply.message_id or "").strip():
        return ""
    payload: dict[str, Any] = {"reply": {}}
    inner = payload["reply"]
    sender_id = (reply.sender_id or "").strip()
    sender_name = (reply.sender_name or "").strip()
    text = (reply.text or "").strip()
    if sender_id:
        inner["sender_id"] = sender_id
    if sender_name:
        inner["sender_name"] = sender_name
    if text:
        inner["text"] = text
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


MarshalChatMetadata = marshal_chat_metadata


def chat_message_reply(message: ChatMessage) -> ReplyContext:
    """从 Chat History 行恢复 ``ReplyContext``。对应 Go ``ChatMessageReply``。"""
    reply = ReplyContext(message_id=(message.reply_to_platform_message_id or "").strip())
    raw = (message.metadata or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        inner = data.get("reply") if isinstance(data, dict) else None
        if isinstance(inner, dict):
            reply.sender_id = str(inner.get("sender_id") or "").strip()
            reply.sender_name = str(inner.get("sender_name") or "").strip()
            reply.text = str(inner.get("text") or "").strip()
    return reply


ChatMessageReply = chat_message_reply


def _reference_sender_name(label: str) -> str:
    """``"引用：小明"`` → ``"小明"``；纯 ``"引用"`` → ``""``。对应 Go ``referenceSenderName``。"""
    label = (label or "").strip()
    if label == DEFAULT_REFERENCE_LABEL:
        return ""
    if label.startswith("引用："):
        return label[len("引用："):].strip()
    return label


def _reference_id_kind(platform_name: str) -> str:
    """不同平台的发送者 ID 用不同前缀标注。对应 Go ``referenceIDKind``。"""
    name = (platform_name or "").strip()
    if name == "qqonebot":
        return "qq"
    if name == "telegram":
        return "tg"
    if name == "qqofficial":
        return "openid"
    return name or "id"


__all__ = [
    "BOT_REFERENCE_LABEL",
    "DEFAULT_REFERENCE_LABEL",
    "MEDIA_ONLY_TEXT",
    "Apply",
    "ChatMessageReply",
    "FormatReferenceText",
    "MarshalChatMetadata",
    "Options",
    "ReferencedMessage",
    "Result",
    "apply",
    "chat_message_reply",
    "format_reference_text",
    "marshal_chat_metadata",
]
