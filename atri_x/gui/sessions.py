"""会话侧边栏：会话列表 + 新建/刷新（GUI 的"会话列表/历史"入口）。

分工：

- 列表数据来自服务端的 ``sessions_result`` 帧（见 :mod:`atri_x.gui.protocol`）；
- 打开某个会话时，界面发 ``/resume <id>`` 命令（复用服务端既有命令链路：
  权限、审计、命名、上下文切换全部走 ATRI-X 自己的实现），再请求该会话的
  ``history`` 帧把记录显示出来；
- 本模块只做 Tk 控件与回调，不碰网络：``on_open``/``on_refresh``/``on_new``
  由 :class:`atri_x.gui.app.GuiApp` 提供。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x.gui.protocol import ROLE_ASSISTANT, HistoryItem, SessionItem, Transcript, format_clock

__all__ = ["SessionSidebar", "load_history_into"]


def load_history_into(transcript: Transcript, items: list[HistoryItem]) -> int:
    """把历史消息灌进聊天记录（先清空），返回条数。

    ``at`` 是服务端给的 RFC3339 时间，这里转成 ``MM-DD HH:MM`` 前缀；历史里的
    助手消息不参与流式，直接作为完整消息插入。
    """
    transcript.clear()
    count = 0
    for item in items or []:
        text = (item.text or "").strip()
        if not text:
            continue
        moment = format_clock(item.at)
        if item.role == ROLE_ASSISTANT:
            transcript.add_chat(text, at=moment)
        else:
            transcript.add_user(text, at=moment)
        count += 1
    return count



class SessionSidebar:
    """左侧会话列表（Listbox + 新建/刷新按钮）。"""

    def __init__(
        self,
        parent: Any,
        *,
        on_open: Callable[[str], None],
        on_refresh: Callable[[], None],
        on_new: Callable[[], None] | None = None,
        width: int = 230,
        visible_rows: int = 16,
    ) -> None:
        self._on_open = on_open
        self._on_refresh = on_refresh
        self._on_new = on_new
        self._items: list[SessionItem] = []

        self.frame = ttk.Frame(parent, padding=(0, 0, 8, 0))
        header = ttk.Frame(self.frame)
        header.pack(fill=tk.X)
        ttk.Label(header, text="会话", font=("Microsoft YaHei UI", 10, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Button(header, text="新建", width=5, command=self._new_clicked).pack(
            side=tk.RIGHT
        )
        ttk.Button(header, text="刷新", width=5, command=self._refresh_clicked).pack(
            side=tk.RIGHT, padx=(0, 4)
        )

        self.search_var = tk.StringVar(master=self.frame)
        search = ttk.Entry(self.frame, textvariable=self.search_var, width=24)
        search.pack(fill=tk.X, pady=(6, 0))
        search.bind("<Return>", lambda _e: self._refresh_clicked())
        ttk.Label(self.frame, text="搜索会话 · 输入后按 Enter", foreground="#718797").pack(anchor="w")
        body = ttk.Frame(self.frame)
        body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL)
        self.listbox = tk.Listbox(
            body,
            width=width // 8,
            height=visible_rows,
            exportselection=False,
            activestyle="none",
            font=("Microsoft YaHei UI", 9),
            yscrollcommand=scrollbar.set,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar.configure(command=self.listbox.yview)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-Button-1>", self._open_clicked)
        self.listbox.bind("<Return>", self._open_clicked)

    def bind_management(self, app: Any) -> None:
        """复用现有命令权限链；从明确选中的会话发起管理操作。"""
        from tkinter import messagebox, simpledialog
        menu = tk.Menu(self.frame, tearoff=False)
        def manage(action: str) -> None:
            session_id = self.selected_id()
            if not session_id:
                return
            suffix = ""
            if action == "rename":
                title = simpledialog.askstring("重命名会话", "新名称：", parent=self.frame)
                if not title or not title.strip() or "\n" in title or "\r" in title:
                    return
                suffix = " " + title.strip()
            elif action in ("archive", "delete"):
                warning = "永久删除会话及其关联记忆，无法撤销。" if action == "delete" else "归档选中的会话。"
                if not messagebox.askyesno("确认会话操作", warning, parent=self.frame):
                    return
                suffix = " --confirm"
            if app.client is None or not app.client.connected or not app.client.send_input(
                    f"/{action} {session_id}{suffix}"):
                app._append_notice("会话操作未发出，请检查连接。")
                return
            app._append_notice("会话操作已发送；以后台回复为准，完成后可刷新列表。")
            self.frame.after(500, app.refresh_sessions)
        for label, action in (("重命名", "rename"), ("置顶", "pin"), ("取消置顶", "unpin"),
                              ("归档", "archive"), ("恢复归档", "unarchive"), ("永久删除…", "delete")):
            menu.add_command(label=label, command=lambda a=action: manage(a))
        def popup(event: Any) -> None:
            index = self.listbox.nearest(event.y)
            bounds = self.listbox.bbox(index)
            if not bounds or not bounds[1] <= event.y < bounds[1] + bounds[3]:
                return
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(index)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        self.listbox.bind("<Button-3>", popup)

    # -- 数据 ---------------------------------------------------------------
    def set_sessions(self, items: list[SessionItem]) -> None:
        """整体替换列表；保留用户当前选中的会话。"""
        selected = self.selected_id()
        self._items = list(items or [])
        self.listbox.delete(0, tk.END)
        for item in self._items:
            self.listbox.insert(tk.END, item.label_text)
        if selected:
            self.select(selected)
        elif self._items:
            self.listbox.selection_clear(0, tk.END)

    def set_active(self, session_id: str) -> None:
        """把某个会话标成"当前"并选中它（列表里没有时只清空选择）。"""
        self.select(session_id)

    def select(self, session_id: str) -> None:
        """按 id 选中列表行；找不到就清空选择。"""
        for index, item in enumerate(self._items):
            if item.id == session_id:
                self.listbox.selection_clear(0, tk.END)
                self.listbox.selection_set(index)
                self.listbox.see(index)
                return
        self.listbox.selection_clear(0, tk.END)

    def selected_id(self) -> str:
        """当前选中的会话 id；没有选中返回空串。"""
        selection = self.listbox.curselection()
        if not selection:
            return ""
        index = int(selection[0])
        if 0 <= index < len(self._items):
            return self._items[index].id
        return ""

    def count(self) -> int:
        """当前列表条数。"""
        return len(self._items)

    def clear(self) -> None:
        """清空列表（断开连接/服务不可用时）。"""
        self._items = []
        self.listbox.delete(0, tk.END)

    def begin_switch(self, app: Any, target: str) -> None:
        """等服务端 current 标记确认后再加载历史，不把浏览历史误当切换成功。"""
        import time
        self._pending_switch = (target, app._active_session_id, time.monotonic() + 10)
        pending = self._pending_switch
        app._runtime_text = "正在等待后台确认会话切换…"
        def poll() -> None:
            if getattr(self, "_pending_switch", None) is not pending:
                return
            if time.monotonic() >= self._pending_switch[2]:
                self._pending_switch = None
                app._append_notice("会话切换未确认：保留原记录。请刷新会话列表后重试。")
                return
            app.refresh_sessions()
            self.frame.after(300, poll)
        self.frame.after(300, poll)

    def confirm_switch(self, app: Any, items: list[SessionItem]) -> None:
        pending = getattr(self, "_pending_switch", None)
        if not pending:
            return
        target, previous, _ = pending
        current = next((item.id for item in items if item.current), "")
        if not current or (target and current != target) or (not target and current == previous):
            return
        if getattr(self, "_history_requested", None) is pending:
            return
        self._history_requested = pending
        app._active_session_id = current
        app._runtime_text = "会话切换已确认，正在加载历史"
        if not app.client.request_history(current, 200):
            app._append_notice("会话已切换，但历史加载失败；请重新打开会话。")

    # -- 回调 ---------------------------------------------------------------
    def _refresh_clicked(self) -> None:
        self._on_refresh()

    def _new_clicked(self) -> None:
        if self._on_new is not None:
            self._on_new()

    def _open_clicked(self, _event: Any = None) -> str:
        session_id = self.selected_id()
        if session_id:
            self._on_open(session_id)
        return "break"
