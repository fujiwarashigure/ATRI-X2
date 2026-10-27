"""``[[time.sources]]`` 的表格 + 表单编辑器（只操作内存里的列表，不碰配置文件）。

从「时间」选项卡里拆出来，因为它是一块自洽的控件：左边一张表列出全部来源，右边一组
表单项编辑"所选来源"，另有新增 / 删除。落盘由调用方（:class:`atri_x.gui.tabs.timepanel.TimeTab`）
用 :func:`atri_x.gui.configedit.write_array_tables` 统一做——这样"界面怎么编"和
"数组表怎么写"两件事可以各自单测。

约定：

* 数字字段在编辑时**宽松**（输入到一半的 ``1`` 不会被拒绝），点保存时才由配置层
  :meth:`atri_x.config.time_sections.TimeSourceConfig.normalized` 严格校验；
* 换选中行时先把编辑区写回上一行（不会丢用户刚敲的内容）；
* 程序化改选中（整表重建、新增一行）用一个"正在同步"标记挡住
  ``<<TreeviewSelect>>`` 回声，避免选择事件自激（这是真的踩过的死循环）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x.config.time_sections import TIME_PROTOCOLS
from atri_x.gui import timeview
from atri_x.gui.tabs.base import confirm

__all__ = ["SOURCE_COLUMNS", "SourceEditor"]

#: 只读表格：``[[time.sources]]`` 一览。
SOURCE_COLUMNS = (
    ("id", "id", 140),
    ("protocol", "协议", 55),
    ("address", "地址", 200),
    ("group", "分组", 80),
    ("enabled", "启用", 50),
)

#: 编辑区字段：(配置键, 界面名, 输入框宽度)。逐字段的规则写在编辑区底部那行说明里，
#: 不再挤在输入框右边——侧栏一长串提示会把整个选项卡撑得比窗口还宽。
_EDIT_FIELDS = (
    ("id", "id", 22),
    ("protocol", "协议", 10),
    ("host", "SNTP 主机", 22),
    ("port", "SNTP 端口", 8),
    ("url", "HTTPS 地址", 30),
    ("max_body_bytes", "响应体上限", 10),
    ("group", "分组 group", 14),
    ("timeout_seconds", "单来源超时（秒）", 8),
    ("enabled", "启用该来源", 0),
    ("allow_private", "允许私网 / 回环地址", 0),
    ("require_no_store", "要求 Cache-Control: no-store", 0),
)

#: 编辑区底部的规则说明（把"哪些字段必填 / 范围多少"集中写一行，省横向空间）。
_EDIT_HINT = (
    "id 必填且唯一；sntp 用主机 + 端口，https 用 https:// 开头的 URL（URL 里不能带凭据）；"
    "响应体上限 256–65536；分组不同才算独立证据；单来源超时填 0 = 沿用 "
    "[time.verification].timeout_seconds。与协议无关的字段会被忽略。"
)

#: 布尔字段（渲染成勾选框）。
_FLAG_FIELDS = ("enabled", "allow_private", "require_no_store")

#: 整数字段 → (错误提示名, 缺省值)。
_INT_FIELDS = (("port", "端口", 123), ("max_body_bytes", "响应体上限", 4096))


class SourceEditor(ttk.Frame):
    """一份 ``[[time.sources]]`` 列表的表格 + 表单编辑器。"""

    def __init__(self, parent: Any, *, on_notice: Callable[[str], None] | None = None) -> None:
        super().__init__(parent)
        self._on_notice = on_notice
        self.sources: list[dict[str, Any]] = []
        self.src_vars: dict[str, tk.StringVar] = {}
        self.src_flags: dict[str, tk.BooleanVar] = {}
        self._editing = -1
        self._selected_row = -1
        self._syncing = False
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        left = ttk.Frame(self)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            left, columns=[name for name, _t, _w in SOURCE_COLUMNS], show="headings", height=6
        )
        for name, title, width in SOURCE_COLUMNS:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, stretch=name in ("address", "group"))
        scrollbar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._select())
        self.tree.tag_configure("off", foreground="#8a8a8a")

        bar = ttk.Frame(left)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="新增来源", command=self.new_source).pack(side=tk.LEFT)
        ttk.Button(bar, text="删除所选", command=self.delete_source).pack(side=tk.LEFT, padx=(6, 0))

        editor = ttk.LabelFrame(self, text="所选来源", padding=8)
        editor.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        for index, (key, label, width) in enumerate(_EDIT_FIELDS):
            if key in _FLAG_FIELDS:
                flag = tk.BooleanVar(value=False)
                self.src_flags[key] = flag
                ttk.Checkbutton(editor, text=label, variable=flag).grid(
                    row=index, column=0, columnspan=2, sticky="w", pady=2
                )
                continue
            ttk.Label(editor, text=label).grid(row=index, column=0, sticky="e", padx=(0, 6), pady=2)
            if key == "protocol":
                variable = tk.StringVar(value=TIME_PROTOCOLS[0])
                widget: Any = ttk.Combobox(
                    editor,
                    textvariable=variable,
                    values=list(TIME_PROTOCOLS),
                    state="readonly",
                    width=width,
                )
            else:
                variable = tk.StringVar()
                widget = ttk.Entry(editor, textvariable=variable, width=width)
            self.src_vars[key] = variable
            widget.grid(row=index, column=1, sticky="w", pady=2)
        ttk.Label(
            editor,
            text=_EDIT_HINT,
            foreground="#8a8a8a",
            wraplength=300,
            justify=tk.LEFT,
        ).grid(row=len(_EDIT_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ---------------------------------------------------------------- 公共
    def load(self, sources: list[dict[str, Any]]) -> None:
        """载入一份来源列表（补默认值后整表重建）。"""
        defaults = timeview.source_defaults()
        self.sources = [
            {key: item.get(key, default) for key, default in defaults.items()} for item in sources
        ]
        self._editing = -1
        self._selected_row = -1
        self._refresh_tree()

    def values(self) -> list[dict[str, Any]]:
        """当前列表（先应用编辑区；数字非法时抛 ``ConfigEditError``）。"""
        self._apply_editor(quiet=False)
        return [dict(item) for item in self.sources]

    def count(self) -> int:
        return len(self.sources)

    # ---------------------------------------------------------------- 表格
    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for index, item in enumerate(self.sources):
            self.tree.insert(
                "", tk.END, iid=f"source-{index}", values=_row_values(item),
                tags=() if item.get("enabled") else ("off",),
            )
        if 0 <= self._editing < len(self.sources):
            self._syncing = True
            try:
                self.tree.selection_set(f"source-{self._editing}")
                self.tree.see(f"source-{self._editing}")
            finally:
                self._syncing = False
            self._selected_row = self._editing

    def _update_row(self, index: int) -> None:
        iid = f"source-{index}"
        if not self.tree.exists(iid):
            self._refresh_tree()
            return
        item = self.sources[index]
        self.tree.item(iid, values=_row_values(item), tags=() if item.get("enabled") else ("off",))

    def _select(self) -> None:
        """用户换选中行：先把编辑区写回上一行，再载入新行。"""
        if self._syncing:
            return
        selection = self.tree.selection()
        if not selection:
            return
        index = int(str(selection[0]).rsplit("-", 1)[-1])
        if index == self._selected_row:
            return
        if index != self._editing:
            self._apply_editor(quiet=True)
            self._editing = index
            self._selected_row = index
            self._fill_editor(self.sources[index])
            return
        self._selected_row = index

    # ---------------------------------------------------------------- 编辑
    def _fill_editor(self, item: dict[str, Any]) -> None:
        for key, variable in self.src_vars.items():
            value = item.get(key)
            variable.set("" if value is None else str(value))
        for key, flag in self.src_flags.items():
            flag.set(bool(item.get(key)))

    def _apply_editor(self, *, quiet: bool) -> bool:
        """把编辑区写回当前行；``quiet`` 时非法数字保留原值（编辑中不打断用户）。"""
        if not (0 <= self._editing < len(self.sources)):
            return False
        current = self.sources[self._editing]
        updated = dict(current)
        for key in ("id", "host", "url", "group"):
            if key in self.src_vars:
                updated[key] = self.src_vars[key].get().strip()
        if "protocol" in self.src_vars:
            protocol = self.src_vars["protocol"].get().strip().lower()
            updated["protocol"] = protocol or str(current.get("protocol") or TIME_PROTOCOLS[0])
        for key, label, default in _INT_FIELDS:
            if key in self.src_vars:
                updated[key] = _int_value(
                    self.src_vars[key].get(), label, int(current.get(key) or default), quiet=quiet
                )
        if "timeout_seconds" in self.src_vars:
            updated["timeout_seconds"] = _float_value(
                self.src_vars["timeout_seconds"].get(),
                "单来源超时",
                float(current.get("timeout_seconds") or 0.0),
                quiet=quiet,
            )
        for key, flag in self.src_flags.items():
            updated[key] = bool(flag.get())
        self.sources[self._editing] = updated
        self._update_row(self._editing)
        return True

    # ---------------------------------------------------------------- 动作
    def new_source(self) -> None:
        self._apply_editor(quiet=True)
        self.sources.append(dict(timeview.source_defaults()))
        self._editing = len(self.sources) - 1
        self._selected_row = -1
        self._refresh_tree()
        self._fill_editor(self.sources[self._editing])
        self._notice("新增了一条来源：填好 id 与地址后点「保存到 app.toml」")

    def delete_source(self) -> None:
        if not (0 <= self._editing < len(self.sources)):
            self._notice("先在上面选中一条要删除的来源")
            return
        name = str(self.sources[self._editing].get("id") or "（未命名）")
        if not confirm(self, "删除来源", f"删除来源 {name}？（保存后才会写入文件）"):
            return
        del self.sources[self._editing]
        self._editing = -1
        self._selected_row = -1
        self._refresh_tree()
        self._notice(f"已移除来源 {name}：点「保存到 app.toml」才会写入文件")

    def _notice(self, text: str) -> None:
        if self._on_notice is not None:
            self._on_notice(text)


def _row_values(item: dict[str, Any]) -> tuple[str, ...]:
    """一条来源在表格里的显示值（顺序与 :data:`SOURCE_COLUMNS` 一致）。"""
    return (
        str(item.get("id") or "（未命名）"),
        str(item.get("protocol") or ""),
        _address(item),
        str(item.get("group") or ""),
        "是" if item.get("enabled") else "否",
    )


def _address(item: dict[str, Any]) -> str:
    """来源的人类可读地址（SNTP 显示 host:port，HTTPS 显示 URL）。"""
    if str(item.get("protocol") or "") == "https":
        return str(item.get("url") or "")
    host = str(item.get("host") or "")
    return f"{host}:{item.get('port')}" if host else ""


def _int_value(text: str, label: str, default: int, *, quiet: bool) -> int:
    from atri_x.gui.configedit import ConfigEditError

    raw = (text or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        if quiet:
            return default
        raise ConfigEditError(f"{label}必须是整数：{raw!r}") from None


def _float_value(text: str, label: str, default: float, *, quiet: bool) -> float:
    from atri_x.gui.configedit import ConfigEditError

    raw = (text or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        if quiet:
            return default
        raise ConfigEditError(f"{label}必须是数字：{raw!r}") from None
