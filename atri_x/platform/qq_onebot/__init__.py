"""QQ OneBot v11 平台适配器（对应 Go 包 ``internal/platform/qq-onebot``）。

模块划分对应 Go 文件：

==========================  ==========================================
Python 模块                 Go 文件
==========================  ==========================================
``adapter.py``              ``adapter.go``（主循环、出站发送）
``attachment.py``           ``attachment.go``（入站媒体解析）
``capabilities.py``         ``capabilities.go``（群历史 / 群资料 / 群成员 / 头像）
``config.py``               ``adapter.go`` 的 ``Config`` / ``applyDefaults`` /
                            ``NewFromPlatformConfig``（Python 侧拆出）
``forward.py``              ``message.go`` 的转发展开与自由函数（Python 侧拆出）
``inbound.py``              ``inbound.go``（去重、限流、事件分发）
``message.py``              ``message.go``（归一化）
``output.py``               ``adapter.go`` 的输出→段 / 分页 / 回执（Python 侧拆出）
``preprocess.py``           ``inbound.go`` 的 ``prepareInbound`` 与 ``adapter.go``
                            的网络型入站预处理（Python 侧拆出）
``target.py``               ``adapter.go`` 的 ``target`` 与 ``targetKey``（Python 侧拆出）
``transport.py``            ``transport.go``（WebSocket 传输与动作表）
``types.py``                ``message.go`` 的类型声明（Python 侧拆出）
==========================  ==========================================

导入顺序刻意按依赖排列：``adapter`` 依赖其余全部模块，放在最后，避免包初始化
期间出现"子模块尚未挂到包上"的循环导入。
"""

from __future__ import annotations

from atri_x.platform.qq_onebot import (  # noqa: F401 - 先建立子模块属性，见模块 docstring
    types as types,
)
from atri_x.platform.qq_onebot import forward as forward
from atri_x.platform.qq_onebot import message as message
from atri_x.platform.qq_onebot import target as target
from atri_x.platform.qq_onebot import output as output
from atri_x.platform.qq_onebot import transport as transport
from atri_x.platform.qq_onebot import attachment as attachment
from atri_x.platform.qq_onebot import capabilities as capabilities
from atri_x.platform.qq_onebot import inbound as inbound
from atri_x.platform.qq_onebot import preprocess as preprocess
from atri_x.platform.qq_onebot import config as config
from atri_x.platform.qq_onebot import adapter as adapter

from atri_x.platform.qq_onebot.adapter import (  # noqa: F401
    Adapter,
    new_adapter,
)
from atri_x.platform.qq_onebot.attachment import (  # noqa: F401
    resolve_media,
)
from atri_x.platform.qq_onebot.capabilities import (  # noqa: F401
    fetch_group_history,
    get_group_info,
    get_group_member_list,
    get_user_avatar_url,
    raw_id_string,
)
from atri_x.platform.qq_onebot.config import (  # noqa: F401
    DEFAULT_WS_URL,
    PLATFORM_NAME,
    Config,
    apply_defaults,
    new_from_platform_config,
    validate_send_file_mode,
)
from atri_x.platform.qq_onebot.forward import (  # noqa: F401
    FORWARD_TRUNCATED_MARKER,
    FORWARD_TRUST_MARKER,
    ForwardParseState,
    at_text,
    clean_text,
    first_non_empty,
    indent_forward_content,
    looks_like_forward_node,
    new_forward_parse_state,
    normalize_forward_segment,
    normalize_forward_segment_with_state,
    render_forward_data,
    segment_data_int64,
    segment_data_string,
)
from atri_x.platform.qq_onebot.inbound import (  # noqa: F401
    DEFAULT_HIGH_PRIORITY_QUEUE,
    DEFAULT_HIGH_PRIORITY_WORKERS,
    DEFAULT_INBOUND_DEDUP_MAX,
    DEFAULT_INBOUND_DEDUP_TTL,
    DEFAULT_PREPROCESS_QUEUE,
    DEFAULT_PREPROCESS_WORKERS,
    INBOUND_DEDUP_STATE_COMPLETED,
    INBOUND_DEDUP_STATE_FAILED,
    INBOUND_DEDUP_STATE_PROCESSING,
    EventDispatcher,
    InboundDeduper,
    InboundRuntime,
    InboundStats,
    OutboundEventJob,
    inbound_key,
    new_inbound_runtime,
    raise_context_error,
    sleep_context,
)
from atri_x.platform.qq_onebot.message import (  # noqa: F401
    display_name,
    file_segment,
    image_segment,
    normalize_message,
    normalize_message_segments,
    normalize_message_with_limits,
    normalize_plain_text,
    normalize_segments,
    normalize_segments_with_limits,
    sender_name,
)
from atri_x.platform.qq_onebot.output import (  # noqa: F401
    QQ_TEXT_PAGE_RUNES,
    SEND_FILE_MODE_BASE64,
    SEND_FILE_MODE_FILE_URI,
    local_path_file_uri,
    one_bot_media_receipt,
    one_bot_source_file,
    output_segments,
    qq_forward_nodes,
    qq_text_pages,
    receipt_with_message_id,
    text_outputs,
)
from atri_x.platform.qq_onebot.preprocess import (  # noqa: F401
    append_non_text_segments,
    conversation_kind,
    final_message_segments,
    first_non_zero,
    is_configured_superadmin,
    is_message_event,
    prepare_inbound,
    record_chat_message,
)
from atri_x.platform.qq_onebot.target import (  # noqa: F401
    TARGET_KEY,
    Target,
    event_scope_id,
    is_group_context,
    one_bot_target_scope,
    scope_id,
    target_from_context,
    target_to_qq,
    with_target,
)
from atri_x.platform.qq_onebot.transport import (  # noqa: F401
    DEFAULT_TIMEOUT,
    GetFileData,
    GetImageData,
    GetMessageData,
    Request,
    Response,
    SendMessageData,
    Transport,
)
from atri_x.platform.qq_onebot.types import (  # noqa: F401
    DEFAULT_FORWARD_FETCH_TIMEOUT,
    DEFAULT_FORWARD_MAX_DEPTH,
    DEFAULT_FORWARD_MAX_FETCHES,
    DEFAULT_FORWARD_MAX_NODES,
    DEFAULT_FORWARD_MAX_NON_TEXT,
    DEFAULT_FORWARD_MAX_RESULT_BYTES,
    DEFAULT_FORWARD_MAX_RUNES,
    Event,
    ForwardLimits,
    NormalizedMessage,
    Segment,
    Sender,
    default_forward_limits,
    parse_int64,
)

__all__ = [name for name in dir() if not name.startswith("_")]
