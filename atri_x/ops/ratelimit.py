"""按键的令牌桶限流器。

对应 Go：``internal/ops/ratelimit/ratelimit.go``。

``rate_per_second <= 0`` 关闭限流；``burst <= 0`` 视为 1；``idle_ttl <= 0``
视为 10 分钟。空闲 key 每 1024 次 ``allow`` 或桶数达到 4096 时清理一次。

``Decision.retry_after`` 是秒数（``float``），对应 Go 的 ``time.Duration``。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

__all__ = ["Decision", "Limit", "Limiter", "new"]

_DEFAULT_IDLE_TTL = 600.0
_CLEANUP_EVERY_N_ALLOWS = 1024
_CLEANUP_WHEN_BUCKET_COUNT = 4096


@dataclass
class Limit:
    """一类令牌桶的配置。对应 Go ``ratelimit.Limit``。"""

    rate_per_second: float = 0.0
    burst: int = 0
    idle_ttl: float = 0.0


@dataclass
class Decision:
    """一次 ``allow`` 的结果。对应 Go ``ratelimit.Decision``。"""

    allowed: bool = False
    remaining: int = 0
    retry_after: float = 0.0


class _Bucket:
    __slots__ = ("last", "last_seen", "tokens")

    def __init__(self, tokens: float, now: float) -> None:
        self.tokens = tokens
        self.last = now
        self.last_seen = now


class Limiter:
    """多个独立 key 共享同一份桶配置。对应 Go ``ratelimit.Limiter``。"""

    def __init__(self, limit: Limit | None = None) -> None:
        limit = limit or Limit()
        if limit.burst <= 0:
            limit.burst = 1
        if limit.idle_ttl <= 0:
            limit.idle_ttl = _DEFAULT_IDLE_TTL
        self._limit = limit
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._requests = 0
        #: 可注入时钟（对应 Go 的 ``now func() time.Time``）。
        self.now: Callable[[], float] = time.monotonic

    @property
    def limit(self) -> Limit:
        return self._limit

    def allow(self, key: str) -> Decision:
        """判断 ``key`` 能否消耗一个令牌。"""
        if self._limit.rate_per_second <= 0:
            return Decision(allowed=True)
        key = (key or "").strip()
        if not key:
            return Decision(allowed=True)

        now = self.now()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(float(self._limit.burst), now)
                self._buckets[key] = bucket
            elapsed = now - bucket.last
            if elapsed > 0:
                bucket.tokens += elapsed * self._limit.rate_per_second
                if bucket.tokens > float(self._limit.burst):
                    bucket.tokens = float(self._limit.burst)
                bucket.last = now
            bucket.last_seen = now
            self._requests += 1
            self._cleanup_locked(now)

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return Decision(allowed=True, remaining=int(bucket.tokens))
            retry_after = (1 - bucket.tokens) / self._limit.rate_per_second
            return Decision(allowed=False, remaining=0, retry_after=retry_after)

    def keys(self) -> int:
        """当前跟踪的 key 数（对应 Go ``Limiter.Keys``）。"""
        with self._lock:
            return len(self._buckets)

    def _cleanup_locked(self, now: float) -> None:
        if (
            self._requests % _CLEANUP_EVERY_N_ALLOWS != 0
            and len(self._buckets) < _CLEANUP_WHEN_BUCKET_COUNT
        ):
            return
        for key in [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.last_seen > self._limit.idle_ttl
        ]:
            del self._buckets[key]


def new(limit: Limit | None = None) -> Limiter:
    """对应 Go ``ratelimit.New``。"""
    return Limiter(limit)
