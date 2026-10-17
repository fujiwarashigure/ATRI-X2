"""QQ 官方机器人网关与消息 API 的线上类型。

对应 Go：``internal/platform/qqofficial/types.go``。

JSON 键名逐字照抄 Go 的 struct tag，``omitempty`` 逐字段复刻：
:func:`omit_empty` 按 Go 的零值判定（``""`` / ``0`` / ``false`` / 空切片 / 空映射 /
``nil``）丢字段，标注为 ``required`` 的键即使为零值也输出。

形式差异：``json.RawMessage`` → 已解析的 JSON 值（``None`` 表示缺失）；
``*T`` 指针 → ``T | None``；``[2]int`` → ``list[int]``（定长 2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.errors import InvalidError

# --- 网关 opcode 与 intent ---------------------------------------------------
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: 群与单聊消息 intent（``1 << 25``）。
INTENT_GROUP_AND_C2C = 1 << 25

EVENT_READY = "READY"
EVENT_RESUMED = "RESUMED"
EVENT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
EVENT_GROUP_MESSAGE_CREATE = "GROUP_MESSAGE_CREATE"


def _is_empty(value: Any) -> bool:
    """Go ``omitempty`` 的零值判定。"""
    if value is None or value is False:
        return True
    if isinstance(value, (str, bytes, bytearray, list, tuple, dict, set)):
        return len(value) == 0
    if isinstance(value, (int, float)):
        return value == 0
    return False


def omit_empty(values: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """按 Go 的 ``omitempty`` 语义丢字段。"""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in required or not _is_empty(value):
            out[key] = value
    return out


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


# ---------------------------------------------------------------------------
# 网关帧
# ---------------------------------------------------------------------------

@dataclass
class Payload:
    """网关帧。对应 Go ``payload``。"""

    id: str = ""
    op: int = 0
    data: Any = None
    seq: int | None = None
    type: str = ""

    def to_json(self) -> dict[str, Any]:
        """对应 Go ``payload`` 的 json tag（``op`` 无 omitempty）。

        ``s`` 是 ``*int64``：Go 的 omitempty 对**指针**只在 nil 时省略，
        因此 ``s=0`` 必须输出，不能按整型零值丢掉。
        """
        out: dict[str, Any] = {}
        if self.id != "":
            out["id"] = self.id
        out["op"] = self.op
        if self.data is not None:
            out["d"] = self.data
        if self.seq is not None:
            out["s"] = self.seq
        if self.type != "":
            out["t"] = self.type
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "Payload":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial payload: not a JSON object")
        seq = raw.get("s")
        if isinstance(seq, bool) or not isinstance(seq, (int, float)):
            seq = None
        return cls(
            id=_str(raw.get("id")),
            op=_int(raw.get("op")),
            data=raw.get("d"),
            seq=int(seq) if seq is not None else None,
            type=_str(raw.get("t")),
        )


@dataclass
class HelloData:
    """``op=10`` 的心跳间隔。对应 Go ``helloData``。"""

    heartbeat_interval: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"heartbeat_interval": self.heartbeat_interval}

    @classmethod
    def from_json(cls, raw: Any) -> "HelloData":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial hello: not a JSON object")
        return cls(heartbeat_interval=_int(raw.get("heartbeat_interval")))


@dataclass
class IdentifyData:
    """``op=2`` 的鉴权载荷。对应 Go ``identifyData``。"""

    token: str = ""
    intents: int = 0
    shard: list[int] = field(default_factory=lambda: [0, 1])
    properties: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "token": self.token,
                "intents": self.intents,
                "shard": list(self.shard),
                "properties": dict(self.properties),
            },
            required=("token", "intents", "shard"),
        )


@dataclass
class ResumeData:
    """``op=6`` 的续连载荷。对应 Go ``resumeData``。"""

    token: str = ""
    session_id: str = ""
    seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "session_id": self.session_id,
            "seq": self.seq,
        }


@dataclass
class ReadyUser:
    """``READY`` 事件里的机器人身份。对应 Go ``readyUser``。"""

    id: str = ""
    username: str = ""
    bot: bool = False

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {"id": self.id, "username": self.username, "bot": self.bot},
            required=("id", "username", "bot"),
        )

    @classmethod
    def from_json(cls, raw: Any) -> "ReadyUser":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            id=_str(raw.get("id")),
            username=_str(raw.get("username")),
            bot=raw.get("bot") is True,
        )


@dataclass
class ReadyData:
    """``READY`` 事件体。对应 Go ``readyData``。"""

    version: int = 0
    session_id: str = ""
    user: ReadyUser = field(default_factory=ReadyUser)
    shard: list[int] = field(default_factory=lambda: [0, 1])

    @classmethod
    def from_json(cls, raw: Any) -> "ReadyData":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial ready: not a JSON object")
        shard = raw.get("shard")
        values = [0, 1]
        if isinstance(shard, list) and len(shard) == 2:
            values = [_int(shard[0]), _int(shard[1])]
        return cls(
            version=_int(raw.get("version")),
            session_id=_str(raw.get("session_id")),
            user=ReadyUser.from_json(raw.get("user")),
            shard=values,
        )


@dataclass
class GatewayResponse:
    """``GET /gateway`` 的响应。对应 Go ``gatewayResponse``。"""

    url: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "GatewayResponse":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial gateway response: not a JSON object")
        return cls(url=_str(raw.get("url")))


@dataclass
class AccessTokenRequest:
    """取 access token 的请求体。对应 Go ``accessTokenRequest``。"""

    app_id: str = ""
    client_secret: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"appId": self.app_id, "clientSecret": self.client_secret}


@dataclass
class AccessTokenResponse:
    """取 access token 的响应。对应 Go ``accessTokenResponse``。

    ``expires_in`` 在真实响应里既可能是数字也可能是字符串（Go 用 ``any`` 接），
    因此这里保留原值，由 :func:`atri_x.platform.qqofficial.token.parse_expires_in`
    解释。
    """

    access_token: str = ""
    expires_in: Any = None
    code: int = 0
    message: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "AccessTokenResponse":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial access token: not a JSON object")
        return cls(
            access_token=_str(raw.get("access_token")),
            expires_in=raw.get("expires_in"),
            code=_int(raw.get("code")),
            message=_str(raw.get("message")),
        )


@dataclass
class ApiErrorResponse:
    """QQ 官方 API 的错误体。对应 Go ``apiErrorResponse``。"""

    code: int = 0
    message: str = ""
    data: Any = None

    @classmethod
    def from_json(cls, raw: Any) -> "ApiErrorResponse":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(code=_int(raw.get("code")), message=_str(raw.get("message")), data=raw.get("data"))


# ---------------------------------------------------------------------------
# 入站消息
# ---------------------------------------------------------------------------

@dataclass
class InboundAuthor:
    """入站消息的作者身份。对应 Go ``inboundAuthor``。"""

    user_openid: str = ""
    member_openid: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "InboundAuthor":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            user_openid=_str(raw.get("user_openid")),
            member_openid=_str(raw.get("member_openid")),
        )


@dataclass
class MessageAttachment:
    """入站附件。对应 Go ``messageAttachment``。"""

    url: str = ""
    content: str = ""
    content_type: str = ""
    filename: str = ""
    height: int = 0
    width: int = 0
    size: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> "MessageAttachment":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            url=_str(raw.get("url")),
            content=_str(raw.get("content")),
            content_type=_str(raw.get("content_type")),
            filename=_str(raw.get("filename")),
            height=_int(raw.get("height")),
            width=_int(raw.get("width")),
            size=_int(raw.get("size")),
        )


@dataclass
class MessageReference:
    """被引用消息。对应 Go ``messageReference``。"""

    message_id: str = ""
    content: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "MessageReference | None":
        if not isinstance(raw, Mapping):
            return None
        return cls(message_id=_str(raw.get("message_id")), content=_str(raw.get("content")))


@dataclass
class InboundMessage:
    """一条入站消息。对应 Go ``inboundMessage``。"""

    id: str = ""
    group_openid: str = ""
    author: InboundAuthor = field(default_factory=InboundAuthor)
    content: str = ""
    timestamp: str = ""
    attachments: list[MessageAttachment] = field(default_factory=list)
    message_reference: MessageReference | None = None

    @classmethod
    def from_json(cls, raw: Any) -> "InboundMessage":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial message: not a JSON object")
        attachments = raw.get("attachments")
        items: list[MessageAttachment] = []
        if isinstance(attachments, list):
            items = [MessageAttachment.from_json(item) for item in attachments]
        return cls(
            id=_str(raw.get("id")),
            group_openid=_str(raw.get("group_openid")),
            author=InboundAuthor.from_json(raw.get("author")),
            content=_str(raw.get("content")),
            timestamp=_str(raw.get("timestamp")),
            attachments=items,
            message_reference=MessageReference.from_json(raw.get("message_reference")),
        )


# ---------------------------------------------------------------------------
# 出站消息
# ---------------------------------------------------------------------------

@dataclass
class MessageMarkdownParam:
    """Markdown 模板参数。对应 Go ``messageMarkdownParam``。"""

    key: str = ""
    values: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "values": list(self.values)}


@dataclass
class MessageMarkdown:
    """Markdown 消息体。对应 Go ``messageMarkdown``。"""

    content: str = ""
    template_id: int = 0
    params: list[MessageMarkdownParam] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "content": self.content,
                "template_id": self.template_id,
                "params": [param.to_json() for param in self.params],
            }
        )


@dataclass
class MessageMedia:
    """媒体消息体。对应 Go ``messageMedia``。"""

    file_info: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"file_info": self.file_info}


@dataclass
class KeyboardPermission:
    """按钮权限。对应 Go ``keyboardPermission``。"""

    type: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type}


@dataclass
class KeyboardRenderData:
    """按钮展示数据。对应 Go ``keyboardRenderData``。"""

    label: str = ""
    visited_label: str = ""
    style: int = 0

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "label": self.label,
                "visited_label": self.visited_label,
                "style": self.style,
            },
            required=("label", "visited_label"),
        )


@dataclass
class KeyboardButtonAction:
    """按钮动作。对应 Go ``keyboardButtonAction``。"""

    type: int = 0
    permission: KeyboardPermission = field(default_factory=KeyboardPermission)
    click_limit: int = 0
    unsupport_tips: str = ""
    data: str = ""
    enter: bool = False
    reply: bool = False

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "type": self.type,
                "permission": self.permission.to_json(),
                "click_limit": self.click_limit,
                "unsupport_tips": self.unsupport_tips,
                "data": self.data,
                "enter": self.enter,
                "reply": self.reply,
            },
            required=("type", "permission", "data"),
        )


@dataclass
class KeyboardButton:
    """一个按钮。对应 Go ``keyboardButton``。"""

    id: str = ""
    render_data: KeyboardRenderData = field(default_factory=KeyboardRenderData)
    action: KeyboardButtonAction = field(default_factory=KeyboardButtonAction)

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "id": self.id,
                "render_data": self.render_data.to_json(),
                "action": self.action.to_json(),
            },
            required=("render_data", "action"),
        )


@dataclass
class KeyboardRow:
    """一行按钮。对应 Go ``keyboardRow``。"""

    buttons: list[KeyboardButton] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"buttons": [button.to_json() for button in self.buttons]}


@dataclass
class InlineKeyboardContent:
    """内联键盘内容。对应 Go ``inlineKeyboardContent``。"""

    rows: list[KeyboardRow] = field(default_factory=list)
    bot_appid: str = ""

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "rows": [row.to_json() for row in self.rows],
                "bot_appid": self.bot_appid,
            },
            required=("rows",),
        )


@dataclass
class MessageKeyboard:
    """键盘（模板 ID 或内联内容）。对应 Go ``messageKeyboard``。"""

    id: str = ""
    content: InlineKeyboardContent | None = None

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "id": self.id,
                "content": self.content.to_json() if self.content is not None else None,
            }
        )


@dataclass
class MessageArkObjKV:
    """Ark 对象键值。对应 Go ``messageArkObjKV``。"""

    key: str = ""
    value: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value}


@dataclass
class MessageArkObj:
    """Ark 对象。对应 Go ``messageArkObj``。"""

    obj_kv: list[MessageArkObjKV] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"obj_kv": [item.to_json() for item in self.obj_kv]}


@dataclass
class MessageArkKV:
    """Ark 键值（``value`` 与 ``obj`` 二选一）。对应 Go ``messageArkKV``。"""

    key: str = ""
    value: str = ""
    obj: list[MessageArkObj] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "key": self.key,
                "value": self.value,
                "obj": [item.to_json() for item in self.obj],
            },
            required=("key",),
        )


@dataclass
class MessageArk:
    """Ark 消息体。对应 Go ``messageArk``。"""

    template_id: int = 0
    kv: list[MessageArkKV] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"template_id": self.template_id, "kv": [item.to_json() for item in self.kv]}


@dataclass
class MessageToCreate:
    """发送消息的请求体。对应 Go ``messageToCreate``。"""

    content: str = ""
    msg_type: int = 0
    markdown: MessageMarkdown | None = None
    keyboard: MessageKeyboard | None = None
    ark: MessageArk | None = None
    media: MessageMedia | None = None
    msg_id: str = ""
    event_id: str = ""
    msg_seq: int = 0
    is_wakeup: bool = False

    def to_json(self) -> dict[str, Any]:
        """对应 Go ``messageToCreate`` 的 json tag（``msg_type`` 无 omitempty）。"""
        return omit_empty(
            {
                "content": self.content,
                "msg_type": self.msg_type,
                "markdown": self.markdown.to_json() if self.markdown is not None else None,
                "keyboard": self.keyboard.to_json() if self.keyboard is not None else None,
                "ark": self.ark.to_json() if self.ark is not None else None,
                "media": self.media.to_json() if self.media is not None else None,
                "msg_id": self.msg_id,
                "event_id": self.event_id,
                "msg_seq": self.msg_seq,
                "is_wakeup": self.is_wakeup,
            },
            required=("msg_type",),
        )


@dataclass
class MessageResponse:
    """发送消息的响应。对应 Go ``messageResponse``。"""

    id: str = ""
    timestamp: Any = None

    @classmethod
    def from_json(cls, raw: Any) -> "MessageResponse":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial message response: not a JSON object")
        return cls(id=_str(raw.get("id")), timestamp=raw.get("timestamp"))


@dataclass
class UploadFileRequest:
    """媒体上传请求体。对应 Go ``uploadFileRequest``。

    ``srv_send_msg`` 没有 omitempty，因此始终输出。
    """

    file_type: int = 0
    url: str = ""
    srv_send_msg: bool = False
    file_data: str = ""

    def to_json(self) -> dict[str, Any]:
        return omit_empty(
            {
                "file_type": self.file_type,
                "url": self.url,
                "srv_send_msg": self.srv_send_msg,
                "file_data": self.file_data,
            },
            required=("file_type", "srv_send_msg"),
        )


@dataclass
class UploadFileResponse:
    """媒体上传响应。对应 Go ``uploadFileResponse``。"""

    file_uuid: str = ""
    file_info: str = ""
    ttl: int = 0
    id: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "UploadFileResponse":
        if not isinstance(raw, Mapping):
            raise InvalidError("decode qqofficial upload response: not a JSON object")
        return cls(
            file_uuid=_str(raw.get("file_uuid")),
            file_info=_str(raw.get("file_info")),
            ttl=_int(raw.get("ttl")),
            id=_str(raw.get("id")),
        )


__all__ = [name for name in dir() if not name.startswith("_")]
