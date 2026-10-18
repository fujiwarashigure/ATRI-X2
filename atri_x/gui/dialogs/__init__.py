"""「进阶」页的二级设置窗口（自学习 / HOOK 插件 / 定时维护任务）。

这三块设置都是「改完要重启后台服务、平时很少动」的进阶项，所以不占主选项卡，
而是由「进阶」页上的按钮打开独立窗口：

| 模块 | 窗口 | 写哪里 |
|---|---|---|
| :mod:`atri_x.gui.dialogs.selflearning` | 自学习设置 | ``app.toml`` 的 ``[self_learning]`` |
| :mod:`atri_x.gui.dialogs.hooks` | HOOK 插件配置 | ``plugins/hooks.toml`` |
| :mod:`atri_x.gui.dialogs.maintenance` | 定时维护任务（每天/每周/每月/自定义 cron + 一次性） | ``app.toml`` 的 ``[maintenance.*]`` |
| :mod:`atri_x.gui.dialogs.tasks` | 维护任务清单 + 表单 ↔ cron 翻译（无 Tk） | — |

公共骨架（窗口、状态行、按钮条、配置错误弹窗、操作日志）在
:mod:`atri_x.gui.dialogs.common`。
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.gui.dialogs.common import Dialog
from atri_x.gui.dialogs.hooks import HooksDialog
from atri_x.gui.dialogs.maintenance import MaintenanceDialog
from atri_x.gui.dialogs.selflearning import SelfLearningDialog

__all__ = [
    "Dialog",
    "HooksDialog",
    "MaintenanceDialog",
    "SelfLearningDialog",
    "open_hooks",
    "open_maintenance",
    "open_self_learning",
]


def open_self_learning(
    parent: Any, paths: Any, *, on_saved: Callable[[], None] | None = None
) -> SelfLearningDialog:
    """打开「自学习设置」窗口。"""
    return SelfLearningDialog(parent, paths, on_saved=on_saved)


def open_hooks(
    parent: Any, paths: Any, *, on_saved: Callable[[], None] | None = None
) -> HooksDialog:
    """打开「HOOK 插件配置」窗口。"""
    return HooksDialog(parent, paths, on_saved=on_saved)


def open_maintenance(
    parent: Any, paths: Any, *, on_saved: Callable[[], None] | None = None
) -> MaintenanceDialog:
    """打开「定时维护任务」窗口。"""
    return MaintenanceDialog(parent, paths, on_saved=on_saved)
