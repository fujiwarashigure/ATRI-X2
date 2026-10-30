"""Tk 主窗口：选项卡（对话/模型/人设卡/平台/群友/知识库/进阶/日志/功能）+ 状态栏 + 托盘常驻。

分工（见 ``docs/windows-app.md``）：

- :mod:`atri_x.gui.client` 负责 WS；:mod:`atri_x.gui.service` 负责配置与后台服务；
- :mod:`atri_x.gui.tabs` 里每个选项卡只管自己那一类配置（写哪份文件见包文档）；
- 本模块只做窗口骨架：顶部状态栏、选项卡容器、菜单、托盘联动、跨线程事件队列。

窗口关闭的语义：**关窗口 ≠ 退出程序**。默认隐藏到托盘，后台服务（``atri-x service
run``）不受影响，QQ 机器人继续在线；要真正退出用托盘菜单或「文件」菜单。

配置保存的线程模型：配置读写都在 Tk 主线程里做（量小、用户点一次保存一次）；
唯一跑在后台线程的是「重启后台服务」，结果经 :meth:`GuiApp.post` 回到主线程。
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from atri_x.gui import actions, journal
from atri_x.gui.client import (
    STATE_AUTH_FAILED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    RemoteClient,
)
from atri_x.gui.configedit import ConfigEditError, default_config_path, load_paths
from atri_x.gui.icon import app_icon, app_icon_png
from atri_x.gui.process import load_window_state, save_window_state
from atri_x.gui.protocol import (
    Transcript,
    apply_completion,
    apply_frame,
    decode_history,
    decode_sessions,
    describe_status,
    status_from_frame,
)
from atri_x.gui.service import GuiSettings
from atri_x.gui.sessions import load_history_into
from atri_x.gui.tabs import (
    AdvancedTab,
    CharacterTab,
    ChatTab,
    FeaturesTab,
    KnowledgeTab,
    LinkTab,
    LogsTab,
    MembersTab,
    ModelTab,
    PermissionTab,
    PlatformTab,
    PromptLibTab,
    TimeTab,
    ToolPermTab,
    ResearchTab,  # [atri-research]
)
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

#: 选项卡顺序（索引 → 名字）；菜单和 Ctrl+N 快捷键共用。
_TABS = (
    "对话",
    "模型",
    "人设卡",
    "平台",
    "在线",
    "群友",
    "知识库",
    "进阶",
    "时间",
    "日志",
    "功能",
    "工具权限",
    "研究",  # [atri-research]
    "权限",
    "提示词",
)

#: 选项卡名字 → 索引（供「功能」页双击跳转）。
_TAB_INDEX = {name: index for index, name in enumerate(_TABS)}

#: 「功能」页里的跳转键 → 选项卡名。
_TAB_KEYS = {
    "model": "模型",
    "character": "人设卡",
    "platform": "平台",
    "advanced": "进阶",
    "time": "时间",
    "members": "群友",
    "knowledge": "知识库",
    "logs": "日志",
    "toolperm": "工具权限",
    # 「工具权限」页的第三个子选项卡「工具参数（全局）」：内容安全等全局参数在这里。
    "toolperm_params": "工具权限",
    # Python 版新增的子选项卡：工具进度预览（工具权限页）/ 表情图库（人设卡页）。
    "toolperm_preview": "工具权限",
    "character_reactions": "人设卡",
    # 超级管理员名单 / 风险阈值 / 工具轮次 / 成员建卡开关。
    "permission": "权限",
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
        self._tray_error = ""
        self._tray_warned = False
        self._icon_photo: Any = None
        self._minimized = minimized
        self._dirty = True
        self._status_text = "未连接"
        self._runtime_text = ""
        self._hide_hint_shown = False
        self._pending_complete = ""
        self._completion_items: list[Any] = []
        self._window_state = load_window_state()
        self._active_session_id = ""
        self.paths = load_paths(_config_path_for(settings))
        self._tabs: dict[int, Any] = {}
        self._active_tab_index: int | None = None

        root.title(WINDOW_TITLE)
        self._apply_window_icon()
        # 1100 宽是「进阶」页两列布局与「模型 → 搜索服务」页左右并列表单的最小舒适宽度；
        # 高度不足时这两页都可以竖向滚动，所以 700 够用。
        root.minsize(1100, 700)
        root.geometry(self._window_state.get("geometry") or "1280x860")
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

        notebook = ttk.Notebook(self.root, padding=(8, 4, 8, 2))
        notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook = notebook

        # 选项卡**按需构建**：启动只建「对话」与「功能」，其余先放占位 Frame，
        # 首次切到该页时才真正建控件。
        #
        # 原因（实测）：Tk 的每个控件都是一个 Win32 子窗口，拖动/移动窗口的成本随
        # 子窗口数线性上升——本窗口 11 个页全建时是 2434 个子窗口，每次移动约 6ms
        # （裸 Tk 窗口约 1ms），拖起来明显发滞；其中「工具权限」一页就占 1811 个。
        # 按需构建后启动时只剩两百级子窗口，拖动恢复顺滑；代价是首次点开某页多几十毫秒。
        self._tab_placeholders: dict[int, ttk.Frame] = {}
        self._tab_instances: dict[int, Any] = {}
        self._tab_factories: dict[int, Callable[[Any], Any]] = {
            0: lambda parent: ChatTab(
                parent,
                on_open=self.open_session,
                on_refresh=self.refresh_sessions,
                on_new=self.new_session,
                on_send=self.send_current_input,
                on_tab=self.request_completion,
                on_clear=self.clear_transcript,
            ),
            1: lambda parent: ModelTab(parent, self.paths, on_restart=self._restart_backend),
            2: lambda parent: CharacterTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            3: lambda parent: PlatformTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            4: lambda parent: LinkTab(
                parent,
                self.paths,
                settings=self.settings,
                on_restart=self._restart_backend,
                can_control_service=bool(self.service_owned and self.settings is not None),
            ),
            5: lambda parent: MembersTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            6: lambda parent: KnowledgeTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            7: lambda parent: AdvancedTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            8: lambda parent: TimeTab(parent, self.paths, on_restart=self._restart_backend),
            9: lambda parent: LogsTab(parent, self.paths, on_restart=self._restart_backend),
            10: lambda parent: FeaturesTab(
                parent,
                self.paths,
                on_goto=self.show_tab_key,
                on_restart=self._restart_backend,
                on_reconnect=self.reconnect,
                service_status=lambda: self._status_text,
            ),
            11: lambda parent: ToolPermTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            12: lambda parent: ResearchTab(parent),  # [atri-research]
            13: lambda parent: PermissionTab(
                parent, self.paths, on_restart=self._restart_backend
            ),
            14: lambda parent: PromptLibTab(parent, self.paths, on_restart=self._restart_backend),
        }
        #: 启动时就构建的页：对话（默认页）与功能（状态栏轮询要写它）。
        eager = (0, 10)
        for index, name in enumerate(_TABS):
            text = f" {name} "
            factory = self._tab_factories.get(index)
            if factory is None or index not in eager:
                placeholder = ttk.Frame(notebook)
                notebook.add(placeholder, text=text)
                self._tab_placeholders[index] = placeholder
                continue
            tab = factory(notebook)
            notebook.add(tab.frame, text=text)
            self._tab_instances[index] = tab
            self._tabs[index] = tab
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.features_tab.set_status(self._status_text)

    def _ensure_tab(self, index: int) -> Any:
        """按索引返回选项卡；懒加载页首次访问时在占位 Frame 里真正构建它。"""
        tab = self._tab_instances.get(index)
        if tab is not None:
            return tab
        factory = self._tab_factories.get(index)
        placeholder = self._tab_placeholders.get(index)
        if factory is None or placeholder is None:
            raise KeyError(index)
        tab = factory(placeholder)
        tab.frame.pack(fill=tk.BOTH, expand=True)
        self._tab_placeholders.pop(index, None)
        self._tab_instances[index] = tab
        self._tabs[index] = tab
        return tab

    # 选项卡的对外属性（懒加载：第一次访问时才构建对应页）。
    @property
    def chat(self) -> Any:
        return self._ensure_tab(0)

    @property
    def model_tab(self) -> Any:
        return self._ensure_tab(1)

    @property
    def character_tab(self) -> Any:
        return self._ensure_tab(2)

    @property
    def platform_tab(self) -> Any:
        return self._ensure_tab(3)

    @property
    def link_tab(self) -> Any:
        return self._ensure_tab(4)

    @property
    def members_tab(self) -> Any:
        return self._ensure_tab(5)

    @property
    def knowledge_tab(self) -> Any:
        return self._ensure_tab(6)

    @property
    def advanced_tab(self) -> Any:
        return self._ensure_tab(7)

    @property
    def logs_tab(self) -> Any:
        return self._ensure_tab(9)

    @property
    def time_tab(self) -> Any:
        return self._ensure_tab(8)

    @property
    def features_tab(self) -> Any:
        return self._ensure_tab(10)

    @property
    def toolperm_tab(self) -> Any:
        return self._ensure_tab(11)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="新建会话", accelerator="Ctrl+N", command=self.new_session)
        file_menu.add_command(label="刷新会话列表", accelerator="F5", command=self.refresh_sessions)
        file_menu.add_command(label="复制全部记录", command=self.copy_all)
        file_menu.add_command(label="清空聊天区", command=self.clear_transcript)
        file_menu.add_separator()
        file_menu.add_command(label="退出（后台继续运行）", command=lambda: self.quit_app(stop_service=False))
        file_menu.add_command(
            label="退出并停止后台服务", command=lambda: self.quit_app(stop_service=True)
        )
        menubar.add_cascade(label="文件", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for index, name in enumerate(_TABS):
            view_menu.add_command(
                label=name,
                accelerator=f"Ctrl+{index + 1}",
                command=lambda position=index: self.select_tab(position),
            )
        menubar.add_cascade(label="视图", menu=view_menu)

        service_menu = tk.Menu(menubar, tearoff=False)
        service_menu.add_command(label="重新连接", command=self.reconnect)
        service_menu.add_command(
            label="启动/确认后台服务", command=lambda: actions.start_backend(self)
        )
        service_menu.add_command(
            label="重启后台服务", command=lambda: actions.restart_backend(self)
        )
        service_menu.add_command(
            label="停止后台服务（强制）", command=lambda: actions.stop_backend(self)
        )
        service_menu.add_separator()
        service_menu.add_command(
            label="打开配置目录", command=lambda: actions.open_config_dir(self)
        )
        service_menu.add_command(
            label="打开后台日志", command=lambda: actions.open_service_log(self)
        )
        menubar.add_cascade(label="服务", menu=service_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="关于 ATRI-X", command=lambda: actions.show_about(self))
        help_menu.add_command(
            label="托盘/后台说明", command=lambda: actions.show_tray_help(self)
        )
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.configure(menu=menubar)

    def _bind_keys(self) -> None:
        self.root.bind("<Control-n>", lambda _event: self.new_session())
        self.root.bind("<F5>", lambda _event: self.refresh_sessions())
        # Ctrl+1…Ctrl+9 对应前 9 页，第 10 页用 Ctrl+0：Tk 没有 <Control-Key-10>
        # 这个 keysym，直接按索引拼会抛 TclError，把整个界面卡在启动阶段。
        for index in range(min(9, len(_TABS))):
            self.root.bind(
                f"<Control-Key-{index + 1}>",
                lambda _event, position=index: self.select_tab(position),
            )
        if len(_TABS) > 9:
            self.root.bind(
                "<Control-Key-0>",
                lambda _event, position=9: self.select_tab(position),
            )

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
            self.chat.render(self.transcript)
            self._dirty = False
        self._update_status()
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def render_now(self) -> None:
        """立即重绘一次（自检/测试用；正常流程由 :meth:`_poll` 驱动）。"""
        self.chat.render(self.transcript)
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
        elif kind == "restart":
            self._handle_restart(payload)

    def _handle_message(self, message: Any) -> None:
        kind = str(getattr(message, "type", "") or "")
        if kind == REMOTE_MSG_SESSIONS_RESULT:
            self.chat.sidebar.set_sessions(decode_sessions(message))
            if self._active_session_id:
                self.chat.sidebar.set_active(self._active_session_id)
            return
        if kind == REMOTE_MSG_HISTORY_RESULT:
            items = decode_history(message)
            if items or str(getattr(message, "session_id", "")):
                self._active_session_id = str(getattr(message, "session_id", "") or "")
                count = load_history_into(self.transcript, items)
                self._dirty = True
                self.chat.sidebar.set_active(self._active_session_id)
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
            self.chat.sidebar.clear()
        if state == STATE_AUTH_FAILED:
            self._append_notice("鉴权失败：请检查 app.toml 的 [platform.cli.server.tokens] 与 .env 里的 token")
        if self._tray is not None:
            self._tray.set_tooltip(f"ATRI-X · {label.lstrip('● ')}")

    def _handle_restart(self, payload: Any) -> None:
        actions.handle_restart(self, payload)

    def _restart_backend(self) -> None:
        """配置页底部的「重启后台服务」按钮回调。"""
        actions.restart_backend(self)

    # ---------------------------------------------------------------- 渲染

    def _update_status(self) -> None:
        if self.status_label.cget("text") != self._status_text:
            self.status_label.configure(text=self._status_text)
        runtime = self._runtime_text or ""
        if self.runtime_label.cget("text") != runtime:
            self.runtime_label.configure(text=runtime)
        self.features_tab.set_status(self._status_text)

    def _append_notice(self, text: str) -> None:
        self.transcript.add_notice(text)
        self._dirty = True

    # ---------------------------------------------------------------- 选项卡

    def select_tab(self, index: int) -> None:
        """切到第 ``index`` 个选项卡（越界忽略）。"""
        if 0 <= index < len(_TABS):
            self.notebook.select(index)

    def show_tab_key(self, key: str) -> None:
        """按「功能」页给的键跳转（``model`` / ``character`` / ``toolperm_params``…）。

        ``toolperm_params`` 还支持 ``toolperm_params:<工具名>`` 这一复合形式：
        切到「工具权限」页的「工具参数（全局）」子选项卡后，直接展开该工具的参数
        面板（1.1.0 实机：图转提示词在「功能」页双击后原来被送去「进阶」页，
        而那一页没有它的字段；真正的字段在工具参数注册表里）。
        """
        base, _, tool = (key or "").partition(":")
        name = _TAB_KEYS.get(base)
        if name is None:
            return
        self.select_tab(_TAB_INDEX[name])
        if base == "toolperm_params":
            toolperm = getattr(self, "toolperm_tab", None)
            show_tool = getattr(toolperm, "show_tool_params", None)
            show_params = getattr(toolperm, "show_global_params", None)
            if tool and callable(show_tool):
                show_tool(tool)
            elif callable(show_params):
                show_params()
        elif key == "toolperm_preview":
            toolperm = getattr(self, "toolperm_tab", None)
            show_preview = getattr(toolperm, "show_preview_tab", None)
            if callable(show_preview):
                show_preview()
        elif key == "character_reactions":
            character = getattr(self, "character_tab", None)
            show_reactions = getattr(character, "show_reactions_tab", None)
            if callable(show_reactions):
                show_reactions()

    def _on_tab_changed(self, _event: Any = None) -> None:
        try:
            index = int(self.notebook.index("current"))
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            return
        previous, self._active_tab_index = self._active_tab_index, index
        if previous is not None and previous != index:
            self._notify_tab_hidden(self._tabs.get(previous))
        try:
            tab = self._ensure_tab(index)
        except Exception as exc:  # noqa: BLE001 - 懒加载失败要能看见，但不能崩窗口
            text = f"打开「{_TABS[index]}」失败：{exc}"
            self._append_notice(text)
            try:
                journal.record(self._tray_config_dir(), text)
            except Exception:  # noqa: BLE001 - 记日志失败不影响提示
                pass
            return
        handler = getattr(tab, "on_show", None)
        if handler is None:
            return
        try:
            handler()
        except (ConfigEditError, tk.TclError) as exc:  # pragma: no cover - 展示给用户
            if hasattr(tab, "fail"):
                tab.fail(exc)

    def _notify_tab_hidden(self, tab: Any) -> None:
        """切走时通知上一页（「在线」页用它停掉状态轮询）。"""
        handler = getattr(tab, "on_hide", None)
        if handler is None:
            return
        try:
            handler()
        except (ConfigEditError, tk.TclError):  # pragma: no cover - 展示期错误不阻断切页
            pass

    # ---------------------------------------------------------------- 交互

    def request_completion(self) -> None:
        """``Tab`` 触发补全：请求候选；拿到后把第一个应用到输入框（连续 Tab 换下一个）。"""
        text = self.chat.get_input()
        if not text.strip():
            return
        if self._completion_items and self._pending_complete:
            item = self._completion_items.pop(0)
            self.chat.set_input(apply_completion(text, item, len(text)))
            return
        if self.client is None or not self.client.connected:
            return
        self._pending_complete = "gui-tab"
        self._completion_items = []
        self.client.request_complete(self._pending_complete, text, len(text))

    def send_current_input(self) -> None:
        text = self.chat.get_input().strip()
        if not text:
            return
        if not text.startswith("/") and (self.client is None or not self.client.connected):
            self._append_notice("还没连上后台服务，消息没有发出去。可以用「服务 → 启动/确认后台服务」。")
            return
        self.chat.set_input("")
        self.transcript.add_user(text)
        self._dirty = True
        if self.client is not None:
            self.client.send_input(text)

    def new_session(self) -> None:
        """新建会话：通知服务端 ``/new``，清空本地记录并刷新列表。"""
        self.transcript.clear()
        self._runtime_text = ""
        self._active_session_id = ""
        self._dirty = True
        if self.client is not None and self.client.connected:
            self.client.send_input("/new")
        self.refresh_sessions()

    def clear_transcript(self) -> None:
        """只清空界面上的聊天记录（不动服务端历史）。"""
        self.transcript.clear()
        self._dirty = True

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
        self.chat.sidebar.set_active(session_id)

    def copy_all(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.transcript.text())

    def reconnect(self) -> None:
        if self.client is None:
            return
        self.client.stop()
        self.client.start()

    # ---------------------------------------------------------------- 服务菜单
    # 具体动作在 :mod:`atri_x.gui.actions`（启动/重启/停止、打开目录/日志、关于），
    # 这里只保证托盘菜单的「打开日志」也能走到同一套实现。

    # ---------------------------------------------------------------- 托盘 / 生命周期

    def _apply_window_icon(self) -> None:
        """给主窗口挂品牌图标。

        **必须显式设置**：Windows 上 Tk 顶层窗口默认用 Tk 自己的羽毛图标，不会继承 exe
        的资源图标（所以只给 exe 换图标，标题栏/任务栏仍然是羽毛）。优先用多尺寸
        ``.ico``（Windows 会按 DPI 自己挑档位，最清晰），失败再退回 ``iconphoto``。

        素材缺失或 Tk 不支持都只是「没有图标」，不能影响启动。
        """
        ico = app_icon()
        if ico:
            try:
                self.root.iconbitmap(ico)
                return
            except tk.TclError:
                pass
        png = app_icon_png()
        if not png:
            return
        try:
            photo = tk.PhotoImage(file=png)
        except tk.TclError:
            return
        try:
            self.root.iconphoto(True, photo)
        except tk.TclError:
            return
        # 必须留引用：PhotoImage 被回收后窗口图标会退回 Tk 默认图标。
        self._icon_photo = photo

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
                ("打开日志", lambda: self.post("tray", "log")),
                None,
                ("退出（后台继续）", lambda: self.post("tray", "quit")),
                ("退出并停止服务", lambda: self.post("tray", "quit-stop")),
            ],
            on_activate=lambda: self.post("tray", "show"),
            icon_path=app_icon(),
        )
        if tray.start():
            self._tray = tray
            return
        # 托盘建不起来不是「无所谓」：关窗口会变成退出窗口、--minimized 更是白屏。
        # 以前这里是静默降级，用户只会看到「没有后台图标」。现在写日志 + 聊天区通知 +
        # 一次性弹窗，并在 --minimized 时强制把窗口显示出来。
        self._tray = None
        self._tray_error = tray.error or "未知原因"
        self._report_tray_failure()

    def _report_tray_failure(self) -> None:
        """托盘不可用：给出可见提示（日志 + 聊天区通知 + 一次性弹窗）。"""
        message = (
            f"托盘图标不可用：{self._tray_error}\n"
            "关闭窗口不会隐藏到托盘，而是退出窗口（后台服务继续运行）。"
        )
        self._append_notice(message)
        journal.record(self._tray_config_dir(), f"托盘图标不可用：{self._tray_error}")
        if self._minimized:
            # 没有托盘时 --minimized（开机自启）会把窗口藏掉且没有任何入口，
            # 所以这里必须把窗口显示回来。
            self._minimized = False
            self.root.deiconify()
        self.root.after(600, self._warn_tray_unavailable)

    def _tray_config_dir(self) -> str:
        """配置目录（用于写界面日志）；自检/无配置场景返回空串。"""
        settings = self.settings
        return str(getattr(settings, "config_dir", "") or "") if settings is not None else ""

    def _warn_tray_unavailable(self) -> None:
        """托盘失败的一次性弹窗（只在第一次失败时弹）。"""
        if self._tray_warned:
            return
        self._tray_warned = True
        messagebox.showwarning(
            "ATRI-X：托盘不可用",
            "托盘图标创建失败，所以关窗口会直接退出窗口（后台服务继续运行）。\n\n"
            f"原因：{self._tray_error}\n\n"
            "如果 ATRI-X 是从某些沙箱/受保护目录（带 Low 完整性标签）启动的，"
            "把程序复制到普通目录（例如 D:\\ATRI-X）再启动即可恢复托盘。",
            parent=self.root,
        )

    def _handle_tray(self, action: str) -> None:  # pragma: no cover - 由托盘线程触发
        if action == "show":
            self.show_window()
        elif action == "new":
            self.new_session()
            self.show_window()
        elif action == "reconnect":
            self.reconnect()
        elif action == "log":
            actions.open_service_log(self)
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

    # ---------------------------------------------------------------- 兼容属性

    @property
    def transcript_view(self) -> tk.Text:
        """聊天记录控件（自检/测试用的旧名字）。"""
        return self.chat.view

    @property
    def input_box(self) -> tk.Text:
        """输入框控件（旧名字）。"""
        return self.chat.input_box

    @property
    def sidebar(self) -> Any:
        """左侧会话列表（旧名字）。"""
        return self.chat.sidebar


# ---------------------------------------------------------------------------
# 配置路径
# ---------------------------------------------------------------------------
def _config_path_for(settings: GuiSettings | None) -> str:
    """确定 ``app.toml`` 路径：优先 GUI 设置里的；否则按查找顺序推（不创建文件）。"""
    if settings is not None and settings.config_path:
        return settings.config_path
    return default_config_path()
