"""「工具参数」子选项卡的控件：每个工具一组可展开 / 收起的参数面板。

为什么单独一层：这一页要渲染十几组面板、上百个输入控件，而且"展开 / 收起、取值、校验、
写盘反馈"是同一套逻辑重复十几遍。数据（哪些键、什么类型、写哪个文件）在
:mod:`atri_x.gui.toolsettings`，这里只做界面：

- :class:`SettingPanel` —— 一组参数 = 一个面板：标题行（▸/▾ 开关 + 标题 + 影响哪些工具 +
  配置状态 + 「保存本组」按钮）+ 折叠起来的字段区；
- :class:`ToolParamsEditor` —— 整页：顶部按钮条（保存全部 / 全部展开 / 全部收起 / 重新载入）、
  全局面板、其余「无工具」面板（内容安全的 chat / work 覆盖）、按工具分类排列的各组面板、
  底部"其余工具没有可调参数"说明。

一个字段的界面控件由 ``SettingField.kind`` 决定（对应关系见
:mod:`atri_x.gui.toolsettings_base` 的模块说明）；每个字段下面还有一行灰色说明，包含
"写入哪个文件的哪个小节 / 键"，避免用户不知道保存去了哪里。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Mapping

from atri_x.gui import widgets
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.toolcatalog import tool_meta
from atri_x.gui.toolsettings import (
    GROUPS,
    SettingField,
    SettingGroup,
    global_groups,
    groups_by_category,
    groups_for_tool,
    load_all,
    load_group,
    save_group,
    status_text,
    uncovered_tools,
)
from atri_x.gui.toolsettings_base import TRILABELS

__all__ = ["SettingPanel", "ToolParamsEditor"]

#: 说明文字的自动换行宽度：参数页会隐藏左侧群列表（全屏宽度），这里取常见的窄栏宽度
#: 换行；窗口再窄时靠竖向滚动，不横向滚动。
_HELP_WRAP = 600

_TITLE_FONT = ("Microsoft YaHei UI", 10, "bold")
_SECTION_FONT = ("Microsoft YaHei UI", 9, "bold")
_SMALL_FONT = ("Microsoft YaHei UI", 9)

#: 未保存改动 / 正常状态的前景色。
_DIRTY_FG = "#a86500"
_CLEAN_FG = "#555555"


class SettingPanel:
    """一组参数的可折叠面板。"""

    def __init__(self, page: Any, parent: Any, group: SettingGroup) -> None:
        self.page = page
        self.group = group
        self.expanded = False
        #: 最近一次成功保存真正改动的文件名（保存全部时汇总用）。
        self.last_changed: list[str] = []
        self._vars: dict[str, tk.Variable] = {}
        self._texts: dict[str, tk.Text] = {}
        self._choice_maps: dict[str, dict[str, str]] = {}
        self._choices: dict[str, ttk.Combobox] = {}
        self._loaded: dict[str, Any] = {}
        self._snapshot: dict[str, Any] = {}

        self.frame = ttk.LabelFrame(parent, padding=(8, 3, 8, 5))
        self.frame.pack(fill=tk.X, padx=2, pady=(0, 4))
        self.header = ttk.Frame(self.frame)
        self.header.pack(fill=tk.X)
        self.toggle_button = ttk.Button(
            self.header, text="▸", width=3, command=self.toggle
        )
        self.toggle_button.pack(side=tk.LEFT)
        ttk.Label(self.header, text=group.title, font=_TITLE_FONT).pack(
            side=tk.LEFT, padx=(2, 8)
        )
        ttk.Label(
            self.header,
            text=group.tools_label,
            font=_SMALL_FONT,
            foreground="#8a8f98",
            wraplength=240,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT)
        self.status_label = ttk.Label(
            self.header, text="", font=_SMALL_FONT, foreground=_CLEAN_FG
        )
        self.status_label.pack(side=tk.LEFT, padx=(10, 0))
        self.save_button = ttk.Button(
            self.header, text="保存本组", command=self._save_clicked
        )
        self.save_button.pack(side=tk.RIGHT)
        self.body = ttk.Frame(self.frame)
        self._build_body()
        # 建好控件先记一份"初始快照"：还没 load() 时不该被当成"有未保存改动"。
        self._snapshot = self._collect()

    # -- 构建 ---------------------------------------------------------------
    def _build_body(self) -> None:
        inner = ttk.Frame(self.body, padding=(22, 4, 2, 2))
        inner.pack(fill=tk.X)
        inner.columnconfigure(1, weight=1)
        row = 0
        for field in self.group.fields:
            row = self._build_field(inner, field, row)
        for text, prefix in (
            (self.group.note, "说明："),
            (self.group.where, "另外也在这里改："),
        ):
            if not text:
                continue
            ttk.Label(
                inner,
                text=prefix + text,
                font=_SMALL_FONT,
                foreground="#8a8a8a",
                wraplength=_HELP_WRAP,
                justify=tk.LEFT,
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

    def _build_field(self, parent: Any, field: SettingField, row: int) -> int:
        ttk.Label(parent, text=field.label, anchor="w").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=(2, 0)
        )
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        self._build_input(holder, field)
        ttk.Label(
            parent,
            text=self._help_text(field),
            font=_SMALL_FONT,
            foreground="#8a8a8a",
            wraplength=_HELP_WRAP,
            justify=tk.LEFT,
        ).grid(row=row + 1, column=0, columnspan=2, sticky="w", pady=(0, 2))
        return row + 2

    def _help_text(self, field: SettingField) -> str:
        text = field.help.strip()
        default = _default_text(field)
        if default:
            text = f"{text}（默认 {default}）" if text else f"默认 {default}"
        location = f"写入 {field.file_name} [{field.section}] {field.key}"
        return f"{text}　·　{location}" if text else location

    def _build_input(self, row: Any, field: SettingField) -> None:
        kind = field.kind
        if kind == "bool":
            variable = tk.BooleanVar(value=False)
            self._vars[field.key] = variable
            ttk.Checkbutton(row, text="开启", variable=variable).pack(side=tk.LEFT)
            return
        if kind in ("tristate", "choice"):
            variable = tk.StringVar()
            self._vars[field.key] = variable
            if kind == "tristate":
                pairs = TRILABELS
            else:
                pairs = field.choices
            labels = [label for _value, label in pairs]
            self._choice_maps[field.key] = {label: value for value, label in pairs}
            combo = ttk.Combobox(
                row, textvariable=variable, values=labels, state="readonly", width=24
            )
            combo.pack(side=tk.LEFT)
            self._choices[field.key] = combo
            return
        if kind == "text":
            widget = tk.Text(
                row, height=3, width=52, wrap=tk.WORD, font=_SMALL_FONT
            )
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._texts[field.key] = widget
            return
        variable = tk.StringVar()
        self._vars[field.key] = variable
        ttk.Entry(row, textvariable=variable, width=40).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        if kind == "path":
            ttk.Button(
                row, text="浏览…", width=8, command=lambda item=field: self._browse(item)
            ).pack(side=tk.LEFT, padx=(4, 0))

    # -- 展开 / 收起 ---------------------------------------------------------
    def toggle(self) -> None:
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        if self.expanded:
            return
        self.body.pack(fill=tk.X, pady=(4, 0))
        self.expanded = True
        self.toggle_button.configure(text="▾")

    def collapse(self) -> None:
        if not self.expanded:
            return
        self.body.pack_forget()
        self.expanded = False
        self.toggle_button.configure(text="▸")

    # -- 数据 ---------------------------------------------------------------
    def load(self, values: Mapping[str, Any]) -> None:
        """按文件里的值回填控件（``None`` = 该键没写，界面上留空或用默认）。"""
        self._loaded = dict(values)
        for field in self.group.fields:
            self._set_widget(field, values.get(field.key))
        self.last_changed = []
        self._snapshot = self._collect()
        self._refresh_status()

    def _set_widget(self, field: SettingField, raw: Any) -> None:
        kind = field.kind
        if kind == "bool":
            variable = self._vars[field.key]
            variable.set(bool(raw) if raw is not None else bool(field.default))
            return
        if kind == "tristate":
            value = None if raw is None else bool(raw)
            label = "（默认）" if value is None else ("开" if value else "关")
            self._vars[field.key].set(label)
            return
        if kind == "choice":
            value = "" if raw is None else str(raw)
            label = field.choice_label(value)
            combo = self._choices.get(field.key)
            if combo is not None:
                labels = [text for _value, text in field.choices]
                if label not in labels:
                    # 文件里可能有界面没列出的值（工具自己接受更多档位）：原样显示，
                    # 不悄悄改成第一项；用户不动它就不会被改写。
                    labels = labels + [label]
                    self._choice_maps[field.key][label] = value
                combo.configure(values=labels)
            self._vars[field.key].set(label)
            return
        if kind == "text":
            widget = self._texts[field.key]
            widget.delete("1.0", tk.END)
            if raw is not None:
                widget.insert("1.0", str(raw))
            return
        if kind == "list":
            text = "" if raw is None else ", ".join(str(item) for item in raw)
            self._vars[field.key].set(text)
            return
        self._vars[field.key].set("" if raw is None else str(raw))

    def _collect(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field in self.group.fields:
            kind = field.kind
            if kind == "bool":
                values[field.key] = bool(self._vars[field.key].get())
            elif kind in ("tristate", "choice"):
                label = str(self._vars[field.key].get())
                mapping = self._choice_maps.get(field.key, {})
                values[field.key] = mapping.get(label, label)
            elif kind == "text":
                values[field.key] = self._texts[field.key].get("1.0", tk.END).strip()
            else:
                values[field.key] = str(self._vars[field.key].get()).strip()
        return values

    def dirty(self) -> bool:
        """界面上的值是否与最后一次载入 / 保存不同。"""
        return self._collect() != self._snapshot

    def _refresh_status(self) -> None:
        if self.dirty():
            self.status_label.configure(text="有未保存改动", foreground=_DIRTY_FG)
        else:
            self.status_label.configure(
                text=status_text(self.group, self._loaded), foreground=_CLEAN_FG
            )

    # -- 保存 ---------------------------------------------------------------
    def _save_clicked(self) -> None:
        self.save()

    def save(self, *, notify: bool = True) -> bool:
        """写回本组**改动过的**字段；失败时弹窗并返回 ``False``（一个字节都不写）。

        只提交改动过的字段（不是整组重写）：这样保存一个键不会把同小节其它键的行内注释
        一起冲掉，也不会把用户手写在文件里的、界面不认识的取值顺手改掉。
        """
        if not self.page.is_ready():
            self.page.fail("找不到 app.toml，无法保存工具参数")
            return False
        current = self._collect()
        changes = {
            key: value
            for key, value in current.items()
            if value != self._snapshot.get(key)
        }
        if not changes:
            self.page.notify(f"「{self.group.title}」没有改动")
            return True
        try:
            paths = self.page.require_paths()
            changed = save_group(paths, self.group, changes)
            values = load_group(paths, self.group)
        except ConfigEditError as exc:
            self.page.fail(exc)
            return False
        self.load(values)
        self.last_changed = changed
        if notify:
            self._announce()
        return True

    def _announce(self) -> None:
        if not self.last_changed:
            self.page.notify(f"「{self.group.title}」没有改动")
            return
        files = " / ".join(self.last_changed)
        self.page.journal(f"保存工具参数：{self.group.title} → {files}")
        self.page.notify(
            f"已保存「{self.group.title}」到 {files}；重启后台服务后生效"
        )

    # -- 路径字段 -----------------------------------------------------------
    def _browse(self, field: SettingField) -> None:
        current = str(self._vars[field.key].get()).strip()
        directory = ""
        try:
            directory = self.page.require_paths().config_dir
        except Exception:  # noqa: BLE001 - 没有配置文件时退回当前目录
            directory = ""
        initial_dir = os.path.dirname(current) if current else directory
        if initial_dir and not os.path.isdir(initial_dir):
            initial_dir = directory
        try:
            if field.path_kind == "dir":
                chosen = filedialog.askdirectory(
                    parent=self.frame, title=f"选择{field.label}", initialdir=initial_dir or "."
                )
            else:
                chosen = filedialog.asksaveasfilename(
                    parent=self.frame,
                    title=f"选择{field.label}",
                    initialdir=initial_dir or ".",
                    initialfile=os.path.basename(current),
                )
        except tk.TclError:  # pragma: no cover - 窗口销毁
            return
        if chosen:
            self._vars[field.key].set(chosen)


class ToolParamsEditor:
    """「工具参数（全局）」整页：工具参数面板列表 + 保存 / 展开 / 重新载入。"""

    def __init__(self, page: Any, parent: Any) -> None:
        self.page = page
        self.frame = ttk.Frame(parent, padding=(8, 6))
        self.panels: dict[str, SettingPanel] = {}
        self._build()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        ttk.Label(
            self.frame,
            text=(
                "这里改的是工具的内部参数（整机生效，与群无关）：点面板标题的 ▸ 展开 / "
                "收起，改完点「保存本组」或「保存全部改动」。这些键在 app.toml / "
                "services.toml，保存后需要重启后台服务。"
            ),
            foreground="#555555",
            wraplength=_HELP_WRAP + 100,
            justify=tk.LEFT,
        ).pack(anchor="w")

        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X, pady=(6, 4))
        ttk.Button(bar, text="保存全部改动", command=self.save_all).pack(side=tk.LEFT)
        ttk.Button(bar, text="全部展开", command=self.expand_all).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(bar, text="全部收起", command=self.collapse_all).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(bar, text="重新载入", command=self.reload).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.summary_label = ttk.Label(
            bar, text="", foreground="#555555", font=_SMALL_FONT
        )
        self.summary_label.pack(side=tk.RIGHT)

        self.scroll = widgets.ScrollFrame(self.frame, height=300)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        body = self.scroll.inner

        first = GROUPS[0]
        self.panels[first.key] = SettingPanel(self.page, body, first)
        # 其余「无工具」面板（内容安全的 chat / work 覆盖）紧跟全局面板、排在各工具分类
        # 之前：它们同样是整机生效的配置，不能混进「按工具分类」的分组里。
        for group in global_groups()[1:]:
            self.panels[group.key] = SettingPanel(self.page, body, group)
        for category, groups in groups_by_category():
            ttk.Label(
                body, text=f"— {category} —", font=_SECTION_FONT, foreground="#39424e"
            ).pack(anchor="w", padx=(4, 0), pady=(8, 2))
            for group in groups:
                self.panels[group.key] = SettingPanel(self.page, body, group)

        self.footer_label = ttk.Label(
            body,
            text="",
            font=_SMALL_FONT,
            foreground="#8a8a8a",
            wraplength=_HELP_WRAP + 60,
            justify=tk.LEFT,
        )
        self.footer_label.pack(anchor="w", padx=(4, 0), pady=(10, 4))
        self._refresh_footer()
        self.refresh_summary()
        self.panels[first.key].expand()

    def _refresh_footer(self) -> None:
        missing = [f"{tool_meta(name).title}（{name}）" for name in uncovered_tools()]
        if not missing:
            self.footer_label.configure(text="")
            return
        self.footer_label.configure(
            text="其余工具没有可调参数（只受工具授权 / 风险等级控制）：" + "、".join(missing)
        )

    def refresh_summary(self) -> None:
        expanded = sum(1 for panel in self.panels.values() if panel.expanded)
        dirty = len(self.dirty_panels())
        self.summary_label.configure(
            text=f"{len(self.panels)} 组参数 · 已展开 {expanded} · 有未保存改动 {dirty} 组"
        )

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        """重新读文件并回填所有面板（工具权限页的「重新载入」也会调它）。"""
        if not self.page.is_ready():
            return
        try:
            data = load_all(self.page.require_paths())
        except ConfigEditError as exc:
            self.page.fail(exc)
            return
        for key, panel in self.panels.items():
            panel.load(data.get(key, {}))
        self.refresh_summary()

    def dirty_panels(self) -> list[SettingPanel]:
        """有未保存改动的面板（按界面顺序）。"""
        return [panel for panel in self.panels.values() if panel.dirty()]

    def save_all(self) -> None:
        """保存所有有改动的面板；遇到第一个错误就停下（错误消息由面板弹出）。"""
        if not self.page.is_ready():
            self.page.fail("找不到 app.toml，无法保存工具参数")
            return
        pending = self.dirty_panels()
        if not pending:
            self.page.notify("工具参数没有未保存的改动")
            return
        changed: list[str] = []
        titles: list[str] = []
        for panel in pending:
            if not panel.save(notify=False):
                return
            titles.append(panel.group.title)
            changed.extend(panel.last_changed)
        self.refresh_summary()
        files = " / ".join(dict.fromkeys(changed)) or "（无实际改动）"
        self.page.journal(f"保存工具参数（{len(titles)} 组）：{files}")
        self.page.notify(
            f"已保存 {len(titles)} 组工具参数到 {files}；重启后台服务后生效"
        )

    # -- 展开 / 定位 ---------------------------------------------------------
    def expand_all(self) -> None:
        for panel in self.panels.values():
            panel.expand()
        self.refresh_summary()

    def collapse_all(self) -> None:
        for panel in self.panels.values():
            panel.collapse()
        self.refresh_summary()

    def reveal_tool(self, tool_name: str) -> bool:
        """展开某个工具对应的面板并滚到它（从工具行的「参数」按钮进来时用）。"""
        groups = groups_for_tool(tool_name)
        if not groups:
            return False
        chosen: list[SettingPanel] = []
        for group in groups:
            panel = self.panels.get(group.key)
            if panel is None:
                continue
            panel.expand()
            chosen.append(panel)
        if not chosen:
            return False
        self.refresh_summary()
        self.scroll.scroll_to_widget(chosen[0].frame)
        return True


def _default_text(field: SettingField) -> str:
    """字段默认值的一行短说明（``None`` / 空串不显示）。"""
    value = field.default
    if value is None or value == "":
        return ""
    if field.kind == "bool":
        return "开" if value else "关"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)
