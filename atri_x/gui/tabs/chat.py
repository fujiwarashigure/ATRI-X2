"""「对话」选项卡：会话列表 + 聊天记录 + 输入框。

从 :mod:`atri_x.gui.app` 拆出来的纯界面部分（原来主窗口就是这个 + 状态栏/菜单）：

- 左侧 :class:`~atri_x.gui.sessions.SessionSidebar`：历史会话，双击切换；
- 中间 :class:`tk.Text`（只读）：渲染 :class:`~atri_x.gui.protocol.Transcript`；
- 底部输入框：``Enter`` 发送、``Shift+Enter`` 换行、``Tab`` 补全。

网络与业务不在这里：发送、切会话、补全都通过构造时传入的回调交给
:class:`~atri_x.gui.app.GuiApp`（它持有 WS 客户端）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x.gui.protocol import (
    ROLE_ASSISTANT,
    ROLE_ERROR,
    ROLE_NOTICE,
    ROLE_REASONING,
    ROLE_USER,
    Transcript,
)
from atri_x.gui.sessions import SessionSidebar

__all__ = ["ChatTab"]

#: 各角色的文本样式（Tk tag）。
_TAG_STYLES: dict[str, dict[str, Any]] = {
    ROLE_USER: {"foreground": "#1a4f9c", "font": ("Microsoft YaHei UI", 10, "bold")},
    ROLE_ASSISTANT: {"foreground": "#1f1f1f", "font": ("Microsoft YaHei UI", 10)},
    ROLE_REASONING: {"foreground": "#8a8a8a", "font": ("Microsoft YaHei UI", 9, "italic")},
    ROLE_NOTICE: {"foreground": "#b26a00", "font": ("Microsoft YaHei UI", 9)},
    ROLE_ERROR: {"foreground": "#b3261e", "font": ("Microsoft YaHei UI", 9, "bold")},
}

#: 角色前缀。
_ROLE_PREFIX = {
    ROLE_USER: "你",
    ROLE_ASSISTANT: "ATRI",
    ROLE_REASONING: "思考",
    ROLE_NOTICE: "通知",
    ROLE_ERROR: "错误",
}

#: 输入框为空时的提示（画在输入框上方，不占用输入框内容，避免被当成消息发出去）。
_INPUT_HINT = "Enter 发送 · Shift+Enter 换行 · Tab 补全命令"


class ChatTab:
    """对话页。构造后由 :class:`GuiApp` 驱动 ``render`` / ``set_input``。"""

    def __init__(
        self,
        parent: Any,
        *,
        on_open: Callable[[str], None],
        on_refresh: Callable[[], None],
        on_new: Callable[[], None],
        on_send: Callable[[], None],
        on_tab: Callable[[], None],
        on_clear: Callable[[], None] | None = None,
    ) -> None:
        self._on_send = on_send
        self._on_tab = on_tab
        self.frame = ttk.Frame(parent, padding=(10, 8, 10, 8))

        body = ttk.Frame(self.frame)
        body.pack(fill=tk.BOTH, expand=True)
        self.sidebar = SessionSidebar(
            body, on_open=on_open, on_refresh=on_refresh, on_new=on_new
        )
        self.sidebar.frame.pack(side=tk.LEFT, fill=tk.Y)
        self.view = tk.Text(
            body,
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            padx=8,
            pady=6,
            font=("Microsoft YaHei UI", 10),
            background="#ffffff",
            cursor="arrow",
        )
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.view.yview)
        self.view.configure(yscrollcommand=scrollbar.set)
        self.view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for role, style_options in _TAG_STYLES.items():
            self.view.tag_configure(role, **style_options)
        self.view.tag_configure("meta", foreground="#9a9a9a", font=("Microsoft YaHei UI", 8))
        self.view.configure(state=tk.DISABLED)

        bottom = ttk.Frame(self.frame)
        bottom.pack(fill=tk.X, pady=(6, 0))
        hint_bar = ttk.Frame(bottom)
        hint_bar.pack(fill=tk.X)
        ttk.Label(hint_bar, text=_INPUT_HINT, foreground="#8a8a8a").pack(side=tk.LEFT)
        if on_clear is not None:
            ttk.Button(hint_bar, text="清屏", width=6, command=on_clear).pack(side=tk.RIGHT)
        self.send_button = ttk.Button(bottom, text="发送", command=self._on_send)
        self.send_button.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        self.input_box = tk.Text(
            bottom,
            height=3,
            wrap=tk.WORD,
            font=("Microsoft YaHei UI", 10),
            relief=tk.SOLID,
            borderwidth=1,
            padx=6,
            pady=4,
        )
        self.input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.input_box.bind("<Return>", self._return_pressed)
        self.input_box.bind("<Shift-Return>", lambda _event: None)
        self.input_box.bind("<Tab>", self._tab_pressed)
        self.input_box.bind("<Control-l>", lambda _event: self.view.yview_moveto(0.0))

    # -- 事件 -----------------------------------------------------------------
    def _return_pressed(self, _event: Any) -> str:
        self._on_send()
        return "break"

    def _tab_pressed(self, _event: Any) -> str:
        self._on_tab()
        return "break"

    # -- 输入框 ---------------------------------------------------------------
    def get_input(self) -> str:
        """输入框全文（不含 Tk 的额外换行）。"""
        return self.input_box.get("1.0", "end-1c")

    def set_input(self, text: str) -> None:
        self.input_box.delete("1.0", tk.END)
        self.input_box.insert("1.0", text)
        self.input_box.focus_set()

    def focus_input(self) -> None:
        try:
            self.input_box.focus_set()
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass

    # -- 渲染 -----------------------------------------------------------------
    def render(self, transcript: Transcript) -> None:
        """把聊天记录整体重画一遍（保持在底部时自动跟随）。"""
        view = self.view
        at_bottom = view.yview()[1] >= 0.999
        view.configure(state=tk.NORMAL)
        view.delete("1.0", tk.END)
        for line in transcript.lines:
            if not line.text:
                continue
            prefix = _ROLE_PREFIX.get(line.role, line.role)
            meta = f"[{line.at}] " if line.at else ""
            view.insert(tk.END, f"{meta}{prefix}：", ("meta",))
            view.insert(tk.END, line.text + ("\n" if not line.streaming else ""), (line.role,))
        view.configure(state=tk.DISABLED)
        if at_bottom:
            view.see(tk.END)
