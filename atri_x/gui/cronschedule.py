"""界面上的 cron 小工具：「每天 / 每周 / 每月」结构 ↔ 5 段表达式。

「定时维护任务」窗口不想让用户直接写 cron，而是选「每天 / 每周 / 每月」再填时分、
周几、几号；这个模块负责那层翻译，以及列表里显示的人话和「下次触发」时间。

放在 ``gui`` 下而不是 ``net``：这里只有界面口径（中文标签、默认值），
真正的 cron 解析与调度在 :mod:`atri_x.net.cronspec`，两边不要混。

设计取舍：只有整除写法（单个分钟、单个小时、单个周几或单个几号）才还原成结构化
形式，列表/区间/步长表达式一律交给「自定义 cron」，避免界面把用户的配置改坏。
"""

from __future__ import annotations

import datetime
from typing import Any

from atri_x.gui.configedit import ConfigEditError
from atri_x.net.cronspec import CronParseError, Schedule, parse_standard

__all__ = [
    "KIND_CUSTOM",
    "KIND_DAILY",
    "KIND_LABELS",
    "KIND_MONTHLY",
    "KIND_WEEKLY",
    "WEEKDAYS",
    "build_cron",
    "describe_cron",
    "kind_of_label",
    "label_of_kind",
    "next_run_text",
    "parse_cron",
    "parse_once_at",
    "split_run_at",
    "weekday_label",
    "weekday_of_label",
]

#: 重复方式（不包含「一次性」：那是另一种调度口径，走 ``run_at``）。
KIND_DAILY = "daily"
KIND_WEEKLY = "weekly"
KIND_MONTHLY = "monthly"
KIND_CUSTOM = "custom"

#: 重复方式 → 下拉显示名（顺序即下拉顺序）。
KIND_LABELS = (
    ("每天", KIND_DAILY),
    ("每周", KIND_WEEKLY),
    ("每月", KIND_MONTHLY),
    ("自定义 cron", KIND_CUSTOM),
)

#: 星期几：cron 口径周日 = 0，周一到周六 = 1…6。
WEEKDAYS = (
    ("周一", 1),
    ("周二", 2),
    ("周三", 3),
    ("周四", 4),
    ("周五", 5),
    ("周六", 6),
    ("周日", 0),
)

_KIND_BY_LABEL = {label: kind for label, kind in KIND_LABELS}
_LABEL_BY_KIND = {kind: label for label, kind in KIND_LABELS}
_LABEL_BY_WEEKDAY = {str(value): label for label, value in WEEKDAYS}
_WEEKDAY_BY_LABEL = {label: str(value) for label, value in WEEKDAYS}


# ---------------------------------------------------------------------------
# 标签 ↔ 内部值
# ---------------------------------------------------------------------------
def kind_of_label(label: str) -> str:
    """下拉显示名 → 重复方式；认不出按「自定义」处理。"""
    return _KIND_BY_LABEL.get(str(label), KIND_CUSTOM)


def label_of_kind(kind: str) -> str:
    """重复方式 → 下拉显示名。"""
    return _LABEL_BY_KIND.get(str(kind), _LABEL_BY_KIND[KIND_CUSTOM])


def weekday_of_label(label: str) -> int:
    """「周一」这类标签 → cron 的周几数字（周日 = 0）。"""
    return int(_WEEKDAY_BY_LABEL.get(str(label), "1"))


def weekday_label(value: Any) -> str:
    """cron 的周几数字 → 「周一」这类标签。"""
    return _LABEL_BY_WEEKDAY.get(str(value), "周一")


# ---------------------------------------------------------------------------
# 结构 → cron
# ---------------------------------------------------------------------------
def build_cron(
    kind: str,
    *,
    hour: Any = 0,
    minute: Any = 0,
    weekday: Any = 1,
    day: Any = 1,
) -> str:
    """按「每天 / 每周 / 每月」生成 5 段 cron 表达式。"""
    minute_value = _int_in(minute, 0, 59, "分钟")
    hour_value = _int_in(hour, 0, 23, "小时")
    if kind == KIND_DAILY:
        return f"{minute_value} {hour_value} * * *"
    if kind == KIND_WEEKLY:
        weekday_value = _int_in(weekday, 0, 7, "星期")
        return f"{minute_value} {hour_value} * * {weekday_value % 7}"
    if kind == KIND_MONTHLY:
        day_value = _int_in(day, 1, 31, "日期")
        return f"{minute_value} {hour_value} {day_value} * *"
    raise ConfigEditError(f"未知的重复方式：{kind!r}")


# ---------------------------------------------------------------------------
# cron → 结构 / 人话
# ---------------------------------------------------------------------------
def parse_cron(expr: str) -> dict[str, Any] | None:
    """把 cron 还原成「每天 / 每周 / 每月 + 时分」；还原不了返回 ``None``。"""
    text = (expr or "").strip()
    fields = text.split()
    if len(fields) != 5:
        return None
    minute_text, hour_text, day_text, month_text, weekday_text = fields
    if month_text != "*":
        return None
    if not (minute_text.isdigit() and hour_text.isdigit()):
        return None
    minute = int(minute_text)
    hour = int(hour_text)
    if minute > 59 or hour > 23:
        return None
    if day_text == "*" and weekday_text == "*":
        return {"kind": KIND_DAILY, "hour": hour, "minute": minute}
    if day_text == "*" and weekday_text.isdigit():
        weekday = int(weekday_text)
        if weekday > 7:
            return None
        return {
            "kind": KIND_WEEKLY,
            "hour": hour,
            "minute": minute,
            "weekday": weekday % 7,
        }
    if weekday_text == "*" and day_text.isdigit():
        day = int(day_text)
        if day < 1 or day > 31:
            return None
        return {"kind": KIND_MONTHLY, "hour": hour, "minute": minute, "day": day}
    return None


def describe_cron(expr: str) -> str:
    """cron → 人话（例如 ``0 3 * * 1`` → ``每周一 03:00``）。"""
    text = (expr or "").strip()
    parsed = parse_cron(text)
    if parsed is None:
        return f"自定义：{text}" if text else "未设置"
    clock = f"{int(parsed['hour']):02d}:{int(parsed['minute']):02d}"
    if parsed["kind"] == KIND_DAILY:
        return f"每天 {clock}"
    if parsed["kind"] == KIND_WEEKLY:
        return f"每{weekday_label(parsed['weekday'])} {clock}"
    return f"每月 {int(parsed['day'])} 日 {clock}"


def next_run_text(cron: str, now: datetime.datetime | None = None) -> str:
    """根据 cron 算「下次触发」的本地时间文本（算不出来给个说明）。"""
    schedule: Schedule = parse_standard(cron)
    moment = schedule.next(now or datetime.datetime.now())
    if moment is None:
        return "5 年内不会触发"
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 一次性任务的时间
# ---------------------------------------------------------------------------
def parse_once_at(date_text: str, time_text: str) -> str:
    """把界面上的日期 + 时间拼成引擎要的 ``YYYY-MM-DD HH:MM:SS``。

    时间接受 ``HH:MM`` 与 ``HH:MM:SS`` 两种写法（后者允许手写秒）。
    """
    date_part = (date_text or "").strip()
    time_part = (time_text or "").strip()
    if not date_part or not time_part:
        raise ConfigEditError("一次性任务要同时填日期（YYYY-MM-DD）和时间（HH:MM）")
    try:
        datetime.datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError as exc:
        raise ConfigEditError(f"日期格式不对（要 %Y-%m-%d）：{date_part!r}") from exc
    parsed_time: datetime.datetime | None = None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed_time = datetime.datetime.strptime(time_part, fmt)
            break
        except ValueError:
            continue
    if parsed_time is None:
        raise ConfigEditError(f"时间格式不对（要 HH:MM 或 HH:MM:SS）：{time_part!r}")
    return f"{date_part} {parsed_time.strftime('%H:%M:%S')}"


def split_run_at(value: str) -> tuple[str, str]:
    """``YYYY-MM-DD HH:MM[:SS]`` → ``(日期, HH:MM)``；解析不了就原样放日期。"""
    date_part, _sep, time_part = value.partition(" ")
    if time_part.count(":") == 2:
        time_part = time_part.rsplit(":", 1)[0]
    return date_part, time_part


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _int_in(value: Any, low: int, high: int, label: str) -> int:
    """把界面上的数字文本收成 ``low..high`` 内的整数。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigEditError(f"{label}必须是整数：{value!r}") from exc
    if number < low or number > high:
        raise ConfigEditError(f"{label}超出范围 {low}-{high}：{number}")
    return number
