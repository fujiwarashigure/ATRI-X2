"""「提示词」页的「标签库」子页 + 群友/人设页共用的标签选择对话框。

标签库给标签一份可复用的词表：名字 / 说明 / 别名 / 分组。群友档案与专属人设的
标签注入提示词时，命中词表的标签会渲染成 ``名字（说明）``，模型不用猜含义；
没登记的标签仍按原样输出（兼容旧档案）。

存储：``<配置目录>/gui/tags.json``；后台按文件指纹热加载，保存后下一轮生效。
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Any

from atri_x.promptlib import TagCatalog, TagDataError, TagEntry

__all__ = [
    "TagLibraryPanel",
    "TagPickerDialog",
    "apply_tag_picker",
    "choose_tags",
    "merge_tag_list",
]


class TagLibraryPanel:
    """标签词表的增删改。"""

    def __init__(self, parent: Any, paths: Any, controller: Any) -> None:
        self.paths = paths
        self.controller = controller
        self.frame = ttk.Frame(parent, padding=(8, 6))
        self._catalog = TagCatalog(paths.config_dir) if paths is not None else None
        self._tags: dict[str, TagEntry] = {}
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
        ttk.Label(
            right,
            text=(
                "标签说明会随群友档案一起注入提示词（渲染成「标签（说明）」）；"
                "别名只是查找用，不参与注入。保存后下一轮对话生效。"
            ),
            foreground="#8a8a8a",
            wraplength=780,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(right, text="标签名").grid(row=1, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.name_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Label(right, text="分组").grid(row=2, column=0, sticky="w", pady=2)
        self.group_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.group_var).grid(
            row=2, column=1, sticky="ew", pady=2
        )
        ttk.Label(right, text="别名（逗号分隔）").grid(row=3, column=0, sticky="w", pady=2)
        self.aliases_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.aliases_var).grid(
            row=3, column=1, sticky="ew", pady=2
        )
        ttk.Label(right, text="说明").grid(row=4, column=0, sticky="nw", pady=2)
        self.text = tk.Text(right, wrap=tk.WORD, height=6, font=("Microsoft YaHei UI", 9))
        self.text.grid(row=4, column=1, sticky="nsew", pady=2)
        right.rowconfigure(4, weight=1)

        bottom = ttk.Frame(self.frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="保存标签库", command=self.save).pack(side=tk.LEFT)
        ttk.Button(bottom, text="重新载入", command=self.reload).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    # -- 读 ---------------------------------------------------------------
    def reload(self) -> None:
        if self._catalog is None:
            self._notify("找不到配置文件，无法读取标签库")
            return
        self._tags = {entry.name: entry for entry in self._catalog.load()}
        self._refill()
        if self._tags:
            self._select(next(iter(self._tags)))
        else:
            self._current = ""
            self.name_var.set("")
            self.group_var.set("")
            self.aliases_var.set("")
            self.text.delete("1.0", tk.END)

    def _refill(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for entry in self._tags.values():
            label = entry.name
            if entry.group:
                label += f"（{entry.group}）"
            self.tree.insert("", tk.END, iid=entry.name, text=label)

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
        self.name_var.set(entry.name)
        self.group_var.set(entry.group)
        self.aliases_var.set(", ".join(entry.aliases))
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", entry.description)

    def _capture(self) -> None:
        name = self._current
        if not name or name not in self._tags:
            return
        entry = self._tags[name]
        entry.group = self.group_var.get().strip()
        entry.aliases = [
            item.strip()
            for item in self.aliases_var.get().replace("，", ",").split(",")
            if item.strip()
        ]
        entry.description = self.text.get("1.0", "end-1c").strip()

    # -- 操作 -------------------------------------------------------------
    def _new_tag(self) -> None:
        self._capture()
        name = simpledialog.askstring(
            "新增标签", "标签名：", parent=self.frame
        )
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self._tags:
            self._fail(f"标签已存在：{name}")
            return
        self._tags[name] = TagEntry(name=name)
        self._refill()
        self._select(name)
        self._notify("已新增标签；填好说明后点「保存标签库」")

    def _delete_tag(self) -> None:
        self._capture()
        name = self._current
        if not name or not self.controller.confirm(f"删除标签 {name}？"):
            return
        self._tags.pop(name, None)
        self._current = ""
        self._refill()
        if self._tags:
            self._select(next(iter(self._tags)))
        else:
            self.name_var.set("")
            self.group_var.set("")
            self.aliases_var.set("")
            self.text.delete("1.0", tk.END)
        self._notify("已删除；点「保存标签库」后落盘")

    def save(self) -> None:
        if self._catalog is None:
            self._fail("找不到配置文件，无法保存")
            return
        self._capture()
        try:
            self._catalog.save(list(self._tags.values()))
        except TagDataError as exc:
            self._fail(exc)
            return
        self._journal("保存标签库（gui/tags.json）")
        self._notify("已保存；下一轮对话生效")

    # -- 控制器转发 --------------------------------------------------------
    def _notify(self, text: str) -> None:
        self.controller.notify(text)

    def _fail(self, exc: Any) -> None:
        self.controller.fail(exc)

    def _journal(self, text: str) -> None:
        self.controller.journal(text)


class TagPickerDialog:
    """多选标签的模态对话框（群友 / 人设卡页的「标签库…」按钮用）。"""

    def __init__(self, parent: Any, catalog: TagCatalog, selected: list[str]) -> None:
        self.catalog = catalog
        self.result: list[str] | None = None
        self._all = catalog.load()
        self._names = [entry.name for entry in self._all]
        self._selected = [str(item).strip() for item in selected if str(item).strip()]
        self._window = tk.Toplevel(parent)
        self._window.title("从标签库选择")
        self._window.transient(parent)
        self._window.grab_set()
        self._window.geometry("520x460")
        self._build()
        self._window.wait_window()

    def _build(self) -> None:
        window = self._window
        top = ttk.Frame(window, padding=(8, 6))
        top.pack(fill=tk.BOTH, expand=True)
        ttk.Label(top, text="搜索").pack(anchor="w")
        self.search_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.search_var)
        entry.pack(fill=tk.X, pady=(2, 6))
        entry.bind("<KeyRelease>", lambda _event: self._refill())

        self.listbox = tk.Listbox(top, selectmode=tk.EXTENDED, font=("Microsoft YaHei UI", 9))
        self.listbox.pack(fill=tk.BOTH, expand=True)
        hint = "（标签库为空：先去「提示词 → 标签库」新增词条；这里的多选只做勾选，不改词表）"
        ttk.Label(top, text=hint, foreground="#8a8a8a", wraplength=480).pack(
            anchor="w", pady=(4, 0)
        )

        bottom = ttk.Frame(window, padding=(8, 6))
        bottom.pack(fill=tk.X)
        ttk.Button(bottom, text="确定", command=self._confirm).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="取消", command=self._cancel).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self._refill()

    def _refill(self) -> None:
        keyword = self.search_var.get().strip().lower()
        self._visible: list[str] = []
        self.listbox.delete(0, tk.END)
        for entry in self._all:
            haystack = " ".join([entry.name, entry.group, *entry.aliases, entry.description]).lower()
            if keyword and keyword not in haystack:
                continue
            label = entry.name
            if entry.description:
                label += f" — {entry.description}"
            self.listbox.insert(tk.END, label)
            self._visible.append(entry.name)
            if entry.name in self._selected:
                self.listbox.selection_set(tk.END)
        self._visible_names = self._visible

    def _confirm(self) -> None:
        chosen = [self._visible[index] for index in self.listbox.curselection()]
        # 保留用户手写、未登记在词表里的标签（只替换词表内的部分）。
        known = set(self._names)
        missing = [item for item in self._selected if item not in known]
        self.result = chosen + missing
        self._window.destroy()

    def _cancel(self) -> None:
        self.result = None
        self._window.destroy()


def merge_tag_list(current: list[str], picked: list[str]) -> list[str]:
    """把词表里勾选的标签合并进现有标签（去空、去重、保持现有顺序在前）。"""
    merged: list[str] = []
    for tag in [*(current or []), *(picked or [])]:
        text = str(tag or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def choose_tags(parent: Any, config_dir: str, selected: list[str]) -> list[str] | None:
    """打开标签选择对话框；用户取消返回 ``None``。"""
    if not str(config_dir or "").strip():
        return None
    return TagPickerDialog(parent, TagCatalog(config_dir), selected).result


def apply_tag_picker(tab: Any, variable: Any, journal_text: str) -> None:
    """群友 / 人设卡页的「标签库…」按钮：弹选择框并把结果合并回输入框。

    ``tab`` 需要提供 ``paths`` / ``frame`` / ``fail`` / ``journal``（即 ConfigTab 子类）。
    """
    paths = getattr(tab, "paths", None)
    if paths is None:
        tab.fail("找不到配置文件，无法打开标签库")
        return
    current = [
        item.strip()
        for item in re.split(r"[,，、;\s]+", str(variable.get() or ""))
        if item.strip()
    ]
    chosen = choose_tags(tab.frame, paths.config_dir, current)
    if chosen is None:
        return
    variable.set(", ".join(merge_tag_list(current, chosen)))
    tab.journal(journal_text)
