"""Tk 主窗口：聊天界面 + 状态栏 + 托盘常驻。

分工（见 ``docs/windows-app.md``）：

- :mod:`atri_x.gui.client` 负责 WS；:mod:`atri_x.gui.service` 负责配置与后台服务；
- 本模块只做界面：把 :class:`atri_x.gui.protocol.Transcript` 渲染成聊天记录，
  把输入发出去，把连接/运行状态放在状态栏；
- 托盘（:mod:`atri_x.gui.tray`）在独立线程里跑，回调经 :meth:`GuiApp.post` 转回主线程。

窗口关闭的语义：**关窗口 ≠ 退出程序**。默认隐藏到托盘，后台服务（``atri-x service
run``）不受影响，QQ 机器人继续在线；要真正退出用托盘菜单或「文件」菜单。
"""

from __future__ import annotations

import os
import queue
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from atri_x import __version__
from atri_x.gui.client import (
    STATE_AUTH_FAILED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    RemoteClient,
)
from atri_x.gui.process import load_window_state, save_window_state
from atri_x.gui.protocol import (
    ROLE_ASSISTANT,
    ROLE_ERROR,
    ROLE_NOTICE,
    ROLE_REASONING,
    ROLE_USER,
    Transcript,
    apply_completion,
    apply_frame,
    decode_history,
    decode_sessions,
    describe_status,
    status_from_frame,
)
from atri_x.gui.service import GuiSettings, app_dir, open_path, service_log_path
from atri_x.gui.sessions import SessionSidebar, load_history_into
from atri_x.gui.tray import TrayIcon
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_HISTORY_RESULT,
    REMOTE_MSG_SESSIONS_RESULT,
)

__all__ = ["GuiApp", "WINDOW_TITLE"]

#: 窗口标题（单实例置前也用它找窗口）。
WINDOW_TITLE = "ATRI-X"

#: 轮询回调间隔（毫秒）。
POLL_INTERVAL_MS = 80

#: 输入框回车/发送按钮的行为。
_PLACEHOLDER = "输入消息，Enter 发送，Shift+Enter 换行，Tab 补全"

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


class GuiApp:
    """主窗口。构造后调用 :meth:`run` 进入事件循环。"""

    def __init__(
        self,
        root: tk.Tk,
        settings: GuiSettings | None = None,
        *,
        client: RemoteClient | None = None,
        tray_enabled: bool = True,
        minimized: bool = False,
        service_owned: bool = False,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.settings = settings
        self.client = client
        self.service_owned = service_owned
        self._on_quit = on_quit
        self.transcript = Transcript()
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._tray: TrayIcon | None = None
        self._tray_enabled = tray_enabled
        self._minimized = minimized
        self._dirty = True
        self._status_text = "未连接"
        self._runtime_text = ""
        self._hide_hint_shown = False
        self._pending_complete = ""
        self._completion_items: list[Any] = []
        self._window_state = load_window_state()
        self._active_session_id = ""

        root.title(WINDOW_TITLE)
        root.minsize(640, 420)
        root.geometry(self._window_state.get("geometry") or "920x640")
        root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        self._build_ui()
        self._build_menu()
        self._bind_keys()
        if client is not None:
            client.set_callbacks(
                on_message=lambda message: self.post("message", message),
                on_state=lambda state, detail: self.post("state", (state, detail)),
            )
        self._start_tray()
        if self._minimized:
            root.withdraw()

    # ---------------------------------------------------------------- 构建界面

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:  # pragma: no cover - 老系统没有 vista 主题
            pass

        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill=tk.X)
        self.status_label = ttk.Label(top, text="● 未连接", font=("Microsoft YaHei UI", 10, "bold"))
        self.status_label.pack(side=tk.LEFT)
        self.runtime_label = ttk.Label(top, text="", foreground="#555555")
        self.runtime_label.pack(side=tk.RIGHT)

        body = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        body.pack(fill=tk.BOTH, expand=True)
        self.sidebar = SessionSidebar(
            body,
            on_open=self.open_session,
            on_refresh=self.refresh_sessions,
            on_new=self.new_session,
        )
        self.sidebar.frame.pack(side=tk.LEFT, fill=tk.Y)
        self.transcript_view = tk.Text(
            body,
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            padx=6,
            pady=6,
            font=("Microsoft YaHei UI", 10),
            background="#ffffff",
            cursor="arrow",
        )
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.transcript_view.yview)
        self.transcript_view.configure(yscrollcommand=scrollbar.set)
        self.transcript_view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for role, style_options in _TAG_STYLES.items():
            self.transcript_view.tag_configure(role, **style_options)
        self.transcript_view.tag_configure("meta", foreground="#9a9a9a", font=("Microsoft YaHei UI", 8))
        self.transcript_view.configure(state=tk.DISABLED)

        bottom = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        bottom.pack(fill=tk.X)
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
        self.input_box.insert("1.0", "")
        self.send_button = ttk.Button(bottom, text="发送", command=self.send_current_input)
        self.send_button.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="新建会话", accelerator="Ctrl+N", command=self.new_session)
        file_menu.add_command(label="刷新会话列表", accelerator="F5", command=self.refresh_sessions)
        file_menu.add_command(label="复制全部记录", command=self.copy_all)
        file_menu.add_separator()
        file_menu.add_command(label="退出（后台继续运行）", command=lambda: self.quit_app(stop_service=False))
        file_menu.add_command(
            label="退出并停止后台服务", command=lambda: self.quit_app(stop_service=True)
        )
        menubar.add_cascade(label="文件", menu=file_menu)

        service_menu = tk.Menu(menubar, tearoff=False)
        service_menu.add_command(label="重新连接", command=self.reconnect)
        service_menu.add_command(label="启动/确认后台服务", command=self.start_service_clicked)
        service_menu.add_command(label="停止后台服务（强制）", command=self.stop_service_clicked)
        service_menu.add_separator()
        service_menu.add_command(label="打开配置目录", command=self.open_config_dir)
        service_menu.add_command(label="打开后台日志", command=self.open_service_log)
        menubar.add_cascade(label="服务", menu=service_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="关于 ATRI-X", command=self.show_about)
        help_menu.add_command(label="托盘/后台说明", command=self.show_tray_help)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.configure(menu=menubar)

    def _bind_keys(self) -> None:
        self.root.bind("<Control-n>", lambda _event: self.new_session())
        self.root.bind("<F5>", lambda _event: self.refresh_sessions())
        self.input_box.bind("<Return>", self._on_return)
        self.input_box.bind("<Shift-Return>", lambda _event: None)
        self.input_box.bind("<Tab>", self._on_tab)
        self.input_box.bind("<Control-l>", lambda _event: self.transcript_view.yview_moveto(0.0))

    # ---------------------------------------------------------------- 主循环

    def run(self) -> None:
        """进入 Tk 事件循环（阻塞到窗口关闭/退出）。"""
        self._poll()
        if self.client is not None:
            self.client.start()
        self.root.mainloop()

    def post(self, kind: str, payload: Any) -> None:
        """把后台线程的事件塞进主线程队列（线程安全）。"""
        self._events.put((kind, payload))

    def _poll(self) -> None:
        """主线程定时器：处理跨线程事件并重绘。"""
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        if self._dirty:
            self._render_transcript()
            self._dirty = False
        self._update_status()
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def render_now(self) -> None:
        """立即重绘一次（自检/测试用；正常流程由 :meth:`_poll` 驱动）。"""
        self._render_transcript()
        self._dirty = False
        self._update_status()

    def _handle_event(self, kind: str, payload: Any) -> None:
        if kind == "message":
            self._handle_message(payload)
        elif kind == "state":
            state, detail = payload
            self._handle_state(state, detail)
        elif kind == "tray":
            self._handle_tray(str(payload))

    def _handle_message(self, message: Any) -> None:
        kind = str(getattr(message, "type", "") or "")
        if kind == REMOTE_MSG_SESSIONS_RESULT:
            self.sidebar.set_sessions(decode_sessions(message))
            if self._active_session_id:
                self.sidebar.set_active(self._active_session_id)
            return
        if kind == REMOTE_MSG_HISTORY_RESULT:
            items = decode_history(message)
            if items or str(getattr(message, "session_id", "")):
                self._active_session_id = str(getattr(message, "session_id", "") or "")
                count = load_history_into(self.transcript, items)
                self._dirty = True
                self.sidebar.set_active(self._active_session_id)
                if not count:
                    self._append_notice("该会话还没有可显示的历史消息。")
            return
        if kind == "complete_result":
            self._completion_items = list(getattr(message, "items", []) or [])
            return
        snapshot = status_from_frame(message)
        if snapshot is not None:
            self._runtime_text = describe_status(snapshot)
            return
        if apply_frame(self.transcript, message):
            self._dirty = True

    def _handle_state(self, state: str, detail: str) -> None:
        labels = {
            STATE_CONNECTING: "● 连接中…",
            STATE_CONNECTED: "● 已连接",
            STATE_DISCONNECTED: "● 已断开",
            STATE_AUTH_FAILED: "● 鉴权失败",
        }
        label = labels.get(state, "● 未连接")
        if detail and state != STATE_CONNECTED:
            label += f"（{detail}）"
        self._status_text = label
        if state == STATE_CONNECTED:
            self._runtime_text = self._runtime_text or ""
            # 连上后立刻拉一次会话列表（服务端可能已有历史会话）。
            self.refresh_sessions()
        if state in (STATE_DISCONNECTED, STATE_AUTH_FAILED):
            self.sidebar.clear()
        if state == STATE_AUTH_FAILED:
            self._append_notice("鉴权失败：请检查 app.toml 的 [platform.cli.server.tokens] 与 .env 里的 token")
        if self._tray is not None:
            self._tray.set_tooltip(f"ATRI-X · {label.lstrip('● ')}")

    # ---------------------------------------------------------------- 渲染

    def _render_transcript(self) -> None:
        view = self.transcript_view
        at_bottom = view.yview()[1] >= 0.999
        view.configure(state=tk.NORMAL)
        view.delete("1.0", tk.END)
        for line in self.transcript.lines:
            if not line.text:
                continue
            prefix = _ROLE_PREFIX.get(line.role, line.role)
            meta = f"[{line.at}] " if line.at else ""
            view.insert(tk.END, f"{meta}{prefix}：", ("meta",))
            view.insert(tk.END, line.text + ("\n" if not line.streaming else ""), (line.role,))
        view.configure(state=tk.DISABLED)
        if at_bottom:
            view.see(tk.END)

    def _update_status(self) -> None:
        if self.status_label.cget("text") != self._status_text:
            self.status_label.configure(text=self._status_text)
        runtime = self._runtime_text or ""
        if self.runtime_label.cget("text") != runtime:
            self.runtime_label.configure(text=runtime)

    def _append_notice(self, text: str) -> None:
        self.transcript.add_notice(text)
        self._dirty = True

    # ---------------------------------------------------------------- 交互

    def _on_return(self, _event: tk.Event[Any]) -> str:
        self.send_current_input()
        return "break"

    def _on_tab(self, _event: tk.Event[Any]) -> str:
        """Tab：请求补全；拿到候选后把第一个应用到输入框（连续 Tab 换下一个）。"""
        text = self.input_box.get("1.0", "end-1c")
        if not text.strip():
            return "break"
        if self._completion_items and self._pending_complete:
            item = self._completion_items.pop(0)
            self._set_input(apply_completion(text, item, len(text)))
            return "break"
        if self.client is None or not self.client.connected:
            return "break"
        self._pending_complete = "gui-tab"
        self._completion_items = []
        self.client.request_complete(self._pending_complete, text, len(text))
        return "break"

    def send_current_input(self) -> None:
        text = self.input_box.get("1.0", "end-1c").strip()
        if not text:
            return
        if not text.startswith("/") and (self.client is None or not self.client.connected):
            self._append_notice("还没连上后台服务，消息没有发出去。可以用「服务 → 启动/确认后台服务」。")
            return
        self._set_input("")
        self.transcript.add_user(text)
        self._dirty = True
        if self.client is not None:
            self.client.send_input(text)

    def _set_input(self, text: str) -> None:
        self.input_box.delete("1.0", tk.END)
        self.input_box.insert("1.0", text)
        self.input_box.focus_set()

    def new_session(self) -> None:
        """新建会话：通知服务端 ``/new``，清空本地记录并刷新列表。"""
        self.transcript.clear()
        self._runtime_text = ""
        self._active_session_id = ""
        self._dirty = True
        if self.client is not None and self.client.connected:
            self.client.send_input("/new")
        self.refresh_sessions()

    def refresh_sessions(self) -> None:
        """请求会话列表（未连接时忽略；连上时会自动调用一次）。"""
        if self.client is None or not self.client.connected:
            return
        self.client.request_sessions(30)

    def open_session(self, session_id: str) -> None:
        """切到某个历史会话：发 ``/resume <id>`` 并拉取该会话历史。"""
        session_id = (session_id or "").strip()
        if not session_id:
            return
        if self.client is None or not self.client.connected:
            self._append_notice("还没连上后台服务，无法切换会话。")
            return
        self._active_session_id = session_id
        self.client.send_input(f"/resume {session_id}")
        self.client.request_history(session_id, 200)
        self.sidebar.set_active(session_id)

    def copy_all(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.transcript.text())

    def reconnect(self) -> None:
        if self.client is None:
            return
        self.client.stop()
        self.client.start()

    # ---------------------------------------------------------------- 服务菜单

    def start_service_clicked(self) -> None:
        """确保后台服务在跑；成功后就重连。"""
        if self.settings is None:
            messagebox.showinfo("ATRI-X", "当前没有后台服务的配置信息。", parent=self.root)
            return
        from atri_x.gui.service import prepare_settings, spawn_service, wait_until_ready

        try:
            settings = prepare_settings(self.settings.config_path)
        except Exception as exc:  # noqa: BLE001 - 直接展示给用户
            messagebox.showerror("ATRI-X", f"准备配置失败：{exc}", parent=self.root)
            return
        ok, reason = wait_until_ready(settings, timeout=1.0)
        if not ok:
            try:
                spawn_service(settings)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("ATRI-X", f"启动后台服务失败：{exc}", parent=self.root)
                return
            self.service_owned = True
            ok, reason = wait_until_ready(settings, timeout=25.0)
        if not ok:
            messagebox.showerror(
                "ATRI-X",
                f"后台服务没有就绪：{reason}\n\n日志：{service_log_path()}",
                parent=self.root,
            )
            return
        self.settings = settings
        self.reconnect()

    def stop_service_clicked(self) -> None:
        if not messagebox.askyesno(
            "ATRI-X",
            "强制结束后台服务？\n\n这会中断正在进行的任务，QQ 机器人也会掉线。",
            parent=self.root,
        ):
            return
        from atri_x.gui.service import stop_service

        ok, detail = stop_service()
        (messagebox.showinfo if ok else messagebox.showerror)("ATRI-X", detail, parent=self.root)

    def open_config_dir(self) -> None:
        directory = self.settings.config_dir if self.settings is not None else app_dir()
        if not open_path(directory):
            messagebox.showinfo("ATRI-X", f"打不开目录：{directory}", parent=self.root)

    def open_service_log(self) -> None:
        path = service_log_path()
        if not os.path.exists(path):
            messagebox.showinfo("ATRI-X", f"还没有日志：{path}", parent=self.root)
            return
        if not open_path(path):
            messagebox.showinfo("ATRI-X", f"日志文件：{path}", parent=self.root)

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于 ATRI-X",
            f"ATRI-X {__version__}\n\n"
            "桌面版：Tk 前端 + 独立后台服务（service run）。\n"
            "关闭窗口后服务继续运行，QQ 机器人保持在线。",
            parent=self.root,
        )

    def show_tray_help(self) -> None:
        messagebox.showinfo(
            "托盘与后台",
            "关窗口只会隐藏到托盘，后台服务不受影响。\n"
            "要彻底退出请用托盘菜单或「文件」菜单。\n\n"
            "停掉后台服务（强制）会中断任务；正常运维建议在控制台里 Ctrl+C。",
            parent=self.root,
        )

    # ---------------------------------------------------------------- 托盘 / 生命周期

    def _start_tray(self) -> None:
        if not self._tray_enabled:
            return
        tray = TrayIcon(
            "ATRI-X · 后台运行中",
            [
                ("显示主窗口", lambda: self.post("tray", "show")),
                ("新建会话", lambda: self.post("tray", "new")),
                None,
                ("重新连接", lambda: self.post("tray", "reconnect")),
                ("打开日志", lambda: self.post("tray", "log")),                None,
                ("退出（后台继续）", lambda: self.post("tray", "quit")),
                ("退出并停止服务", lambda: self.post("tray", "quit-stop")),
            ],
            on_activate=lambda: self.post("tray", "show"),
        )
        if tray.start():
            self._tray = tray
        else:
            self._tray = None

    def _handle_tray(self, action: str) -> None:  # pragma: no cover - 由托盘线程触发
        if action == "show":
            self.show_window()
        elif action == "new":
            self.new_session()
            self.show_window()
        elif action == "reconnect":
            self.reconnect()
        elif action == "log":
            self.open_service_log()
        elif action == "quit":
            self.quit_app(stop_service=False)
        elif action == "quit-stop":
            if messagebox.askyesno("ATRI-X", "退出并强制停止后台服务？", parent=self.root):
                self.quit_app(stop_service=True)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:  # pragma: no cover
            pass

    def on_close_request(self) -> None:
        """点窗口 X：有托盘就隐藏，没有托盘就当作退出 GUI（后台服务照旧）。"""
        if self._tray is not None:
            self.root.withdraw()
            if not self._hide_hint_shown:
                self._hide_hint_shown = True
                self._tray.notify("ATRI-X 仍在后台运行", "窗口已隐藏到托盘，QQ 机器人保持在线。")
            return
        if not self._hide_hint_shown:
            self._hide_hint_shown = True
            messagebox.showinfo(
                "ATRI-X",
                "关闭窗口不会停止后台服务，机器人继续在线。\n"
                "要停止服务请用「服务 → 停止后台服务」。",
                parent=self.root,
            )
        self.quit_app(stop_service=False)

    def quit_app(self, *, stop_service: bool) -> None:
        """退出 GUI（可选顺带强制停服务）。"""
        save_window_state(self.root.geometry())
        if stop_service:
            from atri_x.gui.service import stop_service as force_stop

            ok, detail = force_stop()
            if not ok:
                messagebox.showwarning("ATRI-X", detail, parent=self.root)
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self.client is not None:
            self.client.stop()
        callback = self._on_quit
        if callback is not None:
            callback()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已经销毁
            pass
