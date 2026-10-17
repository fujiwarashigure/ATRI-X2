"""ASR 成功缓存与负缓存。

对应 Go：``internal/asr/cache.go``。

两者都是「TTL + 条数上限」的小映射，淘汰策略是最早到期者优先（earliest-expiry
eviction），与 Go 一致：先清掉已过期项，仍满则删 expiry 最早的一条。

Go 用 ``sync.Mutex``；Python 侧这里的方法都是同步、内部无 ``await`` 的临界区，
用 :class:`threading.Lock` 保留同样的互斥语义（服务本身是 async 的，但缓存不
需要 async 锁）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from atri_x.asr.error import HTTPError, deterministic_error

__all__ = [
    "DEFAULT_CACHE_MAX_ENTRIES",
    "DEFAULT_CACHE_TTL",
    "DEFAULT_MAX_CACHE_VALUE_SIZE",
    "DEFAULT_NEGATIVE_CACHE_MAX_ENTRIES",
    "DEFAULT_NEGATIVE_CACHE_TTL",
    "NegativeCache",
    "SuccessCache",
    "new_negative_cache",
    "new_success_cache",
]

#: 成功缓存 TTL（秒）。对应 Go ``DefaultCacheTTL``。
DEFAULT_CACHE_TTL = 30 * 60.0
#: 成功缓存条数上限。对应 Go ``DefaultCacheMaxEntries``。
DEFAULT_CACHE_MAX_ENTRIES = 128
#: 单条成功缓存文本的上限（字节）。对应 Go ``DefaultMaxCacheValueSize``。
DEFAULT_MAX_CACHE_VALUE_SIZE = 8 * 1024
#: 负缓存 TTL（秒）。对应 Go ``DefaultNegativeCacheTTL``。
DEFAULT_NEGATIVE_CACHE_TTL = 30.0
#: 负缓存条数上限。对应 Go ``DefaultNegativeCacheMaxEntries``。
DEFAULT_NEGATIVE_CACHE_MAX_ENTRIES = 128


def _normalize_now(now: Callable[[], float] | None) -> Callable[[], float]:
    return now if now is not None else time.monotonic


class SuccessCache:
    """成功转写结果的小缓存。对应 Go ``successCache``。

    ``get`` 返回的条目带 ``cached=True``；``put`` 会先归一化文本，空文本或超过
    ``max_value_size`` 的内容不入缓存（一条超长转写不值得占着槽位）。
    """

    def __init__(
        self,
        max_entries: int = 0,
        ttl: float = 0.0,
        max_value_size: int = 0,
        now: Callable[[], float] | None = None,
    ) -> None:
        if max_entries <= 0:
            max_entries = DEFAULT_CACHE_MAX_ENTRIES
        if ttl <= 0:
            ttl = DEFAULT_CACHE_TTL
        if max_value_size <= 0:
            max_value_size = DEFAULT_MAX_CACHE_VALUE_SIZE
        self._entries: dict[str, tuple[Any, float]] = {}
        self._max = max_entries
        self._ttl = ttl
        self._max_value_size = max_value_size
        self._now = _normalize_now(now)
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """命中则返回 ``cached=True`` 的结果副本，否则 ``None``。对应 Go ``get``。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if self._now() >= expiry:
                del self._entries[key]
                return None
            return replace(value, cached=True)

    def put(self, key: str, value: Any) -> None:
        """写入一条成功结果。对应 Go ``put``。"""
        # 延迟导入：asr.py 需要 cache.py，而 ``CleanText`` 按 Go 的文件划分留在
        # asr.py 里（Go 的 cache.go 也是调用 asr.go 的 CleanText）。
        from atri_x.asr.asr import clean_text

        text = clean_text(value.text)
        if not text or len(text) > self._max_value_size:
            return
        stored = replace(value, text=text, cached=False)
        with self._lock:
            now = self._now()
            for candidate, (_, expiry) in list(self._entries.items()):
                if now >= expiry:
                    del self._entries[candidate]
            while len(self._entries) >= self._max:
                oldest_key = ""
                oldest = 0.0
                for candidate, (_, expiry) in self._entries.items():
                    if not oldest_key or expiry < oldest:
                        oldest_key, oldest = candidate, expiry
                if not oldest_key:
                    break
                del self._entries[oldest_key]
            self._entries[key] = (stored, now + self._ttl)


@dataclass(slots=True)
class _NegativeEntry:
    status_code: int
    message: str
    expiry: float


class NegativeCache:
    """只记确定性失败，免得一个坏配置每条录音都打一次 provider。

    对应 Go ``negativeCache``。
    """

    def __init__(
        self, max_entries: int = 0, ttl: float = 0.0, now: Callable[[], float] | None = None
    ) -> None:
        if max_entries <= 0:
            max_entries = DEFAULT_NEGATIVE_CACHE_MAX_ENTRIES
        if ttl <= 0:
            ttl = DEFAULT_NEGATIVE_CACHE_TTL
        self._entries: dict[str, _NegativeEntry] = {}
        self._max = max_entries
        self._ttl = ttl
        self._now = _normalize_now(now)
        self._lock = threading.Lock()

    def get(self, key: str) -> HTTPError | None:
        """命中则返回当初那条错误，否则 ``None``。对应 Go ``get``。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._now() >= entry.expiry:
                del self._entries[key]
                return None
            return HTTPError(status_code=entry.status_code, message=entry.message)

    def put(self, key: str, err: BaseException | None) -> None:
        """记下确定性失败。对应 Go ``put``。"""
        if not deterministic_error(err) or not isinstance(err, HTTPError):
            return
        with self._lock:
            now = self._now()
            for candidate, entry in list(self._entries.items()):
                if now >= entry.expiry:
                    del self._entries[candidate]
            while len(self._entries) >= self._max:
                oldest_key = ""
                oldest = 0.0
                for candidate, entry in self._entries.items():
                    if not oldest_key or entry.expiry < oldest:
                        oldest_key, oldest = candidate, entry.expiry
                if not oldest_key:
                    break
                del self._entries[oldest_key]
            self._entries[key] = _NegativeEntry(
                status_code=err.status_code, message=err.message, expiry=now + self._ttl
            )


#: Go 名别名（``newSuccessCache`` / ``newNegativeCache``）。
new_success_cache = SuccessCache
new_negative_cache = NegativeCache
