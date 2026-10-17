"""Telegram 平台适配器（对应 Go 包 ``internal/platform/telegram``）。

模块划分对应 Go 文件：

==========================  ==========================================
Python 模块                 Go 文件
==========================  ==========================================
``adapter.py``              ``adapter.go``（主循环、入站归一化、出站发送）
``capabilities.py``         ``capabilities.go``（群信息 / 管理员列表）
``client.py``               ``client.go``（Bot API 调用、媒体上传下载）
``config.py``               ``config.go``（配置与装配入口）
``format.py``               ``format.go``（Markdown → HTML 实体渲染）
``keyboard.py``             ``keyboard.go``（风险确认内联键盘）
``message.py``              ``message.go``（入站消息归一化与 Chat History）
``stream.py``               ``stream.go``（流式预览与最终替换）
``types.py``                ``types.go``（Bot API 线上类型）
``target.py``               ``adapter.go`` 里的 ``target`` 与 ``targetKey``（Python 侧拆出）
==========================  ==========================================

导入顺序刻意按依赖排列：``adapter`` 依赖其余全部模块，放在最后，避免包初始化
期间出现"子模块尚未挂到包上"的循环导入。
"""

from __future__ import annotations

from atri_x.platform.telegram import (  # noqa: F401 - 先建立子模块属性，见模块 docstring
    config as config,
)
from atri_x.platform.telegram import format as format  # noqa: A004 - 对齐 Go 的 format.go
from atri_x.platform.telegram import keyboard as keyboard
from atri_x.platform.telegram import message as message
from atri_x.platform.telegram import target as target
from atri_x.platform.telegram import types as types
from atri_x.platform.telegram import client as client
from atri_x.platform.telegram import capabilities as capabilities
from atri_x.platform.telegram import stream as stream
from atri_x.platform.telegram import adapter as adapter

from atri_x.platform.telegram.adapter import (  # noqa: F401
    Adapter,
    new_adapter,
    receipt_with_message_id,
    sleep_context,
    source_from_output,
    target_from_delivery,
    telegram_bot_commands,
    telegram_media_receipt,
    telegram_rich_text_pages,
    telegram_text_pages,
    telegram_text_pages_by_limit,
    valid_telegram_command_name,
)
from atri_x.platform.telegram.client import (  # noqa: F401
    ApiClient,
    MediaSource,
    RedactedTelegramError,
    TelegramAPIError,
    new_api_client,
    new_http_client,
)
from atri_x.platform.telegram.config import (  # noqa: F401
    DEFAULT_API_BASE_URL,
    DEFAULT_FILE_BASE_URL,
    DEFAULT_FORMAT,
    PLATFORM_NAME,
    TELEGRAM_RICH_TEXT_RUNES,
    TELEGRAM_TEXT_PAGE_RUNES,
    Config,
    apply_defaults,
    new_from_platform_config,
    normalize_telegram_format,
)
from atri_x.platform.telegram.format import (  # noqa: F401
    escape_html,
    format_inline_html,
    plain_text_from_markdown,
    telegram_html_from_markdown,
)
from atri_x.platform.telegram.keyboard import (  # noqa: F401
    risk_keyboard,
    should_attach_risk_keyboard,
)
from atri_x.platform.telegram.message import (  # noqa: F401
    NormalizedMessage,
    display_name,
    mentions_from_text,
    normalize_message,
    scope_id,
    strip_bot_mention,
)
from atri_x.platform.telegram.stream import (  # noqa: F401
    MessageStream,
    start_stream,
    stream_preview_text,
)
from atri_x.platform.telegram.types import (  # noqa: F401
    ApiResponse,
    Audio,
    BotCommand,
    CallbackQuery,
    Chat,
    ChatMember,
    Document,
    FileInfo,
    Message,
    PhotoSize,
    Update,
    User,
    Voice,
)

__all__ = [name for name in dir() if not name.startswith("_")]
