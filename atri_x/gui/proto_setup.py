"""协议端环境描述（NapCat / SnowLuma 共用）。

纯数据，不依赖 Tk，也不 import 任何协议端实现——这样 ``qqproto``（NapCat）、
``snowluma_proto``、``service``、GUI 页面都能安全 import 同一个类型，不会成环。

``QqProtoSetup`` 是 1.0.x 的旧名字，保留为别名：外部（打包脚本、测试、旧 import）
不用跟着改名。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.platform.qq_onebot.backend import backend_label

__all__ = ["ProtoSetup", "QqProtoSetup"]


@dataclass(frozen=True)
class ProtoSetup:
    """协议端环境的准备结果（纯数据）。

    字段分两类：

    * **通用**：``available`` / ``reason`` / ``backend`` / 端口 / 令牌 / WebUI；
    * **NapCat 专用**（SnowLuma 留空）：``shell_dir`` / ``start_script`` /
      ``stop_script``——它们是 NapCat Shell 里的批处理。

    ``token`` 用 ``repr=False``：异常和日志顺手打出来的对象不该带令牌。
    """

    available: bool = False
    reason: str = ""
    #: ``napcat`` / ``snowluma``。
    backend: str = "napcat"
    #: 协议端环境目录（NapCat 是 ``qqproto``；SnowLuma 是它自己的安装目录）。
    env_dir: str = ""
    #: NapCat Shell 目录（SnowLuma 为空）。
    shell_dir: str = ""
    #: 协议端自己的配置目录（NapCat 的 ``.../napcat/config``；SnowLuma 的 ``config``）。
    config_dir: str = ""
    start_script: str = ""
    stop_script: str = ""
    onebot_port: int = 0
    webui_port: int = 0
    webui_url: str = ""
    #: OneBot 令牌；``repr=False`` 避免被日志/异常顺手打出来。
    token: str = field(default="", repr=False)
    changed: bool = False
    #: 端口被系统保留/占用而自动改端口时的一句话说明（正常为空）；给界面展示。
    port_note: str = ""
    #: SnowLuma 首启要先同意第三方 EULA/隐私，再设 WebUI 密码；没同意前不代设。
    needs_consent: bool = False
    #: 协议端目录在程序目录之外（用户自备）：界面显示"外部自备（未托管）"。
    external: bool = False

    @property
    def ws_url(self) -> str:
        """后台服务该连的地址（协议端没准备出来时为空串）。"""
        return f"ws://127.0.0.1:{self.onebot_port}/" if self.onebot_port else ""

    @property
    def label(self) -> str:
        """一句话描述，给「在线」页用。"""
        if not self.available:
            return self.reason or "未找到可用的协议端环境"
        name = backend_label(self.backend)
        prefix = "外部" if self.external else "内置"
        return f"{prefix} {name}（端口 {self.onebot_port}）"

    @property
    def name(self) -> str:
        """协议端显示名（``NapCat`` / ``SnowLuma``）。"""
        return backend_label(self.backend)


#: 1.0.x 的旧名字。
QqProtoSetup = ProtoSetup
