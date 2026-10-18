"""子设置弹窗的公共骨架：独立窗口 + 底部状态行 + 保存/重载/关闭按钮。

「进阶」页里的三块高级设置（自学习、Hook 插件、定时维护任务）不占主选项卡，
而是点按钮开一个 :class:`Dialog` 子窗口。这里收掉窗口创建、状态行、按钮条、
配置错误弹窗和「界面操作日志」这些每个弹窗都要来一遍的部分。

约定与选项卡一致：

- 可预期的配置错误（:class:`atri_x.gui.configedit.ConfigEditError`）走 :meth:`fail`：
  状态行 + 弹窗，不让异常穿出 Tk 回调；
- 成功保存后调 :meth:`saved`：写状态行、记 ``gui/gui.log``、通知宿主页刷新；
- 窗口是 ``transient``（跟随主窗口）但**不** ``grab_set``：弹窗开着时仍允许看日志页。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x.gui import journal
from atri_x.gui.configedit import ConfigEditError, ConfigPaths

__all__ = ["Dialog"]


class Dialog:
    """子设置窗口的基类。子类覆盖 :meth:`build` / :meth:`reload` / :meth:`save`。"""

    #: 窗口标题。
    title = "设置"
    #: 窗口初始尺寸。
    geometry = "720x520"

    def __init__(
        self,
        parent: Any,
        paths: ConfigPaths | None,
        *,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        self.paths = paths
        self.on_saved = on_saved
        self._parent = parent
        self.window = tk.Toplevel(parent)
        self.window.title(self.title)
        self.window.geometry(self.geometry)
        self.window.minsize(520, 360)
        try:
            self.window.transient(parent.winfo_toplevel())
        except tk.TclError:  # pragma: no cover - 父窗口不可用
            pass
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.body = ttk.Frame(self.window, padding=(12, 10, 12, 8))
        self.body.pack(fill=tk.BOTH, expand=True)
        self._buttons = ttk.Frame(self.window, padding=(12, 0, 12, 10))
        self._buttons.pack(fill=tk.X)
        self._status = tk.StringVar(value="")
        ttk.Label(self._buttons, textvariable=self._status, foreground="#555555").pack(
            side=tk.LEFT
        )
        ttk.Button(self._buttons, text="关闭", command=self.close).pack(side=tk.RIGHT)
        self.build()
        self.reload()

    # -- 子类接口 -------------------------------------------------------------
    def build(self) -> None:  # pragma: no cover - 子类覆盖
        """构建表单（在 ``self.body`` 里）。"""

    def reload(self) -> None:  # pragma: no cover - 子类覆盖
        """从配置文件重新读入表单。"""

    def save(self) -> None:  # pragma: no cover - 子类覆盖
        """校验并写回配置。"""

    # -- 状态 / 按钮 -----------------------------------------------------------
    def notify(self, text: str) -> None:
        self._status.set(text)

    def fail(self, exc: BaseException | str) -> None:
        text = str(exc)
        self._status.set(f"失败：{text}")
        try:
            from tkinter import messagebox

            messagebox.showerror("保存失败", text, parent=self.window)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass

    def saved(self, text: str) -> None:
        """保存成功的统一收尾：状态行 + 操作日志 + 通知宿主页。"""
        self.notify(text)
        self.journal(text)
        if self.on_saved is not None:
            try:
                self.on_saved()
            except tk.TclError:  # pragma: no cover - 宿主页已销毁
                pass

    def journal(self, text: str) -> None:
        if self.paths is not None:
            journal.record(self.paths.config_dir, text)

    def button(self, text: str, command: Callable[[], None], *, side: str = tk.RIGHT) -> ttk.Button:
        widget = ttk.Button(self._buttons, text=text, command=command)
        widget.pack(side=side, padx=(0, 6))
        return widget

    # -- 生命周期 -------------------------------------------------------------
    def close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:  # pragma: no cover - 已经销毁
            pass

    def is_ready(self) -> bool:
        return self.paths is not None

    def require_paths(self) -> ConfigPaths:
        if self.paths is None:
            raise ConfigEditError("找不到 app.toml，无法保存配置")
        return self.paths
