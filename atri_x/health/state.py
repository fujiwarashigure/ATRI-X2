"""健康状态：进程存活、readiness、平台连通性与模型健康。

对应 Go：``internal/health/state.go``。

语义边界（来自上游 ``devtools/architecture.md`` 的 ``locator:health-ops``，
这三条是**契约**，不能合并）：

- ``/live`` 只由**进程存活**决定：HTTP 服务可响应且未进入 shutdown 即 live；
  心跳、平台连接和模型健康都不影响它。
- ``/ready`` 由进程初始化、数据目录可写、以及调度心跳"已知且未过期"决定；
  平台未连接、模型 API 故障**不会**让 ``/ready`` 失败。
- ``/healthz`` 汇总；平台或模型异常显示 ``degraded``，调度心跳过期时因 readiness
  检查失败返回 ``not_ready``；平台与模型状态数组始终保留。
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from atri_x.redact.redact import secrets as redact_secrets

#: LiveStale 的默认值。对应 Go ``NewState`` 里的 ``90 * time.Second``。
DEFAULT_LIVE_STALE_SECONDS = 90.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    """把 naive datetime 视作 UTC（本仓一律用 aware，这里是防御）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class Options:
    """对应 Go ``Options``。``live_stale`` 单位是秒。"""

    version: str = ""
    live_stale: float = 0.0
    now: Callable[[], datetime] | None = None


@dataclass
class ModelStatus:
    """某个 provider 的当前健康。对应 Go ``ModelStatus``。"""

    provider: str = ""
    status: str = ""
    circuit: str = ""
    last_error: str = ""
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None


@dataclass
class PlatformStatus:
    """某个平台的连接状态。对应 Go ``PlatformStatus``。"""

    name: str = ""
    connected: bool = False
    connect_count: int = 0
    last_connected_at: datetime | None = None
    last_disconnected_at: datetime | None = None
    last_error: str = ""


@dataclass
class Snapshot:
    """某一时刻的健康视图。对应 Go ``Snapshot``。"""

    status: str = ""
    live: bool = False
    ready: bool = False
    degraded: bool = False
    scheduler_live: bool = False
    scheduler_known: bool = False
    version: str = ""
    started_at: datetime = field(default_factory=_utc_now)
    uptime_seconds: int = 0
    last_heartbeat: datetime | None = None
    messages_ok: int = 0
    messages_failed: int = 0
    last_message_at: datetime | None = None
    checks: dict[str, str] = field(default_factory=dict)
    models: list[ModelStatus] = field(default_factory=list)
    platforms: list[PlatformStatus] = field(default_factory=list)
    last_restart_reason: str = ""


class State:
    """跟踪存活、readiness、平台连通性与模型健康。对应 Go ``State``。

    并发：Go 用 ``atomic`` 存计数器、``sync.RWMutex`` 守护 map。Python 里
    计数器与 map 统一用一把 ``threading.RLock`` 保护 —— 语义等价，且不会出现
    "读了心跳又读了 map" 的撕裂视图。

    Go 的方法都在 nil 接收者上做了防御（``if s == nil { return }``）。Python 里
    没有 nil 接收者，改为对 ``None`` 字段做防御。
    """

    def __init__(self, opts: Options | None = None) -> None:
        opts = opts or Options()
        self._now: Callable[[], datetime] = opts.now or _utc_now
        stale = opts.live_stale if opts.live_stale > 0 else DEFAULT_LIVE_STALE_SECONDS
        self._live_stale = timedelta(seconds=stale)
        self._version = opts.version
        self._started_at = self._clock()
        self._mu = threading.RLock()
        self._last_heartbeat: datetime | None = None
        self._ready = False
        self._shutting_down = False
        self._messages_ok = 0
        self._messages_failed = 0
        self._last_message_at: datetime | None = None
        self._expected_platforms: set[str] = set()
        self._platforms: dict[str, PlatformStatus] = {}
        self._models: dict[str, ModelStatus] = {}
        self._last_restart_reason = ""

    # -- 只读属性（对齐 Go 的结构体字段） -----------------------------------
    @property
    def version(self) -> str:
        return self._version

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def live_stale(self) -> timedelta:
        return self._live_stale

    def _clock(self) -> datetime:
        return _as_aware(self._now())

    # -- 写入 ---------------------------------------------------------------
    def beat(self) -> None:
        """关键调度循环仍在推进。对应 Go ``Beat``。"""
        with self._mu:
            self._last_heartbeat = self._clock()

    def set_ready(self, ready: bool) -> None:
        """对应 Go ``SetReady``。"""
        with self._mu:
            self._ready = bool(ready)

    def set_shutting_down(self, shutting_down: bool) -> None:
        """对应 Go ``SetShuttingDown``。"""
        with self._mu:
            self._shutting_down = bool(shutting_down)

    def record_message_handled(self) -> None:
        """对应 Go ``RecordMessageHandled``。"""
        with self._mu:
            self._messages_ok += 1
            self._last_message_at = self._clock()

    def record_message_error(self) -> None:
        """对应 Go ``RecordMessageError``。"""
        with self._mu:
            self._messages_failed += 1

    def expect_platform(self, name: str) -> None:
        """登记一个应上报、且断连会拉低聚合状态的平台。对应 Go ``ExpectPlatform``。"""
        name = (name or "").strip()
        if not name:
            return
        with self._mu:
            self._expected_platforms.add(name)
            if name not in self._platforms:
                self._platforms[name] = PlatformStatus(name=name)

    def mark_platform_connected(self, name: str) -> None:
        """对应 Go ``MarkPlatformConnected``。"""
        name = (name or "").strip()
        if not name:
            return
        now = self._clock()
        with self._mu:
            status = self._platforms.get(name) or PlatformStatus(name=name)
            status.name = name
            status.connected = True
            status.connect_count += 1
            status.last_connected_at = now
            status.last_disconnected_at = None
            status.last_error = ""
            self._platforms[name] = status

    def mark_platform_disconnected(self, name: str, err: BaseException | None = None) -> None:
        """对应 Go ``MarkPlatformDisconnected``（错误文本先脱敏）。"""
        name = (name or "").strip()
        if not name:
            return
        now = self._clock()
        with self._mu:
            status = self._platforms.get(name) or PlatformStatus(name=name)
            status.name = name
            status.connected = False
            status.last_disconnected_at = now
            if err is not None:
                status.last_error = redact_secrets(str(err))
            self._platforms[name] = status

    def record_model_error(self, provider: str, err: BaseException | None = None) -> None:
        """对应 Go ``RecordModelError``。"""
        provider = (provider or "").strip() or "unknown"
        now = self._clock()
        with self._mu:
            status = self._models.get(provider) or ModelStatus(provider=provider)
            status.provider = provider
            status.status = "degraded"
            if err is not None:
                status.last_error = redact_secrets(str(err))
            status.last_failure_at = now
            self._models[provider] = status

    def record_model_success(self, provider: str) -> None:
        """对应 Go ``RecordModelSuccess``。"""
        provider = (provider or "").strip() or "unknown"
        now = self._clock()
        with self._mu:
            status = self._models.get(provider) or ModelStatus(provider=provider)
            status.provider = provider
            status.status = "up"
            status.last_error = ""
            status.last_success_at = now
            self._models[provider] = status

    def set_model_circuit(self, provider: str, circuit: str) -> None:
        """记录 provider 级熔断状态。对应 Go ``SetModelCircuit``。"""
        provider = (provider or "").strip() or "unknown"
        circuit = (circuit or "").strip()
        with self._mu:
            status = self._models.get(provider) or ModelStatus(provider=provider)
            status.provider = provider
            if circuit:
                status.circuit = circuit
            self._models[provider] = status

    def set_last_restart_reason(self, reason: str) -> None:
        """对应 Go ``SetLastRestartReason``（先脱敏）。"""
        with self._mu:
            self._last_restart_reason = redact_secrets(reason)

    # -- 读取 ---------------------------------------------------------------
    def last_restart_reason(self) -> str:
        with self._mu:
            return self._last_restart_reason

    def is_live(self) -> bool:
        """最近是否观测到调度心跳。对应 Go ``IsLive``。"""
        with self._mu:
            last = self._last_heartbeat
        if last is None:
            return False
        return self._clock() - last <= self._live_stale

    def is_process_live(self) -> bool:
        """进程本身是否存活 —— 这是 ``/live`` 的语义。对应 Go ``IsProcessLive``。"""
        with self._mu:
            return not self._shutting_down

    def scheduler_known(self) -> bool:
        """对应 Go ``SchedulerKnown``。"""
        with self._mu:
            return self._last_heartbeat is not None

    def scheduler_live(self) -> bool:
        """对应 Go ``SchedulerLive``。"""
        return self.is_live()

    def is_ready(self) -> bool:
        """进程级 readiness 标志。对应 Go ``IsReady``。"""
        with self._mu:
            return self._ready and not self._shutting_down

    def shutting_down(self) -> bool:
        """对应 Go ``ShuttingDown``（Go 在 nil 接收者上返回 true）。"""
        with self._mu:
            return self._shutting_down

    def platform_readiness(self) -> tuple[bool, dict[str, str]]:
        """每个预期平台是否都已连接。对应 Go ``PlatformReadiness``。"""
        with self._mu:
            checks: dict[str, str] = {}
            ready = True
            for name in sorted(self._expected_platforms):
                status = self._platforms.get(name) or PlatformStatus(name=name)
                if not status.connected:
                    ready = False
                    checks[f"platform:{name}"] = "not connected"
                    continue
                checks[f"platform:{name}"] = "connected"
            if not checks:
                checks["platforms"] = "none configured"
            return ready, checks

    def snapshot(self) -> Snapshot:
        """返回某一时刻的健康视图（**不执行**外部检查）。对应 Go ``Snapshot``。"""
        live = self.is_process_live()
        scheduler_known = self.scheduler_known()
        scheduler_live = self.scheduler_live()
        ready = self.is_ready() and not (scheduler_known and not scheduler_live)
        platform_ready, platform_checks = self.platform_readiness()

        with self._mu:
            models = [dataclasses.replace(m) for m in self._models.values()]
            platforms = [dataclasses.replace(p) for p in self._platforms.values()]
            expected_message_at = self._last_message_at
            messages_ok = self._messages_ok
            messages_failed = self._messages_failed
            last_heartbeat = self._last_heartbeat
            restart_reason = self._last_restart_reason
            version = self._version
            started_at = self._started_at

        degraded = any(m.status == "degraded" for m in models)
        models.sort(key=lambda m: m.provider)
        platforms.sort(key=lambda p: p.name)
        if not platform_ready:
            degraded = True
        if scheduler_known and not scheduler_live:
            degraded = True

        checks: dict[str, str] = dict(platform_checks)
        if not scheduler_known:
            checks["scheduler"] = "not started"
        elif scheduler_live:
            checks["scheduler"] = "ok"
        else:
            checks["scheduler"] = "heartbeat stale"

        if not live:
            status = "not_live"
        elif not ready:
            status = "not_ready"
        elif degraded:
            status = "degraded"
        else:
            status = "ok"

        now = self._clock()
        return Snapshot(
            status=status,
            live=live,
            ready=ready,
            degraded=degraded,
            scheduler_live=scheduler_live,
            scheduler_known=scheduler_known,
            version=version,
            started_at=started_at,
            uptime_seconds=int((now - started_at).total_seconds()),
            last_heartbeat=last_heartbeat,
            messages_ok=messages_ok,
            messages_failed=messages_failed,
            last_message_at=expected_message_at,
            checks=checks,
            models=models,
            platforms=platforms,
            last_restart_reason=restart_reason,
        )


def redact_secrets_health(value: str) -> str:
    """健康包的历史入口。对应 Go ``health.RedactSecrets``。

    上游保留它是为了让既有调用点不必改 import；它委托给共享的
    ``internal/redact``，也是写入 ``State`` 之前的**第二道防线**。
    """
    return redact_secrets(value)


# Go 的导出名
RedactSecrets = redact_secrets_health


def new_state(opts: Options | None = None) -> State:
    """对应 Go ``NewState``。"""
    return State(opts)


__all__ = [name for name in dir() if not name.startswith("_")]
