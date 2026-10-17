"""CLI 平台适配器（对应 Go 包 ``internal/platform/cli``）。

模块划分对应 Go 文件：

==========================  ==========================================================
Python 模块                  Go 文件
==========================  ==========================================================
``adapter.py``               ``cli.go``（stdin 循环、TUI 接线、出站发送）
``config.py``                ``config.go``（配置解析与默认值）
``clipboard.py``             ``clipboard.go``（系统剪贴板 + OSC52）
``local_file.py``            ``tui_file_reference.go``（``#文件引用`` 补全与展开）
``protocol.py``              ``remote_protocol.go``（线上消息与 JSON 编解码）
``remote_server.py``         ``remote_server.go``（远程 CLI 服务端）
``remote_client.py``         ``remote_client.go``（远程 CLI 客户端）
``tui/``                     ``tui.go`` / ``tui_copy.go`` / ``tui_mouse.go``（终端界面）
==========================  ==========================================================

``atri_x/platform/builtin.py``（对应 Go ``internal/platform/builtin``）消费的入口是
:func:`new_config_from_platform_config` 与 :func:`new_remote_server`。

导入顺序刻意按依赖排列：``tui`` → ``protocol`` → ``remote_*`` → ``adapter``，
避免包初始化期间出现"子模块尚未挂到包上"的循环导入。
"""

from __future__ import annotations

from atri_x.platform.cli import (  # noqa: F401 - 先建立子模块属性，见模块 docstring
    clipboard as clipboard,
)
from atri_x.platform.cli import config as config
from atri_x.platform.cli import local_file as local_file
from atri_x.platform.cli import tui as tui
from atri_x.platform.cli import protocol as protocol
from atri_x.platform.cli import remote_client as remote_client
from atri_x.platform.cli import remote_server as remote_server
from atri_x.platform.cli import adapter as adapter

from atri_x.platform.cli.adapter import (  # noqa: F401
    PLATFORM_NAME,
    Adapter,
    CliMessageStream,
    new_adapter,
    outputs_text,
    print_line,
)
from atri_x.platform.cli.clipboard import (  # noqa: F401
    AutoClipboardWriter,
    ClipboardError,
    ClipboardWriter,
    Osc52ClipboardWriter,
    SystemClipboardWriter,
    new_clipboard_writer,
    read_tui_clipboard,
)
from atri_x.platform.cli.config import (  # noqa: F401
    DEFAULT_CLIENT,
    DEFAULT_REMOTE_ADDR,
    DEFAULT_REMOTE_PATH,
    ClientConfig,
    Config,
    ServerConfig,
    apply_config_defaults,
    default_config,
    new_config_from_platform_config,
)
from atri_x.platform.cli.local_file import (  # noqa: F401
    LocalFileResolver,
    new_local_file_resolver,
)
from atri_x.platform.cli.protocol import (  # noqa: F401
    REMOTE_WRITE_TIMEOUT,
    RemoteMessage,
    level_from_text,
    level_text,
    remote_notice_level,
    remote_notice_message,
    set_from_strings,
)
from atri_x.platform.cli.remote_client import (  # noqa: F401
    RemoteClient,
    RemoteClientOptions,
    new_remote_client,
)
from atri_x.platform.cli.remote_server import (  # noqa: F401
    RemoteServer,
    RemoteServerOptions,
    new_remote_server,
)

__all__ = [name for name in dir() if not name.startswith("_")]
