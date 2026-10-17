"""平台适配层：适配器契约、连接信号、配置与媒体编解码。

对应 Go 包：``internal/platform``（本包覆盖 ``platform.go`` / ``backoff.go`` /
``media.go`` / ``config.go``；``cli/``、``qq-onebot/``、``qqofficial/``、
``telegram/``、``builtin/``、``headless/``、``refcontext/`` 在各自的 Python 模块里）。
"""

from __future__ import annotations

from atri_x.platform.backoff import (  # noqa: F401
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_MAX,
    Backoff,
    new_backoff,
)
from atri_x.platform.config import (  # noqa: F401
    command_name,
    decode_config,
    has_command_prefix,
    primary_command_prefix,
    strip_trigger_keyword,
)
from atri_x.platform.media import marshal_chat_segments, unmarshal_chat_segments  # noqa: F401
from atri_x.platform.platform import (  # noqa: F401
    ConnectNotifier,
    ConnectedEvent,
    ContextSender,
    ConversationChannel,
    ConversationGroup,
    ConversationKind,
    ConversationPrivate,
    ConversationUnknown,
    EventKind,
    EventMetaEvent,
    EventNotice,
    EventRequest,
    GroupAssetProvider,
    GroupDirectoryProvider,
    GroupHistoryProvider,
    GroupInfo,
    GroupMember,
    HistoryMessage,
    Identity,
    MediaResolver,
    Mention,
    MessageContext,
    MessageSegment,
    MessageSegmentType,
    MessageSender,
    MessageStream,
    Notice,
    Output,
    PlatformAdapter,
    PlatformConnected,
    PlatformConnectedSignal,
    PlatformEvent,
    PlatformEventHandler,
    PlatformHandler,
    Receipt,
    ReplyContext,
    Runtime,
    SegmentAt,
    SegmentFile,
    SegmentImage,
    SegmentText,
    SentMessage,
    Source,
    StreamingMessageSender,
    Target,
    UserAvatarProvider,
    message_context_from,
    with_message_context,
)

__all__ = [name for name in dir() if not name.startswith("_")]
