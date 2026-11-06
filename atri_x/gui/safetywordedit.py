"""「内容安全 → 词表」子页：用户词表 / 自定义正则的编辑与离线试扫。

数据目录由 ``safety.toml`` 的 ``[local].wordlist_dir`` / ``[local].sensitive_dir``
决定（默认 ``safety/wordlists`` 与 ``safety/sensitive``）：

* ``*.txt``：一行一个词条，``#`` 注释，文件名（去后缀）就是类别名；新类别自动生效。
  同名类别与内置词表**合并**（用户词条在前）；
* ``*.regex``：一行一个 ``名字 = /正则/flags``；非法行整条跳过（后台日志会提示）。

右下角的「试扫」用**当前策略**在本地跑一遍，不联网、不写审计，用来确认
"我要拦的能拦住、我要放行的不误伤"。改完保存后后台 ≤1 秒热加载。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import safetyconfig
from atri_x.gui import process as gui_process
from atri_x.gui.tabs.base import confirm

__all__ = ["WordlistPanel"]

#: 新建词表时默认写入的说明头，防止用户不知道格式。
_WORDLIST_HEADER = "# 一行一个词条；# 开头是注释；文件名（去 .txt）= 类别名。\n"
#: 新建正则时默认写入的格式说明。
_REGEX_HEADER = "# 一行一个：名字 = /正则/flags（flags 支持 i/m/s）。\n"


class WordlistPanel:
    """词表/正则文件列表 + 文本编辑 + 本地试扫。"""

    def __init__(self, parent: Any, paths: Any, tab: Any) -> None:
        self.paths = paths
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=8)
        self._files: dict[str, safetyconfig.WordlistFile] = {}
        self._current = ""
        self._dirs_label = tk.StringVar(value="")
        self._scan_status = tk.StringVar(value="")
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self.frame)
        top.pack(fill=tk.X)
        ttk.Label(top, textvariable=self._dirs_label, foreground="#555555").pack(
            side=tk.LEFT
        )
        ttk.Button(top, text="打开词表目录", command=lambda: self._open("wordlist")).pack(
            side=tk.RIGHT
        )
        ttk.Button(top, text="打开正则目录", command=lambda: self._open("regex")).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        body = ttk.Frame(self.frame)
        body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            left, columns=("kind", "entries"), show="tree headings", height=12
        )
        self.tree.heading("#0", text="文件")
        self.tree.heading("kind", text="类型")
        self.tree.heading("entries", text="条目")
        self.tree.column("#0", width=190)
        self.tree.column("kind", width=70, anchor="center")
        self.tree.column("entries", width=50, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="新建词表", command=lambda: self._create("wordlist")).pack(
            side=tk.LEFT
        )
        ttk.Button(buttons, text="新建正则", command=lambda: self._create("regex")).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(buttons, text="删除", command=self._delete).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.editor = tk.Text(right, wrap=tk.NONE, height=14, undo=True)
        self.editor.grid(row=0, column=0, sticky="nsew")
        scroll2 = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.editor.yview)
        scroll2.grid(row=0, column=1, sticky="ns")
        self.editor.configure(yscrollcommand=scroll2.set)
        save_bar = ttk.Frame(right)
        save_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(save_bar, text="保存文件", command=self._save_file).pack(side=tk.LEFT)
        self._file_label = tk.StringVar(value="未选择文件")
        ttk.Label(save_bar, textvariable=self._file_label, foreground="#8a8a8a").pack(
            side=tk.LEFT, padx=(12, 0)
        )

        scan = ttk.LabelFrame(self.frame, text="本地试扫（不联网、不写审计）", padding=6)
        scan.pack(fill=tk.X, pady=(8, 0))
        scan.columnconfigure(1, weight=1)
        ttk.Label(scan, text="测试文本").grid(row=0, column=0, sticky="nw")
        self._scan_input = tk.Text(scan, wrap=tk.WORD, height=3)
        self._scan_input.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(scan, text="试扫", command=self._scan).grid(row=0, column=2, sticky="n")
        self._scan_result = tk.Text(scan, wrap=tk.WORD, height=6, state=tk.DISABLED)
        self._scan_result.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(scan, textvariable=self._scan_status, foreground="#8a8a8a").grid(
            row=2, column=0, columnspan=3, sticky="w"
        )

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if self.paths is None:
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        word_dir = safetyconfig.wordlists_dir(self.paths, settings)
        sensitive = safetyconfig.sensitive_dir(self.paths, settings)
        self._dirs_label.set(f"词表：{word_dir}    正则：{sensitive}")
        files = safetyconfig.list_wordlist_files(self.paths, settings)
        self._files = {item.path: item for item in files}
        selected = self._current
        self.tree.delete(*self.tree.get_children())
        for item in files:
            self.tree.insert(
                "",
                tk.END,
                iid=item.path,
                text=item.name,
                values=("词表" if item.kind == "wordlist" else "正则", item.entries),
            )
        if selected in self._files:
            self.tree.selection_set(selected)
        elif files:
            self.tree.selection_set(files[0].path)
            self._current = files[0].path
            self._load_editor(files[0].path)
        else:
            self._current = ""
            self._set_editor("")
            self._file_label.set("还没有用户词表；点「新建词表」开始，或先创建 safety.toml")
        if files and self._current in self._files:
            self._load_editor(self._current)

    def _on_select(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = selection[0]
        item = self._files.get(path)
        if item is None:
            return
        self._current = path
        self._load_editor(path)

    def _load_editor(self, path: str) -> None:
        item = self._files.get(path)
        name = item.name if item is not None else path
        self._file_label.set(f"{name}（{item.entries if item else 0} 条）")
        try:
            text = safetyconfig.read_text_file(path)
        except Exception as exc:  # noqa: BLE001 - 读失败展示在编辑框
            text = f"# 读取失败：{exc}\n"
        self._set_editor(text)

    def _set_editor(self, text: str) -> None:
        self.editor.configure(state=tk.NORMAL)
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", text)
        self.editor.edit_reset()

    # -- 按钮 ---------------------------------------------------------------
    def _save_file(self) -> None:
        if not self._current:
            self.tab.notify("先选择一个词表文件（或新建一个）")
            return
        text = self.editor.get("1.0", "end-1c")
        try:
            safetyconfig.write_list_file(self._current, text)
        except Exception as exc:  # noqa: BLE001
            self.tab.fail(exc)
            return
        self.tab.journal(f"内容安全：保存词表文件 {self._current}")
        self.tab.notify("词表已保存；后台 ≤1 秒热加载")
        self.reload()

    def _create(self, kind: str) -> None:
        if self.paths is None:
            return
        from tkinter import simpledialog

        name = simpledialog.askstring(
            "新建文件",
            "文件名（词表无需后缀，自动加 .txt；正则可以写 名字.regex）：",
            parent=self.frame,
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
        suffix = ".txt" if kind == "wordlist" else ".regex"
        if not name.lower().endswith(suffix):
            name += suffix
        settings, _errors = safetyconfig.load_settings(self.paths)
        directory = (
            safetyconfig.wordlists_dir(self.paths, settings)
            if kind == "wordlist"
            else safetyconfig.sensitive_dir(self.paths, settings)
        )
        import os

        try:
            path = safetyconfig.create_list_file(
                directory, name, _WORDLIST_HEADER if kind == "wordlist" else _REGEX_HEADER
            )
        except Exception as exc:  # noqa: BLE001
            self.tab.fail(exc)
            return
        self.tab.journal(f"内容安全：新建词表文件 {path}")
        self.reload()
        if os.path.isfile(path):
            self._current = path
            self.tree.selection_set(path)
            self._load_editor(path)

    def _delete(self) -> None:
        if not self._current:
            return
        item = self._files.get(self._current)
        name = item.name if item is not None else self._current
        if not confirm(self.frame, "删除", f"确定删除 {name} 吗？（会先留一份 .bak 备份）"):
            return
        import os

        try:
            text = safetyconfig.read_text_file(self._current)
            safetyconfig.write_list_file(self._current + ".bak", text)
            os.remove(self._current)
        except OSError as exc:
            self.tab.fail(f"删除失败：{exc}")
            return
        self.tab.journal(f"内容安全：删除词表文件 {self._current}")
        self._current = ""
        self.reload()

    def _open(self, kind: str) -> None:
        if self.paths is None:
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        directory = (
            safetyconfig.wordlists_dir(self.paths, settings)
            if kind == "wordlist"
            else safetyconfig.sensitive_dir(self.paths, settings)
        )
        if not gui_process.open_path(directory):
            self.tab.notify(f"打不开目录：{directory}（可能还不存在）")

    # -- 试扫 ---------------------------------------------------------------
    def _scan(self) -> None:
        if self.paths is None:
            return
        text = self._scan_input.get("1.0", "end-1c")
        settings, errors = safetyconfig.load_settings(self.paths)
        if errors:
            self.tab.notify("当前 safety.toml 有错误：" + "；".join(errors))
        result = safetyconfig.local_scan(self.paths, settings, text)
        lines: list[str] = []
        if not result.enabled:
            lines.append("本地规则层未启用（[local].enabled = false 或 mode = off）。")
        for hit in result.hits:
            lines.append(
                f"命中：{hit['label']}（{hit['category']}）· 动作 {hit['action']} · "
                f"严重度 {hit['severity']} · 片段 {hit['match']!r}"
            )
        if not result.hits:
            lines.append("没有命中。")
        if result.text != text:
            lines.append(f"脱敏后文本：{result.text[:300]}")
        for warning in result.warnings:
            lines.append(f"提示：{warning}")
        self._scan_result.configure(state=tk.NORMAL)
        self._scan_result.delete("1.0", tk.END)
        self._scan_result.insert("1.0", "\n".join(lines))
        self._scan_result.configure(state=tk.DISABLED)
        self._scan_status.set(
            f"模式 {result.mode} · 命中 {len(result.hits)} 处 · 判定 "
            + ("拦截" if result.blocked else "放行/脱敏")
        )
