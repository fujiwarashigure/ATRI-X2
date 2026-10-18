"""「关于 ATRI-X」窗口：版本、项目简介、仓库地址、推荐语与署名。

菜单「帮助 → 关于 ATRI-X」打开本窗口。这里刻意做成独立窗口而不是
``messagebox``：仓库地址要能点开、推荐语要能整段选中复制，`messagebox` 做不到。

文案常量放在模块顶层（:data:`DESCRIPTION` / :data:`REPO_URL` / :data:`RECOMMEND` /
:data:`POWERED_BY`），不建窗口也能单独断言/复用（比如打包脚本或 ``--selftest``）。
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Any

from atri_x import __version__

__all__ = [
    "DESCRIPTION",
    "POWERED_BY",
    "RECOMMEND",
    "REPO_URL",
    "TAGLINE",
    "about_text",
    "show_about_window",
]

#: 项目仓库地址（PyPI / 文档 / Hook 模板里的链接都是这一个）。
REPO_URL = "https://github.com/fujiwarashigure/ATRI-X"

#: 一句话定位。
TAGLINE = "ATRI-X · 群聊里也能干活的 AI 助手（Python 重写版）"

#: 项目简述（关于窗口正文）。
DESCRIPTION = (
    "ATRI-X 是一个面向群聊场景的 AI 助手：接 QQ / Telegram / CLI，"
    "支持多模型 Provider、人设卡、群分析、生图、语音转写、长期记忆、"
    "自学习与 Hook 插件，并提供 Windows 桌面端（Tk 前端 + 独立后台服务）。"
)

#: 好评推荐句段（关于窗口里单独一段，鼓励用户传播）。
RECOMMEND = (
    "如果 ATRI-X 帮到了你，欢迎到仓库点一个 Star，或者把它推荐给同样在折腾"
    "群机器人、自建生图和本地模型的朋友 —— 这是对作者最实在的鼓励。"
)

#: 署名（用户指定，固定大写）。
POWERED_BY = "POWERED BY DAFEIYU"


def about_text(version: str | None = None) -> str:
    """关于窗口的纯文本文案（可直接复制，也方便测试）。"""
    current = version or __version__
    return "\n".join(
        (
            f"ATRI-X {current}",
            TAGLINE,
            "",
            DESCRIPTION,
            "",
            f"项目仓库：{REPO_URL}",
            "",
            RECOMMEND,
            "",
            POWERED_BY,
        )
    )


def show_about_window(app: Any) -> tk.Toplevel:
    """打开「关于 ATRI-X」窗口（``app`` 只需要提供 ``root`` 与 ``version``）。"""
    root = getattr(app, "root", app)
    version = str(getattr(app, "version", "") or __version__)
    window = tk.Toplevel(root)
    window.title(f"关于 ATRI-X {version}")
    window.geometry("620x480")
    window.resizable(True, True)
    try:
        window.transient(root)
    except tk.TclError:  # pragma: no cover - 父窗口不可用
        pass

    body = ttk.Frame(window, padding=(16, 14, 16, 12))
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text=f"ATRI-X {version}", font=("Microsoft YaHei UI", 16, "bold")).pack(
        anchor="w"
    )
    ttk.Label(body, text=TAGLINE, foreground="#1b5e20").pack(anchor="w", pady=(2, 8))

    desc = tk.Text(body, height=3, wrap=tk.WORD, font=("Microsoft YaHei UI", 9), relief=tk.FLAT)
    desc.insert("1.0", DESCRIPTION)
    desc.configure(state=tk.DISABLED, background=window.cget("background"))
    desc.pack(fill=tk.X)

    link_row = ttk.Frame(body)
    link_row.pack(fill=tk.X, pady=(10, 0))
    ttk.Label(link_row, text="项目仓库：").pack(side=tk.LEFT)
    link_label = ttk.Label(link_row, text=REPO_URL, foreground="#0b57d0", cursor="hand2")
    link_label.pack(side=tk.LEFT)
    link_label.bind("<Button-1>", lambda _event: _open_url(REPO_URL))

    recommend = tk.Text(body, height=4, wrap=tk.WORD, font=("Microsoft YaHei UI", 9), relief=tk.FLAT)
    recommend.insert("1.0", RECOMMEND)
    recommend.configure(state=tk.DISABLED, background=window.cget("background"))
    recommend.pack(fill=tk.X, pady=(10, 0))

    ttk.Label(
        body, text=POWERED_BY, font=("Microsoft YaHei UI", 11, "bold"), foreground="#8a6d00"
    ).pack(anchor="w", pady=(12, 0))
    ttk.Label(
        body,
        text="桌面端：Tk 前端 + 独立后台服务（service run）；关窗口不影响后台服务运行。",
        foreground="#8a8a8a",
    ).pack(anchor="w", pady=(4, 0))

    bar = ttk.Frame(window, padding=(16, 0, 16, 12))
    bar.pack(fill=tk.X)
    ttk.Button(bar, text="打开项目仓库", command=lambda: _open_url(REPO_URL)).pack(side=tk.LEFT)
    ttk.Button(bar, text="复制说明", command=lambda: _copy(window, about_text(version))).pack(
        side=tk.LEFT, padx=(8, 0)
    )
    ttk.Button(bar, text="关闭", command=window.destroy).pack(side=tk.RIGHT)
    return window


def _open_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 打不开浏览器时静默（窗口里已经写了地址）
        pass


def _copy(widget: Any, text: str) -> None:
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
    except tk.TclError:  # pragma: no cover - 剪贴板被占用
        pass
