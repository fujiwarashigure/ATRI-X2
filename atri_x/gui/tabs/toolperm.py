"""「工具权限」选项卡：按群、按会话模式管理工具授权（``state.toml`` 的 ``[group_policy]``）。

为什么需要这一页：群工具白名单只写在 ``state.toml`` 里时，界面上没有任何入口，
被拒的工具只会在会话里回一句 ``tool is not in this group's allowlist``，
普通用户既看不到有哪些工具、也不知道该去哪里开权限。这一页把三件事放到一起：

1. **当前群工具授权状态**（继承 / 仅白名单 / 禁止全部，chat 与 work 分别有）；
2. **可视化授权**：``chat`` 与 ``work`` 各有一个子选项卡（编辑器在
   :mod:`atri_x.gui.toolmode`），分别激活要放行的工具，保存到 ``state.toml`` 的
   ``[group_policy."<平台:scope>"]``：``tool_allowlist_chat`` / ``tool_allowlist_work``；
   某个模式没单独配置时回退到通用 ``tool_allowlist``（``/tools allow`` 写的就是它）。
   后台服务 15 秒内热加载；每个子选项卡**顶部**还嵌着该模式的「内容安全覆盖」面板
   （``app.toml`` 的 ``[safety.chat]`` / ``[safety.work]``，留「（默认）」= 继承全局，
   重启后台服务生效）；
3. **工具内部参数**：第三个子选项卡「工具参数（全局）」（控件在
   :mod:`atri_x.gui.toolparams`，定义与读写规则在 :mod:`atri_x.gui.toolsettings`）——
   每个工具一组可展开 / 收起的参数面板（生图留档 ``metadata_store``、搜索通道顺序、
   记忆写入上限、风险阈值……），写回 ``app.toml`` / ``services.toml``，重启后台服务生效。
   工具行右侧的「参数」按钮会直接跳到对应面板；
4. **群聊工具进度开关**（``[platform.qqonebot].show_tool_preview_in_group``）。

界面形态：左侧是群列表 + 作用域输入（候选来自会话库，保存时自动归一/迁移旧写法），
右侧是 chat / work 两个子选项卡，每个下面是按分组排列的纵向滚动工具菜单（一行 =
激活状态 + 中文名 + 工具名 + 风险 + 一句话功能解释 + 激活按钮），各自有搜索、
「只看已激活」与「常用 / 只读」预设；第三个子选项卡「工具参数（全局）」按工具列出
可展开的参数面板。工具展示信息在 :mod:`atri_x.gui.toolcatalog`，菜单控件在
:mod:`atri_x.gui.toolmenu`，配置读写在同名的 :mod:`atri_x.gui.toolpolicy`。

同一份策略也能在群里改：``/tools allow web_search,web_extract``、
``/tools allow all``、``/tools allow none``（群主 / 群管理员 / 超级管理员）——命令写的是
**两个模式共用的通用白名单**，作为分模式配置的兜底。界面保存时按模式分别落盘。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.tabs.base import ConfigTab, message
from atri_x.gui.toolmode import MODE_SHORT, ModeEditor, editor_signature
from atri_x.gui.toolparams import SettingPanel, ToolParamsEditor
from atri_x.gui.toolpolicy import (
    DISCOVER_TOOL,
    PRESET_COMMON,
    PRESET_READONLY,
    canonical_scope,
    discover_group_scopes,
    load_scope_mode_policies,
    load_tool_choices,
    save_scope_mode_policy,
    scope_is_canonical,
    scope_problem,
)
from atri_x.gui.toolpreview import (
    PREVIEW_TAB_TITLE,
    ToolPreviewPanel,
    read_platform_preview_switch,
)
from atri_x.gui.toolsettings import group_by_key, load_all

__all__ = [
    "DISCOVER_TOOL",
    "MODE_ORDER",
    "MODE_SAFETY_GROUPS",
    "PARAMS_TAB_TITLE",
    "PRESET_COMMON",
    "PRESET_READONLY",
    "ToolPermTab",
    "canonical_scope",
    "discover_group_scopes",
    "load_scope_mode_policies",
    "load_tool_choices",
    "save_scope_mode_policy",
]

#: 两个子选项卡的顺序与标题（``state.toml`` 里对应的字段是 ``tool_allowlist_<mode>``）。
MODE_ORDER: tuple[str, ...] = ("chat", "work")
_MODE_TITLES = {
    "chat": "chat 模式（闲聊 / 低成本模型槽位）",
    "work": "work 模式（工作 / 工具模型槽位）",
}

#: 第三个子选项卡：工具内部参数（全局，写 ``app.toml`` / ``services.toml``）。
PARAMS_TAB_TITLE = "工具参数（全局）"

#: 每个会话模式子选项卡顶部嵌的内容安全覆盖面板（面板 id 见
#: :mod:`atri_x.gui.toolsettings_general`）：``app.toml`` 的 ``[safety.chat]`` /
#: ``[safety.work]``，留空 / 「（默认）」= 继承全局 ``[safety]``。
MODE_SAFETY_GROUPS: dict[str, str] = {"chat": "safety_chat", "work": "safety_work"}


class _ModeSafetyPanel(SettingPanel):
    """嵌在 chat / work 子选项卡顶部的「内容安全覆盖」面板。

    与「工具参数（全局）」子选项卡里的同名面板是同一套字段定义、同一套写盘逻辑
    （:meth:`SettingPanel.save`）；这里只多做一件事：保存成功后把参数页里的同名面板
    也回填成落盘值，避免两处显示对不上（用户刚在 chat 页改完，切到参数页还是旧值）。
    """

    def save(self, *, notify: bool = True) -> bool:
        saved = super().save(notify=notify)
        if saved:
            sync = getattr(self.page, "refresh_params_group", None)
            if callable(sync):
                sync(self.group.key)
        return saved


class ToolPermTab(ConfigTab):
    """群工具授权（chat / work 分模式）+ 内容安全按模式覆盖 + 工具内部参数 + 群聊工具进度开关。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        #: 工具名清单（两个编辑器共用一份）。
        self.tool_choices: list[str] = load_tool_choices()
        #: ``{scope: {mode: (授权模式, 工具名)}}``（含 ``generic`` 兜底项）。
        self._mode_scopes: dict[str, dict[str, tuple[str, list[str]]]] = {}
        #: 文件里真正配置过的作用域（与"从会话库发现、尚未配置"的候选区分开）。
        self._configured: set[str] = set()
        #: 从会话库/聊天历史库发现真实群作用域，给作用域输入框做候选。
        self._discovered: list[str] = []
        self.editors: dict[str, ModeEditor] = {}
        #: 第三个子选项卡：工具内部参数面板（全局，与群无关）。
        self.params_editor: ToolParamsEditor | None = None
        #: 第四个子选项卡：工具进度预览（群聊开关 + [tool_preview] 文案）。
        self.preview_panel: ToolPreviewPanel | None = None
        #: chat / work 子选项卡顶部的「内容安全覆盖」面板（与参数页的同名面板同一套字段）。
        self.mode_safety_panels: dict[str, SettingPanel] = {}
        self._current_scope = ""
        self._saved_signature: dict[str, tuple[str, frozenset[str], str]] = {}
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        body = ttk.Frame(self.body)
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.LabelFrame(body, text="群组（state.toml [group_policy]）", padding=8)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self._left_frame = left
        self._build_scope_list(left)
        self._build_scope_box(left)
        right = ttk.LabelFrame(body, text="工具授权（chat / work 分别配置）、工具参数与工具进度预览", padding=8)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        self._right_frame = right
        self._build_editor(right)

        self.button("保存本群工具授权", self._save)
        self.button("重新载入", self.reload)
        self.button("打开 state.toml", self._open_state_file)
        self.button("打开配置目录", self._open_config_dir)

    def _build_scope_list(self, parent: Any) -> None:
        self.scope_tree = ttk.Treeview(
            parent, columns=("scope", "mode"), show="headings", height=10
        )
        for name, title, width in (
            ("scope", "群作用域", 175),
            ("mode", "工具授权（chat / work）", 185),
        ):
            self.scope_tree.heading(name, text=title)
            self.scope_tree.column(name, width=width, stretch=name == "scope")
        self.scope_tree.pack(fill=tk.BOTH, expand=True)
        self.scope_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_scope_selected())
        self.scope_tree.tag_configure("none", foreground="#b3261e")
        self.scope_tree.tag_configure("whitelist", foreground="#1a4f9c")
        self.scope_tree.tag_configure("inherit", foreground="#7a7f87")
        # 作用域写法不规范的旧键：显式标出来，点进去保存一次即可自动改成规范键。
        self.scope_tree.tag_configure("legacy", background="#fff4d6", foreground="#8a5a00")

    def _build_scope_box(self, parent: Any) -> None:
        box = ttk.Frame(parent)
        box.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(box, text="作用域").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(2, 0))
        self.scope_var = tk.StringVar()
        # 候选来自真实会话库：不用手打，也不会再从「群友」页复制出 `·` 写法。
        self.scope_combo = ttk.Combobox(
            row, textvariable=self.scope_var, width=18, values=[]
        )
        self.scope_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.scope_combo.bind("<Return>", lambda _event: self._locate_scope())
        ttk.Button(row, text="定位 / 新建", command=self._locate_scope).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(
            box,
            text=(
                "形如 qqonebot:group:123456789（平台 + 群号）。"
                "从「群友」页复制的 `qqonebot · group:123` 会自动归一成半角冒号。"
            ),
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
        self.generic_label = ttk.Label(
            parent,
            text="通用白名单（群内 /tools allow 写入，作为两个模式的兜底）：未配置",
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=760,
        )
        self.generic_label.pack(anchor="w", pady=(2, 4))

        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        for mode_key in MODE_ORDER:
            editor = ModeEditor(self, self.notebook, mode_key, self.tool_choices)
            self.editors[mode_key] = editor
            self._embed_safety_panel(mode_key, editor)
            self.notebook.add(editor.frame, text=_MODE_TITLES[mode_key])
        # 第三个子选项卡：工具内部参数（全局）。工具行右侧的「参数」按钮会跳到这里。
        self.params_editor = ToolParamsEditor(self, self.notebook)
        self.notebook.add(self.params_editor.frame, text=PARAMS_TAB_TITLE)
        # 第四个子选项卡：工具进度预览（群聊开关 + 兜底文案）。
        self.preview_panel = ToolPreviewPanel(self, self.notebook)
        self.notebook.add(self.preview_panel.frame, text=PREVIEW_TAB_TITLE)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _embed_safety_panel(self, mode_key: str, editor: ModeEditor) -> None:
        """把「内容安全：<模式> 模式覆盖」面板钉在该子选项卡内容区最上面。

        复用工具参数页的同一个 :class:`~atri_x.gui.toolparams.SettingPanel`（字段定义、
        写盘、文案都在那一层），这里只换父容器、用 ``pack(before=…)`` 调整顺序：
        覆盖面板在该模式子选项卡里排第一，下面才是工具授权菜单。默认收起，避免一进来
        就把工具菜单挤出可视区（标题行上的 ▸ 展开）。
        """
        group = group_by_key(MODE_SAFETY_GROUPS.get(mode_key, ""))
        if group is None:  # pragma: no cover - 注册表缺面板时安静跳过，不影响授权编辑
            return
        panel = _ModeSafetyPanel(self, editor.frame, group)
        siblings = editor.frame.pack_slaves()
        anchor = siblings[0] if siblings else None
        if anchor is not None and anchor is not panel.frame:
            panel.frame.pack_configure(before=anchor)
        self.mode_safety_panels[mode_key] = panel

    def refresh_params_group(self, group_key: str) -> None:
        """把参数页里同名面板回填成落盘值（子选项卡里的覆盖面板保存成功后调用）。

        参数页那一份有未保存改动时跳过：用户在那边的输入不该被这边的一次保存冲掉。
        """
        if self.params_editor is None:
            return
        panel = self.params_editor.panels.get(group_key)
        if panel is None or panel.dirty():
            return
        try:
            values = load_all(self.require_paths())
        except ConfigEditError as exc:
            self.fail(exc)
            return
        panel.load(values.get(group_key, {}))
        self.params_editor.refresh_summary()

    def _on_tab_changed(self, _event: Any = None) -> None:
        """切页时：参数页 / 进度预览页隐藏左侧群列表（这两页是全局设置，跟群无关，
        右侧要更宽）；同时刷新一次"有未保存改动"的组数（不重载，避免冲掉输入）。
        """
        if self.params_editor is None:
            return
        selected = str(self.notebook.select())
        global_tabs = {str(self.params_editor.frame)}
        if self.preview_panel is not None:
            global_tabs.add(str(self.preview_panel.frame))
        hide_left = selected in global_tabs
        if hide_left and self._left_frame.winfo_manager():
            self._left_frame.pack_forget()
        elif not hide_left and not self._left_frame.winfo_manager():
            self._left_frame.pack(
                side=tk.LEFT, fill=tk.Y, before=self._right_frame
            )
        self.params_editor.refresh_summary()

    def show_tool_params(self, tool_name: str) -> None:
        """跳到「工具参数」子选项卡并展开该工具的面板（工具行「参数」按钮的回调）。"""
        if self.params_editor is None:
            return
        self.notebook.select(self.params_editor.frame)
        if not self.params_editor.reveal_tool(tool_name):
            self.notify(f"工具 {tool_name} 没有可调参数")

    def show_global_params(self) -> None:
        """跳到「工具参数（全局）」子选项卡并展开「全局参数（所有工具）」面板。

        内容安全（道德限制 + 敏感信息过滤）的全局开关就在那个面板里；同一子选项卡里
        还跟着「chat / work 模式覆盖」两个面板（也在两个模式子选项卡的顶部）。
        「功能」页双击「内容安全」一行会走到这里。
        """
        if self.params_editor is None:
            return
        self.notebook.select(self.params_editor.frame)
        panel = self.params_editor.panels.get("global")
        if panel is not None:
            panel.expand()

    def show_preview_tab(self) -> None:
        """跳到「工具进度预览」子选项卡（「功能」页双击跳转用）。"""
        if self.preview_panel is None:
            return
        self.notebook.select(self.preview_panel.frame)

    # -- 群聊工具进度（兼容旧属性；控件在「工具进度预览」子选项卡里） ----------
    @property
    def preview_var(self) -> tk.BooleanVar:
        """群聊进度开关的 ``BooleanVar``（旧接口；现在属于 preview_panel）。"""
        return self.preview_panel.switch_vars["platform.qqonebot"]

    # -- 兼容旧属性（自检/外部脚本曾直接读单个编辑器） ---------------------
    @property
    def _rows(self) -> dict[str, Any]:
        return self.editors[MODE_ORDER[0]]._rows

    @property
    def mode_var(self) -> tk.StringVar:
        return self.editors[MODE_ORDER[0]].mode_var

    @property
    def _tool_vars(self) -> dict[str, tk.BooleanVar]:
        return self.editors[MODE_ORDER[0]].tool_vars

    def _set_all(self, value: bool) -> None:
        for editor in self.editors.values():
            editor.set_all(value)

    def _apply_preset(self, preset: tuple[str, ...]) -> None:
        for editor in self.editors.values():
            editor.apply_preset(preset)

    # -- 数据 ---------------------------------------------------------------
    def on_show(self) -> None:
        # 只刷新群列表，不重载编辑器：切走再切回不该丢未保存的勾选。
        self._refresh_scopes(reload_editor=False)

    def reload(self) -> None:
        if not self.is_ready():
            return
        paths = self.require_paths()
        self._refresh_scopes(reload_editor=True)
        if self.preview_panel is not None:
            self.preview_panel.load()
        if self.params_editor is not None:
            self.params_editor.reload()
        self._reload_safety_panels(paths)
        self.notify(
            "已载入群工具授权与工具参数（授权约 15 秒热加载；工具参数需重启后台服务）"
        )

    def _reload_safety_panels(self, paths: Any) -> None:
        """按落盘值回填 chat / work 子选项卡顶部的「内容安全覆盖」面板。"""
        if not self.mode_safety_panels:
            return
        try:
            data = load_all(paths)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        for panel in self.mode_safety_panels.values():
            panel.load(data.get(panel.group.key, {}))

    def _refresh_scopes(self, *, reload_editor: bool = True) -> None:
        if not self.is_ready():
            return
        paths = self.require_paths()
        previous = [str(item) for item in self.scope_tree.selection()]
        try:
            self._mode_scopes = load_scope_mode_policies(paths.state_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._configured = set(self._mode_scopes)
        self._refresh_discovered()
        # 会话库里真实出现过、但 state.toml 还没配置的群也列出来：
        # 用户不用猜作用域怎么写，点一下就能开始授权。
        display = dict(self._mode_scopes)
        for scope in self._discovered:
            display.setdefault(scope, {})
        for item in self.scope_tree.get_children(""):
            self.scope_tree.delete(item)
        for scope in sorted(display):
            policies = display.get(scope) or {}
            tags: tuple[str, ...] = ("inherit",)
            label = _scope_summary(policies)
            if scope not in self._configured:
                label += " · 未配置"
            elif not scope_is_canonical(scope):
                label += " · 写法不规范"
                tags = ("legacy",)
            self.scope_tree.insert("", tk.END, iid=scope, values=(scope, label), tags=tags)
        target = ""
        candidates = [
            previous[0] if previous else "",
            self._current_scope,
            *sorted(self._configured),
            *self._discovered,
        ]
        for candidate in candidates:
            if candidate and candidate in display:
                target = candidate
                break
        if not target:
            return
        self.scope_tree.selection_set(target)
        self.scope_tree.see(target)
        if reload_editor or target != self._current_scope:
            self._load_editor(target, display.get(target) or {})

    def _refresh_discovered(self) -> None:
        """刷新真实群作用域候选（会话库 + 聊天历史库；只读，失败安静降级）。"""
        paths = self.require_paths()
        try:
            self._discovered = discover_group_scopes(
                paths.sessions_db_path, paths.chat_history_db_path
            )
        except Exception:  # noqa: BLE001 - 库被占用/损坏都不该让页面起不来
            self._discovered = []
        values = list(dict.fromkeys([*self._discovered, *sorted(self._configured)]))
        if hasattr(self, "scope_combo"):
            self.scope_combo.configure(values=values)

    def _on_scope_selected(self) -> None:
        selection = self.scope_tree.selection()
        if not selection:
            return
        scope = str(selection[0])
        if scope == self._current_scope:
            return
        self._load_editor(scope, self._mode_scopes.get(scope) or {})

    def _locate_scope(self) -> None:
        scope = canonical_scope(self.scope_var.get())
        problem = scope_problem(scope)
        if problem:
            message(self.frame, "群作用域", problem)
            return
        self.scope_var.set(scope)
        if scope == self._current_scope:
            # 已经在编辑这个群：手点一次「定位」不该把未保存的勾选冲掉。
            self.scope_tree.selection_set(scope)
            self.scope_tree.see(scope)
            return
        if self.scope_tree.exists(scope):
            self.scope_tree.selection_set(scope)
            self.scope_tree.see(scope)
        else:
            self.scope_tree.insert(
                "",
                tk.END,
                iid=scope,
                values=(scope, _scope_summary({}) + " · 未配置"),
                tags=("inherit",),
            )
            self.scope_tree.selection_set(scope)
        self._load_editor(scope, self._mode_scopes.get(scope) or {})

    def _load_editor(self, scope: str, policies: dict[str, tuple[str, list[str]]]) -> None:
        self._current_scope = scope
        self.scope_var.set(scope)
        self.current_label.configure(text=f"群组：{scope}")
        generic_mode, generic_names = policies.get("generic", ("inherit", []))
        self.generic_label.configure(
            text="通用白名单（群内 /tools allow 写入，作为两个模式的兜底）："
            + _describe(generic_mode, generic_names)
        )
        for mode_key, editor in self.editors.items():
            mode, names = policies.get(mode_key, ("inherit", []))
            editor.load(mode, names)
        self._saved_signature = {
            mode_key: editor_signature(editor)
            for mode_key, editor in self.editors.items()
        }
        # 签名就位后再刷一次计数：否则 load() 里的那次 update_detail 会把
        # "有未保存改动" 误标出来。
        for editor in self.editors.values():
            editor.update_detail()

    def _is_dirty(self) -> bool:
        """当前编辑与"最后一次载入/保存"是否不同。"""
        if not self._current_scope:
            return False
        for mode_key, editor in self.editors.items():
            saved = self._saved_signature.get(mode_key)
            if saved is None or editor_signature(editor) != saved:
                return True
        return False

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        scope = canonical_scope(self._current_scope)
        if not scope:
            message(self.frame, "保存", "先在左侧选一个群组，或填作用域后点「定位 / 新建」")
            return
        problem = scope_problem(scope)
        if problem:
            self.fail(ConfigEditError(problem))
            return
        try:
            paths = self.require_paths()
            saved: dict[str, tuple[str, list[str]]] = {}
            for mode_key in MODE_ORDER:
                mode, names = self.editors[mode_key].collect()
                save_scope_mode_policy(paths.state_path, scope, mode_key, mode, names)
                saved[mode_key] = (mode, names)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        detail = " / ".join(
            f"{mode_key}={saved[mode_key][0]}({len(saved[mode_key][1])} 个工具)"
            for mode_key in MODE_ORDER
        )
        self.journal(f"保存群工具授权：{scope} → {detail}")
        self._refresh_scopes(reload_editor=False)
        if self.scope_tree.exists(scope):
            self.scope_tree.selection_set(scope)
            self.scope_tree.see(scope)
        # 写盘后用落盘结果回填编辑器：保存状态与「有未保存改动」标记要一起归零。
        self._load_editor(scope, self._mode_scopes.get(scope) or {})
        self.notify(
            f"已保存 {scope} 的工具授权（chat / work 分开生效）；后台服务约 15 秒内热加载"
        )

    def _save_preview_switch(self) -> None:
        """兼容旧调用：保存页面上这个开关（等同于点「保存进度预览设置」）。"""
        if self.preview_panel is None:
            return
        self.preview_panel.save()

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


def _describe(mode: str, names: list[str]) -> str:
    if mode == "inherit":
        return "继承全局（不限制）"
    if mode == "none" or not names:
        return "禁止全部"
    return f"仅白名单（{len(names)}）：{'、'.join(names)}"


def _scope_summary(policies: dict[str, tuple[str, list[str]]]) -> str:
    """群列表里的摘要：``chat:继承 / work:白名单(5)``。"""
    parts: list[str] = []
    for mode_key in MODE_ORDER:
        mode, names = policies.get(mode_key, ("inherit", []))
        short = MODE_SHORT.get(mode, mode)
        if mode == "whitelist":
            short = f"白名单({len(names)})"
        parts.append(f"{mode_key}:{short}")
    return " / ".join(parts)


def _read_preview_switch(app_path: str) -> bool:
    """读 ``[platform.qqonebot].show_tool_preview_in_group``（缺省 False）。"""
    return read_platform_preview_switch(app_path, "platform.qqonebot")
