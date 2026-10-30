"""「提示词」选项卡的模板编辑面板：左分类列表 + 右正文编辑器 + 只读预览。

编辑语义：

* 内置条目改过就写进 ``<配置目录>/gui/prompts.json`` 的"覆盖"区；没改过的
  不写，升级后能自动拿到新的内置文本；
* 「恢复默认」只是把编辑器里的内容换回内置文本，仍然要点「保存」才落盘；
* 自建条目可以勾选「注入系统提示词」，保存后作为独立片段追加到每轮系统提示词；
* 保存后下一轮对话生效（后台按文件指纹热加载），不需要重启。
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any

from atri_x.promptlib import (
    CATEGORIES,
    CATEGORY_LABELS,
    DEFAULT_BY_ID,
    PromptDataError,
    PromptEntry,
    PromptLibrary,
    PromptStore,
    normalize_entry,
    prompt_from_dict,
    prompt_to_dict,
    render_template,
)
from atri_x.promptlib.render import template_variables

__all__ = ["PromptTemplatesPanel"]

#: 预览用的示例变量值（内置模板里声明过的变量都覆盖到）。
SAMPLE_VALUES: dict[str, str] = {
    "platform": "qqonebot",
    "scope_id": "group:123456789",
    "user_id": "10001",
    "speaking_name": "小梅",
    "nickname_line": "昵称：小梅酱",
    "tags_line": "标签：傲娇（嘴硬心软）",
    "profile_body": "角色资料：\n性格直率，喜欢猫。",
    "character_block": "[角色设定]\nid: demo\nname: 示例角色",
    "emotions": "无语、开心、得意",
}

#: 新建条目的 id 前缀。
CUSTOM_PREFIX = "custom."


class PromptTemplatesPanel:
    """模板面板（不是独立选项卡，挂在「提示词」页的子选项卡里）。"""

    def __init__(self, parent: Any, paths: Any, controller: Any) -> None:
        self.paths = paths
        self.controller = controller
        self.frame = ttk.Frame(parent, padding=(8, 6))
        self._library = PromptLibrary(
            PromptStore(paths.config_dir) if paths is not None else None
        )
        self._entries: dict[str, PromptEntry] = {}
        self._order: list[str] = []
        self._current_id = ""
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.frame.columnconfigure(0, weight=0)
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(0, weight=1)

        left = ttk.Frame(self.frame)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", height=18)
        self.tree.grid(row=0, column=0, sticky="ns")
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_select())

        actions = ttk.Frame(left)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(actions, text="新建", command=self._new_entry).pack(side=tk.LEFT)
        ttk.Button(actions, text="删除", command=self._delete_entry).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(actions, text="恢复默认", command=self._reset_entry).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        right = ttk.Frame(self.frame)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(5, weight=1)

        ttk.Label(right, text="标题").grid(row=0, column=0, sticky="w", pady=2)
        self.title_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.title_var).grid(
            row=0, column=1, sticky="ew", pady=2
        )
        ttk.Label(right, text="分类").grid(row=1, column=0, sticky="w", pady=2)
        self.category_var = tk.StringVar()
        self.category_box = ttk.Combobox(
            right,
            textvariable=self.category_var,
            values=[CATEGORY_LABELS[name] for name in CATEGORIES],
            state="readonly",
            width=16,
        )
        self.category_box.grid(row=1, column=1, sticky="w", pady=2)

        self.meta_label = ttk.Label(right, text="", foreground="#8a8a8a")
        self.meta_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.inject_var = tk.BooleanVar(value=False)
        self.inject_check = ttk.Checkbutton(
            right,
            text="注入系统提示词（勾选后每轮追加；只建议用于自建条目）",
            variable=self.inject_var,
        )
        self.inject_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Label(right, text="正文").grid(row=4, column=0, sticky="nw", pady=2)
        self.text = tk.Text(right, wrap=tk.WORD, font=("Microsoft YaHei UI", 9), height=14)
        self.text.grid(row=5, column=1, sticky="nsew", pady=2)
        text_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.text.yview)
        text_scroll.grid(row=5, column=2, sticky="ns")
        self.text.configure(yscrollcommand=text_scroll.set)

        preview_box = ttk.LabelFrame(right, text="预览（示例变量；不会发给模型）", padding=4)
        preview_box.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        preview_box.columnconfigure(0, weight=1)
        self.preview = tk.Text(
            preview_box,
            wrap=tk.WORD,
            height=6,
            font=("Microsoft YaHei UI", 9),
            background="#f5f5f5",
        )
        self.preview.grid(row=0, column=0, sticky="ew")
        self.preview.configure(state=tk.DISABLED)
        ttk.Button(preview_box, text="刷新预览", command=self._refresh_preview).grid(
            row=0, column=1, sticky="n", padx=(6, 0)
        )

        bottom = ttk.Frame(self.frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="保存提示词库", command=self.save).pack(side=tk.LEFT)
        ttk.Button(bottom, text="重新载入", command=self.reload).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(bottom, text="导入…", command=self._import).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(bottom, text="导出…", command=self._export).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    # -- 读 ---------------------------------------------------------------
    def reload(self) -> None:
        """从磁盘重读覆盖（丢弃未保存编辑）。"""
        if self.paths is None:
            self._notify("找不到配置文件，无法读取提示词库")
            return
        try:
            entries = self._library.entries()
        except PromptDataError as exc:
            self._fail(exc)
            return
        self._entries = {entry.id: entry for entry in entries}
        self._order = [entry.id for entry in entries]
        self._fill_tree()
        if self._order:
            self._select(self._order[0])

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for category in CATEGORIES:
            children = [
                entry
                for entry in self._entries.values()
                if entry.category == category
            ]
            if not children:
                continue
            node = self.tree.insert(
                "", tk.END, iid="cat:" + category, text=CATEGORY_LABELS[category], open=True
            )
            for entry in children:
                if entry.builtin:
                    label = entry.title
                else:
                    label = entry.title + "（自建）"
                self.tree.insert(node, tk.END, iid="entry:" + entry.id, text=label)

    # -- 选 ---------------------------------------------------------------
    def _on_select(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        iid = selected[0]
        if not iid.startswith("entry:"):
            return
        self._capture()
        self._select(iid[len("entry:"):])

    def _select(self, prompt_id: str) -> None:
        entry = self._entries.get(prompt_id)
        if entry is None:
            return
        self._current_id = prompt_id
        self.title_var.set(entry.title)
        self.category_var.set(CATEGORY_LABELS.get(entry.category, entry.category))
        variables = entry.variables or template_variables(entry.content)
        meta = "内置模板 · 变量：" + ("、".join(variables) if variables else "无")
        if not entry.builtin:
            meta = "自建条目 · 变量：" + ("、".join(variables) if variables else "无")
        if entry.description:
            meta += " · " + entry.description
        self.meta_label.configure(text=meta)
        self.inject_var.set(bool(entry.inject))
        self.inject_check.configure(state=tk.NORMAL if not entry.builtin else tk.DISABLED)
        if entry.builtin:
            self.inject_var.set(False)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", entry.content)
        self._refresh_preview()
        # 同步左侧选中项（程序化选择时不触发死循环：_on_select 会再 _select 同一 id）。
        iid = "entry:" + prompt_id
        if self.tree.exists(iid) and iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _capture(self) -> None:
        """把编辑器内容写回内存中的当前条目（不落盘）。"""
        entry = self._entries.get(self._current_id)
        if entry is None:
            return
        entry.title = self.title_var.get().strip() or entry.id
        label = self.category_var.get().strip()
        for name in CATEGORIES:
            if CATEGORY_LABELS[name] == label:
                entry.category = name
                break
        entry.content = self.text.get("1.0", "end-1c")
        entry.variables = template_variables(entry.content)
        if not entry.builtin:
            entry.inject = bool(self.inject_var.get())
        else:
            entry.inject = False

    # -- 操作 -------------------------------------------------------------
    def _new_entry(self) -> None:
        self._capture()
        used = set(self._entries)
        index = 1
        while f"{CUSTOM_PREFIX}{index}" in used:
            index += 1
        entry = PromptEntry(
            id=f"{CUSTOM_PREFIX}{index}",
            title=f"自定义提示词 {index}",
            category="custom",
            content="",
            inject=True,
        )
        self._entries[entry.id] = entry
        self._order.append(entry.id)
        self._fill_tree()
        self._select(entry.id)
        self._notify("已新建条目；填好正文后点「保存提示词库」")

    def _delete_entry(self) -> None:
        self._capture()
        entry = self._entries.get(self._current_id)
        if entry is None:
            return
        if entry.builtin:
            self._notify("内置模板不能删除；可以用「恢复默认」还原内容")
            return
        if not self.controller_confirm("删除自建提示词？" + entry.title):
            return
        self._entries.pop(entry.id, None)
        self._order = [item for item in self._order if item != entry.id]
        self._current_id = ""
        self._fill_tree()
        if self._order:
            self._select(self._order[0])
        self._notify("已删除；点「保存提示词库」后落盘")

    def _reset_entry(self) -> None:
        entry = self._entries.get(self._current_id)
        if entry is None:
            return
        default = DEFAULT_BY_ID.get(entry.id)
        if default is None:
            self._notify("只有内置模板才能恢复默认")
            return
        from dataclasses import replace

        self._entries[entry.id] = replace(default, builtin=True)
        self._select(entry.id)
        self._notify("已恢复内置文本；点「保存提示词库」后落盘")

    def _refresh_preview(self) -> None:
        entry = self._entries.get(self._current_id)
        content = self.text.get("1.0", "end-1c") if entry is not None else ""
        try:
            rendered = render_template(content, dict(SAMPLE_VALUES))
        except Exception as exc:  # noqa: BLE001 - 预览失败不该弹窗
            rendered = f"（渲染失败：{exc}）"
        self.preview.configure(state=tk.NORMAL)
        self.preview.delete("1.0", tk.END)
        self.preview.insert("1.0", rendered)
        self.preview.configure(state=tk.DISABLED)

    def save(self) -> None:
        if self.paths is None:
            self._fail("找不到配置文件，无法保存")
            return
        self._capture()
        entries: list[PromptEntry] = []
        for prompt_id in self._order:
            entry = self._entries.get(prompt_id)
            if entry is None:
                continue
            try:
                entries.append(normalize_entry(entry))
            except PromptDataError as exc:
                self._fail(exc)
                return
        try:
            path = self._library.save(entries)
        except PromptDataError as exc:
            self._fail(exc)
            return
        self._journal(f"保存提示词库（{len(entries)} 条）：{path}")
        self._notify("已保存；下一轮对话生效（无需重启）")

    # -- 导入 / 导出 -------------------------------------------------------
    def _export(self) -> None:
        self._capture()
        if self.paths is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self.frame,
            title="导出提示词库",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="prompts.json",
        )
        if not path:
            return
        payload = {
            "version": 1,
            "prompts": [prompt_to_dict(entry) for entry in self._entries.values()],
        }
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except OSError as exc:
            self._fail(f"导出失败：{exc}")
            return
        self._journal(f"导出提示词库：{path}")
        self._notify("已导出：" + path)

    def _import(self) -> None:
        if self.paths is None:
            return
        path = filedialog.askopenfilename(
            parent=self.frame,
            title="导入提示词库",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            self._fail(f"读取失败：{exc}")
            return
        raw = data.get("prompts") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            self._fail("文件结构不对：需要 {\"prompts\": [...]} 或条目数组")
            return
        imported = 0
        for item in raw:
            entry = prompt_from_dict(item)
            if entry is None:
                continue
            if entry.id in DEFAULT_BY_ID:
                continue  # 内置 id 不允许从外部覆盖，避免导入文件顶掉模板
            self._entries[entry.id] = entry
            if entry.id not in self._order:
                self._order.append(entry.id)
            imported += 1
        self._fill_tree()
        self._notify(f"已导入 {imported} 条自建条目；点「保存提示词库」后落盘")

    # -- 控制器转发 --------------------------------------------------------
    def controller_confirm(self, text: str) -> bool:
        return bool(self.controller.confirm(text))

    def _notify(self, text: str) -> None:
        self.controller.notify(text)

    def _fail(self, exc: Any) -> None:
        self.controller.fail(exc)

    def _journal(self, text: str) -> None:
        self.controller.journal(text)
