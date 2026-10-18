"""「知识库」选项卡：群知识库 / FAQ 的全局配置 + 现有条目查看。

两块内容：

- **全局配置**（可编辑）：``app.toml`` 的 ``[group_knowledge]`` —— 总开关、每群条数上限、
  问题/答案/别名/关键词/匹配扫描的字符上限。保存后重启后台服务生效。
- **现有条目**（只读）：``state.toml`` 的 ``[group_knowledge]``，按「平台 + 群 scope」
  分组。这个文件由后台服务运行期回写（并 15 秒热加载一次外部修改），界面只读展示，
  避免和进程回写打架；增删改用群里的 ``/faq`` 命令。

条目命中后由服务端直接本地回答，不创建 Session、不调用 LLM，也不进模型提示词。
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml
from atri_x.gui.process import open_path
from atri_x.gui.tabs.base import ConfigTab, message

__all__ = ["KnowledgeTab", "load_entries"]

#: 全局上限项：(配置键, 界面名)。
_LIMIT_FIELDS = (
    ("max_entries_per_scope", "每群条数上限"),
    ("max_question_runes", "问题最大字符数"),
    ("max_answer_runes", "答案最大字符数"),
    ("max_aliases", "每条别名数上限"),
    ("max_keywords", "每条关键词数上限"),
    ("max_match_runes", "匹配扫描字符数"),
)

#: 匹配模式 → 界面名。
_MATCH_LABELS = {"exact": "精确匹配", "contains": "包含匹配", "keywords": "关键词匹配"}


class KnowledgeTab(ConfigTab):
    """群知识库配置 + 条目查看。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._rows: list[tuple[str, dict[str, Any]]] = []
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        box = ttk.LabelFrame(self.body, text="全局配置（app.toml [group_knowledge]）", padding=8)
        box.pack(fill=tk.X)
        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box, text="启用本地确定性知识库（命中后直接回答，不调用模型）", variable=self.enabled_var
        ).grid(row=0, column=0, columnspan=4, sticky="w")

        self.limit_vars: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate(_LIMIT_FIELDS):
            row, column = divmod(index, 3)
            cell = ttk.Frame(box)
            cell.grid(row=row + 1, column=column, sticky="w", padx=(0, 18), pady=2)
            ttk.Label(cell, text=label).pack(side=tk.LEFT)
            variable = tk.StringVar()
            self.limit_vars[key] = variable
            ttk.Entry(cell, textvariable=variable, width=8).pack(side=tk.LEFT, padx=(6, 0))

        entries_box = ttk.LabelFrame(self.body, text="现有知识条目（state.toml，只读）", padding=8)
        entries_box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        bar = ttk.Frame(entries_box)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="筛选").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        keyword = ttk.Entry(bar, textvariable=self.filter_var, width=24)
        keyword.pack(side=tk.LEFT, padx=(6, 8))
        keyword.bind("<Return>", lambda _event: self._refresh_entries())
        ttk.Button(bar, text="应用筛选", command=self._refresh_entries).pack(side=tk.LEFT)
        self.info_label = ttk.Label(bar, text="", foreground="#555555")
        self.info_label.pack(side=tk.LEFT, padx=(12, 0))

        table = ttk.Frame(entries_box)
        table.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        columns = ("scope", "question", "answer", "match", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=9)
        for name, title, width in (
            ("scope", "群组", 170),
            ("question", "问题 / 触发词", 220),
            ("answer", "答案", 260),
            ("match", "匹配", 80),
            ("status", "状态", 60),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, stretch=name in ("question", "answer"))
        yscroll = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<Double-Button-1>", lambda _event: self._show_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected())
        self.tree.tag_configure("off", foreground="#8a8a8a")

        self.detail = tk.Text(entries_box, height=5, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self.detail.pack(fill=tk.X, pady=(6, 0))
        self.detail.configure(state=tk.DISABLED)

        ttk.Label(
            self.body,
            text=(
                "条目的增删改用群里的 /faq 命令（/faq add、/faq list、/faq clear 等）；"
                "界面只读展示，避免和后台服务的 state.toml 回写互相覆盖。"
            ),
            foreground="#8a8a8a",
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

        self.button("保存全局设置", self._save)
        self.button("重新载入", self.reload)
        self.button("刷新知识条目", self._refresh_entries)
        self.button("打开 state.toml", self._open_state_file)
        self.button("打开配置目录", self._open_config_dir)

    # -- 数据 ---------------------------------------------------------------
    def on_show(self) -> None:
        """只刷只读条目，不动用户可能还没保存的全局设置。"""
        self._refresh_entries()

    def reload(self) -> None:
        if not self.is_ready():
            return
        try:
            data = read_toml(self.require_paths().config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        section = data.get("group_knowledge")
        section = section if isinstance(section, dict) else {}
        enabled = section.get("enabled")
        self.enabled_var.set(True if enabled is None else bool(enabled))
        for key, _label in _LIMIT_FIELDS:
            value = section.get(key)
            self.limit_vars[key].set("" if value in (None, "") else str(value))
        self._refresh_entries()
        self.notify("已载入知识库配置（全局设置改动需要重启后台服务）")

    def _refresh_entries(self) -> None:
        if not self.is_ready():
            self.tree.delete(*self.tree.get_children(""))
            return
        paths = self.require_paths()
        try:
            all_entries = load_entries(paths.state_path)
        except ConfigEditError as exc:
            self.info_label.configure(text=f"读取 state.toml 失败：{exc}")
            self.tree.delete(*self.tree.get_children(""))
            return
        needle = self.filter_var.get().strip().casefold()
        entries = [item for item in all_entries if _matches(item, needle)] if needle else list(all_entries)
        self._rows = entries
        self.tree.delete(*self.tree.get_children(""))
        for index, (scope, entry) in enumerate(entries):
            enabled = entry.get("enabled")
            status = "启用" if enabled is None or bool(enabled) else "停用"
            match = _MATCH_LABELS.get(str(entry.get("match") or "").strip(), "精确匹配")
            question = str(entry.get("question") or "").replace("\n", " ")
            answer = str(entry.get("answer") or "").replace("\n", " ")
            self.tree.insert(
                "",
                tk.END,
                iid=f"entry-{index}",
                values=(
                    scope,
                    _clip(question, 60),
                    _clip(answer, 90),
                    match,
                    status,
                ),
                tags=() if status == "启用" else ("off",),
            )
        total = len(all_entries)
        scopes = len({scope for scope, _entry in entries})
        self.info_label.configure(
            text=f"{total} 条 / {scopes} 个群组" + ("（已筛选）" if needle else "")
        )
        self.notify(f"已载入 {len(entries)} 条知识条目" + ("（筛选后）" if needle else ""))

    def _show_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(str(selection[0]).rsplit("-", 1)[-1])
        if not (0 <= index < len(self._rows)):
            return
        scope, entry = self._rows[index]
        aliases = entry.get("aliases")
        keywords = entry.get("keywords")
        lines = [
            f"群组：{scope}",
            f"id：{entry.get('id') or '（无）'}",
            f"问题：{entry.get('question') or ''}",
            f"答案：{entry.get('answer') or ''}",
            f"匹配：{_MATCH_LABELS.get(str(entry.get('match') or '').strip(), '精确匹配')}",
            f"别名：{', '.join(str(item) for item in aliases) if isinstance(aliases, list) else '（无）'}",
            f"关键词：{', '.join(str(item) for item in keywords) if isinstance(keywords, list) else '（无）'}",
            f"更新时间：{entry.get('updated_at') or '（无）'}",
        ]
        self.detail.configure(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.insert("1.0", "\n".join(lines))
        self.detail.configure(state=tk.DISABLED)

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        try:
            values: dict[str, Any] = {"enabled": bool(self.enabled_var.get())}
            for key, label in _LIMIT_FIELDS:
                raw = self.limit_vars[key].get().strip()
                if not raw:
                    values[key] = None
                    continue
                try:
                    number = int(raw)
                except ValueError as exc:
                    raise ConfigEditError(f"{label}必须是整数：{raw!r}") from exc
                if number <= 0:
                    raise ConfigEditError(f"{label}必须是正整数：{number}")
                values[key] = number
            patch_section(self.require_paths().config_path, "group_knowledge", values)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal("保存知识库全局设置（[group_knowledge]）")
        self.notify("已保存 [group_knowledge]；重启后台服务后生效")

    # -- 打开文件/目录 -------------------------------------------------------
    def _open_state_file(self) -> None:
        path = self.require_paths().state_path
        if not os.path.exists(path):
            message(self.frame, "state.toml", f"还没有 {path}（后台服务启动后会生成）")
            return
        if not open_path(path):
            message(self.frame, "state.toml", f"文件：{path}")

    def _open_config_dir(self) -> None:
        directory = self.require_paths().config_dir
        if not open_path(directory):
            message(self.frame, "配置目录", f"打不开目录：{directory}")


# ---------------------------------------------------------------------------
# 读取 state.toml 里的条目
# ---------------------------------------------------------------------------
def load_entries(state_path: str) -> list[tuple[str, dict[str, Any]]]:
    """读 ``state.toml`` 的 ``[group_knowledge]``，返回 ``[(群组, 条目), ...]``。

    兼容两种写法：标准 TOML 的数组表（``[[group_knowledge.qqonebot:g1]]``）与
    Go 侧可能的 JSON 字符串值。scope 顺序保持文件里的顺序，条目内也保持原顺序。
    """
    if not state_path or not os.path.exists(state_path):
        return []
    data = read_toml(state_path)
    raw = data.get("group_knowledge")
    if not isinstance(raw, dict):
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for scope, values in raw.items():
        items = values
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except ValueError:
                items = []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                entries.append((str(scope), dict(item)))
    return entries


def _matches(item: tuple[str, dict[str, Any]], needle: str) -> bool:
    scope, entry = item
    parts = [scope, str(entry.get("question") or ""), str(entry.get("answer") or "")]
    for key in ("aliases", "keywords"):
        value = entry.get(key)
        if isinstance(value, list):
            parts.extend(str(element) for element in value)
    return any(needle in part.casefold() for part in parts)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
