"""网络时间观测与一轮校验的判定结果（纯数据 + 纯函数，无 I/O）。

对应 Go：Go 侧没有网络时间校验（``timedatectl`` 之类由操作系统负责）。本模块是
Python 侧「多来源时间校验」的契约层：

* :class:`TimeObservation` —— **一个来源的一次观测**。``status`` 描述"收到了什么"，
  与"本地时间准不准"是两件事；
* :class:`RoundResult` —— 一轮（多个来源）的判定结果；
* :func:`evaluate_round` —— 判定逻辑本身（可单测，不碰时钟）。

判定口径（首版 ``verify_only``）：

* 所有比较先换算到**同一 instant**（偏移已含采样时刻差），容差用 ``uncertainty_ms``；
* **不因为 RTT 低就声称误差为零**；``uncertainty_ms`` 估计不出来就填 ``None``；
* 同一 ``group`` 的多个来源**只算一份证据**（跨组一致才进 L2）；
* 永远没有"时间准确"这一结论，最多是"多来源与本地在容差内一致"。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from atri_x.time_service import (
    VERIFY_CONFLICT,
    VERIFY_CONSISTENT,
    VERIFY_INSUFFICIENT,
    VERIFY_UNAVAILABLE,
)

__all__ = [
    "ROUND_TRIGGER_MANUAL",
    "ROUND_TRIGGER_SCHEDULED",
    "STATUS_BLOCKED",
    "STATUS_HTTP_ERROR",
    "STATUS_INVALID",
    "STATUS_OK",
    "STATUS_OVERSIZE",
    "STATUS_RATE_LIMITED",
    "STATUS_REFUSED",
    "STATUS_TIMEOUT",
    "STATUS_TLS_ERROR",
    "STATUS_UNSYNCHRONIZED",
    "TRUST_L0",
    "TRUST_L1",
    "TRUST_L2",
    "TRUST_L3",
    "RoundResult",
    "TimeObservation",
    "evaluate_round",
    "median",
    "trust_level",
]

# --- 观测状态 ---------------------------------------------------------------
STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_REFUSED = "refused"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_UNSYNCHRONIZED = "unsynchronized"
STATUS_INVALID = "invalid"
STATUS_OVERSIZE = "oversize"
STATUS_TLS_ERROR = "tls_error"
STATUS_HTTP_ERROR = "http_error"
STATUS_BLOCKED = "blocked"

#: 一轮的触发方式。
ROUND_TRIGGER_SCHEDULED = "scheduled"
ROUND_TRIGGER_MANUAL = "manual"

# --- 信任等级（只用于描述，不用于放行任何安全判断） -------------------------
TRUST_L0 = "L0_system_clock_unverified"
TRUST_L1 = "L1_network_observed"
TRUST_L2 = "L2_network_consistent"
TRUST_L3 = "L3_endpoint_authenticated"


@dataclass(frozen=True)
class TimeObservation:
    """一个时间来源的一次观测。"""

    provider_id: str
    protocol: str
    #: 来源报告的 UTC 时间；解析失败为 ``None``。
    remote_time: dt.datetime | None
    #: 本地采样时刻（UTC aware）。
    sampled_at: dt.datetime
    round_trip_ms: float | None = None
    #: ``remote - local``（毫秒，正数表示本地慢）。
    estimated_offset_ms: float | None = None
    #: 无法合理估计时必须为 ``None``（**不得填 0**）。
    uncertainty_ms: float | None = None
    #: SNTP 恒为 False；HTTPS 表示"端点身份经 TLS 认证"（不代表时间正确）。
    authenticated: bool = False
    status: str = STATUS_OK
    error_code: str = ""
    #: 独立性分组（配置声明，不由域名/AS 推断）。
    group: str = ""
    stratum: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and self.remote_time is not None

    @property
    def evidence_group(self) -> str:
        return self.group or self.provider_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "protocol": self.protocol,
            "remote_time": _iso(self.remote_time),
            "sampled_at": _iso(self.sampled_at),
            "round_trip_ms": _round(self.round_trip_ms),
            "estimated_offset_ms": _round(self.estimated_offset_ms),
            "uncertainty_ms": _round(self.uncertainty_ms),
            "authenticated": self.authenticated,
            "status": self.status,
            "error_code": self.error_code,
            "group": self.evidence_group,
            "stratum": self.stratum,
        }


@dataclass(frozen=True)
class RoundResult:
    """一轮校验的判定结果。"""

    state: str
    started_at: dt.datetime
    finished_at: dt.datetime
    observations: tuple[TimeObservation, ...] = ()
    groups: int = 0
    #: 有效观测的中位偏移（毫秒；正数表示本地慢）。
    offset_ms: float | None = None
    #: 有效观测偏移的极差（毫秒）。
    spread_ms: float | None = None
    message: str = ""
    trigger: str = ROUND_TRIGGER_SCHEDULED

    @property
    def ok_count(self) -> int:
        return sum(1 for observation in self.observations if observation.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "trigger": self.trigger,
            "groups": self.groups,
            "ok_count": self.ok_count,
            "offset_ms": _round(self.offset_ms),
            "spread_ms": _round(self.spread_ms),
            "message": self.message,
            "observations": [observation.to_dict() for observation in self.observations],
        }


def median(values: Sequence[float]) -> float | None:
    """中位数（空序列返回 ``None``）。"""
    items = sorted(values)
    if not items:
        return None
    middle = len(items) // 2
    if len(items) % 2 == 1:
        return float(items[middle])
    return (float(items[middle - 1]) + float(items[middle])) / 2.0


def trust_level(observations: Iterable[TimeObservation]) -> str:
    """把观测汇总成信任等级（描述性，不参与放行）。"""
    rows = [observation for observation in observations if observation.ok]
    if not rows:
        return TRUST_L0
    groups = {row.evidence_group for row in rows}
    if len(groups) >= 2:
        return TRUST_L2
    if any(row.authenticated for row in rows):
        return TRUST_L3
    return TRUST_L1


def evaluate_round(
    observations: Sequence[TimeObservation],
    *,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    min_groups: int = 2,
    warn_offset_ms: int = 2000,
    conflict_offset_ms: int = 30000,
    trigger: str = ROUND_TRIGGER_SCHEDULED,
) -> RoundResult:
    """根据观测判定本轮状态（纯函数，不碰时钟）。"""
    rows = tuple(observations)
    valid = [row for row in rows if row.ok and row.estimated_offset_ms is not None]
    groups = len({row.evidence_group for row in valid})
    if not valid:
        statuses = sorted({row.status for row in rows}) or ["no_source"]
        rate_limited = all(row.status == STATUS_RATE_LIMITED for row in rows)
        message = "no usable network time source (%s)" % ", ".join(statuses)
        if rate_limited:
            message = "all sources rate limited"
        return RoundResult(
            state=VERIFY_UNAVAILABLE,
            started_at=started_at,
            finished_at=finished_at,
            observations=rows,
            groups=0,
            message=message,
            trigger=trigger,
        )
    offsets = [float(row.estimated_offset_ms) for row in valid if row.estimated_offset_ms is not None]
    offset = median(offsets)
    spread = (max(offsets) - min(offsets)) if len(offsets) > 1 else 0.0
    if groups < max(1, int(min_groups)):
        return RoundResult(
            state=VERIFY_INSUFFICIENT,
            started_at=started_at,
            finished_at=finished_at,
            observations=rows,
            groups=groups,
            offset_ms=offset,
            spread_ms=spread,
            message="independent groups %d < required %d" % (groups, min_groups),
            trigger=trigger,
        )
    # 冲突判定分两层：来源之间的极差，以及中位偏移本身是否离谱。
    if spread > float(conflict_offset_ms):
        return RoundResult(
            state=VERIFY_CONFLICT,
            started_at=started_at,
            finished_at=finished_at,
            observations=rows,
            groups=groups,
            offset_ms=offset,
            spread_ms=spread,
            message="sources disagree by %.0fms (limit %dms)" % (spread, conflict_offset_ms),
            trigger=trigger,
        )
    if offset is not None and abs(offset) > float(conflict_offset_ms):
        return RoundResult(
            state=VERIFY_CONFLICT,
            started_at=started_at,
            finished_at=finished_at,
            observations=rows,
            groups=groups,
            offset_ms=offset,
            spread_ms=spread,
            message="local clock differs from network by %.0fms" % offset,
            trigger=trigger,
        )
    beyond_warn = offset is not None and abs(offset) > float(warn_offset_ms)
    message = "consistent within %.0fms" % float(warn_offset_ms)
    if beyond_warn:
        message = "consistent but local offset exceeds warn threshold (%.0fms)" % offset
    return RoundResult(
        state=VERIFY_CONSISTENT,
        started_at=started_at,
        finished_at=finished_at,
        observations=rows,
        groups=groups,
        offset_ms=offset,
        spread_ms=spread,
        message=message,
        trigger=trigger,
    )


def _iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat(timespec="seconds")


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)
