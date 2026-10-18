"""「HOOK 插件配置」子窗口：直接编辑 ``plugins/hooks.toml``。

Hook 的配置是**声明式 TOML**（规则、插件引用、匹配条件、动作），字段多且示例注释
本身就承担文档作用，所以这里不做表单化，只提供：

- 等宽字体编辑区（整份文件读写，保留注释）；
- 保存前用 ``tomllib`` 校验语法 —— 拒绝把 hooks.toml 写成语法错误，否则后台服务
  启动时会整份加载失败；
- 「重新载入」「打开文件」「打开目录」和规则/插件计数。

保存后的生效方式：管理员在聊天里执行全局命令 ``/hooks reload``（或重启后台服务）。
"""

from __future__ import annotations

import os
import tkinter as tk
import tomllib
from tkinter import ttk
from typing import Any

from atri_x.gui import process
from atri_x.gui.configedit import ConfigEditError, write_text_file
from atri_x.gui.dialogs.common import Dialog

__all__ = ["HooksDialog"]

#: 文件不存在时给一份最小骨架（不是完整模板；完整注释见发行资产）。
_EMPTY_TEMPLATE = (
    "# Hook 规则入口。完整字段说明见配置目录里的发行模板或 docs/hooks.md。\n"
    "# 改完保存后，在聊天里执行 /hooks reload 让后台服务重新加载。\n"
    "\n"
    "# [[rules]]\n"
    '# name = "demo"\n'
    '# on = "message.received"\n'
    '# always = true\n'
    '# actions = [{ type = "send", kind = "text", text = "hi" }]\n'
)


class HooksDialog(Dialog):
    """``plugins/hooks.toml`` 编辑器。"""

    title = "HOOK 插件配置（plugins/hooks.toml）"
    geometry = "860x600"

    def build(self) -> None:
        info = ttk.Frame(self.body)
        info.pack(fill=tk.X)
        self.path_label = ttk.Label(info, text="", foreground="#555555", wraplength=800)
        self.path_label.pack(anchor="w")
        self.count_label = ttk.Label(self.body, text="", foreground="#555555")
        self.count_label.pack(anchor="w", pady=(2, 6))

        wrap = ttk.Frame(self.body)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(
            wrap, wrap=tk.NONE, font=("Consolas", 9), undo=True, height=22
        )
        yscroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.text.yview)
        xscroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        ttk.Label(
            self.body,
            text=(
                "保存前会校验 TOML 语法；保存后执行 /hooks reload（或重启后台服务）生效。"
                "规则里写的相对路径默认相对 plugins/。"
            ),
            foreground="#8a8a8a",
            wraplength=800,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

        self.button("保存", self._save)
        self.button("重新载入", self.reload)
        self.button("打开文件", self._open_file)
        self.button("打开目录", self._open_dir)

    # -- 数据 ---------------------------------------------------------------
    def _hooks_path(self) -> str:
        return self.require_paths().hooks_path

    def reload(self) -> None:
        if not self.is_ready():
            return
        path = self._hooks_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            text = _EMPTY_TEMPLATE
        except OSError as exc:
            self.fail(ConfigEditError(f"读取 {path} 失败：{exc}"))
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", text)
        self.path_label.configure(text=f"文件：{path}")
        self._update_counts()
        self.notify("已载入 hooks.toml")

    def _update_counts(self) -> None:
        try:
            data = tomllib.loads(self.text.get("1.0", "end-1c"))
        except tomllib.TOMLDecodeError as exc:
            self.count_label.configure(text=f"当前内容不是合法 TOML：{exc}")
            return
        rules = data.get("rules")
        plugins = data.get("plugins")
        rule_count = len(rules) if isinstance(rules, list) else 0
        plugin_count = len(plugins) if isinstance(plugins, list) else 0
        self.count_label.configure(text=f"规则 {rule_count} 条，插件 {plugin_count} 个")

    def _save(self) -> None:
        text = self.text.get("1.0", "end-1c")
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self.fail(ConfigEditError(f"hooks.toml 不是合法 TOML：{exc}"))
            return
        try:
            directory = os.path.dirname(self._hooks_path())
            if directory:
                os.makedirs(directory, exist_ok=True)
            write_text_file(self._hooks_path(), text)
        except (ConfigEditError, OSError) as exc:
            self.fail(exc)
            return
        self._update_counts()
        self.saved("已保存 plugins/hooks.toml；执行 /hooks reload 后生效")

    def _open_file(self) -> None:
        path = self._hooks_path()
        if not os.path.exists(path):
            self._save()
        if not process.open_path(path):
            self.notify(f"打不开文件，路径：{path}")

    def _open_dir(self) -> None:
        directory = os.path.dirname(self._hooks_path())
        os.makedirs(directory, exist_ok=True)
        if not process.open_path(directory):
            self.notify(f"打不开目录，路径：{directory}")


def _count_hooks(text: str) -> tuple[int, int]:
    """解析文本里的规则/插件数（测试用；解析失败返回 ``(0, 0)``）。"""
    try:
        data: Any = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return 0, 0
    rules = data.get("rules")
    plugins = data.get("plugins")
    return (
        len(rules) if isinstance(rules, list) else 0,
        len(plugins) if isinstance(plugins, list) else 0,
    )
