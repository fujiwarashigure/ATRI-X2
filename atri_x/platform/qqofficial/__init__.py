"""QQ 官方机器人平台适配器（对应 Go 包 ``internal/platform/qqofficial``）。

模块划分对应 Go 文件：

==========================  ==========================================
Python 模块                 Go 文件
==========================  ==========================================
``adapter.py``              ``adapter.go``（生命周期、落点展开、日志）
``attachment.py``           ``attachment.go``（入站附件 → 消息段）
``client.py``               ``client.go``（HTTP API、媒体上传）
``config.py``               ``config.go``（配置与装配入口）
``gateway.py``              ``gateway.go``（WebSocket 网关、心跳、重连判定）
``keyboard.py``             ``keyboard.go``（风险确认内联键盘）
``message.py``              ``message.go``（入站归一化与 Chat History）
``send.py``                 ``send.go``（出站：文本分页 / Markdown 降级 / 媒体）
``target.py``               ``adapter.go`` 的 ``sendTarget`` 与 ``targetKey``（Python 侧拆出）
``token.py``                ``token.go``（access token 缓存与刷新）
``types.py``                ``types.go``（线上类型与 opcode / 事件名常量）
==========================  ==========================================

导入顺序刻意按依赖排列：``adapter`` 依赖其余全部模块，放在最后，避免包初始化
期间出现"子模块尚未挂到包上"的循环导入。
"""

from __future__ import annotations

from atri_x.platform.qqofficial import (  # noqa: F401 - 先建立子模块属性，见模块 docstring
    types as types,
)
from atri_x.platform.qqofficial import target as target
from atri_x.platform.qqofficial import keyboard as keyboard
from atri_x.platform.qqofficial import config as config
from atri_x.platform.qqofficial import token as token
from atri_x.platform.qqofficial import client as client
from atri_x.platform.qqofficial import attachment as attachment
from atri_x.platform.qqofficial import message as message
from atri_x.platform.qqofficial import send as send
from atri_x.platform.qqofficial import gateway as gateway
from atri_x.platform.qqofficial import adapter as adapter

from atri_x.platform.qqofficial.adapter import (  # noqa: F401
    Adapter,
    new_adapter,
    raise_context_error,
    sleep_context,
)
from atri_x.platform.qqofficial.attachment import (  # noqa: F401
    attachment_declared_mime_type,
    content_disposition_filename,
    extension_from_content_type,
    inbound_attachment_name,
    inbound_attachment_segments,
    is_image_attachment,
    is_image_url,
    looks_like_qq_download_name,
    sanitize_filename,
)
from atri_x.platform.qqofficial.client import (  # noqa: F401
    ApiClient,
    PreparedSource,
    prepare_source,
    target_api_path,
)
from atri_x.platform.qqofficial.config import (  # noqa: F401
    DEFAULT_API_BASE_URL,
    DEFAULT_TOKEN_URL,
    PLATFORM_NAME,
    Config,
    apply_defaults,
    new_from_platform_config,
    resolve_secret,
)
from atri_x.platform.qqofficial.gateway import (  # noqa: F401
    DEFAULT_HEARTBEAT_INTERVAL,
    RECONNECT_IDENTIFY,
    RECONNECT_RESUME,
    GatewayState,
    ReconnectReason,
    classify_gateway_error,
    handle_dispatch,
    read_payload,
    reconnect_mode_text,
    run_gateway_once,
    send_identify,
    send_resume,
    write_gateway,
)
from atri_x.platform.qqofficial.keyboard import (  # noqa: F401
    command_button,
    risk_keyboard,
    should_attach_risk_keyboard,
)
from atri_x.platform.qqofficial.message import (  # noqa: F401
    META_EVENT_ID,
    META_EVENT_TYPE,
    META_GROUP_ID,
    META_MEMBER_ID,
    META_MSG_ID,
    append_non_text_segments,
    final_message_segments,
    handle_c2c_message,
    handle_group_message,
    handle_inbound_message,
    inbound_reference_fetcher,
    inbound_reply_id,
    inbound_route,
    inbound_segments,
    is_configured_superadmin,
    normalized_inbound_text,
    parse_rfc3339_nano,
    record_chat_message,
)
from atri_x.platform.qqofficial.send import (  # noqa: F401
    FILE_TYPE_FILE,
    FILE_TYPE_IMAGE,
    MSG_TYPE_ARK,
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_MEDIA,
    MSG_TYPE_TEXT,
    QQ_OFFICIAL_TEXT_PAGE_RUNES,
    base_message,
    context_target,
    meta_string,
    qq_official_media_receipt,
    qq_official_text_pages,
    receipt_with_message_id,
    send_ark,
    send_context_output,
    send_media,
    send_output,
    send_text,
    send_text_pages,
)
from atri_x.platform.qqofficial.target import (  # noqa: F401
    TARGET_C2C,
    TARGET_GROUP,
    TARGET_KEY,
    SendTarget,
    is_group_context,
    target_from_context,
    target_scope,
    with_target,
)
from atri_x.platform.qqofficial.token import (  # noqa: F401
    DEFAULT_EXPIRES_SECONDS,
    REFRESH_MARGIN_SECONDS,
    TokenSource,
    parse_expires_in,
    parse_expires_text,
)
from atri_x.platform.qqofficial.types import (  # noqa: F401
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_AT_MESSAGE_CREATE,
    EVENT_GROUP_MESSAGE_CREATE,
    EVENT_READY,
    EVENT_RESUMED,
    INTENT_GROUP_AND_C2C,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    AccessTokenRequest,
    AccessTokenResponse,
    ApiErrorResponse,
    GatewayResponse,
    HelloData,
    IdentifyData,
    InboundAuthor,
    InboundMessage,
    InlineKeyboardContent,
    KeyboardButton,
    KeyboardButtonAction,
    KeyboardPermission,
    KeyboardRenderData,
    KeyboardRow,
    MessageArk,
    MessageArkKV,
    MessageArkObj,
    MessageArkObjKV,
    MessageAttachment,
    MessageKeyboard,
    MessageMarkdown,
    MessageMarkdownParam,
    MessageMedia,
    MessageReference,
    MessageResponse,
    MessageToCreate,
    Payload,
    ReadyData,
    ReadyUser,
    ResumeData,
    UploadFileRequest,
    UploadFileResponse,
    omit_empty,
)

__all__ = [name for name in dir() if not name.startswith("_")]
