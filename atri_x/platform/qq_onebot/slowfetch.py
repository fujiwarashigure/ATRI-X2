"""引用 / 转发的慢取：预算等待、单飞、成功 / 失败分级缓存。**Python 版新增**
（Go 侧没有对应文件）。

为什么需要：引用消息（``get_msg``）与合并转发（``get_forward_msg``）都在入站
预处理的 worker 里 ``await``。平台一慢，这几秒就变成整个入站队列的延迟，而
同一段被反复引用的消息还会被重复拉取。本模块提供三件事：

1. **短等待预算**：只在预算内等平台返回；超时就把这一次当作"没取到"，
   后台任务继续跑，结果进缓存 —— 入站不被拖住。
2. **单飞**：同一个消息 id 并发只发一次请求，其余调用等同一个 Future。
3. **分级缓存**：成功与失败分开 TTL（失败只记很短，避免"平台刚拒绝就重试"
   的风暴，也不把一次失败当成永久结论）。

缓存与单飞都按 key（``引用 id`` / ``转发 id``）组织；超时后仍在跑的 Future
由 :meth:`FetchCache._finish` 收尾并写入缓存，下一次调用（或 ``fetch_message``
工具）就能直接命中。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_FAILURE_TTL_SECONDS",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_SUCCESS_TTL_SECONDS",
    "FetchCache",
    "FetchOutcome",
    "cache_for",
    "fetch_now",
    "fetch_with_budget",
    "new_fetch_cache",
]

#: 默认短等待预算（秒）。超过就不等平台了，内容交给后台 + ``fetch_message``。
DEFAULT_BUDGET_SECONDS = 1.5
#: 成功结果缓存时长（秒）。引用 / 转发内容基本不可变，可以缓存久一点。
DEFAULT_SUCCESS_TTL_SECONDS = 300.0
#: 失败结果缓存时长（秒）。短：平台偶发失败不该长期生效，也不该立刻重试。
DEFAULT_FAILURE_TTL_SECONDS = 30.0
#: 缓存条目上限（LRU）。避免长连接里无限增长。
DEFAULT_MAX_ENTRIES = 512

#: 键前缀：平台名 + 种类，避免不同种类 / 平台共用一个 id 时串味。
KEY_PREFIX = "qqonebot"


@dataclass
class FetchOutcome:
    """一次慢取的结果。

    ``deferred=True`` 表示"预算内没等到，后台还在跑"：调用方应当用占位文本先把
    这一轮走完，之后可以用 ``fetch_message`` 取回内容。
    """

    ok: bool = False
    value: Any = None
    deferred: bool = False
    cached: bool = False


class FetchCache:
    """按 key 的 LRU + 分级 TTL 缓存，并持有在飞 Future（单飞）。"""

    def __init__(
        self,
        *,
        budget: float = DEFAULT_BUDGET_SECONDS,
        success_ttl: float = DEFAULT_SUCCESS_TTL_SECONDS,
        failure_ttl: float = DEFAULT_FAILURE_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.budget = float(budget)
        self.success_ttl = float(success_ttl)
        self.failure_ttl = float(failure_ttl)
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, tuple[float, bool, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        #: 诊断计数（``/status``、日志、测试都读它）。
        self.stats: dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "fetches": 0,
            "coalesced": 0,
            "deferred": 0,
            "failures": 0,
            "evictions": 0,
        }

    # -- 缓存 ---------------------------------------------------------------

    def get(self, key: str) -> tuple[bool, Any] | None:
        """读缓存。命中返回 ``(ok, value)``；没有或已过期返回 ``None``。"""
        entry = self._entries.get(key)
        if entry is None:
            self.stats["misses"] += 1
            return None
        expires_at, ok, value = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            self.stats["misses"] += 1
            return None
        self._entries.move_to_end(key)
        self.stats["hits"] += 1
        return ok, value

    def put(self, key: str, ok: bool, value: Any) -> None:
        """写入缓存（成功 / 失败用不同 TTL；TTL <= 0 表示不缓存）。"""
        ttl = self.success_ttl if ok else self.failure_ttl
        if ttl <= 0:
            return
        self._entries[key] = (time.monotonic() + ttl, ok, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.stats["evictions"] += 1

    def clear(self) -> None:
        """清空缓存（不影响在飞请求）。"""
        self._entries.clear()

    def inflight(self) -> int:
        """还在跑的请求数。"""
        return len(self._inflight)

    # -- 单飞 ---------------------------------------------------------------

    def task_for(
        self, key: str, loader: Callable[[], Awaitable[Any]]
    ) -> tuple[asyncio.Task, bool]:
        """取 / 建一个在飞任务。返回 ``(task, joined)``；``joined`` 表示复用了别人的请求。"""
        task = self._inflight.get(key)
        if task is not None:
            self.stats["coalesced"] += 1
            return task, True
        task = asyncio.ensure_future(loader())
        self._inflight[key] = task
        self.stats["fetches"] += 1
        task.add_done_callback(lambda finished: self._finish(key, finished))
        return task, False

    def _finish(self, key: str, task: asyncio.Task) -> None:
        """在飞任务收尾：写缓存、清在飞记录、吞掉异常引用。"""
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self.put(key, True, task.result())
            return
        self.stats["failures"] += 1
        self.put(key, False, None)


def new_fetch_cache(
    *,
    budget: float = DEFAULT_BUDGET_SECONDS,
    success_ttl: float = DEFAULT_SUCCESS_TTL_SECONDS,
    failure_ttl: float = DEFAULT_FAILURE_TTL_SECONDS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> FetchCache:
    """构造一个缓存（配置层只做数值归一，真正的默认值在本模块）。"""
    return FetchCache(
        budget=budget,
        success_ttl=success_ttl,
        failure_ttl=failure_ttl,
        max_entries=max_entries,
    )


_FALLBACK_CACHE = new_fetch_cache()


def cache_for(adapter: Any) -> FetchCache:
    """取适配器上的缓存；没有就挂一个（拿不到属性时退回模块级兜底）。"""
    cache = getattr(adapter, "fetch_cache", None)
    if isinstance(cache, FetchCache):
        return cache
    if adapter is None or not hasattr(adapter, "__dict__"):
        return _FALLBACK_CACHE
    created = new_fetch_cache()
    adapter.__dict__["fetch_cache"] = created
    return created


async def fetch_with_budget(
    cache: FetchCache,
    key: str,
    loader: Callable[[], Awaitable[Any]],
    budget: float | None = None,
) -> FetchOutcome:
    """缓存 → 单飞 → 在预算内等待 ``loader``。超时返回 ``deferred=True``。"""
    cached = cache.get(key)
    if cached is not None:
        ok, value = cached
        return FetchOutcome(ok=ok, value=value, cached=True)
    task, _joined = cache.task_for(key, loader)
    limit = cache.budget if budget is None else float(budget)
    if limit <= 0:
        done, _pending = await asyncio.wait({task})
    else:
        done, _pending = await asyncio.wait({task}, timeout=limit)
    if task not in done:
        # 预算用完：不取消后台任务，让它继续跑，结果由 _finish 写进缓存。
        cache.stats["deferred"] += 1
        return FetchOutcome(ok=False, deferred=True)
    return _settle(task)


async def fetch_now(
    cache: FetchCache,
    key: str,
    loader: Callable[[], Awaitable[Any]],
    timeout: float = 0.0,
) -> FetchOutcome:
    """不设短预算地取一次（``fetch_message`` 工具用；``timeout`` 由传输层兜底）。"""
    cached = cache.get(key)
    if cached is not None:
        ok, value = cached
        return FetchOutcome(ok=ok, value=value, cached=True)
    task, _joined = cache.task_for(key, loader)
    if timeout > 0:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            cache.stats["deferred"] += 1
            return FetchOutcome(ok=False, deferred=True)
    else:
        done, _pending = await asyncio.wait({task})
    if task not in done:
        return FetchOutcome(ok=False, deferred=True)
    return _settle(task)


def _settle(task: asyncio.Task) -> FetchOutcome:
    """把已结束的任务转成结果；异常按失败处理（异常已由 done callback 取走）。"""
    if task.cancelled():
        return FetchOutcome(ok=False)
    error = task.exception()
    if error is not None:
        return FetchOutcome(ok=False)
    return FetchOutcome(ok=True, value=task.result())


def reference_key(message_id: str) -> str:
    """引用消息的缓存键。"""
    return KEY_PREFIX + "\x00reference\x00" + (message_id or "").strip()


def forward_key(message_id: str) -> str:
    """合并转发的缓存键。"""
    return KEY_PREFIX + "\x00forward\x00" + (message_id or "").strip()
