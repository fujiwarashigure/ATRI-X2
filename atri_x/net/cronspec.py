"""cron 表达式解析与下次触发时间：对应 Go 的 ``robfig/cron/v3``。

Python 标准库没有 cron 解析，本模块补齐 ATRI-X 定时任务所需的部分：

* 5 段（分 时 日 月 周）与 6 段（秒 分 时 日 月 周）表达式、``@yearly`` 等描述符、
  ``@every <go duration>``；
* 字段语法 ``* ? a a-b a-b/c */c a,c`` 与 ``JAN-DEC`` / ``SUN-SAT`` 名称；
* **DOM/DOW OR 语义**（与 ``robfig/cron/v3`` 及标准 cron 一致）：日与周同时受限时，
  命中任一即触发；
* :meth:`SpecSchedule.next` / :meth:`prev` 在传入 datetime 的时区里搜索，遵守该时区的
  DST 规则（不存在的本地时刻会被跳过），搜索上限为 :data:`YEAR_LIMIT` 年，超出返回
  ``None``（对齐 robfig 的 5 年上限）。

与 robfig 的有意差异（均为超集，已在测试中固定）：

* 周字段接受 ``7`` 并归一化为周日 ``0``（robfig 文档只写 0-6）；
* 额外提供 :meth:`prev`（robfig 没有）与 :func:`parse_with_seconds`。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "YEAR_LIMIT",
    "ConstantDelaySchedule",
    "CronParseError",
    "Schedule",
    "SpecSchedule",
    "every",
    "parse",
    "parse_standard",
    "parse_with_seconds",
]

YEAR_LIMIT = 5
"""向前/向后搜索的年数上限，与 robfig/cron 的 ``yearLimit`` 一致。"""

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_DOWS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")
_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


class CronParseError(ValueError):
    """cron 表达式非法。

    ``field`` 是出错字段名（``"second"``/``"minute"``/... ，字段数错误时为
    ``"spec"``），``index`` 是**该字段内**的 0 基字符偏移，``spec`` 是原始表达式。
    """

    def __init__(self, message: str, *, field: str, index: int, spec: str) -> None:
        super().__init__(
            f"cron 表达式解析失败：{message}"
            f"（字段 {field!r} 第 {index + 1} 个字符，表达式 {spec!r}）"
        )
        self.message = message
        self.field = field
        self.index = index
        self.spec = spec


@runtime_checkable
class Schedule(Protocol):
    """调度接口：与 ``robfig/cron`` 的 ``Schedule`` 等价（外加 ``prev``）。"""

    def next(self, after: _dt.datetime) -> _dt.datetime | None:
        """严格晚于 ``after`` 的下一次触发时间；不存在则 ``None``。"""

    def prev(self, before: _dt.datetime) -> _dt.datetime | None:
        """严格早于 ``before`` 的上一次触发时间；不存在则 ``None``。"""


# ---------------------------------------------------------------------------
# 时区辅助
# ---------------------------------------------------------------------------
def _exists(moment: _dt.datetime) -> bool:
    """判定本地时刻是否真实存在（DST 春季跳变区间内的墙钟时间不存在）。"""
    if moment.tzinfo is None:
        return True
    try:
        back = moment.astimezone(_dt.timezone.utc).astimezone(moment.tzinfo)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 极端年份
        return False
    return back.replace(tzinfo=None) == moment.replace(tzinfo=None)


def _next_of(values: frozenset[int], current: int) -> int | None:
    candidates = [value for value in values if value >= current]
    return min(candidates) if candidates else None


def _prev_of(values: frozenset[int], current: int) -> int | None:
    candidates = [value for value in values if value <= current]
    return max(candidates) if candidates else None


def _first_of_next_month(moment: _dt.datetime) -> _dt.datetime:
    day = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (day + _dt.timedelta(days=32)).replace(day=1)


def _start_of_month(moment: _dt.datetime) -> _dt.datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _dow_of(moment: _dt.datetime) -> int:
    """Go ``time.Weekday()`` 口径：周日 = 0。"""
    return (moment.weekday() + 1) % 7


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Field:
    values: frozenset[int]
    star: bool
    text: str


def _tokens(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char == ",":
            out.append((text[start:index], start))
            start = index + 1
    out.append((text[start:], start))
    return out


def _canonical_field(values: frozenset[int], low: int, high: int) -> str:
    ordered = sorted(values)
    if not ordered:
        return "*"
    if len(ordered) == high - low + 1:
        return "*"
    if len(ordered) >= 3:
        step = ordered[1] - ordered[0]
        if step > 1 and all(b - a == step for a, b in zip(ordered, ordered[1:])):
            if ordered[0] == low:
                return f"*/{step}"
            if ordered[-1] == high:
                return f"{ordered[0]}-{high}/{step}"
    parts: list[str] = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"{ordered[index]}-{ordered[end]}")
        else:
            parts.extend(str(value) for value in ordered[index : end + 1])
        index = end + 1
    return ",".join(parts)


def _parse_value(
    raw: str,
    offset: int,
    *,
    name: str,
    spec: str,
    low: int,
    high: int,
    names: list[str] | None,
    home: int,
) -> int:
    text = raw
    if names is not None:
        upper = text.upper()
        if upper in names:
            return names.index(upper) + (low if low == 1 else 0)
    if not text.isdigit():
        raise CronParseError(
            f"{name} 字段的取值 {text!r} 不是数字"
            + (f"也不是合法名称（{'/'.join(names)}）" if names else ""),
            field=name,
            index=offset,
            spec=spec,
        )
    value = int(text)
    if value == home:
        return value
    if value < low or value > high:
        raise CronParseError(
            f"{name} 字段取值 {value} 超出范围 {low}-{high}", field=name, index=offset, spec=spec
        )
    return value


def _parse_field(
    text: str,
    *,
    name: str,
    spec: str,
    low: int,
    high: int,
    names: list[str] | None = None,
    allow_question: bool = False,
    home: int | None = None,
) -> _Field:
    """解析单个字段。``home`` 是超出 ``high`` 但等价于 ``low`` 的取值（周日 7）。"""
    if text == "":
        raise CronParseError("字段为空", field=name, index=0, spec=spec)
    if text == "*" or (allow_question and text == "?"):
        return _Field(frozenset(range(low, high + 1)), True, "*")
    values: set[int] = set()
    for token, offset in _tokens(text):
        if token == "":
            raise CronParseError("列表中存在空元素", field=name, index=offset, spec=spec)
        body, slash, step_text = token.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit():
                raise CronParseError(
                    f"步长 {step_text!r} 不是正整数",
                    field=name,
                    index=offset + len(body) + 1,
                    spec=spec,
                )
            step = int(step_text)
            if step == 0:
                raise CronParseError("步长不能为 0", field=name, index=offset + len(body) + 1, spec=spec)
        if body == "*" or (allow_question and body == "?"):
            start, end = low, high
        else:
            if body.count("-") > 1:
                raise CronParseError(f"区间 {body!r} 非法", field=name, index=offset, spec=spec)
            head, dash, tail = body.partition("-")
            if dash:
                if head == "" or tail == "":
                    raise CronParseError(f"区间 {body!r} 缺少端点", field=name, index=offset, spec=spec)
                start = _parse_value(
                    head, offset, name=name, spec=spec, low=low, high=high, names=names, home=home
                )
                end = _parse_value(
                    tail,
                    offset + len(head) + 1,
                    name=name,
                    spec=spec,
                    low=low,
                    high=high,
                    names=names,
                    home=home,
                )
                if start > end:
                    raise CronParseError(
                        f"区间 {body!r} 的起点大于终点", field=name, index=offset, spec=spec
                    )
            else:
                start = _parse_value(
                    body, offset, name=name, spec=spec, low=low, high=high, names=names, home=home
                )
                end = high if slash else start
        for value in range(start, end + 1, step):
            values.add(low if home is not None and value == home else value)
    return _Field(
        frozenset(values), False, _canonical_field(frozenset(values), low, high)
    )


# ---------------------------------------------------------------------------
# 调度表
# ---------------------------------------------------------------------------
class SpecSchedule:
    """cron 表达式对应的调度表（对应 robfig 的 ``SpecSchedule``）。"""

    __slots__ = ("seconds", "minutes", "hours", "dom", "month", "dow", "has_seconds", "spec")

    def __init__(
        self,
        *,
        seconds: _Field,
        minutes: _Field,
        hours: _Field,
        dom: _Field,
        month: _Field,
        dow: _Field,
        has_seconds: bool,
        spec: str,
    ) -> None:
        self.seconds = seconds
        self.minutes = minutes
        self.hours = hours
        self.dom = dom
        self.month = month
        self.dow = dow
        self.has_seconds = has_seconds
        self.spec = spec

    # -- 匹配 ---------------------------------------------------------------
    def _day_matches(self, moment: _dt.datetime) -> bool:
        """DOM/DOW 的 OR 语义：两边都受限时命中任一即可。"""
        if self.dom.star and self.dow.star:
            return True
        if self.dom.star:
            return _dow_of(moment) in self.dow.values
        if self.dow.star:
            return moment.day in self.dom.values
        return moment.day in self.dom.values or _dow_of(moment) in self.dow.values

    def _matches(self, moment: _dt.datetime) -> bool:
        return (
            moment.month in self.month.values
            and self._day_matches(moment)
            and moment.hour in self.hours.values
            and moment.minute in self.minutes.values
            and moment.second in self.seconds.values
        )

    # -- 搜索 ---------------------------------------------------------------
    def next(self, after: _dt.datetime) -> _dt.datetime | None:
        """严格晚于 ``after`` 的下一次触发；``after`` 为 aware 时结果同 tzinfo。"""
        moment = after.replace(microsecond=0) + _dt.timedelta(seconds=1)
        limit = moment.year + YEAR_LIMIT
        while True:
            if moment.year > limit:
                return None
            if moment.month not in self.month.values:
                moment = _first_of_next_month(moment)
                continue
            if not self._day_matches(moment):
                moment = (moment.replace(hour=0, minute=0, second=0) + _dt.timedelta(days=1))
                continue
            if moment.hour not in self.hours.values:
                found = _next_of(self.hours.values, moment.hour)
                if found is None:
                    moment = moment.replace(hour=0, minute=0, second=0) + _dt.timedelta(days=1)
                else:
                    moment = moment.replace(hour=found, minute=0, second=0)
                continue
            if moment.minute not in self.minutes.values:
                found = _next_of(self.minutes.values, moment.minute)
                if found is None:
                    moment = moment.replace(minute=0, second=0) + _dt.timedelta(hours=1)
                else:
                    moment = moment.replace(minute=found, second=0)
                continue
            if moment.second not in self.seconds.values:
                found = _next_of(self.seconds.values, moment.second)
                if found is None:
                    moment = moment.replace(second=0) + _dt.timedelta(minutes=1)
                else:
                    moment = moment.replace(second=found)
                continue
            if not _exists(moment):
                moment += _dt.timedelta(seconds=1)
                continue
            return moment

    def prev(self, before: _dt.datetime) -> _dt.datetime | None:
        """严格早于 ``before`` 的上一次触发（robfig 没有；语义与之对称）。"""
        moment = before.replace(microsecond=0) - _dt.timedelta(seconds=1)
        limit = moment.year - YEAR_LIMIT
        while True:
            if moment.year < limit:
                return None
            if moment.month not in self.month.values:
                moment = _start_of_month(moment) - _dt.timedelta(seconds=1)
                continue
            if not self._day_matches(moment):
                moment = moment.replace(hour=0, minute=0, second=0) - _dt.timedelta(seconds=1)
                continue
            if moment.hour not in self.hours.values:
                found = _prev_of(self.hours.values, moment.hour)
                if found is None:
                    moment = moment.replace(hour=0, minute=0, second=0) - _dt.timedelta(seconds=1)
                else:
                    moment = moment.replace(hour=found, minute=59, second=59)
                continue
            if moment.minute not in self.minutes.values:
                found = _prev_of(self.minutes.values, moment.minute)
                if found is None:
                    moment = moment.replace(minute=0, second=0) - _dt.timedelta(seconds=1)
                else:
                    moment = moment.replace(minute=found, second=59)
                continue
            if moment.second not in self.seconds.values:
                found = _prev_of(self.seconds.values, moment.second)
                if found is None:
                    moment = moment.replace(second=0) - _dt.timedelta(seconds=1)
                else:
                    moment = moment.replace(second=found)
                continue
            if not _exists(moment):
                moment -= _dt.timedelta(seconds=1)
                continue
            return moment

    def __str__(self) -> str:
        fields = [self.seconds, self.minutes, self.hours, self.dom, self.month, self.dow]
        text = [field.text for field in (fields if self.has_seconds else fields[1:])]
        return " ".join(text)

    def __repr__(self) -> str:
        return f"SpecSchedule({str(self)!r})"


# ---------------------------------------------------------------------------
# 固定间隔
# ---------------------------------------------------------------------------
def _format_duration(seconds: float) -> str:
    """Go ``time.Duration.String()`` 的常用子集（h/m/s/ms/us/ns）。"""
    total_ns = int(round(seconds * 1_000_000_000))
    if total_ns <= 0:
        return "0s"
    if total_ns < 1_000_000_000:
        for suffix, scale in (("ms", 1_000_000), ("us", 1_000), ("ns", 1)):
            if total_ns >= scale:
                whole, rest = divmod(total_ns, scale)
                if rest:
                    digits = f"{rest:0{len(str(scale)) - 1}d}".rstrip("0")
                    return f"{whole}.{digits}{suffix}"
                return f"{whole}{suffix}"
        return "0s"  # pragma: no cover - 上面的循环已覆盖
    hours, rest = divmod(total_ns, 3_600_000_000_000)
    minutes, rest = divmod(rest, 60_000_000_000)
    whole, nanos = divmod(rest, 1_000_000_000)
    text = f"{hours}h" if hours else ""
    if hours or minutes:
        text += f"{minutes}m"
    text += f"{whole}"
    if nanos:
        text += "." + f"{nanos:09d}".rstrip("0")
    return text + "s"


class ConstantDelaySchedule:
    """固定间隔调度（对应 robfig 的 ``ConstantDelaySchedule``）。

    间隔小于 1 秒会被抬到 1 秒，亚秒精度被截断，与 ``robfig/cron`` 的 ``Every``
    一致；``next(after)`` 是 ``after + delay`` 并去掉 ``after`` 的微秒。
    """

    __slots__ = ("delay", "interval")

    def __init__(self, delay: _dt.timedelta) -> None:
        if delay < _dt.timedelta(seconds=1):
            delay = _dt.timedelta(seconds=1)
        self.delay = _dt.timedelta(seconds=int(delay.total_seconds()))
        self.interval = self.delay.total_seconds()

    def next(self, after: _dt.datetime) -> _dt.datetime:
        return after + self.delay - _dt.timedelta(microseconds=after.microsecond)

    def prev(self, before: _dt.datetime) -> _dt.datetime:
        return before - self.delay - _dt.timedelta(microseconds=before.microsecond)

    def __str__(self) -> str:
        return f"@every {_format_duration(self.interval)}"

    def __repr__(self) -> str:
        return f"ConstantDelaySchedule({self.interval!r})"


def every(duration: float | _dt.timedelta) -> ConstantDelaySchedule:
    """构造固定间隔调度；``duration`` 为秒数或 ``timedelta``。"""
    if isinstance(duration, _dt.timedelta):
        return ConstantDelaySchedule(duration)
    return ConstantDelaySchedule(_dt.timedelta(seconds=float(duration)))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _parse_duration(text: str) -> _dt.timedelta:
    body = text.strip()
    if body in ("", "0"):
        return _dt.timedelta(0)
    sign = 1.0
    if body[0] in "+-":
        sign = -1.0 if body[0] == "-" else 1.0
        body = body[1:]
    total = 0.0
    position = 0
    for match in _DURATION.finditer(body):
        if match.start() != position:
            raise ValueError(f"非法时长 {text!r}")
        total += float(match.group(1)) * _UNITS[match.group(2)]
        position = match.end()
    if position != len(body) or position == 0:
        raise ValueError(f"非法时长 {text!r}")
    return _dt.timedelta(seconds=sign * total)


def _parse_descriptor(spec: str) -> Schedule:
    text = spec.strip()
    lowered = text.lower()
    if lowered.startswith("@every"):
        argument = text[len("@every") :].strip()
        try:
            return ConstantDelaySchedule(_parse_duration(argument))
        except ValueError as exc:
            raise CronParseError(
                f"@every 的时长非法：{argument!r}", field="@every", index=0, spec=spec
            ) from exc
    aliases = {
        "@yearly": "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
        "@monthly": "0 0 1 * *",
        "@weekly": "0 0 * * 0",
        "@daily": "0 0 * * *",
        "@midnight": "0 0 * * *",
        "@hourly": "0 * * * *",
    }
    expanded = aliases.get(lowered)
    if expanded is None:
        raise CronParseError(
            f"未知描述符 {text!r}（支持 @yearly/@annually/@monthly/@weekly/@daily/"
            "@midnight/@hourly/@every）",
            field="descriptor",
            index=0,
            spec=spec,
        )
    return _parse_fields(expanded.split(), has_seconds=False, spec=spec)


def _parse_fields(fields: list[str], *, has_seconds: bool, spec: str) -> SpecSchedule:
    names = ["second", "minute", "hour", "dom", "month", "dow"]
    labels = names if has_seconds else names[1:]
    table: dict[str, _Field] = {}
    for label, text in zip(labels, fields):
        if label == "second":
            table[label] = _parse_field(text, name=label, spec=spec, low=0, high=59)
        elif label == "minute":
            table[label] = _parse_field(text, name=label, spec=spec, low=0, high=59)
        elif label == "hour":
            table[label] = _parse_field(text, name=label, spec=spec, low=0, high=23)
        elif label == "dom":
            table[label] = _parse_field(
                text, name=label, spec=spec, low=1, high=31, allow_question=True
            )
        elif label == "month":
            table[label] = _parse_field(
                text, name=label, spec=spec, low=1, high=12, names=_MONTHS
            )
        else:
            table[label] = _parse_field(
                text, name=label, spec=spec, low=0, high=6, names=_DOWS,
                allow_question=True, home=7,
            )
    if "second" not in table:
        table["second"] = _Field(frozenset({0}), False, "0")
    return SpecSchedule(
        seconds=table["second"],
        minutes=table["minute"],
        hours=table["hour"],
        dom=table["dom"],
        month=table["month"],
        dow=table["dow"],
        has_seconds=has_seconds,
        spec=spec,
    )


def _split(spec: str, wanted: int, api: str) -> list[str]:
    text = spec.strip()
    fields = text.split()
    if len(fields) != wanted:
        raise CronParseError(
            f"{api} 需要恰好 {wanted} 段，实际 {len(fields)} 段：{fields}",
            field="spec",
            index=0,
            spec=spec,
        )
    return fields


def parse(spec: str) -> Schedule:
    """解析 5 段或 6 段表达式，以及 ``@`` 描述符。"""
    text = spec.strip()
    if text.startswith("@"):
        return _parse_descriptor(text)
    fields = text.split()
    if len(fields) == 5:
        return _parse_fields(fields, has_seconds=False, spec=spec)
    if len(fields) == 6:
        return _parse_fields(fields, has_seconds=True, spec=spec)
    raise CronParseError(
        f"表达式必须为 5 段（分 时 日 月 周）或 6 段（含秒），实际 {len(fields)} 段：{fields}",
        field="spec",
        index=0,
        spec=spec,
    )


def parse_standard(spec: str) -> Schedule:
    """5 段标准解析（等价 ``robfig/cron`` 的 ``ParseStandard``）。"""
    text = spec.strip()
    if text.startswith("@"):
        return _parse_descriptor(text)
    return _parse_fields(_split(text, 5, "parse_standard"), has_seconds=False, spec=spec)


def parse_with_seconds(spec: str) -> Schedule:
    """6 段解析（等价 ``cron.NewParser(Second|Minute|Hour|Dom|Month|Dow)``）。"""
    text = spec.strip()
    if text.startswith("@"):
        return _parse_descriptor(text)
    return _parse_fields(_split(text, 6, "parse_with_seconds"), has_seconds=True, spec=spec)
