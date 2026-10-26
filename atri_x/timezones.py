"""时区解析与 tzdata 数据探测（Python 侧新增模块）。

Go 侧直接用 ``time.LoadLocation`` 并信任宿主 ``/usr/share/zoneinfo``；Python 侧在
Windows 上没有系统时区库（``zoneinfo.TZPATH`` 为空），必须依赖已安装的 ``tzdata``
包。本模块是**唯一的时区解析入口**，也是「数据到底在不在」的唯一探测点。

契约（见 ``docs/configuration.md`` 的 ``[time]`` 一节）：

* IANA 名（``Asia/Shanghai``）：走 :mod:`zoneinfo`，内含 DST 规则；
* ``fixed:+08:00`` / ``fixed:-05:00``：显式固定偏移，**不支持 DST**，仅给
  "部署地没有 DST、又确实缺时区数据"的场合兜底；
* ``system``：显式声明沿用进程本地时区，用于兼容旧行为；同样是**固定偏移**语义
  （``datetime.now().astimezone()`` 只反映此刻的偏移，不跟随 DST）；
* 其它一律**硬失败**，不回落。写错名字与数据缺失分成两个异常，因为修复动作不同。
"""

from __future__ import annotations

import datetime as dt
import functools
import re

from atri_x.errors import InvalidTimezoneError, TimezoneDataUnavailableError

__all__ = [
    "DEFAULT_TIMEZONE",
    "FIXED_PREFIX",
    "SYSTEM_TIMEZONE_NAME",
    "TimezoneResolution",
    "available_timezones",
    "canonical_name",
    "format_offset",
    "iana_zone_count",
    "is_known_timezone",
    "parse_fixed_offset",
    "resolve_timezone",
    "system_timezone",
    "tzdata_version",
    "zoneinfo_available",
]

#: 新装默认时区：与 Docker 镜像的 ``TZ``、用量报告/峰谷计价的既有口径一致。
DEFAULT_TIMEZONE = "Asia/Shanghai"
#: 显式「沿用进程本地时区」的名字（不是 IANA 名）。
SYSTEM_TIMEZONE_NAME = "system"
#: 固定偏移前缀，例如 ``fixed:+08:00``。
FIXED_PREFIX = "fixed:"

#: 现实世界的最大法定偏移（UTC+14:00）；超出视为写错。
_MAX_OFFSET_HOURS = 14

_FIXED_RE = re.compile(r"^(?P<sign>[+-])(?P<hour>\d{1,2}):(?P<minute>\d{2})$")


@functools.lru_cache(maxsize=1)
def _zoneinfo_names() -> frozenset[str]:
    """:func:`zoneinfo.available_timezones` 的缓存结果（空集合 = 没有时区数据）。"""
    try:
        from zoneinfo import available_timezones
    except Exception:  # pragma: no cover - Python < 3.9 才会发生
        return frozenset()
    try:
        return frozenset(available_timezones())
    except Exception:  # noqa: BLE001 - 数据损坏时按不可用处理
        return frozenset()


def available_timezones() -> tuple[str, ...]:
    """返回本机可用的 IANA 时区名（排序后）。

    冻结版里这一集合应当**非空**（构建期断言 ``Asia/Shanghai`` 存在）；为空说明
    时区数据没打进产物，任何 IANA 名都无法解析。
    """
    return tuple(sorted(_zoneinfo_names()))


def iana_zone_count() -> int:
    """可用 IANA 时区数量（诊断用；0 表示缺数据）。"""
    return len(_zoneinfo_names())


def zoneinfo_available() -> bool:
    """本机是否有可用的 IANA 时区数据。"""
    return bool(_zoneinfo_names())


def tzdata_version() -> str:
    """已安装的 ``tzdata`` 包版本；不可用时返回空串。

    只读 ``importlib.metadata``，**不 import tzdata**——运行期仍然不依赖第三方包，
    :mod:`zoneinfo` 自己会按需读取该分发。
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("tzdata")
        except PackageNotFoundError:
            return ""
    except Exception:  # noqa: BLE001 - 元数据不可读按缺失处理
        return ""


def system_timezone() -> dt.tzinfo:
    """进程本地时区（按**此刻**的偏移）。

    返回的是 ``timezone`` 固定偏移对象，不跟随 DST；这是 ``system`` 模式被标注
    ``tz_rules=system_offset`` 的原因。
    """
    try:
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    except Exception:  # pragma: no cover - 极端环境下系统时区不可读
        return dt.timezone.utc


def parse_fixed_offset(text: str) -> dt.tzinfo:
    """解析 ``+08:00`` / ``-05:30`` 形态的固定偏移。

    非法输入抛 :class:`atri_x.errors.InvalidTimezoneError`（不回落）。
    """
    raw = (text or "").strip()
    match = _FIXED_RE.match(raw)
    if match is None:
        raise InvalidTimezoneError(
            f"fixed offset must look like +08:00 or -05:00, got {raw!r}"
        )
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if minute > 59:
        raise InvalidTimezoneError(f"fixed offset minutes must be 0-59, got {raw!r}")
    if hour > _MAX_OFFSET_HOURS:
        raise InvalidTimezoneError(
            f"fixed offset hours must be within ±{_MAX_OFFSET_HOURS}, got {raw!r}"
        )
    sign = 1 if match.group("sign") == "+" else -1
    delta = dt.timedelta(hours=hour, minutes=minute) * sign
    return dt.timezone(delta, f"{raw}")


def format_offset(tz: dt.tzinfo, when: dt.datetime | None = None) -> str:
    """把时区在某个时刻的偏移格式化成 ``+08:00``。"""
    moment = when or dt.datetime.now(dt.timezone.utc)
    try:
        offset = moment.astimezone(tz).utcoffset()
    except Exception:  # noqa: BLE001 - 时区对象异常时按 UTC 报告
        offset = dt.timedelta(0)
    total = int((offset or dt.timedelta(0)).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


class TimezoneResolution:
    """一次时区解析的结果（不可变）。"""

    __slots__ = ("name", "source", "tz", "rules", "data_version")

    def __init__(
        self,
        name: str,
        tz: dt.tzinfo,
        *,
        source: str = "config",
        rules: str = "iana",
        data_version: str = "",
    ) -> None:
        self.name = name
        self.tz = tz
        self.source = source
        self.rules = rules
        self.data_version = data_version

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TimezoneResolution):
            return NotImplemented
        return (
            self.name == other.name
            and self.rules == other.rules
            and self.source == other.source
            and self.data_version == other.data_version
        )

    def __hash__(self) -> int:
        return hash((self.name, self.rules, self.source, self.data_version))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"TimezoneResolution(name={self.name!r}, rules={self.rules!r}, "
            f"source={self.source!r})"
        )


def canonical_name(value: str) -> str:
    """把用户写的时区名归一成诊断里显示的规范名（不解析、不校验存在性）。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw == SYSTEM_TIMEZONE_NAME:
        return SYSTEM_TIMEZONE_NAME
    lowered = raw.lower()
    if lowered.startswith(FIXED_PREFIX):
        return FIXED_PREFIX + format_offset(parse_fixed_offset(raw[len(FIXED_PREFIX):]))
    return raw


def resolve_timezone(value: str, *, source: str = "config") -> TimezoneResolution:
    """解析时区名，失败**硬抛**（不回落）。

    :param value: IANA 名 / ``fixed:+08:00`` / ``system``；空串按默认时区处理。
    :param source: 这个值从哪来（``config`` / ``default`` / ``argument``），只用于诊断。
    """
    raw = (value or "").strip()
    if not raw:
        raw = DEFAULT_TIMEZONE
        source = source if source != "config" else "default"
    if raw == SYSTEM_TIMEZONE_NAME:
        return TimezoneResolution(
            SYSTEM_TIMEZONE_NAME,
            system_timezone(),
            source=source,
            rules="system_offset",
            data_version=tzdata_version(),
        )
    lowered = raw.lower()
    if lowered.startswith(FIXED_PREFIX):
        offset_text = raw[len(FIXED_PREFIX):]
        zone = parse_fixed_offset(offset_text)
        return TimezoneResolution(
            FIXED_PREFIX + format_offset(zone),
            zone,
            source=source,
            rules="naive_offset",
            data_version=tzdata_version(),
        )
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            zone = ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            if not _zoneinfo_names():
                raise TimezoneDataUnavailableError(
                    "no IANA timezone data is available; install the 'tzdata' package "
                    "(pip install tzdata) or make sure the frozen build bundles it. "
                    f"requested timezone: {raw!r}"
                ) from exc
            raise InvalidTimezoneError(
                f"unknown timezone {raw!r}; see config [time].timezone "
                f"(known zones: {iana_zone_count()})"
            ) from exc
    except ImportError as exc:  # pragma: no cover - Python < 3.9
        raise TimezoneDataUnavailableError("zoneinfo is unavailable") from exc
    return TimezoneResolution(
        raw, zone, source=source, rules="iana", data_version=tzdata_version()
    )


def is_known_timezone(value: str) -> bool:
    """只判断能否解析（不抛异常），供 ``doctor`` / GUI 校验用。"""
    try:
        resolve_timezone(value)
    except Exception:  # noqa: BLE001 - 探测函数，任何失败都等价于"不可用"
        return False
    return True
