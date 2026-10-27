"""平台抽象：适配器、入站/出站类型、可选能力接口与"平台已连接"信号。

对应 Go：``internal/platform/platform.go``。

与 Go 的差异（仅形式）：

* Go 的接口 → ``typing.Protocol``（``@runtime_checkable``），因此
  ``isinstance(adapter, GroupHistoryProvider)`` 与 Go 的类型断言同样可用。
* ``context.Context`` → 鸭子类型 ``ctx``（``Mapping`` /
  :class:`atri_x.safecontext.CancelToken`），读写助手复用 :mod:`atri_x.delivery`。
* ``signal.New[ConnectedEvent, signal.Empty]`` → :class:`PlatformConnectedSignal`：
  进程生命期内稳定的全局信号、不重放。``atri_x/signal/`` 落地后应换成它创建的
  信号，调用点（``connect`` / ``emit`` / ``emit_async``）保持不变。
* Go 的 ``Actor`` / ``Scope`` 分别属于 ``internal/security`` / ``internal/session``
  （本文件里没有这两个类型，Go 侧同样没有）；``GroupRole`` 的定义在
  :mod:`atri_x.tool.capability`，此处原样再导出，避免出现两份互不相认的枚举。
* ``Receipt`` / ``SentMessage`` / ``Target`` / ``Notice`` / ``Output`` 属于
  ``internal/delivery``，本模块再导出，使平台契约能一处取全。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from atri_x.delivery import (
    ContextSender,
    MessageSender,
    MessageStream,
    Notice,
    Output,
    Receipt,
    SentMessage,
    Source,
    StreamingMessageSender,
    Target,
    ctx_value,
    ctx_with_value,
)
from atri_x.tool.capability import GroupRole, GroupRoleUnknown


class EventKind:
    """非消息事件的平台中立分类。对应 Go ``EventKind``。"""

    NOTICE = "notice"
    REQUEST = "request"
    META_EVENT = "meta_event"

    ALL = (NOTICE, REQUEST, META_EVENT)


EventNotice = EventKind.NOTICE
EventRequest = EventKind.REQUEST
EventMetaEvent = EventKind.META_EVENT


class MessageSegmentType:
    """入站消息段的类型。对应 Go ``MessageSegmentType``。"""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AT = "at"

    ALL = (TEXT, IMAGE, FILE, AT)


SegmentText = MessageSegmentType.TEXT
SegmentImage = MessageSegmentType.IMAGE
SegmentFile = MessageSegmentType.FILE
SegmentAt = MessageSegmentType.AT


class ConversationKind:
    """会话种类。对应 Go ``ConversationKind``。"""

    UNKNOWN = "unknown"
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"

    ALL = (UNKNOWN, PRIVATE, GROUP, CHANNEL)


ConversationUnknown = ConversationKind.UNKNOWN
ConversationPrivate = ConversationKind.PRIVATE
ConversationGroup = ConversationKind.GROUP
ConversationChannel = ConversationKind.CHANNEL


@dataclass
class MessageSegment:
    """从入站平台消息里解析出的一段带类型内容。对应 Go ``MessageSegment``。

    注意：这与 :class:`atri_x.llm.llm.MessageSegment` 是两个类型（Go 亦然）：
    本类型多出 ``user_id`` / ``platform_file_id`` / ``size``。
    """

    type: str = MessageSegmentType.TEXT
    text: str = ""
    user_id: str = ""
    url: str = ""
    media_id: str = ""
    platform_file_id: str = ""
    mime_type: str = ""
    name: str = ""
    size: int = 0
    #: 平台私有元数据（**Python 侧新增**，JSON 键 ``platform_meta``）。
    #: 只承载"解析媒体时还要用、但跨平台没有统一语义"的字段，例：QQ 私聊文件下载
    #: 需要入站 ``file`` 段的 ``file_hash`` 与归属的群号/用户号。空字典不进 JSON。
    platform_meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """对应 Go ``MessageSegment`` 的 json tag（除 type 外全部 omitempty）。"""
        out: dict[str, Any] = {"type": self.type}
        for key, value in (
            ("text", self.text),
            ("user_id", self.user_id),
            ("url", self.url),
            ("media", self.media_id),
            ("platform_file_id", self.platform_file_id),
            ("mime_type", self.mime_type),
            ("name", self.name),
            ("size", self.size),
            ("platform_meta", self.platform_meta),
        ):
            if value:
                out[key] = value
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "MessageSegment":
        """对应 Go ``encoding/json`` 反序列化（未知键忽略）。"""
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls()
        size = raw.get("size")
        meta = raw.get("platform_meta")
        return cls(
            type=str(raw.get("type") or MessageSegmentType.TEXT),
            text=str(raw.get("text") or ""),
            user_id=str(raw.get("user_id") or ""),
            url=str(raw.get("url") or ""),
            media_id=str(raw.get("media") or ""),
            platform_file_id=str(raw.get("platform_file_id") or ""),
            mime_type=str(raw.get("mime_type") or ""),
            name=str(raw.get("name") or ""),
            size=int(size) if isinstance(size, (int, float)) else 0,
            platform_meta=dict(meta) if isinstance(meta, dict) else {},
        )


@dataclass
class PlatformEvent:
    """notice / request / 生命周期事件的平台中立视图。对应 Go ``PlatformEvent``。

    刻意保持很小：平台特有字段留在 ``meta`` / ``raw``，本地事件策略只需要稳定的
    路由字段。
    """

    platform: str = ""
    kind: str = ""
    type: str = ""
    scope_id: str = ""
    user_id: str = ""
    message_id: str = ""
    raw: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectedEvent:
    """描述"某个平台连接成功"。对应 Go ``ConnectedEvent``。

    只带发布方（适配器）的非空 ``platform``，不含会话、用户或消息身份。
    """

    platform: str = ""


class PlatformConnectedSignal:
    """"平台已连接"全局信号：进程生命期内稳定、不重放。对应 Go ``PlatformConnected``。

    Go 侧是 ``signal.New[ConnectedEvent, signal.Empty]("platform.connected")``。
    ``atri_x/signal/`` 由并行任务负责，落地后这里应换成它创建的信号；本类只保留
    订阅/发布这一最小语义，方法名刻意与其保持一致（``connect`` / ``disconnect`` /
    ``emit`` / ``emit_async``）。

    发布方直接发布；每个订阅方自己持有订阅、队列和关闭，不经过 App 或别的消费者
    转发。订阅按注册顺序串行执行。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._handlers: list[Callable[..., Any]] = []

    def connect(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """订阅；返回同一个 handler，便于当注销句柄用。"""
        self._handlers.append(handler)
        return handler

    def disconnect(self, handler: Callable[..., Any]) -> None:
        """退订（幂等）。"""
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def handlers(self) -> list[Callable[..., Any]]:
        """当前订阅者快照（发布时按快照遍历，允许 handler 内退订）。"""
        return list(self._handlers)

    def emit(self, ctx: Any, event: ConnectedEvent) -> None:
        """同步发布。handler 是协程函数时抛 ``TypeError``（应改用 ``emit_async``）。"""
        for handler in self.handlers():
            result = handler(ctx, event)
            if asyncio.iscoroutine(result):
                result.close()
                raise TypeError(
                    "async platform.connected handler requires await emit_async(...)"
                )

    async def emit_async(self, ctx: Any, event: ConnectedEvent) -> None:
        """异步发布：按注册顺序串行等待每个 handler。"""
        for handler in self.handlers():
            result = handler(ctx, event)
            if asyncio.iscoroutine(result):
                await result


# PlatformConnected 是进程生命期内稳定的全局信号（不重放）。
PlatformConnected = PlatformConnectedSignal("platform.connected")


# ---------------------------------------------------------------------------
# 适配器接口（对应 Go 的 interface；用 Protocol 保留类型断言语义）
# ---------------------------------------------------------------------------

@runtime_checkable
class PlatformHandler(Protocol):
    """处理平台入站消息。对应 Go ``PlatformHandler``。"""

    async def handle_message(self, ctx: Any, text: str) -> None: ...


@runtime_checkable
class PlatformEventHandler(Protocol):
    """除聊天消息外还想要 notice/request/生命周期事件的处理器。"""

    async def handle_platform_event(self, ctx: Any, event: PlatformEvent) -> None: ...


@runtime_checkable
class ConnectNotifier(Protocol):
    """能上报"平台连接成功"的适配器。对应 Go ``ConnectNotifier``。"""

    def set_connect_notifier(self, fn: Callable[[Any, str], Any] | None) -> None: ...


@runtime_checkable
class MediaResolver(Protocol):
    """把入站媒体段解析成可发送的媒体来源。对应 Go ``MediaResolver``。"""

    async def resolve_media(self, ctx: Any, segment: MessageSegment, max_bytes: int) -> Source: ...


@runtime_checkable
class GroupHistoryProvider(Protocol):
    """能拉远端群历史；调用方必须把它当可选能力。对应 Go ``GroupHistoryProvider``。"""

    async def fetch_group_history(
        self, ctx: Any, group_id: str, since: Any, until: Any, limit: int
    ) -> list["HistoryMessage"]: ...


@runtime_checkable
class GroupDirectoryProvider(Protocol):
    """能解析群资料与成员列表。对应 Go ``GroupDirectoryProvider``。"""

    async def get_group_info(self, ctx: Any, group_id: str) -> "GroupInfo": ...

    async def get_group_member_list(self, ctx: Any, group_id: str) -> list["GroupMember"]: ...


@runtime_checkable
class UserAvatarProvider(Protocol):
    """能解析用户头像 URL。对应 Go ``UserAvatarProvider``。"""

    async def get_user_avatar_url(self, ctx: Any, user_id: str, size: int) -> str: ...


@runtime_checkable
class GroupAssetProvider(Protocol):
    """可选的群文件/群相册上传。对应 Go ``GroupAssetProvider``。"""

    async def upload_group_file(
        self, ctx: Any, group_id: str, file_path: str, filename: str
    ) -> None: ...

    async def upload_group_album(
        self, ctx: Any, group_id: str, image_path: str, album_id: str, album_name: str
    ) -> None: ...


@runtime_checkable
class Runtime(Protocol):
    """适配器共享的生命周期与发送面。对应 Go ``Runtime``。"""

    def name(self) -> str: ...

    async def run(self, ctx: Any, handler: PlatformHandler) -> None: ...

    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt: ...

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt: ...


# PlatformAdapter 是消息平台适配器（CLI、QQ 等）的接口；Go 侧与 Runtime 定义相同。
PlatformAdapter = Runtime


# ---------------------------------------------------------------------------
# 平台中立的数据结构
# ---------------------------------------------------------------------------

@dataclass
class GroupInfo:
    """群分析用到的平台中立子集。对应 Go ``GroupInfo``。"""

    id: str = ""
    name: str = ""
    member_count: int = 0


@dataclass
class GroupMember:
    """群分析用到的平台中立子集。对应 Go ``GroupMember``。"""

    user_id: str = ""
    nickname: str = ""
    group_card: str = ""
    display_name: str = ""
    role: str = ""


@dataclass
class HistoryMessage:
    """可选群历史能力返回的一行。对应 Go ``HistoryMessage``。

    ``created_at`` 是 UTC aware ``datetime``（Go ``time.Time``）。
    """

    platform_message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    created_at: Any = None


@dataclass
class ReplyContext:
    """入站消息引用的那条消息。对应 Go ``ReplyContext``。"""

    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    segments: list[MessageSegment] = field(default_factory=list)


@dataclass
class Identity:
    """对应 Go ``Identity``。"""

    user_id: str = ""
    username: str = ""


@dataclass
class Mention:
    """对应 Go ``Mention``。"""

    user_id: str = ""
    username: str = ""
    text: str = ""


def _identity() -> Identity:
    return Identity()


def _reply_context() -> ReplyContext:
    return ReplyContext()


_MESSAGE_CONTEXT_KEY = object()


@dataclass
class MessageContext:
    """单条消息的平台路由与 actor 数据。对应 Go ``MessageContext``。"""

    platform: str = ""
    actor_id: str = ""
    platform_user_id: str = ""
    nickname: str = ""
    group_card: str = ""
    display_name: str = ""
    group_role: str = GroupRoleUnknown
    scope_id: str = ""
    session_id: str = ""
    conversation_kind: str = ConversationKind.UNKNOWN
    platform_message_id: str = ""
    reply_to_message_id: str = ""
    reply_to_sender_id: str = ""
    sender: ContextSender | None = None
    buffer_assistant_output: bool = False
    fork_from_message_id: str = ""
    resume_session_id: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    context_text: str = ""
    context_segments: list[MessageSegment] = field(default_factory=list)
    reply: ReplyContext = field(default_factory=_reply_context)
    meta: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    # match_text 是唤醒/命令匹配视图，不含不可信的转发内容。
    # match_text_set 用来区分"刻意匹配空视图（纯转发消息）"与"适配器不提供"。
    match_text: str = ""
    match_text_set: bool = False
    platform_message: str = ""
    bot: Identity = field(default_factory=_identity)
    mentions: list[Mention] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)
    media_resolver: MediaResolver | None = None


def with_message_context(ctx: Any, msg: MessageContext) -> Any:
    """对应 Go ``WithMessageContext``。返回新的 ctx。"""
    return ctx_with_value(ctx, _MESSAGE_CONTEXT_KEY, msg)


def message_context_from(ctx: Any) -> tuple[MessageContext, bool]:
    """对应 Go ``MessageContextFrom``。"""
    msg = ctx_value(ctx, _MESSAGE_CONTEXT_KEY)
    if isinstance(msg, MessageContext):
        return msg, True
    return MessageContext(), False


__all__ = [name for name in dir() if not name.startswith("_")]
