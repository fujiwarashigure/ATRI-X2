"""DSH 轮次的并发槽、忙时回退、卡死看门狗与运行指标。

背景（1.1.8 之前）：``DshRuntime`` 用一把进程级 ``threading.Lock`` 把**所有**路由轮次
串成 1 并发；单轮上限 180 秒，于是第 2、3 个并发复杂任务只能干等，最坏等满超时。
而 DSH 内核本身支持「一条 ACP 连接上并发多个独立会话」（每个会话自带 inflight 槽），
所以串行是 ATRI-X 这层的限制，不是内核的限制。

这个模块把"能同时跑几个"独立出来：

- **有界并发**：``max_concurrent_prompts`` 个槽（默认 2），槽满时最多等
  ``busy_wait_seconds``；等不到就抛 :class:`DshBusyError`，调用方**静默回退原生**
  （不记熔断失败、不占预算），用户不会排队 3 分钟；
- **看门狗**：每个在途轮次记录"最后一次事件"时间，超过 ``stall_timeout_seconds``
  没有任何 ``session/update`` 就发 ``session/cancel``（内核 hang 时不再占满槽位等
  到整轮超时）；
- **空闲回收**：没有在途轮次且空闲超过 ``idle_timeout_seconds`` 时回调运行时关掉
  子进程（省 ~170MB 常驻内存；下次路由重新冷启动约 1.7 秒）；
- **指标**：放行/忙拒/完成/失败/卡死/冷启动/空闲回收/预热次数与等待、运行耗时，
  经 ``/metrics`` 的 ``dsh`` 段与「性能」页展示。

线程模型：槽位在线程池的 worker 线程里获取（``Admit`` 是同步阻塞，不会卡事件循环），
监控线程只做"判断 + 调 cancel / 回调"，所有可变状态都用一把 ``threading.Lock`` 保护。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "ActiveTurn",
    "DshBusyError",
    "SchedulerStats",
    "TurnScheduler",
]

#: 监控线程的轮询间隔（秒）—— 卡死/空闲都不需要毫秒级精度。
DEFAULT_MONITOR_INTERVAL = 5.0


class DshBusyError(RuntimeError):
    """并发槽在等待窗口内没空出来（调用方应回退原生，而不是当作 DSH 失败）。"""


@dataclass
class SchedulerStats:
    """进程内累计指标（只增不减，重启清零）。"""

    admitted: int = 0
    busy_rejected: int = 0
    completed: int = 0
    failed: int = 0
    stalled: int = 0
    cold_starts: int = 0
    idle_closes: int = 0
    prewarmed: int = 0
    total_wait_seconds: float = 0.0
    total_run_seconds: float = 0.0
    max_concurrent_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        """给 ``/metrics`` / ``doctor`` 用的普通字典（键名与其它段一致用 snake_case）。"""
        return {
            "admitted": self.admitted,
            "busy_rejected": self.busy_rejected,
            "completed": self.completed,
            "failed": self.failed,
            "stalled": self.stalled,
            "cold_starts": self.cold_starts,
            "idle_closes": self.idle_closes,
            "prewarmed": self.prewarmed,
            "total_wait_seconds": round(self.total_wait_seconds, 3),
            "total_run_seconds": round(self.total_run_seconds, 3),
            "max_concurrent_seen": self.max_concurrent_seen,
        }


@dataclass
class ActiveTurn:
    """一个在途轮次：看门狗按 ``last_activity`` 判断是否卡死。"""

    token: object
    session_id: str
    started: float
    last_activity: float
    #: 已经发过 ``session/cancel``（避免监控线程反复发）。
    cancel_requested: bool = False
    stalled: bool = False
    #: 由运行时注入：``client.cancel(session_id)`` 的 best-effort 包装。
    cancel: Callable[[], None] | None = None

    def touch(self) -> None:
        """记录一次事件（读线程调用）。"""
        self.last_activity = time.monotonic()


class TurnScheduler:
    """并发槽 + 在途轮次表 + 监控线程。"""

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        busy_wait_seconds: float = 2.0,
        stall_timeout_seconds: float = 90.0,
        idle_timeout_seconds: float = 0.0,
        on_idle: Callable[[], bool] | None = None,
        interval: float = DEFAULT_MONITOR_INTERVAL,
    ) -> None:
        limit = max(1, int(max_concurrent or 1))
        self.max_concurrent = limit
        self.busy_wait_seconds = max(0.0, float(busy_wait_seconds or 0.0))
        self.stall_timeout_seconds = max(0.0, float(stall_timeout_seconds or 0.0))
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds or 0.0))
        self.stats = SchedulerStats()
        self._on_idle = on_idle
        self._interval = max(0.2, float(interval or DEFAULT_MONITOR_INTERVAL))
        self._slots = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self._turns: dict[object, ActiveTurn] = {}
        self._last_activity = time.monotonic()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._closed = False

    # -- 槽位 -----------------------------------------------------------------
    def admit(self, timeout: float | None = None) -> float:
        """获取一个并发槽；返回等待秒数，等不到抛 :class:`DshBusyError`。

        在 worker 线程里调用（``Semaphore.acquire`` 是阻塞的）；``timeout<=0`` 表示
        "有空槽就跑、没空槽立即回退"。
        """
        wait = self.busy_wait_seconds if timeout is None else max(0.0, float(timeout))
        started = time.monotonic()
        acquired = self._slots.acquire(blocking=wait > 0, timeout=wait if wait > 0 else None)
        waited = max(0.0, time.monotonic() - started)
        if not acquired:
            with self._lock:
                self.stats.busy_rejected += 1
            raise DshBusyError(
                f"DSH 并发槽已满（上限 {self.max_concurrent}），等待 {waited:.2f}s 后回退原生"
            )
        with self._lock:
            self.stats.admitted += 1
            self.stats.total_wait_seconds += waited
            active = len(self._turns) + 1
            if active > self.stats.max_concurrent_seen:
                self.stats.max_concurrent_seen = active
        return waited

    def release(self) -> None:
        """归还并发槽（幂等调用会抛 ValueError，所以由运行时保证只还一次）。"""
        try:
            self._slots.release()
        except ValueError:  # pragma: no cover - 防御性：多还一次不应炸掉调用方
            pass

    # -- 在途轮次 -------------------------------------------------------------
    def register(
        self,
        token: object,
        session_id: str,
        cancel: Callable[[], None] | None = None,
    ) -> ActiveTurn:
        """登记一个在途轮次（``token`` 是调用方给的每轮唯一对象）。"""
        now = time.monotonic()
        turn = ActiveTurn(
            token=token,
            session_id=session_id,
            started=now,
            last_activity=now,
            cancel=cancel,
        )
        with self._lock:
            self._turns[token] = turn
            self._last_activity = now
        return turn

    def touch(self, token: object) -> None:
        """本轮有新事件（读线程调用，必须快）。"""
        with self._lock:
            turn = self._turns.get(token)
        if turn is not None:
            turn.touch()
            with self._lock:
                self._last_activity = turn.last_activity

    def unregister(self, token: object) -> ActiveTurn | None:
        """注销在途轮次；返回它（供调用方判断是否被看门狗取消过）。"""
        with self._lock:
            turn = self._turns.pop(token, None)
            self._last_activity = time.monotonic()
        return turn

    def cancel(self, token: object) -> bool:
        """取消指定轮次（best-effort）；返回是否真的发了 cancel。"""
        with self._lock:
            turn = self._turns.get(token)
            if turn is None or turn.cancel_requested or turn.cancel is None:
                return False
            turn.cancel_requested = True
        try:
            turn.cancel()
        except Exception:  # noqa: BLE001 - 取消失败由整轮超时兜底
            return False
        return True

    def cancel_all(self) -> int:
        """取消所有在途轮次（关停路径）。"""
        with self._lock:
            tokens = list(self._turns)
        return sum(1 for token in tokens if self.cancel(token))

    def mark_activity(self) -> None:
        """记录"内核最近动过"（冷启动、关闭等生命周期事件）。"""
        with self._lock:
            self._last_activity = time.monotonic()

    def record_success(self, run_seconds: float = 0.0) -> None:
        with self._lock:
            self.stats.completed += 1
            self.stats.total_run_seconds += max(0.0, float(run_seconds))

    def record_failure(self, run_seconds: float = 0.0) -> None:
        with self._lock:
            self.stats.failed += 1
            self.stats.total_run_seconds += max(0.0, float(run_seconds))

    def record_cold_start(self) -> None:
        with self._lock:
            self.stats.cold_starts += 1

    def record_idle_close(self) -> None:
        with self._lock:
            self.stats.idle_closes += 1

    def record_prewarm(self) -> None:
        with self._lock:
            self.stats.prewarmed += 1

    # -- 监控线程 -------------------------------------------------------------
    def start(self) -> None:
        """按需启动监控线程（没有超时/回收配置时不启动）。"""
        if self.stall_timeout_seconds <= 0 and self.idle_timeout_seconds <= 0:
            return
        with self._lock:
            if self._monitor is not None or self._closed:
                return
            thread = threading.Thread(
                target=self._monitor_loop, name="dsh-monitor", daemon=True
            )
            self._monitor = thread
        thread.start()

    def stop(self) -> None:
        """停止监控线程（幂等）。"""
        self._stop.set()
        with self._lock:
            thread, self._monitor = self._monitor, None
            self._closed = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._check_once()
            except Exception:  # noqa: BLE001 - 监控线程不能因为单次异常退出
                continue

    def _check_once(self) -> None:
        now = time.monotonic()
        stalled: list[ActiveTurn] = []
        idle = False
        with self._lock:
            for turn in self._turns.values():
                if turn.stalled or turn.cancel_requested or turn.cancel is None:
                    continue
                if self.stall_timeout_seconds <= 0:
                    continue
                if now - turn.last_activity >= self.stall_timeout_seconds:
                    turn.stalled = True
                    turn.cancel_requested = True
                    stalled.append(turn)
            if self.idle_timeout_seconds > 0 and not self._turns:
                idle = now - self._last_activity >= self.idle_timeout_seconds
                if idle:
                    self._last_activity = now  # 避免回调失败时每轮重试打日志
        for turn in stalled:
            try:
                if turn.cancel is not None:
                    turn.cancel()
            except Exception:  # noqa: BLE001
                continue
            with self._lock:
                self.stats.stalled += 1
        if idle and self._on_idle is not None:
            try:
                self._on_idle()
            except Exception:  # noqa: BLE001 - 回收失败不影响后续轮次
                pass

    # -- 快照 -----------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """状态快照（``describe``/``/metrics`` 用）。"""
        with self._lock:
            active = len(self._turns)
            snapshot = {
                "maxConcurrent": self.max_concurrent,
                "active": active,
                "availableSlots": max(0, self.max_concurrent - active),
                "busyWaitSeconds": self.busy_wait_seconds,
                "stallTimeoutSeconds": self.stall_timeout_seconds,
                "idleTimeoutSeconds": self.idle_timeout_seconds,
                "stats": self.stats.to_dict(),
            }
        return snapshot
