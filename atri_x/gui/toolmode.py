"""「工具权限」页的分模式工具编辑器（chat / work 各一个）。

为什么单独一层：这一块是"某个会话模式下放行哪些工具"的完整编辑区（三态单选 +
搜索 + 预设 + 纵向工具菜单 + 额外工具名），两个模式各实例化一份；把它放进
:mod:`atri_x.gui.tabs.toolperm` 会让页面文件过长，也让页面只负责"群 / 作用域 / 保存"。

编辑器不碰配置读写（那在 :mod:`atri_x.gui.toolpolicy`），只维护界面状态并提供
:meth:`ModeEditor.collect` 给页面落盘；模式三态语义与群策略字段一一对应：

- ``inherit``：本模式不单独配置，跟随通用 ``tool_allowlist``；
- ``whitelist``：只放行激活的工具（保存时自动补 ``discover_tool``）；
- ``none``：本模式禁止全部工具。

工具行的「参数」按钮（只在该工具于 :mod:`atri_x.gui.toolsettings` 里有可调参数时出现）
会回调页面的 ``show_tool_params``，跳到同一个选项卡的「工具参数（全局）」子选项卡并展开
对应的面板——这样"授权"和"参数"在同一个页面里，不用记两套入口。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import widgets
from atri_x.gui.toolcatalog import catalog_by_category, ordered_names, tool_meta
from atri_x.gui.toolmenu import MenuResize, ToolRow, category_header
from atri_x.gui.toolpolicy import DISCOVER_TOOL, PRESET_COMMON, PRESET_READONLY
from atri_x.gui.toolsettings import has_settings

__all__ = [
    "MODE_HINTS",
    "MODE_LABELS",
    "MODE_SHORT",
    "ModeEditor",
    "active_names",
    "editor_signature",
]

#: 三种授权模式 → 界面短标签。
MODE_LABELS = (
    ("inherit", "继承通用"),
    ("whitelist", "仅白名单"),
    ("none", "禁止全部"),
)

#: 三种授权模式 → 一行解释。
MODE_HINTS = {
    "inherit": (
        "本模式不单独设白名单：跟随「通用」策略（群内 /tools allow 写入的那份）；"
        "通用也没配就是不做限制。"
    ),
    "whitelist": (
        "只放行「已激活」的工具：点右侧「激活」按钮（或点整行）即可切换；"
        "discover_tool 保存时会自动加入，否则模型无法发现工具。"
    ),
    "none": "本模式禁止全部工具（不影响另一个模式）。",
}

#: 群列表摘要用的短标签。
MODE_SHORT = {"inherit": "继承", "whitelist": "白名单", "none": "禁止"}


class ModeEditor:
    """一个会话模式（chat / work）的工具授权编辑器。"""

    def __init__(self, page: Any, parent: Any, mode_key: str, tool_choices: list[str]) -> None:
        self.page = page
        self.mode_key = mode_key
        self.mode_var = tk.StringVar(value="inherit")
        self.search_var = tk.StringVar()
        self.only_active_var = tk.BooleanVar(value=False)
        self.extra_var = tk.StringVar()
        self.tool_vars: dict[str, tk.BooleanVar] = {}
        self._rows: dict[str, ToolRow] = {}
        self._headers: list[tuple[Any, list[str]]] = []
        self._menu_width = 520
        self.frame = ttk.Frame(parent, padding=(8, 6))
        # 先把工具名变量建好，_build() 末尾的首次渲染才有东西可画。
        self._ensure_vars(tool_choices)
        self._build()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        modes = ttk.Frame(self.frame)
        modes.pack(fill=tk.X)
        for value, label in MODE_LABELS:
            ttk.Radiobutton(
                modes,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._on_mode_changed,
            ).pack(side=tk.LEFT, padx=(0, 16))

        self.detail_label = ttk.Label(
            self.frame, text="", foreground="#555555", justify=tk.LEFT, wraplength=700
        )
        self.detail_label.pack(anchor="w")

        search_bar = ttk.Frame(self.frame)
        search_bar.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(search_bar, text="搜索").pack(side=tk.LEFT)
        ttk.Entry(search_bar, textvariable=self.search_var, width=14).pack(
            side=tk.LEFT, padx=(4, 4)
        )
        ttk.Button(search_bar, text="清空", width=6, command=self._clear_search).pack(
            side=tk.LEFT
        )
        ttk.Checkbutton(
            search_bar,
            text="只看已激活",
            variable=self.only_active_var,
            command=self.render_rows,
        ).pack(side=tk.LEFT, padx=(10, 0))
        self.count_label = ttk.Label(search_bar, text="", foreground="#555555")
        self.count_label.pack(side=tk.RIGHT)
        self.search_var.trace_add("write", lambda *_a: self.render_rows())

        actions = ttk.Frame(self.frame)
        actions.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(actions, text="放行的工具（仅白名单模式生效）").pack(side=tk.LEFT)
        for text, command in (
            ("常用预设", lambda: self.apply_preset(PRESET_COMMON)),
            ("只读预设", lambda: self.apply_preset(PRESET_READONLY)),
            ("全选", lambda: self.set_all(True)),
            ("全不选", lambda: self.set_all(False)),
        ):
            ttk.Button(actions, text=text, command=command).pack(
                side=tk.RIGHT, padx=(4, 0)
            )

        self.menu = widgets.ScrollFrame(self.frame, height=190)
        self.menu.pack(fill=tk.BOTH, expand=True)
        self.menu_inner = self.menu.inner
        self.menu.canvas.bind("<Configure>", self._on_menu_resize, add="+")

        ttk.Label(
            self.frame,
            text=(
                "工具的内部参数（生图留档、搜索通道顺序、记忆写入上限…）在同一页的"
                "「工具参数（全局）」子选项卡里改；有参数的工具行右侧会多一个「参数」按钮。"
            ),
            foreground="#8a8a8a",
            wraplength=640,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

        extra = ttk.Frame(self.frame)
        extra.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(extra, text="额外工具名（逗号分隔）").pack(side=tk.LEFT)
        ttk.Entry(extra, textvariable=self.extra_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )

        self.render_rows()
        self.update_detail()

    # -- 工具菜单 -----------------------------------------------------------
    def _ensure_vars(self, names: Any = ()) -> None:
        for name in ordered_names(list(names or ())):
            if name not in self.tool_vars:
                self.tool_vars[name] = tk.BooleanVar(value=False)

    def _on_menu_resize(self, event: Any) -> None:
        self._menu_width = max(260, int(getattr(event, "width", 0)) - 26)
        for row in self._rows.values():
            row.set_wraplength(self._menu_width)

    def _clear_search(self) -> None:
        self.search_var.set("")
        self.only_active_var.set(False)
        self.render_rows()

    def _visible_names(self) -> list[str]:
        query = self.search_var.get().strip().lower()
        only_active = bool(self.only_active_var.get())
        names: list[str] = []
        for name in ordered_names(self.tool_vars):
            meta = tool_meta(name)
            if only_active and not self.tool_vars[name].get():
                continue
            if query:
                haystack = f"{name} {meta.title} {meta.summary} {meta.category}".lower()
                if query not in haystack:
                    continue
            names.append(name)
        return names

    def render_rows(self) -> None:
        """按当前过滤条件重建整份工具菜单（切分组 / 搜索时才重建）。"""
        for child in self.menu_inner.winfo_children():
            child.destroy()
        self._rows = {}
        self._headers = []
        names = self._visible_names()
        if not names:
            tk.Label(
                self.menu_inner,
                text="没有匹配的工具：清空搜索框或取消「只看已激活」。",
                font=("Microsoft YaHei UI", 9),
                fg="#7a7f87",
                bg="#ffffff",
                anchor="w",
                justify=tk.LEFT,
            ).pack(fill=tk.X, padx=10, pady=14)
        else:
            for category, metas in catalog_by_category(names):
                count = category_header(self.menu_inner, category)
                self._headers.append((count, [meta.name for meta in metas]))
                on_params = getattr(self.page, "show_tool_params", None)
                for meta in metas:
                    callback = on_params if (on_params is not None and has_settings(meta.name)) else None
                    self._rows[meta.name] = ToolRow(
                        self.menu_inner, meta, self._toggle_tool, on_params=callback
                    )
        self._on_menu_resize(MenuResize(self.menu.canvas.winfo_width()))
        self.menu.scroll_to_top()
        self.refresh_rows()

    def refresh_rows(self) -> None:
        """只刷新状态（切激活 / 切模式），不重建控件、不打断滚动位置。"""
        mode = self.mode_var.get()
        for name, row in self._rows.items():
            row.refresh(bool(self.tool_vars[name].get()), mode)
        for label, names in self._headers:
            active = sum(1 for name in names if self.tool_vars[name].get())
            if mode == "whitelist":
                label.configure(text=f"已激活 {active} / {len(names)}")
            else:
                label.configure(text=f"{len(names)} 个工具")
        self.update_detail()

    def update_detail(self) -> None:
        text = MODE_HINTS.get(self.mode_var.get(), "")
        active = sum(1 for var in self.tool_vars.values() if var.get())
        total = len(self.tool_vars)
        if self.mode_var.get() == "whitelist":
            if not active:
                text += "（当前还没激活任何工具：保存后本模式只剩 discover_tool。）"
            counts = f"已激活 {active} / {total}"
        elif self.mode_var.get() == "inherit":
            counts = f"共 {total} 个工具；跟随通用策略"
        else:
            counts = f"全部工具已禁止（共 {total} 个）"
        shown = len(self._rows)
        if shown and shown != total:
            counts += f" · 当前显示 {shown}"
        if getattr(self.page, "_current_scope", "") and self.page._is_dirty():
            counts += " · 有未保存改动"
        self.detail_label.configure(text=text)
        self.count_label.configure(text=counts)

    def _toggle_tool(self, name: str) -> None:
        if self.mode_var.get() != "whitelist":
            self.page.notify("先在「仅白名单」模式下逐个激活工具（其它模式是全模式统一策略）")
            return
        variable = self.tool_vars.get(name)
        if variable is None:
            return
        variable.set(not variable.get())
        if variable.get() and name != DISCOVER_TOOL:
            # 保存时总会补 discover_tool，界面同步成"已激活"，避免保存后才发现多一个。
            self.tool_vars[DISCOVER_TOOL].set(True)
        if self.only_active_var.get():
            self.render_rows()
        else:
            self.refresh_rows()

    def _on_mode_changed(self) -> None:
        self.refresh_rows()

    def set_all(self, value: bool) -> None:
        self.mode_var.set("whitelist")
        for variable in self.tool_vars.values():
            variable.set(bool(value))
        self.render_rows()

    def apply_preset(self, preset: tuple[str, ...]) -> None:
        wanted = {name.lower() for name in preset}
        self.mode_var.set("whitelist")
        for name, variable in self.tool_vars.items():
            variable.set(name.lower() in wanted)
        self.search_var.set("")
        self.only_active_var.set(False)
        self.render_rows()
        self.page.notify("已套用预设；点「保存本群工具授权」写入 state.toml")

    # -- 数据 ---------------------------------------------------------------
    def load(self, mode: str, names: list[str]) -> None:
        self.mode_var.set(mode)
        self._ensure_vars(names)
        wanted = {item.strip().lower() for item in names if item.strip()}
        for name, variable in self.tool_vars.items():
            variable.set(name.lower() in wanted)
        self.extra_var.set("")
        self.render_rows()

    def collect(self) -> tuple[str, list[str]]:
        """返回 ``(模式, 工具名列表)``；``inherit`` 时列表无意义。"""
        mode = self.mode_var.get()
        names = [name for name, variable in self.tool_vars.items() if variable.get()]
        for item in self.extra_var.get().replace("，", ",").split(","):
            text = item.strip()
            if text and text not in names:
                names.append(text)
        return mode, names


def active_names(editor: ModeEditor) -> frozenset[str]:
    """编辑器里当前激活的工具名集合。"""
    return frozenset(name for name, var in editor.tool_vars.items() if var.get())


def editor_signature(editor: ModeEditor) -> tuple[str, frozenset[str], str]:
    """脏标记用的编辑器快照（模式 + 勾选 + 额外输入框）。"""
    return (
        editor.mode_var.get(),
        active_names(editor),
        editor.extra_var.get().strip(),
    )
