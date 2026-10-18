"""配置选项卡的公共骨架：底部状态行、错误弹窗、"需要重启"提示。

多个选项卡都是同一个套路：上面建表单、下面一行「保存 / 重新载入 / 重启后台服务」
按钮 + 一行状态文字。把这条公共部分收在这里，各选项卡只写自己的表单逻辑。

约定：

- 状态文字用 :meth:`ConfigTab.notify` 写（成功绿色语义靠文案，不用颜色）；
- 可预期的配置错误（:class:`atri_x.gui.configedit.ConfigEditError`）统一走
  :meth:`ConfigTab.fail`：状态行 + 弹窗，不让异常穿出 Tk 回调；
- 没有配置文件时（``paths is None``）表单照建但保存会被 :meth:`require_paths`
  拒绝，界面提示"找不到 app.toml"；
- 表单比窗口高的选项卡把类属性 ``scrollable`` 设成 ``True``：``self.body`` 会变成
  可滚动区域里的容器，用户滚轮/拖滚动条就能看到被窗口挡住的那部分（不加滚动时
  Tk 会把超出的控件排到窗口外面，既点不到也看不到）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from atri_x.gui import journal, widgets
from atri_x.gui.configedit import ConfigEditError, ConfigPaths

__all__ = ["ConfigTab", "confirm", "message"]


def message(parent: Any, title: str, text: str, *, error: bool = False) -> None:
    """统一弹窗（父窗口不可用时退化成不弹，避免 Tk 报错）。"""
    try:
        if error:
            messagebox.showerror(title, text, parent=parent)
        else:
            messagebox.showinfo(title, text, parent=parent)
    except tk.TclError:  # pragma: no cover - 窗口已销毁
        pass


def confirm(parent: Any, title: str, text: str) -> bool:
    """确认对话框（父窗口不可用时视为取消）。"""
    try:
        return bool(messagebox.askyesno(title, text, parent=parent))
    except tk.TclError:  # pragma: no cover - 窗口已销毁
        return False


class ConfigTab:
    """配置选项卡基类：``self.body`` 给子类放表单，底部按钮/状态行由基类管。"""

    #: 状态行左侧的固定前缀（例如"需要重启后台服务才生效"）。
    hint = ""

    #: 内容区是否竖向滚动（表单比窗口高的选项卡设 ``True``）；``self.body`` 仍是
    #: 表单的父容器，只是被放进了可滚动区域。
    scrollable = False

    def __init__(
        self,
        parent: Any,
        paths: ConfigPaths | None,
        *,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        self.paths = paths
        self.on_restart = on_restart
        self.frame = ttk.Frame(parent, padding=(10, 8, 10, 8))
        self.scroller: widgets.ScrollFrame | None = None
        if self.scrollable:
            self.scroller = widgets.ScrollFrame(self.frame)
            self.scroller.pack(fill=tk.BOTH, expand=True)
            self.body = self.scroller.inner
        else:
            self.body = ttk.Frame(self.frame)
            self.body.pack(fill=tk.BOTH, expand=True)
        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X, pady=(8, 0))
        self._buttons = ttk.Frame(bar)
        self._buttons.pack(side=tk.LEFT)
        self._status = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._status, foreground="#555555").pack(
            side=tk.LEFT, padx=(12, 0)
        )
        if on_restart is not None:
            ttk.Button(bar, text="重启后台服务", command=on_restart).pack(side=tk.RIGHT)
        if not self.is_ready():
            self.notify("找不到配置文件：请先用桌面端启动一次，或到「功能」页查看配置目录。")

    # -- 状态 -----------------------------------------------------------------
    def is_ready(self) -> bool:
        return self.paths is not None

    def require_paths(self) -> ConfigPaths:
        if self.paths is None:
            raise ConfigEditError("找不到 app.toml，无法保存配置")
        return self.paths

    def notify(self, text: str) -> None:
        prefix = f"{self.hint} · " if self.hint else ""
        self._status.set(prefix + text)

    def fail(self, exc: BaseException | str) -> None:
        text = str(exc)
        self._status.set(f"失败：{text}")
        message(self.frame, "保存失败", text, error=True)

    def ok(self, text: str) -> None:
        self.notify(text)

    def journal(self, text: str) -> None:
        """把一条界面操作记进 ``<配置目录>/gui/gui.log``（「日志」页的「界面操作」通道）。

        只记**用户动作**（保存了什么、拉取成功/失败、下载头像），不记每次重载；
        写失败静默——操作日志不该影响正在做的事。
        """
        if self.paths is not None:
            journal.record(self.paths.config_dir, text)

    # -- 按钮 -----------------------------------------------------------------
    def button(self, text: str, command: Callable[[], None], *, side: str = tk.LEFT) -> ttk.Button:
        """在底部按钮条上放一个按钮（子类在 :meth:`build` 里一次建好）。"""
        widget = ttk.Button(self._buttons, text=text, command=command)
        widget.pack(side=side, padx=(0, 6))
        return widget

    # -- 生命周期 -------------------------------------------------------------
    def reload(self) -> None:  # pragma: no cover - 子类覆盖
        """从配置文件重新载入表单（切到本页时调用）。"""

    def on_show(self) -> None:
        """切到本页时调用。

        默认**不重新载入**：配置页可能还有用户没保存的输入，切走再切回不该丢；
        需要刷新的页（比如只读的「功能」页）自己覆盖本方法。
        """
