"""「提示词」页的「工具标签」子页：编辑独立配置文件 ``tool_tags.toml``。

``tool_tags.toml`` 把「一组工具 + 一段提示词」打包成 tag：聊天里发
``@tool:<tag>`` / ``@t@skill:<tag>`` 命中后，这组工具被预载、``prompt`` 追加到
系统提示词。以前只能手写 TOML（多行字符串尤其难写），本面板提供列表 + 表单。

生效：该文件由后台按 ``(size, mtime)`` 热加载，保存后**下一轮生效**，不需要重启。
"""

from __future__ import annotations

import re
import tkinter as tk
import tomllib
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import ConfigEditError, read_toml, toml_value, write_text_file

__all__ = ["ToolTagsPanel"]

#: tag 名允许的字符（与后台 ``normalize_tool_tag`` 一致）。
_TAG_PATTERN = re.compile(r"^[a-z0-9_.-]{1,60}$")

_HEADER = (
    "# tool_tags.toml —— 由桌面端「提示词 → 工具标签」维护。\n"
    "# tag 激活方式：@tool:<tag> / @t:<tag>；prompt 只在激活后追加到 system prompt。\n"
)


class ToolTagsPanel:
    """工具标签的增删改（tool_tags.toml）。"""

    def __init__(self, parent: Any, paths: Any, controller: Any) -> None:
        self.paths = paths
        self.controller = controller
        self.frame = ttk.Frame(parent, padding=(8, 6))
        self._tags: dict[str, dict[str, Any]] = {}
        self._current = ""
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(0, weight=1)
        left = ttk.Frame(self.frame)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", height=16)
        self.tree.grid(row=0, column=0, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_select())
        actions = ttk.Frame(left)
        actions.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(actions, text="新增标签", command=self._new_tag).pack(side=tk.LEFT)
        ttk.Button(actions, text="删除标签", command=self._delete_tag).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        right = ttk.Frame(self.frame)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(2, weight=1)
        ttk.Label(
            right,
            text=(
                "tag 名只允许小写字母、数字、_ - .；工具名用逗号分隔。"
                "保存后下一轮对话生效（后台热加载，无需重启）。"
            ),
            foreground="#8a8a8a",
            wraplength=780,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(right, text="tag 名").grid(row=1, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.name_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Label(right, text="工具（逗号分隔）").grid(row=2, column=0, sticky="nw", pady=2)
        self.tools_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.tools_var).grid(
            row=2, column=1, sticky="new", pady=2
        )
        ttk.Label(right, text="提示词").grid(row=3, column=0, sticky="nw", pady=2)
        self.text = tk.Text(right, wrap=tk.WORD, height=12, font=("Microsoft YaHei UI", 9))
        self.text.grid(row=3, column=1, sticky="nsew", pady=2)
        right.rowconfigure(3, weight=1)

        bottom = ttk.Frame(self.frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="保存工具标签", command=self.save).pack(side=tk.LEFT)
        ttk.Button(bottom, text="重新载入", command=self.reload).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    # -- 读 ---------------------------------------------------------------
    def reload(self) -> None:
        if self.paths is None or not self.paths.tool_tags_path:
            self._notify("找不到 tool_tags.toml 路径，无法读取")
            return
        try:
            data = read_toml(self.paths.tool_tags_path)
        except ConfigEditError as exc:
            self._fail(exc)
            return
        tags = data.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        self._tags = {
            str(name): {
                "tools": [str(item) for item in (value.get("tools") or [])]
                if isinstance(value.get("tools"), list)
                else [],
                "prompt": str(value.get("prompt") or ""),
            }
            for name, value in tags.items()
            if isinstance(value, dict)
        }
        self._refill()
        if self._tags:
            self._select(next(iter(self._tags)))
        else:
            self._current = ""
            self.name_var.set("")
            self.tools_var.set("")
            self.text.delete("1.0", tk.END)

    def _refill(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for name in self._tags:
            self.tree.insert("", tk.END, iid=name, text=name)

    def _on_select(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self._capture()
        self._select(selected[0])

    def _select(self, name: str) -> None:
        entry = self._tags.get(name)
        if entry is None:
            return
        self._current = name
        self.name_var.set(name)
        self.tools_var.set(", ".join(entry.get("tools") or []))
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", str(entry.get("prompt") or ""))

    def _capture(self) -> None:
        name = self._current
        if not name or name not in self._tags:
            return
        entry = self._tags[name]
        entry["tools"] = [
            item.strip()
            for item in self.tools_var.get().replace("，", ",").split(",")
            if item.strip()
        ]
        entry["prompt"] = self.text.get("1.0", "end-1c")

    # -- 操作 -------------------------------------------------------------
    def _new_tag(self) -> None:
        self._capture()
        index = 1
        while f"tag{index}" in self._tags:
            index += 1
        from tkinter import simpledialog

        name = simpledialog.askstring(
            "新增工具标签",
            "tag 名（小写字母/数字/_/-/.）：",
            initialvalue=f"tag{index}",
            parent=self.frame,
        )
        if not name:
            return
        name = name.strip()
        if not _TAG_PATTERN.match(name):
            self._fail("tag 名只允许小写字母、数字、_ - .（1–60 个字符）")
            return
        self._tags[name] = {"tools": [], "prompt": ""}
        self._refill()
        self._select(name)
        self._notify("已新增标签；填好内容后点「保存工具标签」")

    def _delete_tag(self) -> None:
        self._capture()
        name = self._current
        if not name or not self.controller.confirm(f"删除工具标签 {name}？"):
            return
        self._tags.pop(name, None)
        self._current = ""
        self._refill()
        if self._tags:
            self._select(next(iter(self._tags)))
        else:
            self.name_var.set("")
            self.tools_var.set("")
            self.text.delete("1.0", tk.END)
        self._notify("已删除；点「保存工具标签」后落盘")

    def save(self) -> None:
        if self.paths is None or not self.paths.tool_tags_path:
            self._fail("找不到 tool_tags.toml 路径，无法保存")
            return
        self._capture()
        lines = _HEADER.splitlines()
        lines.append("")
        for name, entry in self._tags.items():
            if not _TAG_PATTERN.match(name):
                self._fail(f"tag 名不合法：{name!r}")
                return
            lines.append("[tags." + name + "]")
            tools = list(entry.get("tools") or [])
            if tools:
                lines.append("tools = " + toml_value(tools))
            prompt = str(entry.get("prompt") or "")
            if prompt.strip():
                lines.append("prompt = " + toml_value(prompt))
            lines.append("")
        text = "\n".join(lines).rstrip("\n") + "\n"
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - 理论上不可达
            self._fail(f"生成的 TOML 不合法：{exc}")
            return
        try:
            write_text_file(self.paths.tool_tags_path, text)
        except ConfigEditError as exc:
            self._fail(exc)
            return
        self._journal("保存工具标签（tool_tags.toml）")
        self._notify("已保存；下一轮对话生效")

    # -- 控制器转发 --------------------------------------------------------
    def _notify(self, text: str) -> None:
        self.controller.notify(text)

    def _fail(self, exc: Any) -> None:
        self.controller.fail(exc)

    def _journal(self, text: str) -> None:
        self.controller.journal(text)
