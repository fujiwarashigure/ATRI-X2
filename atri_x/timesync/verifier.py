"""间歇性多来源时间校验器（首版 ``verify_only``：只观测，不接管业务时间）。

对应 Go：Go 侧没有这层。设计约束（见 ``docs/configuration.md`` 的 ``[time]``）：

* 默认**关闭**：``[time.verification].enabled = false`` 时不会发起任何网络请求；
* 网络来源只写诊断/审计，**绝不**修改系统时钟、绝不修改业务时间；
* 一轮 = 有界并发 + 单来源超时 + 重试上限 + 整轮 deadline；手动与后台共用单飞锁；
* 等待用单调时钟 + 墙钟双记录，检测校时/休眠造成的跳变并作废样本；
* 状态一旦进入"可疑"（conflict/stale/unavailable），**网络失败不能清除告警**，
  只有新一轮一致证据才能降级；
* 状态**不接 /ready**（网络抖动不得触发容器重启），只进 ``/diagnostics`` 与 doctor。

本模块只做编排：协议实现在 :mod:`atri_x.timesync.sntp` /
:mod:`atri_x.timesync.https_time`，判定在 :mod:`atri_x.timesync.model`。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from atri_x.errors import TimeSourceRejectedError
from atri_x.time_service import (
    VERIFY_CHECKING,
    VERIFY_CONFLICT,
    VERIFY_CONSISTENT,
    VERIFY_DISABLED,
    VERIFY_INSUFFICIENT,
    VERIFY_STALE,
    VERIFY_UNAVAILABLE,
    clock_delta_mismatch,
)
from atri_x.timesync import https_time, sntp
from atri_x.timesync.model import (
    ROUND_TRIGGER_MANUAL,
    ROUND_TRIGGER_SCHEDULED,
    STATUS_BLOCKED,
    STATUS_HTTP_ERROR,
    STATUS_REFUSED,
    STATUS_TIMEOUT,
    RoundResult,
    TimeObservation,
    evaluate_round,
)
from atri_x.timesync.policy import OutboundPolicy

__all__ = [
    "ALERT_HISTORY",
    "MAX_SAMPLE_HISTORY",
    "Verifier",
    "VerifierOptions",
]

#: 偏移样本历史长度（内存环形缓冲；不落库，重启即清空）。
MAX_SAMPLE_HISTORY = 64
#: 告警历史长度（诊断用）。
ALERT_HISTORY = 20
#: 等待循环的单段最长休眠（秒）——分段才能及时发现墙钟跳变。
_WAIT_CHUNK_SECONDS = 5.0
#: 启动后第一次校验的最大延迟（秒，带抖动）。
_INITIAL_DELAY_SECONDS = 2.0
#: 退避上限倍数（连续失败时把间隔拉长，但不会永久停用全部来源）。
_MAX_BACKOFF_FACTOR = 8
#: 判为"可重试"的观测状态（瞬时网络故障）。
_RETRYABLE = frozenset({STATUS_TIMEOUT, STATUS_REFUSED, STATUS_HTTP_ERROR})
#: 进入后不得被网络失败清除的"可疑"状态。
_SUSPECT = frozenset({VERIFY_CONFLICT, VERIFY_STALE, VERIFY_UNAVAILABLE})


@dataclass
class VerifierOptions:
    """校验器装配参数。"""

    config: Any = None
    time_service: Any = None
    sources: Any = None
    logger: Any = None
    audit: Any = None
    #: 注入的传输层（测试用）；``None`` 用真实网络。
    sntp_transport: Any = None
    https_fetch: Any = None
    policy: Any = None
    #: 单轮最多使用几个来源（覆盖配置，主要给测试用）。
    max_sources: int = 0
    #: 网络样本有效期上限（来自 ``[time].max_sample_age_seconds``）。
    max_sample_age_seconds: int = 0


class Verifier:
    """后台/手动共用的一轮轮校验器。"""

    def __init__(self, opts: VerifierOptions) -> None:
        self.config = opts.config
        self.time_service = opts.time_service
        sources = [source for source in (opts.sources or []) if _enabled(source)]
        limit = int(opts.max_sources or getattr(self.config, "max_sources", 0) or 4)
        self.sources = sources[: max(1, limit)]
        self.logger = opts.logger
        self.audit = opts.audit
        self.policy = opts.policy or OutboundPolicy()
        self._sntp_transport = opts.sntp_transport
        self._https_fetch = opts.https_fetch
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = self._initial_state()
        self._result: RoundResult | None = None
        self._observations: dict[str, TimeObservation] = {}
        self._history: deque[tuple[dt.datetime, float]] = deque(maxlen=MAX_SAMPLE_HISTORY)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=ALERT_HISTORY)
        self._last_check_at: dt.datetime | None = None
        self._last_success_at: dt.datetime | None = None
        self._next_check_at: dt.datetime | None = None
        self._consecutive_failures = 0
        self._manual_monotonic: float | None = None
        self._random = random.Random()
        self._max_sample_age_seconds = int(opts.max_sample_age_seconds or 0)

    # -- 基本属性 -----------------------------------------------------------
    def enabled(self) -> bool:
        """校验是否真正可运行（开关打开且至少有一个启用来源）。"""
        if not bool(getattr(self.config, "enabled", False)):
            return False
        return bool(self.sources)

    def state(self) -> str:
        return self._state

    def result(self) -> RoundResult | None:
        return self._result

    def next_check_at(self) -> dt.datetime | None:
        return self._next_check_at

    def manual_cooldown_seconds(self) -> float:
        """手动校验还要等多少秒（0 = 可以立刻执行）。"""
        if self._manual_monotonic is None:
            return 0.0
        minimum = float(getattr(self.config, "manual_min_interval_seconds", 60) or 60)
        elapsed = self._now_monotonic() - self._manual_monotonic
        return max(0.0, minimum - elapsed)

    # -- 生命周期 -----------------------------------------------------------
    async def start(self) -> None:
        """启动后台循环；未启用时立即返回（状态 disabled）。"""
        if self._task is not None and not self._task.done():
            return
        if not self.enabled():
            self._set_state(self._initial_state(), "verifier is not enabled")
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        """停止后台循环并等待任务退出（幂等）。"""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停机关闭不抛
            pass

    async def run(self) -> None:
        """后台主循环（启动后先做一次校验，之后按间隔 + 抖动）。"""
        delay = self._random.uniform(0.5, _INITIAL_DELAY_SECONDS)
        while not self._stopping.is_set():
            target = self._time_service_now() + dt.timedelta(seconds=delay)
            self._next_check_at = target
            outcome = await self._wait_until(target)
            if outcome == "stopped":
                return
            if outcome == "jumped":
                self._invalidate("clock_jump_or_suspend")
                delay = self._jitter(self._interval())
                continue
            try:
                await self.check_round(ROUND_TRIGGER_SCHEDULED)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败不得杀掉后台循环
                self._set_state(VERIFY_UNAVAILABLE, f"round failed: {exc}")
            delay = self._jitter(self._next_interval())

    # -- 一轮校验 -----------------------------------------------------------
    async def check_round(self, trigger: str = ROUND_TRIGGER_MANUAL) -> RoundResult:
        """执行一轮（单飞：已有在途轮次时直接返回 checking 结果）。"""
        if not self.enabled():
            return self._result or RoundResult(
                state=self._initial_state(),
                started_at=self._time_service_now(),
                finished_at=self._time_service_now(),
                message="verification is not enabled",
                trigger=trigger,
            )
        if self._lock.locked():
            return RoundResult(
                state=VERIFY_CHECKING,
                started_at=self._time_service_now(),
                finished_at=self._time_service_now(),
                message="a verification round is already in progress",
                trigger=trigger,
            )
        async with self._lock:
            if trigger == ROUND_TRIGGER_MANUAL:
                self._manual_monotonic = self._now_monotonic()
            return await self._run_round(trigger)

    async def _run_round(self, trigger: str) -> RoundResult:
        started = self._time_service_now()
        previous_state = self._state
        self._set_state(VERIFY_CHECKING, "verification round started")
        self._audit("time.verify.started", trigger=trigger, sources=len(self.sources))
        observations: list[TimeObservation] = []
        wall_before = self._time_service_now()
        mono_before = self._now_monotonic()
        deadline = max(2.0, float(getattr(self.config, "round_deadline_seconds", 8) or 8))
        try:
            observations = await asyncio.wait_for(
                self._gather_observations(), timeout=deadline
            )
        except asyncio.TimeoutError:
            self._alerts.append(
                {
                    "code": "round_deadline",
                    "at": started.isoformat(timespec="seconds"),
                    "reason": "round exceeded %.1fs" % deadline,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 观测层异常不应抛出
            self._alerts.append(
                {
                    "code": "round_error",
                    "at": started.isoformat(timespec="seconds"),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        finished = self._time_service_now()
        self._last_check_at = finished
        wall_after = finished
        mono_after = self._now_monotonic()
        jumped = (
            clock_delta_mismatch(wall_before, mono_before, wall_after, mono_after)
            > float(getattr(self.config, "suspend_threshold_seconds", 5) or 5)
        )
        if jumped:
            self._invalidate("clock_jump_or_suspend")
            return self._result or RoundResult(
                state=self._state,
                started_at=started,
                finished_at=finished,
                message="clock jumped during the verification round",
                trigger=trigger,
            )
        result = evaluate_round(
            observations,
            started_at=started,
            finished_at=finished,
            min_groups=int(getattr(self.config, "min_groups", 2) or 2),
            warn_offset_ms=int(getattr(self.config, "warn_offset_ms", 2000) or 2000),
            conflict_offset_ms=int(getattr(self.config, "conflict_offset_ms", 30000) or 30000),
            trigger=trigger,
        )
        result = self._apply_state(result, trigger, previous=previous_state)
        self._result = result
        for observation in result.observations:
            self._observations[observation.provider_id] = observation
        if result.ok_count:
            self._record_success(result)
        else:
            self._consecutive_failures += 1
        self._audit(
            "time.verify.finished",
            trigger=trigger,
            state=result.state,
            groups=result.groups,
            offset_ms=result.offset_ms,
            spread_ms=result.spread_ms,
            ok_count=result.ok_count,
        )
        return result

    async def _gather_observations(self) -> list[TimeObservation]:
        concurrency = max(1, min(4, int(getattr(self.config, "max_concurrency", 2) or 2)))
        semaphore = asyncio.Semaphore(concurrency)

        async def one(source: Any) -> TimeObservation:
            async with semaphore:
                return await self._observe(source)

        results = await asyncio.gather(
            *(one(source) for source in self.sources), return_exceptions=True
        )
        out: list[TimeObservation] = []
        for source, item in zip(self.sources, results):
            if isinstance(item, TimeObservation):
                out.append(item)
                continue
            out.append(
                TimeObservation(
                    provider_id=str(getattr(source, "id", "") or "?"),
                    protocol=str(getattr(source, "protocol", "") or ""),
                    remote_time=None,
                    sampled_at=self._time_service_now(),
                    status=STATUS_BLOCKED,
                    error_code=type(item).__name__,
                    group=str(getattr(source, "group", "") or ""),
                )
            )
        return out

    async def _observe(self, source: Any) -> TimeObservation:
        """单个来源：出站策略检查 → 查询 → 有上限重试。"""
        source_id = str(getattr(source, "id", "") or "?")
        group = str(getattr(source, "group", "") or "")
        try:
            await self.policy.check_source(source)
        except TimeSourceRejectedError as exc:
            self._audit("time.source_rejected", source=source_id, reason=str(exc))
            return TimeObservation(
                provider_id=source_id,
                protocol=str(getattr(source, "protocol", "") or ""),
                remote_time=None,
                sampled_at=self._time_service_now(),
                status=STATUS_BLOCKED,
                error_code="outbound_policy",
                group=group,
            )
        timeout = self._source_timeout(source)
        retries = max(0, min(3, int(getattr(self.config, "max_retries", 1) or 0)))
        observation = await self._query_once(source, timeout)
        attempt = 0
        while not observation.ok and attempt < retries:
            if observation.status not in _RETRYABLE:
                break
            attempt += 1
            observation = await self._query_once(source, timeout)
        return observation

    async def _query_once(self, source: Any, timeout: float) -> TimeObservation:
        protocol = str(getattr(source, "protocol", "") or "").lower()
        try:
            if protocol == "sntp":
                return await asyncio.wait_for(
                    sntp.query_source(
                        source,
                        now_utc=self._time_service_now,
                        timeout=timeout,
                        transport=self._sntp_transport,
                    ),
                    timeout=timeout + 1.0,
                )
            if protocol == "https":
                return await asyncio.wait_for(
                    https_time.query_source(
                        source,
                        now_utc=self._time_service_now,
                        timeout=timeout,
                        fetch=self._https_fetch,
                        max_plausible_skew_seconds=int(
                            getattr(self.config, "max_plausible_skew_seconds", 604800) or 604800
                        ),
                    ),
                    timeout=timeout + 1.0,
                )
        except asyncio.TimeoutError:
            return TimeObservation(
                provider_id=str(getattr(source, "id", "") or "?"),
                protocol=protocol,
                remote_time=None,
                sampled_at=self._time_service_now(),
                status=STATUS_TIMEOUT,
                error_code="timeout",
                group=str(getattr(source, "group", "") or ""),
            )
        return TimeObservation(
            provider_id=str(getattr(source, "id", "") or "?"),
            protocol=protocol,
            remote_time=None,
            sampled_at=self._time_service_now(),
            status=STATUS_BLOCKED,
            error_code="unsupported_protocol",
            group=str(getattr(source, "group", "") or ""),
        )

    # -- 状态机 -------------------------------------------------------------
    def _apply_state(
        self, result: RoundResult, trigger: str, *, previous: str | None = None
    ) -> RoundResult:
        if previous is None:
            previous = self._state
        state = result.state
        message = result.message
        # 可疑状态只能被"一致"证据降级：网络失败不能把冲突/过期洗掉。
        if previous in _SUSPECT and state in (VERIFY_UNAVAILABLE, VERIFY_INSUFFICIENT):
            state = previous
            message = result.message + " (previous suspect state kept)"
        if state == VERIFY_CONSISTENT and self._drift_exceeded(result):
            state = VERIFY_CONFLICT
            message = "clock drift rate exceeds threshold"
        updated = RoundResult(
            state=state,
            started_at=result.started_at,
            finished_at=result.finished_at,
            observations=result.observations,
            groups=result.groups,
            offset_ms=result.offset_ms,
            spread_ms=result.spread_ms,
            message=message,
            trigger=trigger,
        )
        self._set_state(state, message)
        if state != previous:
            code = {
                VERIFY_CONFLICT: "conflict",
                VERIFY_STALE: "stale",
                VERIFY_UNAVAILABLE: "unavailable",
                VERIFY_CONSISTENT: "consistent",
            }.get(state)
            if code is not None:
                self._alerts.append(
                    {
                        "code": code,
                        "at": result.finished_at.isoformat(timespec="seconds"),
                        "reason": message,
                    }
                )
            if state == VERIFY_CONFLICT:
                self._audit("time.verify.conflict", offset_ms=result.offset_ms,
                            spread_ms=result.spread_ms, message=message)
            elif state in (VERIFY_STALE, VERIFY_UNAVAILABLE):
                self._audit("time.verify.stale", state=state, message=message)
        return updated

    def _record_success(self, result: RoundResult) -> None:
        self._consecutive_failures = 0
        self._last_success_at = result.finished_at
        if result.offset_ms is not None:
            self._history.append((result.finished_at, float(result.offset_ms)))

    def _drift_exceeded(self, result: RoundResult) -> bool:
        """最近样本的偏移变化速率是否超过告警阈值（毫秒/小时）。"""
        if len(self._history) < 4 or result.offset_ms is None:
            return False
        first_at, first_offset = self._history[0]
        last_at, last_offset = self._history[-1]
        hours = (last_at - first_at).total_seconds() / 3600.0
        if hours <= 0:
            return False
        slope = abs(last_offset - first_offset) / hours
        limit = float(getattr(self.config, "warn_offset_ms", 2000) or 2000) * 60.0
        return slope > limit

    def _invalidate(self, reason: str) -> None:
        """作废全部样本（校时 / 休眠 / 冻结）。"""
        self._history.clear()
        self._observations.clear()
        self._last_success_at = None
        self._next_check_at = None
        self._set_state(VERIFY_STALE, f"samples invalidated: {reason}")
        self._alerts.append(
            {
                "code": reason,
                "at": self._time_service_now().isoformat(timespec="seconds"),
                "reason": "verification samples invalidated",
            }
        )
        self._audit("time.verify.clock_jump", reason=reason)

    def _initial_state(self) -> str:
        if not bool(getattr(self.config, "enabled", False)):
            return VERIFY_DISABLED
        if not self.sources:
            return VERIFY_UNAVAILABLE
        # 已启用但还没跑过任何一轮：证据不足（不是"一致"，也不是"不可用"）。
        return VERIFY_INSUFFICIENT

    def _set_state(self, state: str, message: str = "") -> None:
        self._state = state
        if self.time_service is not None and hasattr(self.time_service, "set_verification_state"):
            try:
                self.time_service.set_verification_state(state, message)
            except Exception:  # noqa: BLE001 - 状态写失败不应影响校验
                pass

    # -- 间隔 / 抖动 --------------------------------------------------------
    def _interval(self) -> float:
        return max(60.0, float(getattr(self.config, "interval_seconds", 1800) or 1800))

    def _next_interval(self) -> float:
        base = self._interval()
        if self._consecutive_failures <= 0:
            return base
        factor = min(_MAX_BACKOFF_FACTOR, 2 ** min(self._consecutive_failures, 4))
        return min(base * factor, 86400.0)

    def _jitter(self, seconds: float) -> float:
        ratio = max(0.0, min(0.5, float(getattr(self.config, "jitter_ratio", 0.2) or 0.0)))
        spread = seconds * ratio
        return max(1.0, seconds + self._random.uniform(-spread, spread))

    def _source_timeout(self, source: Any) -> float:
        override = float(getattr(source, "timeout_seconds", 0.0) or 0.0)
        if override > 0:
            return override
        return max(0.5, float(getattr(self.config, "timeout_seconds", 2.0) or 2.0))

    # -- 等待 / 时钟 --------------------------------------------------------
    async def _wait_until(self, target: dt.datetime) -> str:
        """分段等待到 ``target``；返回 ``ok`` / ``jumped`` / ``stopped``。"""
        while not self._stopping.is_set():
            remaining = (target - self._time_service_now()).total_seconds()
            if remaining <= 0:
                return "ok"
            chunk = min(_WAIT_CHUNK_SECONDS, remaining)
            wall_before = self._time_service_now()
            mono_before = self._now_monotonic()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=chunk)
                return "stopped"
            except asyncio.TimeoutError:
                pass
            wall_after = self._time_service_now()
            mono_after = self._now_monotonic()
            threshold = float(getattr(self.config, "suspend_threshold_seconds", 5) or 5)
            if clock_delta_mismatch(wall_before, mono_before, wall_after, mono_after) > threshold:
                return "jumped"
        return "stopped"

    def _time_service_now(self) -> dt.datetime:
        if self.time_service is not None:
            return self.time_service.now_utc()
        return dt.datetime.now(dt.timezone.utc)

    def _now_monotonic(self) -> float:
        if self.time_service is not None and hasattr(self.time_service, "monotonic"):
            return float(self.time_service.monotonic())
        import time as _time

        return _time.monotonic()

    def _audit(self, event: str, **attrs: Any) -> None:
        if callable(self.audit):
            try:
                self.audit(event, **attrs)
            except Exception:  # noqa: BLE001 - 审计失败不影响校验
                pass

    # -- 只读快照（诊断 / 工具） -------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """只读状态快照（**不触网、不抛异常**）。"""
        now = self._time_service_now()
        sample_age: float | None = None
        if self._last_success_at is not None:
            sample_age = max(0.0, (now - self._last_success_at).total_seconds())
        max_age = self._max_sample_age_seconds or int(
            getattr(self.config, "max_sample_age_seconds", 21600) or 21600
        )
        state = self._state
        if (
            state == VERIFY_CONSISTENT
            and sample_age is not None
            and sample_age > max_age
        ):
            state = VERIFY_STALE
        elif state == VERIFY_CONSISTENT and sample_age is None:
            state = VERIFY_STALE
        sources = []
        for source in self.sources:
            source_id = str(getattr(source, "id", "") or "?")
            observation = self._observations.get(source_id)
            sources.append(
                {
                    "provider_id": source_id,
                    "protocol": str(getattr(source, "protocol", "") or ""),
                    "group": str(getattr(source, "group", "") or source_id),
                    "authenticated": bool(observation.authenticated) if observation else False,
                    "status": observation.status if observation else "unknown",
                    "error_code": observation.error_code if observation else "",
                    "rtt_ms": _round(observation.round_trip_ms) if observation else None,
                    "offset_ms": _round(observation.estimated_offset_ms) if observation else None,
                    "uncertainty_ms": _round(observation.uncertainty_ms) if observation else None,
                    "last_ok_at": (
                        observation.sampled_at.isoformat(timespec="seconds")
                        if observation is not None and observation.ok
                        else ""
                    ),
                    "consecutive_failures": 0 if (observation and observation.ok) else (
                        self._consecutive_failures if observation else 0
                    ),
                }
            )
        return {
            "state": state,
            "mode": str(getattr(self.config, "mode", "verify_only") or "verify_only"),
            "enabled": bool(getattr(self.config, "enabled", False)),
            "sources_configured": len(self.sources),
            "last_check_at": _iso(self._last_check_at),
            "last_success_at": _iso(self._last_success_at),
            "next_check_at": _iso(self._next_check_at),
            "sample_age_seconds": _round(sample_age),
            "max_sample_age_seconds": max_age,
            "consecutive_failures": self._consecutive_failures,
            "offset_ms": _round(self._result.offset_ms) if self._result else None,
            "spread_ms": _round(self._result.spread_ms) if self._result else None,
            "groups": self._result.groups if self._result else 0,
            "message": self._result.message if self._result else "",
            "sources": sources,
            "alerts": list(self._alerts),
        }

    def observations(self) -> list[dict[str, Any]]:
        """最近一次每个来源的观测（只读缓存）。"""
        return [observation.to_dict() for observation in self._observations.values()]


def _enabled(source: Any) -> bool:
    return bool(getattr(source, "enabled", False))


def _iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)
