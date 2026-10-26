"""统一时间服务：业务时间的**唯一**读取点（Python 侧新增模块）。

对应 Go：Go 侧直接散用 ``time.Now()`` / ``time.Local``；本模块是 Python 侧为
「提示词里有当前时间、时区可配置、调度可复核、诊断可观测」补的契约层。

三条硬约束（见 ``docs/configuration.md`` 的 ``[time]`` 与
``devdocs/code-map.md`` 的时间段）：

1. **业务时间源 = 系统时钟**。``source=system_clock`` 只表示「读到了系统时钟」，
   **不表示准确、不表示已同步**；网络来源只做观测，不接管业务时间（首版
   ``verify_only``）。
2. **一次快照只采一次样**：同一 :class:`TimeSnapshot` 里所有字段来自同一次
   ``now_fn``/``monotonic_fn`` 调用，避免跨午夜时 ``current_time`` 与 ``weekday``
   分属两天。
3. **拒绝 naive datetime**：入口统一校验；时区解析失败硬抛
   （:class:`atri_x.errors.InvalidTimezoneError` /
   :class:`atri_x.errors.TimezoneDataUnavailableError`），不回落。

本模块**不做 I/O**（除了读时钟）：网络校验在 :mod:`atri_x.timesync`，提示词片段在
:mod:`atri_x.agent.time_context`。
"""

from __future__ import annotations

import datetime as dt
import time as _time
from dataclasses import dataclass
from typing import Any, Callable

from atri_x.errors import InvalidError, InvalidTimezoneError
from atri_x.timezones import (
    DEFAULT_TIMEZONE,
    TimezoneResolution,
    format_offset,
    iana_zone_count,
    resolve_timezone,
    tzdata_version,
    zoneinfo_available,
)

__all__ = [
    "CLOCK_JUMP_THRESHOLD_SECONDS",
    "DEFAULT_TIME_SOURCE",
    "TimeService",
    "TimeSnapshot",
    "VERIFICATION_STATES",
    "VERIFY_CHECKING",
    "VERIFY_CONFLICT",
    "VERIFY_CONSISTENT",
    "VERIFY_DISABLED",
    "VERIFY_INSUFFICIENT",
    "VERIFY_STALE",
    "VERIFY_UNAVAILABLE",
    "clock_delta_mismatch",
    "default_time_service",
]

#: 首版唯一业务时间来源。网络观测不得写这里。
DEFAULT_TIME_SOURCE = "system_clock"

#: 墙钟与单调时钟在一段等待里的差值超过它，就判定为「校时 / 休眠 / 虚拟化冻结」。
CLOCK_JUMP_THRESHOLD_SECONDS = 5.0

# --- 校验状态（首版 verify_only 的全部取值） --------------------------------
VERIFY_DISABLED = "disabled"
VERIFY_CHECKING = "checking"
VERIFY_CONSISTENT = "consistent"
VERIFY_INSUFFICIENT = "insufficient_evidence"
VERIFY_CONFLICT = "conflict"
VERIFY_STALE = "stale"
VERIFY_UNAVAILABLE = "unavailable"

#: 合法状态集合；任何写入都必须落在这里面（防止拼写错误把诊断搞坏）。
VERIFICATION_STATES: frozenset[str] = frozenset(
    {
        VERIFY_DISABLED,
        VERIFY_CHECKING,
        VERIFY_CONSISTENT,
        VERIFY_INSUFFICIENT,
        VERIFY_CONFLICT,
        VERIFY_STALE,
        VERIFY_UNAVAILABLE,
    }
)

#: 认为「本地时间可疑」的状态（网络失败不得清除这些告警）。
_SUSPECT_STATES = frozenset({VERIFY_CONFLICT, VERIFY_STALE, VERIFY_UNAVAILABLE})


def _aware_utc(value: dt.datetime) -> dt.datetime:
    """把 aware datetime 归一化到 UTC；naive 抛 :class:`InvalidError`。

    与 :func:`atri_x.storage.times.ensure_utc` 同口径（这里不复用是为了让本模块
    保持零依赖：它是所有工具/提示词路径的底座，不能引入 storage 包）。
    """
    if not isinstance(value, dt.datetime):
        raise InvalidError(f"value must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidError(f"datetime must be timezone-aware: {value!r}")
    return value.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class TimeSnapshot:
    """同一 instant 的一致时间快照。"""

    #: 目标时区下的本地时间（aware）。
    current_time: dt.datetime
    #: 同一 instant 的 UTC 时间（aware）。
    utc_time: dt.datetime
    #: 解析后的时区名（IANA 名 / ``fixed:+08:00`` / ``system``）。
    timezone: str
    #: 该 instant 在该时区的真实偏移（DST 下会随日期变化）。
    utc_offset: dt.timedelta
    #: 0=周一 … 6=周日（与 :meth:`datetime.datetime.weekday` 一致）。
    weekday: int
    #: UNIX 秒（向下取整）。
    unix_timestamp: int
    #: 业务时间来源；首版恒为 ``system_clock``。
    source: str
    #: UTC 采样时刻。
    sampled_at: dt.datetime
    #: 采样时的单调时钟读数（秒），用于墙钟跳变检测。
    monotonic_seconds: float
    #: **恒为 False**：网络观测不会让业务时间变成"已验证准确"。
    verified: bool = False
    #: 网络校验状态（``disabled`` 表示未启用校验）。
    verification_state: str = VERIFY_DISABLED
    #: 时区值从哪来（``config`` / ``default`` / ``argument``）。
    timezone_source: str = "config"
    #: 时区规则（``iana`` / ``naive_offset`` / ``system_offset``）。
    tz_rules: str = "iana"

    def to_dict(self) -> dict[str, Any]:
        """诊断/工具输出用的稳定形态（时间一律 RFC3339 文本）。"""
        return {
            "timezone": self.timezone,
            "current_time": self.current_time.isoformat(),
            "utc_time": self.utc_time.isoformat(),
            "utc_offset": format_offset(dt.timezone(self.utc_offset), self.utc_time),
            "weekday": self.weekday,
            "unix_timestamp": self.unix_timestamp,
            "source": self.source,
            "sampled_at": self.sampled_at.isoformat(),
            "verified": self.verified,
            "verification_state": self.verification_state,
            "timezone_source": self.timezone_source,
            "tz_rules": self.tz_rules,
        }


class TimeService:
    """业务时间读取点。

    ``now_fn`` / ``monotonic_fn`` 可注入（测试假时钟）；默认读系统时钟。系统时钟读
    失败时**抛异常**，不返回编造值。
    """

    def __init__(
        self,
        timezone: str = DEFAULT_TIMEZONE,
        *,
        timezone_source: str = "default",
        now_fn: Callable[[], dt.datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
        inject_context: bool = True,
    ) -> None:
        if not isinstance(timezone, str):
            raise InvalidTimezoneError(f"timezone must be a string, got {timezone!r}")
        self._resolution: TimezoneResolution = resolve_timezone(
            timezone, source=timezone_source
        )
        self._now_fn: Callable[[], dt.datetime] = now_fn or _system_utc
        self._monotonic_fn: Callable[[], float] = monotonic_fn or _time.monotonic
        self._verification_state: str = VERIFY_DISABLED
        self._verification_notice: str = ""
        #: 是否把当前时间注入每轮系统提示（``[time].inject_context``）。
        self.inject_context = bool(inject_context)

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        time_config: Any,
        *,
        now_fn: Callable[[], dt.datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> "TimeService":
        """从 ``[time]`` 配置段构造（``timezone`` 缺省用 :data:`DEFAULT_TIMEZONE`）。"""
        raw = str(getattr(time_config, "timezone", "") or "").strip()
        return cls(
            raw or DEFAULT_TIMEZONE,
            timezone_source="config" if raw else "default",
            now_fn=now_fn,
            monotonic_fn=monotonic_fn,
            inject_context=bool(getattr(time_config, "inject_context", True)),
        )

    # -- 时区 ---------------------------------------------------------------
    def timezone(self) -> dt.tzinfo:
        return self._resolution.tz

    def timezone_name(self) -> str:
        return self._resolution.name

    def resolution(self) -> TimezoneResolution:
        return self._resolution

    def timezone_offset(self, moment: dt.datetime | None = None) -> str:
        return format_offset(self._resolution.tz, _aware_utc(moment or self.now_utc()))

    # -- 读时钟 -------------------------------------------------------------
    def now_utc(self) -> dt.datetime:
        """当前 UTC 时间（aware）；系统时钟读不出来时抛异常。"""
        return _aware_utc(self._now_fn())

    def now_local(self) -> dt.datetime:
        """当前目标时区时间（aware）。"""
        return self.now_utc().astimezone(self._resolution.tz)

    def monotonic(self) -> float:
        """单调时钟读数（秒）。只用于测量间隔，绝不持久化。"""
        return float(self._monotonic_fn())

    def sample(self) -> tuple[dt.datetime, float]:
        """采样一次：返回 ``(utc_wall, monotonic_seconds)``。

        两者**紧邻**取得，供调用方记录"这次观察发生在哪个墙钟/单调时刻"。
        """
        wall = self.now_utc()
        return wall, self.monotonic()

    def wall_pair(self) -> tuple[dt.datetime, float]:
        """:meth:`sample` 的别名（可读性更好的调用点）。"""
        return self.sample()

    def next_seconds(self, delta: float) -> dt.datetime:
        """``now_utc + delta``（delta 可为负）。"""
        return self.now_utc() + dt.timedelta(seconds=float(delta))

    def age_seconds(self, moment: dt.datetime | None) -> float | None:
        """``moment`` 距现在多少秒（同一 ``now`` 基准）；``None`` 原样返回 ``None``。"""
        if moment is None:
            return None
        return (self.now_utc() - _aware_utc(moment)).total_seconds()

    # -- 快照 ---------------------------------------------------------------
    def snapshot(self, timezone: str | None = None) -> TimeSnapshot:
        """生成同一 instant 的一致快照。

        :param timezone: 覆盖本次快照用的时区（``None`` = 服务配置的时区）；
            覆盖值同样走硬失败解析。
        """
        resolution = self._resolution
        if timezone is not None and str(timezone).strip():
            resolution = resolve_timezone(timezone, source="argument")
        wall, mono = self.sample()
        local = wall.astimezone(resolution.tz)
        offset = local.utcoffset() or dt.timedelta(0)
        return TimeSnapshot(
            current_time=local,
            utc_time=wall,
            timezone=resolution.name,
            utc_offset=offset,
            weekday=local.weekday(),
            unix_timestamp=int(wall.timestamp()),
            source=DEFAULT_TIME_SOURCE,
            sampled_at=wall,
            monotonic_seconds=mono,
            verified=False,
            verification_state=self._verification_state,
            timezone_source=resolution.source,
            tz_rules=resolution.rules,
        )

    # -- 校验状态（由 timesync.verifier 写入，只读消费） ---------------------
    def verification_state(self) -> str:
        return self._verification_state

    def verification_notice(self) -> str:
        return self._verification_notice

    def set_verification_state(self, state: str, notice: str = "") -> None:
        """写入网络校验状态；状态必须是 :data:`VERIFICATION_STATES` 之一。"""
        if state not in VERIFICATION_STATES:
            raise InvalidError(f"unknown time verification state {state!r}")
        self._verification_state = state
        if notice:
            self._verification_notice = str(notice)

    def is_suspect(self) -> bool:
        """本地时间是否处于「可疑」状态（冲突/过期/不可用）。

        语义：一旦可疑，**网络失败不得清除告警**；只有新一轮一致证据才能降级。
        """
        return self._verification_state in _SUSPECT_STATES

    # -- 诊断 ---------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """诊断用只读描述（不触网、不抛异常）。"""
        resolution = self._resolution
        version = resolution.data_version or tzdata_version()
        return {
            "effective_source": DEFAULT_TIME_SOURCE,
            "timezone": resolution.name,
            "timezone_source": resolution.source,
            "tz_rules": resolution.rules,
            "tz_data": "present" if zoneinfo_available() else "missing",
            "tzdata_version": version,
            "tz_zones": iana_zone_count(),
            "utc_offset": self.timezone_offset(),
            "verification_state": self._verification_state,
            "verification_notice": self._verification_notice,
        }


def _system_utc() -> dt.datetime:
    """系统时钟（UTC aware）。刻意不用 :func:`atri_x.storage.times.now` 以免引入存储层。"""
    return dt.datetime.now(dt.timezone.utc)


def clock_delta_mismatch(
    wall_before: dt.datetime,
    monotonic_before: float,
    wall_after: dt.datetime,
    monotonic_after: float,
) -> float:
    """返回「墙钟增量 - 单调增量」的绝对值（秒）。

    明显不为 0 说明这段时间里系统被校时、或进程经历休眠/虚拟化冻结：

    * 单调时钟通常**不**包含休眠时间（Linux ``CLOCK_MONOTONIC``），
    * 墙钟会被 NTP 前后跳。

    因此等待循环必须在醒来后**复核目标时刻**，不能只信 ``sleep`` 的时长。
    """
    wall_delta = (_aware_utc(wall_after) - _aware_utc(wall_before)).total_seconds()
    mono_delta = float(monotonic_after) - float(monotonic_before)
    return abs(wall_delta - mono_delta)


_DEFAULT_SERVICE: list[TimeService] = []


def default_time_service() -> TimeService:
    """进程级默认时间服务（懒加载、单例）。

    只给「没有装配时间服务的散装调用点」用（例如 CLI 子命令、脚本、旧单测）；
    **正式运行路径必须**用装配注入的实例，否则 ``/diagnostics`` 与提示词可能读到
    两份不同状态。
    """
    if not _DEFAULT_SERVICE:
        _DEFAULT_SERVICE.append(TimeService(DEFAULT_TIMEZONE, timezone_source="default"))
    return _DEFAULT_SERVICE[0]
