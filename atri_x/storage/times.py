"""通用时间 helper。对应 Go：``internal/storage/time.go``。

落库格式统一 RFC3339（``2026-10-13T12:00:00Z``），解析兼容 Go 的
``time.RFC3339Nano``：可选小数秒（``.123456789``，Go 也接受逗号写法）、
``Z`` 或 ``+08:00`` 偏移量。

与 Go 的两处刻意差异（见 ``PORTING.md`` §1.7 硬约束）：

1. Go 的 ``time.Time`` 保留解析出来的偏移量；本仓一律归一化到 ``timezone.utc``。
2. Go 的纳秒精度在本仓截断到微秒（``datetime`` 的精度上限）。

零值时间沿用 Go 的 ``time.Time.IsZero`` 语义：``None`` 或 ``0001-01-01T00:00:00Z``，
``format_time`` 对它返回空串（与 Go 的 ``FormatTime`` 一致）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from atri_x.errors import InvalidError

# Go 的 time.Time 零值（0001-01-01T00:00:00Z）。
ZERO_TIME = datetime(1, 1, 1, tzinfo=timezone.utc)

_ZERO_NAIVE = datetime(1, 1, 1)

# Go time.RFC3339Nano 布局：2006-01-02T15:04:05.999999999Z07:00
# 小数秒与偏移量都可选边界放宽：`T`/`Z` 接受小写，小数分隔符接受 `.` 与 `,`（Go 的 Parse 同样接受逗号）。
_RFC3339_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:[.,](?P<fraction>\d+))?"
    r"(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)


def now() -> datetime:
    """当前时间（UTC aware）。对应 Go：``storage.Now()``。"""
    return datetime.now(timezone.utc)


def is_zero_time(value: datetime | None) -> bool:
    """报告 value 是否是 Go 的零值时间（``None`` 视为零值）。"""
    if value is None:
        return True
    return value.replace(tzinfo=None) == _ZERO_NAIVE


def ensure_utc(value: datetime) -> datetime:
    """把 aware datetime 归一化到 UTC。

    对应 Go：``time.Time.UTC()``。naive datetime 一律抛 :class:`InvalidError`：
    契约要求时间必须带时区（``PORTING.md`` §1.7），静默按本地时间解释会把落库时间写偏。
    """
    if not isinstance(value, datetime):  # pragma: no cover - 防御性分支
        raise InvalidError(f"value must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidError(f"datetime must be timezone-aware: {value!r}")
    return value.astimezone(timezone.utc)


def format_time(value: datetime | None) -> str:
    """把时间格式化成 RFC3339 文本；零值/``None`` 返回空串。

    对应 Go：``storage.FormatTime(t time.Time) string``。

    小数秒按 Go ``time.RFC3339Nano`` 的规则去掉末尾的 0（``.100000`` → ``.1``），
    整秒时不输出小数点。兼容 SQLite 触发器写出的 ``strftime('%Y-%m-%dT%H:%M:%fZ')``
    形式（三位小数）。
    """
    if value is None or is_zero_time(value):
        return ""
    moment = ensure_utc(value)
    out = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        out += "." + f"{moment.microsecond:06d}".rstrip("0")
    return out + "Z"


def parse_time(text: str) -> datetime:
    """解析 RFC3339 / RFC3339Nano 文本（UTC aware）。

    对应 Go：``storage.ParseTime(s string) (time.Time, error)``。
    解析失败抛 ``ValueError``（Go 返回 error）。
    """
    if not isinstance(text, str):  # pragma: no cover - 防御性分支
        raise ValueError(f"parse time {text!r}: not a string")
    match = _RFC3339_RE.match(text)
    if match is None:
        raise ValueError(f'parsing time {text!r}: not RFC3339Nano')
    fraction = match.group("fraction") or ""
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    raw_offset = match.group("offset")
    if raw_offset in ("Z", "z"):
        offset = timezone.utc
    else:
        sign = 1 if raw_offset[0] == "+" else -1
        hours = int(raw_offset[1:3])
        minutes = int(raw_offset[4:6])
        offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        moment = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            microsecond,
            tzinfo=offset,
        )
    except ValueError as exc:  # 例如 2 月 30 日、秒 = 60
        raise ValueError(f'parsing time {text!r}: {exc}') from exc
    return moment.astimezone(timezone.utc)


def parse_optional_time(text: str) -> datetime | None:
    """解析可空时间列；空串表示 NULL，返回 ``None``。

    对应 Go：``storage.ParseOptionalTime(s string) (*time.Time, error)``。
    """
    if text == "":
        return None
    return parse_time(text)


__all__ = [
    "ZERO_TIME",
    "ensure_utc",
    "format_time",
    "is_zero_time",
    "now",
    "parse_optional_time",
    "parse_time",
]
