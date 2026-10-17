"""Telegram Bot API 的线上类型与请求体。

对应 Go：``internal/platform/telegram/types.go``。

Go 用 struct tag 做 ``encoding/json`` 编解码；本模块用 dataclass 的
:meth:`to_json` / :meth:`from_json` 复刻同样的线上形状（含 ``omitempty``）。
两处刻意放宽（Go 会因类型不符整包解码失败）：

* 字段类型不符时退化为零值而不是报错（线上响应字段缺失/为 null 更常见）；
* ``json.RawMessage`` 用重新序列化后的 JSON 文本表示（Go 保留原始字节）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 线上响应
# ---------------------------------------------------------------------------

@dataclass
class ApiResponse:
    """Bot API 统一响应包。对应 Go ``apiResponse[T]``。"""

    ok: bool = False
    result: Any = None
    description: str = ""
    error_code: int = 0


def api_response_from_json(raw: Any, parse_result: Callable[[Any], Any] | None = None) -> ApiResponse:
    """解码统一响应包；``parse_result`` 把 ``result`` 转成具体类型。"""
    if not isinstance(raw, dict):
        raise ValueError("telegram response is not a JSON object")
    result = raw.get("result")
    if parse_result is not None:
        result = parse_result(result)
    return ApiResponse(
        ok=_as_bool(raw.get("ok")),
        result=result,
        description=_as_str(raw.get("description")),
        error_code=_as_int(raw.get("error_code")),
    )


@dataclass
class User:
    """对应 Go ``user``。"""

    id: int = 0
    is_bot: bool = False
    first_name: str = ""
    last_name: str = ""
    username: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "User":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            id=_as_int(raw.get("id")),
            is_bot=_as_bool(raw.get("is_bot")),
            first_name=_as_str(raw.get("first_name")),
            last_name=_as_str(raw.get("last_name")),
            username=_as_str(raw.get("username")),
        )


@dataclass
class Chat:
    """对应 Go ``chat``。"""

    id: int = 0
    type: str = ""
    title: str = ""
    username: str = ""
    first_name: str = ""
    last_name: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "Chat":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            id=_as_int(raw.get("id")),
            type=_as_str(raw.get("type")),
            title=_as_str(raw.get("title")),
            username=_as_str(raw.get("username")),
            first_name=_as_str(raw.get("first_name")),
            last_name=_as_str(raw.get("last_name")),
        )


@dataclass
class ChatMember:
    """对应 Go ``chatMember``。"""

    status: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "ChatMember":
        if not isinstance(raw, dict):
            return cls()
        return cls(status=_as_str(raw.get("status")))


@dataclass
class PhotoSize:
    """对应 Go ``photoSize``。"""

    file_id: str = ""
    file_unique_id: str = ""
    width: int = 0
    height: int = 0
    file_size: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> "PhotoSize":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            file_id=_as_str(raw.get("file_id")),
            file_unique_id=_as_str(raw.get("file_unique_id")),
            width=_as_int(raw.get("width")),
            height=_as_int(raw.get("height")),
            file_size=_as_int(raw.get("file_size")),
        )


@dataclass
class Document:
    """对应 Go ``document``。"""

    file_id: str = ""
    file_unique_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> "Document":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            file_id=_as_str(raw.get("file_id")),
            file_unique_id=_as_str(raw.get("file_unique_id")),
            file_name=_as_str(raw.get("file_name")),
            mime_type=_as_str(raw.get("mime_type")),
            file_size=_as_int(raw.get("file_size")),
        )


@dataclass
class Voice:
    """对应 Go ``voice``。"""

    file_id: str = ""
    file_unique_id: str = ""
    mime_type: str = ""
    duration: int = 0
    file_size: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> "Voice":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            file_id=_as_str(raw.get("file_id")),
            file_unique_id=_as_str(raw.get("file_unique_id")),
            mime_type=_as_str(raw.get("mime_type")),
            duration=_as_int(raw.get("duration")),
            file_size=_as_int(raw.get("file_size")),
        )


@dataclass
class Audio:
    """对应 Go ``audio``。"""

    file_id: str = ""
    file_unique_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    title: str = ""
    performer: str = ""
    duration: int = 0
    file_size: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> "Audio":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            file_id=_as_str(raw.get("file_id")),
            file_unique_id=_as_str(raw.get("file_unique_id")),
            file_name=_as_str(raw.get("file_name")),
            mime_type=_as_str(raw.get("mime_type")),
            title=_as_str(raw.get("title")),
            performer=_as_str(raw.get("performer")),
            duration=_as_int(raw.get("duration")),
            file_size=_as_int(raw.get("file_size")),
        )


@dataclass
class Message:
    """对应 Go ``message``（``from`` / ``document`` / ``voice`` / ``audio`` /
    ``reply_to_message`` 都是可空指针）。"""

    message_id: int = 0
    date: int = 0
    from_: User | None = None
    chat: Chat = field(default_factory=Chat)
    text: str = ""
    caption: str = ""
    photo: list[PhotoSize] = field(default_factory=list)
    document: Document | None = None
    voice: Voice | None = None
    audio: Audio | None = None
    reply_to_message: "Message | None" = None

    @classmethod
    def from_json(cls, raw: Any) -> "Message":
        if not isinstance(raw, dict):
            return cls()
        photo = raw.get("photo")
        reply = raw.get("reply_to_message")
        return cls(
            message_id=_as_int(raw.get("message_id")),
            date=_as_int(raw.get("date")),
            from_=_optional(raw.get("from"), User.from_json),
            chat=Chat.from_json(raw.get("chat")),
            text=_as_str(raw.get("text")),
            caption=_as_str(raw.get("caption")),
            photo=[PhotoSize.from_json(item) for item in photo] if isinstance(photo, list) else [],
            document=_optional(raw.get("document"), Document.from_json),
            voice=_optional(raw.get("voice"), Voice.from_json),
            audio=_optional(raw.get("audio"), Audio.from_json),
            reply_to_message=_optional(reply, Message.from_json),
        )


@dataclass
class CallbackQuery:
    """对应 Go ``callbackQuery``。"""

    id: str = ""
    from_: User = field(default_factory=User)
    message: Message | None = None
    data: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "CallbackQuery":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            id=_as_str(raw.get("id")),
            from_=User.from_json(raw.get("from")),
            message=_optional(raw.get("message"), Message.from_json),
            data=_as_str(raw.get("data")),
        )


@dataclass
class Update:
    """对应 Go ``update``。"""

    update_id: int = 0
    message: Message | None = None
    edited_message: Message | None = None
    callback_query: CallbackQuery | None = None

    @classmethod
    def from_json(cls, raw: Any) -> "Update":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            update_id=_as_int(raw.get("update_id")),
            message=_optional(raw.get("message"), Message.from_json),
            edited_message=_optional(raw.get("edited_message"), Message.from_json),
            callback_query=_optional(raw.get("callback_query"), CallbackQuery.from_json),
        )


@dataclass
class FileInfo:
    """对应 Go ``fileInfo``。"""

    file_id: str = ""
    file_unique_id: str = ""
    file_size: int = 0
    file_path: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "FileInfo":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            file_id=_as_str(raw.get("file_id")),
            file_unique_id=_as_str(raw.get("file_unique_id")),
            file_size=_as_int(raw.get("file_size")),
            file_path=_as_str(raw.get("file_path")),
        )


@dataclass
class SentMessage:
    """对应 Go ``sentMessage``。"""

    message_id: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> "SentMessage":
        if not isinstance(raw, dict):
            return cls()
        return cls(message_id=_as_int(raw.get("message_id")))


# ---------------------------------------------------------------------------
# 请求体（json tag 对齐 Go，含 omitempty）
# ---------------------------------------------------------------------------

@dataclass
class BotCommand:
    """对应 Go ``botCommand``。"""

    command: str = ""
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"command": self.command, "description": self.description}


@dataclass
class InlineKeyboardButton:
    """对应 Go ``inlineKeyboardButton``。"""

    text: str = ""
    callback_data: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "callback_data": self.callback_data}


@dataclass
class ReplyMarkup:
    """对应 Go ``replyMarkup``。"""

    inline_keyboard: list[list[InlineKeyboardButton]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [button.to_json() for button in row] for row in self.inline_keyboard
            ]
        }


@dataclass
class InputRichMessage:
    """对应 Go ``inputRichMessage``。"""

    html: str = ""
    markdown: str = ""
    is_rtl: bool = False
    skip_entity_detection: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.html:
            out["html"] = self.html
        if self.markdown:
            out["markdown"] = self.markdown
        if self.is_rtl:
            out["is_rtl"] = True
        if self.skip_entity_detection:
            out["skip_entity_detection"] = True
        return out


@dataclass
class ReplyParameters:
    """对应 Go ``replyParameters``。"""

    message_id: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"message_id": self.message_id}


@dataclass
class SendMessageRequest:
    """对应 Go ``sendMessageRequest``。"""

    chat_id: int = 0
    text: str = ""
    parse_mode: str = ""
    reply_to_message_id: int = 0
    reply_markup: ReplyMarkup | None = None
    disable_web_page_preview: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"chat_id": self.chat_id, "text": self.text}
        if self.parse_mode:
            out["parse_mode"] = self.parse_mode
        if self.reply_to_message_id:
            out["reply_to_message_id"] = self.reply_to_message_id
        if self.reply_markup is not None:
            out["reply_markup"] = self.reply_markup.to_json()
        if self.disable_web_page_preview:
            out["disable_web_page_preview"] = True
        return out


@dataclass
class EditMessageTextRequest:
    """对应 Go ``editMessageTextRequest``。"""

    chat_id: int = 0
    message_id: int = 0
    text: str = ""
    parse_mode: str = ""
    reply_markup: ReplyMarkup | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": self.text,
        }
        if self.parse_mode:
            out["parse_mode"] = self.parse_mode
        if self.reply_markup is not None:
            out["reply_markup"] = self.reply_markup.to_json()
        return out


@dataclass
class AnswerCallbackQueryRequest:
    """对应 Go ``answerCallbackQueryRequest``。"""

    callback_query_id: str = ""
    text: str = ""
    show_alert: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"callback_query_id": self.callback_query_id}
        if self.text:
            out["text"] = self.text
        if self.show_alert:
            out["show_alert"] = True
        return out


@dataclass
class SendRichMessageRequest:
    """对应 Go ``sendRichMessageRequest``。"""

    chat_id: int = 0
    rich_message: InputRichMessage = field(default_factory=InputRichMessage)
    reply_parameters: ReplyParameters | None = None
    reply_markup: ReplyMarkup | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "chat_id": self.chat_id,
            "rich_message": self.rich_message.to_json(),
        }
        if self.reply_parameters is not None:
            out["reply_parameters"] = self.reply_parameters.to_json()
        if self.reply_markup is not None:
            out["reply_markup"] = self.reply_markup.to_json()
        return out


@dataclass
class SendRichMessageDraftRequest:
    """对应 Go ``sendRichMessageDraftRequest``。"""

    chat_id: int = 0
    draft_id: int = 0
    rich_message: InputRichMessage = field(default_factory=InputRichMessage)

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "draft_id": self.draft_id,
            "rich_message": self.rich_message.to_json(),
        }


@dataclass
class GetUpdatesRequest:
    """对应 Go ``getUpdatesRequest``。"""

    offset: int = 0
    timeout: int = 0
    allowed_updates: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.offset:
            out["offset"] = self.offset
        if self.timeout:
            out["timeout"] = self.timeout
        if self.allowed_updates:
            out["allowed_updates"] = list(self.allowed_updates)
        return out


@dataclass
class GetFileRequest:
    """对应 Go ``getFileRequest``。"""

    file_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"file_id": self.file_id}


@dataclass
class GetChatMemberRequest:
    """对应 Go ``getChatMemberRequest``。"""

    chat_id: int = 0
    user_id: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "user_id": self.user_id}


@dataclass
class SetMyCommandsRequest:
    """对应 Go ``setMyCommandsRequest``。"""

    commands: list[BotCommand] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"commands": [command.to_json() for command in self.commands]}


def must_raw_json(value: Any) -> str:
    """对应 Go ``mustRawJSON``：序列化后返回 JSON 文本（失败时返回空串）。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 宽松取值
# ---------------------------------------------------------------------------

def _optional(raw: Any, parse: Callable[[Any], Any]) -> Any:
    """对应 Go 的可空指针字段：JSON null / 缺失 → ``None``。"""
    if raw is None:
        return None
    return parse(raw)


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _as_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


__all__ = [name for name in dir() if not name.startswith("_")]
