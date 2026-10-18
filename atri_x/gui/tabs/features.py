"""「功能」选项卡：功能状态一览 + 服务/日志面板。

上半部分是 :func:`atri_x.gui.featureview.collect_features` 产出的只读状态表
（哪些功能开了、配置在哪、还缺什么），双击一行跳到对应的配置选项卡；
下半部分是运维入口：连接状态、重启后台服务、打开配置目录、看后台日志。

设计上刻意不做成"设置项汇总"：改配置去各页改，这里只回答"我现在有哪些能力、
哪些没开、去哪开"。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.featureview import FeatureRow, collect_features
from atri_x.gui.process import open_path, service_log_path
from atri_x.gui.tabs.base import ConfigTab, message

__all__ = ["FeaturesTab"]

#: 日志查看窗口里最多显示多少行（只读尾部，不把整个文件读进内存）。
_LOG_TAIL_LINES = 400

#: 功能行 → 配置文件（显示在说明列时用不到，留给"打开"按钮）。
_TAB_NAMES = {
    "model": "模型",
    "character": "人设卡",
    "platform": "平台",
    "members": "群友",
    "knowledge": "知识库",
    "logs": "日志",
}


class FeaturesTab(ConfigTab):
    """功能状态表 + 服务与日志。"""

    hint = ""

    def __init__(
        self,
        parent: Any,
        paths: Any,
        *,
        on_goto: Callable[[str], None] | None = None,
        on_restart: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        service_status: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self.on_goto = on_goto
        self.on_reconnect = on_reconnect
        self._service_status = service_status
        self._last_status = ""
        self._rows: dict[str, FeatureRow] = {}
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.button("刷新状态", self.reload)
        self.button("打开配置目录", self._open_config_dir)
        self.button("打开后台日志", self._open_log_file)
        self.button("查看日志", self._show_log_window)

        columns = ("name", "status", "detail")
        self.tree = ttk.Treeview(self.body, columns=columns, show="headings", height=14)
        self.tree.heading("name", text="功能")
        self.tree.heading("status", text="状态")
        self.tree.heading("detail", text="说明 / 关键配置")
        self.tree.column("name", width=200, stretch=False)
        self.tree.column("status", width=80, stretch=False)
        self.tree.column("detail", width=520)
        scrollbar = ttk.Scrollbar(self.body, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<Double-Button-1>", self._goto_selected)
        self.tree.tag_configure("on", foreground="#1b5e20")
        self.tree.tag_configure("off", foreground="#8a8a8a")

        service_box = ttk.LabelFrame(self.body, text="服务", padding=8)
        service_box.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        self.status_label = ttk.Label(service_box, text="", wraplength=180)
        self.status_label.pack(anchor="w", pady=(0, 6))
        self.service_hint = ttk.Label(service_box, text="", foreground="#555555", wraplength=180)
        self.service_hint.pack(anchor="w")
        ttk.Button(service_box, text="重新连接", width=14, command=self._reconnect).pack(
            fill=tk.X, pady=(8, 0)
        )
        ttk.Button(service_box, text="打开配置目录", width=14, command=self._open_config_dir).pack(
            fill=tk.X, pady=(4, 0)
        )
        ttk.Button(service_box, text="查看后台日志", width=14, command=self._show_log_window).pack(
            fill=tk.X, pady=(4, 0)
        )

    # -- 数据 ---------------------------------------------------------------
    def on_show(self) -> None:
        """只读页，每次切回来都刷新一次状态。"""
        self.reload()

    def reload(self) -> None:
        if not self.is_ready():
            self.tree.delete(*self.tree.get_children())
            return
        try:
            rows = collect_features(self.require_paths())
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._rows = {row.key: row for row in rows}
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            tag = "on" if row.status in ("已启用", "已配置", "默认启用", "已安装") else "off"
            self.tree.insert(
                "", tk.END, iid=row.key, values=(row.name, row.status, row.detail), tags=(tag,)
            )
        for item in selected:
            if item in self._rows and self.tree.exists(item):
                self.tree.selection_set(item)
        self.notify(f"共 {len(rows)} 项功能；双击一行跳到对应配置页")

    def set_status(self, text: str) -> None:
        """由主窗口推送连接状态（``● 已连接`` 之类）。"""
        if text == self._last_status:
            return
        self._last_status = text
        self.status_label.configure(text=text)

    # -- 动作 ---------------------------------------------------------------
    def _goto_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection or self.on_goto is None:
            return
        row = self._rows.get(str(selection[0]))
        if row is None or not row.tab:
            message(
                self.frame,
                "功能说明",
                f"{row.name if row else ''}\n\n{row.detail if row else ''}\n\n"
                "这个功能没有单独的选项卡：在「进阶」页的按钮（自学习 / HOOK / 定时维护任务）"
                "或配置文件里改（点「打开配置目录」）。",
            )
            return
        self.on_goto(row.tab)

    def _open_config_dir(self) -> None:
        if not self.is_ready():
            return
        directory = self.require_paths().config_dir
        if not open_path(directory):
            message(self.frame, "打开配置目录", f"打不开目录：{directory}")

    def _open_log_file(self) -> None:
        path = service_log_path()
        if not os.path.exists(path):
            message(self.frame, "后台日志", f"还没有日志：{path}")
            return
        if not open_path(path):
            message(self.frame, "后台日志", f"日志文件：{path}")

    def _reconnect(self) -> None:
        if self.on_reconnect is not None:
            self.on_reconnect()

    def _show_log_window(self) -> None:
        """「查看日志」：有「日志」选项卡就跳过去，没有才退回独立窗口。"""
        if self.on_goto is not None:
            self.on_goto("logs")
            return
        self._open_log_window()

    def _open_log_window(self) -> None:
        path = service_log_path()
        window = tk.Toplevel(self.frame)
        window.title("ATRI-X 后台日志")
        window.geometry("900x520")
        bar = ttk.Frame(window)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        text = tk.Text(window, wrap=tk.NONE, font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(window, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)

        def refresh() -> None:
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert("1.0", _tail(path, _LOG_TAIL_LINES) or f"（日志是空的：{path}）")
            text.see(tk.END)
            text.configure(state=tk.DISABLED)

        ttk.Button(bar, text="刷新", command=refresh).pack(side=tk.LEFT, padx=6, pady=4)
        ttk.Button(bar, text="关闭", command=window.destroy).pack(side=tk.LEFT, pady=4)
        ttk.Button(bar, text="路径", command=lambda: message(window, "日志路径", path)).pack(
            side=tk.LEFT, padx=6, pady=4
        )
        refresh()


def _tail(path: str, lines: int) -> str:
    """读文件末尾 ``lines`` 行（最多读 256 KiB，避免大日志卡界面）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > 256 * 1024:
                handle.seek(size - 256 * 1024)
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    return "\n".join(rows[-lines:])
