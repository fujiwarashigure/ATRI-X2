"""入站消息归一化：文本、@提及、媒体段、Chat History 落库与媒体解析。

对应 Go：``internal/platform/telegram/message.go``。

形式差异：

* ``(T, error)`` → 抛异常：``ResolveMedia`` 失败抛 :class:`atri_x.errors.AtriXError`
  子类（超出导入上限用 :class:`~atri_x.errors.MediaTooLargeError`，其余用
  :class:`~atri_x.errors.InvalidError`，错误文案与 Go 逐字一致）。
* ``time.Unix(msg.Date, 0)`` → ``datetime.fromtimestamp(..., timezone.utc)``：
  本仓时间必须是 aware UTC（PORTING §1.7），Go 侧取的是本机时区。
* 本模块的 :func:`first_non_empty` 返回**去空白后的值**（Go ``telegram.firstNonEmpty``），
  与 :func:`atri_x.delivery.delivery.first_non_empty`（返回原值）不是同一个语义。
* ``recordChatMessage`` / ``referenceFetcher`` 在 Go 里是 ``*Adapter`` 方法；这里做成
  模块函数，由适配器把 ``chat_history`` / ``platform`` / ``log_warn`` 传进来，
  以免 ``message`` ↔ ``adapter`` 循环导入。

**群聊工具进度预览**：本模块（以及整个 Telegram 适配器）**不调用**
``should_drop_group_tool_preview`` —— 跳过预览只被 OneBot / QQ 官方使用，
Telegram 与本地 CLI 不跳过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from atri_x.delivery.delivery import Output as DeliveryOutput
from atri_x.delivery.delivery import Source
from atri_x.errors import InvalidError, MediaTooLargeError
from atri_x.platform.media import marshal_chat_segments
from atri_x.platform.platform import (
    ConversationChannel,
    ConversationGroup,
    ConversationPrivate,
    ConversationUnknown,
    Mention,
    MessageSegment,
    MessageSegmentType,
    ReplyContext,
)
from atri_x.platform.refcontext import MarshalChatMetadata, ReferencedMessage
from atri_x.platform.telegram.types import Chat, Message, PhotoSize, User
from atri_x.storage.models import ChatMessage
from atri_x.storage.times import now as storage_now

#: @提及字段两端要剥掉的标点（与 Go 的字面量逐字一致）。
MENTION_TRIM_CUTSET = ",，.:：;；!！?？()（）[]【】"


@dataclass
class NormalizedMessage:
    """一条入站消息的归一化结果。对应 Go ``normalizedMessage``。"""

    text: str = ""
    reply_id: str = ""
    reply_message: Message | None = None
    mentions: list[Mention] = field(default_factory=list)
    segments: list[MessageSegment] = field(default_factory=list)


def normalize_message(msg: Message) -> NormalizedMessage:
    """解析文本/引用/媒体段。对应 Go ``normalizeMessage``。

    纯媒体消息会补一个占位文本（``[图片]`` / ``[文件]`` / ``[语音]``），
    媒体段按 文本 → 图片 → 文件 → 语音 → 音频 的固定顺序排列。
    """
    out = NormalizedMessage()
    text = first_non_empty(msg.text, msg.caption)
    out.mentions = mentions_from_text(text)
    out.text = clean_text(text)
    if msg.reply_to_message is not None:
        out.reply_id = format_message_id(msg.reply_to_message.message_id)
        out.reply_message = msg.reply_to_message
    if out.text != "":
        out.segments.append(MessageSegment(type=MessageSegmentType.TEXT, text=out.text))
    if msg.photo:
        photo = largest_photo(msg.photo)
        out.segments.append(
            MessageSegment(
                type=MessageSegmentType.IMAGE,
                name=photo.file_id,
                platform_file_id=photo.file_id,
                mime_type="image/jpeg",
                size=photo.file_size,
            )
        )
        if out.text == "":
            out.text = "[图片]"
    if msg.document is not None:
        document = msg.document
        name = document.file_name or document.file_id
        out.segments.append(
            MessageSegment(
                type=MessageSegmentType.FILE,
                name=name,
                mime_type=document.mime_type,
                platform_file_id=document.file_id,
                size=document.file_size,
            )
        )
        if out.text == "":
            out.text = "[文件]"
    if msg.voice is not None:
        voice = msg.voice
        out.segments.append(
            MessageSegment(
                type=MessageSegmentType.FILE,
                text="语音",
                name="voice.ogg",
                mime_type=first_non_empty(voice.mime_type, "audio/ogg"),
                platform_file_id=voice.file_id,
                size=voice.file_size,
            )
        )
        if out.text == "":
            out.text = "[语音]"
    if msg.audio is not None:
        audio = msg.audio
        name = (audio.file_name or "").strip()
        if name == "":
            name = "audio" + audio_extension(audio.mime_type)
        out.segments.append(
            MessageSegment(
                type=MessageSegmentType.FILE,
                text="语音",
                name=name,
                mime_type=first_non_empty(audio.mime_type, "audio/mpeg"),
                platform_file_id=audio.file_id,
                size=audio.file_size,
            )
        )
        if out.text == "":
            out.text = "[语音]"
    if not out.segments and out.text != "":
        out.segments.append(MessageSegment(type=MessageSegmentType.TEXT, text=out.text))
    return out


def strip_bot_mention(text: str, bot_username: str) -> tuple[str, bool]:
    """剥掉 ``/cmd@bot`` 或正文里的 ``@bot``。对应 Go ``stripBotMention``。

    返回 ``(处理后的文本, 是否命中)``。
    """
    bot_username = (bot_username or "").strip().removeprefix("@")
    if text == "" or bot_username == "":
        return text, False
    mention = "@" + bot_username.lower()
    mentioned = mention in text.lower()
    if text.startswith("/"):
        fields = text.split()
        if fields:
            command = fields[0]
            at = command.find("@")
            if at >= 0 and command[at:].removeprefix("@").lower() == bot_username.lower():
                fields[0] = command[:at]
                return " ".join(fields), True
    if mentioned:
        kept = [field for field in text.split() if field.lower() != "@" + bot_username.lower()]
        return " ".join(kept), True
    return text, False


def largest_photo(photos: Sequence[PhotoSize]) -> PhotoSize | None:
    """取面积最大的 ``photoSize``。对应 Go ``largestPhoto``。"""
    if not photos:
        return None
    best = photos[0]
    for photo in photos[1:]:
        if photo.width * photo.height > best.width * best.height:
            best = photo
    return best


def telegram_conversation_kind(chat: Chat) -> str:
    """对应 Go ``telegramConversationKind``。"""
    if chat.type == "private":
        return ConversationPrivate
    if chat.type in ("group", "supergroup"):
        return ConversationGroup
    if chat.type == "channel":
        return ConversationChannel
    return ConversationUnknown


def mentions_from_text(text: str) -> list[Mention]:
    """从文本里抽出 ``@username``。对应 Go ``mentionsFromText``。"""
    mentions: list[Mention] = []
    for field_value in (text or "").split():
        name = field_value.strip().strip(MENTION_TRIM_CUTSET)
        if not name.startswith("@") or len(name) <= 1:
            continue
        mentions.append(Mention(username=name[1:], text=field_value))
    return mentions


def reference_fetcher(
    msg: Message, normalized: NormalizedMessage
) -> Callable[[Any, str], tuple[ReferencedMessage, bool]]:
    """平台兜底的引用读取。对应 Go ``(*Adapter).referenceFetcher``。

    Go 侧的接收者是 ``*Adapter`` 但从未使用，因此这里是模块函数；返回的闭包
    只在"引用的正是本条的 ``reply_to_message``"时命中。
    """

    def fetch(ctx: Any, reply_id: str) -> tuple[ReferencedMessage, bool]:
        if normalized.reply_message is None or normalized.reply_id != (reply_id or "").strip():
            return ReferencedMessage(), False
        ref = normalize_message(normalized.reply_message)
        label = "引用"
        sender_name = ""
        if normalized.reply_message.from_ is not None:
            sender_name = display_name(normalized.reply_message.from_)
            label = "引用：" + sender_name
        return (
            ReferencedMessage(
                sender_id=user_id_string(normalized.reply_message.from_),
                sender_name=sender_name,
                label=label,
                text=ref.text,
                segments=append_non_text_segments([], ref.segments),
            ),
            True,
        )

    return fetch


def record_chat_message(
    chat_history: Any,
    platform_name: str,
    log_warn: Callable[..., None],
    msg: Message,
    normalized: NormalizedMessage,
    reply: ReplyContext,
) -> None:
    """把入站消息写进 Chat History。对应 Go ``(*Adapter).recordChatMessage``。

    ``chat_history`` 为空（未配置）、内容为空或消息 ID 为 0 时直接返回；
    写入失败只记 WARN，不影响消息处理。
    """
    if chat_history is None:
        return
    if not (normalized.text or "").strip() and not normalized.segments:
        return
    if msg.message_id == 0:
        return
    created_at = storage_now()
    if msg.date > 0:
        created_at = datetime.fromtimestamp(msg.date, timezone.utc)
    row = ChatMessage(
        platform=platform_name,
        platform_scope_id=scope_id(msg.chat),
        scope_type=msg.chat.type,
        platform_message_id=format_message_id(msg.message_id),
        sender_id=user_id_string(msg.from_),
        sender_name=display_name_ptr(msg.from_, ""),
        text=normalized.text,
        raw=first_non_empty(msg.text, msg.caption),
        segments=marshal_chat_segments(normalized.segments),
        reply_to_platform_message_id=normalized.reply_id,
        metadata=MarshalChatMetadata(reply),
        created_at=created_at,
    )
    try:
        chat_history.append(row)
    except Exception as exc:  # noqa: BLE001 - 历史写入失败不影响本轮处理
        log_warn("record telegram chat message failed", error=exc, message_id=msg.message_id)


async def resolve_media(
    client: Any, ctx: Any, segment: MessageSegment, max_bytes: int
) -> Source:
    """下载入站媒体。对应 Go ``(*Adapter).ResolveMedia``。"""
    file = await client.get_file(ctx, segment.platform_file_id)
    if not (file.file_path or "").strip():
        raise InvalidError("telegram file path is empty")
    if file.file_size > max_bytes:
        raise MediaTooLargeError(f"telegram media exceeds import limit of {max_bytes} bytes")
    data = await client.download_file(ctx, file.file_path, max_bytes)
    return Source(data=data, mime_type=segment.mime_type)


def final_message_segments(
    text: str, current: Sequence[MessageSegment], referenced: Sequence[MessageSegment]
) -> list[MessageSegment]:
    """文本头 + 当前媒体段 + 引用媒体段。对应 Go ``finalMessageSegments``。"""
    out: list[MessageSegment] = []
    if (text or "").strip() != "":
        out.append(MessageSegment(type=MessageSegmentType.TEXT, text=text))
    out = append_non_text_segments(out, current)
    out = append_non_text_segments(out, referenced)
    return out


def append_non_text_segments(
    out: list[MessageSegment], segments: Sequence[MessageSegment]
) -> list[MessageSegment]:
    """只追加非文本段。对应 Go ``appendNonTextSegments``。"""
    for segment in segments:
        if segment.type != MessageSegmentType.TEXT:
            out.append(segment)
    return out


def scope_id(chat: Chat) -> str:
    """平台 scope：``group:<id>`` / ``supergroup:<id>`` / ``private:<id>``。"""
    if chat.type == "group":
        return f"group:{chat.id}"
    if chat.type == "supergroup":
        return f"supergroup:{chat.id}"
    return f"private:{chat.id}"


def user_id_string(user: User | None) -> str:
    """对应 Go ``userIDString``。"""
    if user is None:
        return ""
    return str(user.id)


def reply_sender(msg: Message | None) -> User | None:
    """对应 Go ``replySender``。"""
    if msg is None:
        return None
    return msg.from_


def is_configured_superadmin(superadmins: Sequence[str] | None, user_id: str) -> bool:
    """该平台用户 ID 是否在超管名单里。对应 Go ``isConfiguredSuperadmin``。"""
    user_id = (user_id or "").strip()
    if user_id == "":
        return False
    for candidate in superadmins or []:
        candidate = (candidate or "").strip().removeprefix("telegram:")
        if candidate == user_id:
            return True
    return False


def format_message_id(value: int) -> str:
    """消息 ID → 文本；0 表示"没有"（返回空串）。对应 Go ``formatMessageID``。"""
    if value == 0:
        return ""
    return str(value)


def display_name_ptr(user: User | None, fallback: str) -> str:
    """对应 Go ``displayNamePtr``。"""
    if user is None:
        return fallback
    return display_name(user)


def display_name(user: User) -> str:
    """``名 姓``，都为空时退回用户名。对应 Go ``displayName``。"""
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    if name == "":
        name = (user.username or "").strip()
    return name


def is_from_bot(msg: Message) -> bool:
    """对应 Go ``isFromBot``。"""
    return msg.from_ is not None and msg.from_.is_bot


def clean_text(text: str) -> str:
    """折叠空白。对应 Go ``cleanText``（``strings.Fields`` + ``strings.Join``）。"""
    return " ".join((text or "").split())


def first_non_empty(*values: str) -> str:
    """第一个去空白后非空的值（**已去空白**）。对应 Go ``telegram.firstNonEmpty``。"""
    for value in values:
        value = (value or "").strip()
        if value != "":
            return value
    return ""


def audio_extension(mime_type: str) -> str:
    """按 MIME 推断音频扩展名。对应 Go ``audioExtension``。"""
    value = (mime_type or "").strip().lower()
    if value in ("audio/ogg", "audio/opus"):
        return ".ogg"
    if value in ("audio/mp4", "audio/m4a", "audio/x-m4a"):
        return ".m4a"
    if value in ("audio/wav", "audio/x-wav", "audio/wave"):
        return ".wav"
    if value == "audio/webm":
        return ".webm"
    return ".mp3"


def media_source_name(out: DeliveryOutput) -> str:
    """出站媒体的文件名。对应 Go ``mediaSourceName``。"""
    name = (out.name or "").strip()
    if name != "":
        return name
    for value in (out.source.path, out.source.url):
        value = (value or "").strip()
        if value == "":
            continue
        base = _path_base(value)
        if base.strip() != "" and base != "." and base != "/":
            return base
    return "file"


def _path_base(value: str) -> str:
    """对应 Go ``filepath.Base``（Windows 与 POSIX 分隔符都认）。"""
    value = (value or "").replace("\\", "/")
    value = value.rstrip("/")
    if "/" in value:
        value = value[value.rindex("/") + 1 :]
    return value or "/"


__all__ = [name for name in dir() if not name.startswith("_")]
