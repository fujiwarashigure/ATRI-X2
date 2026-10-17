"""进程内计数器。

对应 Go：``internal/vision/counters.go``。

只保留有界、低基数的键，**不**记录 media ID 之类的标签。Go 侧用
``sync.Mutex``；这里是同步结构（会被 asyncio 与线程两条路径调用），所以用
``threading.Lock``（PORTING.md §1.6）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

__all__ = ["Counters", "Snapshot"]


@dataclass
class Snapshot:
    """计数器的一份深拷贝。对应 Go ``Snapshot``。

    ``durations`` 的值是秒（Go 是 ``time.Duration``）。
    """

    cache: dict[str, int] = field(default_factory=dict)
    coalesced: int = 0
    jobs: int = 0
    durations: dict[str, float] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)


class Counters:
    """线程安全的 :class:`Metrics` 实现。对应 Go ``Counters``。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, int] = {}
        self._coalesced = 0
        self._jobs = 0
        self._durations: dict[str, float] = {}
        self._errors: dict[str, int] = {}

    # -- Metrics 接口 -------------------------------------------------------

    def cache_lookup(self, result: str) -> None:
        """对应 Go ``Counters.CacheLookup``。"""
        with self._lock:
            self._cache[result] = self._cache.get(result, 0) + 1

    def coalesced(self) -> None:
        """对应 Go ``Counters.Coalesced``。"""
        with self._lock:
            self._coalesced += 1

    def job_started(self) -> None:
        """对应 Go ``Counters.JobStarted``。"""
        with self._lock:
            self._jobs += 1

    def duration(self, stage: str, seconds: float) -> None:
        """对应 Go ``Counters.Duration``（累加，不是覆盖）。"""
        with self._lock:
            self._durations[stage] = self._durations.get(stage, 0.0) + float(seconds)

    def error(self, error_class: str) -> None:
        """对应 Go ``Counters.Error``。"""
        with self._lock:
            self._errors[error_class] = self._errors.get(error_class, 0) + 1

    # -- 快照 ---------------------------------------------------------------

    def metrics_snapshot(self) -> Snapshot:
        """对应 Go ``Counters.MetricsSnapshot``。"""
        return self.snapshot()

    def snapshot(self) -> Snapshot:
        """返回计数器的深拷贝。对应 Go ``Counters.Snapshot``。"""
        with self._lock:
            return Snapshot(
                cache=dict(self._cache),
                coalesced=self._coalesced,
                jobs=self._jobs,
                durations=dict(self._durations),
                errors=dict(self._errors),
            )


def new_counters() -> Counters:
    """对应 Go ``NewCounters``。"""
    return Counters()
