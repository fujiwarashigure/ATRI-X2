"""「日志」选项卡：把后台服务写的日志和界面操作日志放到一个可筛选的窗口里。

数据来源都是本地文件，不经过 WS、不需要服务在线：

| 通道 | 文件 | 说明 |
|---|---|---|
| 运行日志 | ``<数据目录>/logs/atri-x-YYYY-MM-DD.log`` | 服务主日志（结构化键值行） |
| 审计日志 | ``<数据目录>/logs/audit-YYYY-MM-DD.log`` | 权限/敏感操作审计 |
| Atrinis 日志 | ``<数据目录>/logs/atrinis-YYYY-MM-DD.log`` | 外部事件入口 |
| 后台服务输出 | ``<配置目录>/gui/service.out.log`` | 服务进程 stdout/stderr（崩溃栈常在这里） |
| 界面操作 | ``<配置目录>/gui/gui.log`` | 桌面端"点了保存/拉了模型"的痕迹 |

前三个通道用 :class:`atri_x.logging.reader.Reader` 反向分块读：只解析到够 ``条数`` 为止，
不会把上百 MB 的日志整份读进内存；后两个通道是纯文本，按行取尾部。

级别下拉只对结构化通道生效（服务输出/界面操作的每一行没有统一的 ``level=`` 字段），
界面上会用一行灰字说明当前口径。
"""

from __future__ import annotations

import os
import re
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk
from typing import Any

from atri_x.gui import journal
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.process import open_path, service_log_path
from atri_x.gui.tabs.base import ConfigTab, message
from atri_x.logging.reader import LogQuery, Reader

__all__ = ["LogsTab"]

#: 通道展示名 → 内部键（顺序即下拉顺序）。
_CHANNELS = (
    ("运行日志（atri-x）", "runtime"),
    ("审计日志（audit）", "audit"),
    ("Atrinis 日志（atrinis）", "atrinis"),
    ("后台服务输出（service.out.log）", "service"),
    ("界面操作（gui.log）", "gui"),
)
_CHANNEL_KEY = {label: key for label, key in _CHANNELS}

#: 结构化通道 → 日志文件名前缀。
_PREFIX = {"runtime": "atri-x", "audit": "audit", "atrinis": "atrinis"}

#: 级别下拉项（「全部」= 不按级别过滤）。
_LEVELS = ("全部", "debug", "info", "warn", "error")

#: 单次最多显示多少条。
_LIMITS = (100, 300, 1000, 3000)

#: 纯文本通道尾部最多读多少字节。
_TAIL_BYTES = 512 * 1024

#: 自动刷新间隔（毫秒）。
_AUTO_REFRESH_MS = 3000

#: 日志文件名里的日期。
_DAY_IN_NAME = re.compile(r"^(?P<prefix>[A-Za-z0-9_-]+?)-(?P<day>\d{4}-\d{2}-\d{2})\.log$")


class LogsTab(ConfigTab):
    """日志查看器（通道 / 日期 / 级别 / 关键字 / 条数 + 自动刷新）。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._auto_timer: str | None = None
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self.body)
        top.pack(fill=tk.X)
        ttk.Label(top, text="通道").pack(side=tk.LEFT)
        self.channel_var = tk.StringVar(value=_CHANNELS[0][0])
        self.channel_box = ttk.Combobox(
            top,
            textvariable=self.channel_var,
            values=[label for label, _key in _CHANNELS],
            state="readonly",
            width=26,
            height=8,
        )
        self.channel_box.pack(side=tk.LEFT, padx=(4, 12))
        self.channel_box.bind("<<ComboboxSelected>>", self._channel_changed)

        ttk.Label(top, text="日期").pack(side=tk.LEFT)
        self.day_var = tk.StringVar()
        self.day_box = ttk.Combobox(top, textvariable=self.day_var, state="readonly", width=12, height=12)
        self.day_box.pack(side=tk.LEFT, padx=(4, 12))
        self.day_box.bind("<<ComboboxSelected>>", lambda _event: self.render())

        ttk.Label(top, text="级别").pack(side=tk.LEFT)
        self.level_var = tk.StringVar(value="全部")
        self.level_box = ttk.Combobox(
            top, textvariable=self.level_var, values=list(_LEVELS), state="readonly", width=7
        )
        self.level_box.pack(side=tk.LEFT, padx=(4, 12))
        self.level_box.bind("<<ComboboxSelected>>", lambda _event: self.render())

        ttk.Label(top, text="关键字").pack(side=tk.LEFT)
        self.keyword_var = tk.StringVar()
        keyword = ttk.Entry(top, textvariable=self.keyword_var, width=18)
        keyword.pack(side=tk.LEFT, padx=(4, 12))
        keyword.bind("<Return>", lambda _event: self.render())

        ttk.Label(top, text="条数").pack(side=tk.LEFT)
        self.limit_var = tk.StringVar(value="300")
        self.limit_box = ttk.Combobox(
            top,
            textvariable=self.limit_var,
            values=[str(item) for item in _LIMITS],
            state="readonly",
            width=6,
        )
        self.limit_box.pack(side=tk.LEFT, padx=(4, 0))
        self.limit_box.bind("<<ComboboxSelected>>", lambda _event: self.render())

        bar = ttk.Frame(self.body)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="刷新", command=self.render).pack(side=tk.LEFT, padx=(0, 4))
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="自动刷新（3 秒）", variable=self.auto_var, command=self._toggle_auto
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(bar, text="打开文件", command=self._open_file).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="打开目录", command=self._open_dir).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="复制全部", command=self._copy_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="导出…", command=self.export).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="清空视图", command=lambda: self._show_text("")).pack(side=tk.LEFT)

        self.info = ttk.Label(self.body, text="", foreground="#555555", wraplength=980)
        self.info.pack(fill=tk.X, pady=(6, 2))

        wrap = ttk.Frame(self.body)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(wrap, wrap=tk.NONE, font=("Consolas", 9), height=18, state=tk.DISABLED)
        yscroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.text.yview)
        xscroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.text.tag_configure("error", foreground="#b00020")
        self.text.tag_configure("warn", foreground="#b26a00")
        self.text.tag_configure("debug", foreground="#7a7a7a")
        self.text.tag_configure("meta", foreground="#555555")

        self.button("重新载入", self.reload)

    # -- 数据 ---------------------------------------------------------------
    def on_show(self) -> None:
        """只读页：切回来时重新扫一遍日期并渲染。"""
        self.reload()

    def reload(self) -> None:
        """刷新日期候选与正文。"""
        if not self.is_ready():
            self._show_text("")
            self.info.configure(text="找不到配置文件：请先用桌面端启动一次。")
            return
        try:
            self._reload_days()
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.render()

    def render(self) -> None:
        """按当前筛选条件重新渲染正文。"""
        if not self.is_ready():
            return
        channel = self._channel()
        limit = _limit_of(self.limit_var.get())
        keyword = self.keyword_var.get().strip()
        level = "" if self.level_var.get() == "全部" else self.level_var.get()
        if channel in _PREFIX:
            self._render_structured(_PREFIX[channel], channel, limit, level, keyword)
        else:
            self._render_plain(channel, limit, keyword)

    def _render_structured(
        self, prefix: str, channel: str, limit: int, level: str, keyword: str
    ) -> None:
        day = self.day_var.get().strip()
        if not day:
            self._show_text("")
            self.info.configure(
                text=f"日志目录里还没有 {prefix}-YYYY-MM-DD.log：{self.require_paths().log_dir}"
            )
            self.notify("还没有这个通道的日志")
            return
        try:
            reader = Reader(dir=self.require_paths().log_dir, today=_day_start(day))
            result = reader.query(
                None,
                LogQuery(prefix=prefix, days=1, limit=limit, min_level=level, contains=keyword),
            )
        except (OSError, ValueError) as exc:
            self._show_text("")
            self.info.configure(text=f"读取日志失败：{exc}")
            self.notify(f"读取日志失败：{exc}")
            return
        lines = [entry.raw for entry in result.entries]
        self._fill(lines, channel)
        path = os.path.join(self.require_paths().log_dir, f"{prefix}-{day}.log")
        detail = f"{path} · 命中 {len(result.entries)} 条"
        if result.damaged_count:
            detail += f"（另有 {result.damaged_count} 行损坏被跳过）"
        self.info.configure(text=detail)
        self.notify(f"已显示 {len(result.entries)} 条（{_label_of(channel)} / {day}）")

    def _render_plain(self, channel: str, limit: int, keyword: str) -> None:
        path = self._target_file(channel)
        text = _tail_text(path, limit * 2)
        lines = text.splitlines()
        if keyword:
            needle = keyword.casefold()
            lines = [line for line in lines if needle in line.casefold()]
        lines = lines[-limit:]
        self._fill(lines, channel)
        if not lines:
            detail = f"{path} · 没有内容（或都被关键字过滤掉了）"
        else:
            detail = f"{path} · 显示 {len(lines)} 行（纯文本通道，级别过滤不生效）"
        self.info.configure(text=detail)
        self.notify(f"已显示 {len(lines)} 行（{_label_of(channel)}）")

    def _fill(self, lines: list[str], channel: str) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        if not lines:
            hint = "（没有可显示的行）"
            if channel in ("service", "gui"):
                hint += f"\n路径：{self._target_file(channel)}"
            self.text.insert(tk.END, hint, ("meta",))
        else:
            for line in lines:
                self.text.insert(tk.END, line + "\n", (channel_level_tag(line),))
        self.text.configure(state=tk.DISABLED)
        self.text.see("1.0")

    def _show_text(self, text: str) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        if text:
            self.text.insert(tk.END, text)
        self.text.configure(state=tk.DISABLED)

    # -- 通道 / 日期 ---------------------------------------------------------
    def _channel(self) -> str:
        return _CHANNEL_KEY.get(self.channel_var.get(), "runtime")

    def _channel_changed(self, _event: Any = None) -> None:
        self._reload_days()
        self.render()

    def _reload_days(self) -> None:
        channel = self._channel()
        if channel not in _PREFIX:
            self.day_box.configure(values=[], state=tk.DISABLED)
            self.day_var.set("")
            return
        days = _list_days(self.require_paths().log_dir, _PREFIX[channel])
        current = self.day_var.get().strip()
        self.day_box.configure(values=days, state="readonly")
        self.day_var.set(current if current in days else (days[0] if days else ""))

    # -- 文件 ---------------------------------------------------------------
    def _target_file(self, channel: str) -> str:
        """当前通道对应的**今天/最新**文件（打开文件、显示路径用）。"""
        if channel in _PREFIX:
            day = self.day_var.get().strip() or datetime.now().strftime("%Y-%m-%d")
            return os.path.join(self.require_paths().log_dir, f"{_PREFIX[channel]}-{day}.log")
        if channel == "service":
            return service_log_path()
        return journal.journal_path(self.require_paths().config_dir)

    def _open_file(self) -> None:
        path = self._target_file(self._channel())
        if not os.path.exists(path):
            message(self.frame, "打开日志", f"还没有这个文件：\n{path}")
            return
        if not open_path(path):
            message(self.frame, "打开日志", f"日志文件：\n{path}")

    def _open_dir(self) -> None:
        path = self._target_file(self._channel())
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            message(self.frame, "打开日志目录", f"目录还不存在：\n{directory}")
            return
        if not open_path(directory):
            message(self.frame, "打开日志目录", f"日志目录：\n{directory}")

    def _copy_all(self) -> None:
        text = self.text.get("1.0", "end-1c")
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
        except tk.TclError:  # pragma: no cover - 剪贴板被占用
            message(self.frame, "复制日志", "剪贴板不可用")
            return
        self.notify(f"已复制 {len(text.splitlines())} 行到剪贴板")

    # -- 导出 ---------------------------------------------------------------
    def export(self) -> None:
        """把当前视图（已按通道/级别/关键字过滤）导出成文件。"""
        body = self.text.get("1.0", "end-1c")
        if not body.strip():
            self.notify("当前视图是空的，没有可导出的内容")
            return
        try:
            path = filedialog.asksaveasfilename(
                parent=self.frame,
                title="导出日志",
                defaultextension=".log",
                initialfile=self._default_export_name(),
                filetypes=(("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")),
            )
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            return
        if not path:
            return
        try:
            self._export_to(path)
        except OSError as exc:
            message(self.frame, "导出日志", f"写文件失败：{exc}")
            return
        self.journal(f"导出日志：{path}（{self._label()}）")
        self.notify(f"已导出 {len(body.splitlines())} 行到 {path}")

    def export_text(self) -> str:
        """当前视图的纯文本（带一段说明头，导出的文件自带上下文）。"""
        body = self.text.get("1.0", "end-1c")
        channel = self._channel()
        day = self.day_var.get().strip() or "（纯文本通道）"
        header = (
            "# ATRI-X 日志导出",
            f"# 通道：{_label_of(channel)}",
            f"# 日期：{day}",
            f"# 级别：{self.level_var.get()}",
            f"# 关键字：{self.keyword_var.get().strip() or '（无）'}",
            f"# 条数上限：{self.limit_var.get()}",
            f"# 行数：{len(body.splitlines())}",
            f"# 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )
        return "\n".join(header) + "\n\n" + body + "\n"

    def _export_to(self, path: str) -> str:
        """把 :meth:`export_text` 写到指定路径（UTF-8 + LF），返回路径。"""
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.export_text())
        return path

    def _default_export_name(self) -> str:
        channel = self._channel()
        day = self.day_var.get().strip() or datetime.now().strftime("%Y-%m-%d")
        return f"atri-x-{channel}-{day}.log"

    def _label(self) -> str:
        return _label_of(self._channel())

    # -- 自动刷新 -----------------------------------------------------------
    def _toggle_auto(self) -> None:
        if self.auto_var.get():
            self._schedule_auto()
            self.notify("自动刷新已开启（每 3 秒）")
        else:
            self._cancel_auto()
            self.notify("自动刷新已关闭")

    def _schedule_auto(self) -> None:
        self._cancel_auto()
        try:
            self._auto_timer = self.frame.after(_AUTO_REFRESH_MS, self._tick)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            self._auto_timer = None

    def _cancel_auto(self) -> None:
        if self._auto_timer is None:
            return
        try:
            self.frame.after_cancel(self._auto_timer)
        except tk.TclError:  # pragma: no cover - 已经触发过
            pass
        self._auto_timer = None

    def _tick(self) -> None:
        self._auto_timer = None
        if not self.auto_var.get():
            return
        try:
            self.reload()
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            return
        self._schedule_auto()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _limit_of(text: str) -> int:
    try:
        value = int((text or "").strip())
    except ValueError:
        return 300
    return min(max(value, 10), 20000)


def _label_of(channel: str) -> str:
    for label, key in _CHANNELS:
        if key == channel:
            return label
    return channel


def _day_start(day: str) -> datetime:
    """日期字符串 → 当天正午的本地 naive 时间（``Reader`` 只用它的 ``.date()``）。"""
    try:
        return datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return datetime.now()


def _list_days(log_dir: str, prefix: str) -> list[str]:
    """列出日志目录里属于 ``prefix`` 的日期（新 → 旧）。"""
    days: list[str] = []
    try:
        entries = list(os.scandir(log_dir))
    except OSError:
        return days
    for entry in entries:
        if not entry.is_file():
            continue
        match = _DAY_IN_NAME.match(entry.name)
        if match and match.group("prefix") == prefix:
            days.append(match.group("day"))
    return sorted(set(days), reverse=True)


def _tail_text(path: str, lines: int) -> str:
    """读文件末尾（最多 ``_TAIL_BYTES`` 字节）的文本。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    return "\n".join(rows[-max(lines, 1) :])


def channel_level_tag(line: str) -> str:
    """按行里的 ``level=`` 字段给一行选颜色标签（没有就返回空串）。"""
    for level in ("ERROR", "WARN", "DEBUG"):
        if f"level={level}" in line:
            return level.lower()
    return ""


# 便于测试直接引用的名字（小写下划线形式）。
level_tag = channel_level_tag
