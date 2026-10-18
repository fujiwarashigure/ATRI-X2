"""「定时维护任务」子窗口：编辑 ``app.toml`` 的 ``[maintenance.*]``。

维护任务有两种调度口径，界面都支持：

- **周期任务**：先选重复方式，再填具体时间——
  ``每天``（几点几分）、``每周``（周几 + 几点几分）、``每月``（几号 + 几点几分），
  界面实时生成对应的 5 段 cron 并显示「下次触发」时间；
  cron 里表达不了的写法（每 6 小时、一天两个时间点等）选 ``自定义 cron`` 手填；
- **指定日期**：填日期 + 时间，写成一次性任务（配置键 ``run_at``，后端按
  ``metadata.schedule.mode = "once"`` 定点触发，跑完不再重复）。

每个任务还能单独开关、设置保留天数（只有清理类任务有保留期）。``media_cleanup``
是硬编码启用的隐式任务，沿用沙箱清理的调度，界面上只做说明、不单独列出。

任务清单与「表单 ↔ cron」的翻译在 :mod:`atri_x.gui.dialogs.tasks`，结构化调度的
翻译与「下次触发」计算在 :mod:`atri_x.gui.cronschedule`；本模块只管摆控件。
保存写回 ``app.toml``；维护任务由后台服务启动时装配，所以保存后需要重启后台服务。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml
from atri_x.gui.cronschedule import (
    KIND_CUSTOM,
    KIND_DAILY,
    KIND_LABELS,
    KIND_MONTHLY,
    KIND_WEEKLY,
    WEEKDAYS,
    build_cron,
    describe_cron,
    kind_of_label,
    label_of_kind,
    next_run_text,
    parse_once_at,
    split_run_at,
    weekday_label,
    weekday_of_label,
)
from atri_x.gui.dialogs.common import Dialog
from atri_x.gui.dialogs.tasks import (
    CRON_PRESETS,
    MAINTENANCE_TASKS,
    TASK_BY_KEY,
    TaskForm,
    clock_text,
    load_schedule,
    schedule_text,
    validate_schedule,
)
from atri_x.net.cronspec import CronParseError

__all__ = ["MaintenanceDialog"]

#: 「每天 / 每周 / 每月」用的时分下拉候选。
_HOURS = tuple(f"{hour:02d}" for hour in range(24))
_MINUTES = tuple(f"{minute:02d}" for minute in range(60))
_DAYS = tuple(str(day) for day in range(1, 32))


class MaintenanceDialog(Dialog):
    """定时维护任务编辑窗口。"""

    title = "定时维护任务（app.toml [maintenance.*]）"
    geometry = "960x640"

    def build(self) -> None:
        self._forms: dict[str, TaskForm] = {}
        self._current = ""
        self._updating = False

        outer = ttk.Panedwindow(self.body, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(outer)
        outer.add(left, weight=1)
        columns = ("task", "status", "schedule")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=14)
        self.tree.heading("task", text="任务")
        self.tree.heading("status", text="状态")
        self.tree.heading("schedule", text="调度")
        self.tree.column("task", width=140, stretch=False)
        self.tree.column("status", width=56, stretch=False)
        self.tree.column("schedule", width=250)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("on", foreground="#1b5e20")
        self.tree.tag_configure("off", foreground="#8a8a8a")

        right = ttk.Frame(outer, padding=(10, 0, 0, 0))
        outer.add(right, weight=1)
        right.columnconfigure(1, weight=1)
        self.title_label = ttk.Label(right, text="", font=("Microsoft YaHei UI", 10, "bold"))
        self.title_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="启用这个任务", variable=self.enabled_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )

        self.mode_var = tk.StringVar(value="period")
        mode_row = ttk.Frame(right)
        mode_row.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="周期任务",
            value="period",
            variable=self.mode_var,
            command=self._refresh_fields,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_row,
            text="指定日期（一次性）",
            value="once",
            variable=self.mode_var,
            command=self._refresh_fields,
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._build_period_frame(right)
        self._build_once_frame(right)

        self.preview_label = ttk.Label(
            right, text="", foreground="#1b5e20", wraplength=440, justify=tk.LEFT
        )
        self.preview_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(right, text="保留天数").grid(row=6, column=0, sticky="w", pady=3)
        self.retention_var = tk.StringVar()
        self.retention_entry = ttk.Entry(right, textvariable=self.retention_var, width=10)
        self.retention_entry.grid(row=6, column=1, sticky="w", pady=3)

        self.hint_label = ttk.Label(
            right, text="", foreground="#555555", wraplength=440, justify=tk.LEFT
        )
        self.hint_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(
            right,
            text=(
                "「每天 / 每周 / 每月」会自动生成 cron；表达不了的写法选「自定义 cron」。"
                "「指定日期」到点只跑一次，跑完不再重复（引擎仍要求 cron 合法，会保留周期表达式兜底）。"
                "保存后需要重启后台服务。"
            ),
            foreground="#8a8a8a",
            wraplength=440,
            justify=tk.LEFT,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))

        for variable in (
            self.enabled_var,
            self.kind_var,
            self.weekday_var,
            self.day_var,
            self.hour_var,
            self.minute_var,
            self.custom_var,
            self.date_var,
            self.time_var,
        ):
            variable.trace_add("write", self._on_var_changed)

        self.button("保存全部", self._save)
        self.button("重新载入", self.reload)
        self._refresh_fields()

    # -- 构建 ---------------------------------------------------------------
    def _build_period_frame(self, right: Any) -> None:
        self._period_frame = ttk.LabelFrame(right, text="周期设置", padding=8)
        self._period_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._period_frame.columnconfigure(1, weight=1)

        row = ttk.Frame(self._period_frame)
        row.grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(row, text="重复方式").pack(side=tk.LEFT)
        self.kind_var = tk.StringVar(value=label_of_kind(KIND_DAILY))
        self.kind_box = ttk.Combobox(
            row,
            textvariable=self.kind_var,
            values=[label for label, _kind in KIND_LABELS],
            state="readonly",
            width=14,
        )
        self.kind_box.pack(side=tk.LEFT, padx=(6, 0))
        self.kind_box.bind("<<ComboboxSelected>>", self._refresh_fields)

        self._weekday_row = ttk.Frame(self._period_frame)
        self._weekday_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(self._weekday_row, text="星期几").pack(side=tk.LEFT)
        self.weekday_var = tk.StringVar(value=WEEKDAYS[0][0])
        ttk.Combobox(
            self._weekday_row,
            textvariable=self.weekday_var,
            values=[label for label, _value in WEEKDAYS],
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT, padx=(6, 0))

        self._day_row = ttk.Frame(self._period_frame)
        self._day_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(self._day_row, text="每月几号").pack(side=tk.LEFT)
        self.day_var = tk.StringVar(value="1")
        ttk.Combobox(
            self._day_row,
            textvariable=self.day_var,
            values=_DAYS,
            state="readonly",
            width=6,
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(
            self._day_row,
            text="号（小月没有这天时会顺延到下一个有它的月份）",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(4, 0))

        self._time_row = ttk.Frame(self._period_frame)
        self._time_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(self._time_row, text="时间").pack(side=tk.LEFT)
        self.hour_var = tk.StringVar(value="03")
        ttk.Combobox(
            self._time_row,
            textvariable=self.hour_var,
            values=_HOURS,
            state="readonly",
            width=4,
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(self._time_row, text="时").pack(side=tk.LEFT, padx=(2, 0))
        self.minute_var = tk.StringVar(value="00")
        ttk.Combobox(
            self._time_row,
            textvariable=self.minute_var,
            values=_MINUTES,
            state="readonly",
            width=4,
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(self._time_row, text="分").pack(side=tk.LEFT, padx=(2, 0))

        self._custom_row = ttk.Frame(self._period_frame)
        self._custom_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(self._custom_row, text="cron 表达式").pack(side=tk.LEFT)
        self.custom_var = tk.StringVar()
        self.custom_entry = ttk.Entry(self._custom_row, textvariable=self.custom_var, width=18)
        self.custom_entry.pack(side=tk.LEFT, padx=(6, 0))
        self.preset_box = ttk.Combobox(
            self._custom_row,
            values=[name for name, _expr in CRON_PRESETS],
            state="readonly",
            width=18,
        )
        self.preset_box.pack(side=tk.LEFT, padx=(6, 0))
        self.preset_box.bind("<<ComboboxSelected>>", self._apply_preset)

    def _build_once_frame(self, right: Any) -> None:
        self._once_frame = ttk.LabelFrame(right, text="一次性时间（本地时间）", padding=8)
        self._once_frame.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(self._once_frame, text="日期").pack(side=tk.LEFT)
        self.date_var = tk.StringVar()
        ttk.Entry(self._once_frame, textvariable=self.date_var, width=12).pack(
            side=tk.LEFT, padx=(6, 10)
        )
        ttk.Label(self._once_frame, text="时间").pack(side=tk.LEFT)
        self.time_var = tk.StringVar()
        ttk.Entry(self._once_frame, textvariable=self.time_var, width=7).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(self._once_frame, text="（YYYY-MM-DD / HH:MM）", foreground="#8a8a8a").pack(
            side=tk.LEFT, padx=(8, 0)
        )

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        self._forms = {}
        if not self.is_ready():
            return
        try:
            data = read_toml(self.require_paths().config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        maintenance = data.get("maintenance")
        maintenance = maintenance if isinstance(maintenance, dict) else {}
        for task in MAINTENANCE_TASKS:
            entry = maintenance.get(task.key)
            entry = entry if isinstance(entry, dict) else {}
            form = TaskForm()
            form.enabled = bool(entry.get("enabled"))
            schedule = entry.get("schedule")
            schedule = (
                schedule.strip()
                if isinstance(schedule, str) and schedule.strip()
                else task.default_schedule
            )
            load_schedule(form, schedule)
            run_at = entry.get("run_at")
            if isinstance(run_at, str) and run_at.strip():
                form.once = True
                form.date, form.time = split_run_at(run_at.strip())
            retention = entry.get("retention_days")
            form.retention = "" if retention in (None, "") else str(retention)
            self._forms[task.key] = form
        self._refresh_tree()
        keys = self.tree.get_children("")
        if keys:
            self.tree.selection_set(keys[0])
            self._load_form(keys[0])
        self.notify("已载入维护任务配置（改动需要重启后台服务）")

    def _refresh_tree(self) -> None:
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children(""))
        for task in MAINTENANCE_TASKS:
            form = self._forms.get(task.key)
            if form is None:
                continue
            status = "已启用" if form.enabled else "已停用"
            self.tree.insert(
                "",
                tk.END,
                iid=task.key,
                values=(task.label, status, schedule_text(form, task)),
                tags=("on" if form.enabled else "off",),
            )
        for item in selected:
            if self.tree.exists(item):
                self.tree.selection_set(item)

    # -- 表单 ---------------------------------------------------------------
    def _on_select(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self._commit_form()
        self._load_form(str(selection[0]))

    def _load_form(self, key: str) -> None:
        task = TASK_BY_KEY.get(key)
        form = self._forms.get(key)
        if task is None or form is None:
            return
        self._current = key
        self._updating = True
        try:
            self.title_label.configure(text=task.label)
            self.enabled_var.set(form.enabled)
            self.mode_var.set("once" if form.once else "period")
            self.kind_var.set(label_of_kind(form.kind))
            self.weekday_var.set(weekday_label(form.weekday))
            self.day_var.set(form.day if form.day.isdigit() else "1")
            self.hour_var.set(clock_text(form.hour, "03"))
            self.minute_var.set(clock_text(form.minute, "00"))
            self.custom_var.set(form.custom)
            self.date_var.set(form.date)
            self.time_var.set(form.time)
            self.retention_var.set(form.retention)
            self.retention_entry.configure(
                state=tk.NORMAL if task.has_retention else tk.DISABLED
            )
            hint = f"[maintenance.{task.key}]"
            if task.hint:
                hint += f" · {task.hint}"
            if not task.has_retention:
                hint += " · 这个任务没有单独保留期"
            self.hint_label.configure(text=hint)
        finally:
            self._updating = False
        self._refresh_fields()

    def _commit_form(self) -> None:
        form = self._forms.get(self._current)
        if form is None:
            return
        form.enabled = bool(self.enabled_var.get())
        form.once = self.mode_var.get() == "once"
        form.kind = kind_of_label(self.kind_var.get())
        form.hour = self.hour_var.get().strip()
        form.minute = self.minute_var.get().strip()
        form.weekday = str(weekday_of_label(self.weekday_var.get()))
        form.day = self.day_var.get().strip()
        form.custom = self.custom_var.get().strip()
        form.date = self.date_var.get().strip()
        form.time = self.time_var.get().strip()
        form.retention = self.retention_var.get().strip()

    # -- 显示联动 -----------------------------------------------------------
    def _on_var_changed(self, *_args: Any) -> None:
        if self._updating:
            return
        self._refresh_preview()

    def _refresh_fields(self, _event: Any = None) -> None:
        """按「周期 / 一次性」和重复方式显示对应的输入行。"""
        once = self.mode_var.get() == "once"
        if once:
            self._period_frame.grid_remove()
            self._once_frame.grid()
        else:
            self._once_frame.grid_remove()
            self._period_frame.grid()
            kind = kind_of_label(self.kind_var.get())
            for row, visible in (
                (self._weekday_row, kind == KIND_WEEKLY),
                (self._day_row, kind == KIND_MONTHLY),
                (self._time_row, kind != KIND_CUSTOM),
                (self._custom_row, kind == KIND_CUSTOM),
            ):
                if visible:
                    row.grid()
                else:
                    row.grid_remove()
        self._refresh_preview()

    def _cron_from_vars(self) -> str:
        """按界面上当前的选择生成 cron（自定义方式原样返回）。"""
        kind = kind_of_label(self.kind_var.get())
        if kind == KIND_CUSTOM:
            text = self.custom_var.get().strip()
            if not text:
                raise ConfigEditError("选「自定义 cron」时要填表达式，例如 0 3 * * *")
            return text
        return build_cron(
            kind,
            hour=self.hour_var.get(),
            minute=self.minute_var.get(),
            weekday=weekday_of_label(self.weekday_var.get()),
            day=self.day_var.get(),
        )

    def _refresh_preview(self) -> None:
        """实时显示生成的 cron / 一次性时间 + 下次触发时间。"""
        try:
            if self.mode_var.get() == "once":
                moment = parse_once_at(self.date_var.get(), self.time_var.get())
                text = f"一次性：{moment}（本地时间，到点跑一次）"
            else:
                cron = self._cron_from_vars()
                text = f"{describe_cron(cron)} · cron：{cron} · 下次触发：{next_run_text(cron)}"
        except (ConfigEditError, CronParseError) as exc:
            text = f"待修正：{exc}"
        self.preview_label.configure(text=text)
        self._refresh_current_row()

    def _refresh_current_row(self) -> None:
        """把列表里当前任务那一行改成"待保存"的样子（状态 + 调度摘要）。"""
        key = self._current
        task = TASK_BY_KEY.get(key)
        if task is None or not key or not self.tree.exists(key):
            return
        enabled = bool(self.enabled_var.get())
        try:
            if self.mode_var.get() == "once":
                stamp = f"{self.date_var.get().strip()} {self.time_var.get().strip()}".strip()
                text = f"一次性：{stamp}"
            else:
                text = describe_cron(self._cron_from_vars())
        except (ConfigEditError, CronParseError):
            text = "待修正"
        self.tree.item(
            key,
            values=(task.label, "已启用" if enabled else "已停用", text),
            tags=("on" if enabled else "off",),
        )

    def _apply_preset(self, _event: Any = None) -> None:
        index = self.preset_box.current()
        if 0 <= index < len(CRON_PRESETS):
            self._updating = True
            try:
                self.kind_var.set(label_of_kind(KIND_CUSTOM))
                self.custom_var.set(CRON_PRESETS[index][1])
            finally:
                self._updating = False
            self._refresh_fields()

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        self._commit_form()
        try:
            values = self._values()
        except ConfigEditError as exc:
            self.fail(exc)
            return
        path = self.require_paths().config_path
        try:
            for task in MAINTENANCE_TASKS:
                patch_section(path, f"maintenance.{task.key}", values[task.key])
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._refresh_tree()
        self.saved("已保存 maintenance 配置；重启后台服务后生效")

    def _values(self) -> dict[str, dict[str, Any]]:
        """把界面状态转成每个小节的补丁（含校验）。"""
        result: dict[str, dict[str, Any]] = {}
        for task in MAINTENANCE_TASKS:
            form = self._forms.get(task.key)
            if form is None:
                continue
            schedule = validate_schedule(form, task)
            run_at = parse_once_at(form.date, form.time) if form.once else ""
            values: dict[str, Any] = {
                "enabled": bool(form.enabled),
                "schedule": schedule,
                "run_at": run_at or None,
            }
            if task.has_retention:
                raw = form.retention.strip()
                if raw:
                    try:
                        days = int(raw)
                    except ValueError as exc:
                        raise ConfigEditError(f"{task.label} 的保留天数必须是整数：{raw!r}") from exc
                    if days < 0:
                        raise ConfigEditError(f"{task.label} 的保留天数不能是负数")
                    values["retention_days"] = days
                else:
                    values["retention_days"] = None
            result[task.key] = values
        return result
