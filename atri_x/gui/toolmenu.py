"""「工具权限」页的纵向工具菜单控件：一行一个工具（激活按钮 + 状态 + 功能解释）。

为什么单独一层：这一行要调 Tk 的颜色/字体（``tk.Frame`` / ``tk.Label`` 才上得了底色），
和页面里的配置读写逻辑混在同一个文件里会让 :mod:`atri_x.gui.tabs.toolperm` 超长；
这里只做"长什么样、怎么跟着状态变"，不做任何授权判断（状态由页面传进来）。

一行的结构::

    [已激活]  网页搜索  web_search  低风险                     [停用]
              联网搜索关键词，返回简洁答案、来源链接和摘要。

点按钮或点整行都会回调 ``on_toggle(tool_name)``；锁定模式（继承全局 / 禁止全部）下
按钮置灰、状态徽标换成对应文案，点击不再改变状态。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

from atri_x.gui.toolcatalog import RISK_COLORS, RISK_LABELS, ToolMeta

__all__ = ["MenuResize", "ToolRow", "category_header"]

# -- 配色 / 字体（tk 控件才能上色：状态徽标、行底色、激活按钮）------------------
_ROW_BG_ACTIVE = "#f1f9f3"
_ROW_BG_IDLE = "#ffffff"
_ROW_BG_LOCKED = "#f6f6f7"
_ROW_BORDER_ACTIVE = "#b9dcc5"
_ROW_BORDER_IDLE = "#e2e5e9"
_HEADER_BG = "#e8ecf1"

#: 激活状态 → (徽标文案, 前景色, 背景色)。
_BADGES = {
    "active": ("已激活", "#146c37", "#d9f2e1"),
    "idle": ("未激活", "#6b7280", "#eceef1"),
    "inherit": ("继承全局", "#2b4d9c", "#e5edfb"),
    "none": ("禁止使用", "#a3271f", "#fbe9e7"),
}

#: 激活按钮 → ``tk.Button`` 配色。
_BUTTON_ON = {
    "bg": "#2e7d4f",
    "fg": "#ffffff",
    "activebackground": "#276b44",
    "activeforeground": "#ffffff",
}
_BUTTON_OFF = {
    "bg": "#e4e7eb",
    "fg": "#333a42",
    "activebackground": "#d5d9de",
    "activeforeground": "#333a42",
}
_BUTTON_LOCKED = {
    "bg": "#ececee",
    "fg": "#a9adb3",
    "activebackground": "#ececee",
    "activeforeground": "#a9adb3",
}

_TITLE_FONT = ("Microsoft YaHei UI", 10, "bold")
_NAME_FONT = ("Consolas", 9)
_SUMMARY_FONT = ("Microsoft YaHei UI", 9)
_SMALL_FONT = ("Microsoft YaHei UI", 9)
_SMALL_FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
_BADGE_FONT = ("Microsoft YaHei UI", 9, "bold")


class ToolRow:
    """工具菜单里的一行：状态徽标 + 中文名 + 工具名 + 风险 + 功能解释 + 激活按钮。"""

    def __init__(self, parent: Any, meta: ToolMeta, on_toggle: Any) -> None:
        self.meta = meta
        self._on_toggle = on_toggle
        self._bg = _ROW_BG_IDLE
        self.frame = tk.Frame(
            parent,
            bg=self._bg,
            bd=0,
            highlightthickness=1,
            highlightbackground=_ROW_BORDER_IDLE,
            highlightcolor=_ROW_BORDER_IDLE,
        )
        self.frame.pack(fill=tk.X, padx=(2, 6), pady=(0, 4))

        head = tk.Frame(self.frame, bg=self._bg)
        head.pack(fill=tk.X, padx=10, pady=(5, 0))
        self.badge = tk.Label(
            head,
            text=_BADGES["idle"][0],
            width=8,
            font=_BADGE_FONT,
            anchor="center",
            padx=2,
            pady=1,
        )
        self.badge.pack(side=tk.LEFT)
        self.title = tk.Label(
            head, text=meta.title, font=_TITLE_FONT, fg="#1f2328", bg=self._bg, anchor="w"
        )
        self.title.pack(side=tk.LEFT, padx=(8, 6))
        self.name = tk.Label(
            head, text=meta.name, font=_NAME_FONT, fg="#8a8f98", bg=self._bg, anchor="w"
        )
        self.name.pack(side=tk.LEFT)
        self.risk = tk.Label(
            head,
            text=RISK_LABELS.get(meta.risk, meta.risk),
            font=_SMALL_FONT,
            fg=RISK_COLORS.get(meta.risk, "#5f6b7a"),
            bg=self._bg,
        )
        self.risk.pack(side=tk.LEFT, padx=(10, 0))

        self.button = tk.Button(
            head,
            text="激活",
            width=9,
            command=self._toggle,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            font=_SMALL_FONT,
            **_BUTTON_OFF,
        )
        self.button.pack(side=tk.RIGHT)

        self.summary = tk.Label(
            self.frame,
            text=meta.summary,
            font=_SUMMARY_FONT,
            fg="#4a4f57",
            bg=self._bg,
            anchor="w",
            justify=tk.LEFT,
        )
        self.summary.pack(fill=tk.X, padx=(10, 10), pady=(0, 6))

        self._painted = (self.frame, head, self.title, self.name, self.risk, self.summary)
        for widget in (self.frame, head, self.title, self.name, self.summary):
            widget.bind("<Button-1>", self._row_clicked)
        for widget in (self.title, self.name, self.summary):
            try:
                widget.configure(cursor="hand2")
            except tk.TclError:  # pragma: no cover - 老 Tk 不支持 cursor
                pass

    # -- 交互 ---------------------------------------------------------------
    def _row_clicked(self, _event: Any = None) -> None:
        """点整行也切换（按钮自身有 command，不重复绑）。"""
        self._toggle()

    def _toggle(self) -> None:
        if str(self.button.cget("state")) == tk.DISABLED:
            return
        self._on_toggle(self.meta.name)

    # -- 状态 ---------------------------------------------------------------
    def _paint(self, bg: str, border: str) -> None:
        """换行底色 / 边框（激活的行浅绿，锁定模式浅灰）。"""
        if bg != self._bg:
            self._bg = bg
            for widget in self._painted:
                try:
                    widget.configure(bg=bg)
                except tk.TclError:  # pragma: no cover - 窗口销毁中
                    return
        self.frame.configure(highlightbackground=border, highlightcolor=border)

    def _set_button(self, style: dict[str, str], text: str, state: str) -> None:
        self.button.configure(
            text=text, state=state, disabledforeground=style["fg"], **style
        )

    def refresh(self, active: bool, mode: str) -> None:
        """按当前模式 + 是否激活刷新徽标、按钮文案与行配色。"""
        if mode == "whitelist":
            key = "active" if active else "idle"
            label, fg, bg = _BADGES[key]
            self.badge.configure(text=label, fg=fg, bg=bg)
            self._paint(
                _ROW_BG_ACTIVE if active else _ROW_BG_IDLE,
                _ROW_BORDER_ACTIVE if active else _ROW_BORDER_IDLE,
            )
            self._set_button(_BUTTON_ON if active else _BUTTON_OFF, "停用" if active else "激活", tk.NORMAL)
            return
        key = "inherit" if mode == "inherit" else "none"
        label, fg, bg = _BADGES[key]
        self.badge.configure(text=label, fg=fg, bg=bg)
        self._paint(_ROW_BG_LOCKED, _ROW_BORDER_IDLE)
        self._set_button(_BUTTON_LOCKED, "—", tk.DISABLED)

    def set_wraplength(self, width: int) -> None:
        """功能解释按菜单宽度换行（窗口拉伸后仍是一行到边）。"""
        self.summary.configure(wraplength=max(240, int(width)))


def category_header(parent: Any, category: str) -> tk.Label:
    """分组标题条；返回右侧"已激活 x / y"计数标签，供页面刷新。"""
    header = tk.Frame(parent, bg=_HEADER_BG)
    header.pack(fill=tk.X, padx=(2, 6), pady=(6, 3))
    tk.Label(
        header,
        text=category,
        font=_SMALL_FONT_BOLD,
        fg="#39424e",
        bg=_HEADER_BG,
    ).pack(side=tk.LEFT, padx=8, pady=3)
    count = tk.Label(header, text="", font=_SMALL_FONT, fg="#7b838d", bg=_HEADER_BG)
    count.pack(side=tk.RIGHT, padx=8)
    return count


class MenuResize:
    """给页面 ``_on_menu_resize`` 兜一个"只有宽度"的事件对象（重建菜单时手动调）。"""

    __slots__ = ("width",)

    def __init__(self, width: int) -> None:
        self.width = width
