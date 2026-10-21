"""桌面端的选项卡集合。

| 模块 | 选项卡 | 写哪份配置 |
|---|---|---|
| :mod:`atri_x.gui.tabs.chat` | 对话 | 不写配置（WS 前端） |
| :mod:`atri_x.gui.tabs.model` | 模型 | ``services.toml`` / ``.env`` / ``state.toml``（+ ``gui/model_catalog.json``） |
| :mod:`atri_x.gui.tabs.character` | 人设卡 | ``characters/`` + ``SOUL.md`` |
| :mod:`atri_x.gui.tabs.platform` | 平台 | ``app.toml`` 的 ``[platform.*]`` / ``[security]`` |
| :mod:`atri_x.gui.tabs.link` | 在线 | ``app.toml`` 的 ``[platform.qqonebot].enabled``（连接开关）+ 只读状态 + 内置协议端启动/WebUI |
| :mod:`atri_x.gui.tabs.members` | 群友 | ``gui/members.json`` + ``gui/avatars/``（只读聊天历史库） |
| :mod:`atri_x.gui.tabs.knowledge` | 知识库 | ``app.toml`` 的 ``[group_knowledge]``（只读 ``state.toml`` 的条目） |
| :mod:`atri_x.gui.tabs.advanced` | 进阶 | 生图/视觉/ASR、记忆与群功能开关；自学习/HOOK/维护任务是二级窗口 |
| :mod:`atri_x.gui.tabs.logs` | 日志 | 只读（服务日志 + ``gui/gui.log`` 界面操作日志），可导出 |
| :mod:`atri_x.gui.tabs.features` | 功能 | 只读状态 + 服务/日志入口 |
| :mod:`atri_x.gui.tabs.toolperm` | 工具权限 | ``state.toml`` 的 ``[group_policy]`` 群工具白名单（纵向滚动菜单：激活按钮 + 状态 + 功能解释）+ ``app.toml`` 的群聊工具进度开关 |

公共骨架在 :mod:`atri_x.gui.tabs.base`（底部状态行、错误弹窗、重启按钮、操作日志）。
"""

from __future__ import annotations

from atri_x.gui.tabs.advanced import AdvancedTab
from atri_x.gui.tabs.base import ConfigTab
from atri_x.gui.tabs.character import CharacterTab
from atri_x.gui.tabs.chat import ChatTab
from atri_x.gui.tabs.features import FeaturesTab
from atri_x.gui.tabs.knowledge import KnowledgeTab
from atri_x.gui.tabs.link import LinkTab
from atri_x.gui.tabs.logs import LogsTab
from atri_x.gui.tabs.members import MembersTab
from atri_x.gui.tabs.model import ModelTab
from atri_x.gui.tabs.platform import PlatformTab
from atri_x.gui.tabs.toolperm import ToolPermTab

__all__ = [
    "AdvancedTab",
    "CharacterTab",
    "ChatTab",
    "ConfigTab",
    "FeaturesTab",
    "KnowledgeTab",
    "LinkTab",
    "LogsTab",
    "MembersTab",
    "ModelTab",
    "PlatformTab",
    "ToolPermTab",
]
