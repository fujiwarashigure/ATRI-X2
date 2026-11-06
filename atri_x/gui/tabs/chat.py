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

from atri_x.gui import aero
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
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._on_send = on_send
        self._on_tab = on_tab
        self.frame = ttk.Frame(parent, padding=(10, 8, 10, 8))

        self.frame.rowconfigure(0, weight=1, minsize=80)
        self.frame.rowconfigure(1, weight=0, minsize=112)
        self.frame.columnconfigure(0, weight=1)
        body = ttk.Frame(self.frame)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_propagate(False)
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
            padx=24,
            pady=20,
            width=30,
            height=8,
            spacing1=8,
            spacing3=12,
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

        self._welcome = tk.Canvas(body, background="white", highlightthickness=0)
        self._wallpaper = None
        try:
            self._wallpaper = aero.load_brand_image(self.frame, "aero-wallpaper.png",
                                                    "wallpaper.png")
        except tk.TclError:
            pass
        self._welcome.bind("<Configure>", self._draw_welcome)
        bottom = ttk.Frame(self.frame)
        bottom.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        hint_bar = ttk.Frame(bottom)
        hint_bar.pack(fill=tk.X)
        ttk.Label(hint_bar, text=_INPUT_HINT, foreground="#8a8a8a").pack(side=tk.LEFT)
        if on_clear is not None:
            ttk.Button(hint_bar, text="清屏", width=6, command=on_clear).pack(side=tk.RIGHT)
        if on_stop is not None:
            ttk.Button(hint_bar, text="停止当前请求", command=on_stop).pack(side=tk.RIGHT, padx=6)
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
        self._message_menu = tk.Menu(self.view, tearoff=False)
        self._message_menu.add_command(label="复制这条消息", command=self.copy_message)
        self._message_menu.add_command(label="放入输入框编辑（不自动发送）", command=self.edit_message)
        self._message_menu.add_command(label="复制本条代码块", command=self.copy_code)
        self._message_menu.add_separator()
        self._message_menu.add_command(label="导出当前已加载对话…", command=self.export_transcript)
        self.view.bind("<Button-3>", self._message_popup)
        self.view.tag_configure("heading", font=("Microsoft YaHei UI", 13, "bold"), spacing1=12)
        self.view.tag_configure("code", font=("Consolas", 10), background="#edf2f5", lmargin1=12, lmargin2=12)
        self.view.tag_configure("quote", foreground="#557687", lmargin1=16, lmargin2=16)
        self.view.tag_configure("bold", font=("Microsoft YaHei UI", 10, "bold"))

    def bind_privacy_controls(self, app: Any) -> None:
        """只复用服务端命令；不读取本机数据库、不绕过当前主体权限。"""
        self._app = app
        bar = ttk.Frame(self.sidebar.frame)
        bar.pack(side=tk.BOTTOM, fill=tk.X, pady=6)
        ttk.Button(bar, text="记忆与隐私…", command=self.open_memory_controls).pack(fill=tk.X)
        ttk.Button(bar, text="工具确认…", command=self.open_tool_controls).pack(fill=tk.X, pady=(4, 0))

    def _send_control(self, command: str, *, destructive: bool = False) -> bool:
        from tkinter import messagebox
        app = self._app
        if getattr(self.sidebar, "_pending_switch", None):
            messagebox.showwarning("请稍候", "正在切换会话，操作尚未发送。", parent=self.frame)
            return False
        if destructive and not messagebox.askyesno(
                "确认操作", f"将对当前会话作用域执行：\n{command}\n\n删除不可撤销；请确认范围和 ID。", parent=self.frame):
            return False
        client = app.client
        if client is None or not client.connected or not client.send_input(command):
            app._append_notice("管理请求未发出，请检查连接。草稿未改变。")
            return False
        app._append_notice("管理请求已发送；结果和权限检查请查看聊天区后台回复。")
        return True

    def open_memory_controls(self) -> None:
        from tkinter import messagebox
        window = tk.Toplevel(self.frame)
        window.title("记忆与隐私 · 当前会话作用域")
        window.geometry("580x420")
        window.transient(self.frame.winfo_toplevel())
        body = ttk.Frame(window, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="查看、追溯和删除记忆", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(body, text="结果显示在聊天区；权限由后台校验。管理员查询不等于所有用户都能查看。",
                  wraplength=530).pack(anchor="w", pady=8)
        row = ttk.Frame(body); row.pack(fill=tk.X)
        for label, command in (("我的可管理记忆", "/forget list 100"),
                               ("范围统计（管理员）", "/memory status"),
                               ("学习候选（授权用户）", "/learning review pending")):
            ttk.Button(row, text=label, command=lambda c=command: self._send_control(c)).pack(side=tk.LEFT, padx=2)
        ttk.Label(body, text="记忆 ID / 唯一前缀（从后台列表复制）").pack(anchor="w", pady=(16, 4))
        value = tk.StringVar(master=window)
        ttk.Entry(body, textvariable=value).pack(fill=tk.X)
        def action(prefix: str, delete: bool = False) -> None:
            token = value.get().strip()
            if not token or any(c.isspace() for c in token) or token.startswith("/"):
                messagebox.showwarning("无效 ID", "请输入单个记忆 ID 或唯一前缀。", parent=window)
                return
            self._send_control(f"{prefix} {token}", destructive=delete)
        actions = ttk.Frame(body); actions.pack(fill=tk.X, pady=8)
        ttk.Button(actions, text="查看来源详情（管理员）", command=lambda: action("/memory show")).pack(side=tk.LEFT)
        ttk.Button(actions, text="忘记这条", command=lambda: action("/forget", True)).pack(side=tk.LEFT, padx=8)
        ttk.Separator(body).pack(fill=tk.X, pady=12)
        ttk.Label(body, text="临时聊天：暂不支持严格无痕模式", foreground="#9b5b22").pack(anchor="w")
        ttk.Label(body, text="清屏不删除历史。新建会话也不阻止记忆、学习或日志写入。\n"
                  "删除记忆不会自动清除原始消息、日志或模型服务商的数据。\n"
                  "没有完整的后端隔离与保留策略前，不提供误导性的“无痕”开关。",
                  wraplength=530, justify=tk.LEFT).pack(anchor="w", pady=8)

    def open_tool_controls(self) -> None:
        from tkinter import messagebox
        window = tk.Toplevel(self.frame)
        window.title("工具确认 · 当前会话")
        window.transient(self.frame.winfo_toplevel())
        body = ttk.Frame(window, padding=18); body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="先查看后台详情，再决定是否执行", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, text="这些按钮控制后台当前待确认项，并非绑定某条历史消息。\n"
                  "不会自动执行或批量放行；已发生的外部操作不会因拒绝而回滚。",
                  justify=tk.LEFT).pack(anchor="w", pady=12)
        def confirm() -> None:
            if messagebox.askyesno("允许当前待确认项", "请先核对聊天区最新 /detail 回复。\n确定允许后台当前待确认的工具调用？", parent=window):
                self._send_control("/confirm")
        for label, callback in (("查看当前详情", lambda: self._send_control("/detail")),
                                ("允许当前项…", confirm),
                                ("拒绝当前项", lambda: self._send_control("/reject")),
                                ("停止当前请求", lambda: self._send_control("/stop"))):
            ttk.Button(body, text=label, command=callback).pack(fill=tk.X, pady=3)

    def _message_popup(self, event: Any) -> None:
        position = self.view.index(f"@{event.x},{event.y}")
        self._selected_message = next((i for i in reversed(range(len(getattr(self, "_rendered_rows", []))))
                                       if self.view.compare(position, ">=", f"message_{i}")), None)
        if self._selected_message is None:
            return
        role = self._rendered_rows[self._selected_message][0]
        self._message_menu.entryconfigure(1, state=tk.NORMAL if role == ROLE_USER else tk.DISABLED)
        try:
            self._message_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._message_menu.grab_release()

    def copy_message(self) -> None:
        index = getattr(self, "_selected_message", None)
        rows = getattr(self, "_rendered_rows", [])
        if index is not None and 0 <= index < len(rows):
            self.view.clipboard_clear()
            self.view.clipboard_append(rows[index][2])

    def copy_code(self) -> None:
        import re
        from tkinter import messagebox
        index = getattr(self, "_selected_message", None)
        rows = getattr(self, "_rendered_rows", [])
        if index is None or not 0 <= index < len(rows):
            return
        blocks = re.findall(r"(?m)^\s*```[^\n]*\n(.*?)^\s*```\s*$", rows[index][2], re.DOTALL)
        if not blocks:
            messagebox.showinfo("复制代码", "这条消息没有完整的三反引号代码块。", parent=self.frame)
            return
        self.view.clipboard_clear()
        self.view.clipboard_append("\n\n".join(block.rstrip("\n") for block in blocks))

    def export_transcript(self) -> None:
        from tkinter import filedialog, messagebox
        from pathlib import Path
        path = filedialog.asksaveasfilename(parent=self.frame, title="导出已加载对话（包含原文）",
                                          defaultextension=".md", filetypes=[("Markdown", "*.md"), ("Text", "*.txt")])
        if not path:
            return
        rows = getattr(self, "_rendered_rows", [])
        text = "\n\n".join(f"## {_ROLE_PREFIX.get(role, role)} · {at}\n\n{body}"
                             for role, at, body, _ in rows)
        try:
            Path(path).write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.frame)

    def edit_message(self) -> None:
        from tkinter import messagebox
        index = getattr(self, "_selected_message", None)
        rows = getattr(self, "_rendered_rows", [])
        if index is None or not 0 <= index < len(rows) or rows[index][0] != ROLE_USER:
            return
        if self.get_input().strip() and not messagebox.askyesno(
                "替换草稿", "输入框已有内容，是否替换？原消息不会被修改。", parent=self.frame):
            return
        self.set_input(rows[index][2])

    def _insert_message(self, text: str, role: str) -> None:
        """有限 Markdown 样式；保留原文字符，复制和流式更新不丢内容。"""
        import re
        code = False
        for line in text.splitlines(keepends=True):
            start = self.view.index("end-1c")
            fence = line.lstrip().startswith("```")
            tags = [role]
            if code or fence:
                tags.append("code")
            elif re.match(r"^#{1,6}\s", line):
                tags.append("heading")
            elif line.lstrip().startswith("> "):
                tags.append("quote")
            self.view.insert(tk.END, line, tuple(tags))
            if not code and not fence:
                for match in re.finditer(r"\*\*([^*\n]+)\*\*", line):
                    self.view.tag_add("bold", f"{start}+{match.start()}c", f"{start}+{match.end()}c")
            if fence:
                code = not code

    def _draw_welcome(self, _event: Any = None) -> None:
        canvas = self._welcome
        w, h = canvas.winfo_width(), canvas.winfo_height()
        canvas.delete("all")
        if self._wallpaper is not None:
            canvas.create_image(w, 0, image=self._wallpaper, anchor="ne")
        canvas.create_text(24, 24, anchor="nw", text="你好，我是 ATRI。", fill="#234957",
                           font=("Microsoft YaHei UI", 18, "bold"))
        canvas.create_text(24, 65, anchor="nw", text="今天，一起创造些什么？",
                           fill="#517e8c", font=("Microsoft YaHei UI", 10))

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

    def stop_request(self, client: Any) -> bool:
        """停止当前会话请求，不修改草稿，也不停止后台服务。"""
        from tkinter import messagebox
        sent = bool(client is not None and client.connected and client.send_input("/stop"))
        if not sent:
            messagebox.showwarning("停止请求", "请求未发出，请检查连接。", parent=self.frame)
        return sent

    # -- 渲染 -----------------------------------------------------------------
    def render(self, transcript: Transcript) -> None:
        """把聊天记录整体重画一遍（保持在底部时自动跟随）。"""
        view = self.view
        at_bottom = view.yview()[1] >= 0.999
        rows = [(line.role, line.at, line.text, line.streaming)
                for line in transcript.lines if line.text]
        previous = getattr(self, "_rendered_rows", [])
        common = 0
        while common < min(len(rows), len(previous)) and rows[common] == previous[common]:
            common += 1
        if rows == previous and rows:
            return
        view.configure(state=tk.NORMAL)
        if common == 0:
            view.delete("1.0", tk.END)
        elif common < len(previous):
            view.delete(f"message_{common}", "end-1c")
        self._rendered_rows = rows
        if not any(line.text for line in transcript.lines):
            self._welcome.place(in_=view, x=0, y=0, relwidth=1, relheight=1)
            tk.Misc.lift(self._welcome)
            self._draw_welcome()
        else:
            self._welcome.place_forget()
        for index in range(common, len(rows)):
            role, at, text, streaming = rows[index]
            view.mark_set(f"message_{index}", "end-1c")
            view.mark_gravity(f"message_{index}", tk.LEFT)
            prefix = _ROLE_PREFIX.get(role, role)
            meta = f"[{at}] " if at else ""
            view.insert(tk.END, f"{meta}{prefix}：", ("meta",))
            self._insert_message(text + ("\n" if not streaming else ""), role)
        view.configure(state=tk.DISABLED)
        if at_bottom:
            view.see(tk.END)
