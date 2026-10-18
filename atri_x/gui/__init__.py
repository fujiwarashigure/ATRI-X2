"""``atri_x.gui``：Windows 桌面端（Tk 前端 + 托盘 + 后台服务管理）。

用法::

    python -m atri_x.gui                 # 启动桌面端（必要时自动拉起后台服务）
    python -m atri_x.gui --minimized     # 托盘常驻启动（开机自启用这个）
    python -m atri_x.gui --selftest      # 不连服务，只验证 Tk 界面能构建
    python -m atri_x.gui --no-service    # 只连接已有的后台服务，不自己拉

架构要点（详见 ``docs/windows-app.md``）：

- **GUI 不是 Agent**：Agent 跑在 ``atri-x service run`` 这个独立进程里，GUI 只是
  ``docs/frontend-api.md`` 那套 WS 协议的前端；
- **关窗口不退出**：默认隐藏到托盘，后台服务继续跑，QQ 机器人保持在线；
- **服务可自带也可复用**：探测到 ``ws://127.0.0.1:32172`` 可用就直接连，否则按
  :func:`atri_x.gui.service.prepare_settings` 准备配置并拉起服务。

模块职责：

| 模块 | 职责 |
|---|---|
| :mod:`atri_x.gui.app` | Tk 主窗口、菜单、状态栏、托盘联动 |
| :mod:`atri_x.gui.client` | WS 客户端（握手/收发/断线重连） |
| :mod:`atri_x.gui.protocol` | 帧 → 界面状态的纯逻辑（聊天记录、状态栏文本、补全） |
| :mod:`atri_x.gui.service` | 配置与 token 准备、后台服务探测/拉起/停止 |
| :mod:`atri_x.gui.tray` | ``ctypes`` 写的托盘图标（无第三方依赖） |
| :mod:`atri_x.gui.autostart` | 开机自启（注册表 ``Run`` 项） |
"""

from __future__ import annotations

from atri_x.gui.app import WINDOW_TITLE, GuiApp
from atri_x.gui.client import RemoteClient
from atri_x.gui.protocol import Transcript
from atri_x.gui.service import GuiConfigError, GuiSettings, prepare_settings

__all__ = [
    "WINDOW_TITLE",
    "GuiApp",
    "GuiConfigError",
    "GuiSettings",
    "RemoteClient",
    "Transcript",
    "prepare_settings",
]
