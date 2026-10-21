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
| :mod:`atri_x.gui.app` | Tk 主窗口、选项卡容器、菜单、状态栏、托盘联动 |
| :mod:`atri_x.gui.actions` | 「服务」动作：启动/重启/停止后台服务、打开目录/日志、关于 |
| :mod:`atri_x.gui.about` | 「关于 ATRI-X」窗口（简介 / 仓库地址 / 推荐语 / 署名） |
| :mod:`atri_x.gui.tabs` | 各选项卡：对话 / 模型 / 人设卡 / 平台 / 在线 / 群友 / 知识库 / 进阶 / 日志 / 功能 / 工具权限 |
| :mod:`atri_x.gui.dialogs` | 「进阶」页的二级窗口：自学习 / HOOK 插件 / 定时维护任务（任务清单与表单翻译在 ``dialogs.tasks``） |
| :mod:`atri_x.gui.cronschedule` | 界面用的 cron 小工具：「每天 / 每周 / 每月」结构 ↔ 5 段表达式、人话摘要、下次触发 |
| :mod:`atri_x.gui.widgets` | 密钥显示/隐藏输入框、可滚动下拉、竖向滚动容器、后台 IO 回主线程 |
| :mod:`atri_x.gui.toolcatalog` | 「工具权限」页工具目录：中文名 / 一句话功能解释 / 分组 / 风险等级（静态） |
| :mod:`atri_x.gui.toolmenu` | 「工具权限」页的纵向工具菜单行：激活按钮 + 状态徽标 + 功能解释 |
| :mod:`atri_x.gui.toolpolicy` | 「工具权限」页的群工具白名单读写与预设（无 Tk） |
| :mod:`atri_x.gui.modelcatalog` | 模型目录（``/models`` 拉取 + 本地备注名） |
| :mod:`atri_x.gui.imagemount` | 本地生图服务挂载（端口映射 / SSL 链接 → OpenAI 兼容 Base URL） |
| :mod:`atri_x.gui.memberdata` | 群友档案 + 从聊天历史库发现群友 |
| :mod:`atri_x.gui.avatars` | 头像下载与 PNG 缓存（纯标准库图像编解码） |
| :mod:`atri_x.gui.journal` | 界面操作日志（``<配置目录>/gui/gui.log``） |
| :mod:`atri_x.gui.configedit` | 配置路径、TOML 行级补丁、``.env``、原子写 + 备份 |
| :mod:`atri_x.gui.configmodel` | LLM Provider 与模型分配的数据模型 |
| :mod:`atri_x.gui.featureview` | 「功能」页的能力/开关状态汇总 |
| :mod:`atri_x.gui.client` | WS 客户端（握手/收发/断线重连） |
| :mod:`atri_x.gui.protocol` | 帧 → 界面状态的纯逻辑（聊天记录、状态栏文本、补全） |
| :mod:`atri_x.gui.service` | 配置与 token 准备、后台服务探测/拉起/停止/重启 |
| :mod:`atri_x.gui.qqproto` | 内置 NapCat/便携 QQ 协议端：令牌、端口、NapCat 配置与 `[platform.qqonebot]` 补齐 |
| :mod:`atri_x.gui.linkstate` | 「在线」页的数据层：平台配置、协议端端口探测、``/healthz`` 健康快照（无 Tk） |
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
