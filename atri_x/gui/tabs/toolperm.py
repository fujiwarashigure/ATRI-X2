"""「工具权限」选项卡：按群管理工具授权（``state.toml`` 的 ``[group_policy]``）。

为什么需要这一页：群工具白名单只写在 ``state.toml`` 里时，界面上没有任何入口，
被拒的工具只会在会话里回一句 ``tool is not in this group's allowlist``，
普通用户既看不到有哪些工具、也不知道该去哪里开权限。这一页把三件事放到一起：

1. **当前群工具授权状态**（继承全局 / 仅白名单 / 禁止全部）；
2. **可视化授权**：激活要放行的工具，保存到 ``state.toml`` 的
   ``[group_policy."<平台:scope>"]``，后台服务 15 秒内热加载，无需重启；
3. **群聊工具进度开关**（``[platform.qqonebot].show_tool_preview_in_group``）。

界面形态：左侧是群列表 + 作用域输入，右侧是**纵向滚动的工具菜单**——按分组
（工具发现 / 联网 / 文件与执行 / 记忆 / 群聊与角色 / 图片与画师 / 技能与维护）排列，
每一行是

    [未激活]  网页搜索  web_search  低风险                     [激活]
              联网搜索关键词，返回简洁答案、来源链接和摘要。

即「激活状态 + 中文名 + 工具名 + 风险 + 一句话功能解释 + 激活按钮」，点按钮或点整行
即可切换；顶部有搜索框、「只看已激活」和「常用 / 只读」预设。工具的展示信息来自
:mod:`atri_x.gui.toolcatalog`，菜单控件在 :mod:`atri_x.gui.toolmenu`，写的策略格式
由 :mod:`atri_x.gui.toolpolicy` 负责（与群内 ``/tools allow`` 命令同一份）。

同一份策略也能在群里改：``/tools allow web_search,web_extract``、
``/tools allow all``、``/tools allow none``（群主 / 群管理员 / 超级管理员）。
界面只写工具白名单这一项，其它群策略仍走 ``/grouppolicy``。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import widgets
from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml
from atri_x.gui.tabs.base import ConfigTab, message
from atri_x.gui.toolcatalog import catalog_by_category, ordered_names, tool_meta
from atri_x.gui.toolmenu import MenuResize, ToolRow, category_header
from atri_x.gui.toolpolicy import (
    DISCOVER_TOOL,
    PRESET_COMMON,
    PRESET_READONLY,
    load_scope_policies,
    load_tool_choices,
    save_scope_policy,
)

__all__ = [
    "DISCOVER_TOOL",
    "PRESET_COMMON",
    "PRESET_READONLY",
    "ToolPermTab",
    "load_scope_policies",
    "load_tool_choices",
    "save_scope_policy",
]

#: 三种授权模式 → 界面短标签（说明在模式下方动态显示）。
_MODE_LABELS = (
    ("inherit", "继承全局"),
    ("whitelist", "仅白名单"),
    ("none", "禁止全部"),
)

#: 三种授权模式 → 一行解释。
_MODE_HINTS = {
    "inherit": "本群不设白名单：所有已装配的工具都可用（等价于 /tools allow all）。",
    "whitelist": (
        "只放行「已激活」的工具：点右侧「激活」按钮（或点整行）即可切换；"
        "discover_tool 保存时会自动加入，否则模型无法发现工具。"
    ),
    "none": "本群禁止全部工具：模型只会纯聊天（等价于 /tools allow none）。",
}


class ToolPermTab(ConfigTab):
    """群工具授权 + 群聊工具进度开关。"""

    hint = "state.toml 热加载，保存后约 15 秒内生效（无需重启）"

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._scopes: dict[str, tuple[str, list[str]]] = {}
        self._tool_vars: dict[str, tk.BooleanVar] = {}
        self._current_scope = ""
        self._saved_mode = "inherit"
        self._saved_names: frozenset[str] = frozenset()
        self._rows: dict[str, ToolRow] = {}
        self._headers: list[tuple[Any, list[str]]] = []
        self._menu_width = 640
        self._ensure_vars()
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        body = ttk.Frame(self.body)
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.LabelFrame(body, text="群组（state.toml [group_policy]）", padding=8)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self._build_scope_list(left)
        self._build_scope_box(left)
        right = ttk.LabelFrame(body, text="工具授权", padding=8)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        self._build_editor(right)
        self._build_preview_switch()

        self.button("保存本群工具授权", self._save)
        self.button("重新载入", self.reload)
        self.button("打开 state.toml", self._open_state_file)
        self.button("打开配置目录", self._open_config_dir)

    def _build_scope_list(self, parent: Any) -> None:
        self.scope_tree = ttk.Treeview(
            parent, columns=("scope", "mode"), show="headings", height=10
        )
        for name, title, width in (
            ("scope", "群作用域", 190),
            ("mode", "工具授权", 100),
        ):
            self.scope_tree.heading(name, text=title)
            self.scope_tree.column(name, width=width, stretch=name == "scope")
        self.scope_tree.pack(fill=tk.BOTH, expand=True)
        self.scope_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_scope_selected())
        self.scope_tree.tag_configure("none", foreground="#b3261e")
        self.scope_tree.tag_configure("whitelist", foreground="#1a4f9c")

    def _build_scope_box(self, parent: Any) -> None:
        box = ttk.Frame(parent)
        box.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(box, text="作用域").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(2, 0))
        self.scope_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.scope_var, width=16)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _event: self._locate_scope())
        ttk.Button(row, text="定位 / 新建", command=self._locate_scope).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(
            box,
            text="形如 qqonebot:group:1126294273（平台 + 群号；群号可在日志 / 群友页查到）",
            foreground="#8a8a8a",
            wraplength=300,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

    def _build_editor(self, parent: Any) -> None:
        self.current_label = ttk.Label(
            parent,
            text="（未选择群组）先在左侧选一个群组，或填作用域后点「定位 / 新建」",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.current_label.pack(anchor="w")

        modes = ttk.Frame(parent)
        modes.pack(fill=tk.X, pady=(4, 2))
        self.mode_var = tk.StringVar(value="inherit")
        for value, label in _MODE_LABELS:
            ttk.Radiobutton(
                modes,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._on_mode_changed,
            ).pack(side=tk.LEFT, padx=(0, 16))

        self.detail_label = ttk.Label(
            parent, text="", foreground="#555555", justify=tk.LEFT, wraplength=760
        )
        self.detail_label.pack(anchor="w")

        # 搜索 / 过滤条。
        search_bar = ttk.Frame(parent)
        search_bar.pack(fill=tk.X, pady=(8, 2))
        ttk.Label(search_bar, text="搜索").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        ttk.Entry(search_bar, textvariable=self.search_var, width=16).pack(
            side=tk.LEFT, padx=(4, 4)
        )
        ttk.Button(search_bar, text="清空", width=6, command=self._clear_search).pack(
            side=tk.LEFT
        )
        self.only_active_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            search_bar,
            text="只看已激活",
            variable=self.only_active_var,
            command=self._render_rows,
        ).pack(side=tk.LEFT, padx=(12, 0))
        self.count_label = ttk.Label(search_bar, text="", foreground="#555555")
        self.count_label.pack(side=tk.RIGHT)
        self.search_var.trace_add("write", self._on_search_changed)

        # 预设 / 全选。
        actions = ttk.Frame(parent)
        actions.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(actions, text="放行的工具（白名单模式生效）").pack(side=tk.LEFT)
        for text, command in (
            ("常用预设", lambda: self._apply_preset(PRESET_COMMON)),
            ("只读预设", lambda: self._apply_preset(PRESET_READONLY)),
            ("全选", lambda: self._set_all(True)),
            ("全不选", lambda: self._set_all(False)),
        ):
            ttk.Button(actions, text=text, command=command).pack(side=tk.RIGHT, padx=(4, 0))

        # 纵向滚动的工具菜单（高度 240：窗口缩到最小 1100x700 时下方开关仍可见）。
        self.menu = widgets.ScrollFrame(parent, height=240)
        self.menu.pack(fill=tk.BOTH, expand=True)
        self.menu_inner = self.menu.inner
        self.menu.canvas.bind("<Configure>", self._on_menu_resize, add="+")

        extra = ttk.Frame(parent)
        extra.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(extra, text="额外工具名（逗号分隔）").pack(side=tk.LEFT)
        self.extra_var = tk.StringVar()
        ttk.Entry(extra, textvariable=self.extra_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )

        self._render_rows()
        self._update_detail()

    def _build_preview_switch(self) -> None:
        box = ttk.LabelFrame(self.body, text="群聊工具进度", padding=8)
        box.pack(fill=tk.X, pady=(8, 0))
        self.preview_var = tk.BooleanVar(value=False)
        top = ttk.Frame(box)
        top.pack(fill=tk.X)
        ttk.Checkbutton(
            top,
            text="群聊里显示工具调用进度（正在调用哪个工具）",
            variable=self.preview_var,
        ).pack(side=tk.LEFT)
        ttk.Button(top, text="保存该开关", command=self._save_preview_switch).pack(
            side=tk.RIGHT
        )
        ttk.Label(
            box,
            text=(
                "写入 app.toml 的 [platform.qqonebot].show_tool_preview_in_group；"
                "QQ 官方机器人同名键写在 [platform.qqofficial]。默认关闭（群里的进度提示算噪声）。"
            ),
            foreground="#8a8a8a",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor="w")

    # -- 工具菜单 -----------------------------------------------------------
    def _ensure_vars(self, extra_names: Any = ()) -> None:
        """建好每个工具名的勾选变量（目录 + 群策略里出现的自定义名）。"""
        for name in ordered_names(list(load_tool_choices()) + list(extra_names or ())):
            if name not in self._tool_vars:
                self._tool_vars[name] = tk.BooleanVar(value=False)

    def _on_menu_resize(self, event: Any) -> None:
        self._menu_width = max(280, int(getattr(event, "width", 0)) - 26)
        for row in self._rows.values():
            row.set_wraplength(self._menu_width)

    def _on_search_changed(self, *_args: Any) -> None:
        self._render_rows()

    def _clear_search(self) -> None:
        self.search_var.set("")
        self.only_active_var.set(False)
        self._render_rows()

    def _visible_names(self) -> list[str]:
        """当前搜索 / 过滤条件下要显示的工具名（菜单顺序）。"""
        query = self.search_var.get().strip().lower()
        only_active = bool(self.only_active_var.get())
        names: list[str] = []
        for name in ordered_names(self._tool_vars):
            meta = tool_meta(name)
            if only_active and not self._tool_vars[name].get():
                continue
            if query:
                haystack = f"{name} {meta.title} {meta.summary} {meta.category}".lower()
                if query not in haystack:
                    continue
            names.append(name)
        return names

    def _render_rows(self) -> None:
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
                for meta in metas:
                    self._rows[meta.name] = ToolRow(
                        self.menu_inner, meta, self._toggle_tool
                    )
        self._on_menu_resize(MenuResize(self.menu.canvas.winfo_width()))
        self.menu.scroll_to_top()
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        """只刷新状态（切激活 / 切模式），不重建控件、不打断滚动位置。"""
        mode = self.mode_var.get()
        for name, row in self._rows.items():
            row.refresh(bool(self._tool_vars[name].get()), mode)
        for label, names in self._headers:
            active = sum(1 for name in names if self._tool_vars[name].get())
            if mode == "whitelist":
                label.configure(text=f"已激活 {active} / {len(names)}")
            else:
                label.configure(text=f"{len(names)} 个工具")
        self._update_detail()

    def _update_counts(self) -> None:
        total = len(self._tool_vars)
        active = sum(1 for var in self._tool_vars.values() if var.get())
        mode = self.mode_var.get()
        if mode == "whitelist":
            parts = [f"已激活 {active} / {total}"]
        elif mode == "inherit":
            parts = [f"全部工具可用（共 {total} 个）"]
        else:
            parts = [f"全部工具已禁止（共 {total} 个）"]
        shown = len(self._rows)
        if shown and shown != total:
            parts.append(f"当前显示 {shown}")
        if self._current_scope and self._is_dirty():
            parts.append("有未保存改动")
        self.count_label.configure(text=" · ".join(parts))

    def _is_dirty(self) -> bool:
        """当前编辑与"最后一次载入/保存"是否不同。"""
        if not self._current_scope:
            return False
        names = frozenset(name for name, var in self._tool_vars.items() if var.get())
        return self.mode_var.get() != self._saved_mode or names != self._saved_names

    def _toggle_tool(self, name: str) -> None:
        """点一行 / 点激活按钮：在「仅白名单」模式下切换放行状态。"""
        if self.mode_var.get() != "whitelist":
            self.notify("先在「仅白名单」模式下逐个激活工具（其它模式是全群统一策略）")
            return
        variable = self._tool_vars.get(name)
        if variable is None:
            return
        variable.set(not variable.get())
        if variable.get() and name != DISCOVER_TOOL:
            # 保存时总会补 discover_tool，界面同步成"已激活"，避免保存后才发现多一个。
            self._tool_vars[DISCOVER_TOOL].set(True)
        if self.only_active_var.get():
            self._render_rows()
        else:
            self._refresh_rows()

    # -- 数据 ---------------------------------------------------------------
    def on_show(self) -> None:
        # 只刷新群列表，不重载编辑器：切走再切回不该丢未保存的勾选。
        self._refresh_scopes(reload_editor=False)

    def reload(self) -> None:
        if not self.is_ready():
            return
        paths = self.require_paths()
        self._refresh_scopes(reload_editor=True)
        self.preview_var.set(_read_preview_switch(paths.config_path))
        self.notify("已载入群工具授权（改动保存后约 15 秒热加载）")

    def _refresh_scopes(self, *, reload_editor: bool = True) -> None:
        if not self.is_ready():
            return
        paths = self.require_paths()
        previous = [str(item) for item in self.scope_tree.selection()]
        try:
            self._scopes = load_scope_policies(paths.state_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        for item in self.scope_tree.get_children(""):
            self.scope_tree.delete(item)
        for scope, (mode, names) in sorted(self._scopes.items()):
            label = {"inherit": "继承全局", "whitelist": "白名单", "none": "禁止全部"}[mode]
            if mode == "whitelist":
                label = f"白名单（{len(names)}）"
            self.scope_tree.insert(
                "", tk.END, iid=scope, values=(scope, label), tags=(mode,)
            )
        target = ""
        for candidate in (previous[0] if previous else "", self._current_scope):
            if candidate and candidate in self._scopes:
                target = candidate
                break
        if not target and self._scopes:
            target = sorted(self._scopes)[0]
        if not target:
            return
        self.scope_tree.selection_set(target)
        self.scope_tree.see(target)
        if reload_editor or target != self._current_scope:
            mode, names = self._scopes.get(target, ("inherit", []))
            self._load_editor(target, mode, names)

    def _on_scope_selected(self) -> None:
        selection = self.scope_tree.selection()
        if not selection:
            return
        scope = str(selection[0])
        if scope == self._current_scope:
            return
        mode, names = self._scopes.get(scope, ("inherit", []))
        self._load_editor(scope, mode, names)

    def _locate_scope(self) -> None:
        scope = self.scope_var.get().strip()
        if not scope:
            message(self.frame, "群作用域", "先填一个作用域，例如 qqonebot:group:1126294273")
            return
        if scope == self._current_scope:
            # 已经在编辑这个群：手点一次「定位」不该把未保存的勾选冲掉。
            self.scope_tree.selection_set(scope)
            self.scope_tree.see(scope)
            return
        mode, names = self._scopes.get(scope, ("inherit", []))
        if scope in self._scopes:
            self.scope_tree.selection_set(scope)
            self.scope_tree.see(scope)
        else:
            self.scope_tree.insert(
                "",
                tk.END,
                iid=scope,
                values=(
                    scope,
                    {"inherit": "继承全局", "whitelist": "白名单", "none": "禁止全部"}[mode],
                ),
                tags=(mode,),
            )
            self.scope_tree.selection_set(scope)
        self._load_editor(scope, mode, names)

    def _load_editor(self, scope: str, mode: str, names: list[str]) -> None:
        self._current_scope = scope
        self.current_label.configure(text=f"群组：{scope}")
        self.mode_var.set(mode)
        self._ensure_vars(names)
        wanted = {item.strip().lower() for item in names if item.strip()}
        for name, variable in self._tool_vars.items():
            variable.set(name.lower() in wanted)
        self._saved_mode = mode
        self._saved_names = frozenset(
            name for name, variable in self._tool_vars.items() if variable.get()
        )
        self.extra_var.set("")
        self._render_rows()

    def _on_mode_changed(self) -> None:
        self._refresh_rows()

    def _update_detail(self) -> None:
        mode = self.mode_var.get()
        text = _MODE_HINTS.get(mode, "")
        if mode == "whitelist" and not any(
            variable.get() for variable in self._tool_vars.values()
        ):
            text += "（当前还没激活任何工具：保存后本群只剩 discover_tool。）"
        self.detail_label.configure(text=text)
        self._update_counts()

    # -- 批量操作 -----------------------------------------------------------
    def _set_all(self, value: bool) -> None:
        self.mode_var.set("whitelist")
        for variable in self._tool_vars.values():
            variable.set(bool(value))
        self._render_rows()

    def _apply_preset(self, preset: tuple[str, ...]) -> None:
        wanted = {name.lower() for name in preset}
        self.mode_var.set("whitelist")
        for name, variable in self._tool_vars.items():
            variable.set(name.lower() in wanted)
        self.search_var.set("")
        self.only_active_var.set(False)
        self._render_rows()
        self.notify("已套用预设；点「保存本群工具授权」写入 state.toml")

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        scope = self._current_scope.strip()
        if not scope:
            message(self.frame, "保存", "先在左侧选一个群组，或填作用域后点「定位 / 新建」")
            return
        mode = self.mode_var.get()
        names = [name for name, variable in self._tool_vars.items() if variable.get()]
        for item in self.extra_var.get().replace("，", ",").split(","):
            text = item.strip()
            if text and text not in names:
                names.append(text)
        try:
            paths = self.require_paths()
            save_scope_policy(paths.state_path, scope, mode, names)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal(f"保存群工具授权：{scope} → {mode}（{len(names)} 个工具）")
        self._refresh_scopes(reload_editor=False)
        if self.scope_tree.exists(scope):
            self.scope_tree.selection_set(scope)
            self.scope_tree.see(scope)
        # 写盘后用落盘结果回填编辑器：保存状态与「有未保存改动」标记要一起归零。
        saved_mode, saved_names = self._scopes.get(scope, (mode, list(names)))
        self._load_editor(scope, saved_mode, saved_names)
        self.notify(f"已保存 {scope} 的工具授权；后台服务约 15 秒内热加载")

    def _save_preview_switch(self) -> None:
        try:
            paths = self.require_paths()
            patch_section(
                paths.config_path,
                "platform.qqonebot",
                {"show_tool_preview_in_group": bool(self.preview_var.get())},
                header_comment="群聊工具进度（桌面端工具权限页）",
            )
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal(
            f"保存群聊工具进度开关：show_tool_preview_in_group={bool(self.preview_var.get())}"
        )
        self.notify("已保存 [platform.qqonebot].show_tool_preview_in_group；重启后台服务后生效")

    # -- 打开文件/目录 -------------------------------------------------------
    def _open_state_file(self) -> None:
        from atri_x.gui.process import open_path

        path = self.require_paths().state_path
        if not os.path.exists(path):
            message(self.frame, "state.toml", f"还没有 {path}（后台服务启动后会生成）")
            return
        if not open_path(path):
            message(self.frame, "state.toml", f"文件：{path}")

    def _open_config_dir(self) -> None:
        from atri_x.gui.process import open_path

        directory = self.require_paths().config_dir
        if not open_path(directory):
            message(self.frame, "配置目录", f"打不开目录：{directory}")


def _read_preview_switch(app_path: str) -> bool:
    """读 ``[platform.qqonebot].show_tool_preview_in_group``（缺省 False）。"""
    if not app_path or not os.path.exists(app_path):
        return False
    try:
        data = read_toml(app_path)
    except ConfigEditError:
        return False
    platform = data.get("platform")
    if not isinstance(platform, dict):
        return False
    section = platform.get("qqonebot")
    if not isinstance(section, dict):
        return False
    return bool(section.get("show_tool_preview_in_group"))
