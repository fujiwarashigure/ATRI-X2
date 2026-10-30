"""「提示词」选项卡：自定义提示词模板 / 生图档案 / 工具标签 / 标签库。

四个子页共享同一个配置目录（``app.toml`` 所在目录），但写入的文件不同：

| 子页 | 写哪个文件 | 生效时机 |
|---|---|---|
| 提示词模板 | ``gui/prompts.json`` | 下一轮对话（后台热加载） |
| 生图档案 | ``services.toml`` 的 ``[image_generation.profiles.*]`` | 重启后台服务 |
| 工具标签 | ``tool_tags.toml`` | 下一轮对话（后台热加载） |
| 标签库 | ``gui/tags.json`` | 下一轮对话（后台热加载） |

设计说明：GUI 不跑 Agent，也不通过 WebSocket 改配置——配置就是本地文件，界面
直接读写、后台按文件指纹跟随（与「群友」「人设卡」页一致）。因此这里没有
「应用」按钮，每个子页自己保存。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.imageprofileedit import ImageProfilesPanel
from atri_x.gui.promptedit import PromptTemplatesPanel
from atri_x.gui.taglibedit import TagLibraryPanel
from atri_x.gui.tabs.base import ConfigTab, confirm
from atri_x.gui.tooltagedit import ToolTagsPanel

__all__ = ["PromptLibTab"]


class PromptLibTab(ConfigTab):
    """自定义提示词 / 生图档案 / 工具标签 / 标签库的编辑页。"""

    hint = "各子页保存后按提示生效"

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._panels: dict[str, Any] = {}
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        text = (
            "这里管理「写死在代码里」的那层提示词：生图改写的两套教义、识图四套模板、"
            "群友档案 / 专属人设的注入模板、表情提示与人设卡指引。改过的内置模板保存在 "
            "gui/prompts.json（只存改动，升级后没改过的条目自动跟随新默认）。"
        )
        ttk.Label(
            self.body,
            text=text,
            foreground="#555555",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        notebook = ttk.Notebook(self.body)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook = notebook
        pages = (
            (" 提示词模板 ", PromptTemplatesPanel),
            (" 生图档案 ", ImageProfilesPanel),
            (" 工具标签 ", ToolTagsPanel),
            (" 标签库 ", TagLibraryPanel),
        )
        for title, panel_class in pages:
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=title)
            panel = panel_class(frame, self.paths, self)
            panel.frame.pack(fill=tk.BOTH, expand=True)
            self._panels[title.strip()] = panel

        bar = ttk.Frame(self.body)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="全部重新载入", command=self.reload).pack(side=tk.LEFT)
        ttk.Label(
            bar,
            text="提示词/工具标签/标签库保存后下一轮生效；生图档案需要重启后台服务。",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(12, 0))

    # -- 生命周期 -----------------------------------------------------------
    def reload(self) -> None:
        for panel in self._panels.values():
            try:
                panel.reload()
            except Exception as exc:  # noqa: BLE001 - 单个子页失败不影响其它页
                self.fail(exc)
                return

    def on_show(self) -> None:
        # 不自动重载：用户可能还有没保存的编辑；需要刷新请点「全部重新载入」。
        pass

    # -- 面板回调 -----------------------------------------------------------
    def confirm(self, text: str) -> bool:
        """面板的确认弹窗（统一走基类的 modal 包装，测试里可 mock）。"""
        return confirm(self.frame, "确认", text)
