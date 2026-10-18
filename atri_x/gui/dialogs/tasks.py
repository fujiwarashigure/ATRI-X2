"""维护任务清单 + 「界面状态 ↔ cron」翻译（无 Tk，便于单独测试）。

「定时维护任务」窗口里的每个任务在 ``app.toml`` 都对应一个 ``[maintenance.<key>]``
小节；窗口只负责摆控件，任务清单、默认调度、表单状态和校验都收在这里：

- :data:`MAINTENANCE_TASKS` —— 8 个任务及其默认 cron、是否有保留期、提示语；
- :class:`TaskForm` —— 一个任务在界面上的待保存状态（开关、重复方式、时分、一次性时间）；
- :func:`schedule_of` / :func:`validate_schedule` —— 表单 → 要落盘的 cron（含一次性兜底）；
- :func:`load_schedule` —— 已有 cron → 表单状态（还原不了就走「自定义 cron」）；
- :func:`schedule_text` —— 列表里那一列的中文摘要。

cron 本身的结构化翻译在 :mod:`atri_x.gui.cronschedule`，这里只做表单层的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.cronschedule import (
    KIND_CUSTOM,
    KIND_DAILY,
    build_cron,
    describe_cron,
    parse_cron,
)
from atri_x.net.cronspec import CronParseError, parse_standard

__all__ = [
    "CRON_PRESETS",
    "MAINTENANCE_TASKS",
    "TASK_BY_KEY",
    "MaintenanceTask",
    "TaskForm",
    "clock_text",
    "form_cron",
    "load_schedule",
    "schedule_of",
    "schedule_text",
    "validate_schedule",
]

#: cron 预设（「自定义 cron」下拉里的常用写法）：(显示名, 表达式)。
CRON_PRESETS = (
    ("每天 03:00", "0 3 * * *"),
    ("每天 08:00", "0 8 * * *"),
    ("每天 09:00 与 21:00", "0 9,21 * * *"),
    ("每周一 03:00", "0 3 * * 1"),
    ("每月 1 日 03:00", "0 3 1 * *"),
    ("每 6 小时", "0 */6 * * *"),
)


@dataclass(frozen=True)
class MaintenanceTask:
    """界面上的一个维护任务。"""

    key: str
    label: str
    default_schedule: str
    has_retention: bool = False
    hint: str = ""


#: 维护任务清单（顺序即列表顺序）。
MAINTENANCE_TASKS: tuple[MaintenanceTask, ...] = (
    MaintenanceTask("log_cleanup", "日志清理", "0 3 * * *", hint="保留天数用 [runtime].log_retention_days"),
    MaintenanceTask("session_cleanup", "会话表清理", "15 3 * * *", True, "删未归档/未置顶的旧会话行"),
    MaintenanceTask("sandbox_cleanup", "沙箱目录清理", "0 4 * * *", True, "媒体清理也沿用这个调度"),
    MaintenanceTask("chat_history_cleanup", "聊天历史清理", "35 4 * * *", True, "清聊天历史库里的旧消息"),
    MaintenanceTask("dsh_cleanup", "DSH 内核会话清理", "50 4 * * *", True, "只清 DSH_HOME/sessions，不动 profiles"),
    MaintenanceTask("privacy_cleanup", "隐私数据清理", "45 4 * * *", hint="清理长期记忆/自学习的过期数据"),
    MaintenanceTask("daily_report", "资源/用量报告", "0 9,21 * * *", hint="Token/费用/磁盘/内存报告"),
    MaintenanceTask("daily_summary", "会话日总结", "0 8 * * *", hint="写 <数据目录>/daily_summaries/"),
)

#: 任务 key → 任务定义。
TASK_BY_KEY = {task.key: task for task in MAINTENANCE_TASKS}


@dataclass
class TaskForm:
    """一个任务在界面上的待保存状态。"""

    enabled: bool = False
    #: 是否一次性任务（勾上就写 ``run_at``，跑完不再重复）。
    once: bool = False
    #: 周期任务的重复方式（``daily``/``weekly``/``monthly``/``custom``）。
    kind: str = KIND_DAILY
    hour: str = "3"
    minute: str = "0"
    weekday: str = "1"
    day: str = "1"
    #: 重复方式选「自定义 cron」时的原始表达式。
    custom: str = ""
    date: str = ""
    time: str = ""
    retention: str = ""


# ---------------------------------------------------------------------------
# 表单 → cron
# ---------------------------------------------------------------------------
def form_cron(form: TaskForm) -> str:
    """按表单上的重复方式生成 cron（自定义方式原样返回，可能是非法文本）。"""
    if form.kind == KIND_CUSTOM:
        return form.custom.strip()
    return build_cron(
        form.kind,
        hour=form.hour or 0,
        minute=form.minute or 0,
        weekday=form.weekday or 0,
        day=form.day or 1,
    )


def schedule_of(form: TaskForm, task: MaintenanceTask) -> str:
    """表单 → 要写进 ``schedule`` 的 cron（一次性任务非法时退回默认表达式）。"""
    try:
        schedule = form_cron(form)
    except ConfigEditError:
        if not form.once:
            raise
        return task.default_schedule
    if not schedule:
        return task.default_schedule
    if form.once:
        try:
            parse_standard(schedule)
        except CronParseError:
            return task.default_schedule
    return schedule


def validate_schedule(form: TaskForm, task: MaintenanceTask) -> str:
    """校验并返回要保存的 cron；一次性任务允许用默认表达式兜底。"""
    try:
        schedule = schedule_of(form, task)
    except ConfigEditError as exc:
        raise ConfigEditError(f"{task.label} 的调度不合法：{exc}") from exc
    try:
        parse_standard(schedule)
    except CronParseError as exc:
        raise ConfigEditError(f"{task.label} 的 cron 表达式不合法：{exc}") from exc
    return schedule


# ---------------------------------------------------------------------------
# cron → 表单 / 列表文案
# ---------------------------------------------------------------------------
def load_schedule(form: TaskForm, schedule: str) -> None:
    """cron → 表单状态：能还原就还原成「每天/每周/每月」，否则走自定义。"""
    parsed = parse_cron(schedule)
    if parsed is None:
        form.kind = KIND_CUSTOM
        form.custom = schedule
        return
    form.kind = parsed["kind"]
    form.hour = f"{int(parsed['hour']):02d}"
    form.minute = f"{int(parsed['minute']):02d}"
    form.weekday = str(int(parsed.get("weekday", 1)))
    form.day = str(int(parsed.get("day", 1)))
    form.custom = ""


def schedule_text(form: TaskForm, task: MaintenanceTask) -> str:
    """列表里显示的调度摘要。"""
    if form.once:
        return f"一次性：{form.date} {form.time}".strip()
    try:
        return describe_cron(form_cron(form) or task.default_schedule)
    except ConfigEditError as exc:
        return f"待修正：{exc}"


def clock_text(value: Any, fallback: str) -> str:
    """时分下拉的显示值："3" → "03"；不是数字就用兜底。"""
    text = str(value or "").strip()
    return text.zfill(2) if text.isdigit() else fallback
