"""进程内缓存与在途任务合并。

对应 Go：``internal/vision/cache.go``。

三层结构，按 Go 逐一复刻：

* :class:`SuccessCache` —— 带 TTL 的小 map，驱逐时选**最早过期**的条目；
* :class:`NegativeCache` —— 只记确定性、安全的失败，使一个「已知坏配置」
  不必对每张图都打一次上游；它存结构化字段，绝不存原始响应体；
* :class:`Inflight` —— 把并发的同 key 任务合并成一次上游调用。

并发：Go 的 ``sync.Mutex`` → ``asyncio.Lock``；``sync.WaitGroup`` 语义用
``asyncio.Future``。``context.WithoutCancel`` 在 Python 侧没有对应 API，用
:class:`DetachedContext` 复刻（保留值传递、去掉取消传播）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from atri_x.errors import ErrTooManyWaiters
from atri_x.llm.errors import APIError, as_api_error
from atri_x.safecontext import CancelToken, CancelledError, DeadlineExceededError, with_timeout
from atri_x.vision.service_types import Result

__all__ = [
    "DefaultCacheMaxEntries",
    "DefaultCacheTTL",
    "DefaultMaxCacheValueSize",
    "DefaultNegativeCacheTTL",
    "DefaultNegativeMax",
    "DefaultSharedTimeout",
    "DetachedContext",
    "Inflight",
    "NegativeCache",
    "SuccessCache",
    "is_cancellation",
]

#: 进程内缓存的默认值。刻意保守：即使运维什么都不调，小容器也保持有界。
DefaultCacheTTL = 30 * 60.0
DefaultCacheMaxEntries = 128
DefaultMaxCacheValueSize = 16 * 1024
DefaultNegativeCacheTTL = 30.0
DefaultNegativeMax = 128
DefaultSharedTimeout = 3 * 60.0

_MISSING = object()
_LOOP_MISSING = object()


def _default_now() -> float:
    """默认时钟（单调秒）。Go 用 ``time.Now``。"""
    return time.monotonic()


def is_cancellation(err: BaseException | None) -> bool:
    """对应 Go 的 ``errors.Is(err, context.Canceled|DeadlineExceeded)``。"""
    seen = 0
    current: Any = err
    while current is not None and seen < 32:
        if isinstance(
            current,
            (asyncio.CancelledError, TimeoutError, CancelledError, DeadlineExceededError),
        ):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "cause", None)
        seen += 1
    return False


class DetachedContext:
    """``context.WithoutCancel(ctx)`` 的 Python 对应物。

    保留调用者的**值传递**，但切断取消传播；随后由 :class:`Inflight` 显式
    重新施加超时/截止与服务级取消。之所以需要它：一个调用者放弃，不能把
    共享任务对其他调用者一起杀掉。

    它暴露 :class:`atri_x.safecontext.CancelToken` 用到的两个私有协议点
    （``_register_child`` 与 ``_parent`` / ``_values``），好让
    ``with_timeout(detached, ...)`` 既能挂真实子 token，又能让 ``value()``
    的向上遍历穿透本对象回到原 ctx。这是 Python 侧的必要偏差：标准库没有
    「保留值、丢弃取消」的上下文原语。
    """

    __slots__ = ("_ctx_parent", "_token")

    def __init__(self, parent: CancelToken | None = None) -> None:
        self._ctx_parent = parent
        self._token = CancelToken(None)

    # -- CancelToken 的私有协议 --------------------------------------------

    @property
    def _parent(self) -> CancelToken | None:
        return self._ctx_parent

    @property
    def _values(self) -> dict[Any, Any]:
        return self._token._values  # noqa: SLF001 - 见类 docstring

    def _register_child(self, child: CancelToken) -> None:
        self._token._register_child(child)  # noqa: SLF001 - 见类 docstring

    # -- 取消语义（只反映自身，不被 parent 传播） ---------------------------

    def is_cancelled(self) -> bool:
        return self._token.is_cancelled()

    def is_done(self) -> bool:
        return self._token.is_cancelled()

    def error(self) -> BaseException | None:
        return self._token.error()

    def error_or(self, fallback: BaseException) -> BaseException:
        return self._token.error_or(fallback)

    def raise_if_cancelled(self) -> None:
        self._token.raise_if_cancelled()

    def cancel(self, reason: str | None = None, error: BaseException | None = None) -> None:
        self._token.cancel(reason, error)

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._token.add_callback(callback)

    def value(self, key: Any, default: Any = None) -> Any:
        found = self._token.value(key, _MISSING)
        if found is not _MISSING:
            return found
        if self._ctx_parent is not None:
            return self._ctx_parent.value(key, default)
        return default


@dataclass
class _SuccessEntry:
    value: Result
    expiry: float


class SuccessCache:
    """带 TTL、按最早过期驱逐的小 map。对应 Go ``successCache``。"""

    def __init__(
        self,
        max_entries: int = 0,
        ttl: float = 0.0,
        max_value_size: int = 0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._now = now or _default_now
        self._max = max_entries if max_entries > 0 else DefaultCacheMaxEntries
        self._ttl = ttl if ttl > 0 else DefaultCacheTTL
        self._max_value_size = (
            max_value_size if max_value_size > 0 else DefaultMaxCacheValueSize
        )
        self._entries: dict[str, _SuccessEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> tuple[Result, bool]:
        """读一条未过期结果（返回值带 ``cached=True``）。对应 Go ``get``。"""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return Result(), False
            if not self._now() < entry.expiry:
                del self._entries[key]
                return Result(), False
            value = entry.value
            return _with_cached(value), True

    async def put(self, key: str, value: Result) -> None:
        """写一条结果。对应 Go ``put``。

        空值或超长值**永不**缓存，这样一个不守规矩的上游无法把缓存撑爆；
        配合条目上限，缓存持有的总字节数也是有界的。
        """
        value = Result(
            text=(value.text or "").strip(),
            provider=value.provider,
            model=value.model,
            cached=False,
        )
        if value.text == "" or len(value.text.encode("utf-8")) > self._max_value_size:
            return
        async with self._lock:
            now = self._now()
            for candidate in [k for k, e in self._entries.items() if not now < e.expiry]:
                del self._entries[candidate]
            while len(self._entries) >= self._max:
                oldest_key = ""
                oldest = 0.0
                for candidate, entry in self._entries.items():
                    if oldest_key == "" or entry.expiry < oldest:
                        oldest_key, oldest = candidate, entry.expiry
                if oldest_key == "":
                    break
                del self._entries[oldest_key]
            self._entries[key] = _SuccessEntry(value=value, expiry=now + self._ttl)


def _with_cached(value: Result) -> Result:
    return Result(
        text=value.text,
        provider=value.provider,
        model=value.model,
        cached=True,
    )


@dataclass
class _NegativeEntry:
    status_code: int = 0
    code: str = ""
    type_name: str = ""
    param: str = ""
    message: str = ""
    #: 适配器控制的失败类别；回放时原样带出，保证缓存命中与首次失败分类一致。
    category: str = ""
    expiry: float = 0.0


class NegativeCache:
    """只记住确定性失败的小 map。对应 Go ``negativeCache``。"""

    def __init__(
        self,
        max_entries: int = 0,
        ttl: float = 0.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._now = now or _default_now
        self._max = max_entries if max_entries > 0 else DefaultNegativeMax
        self._ttl = ttl if ttl > 0 else DefaultNegativeCacheTTL
        self._entries: dict[str, _NegativeEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> APIError | None:
        """回放一条未过期的确定性失败。对应 Go ``get``。"""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if not self._now() < entry.expiry:
                del self._entries[key]
                return None
            return APIError(
                entry.message,
                status_code=entry.status_code,
                code=entry.code,
                type=entry.type_name,
                param=entry.param,
                category=entry.category,
            )

    async def put(self, key: str, err: BaseException | None) -> None:
        """只存明确的、确定性的 API 失败。对应 Go ``put``。

        瞬时失败（429、5xx、网络）与任何取消/超时永不缓存，无论网关给它们挂了
        什么状态码。
        """
        if err is None or is_cancellation(err):
            return
        api_err = as_api_error(err)
        if api_err is None or not api_err.deterministic():
            return
        async with self._lock:
            now = self._now()
            for candidate in [k for k, e in self._entries.items() if not now < e.expiry]:
                del self._entries[candidate]
            while len(self._entries) >= self._max:
                oldest_key = ""
                oldest = 0.0
                for candidate, entry in self._entries.items():
                    if oldest_key == "" or entry.expiry < oldest:
                        oldest_key, oldest = candidate, entry.expiry
                if oldest_key == "":
                    break
                del self._entries[oldest_key]
            self._entries[key] = _NegativeEntry(
                status_code=api_err.status_code,
                code=api_err.code,
                type_name=api_err.type,
                param=api_err.param,
                message=api_err.message,
                category=api_err.category,
                expiry=now + self._ttl,
            )


@dataclass
class _InflightCall:
    done: asyncio.Future
    result: Result = field(default_factory=Result)
    err: BaseException | None = None
    #: 正在等待 done 的调用者数量；由 ``Inflight`` 的锁保护，用来给单个上游
    #: 任务后面的队列封顶。
    waiters: int = 0


class Inflight:
    """把并发的同 key 任务合并成一次上游调用。对应 Go ``inflight``。

    生命周期：共享任务跑在**脱离首个调用者取消**的上下文里，所以一个调用者
    中止不会连累其他人；它仍然继承调用者的截止时间（单请求超时必须能约束这段
    工作），并且在服务上下文取消时一并取消，使关闭服务能真正停掉在途上游工作，
    而不是让它跑到共享超时。每个 waiter 可以独立放弃自己的等待；即使全部
    waiter 都走了，工作仍受 ``shared_timeout`` 约束。
    """

    def __init__(self, base: CancelToken | None = None) -> None:
        self._calls: dict[str, _InflightCall] = {}
        self._base = base
        self._lock = asyncio.Lock()

    async def do(
        self,
        ctx: CancelToken | None,
        key: str,
        shared_timeout: float,
        max_waiters: int,
        fn: Callable[[Any], Awaitable[Result]],
        deadline: float | None = None,
    ) -> tuple[Result, bool]:
        """执行（或复用）一个任务，返回 ``(结果, 是否合并等待)``。

        对应 Go ``inflight.do``。失败时抛异常。

        偏差（Python 侧必需）：Go 从 ``ctx`` 上读 ``Deadline()``，而
        :class:`atri_x.safecontext.CancelToken` 不暴露截止时间，所以调用方用
        ``deadline``（``time.monotonic()`` 基准）显式传入；``None`` 表示无截止。
        """
        current = _ctx_error(ctx)
        if current is not None:
            raise current
        async with self._lock:
            call = self._calls.get(key)
            if call is not None:
                if max_waiters > 0 and call.waiters >= max_waiters:
                    raise ErrTooManyWaiters
                call.waiters += 1
                reuse = True
            else:
                call = _InflightCall(done=asyncio.get_running_loop().create_future())
                self._calls[key] = call
                reuse = False
        if reuse:
            try:
                return await self._wait(ctx, call), True
            finally:
                async with self._lock:
                    call.waiters -= 1

        if shared_timeout <= 0:
            shared_timeout = DefaultSharedTimeout
        timeout = shared_timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 截止时间已过：不启动上游，直接把错误交给等待者。
                call.err = DeadlineExceededError("context deadline exceeded")
                call.done.set_result(None)
                async with self._lock:
                    self._calls.pop(key, None)
                raise call.err
            timeout = min(timeout, remaining)
        token = with_timeout(DetachedContext(ctx), timeout)
        remove_base: Callable[[], None] | None = None
        if self._base is not None:
            remove_base = self._base.add_callback(
                lambda: token.cancel("service context canceled")
            )
        task = asyncio.ensure_future(self._leader(key, call, token, fn, remove_base))
        # 任务引用保住即可；异常已经在 _leader 内部转成结果/错误。
        task.add_done_callback(_consume_task)
        return await self._wait(ctx, call), False

    async def _leader(
        self,
        key: str,
        call: _InflightCall,
        token: CancelToken,
        fn: Callable[[Any], Awaitable[Result]],
        remove_base: Callable[[], None] | None,
    ) -> None:
        """跑共享任务并结算给所有 waiter。对应 Go 里那段 ``go func()``。"""
        # 共享上下文被取消（共享超时 / 服务级取消）时，把 Python 任务一并
        # 取消：Go 靠 fn 自己 select ``ctx.Done()``，而 asyncio 里任务取消是
        # 唯一能穿透到正在 await 的协程的手段。
        task = asyncio.current_task()
        remove_task_cancel: Callable[[], None] | None = None
        if task is not None:
            remove_task_cancel = token.add_callback(task.cancel)
        result = Result()
        err: BaseException | None = None
        try:
            try:
                result = await fn(token)
            except BaseException as exc:  # 崩溃也要交给 waiter，不能带走事件循环
                result, err = Result(), exc
        finally:
            if remove_task_cancel is not None:
                remove_task_cancel()
            token.cancel("shared job finished")
            if remove_base is not None:
                remove_base()
            # 这段结算不含 await，在事件循环内天然原子；先写结果再移除 key，
            # 使窗口期内到达的调用者仍能复用刚完成的结果而不是再起一次。
            call.result, call.err = result, err
            self._calls.pop(key, None)
            if not call.done.done():
                call.done.set_result(None)

    async def _wait(self, ctx: CancelToken | None, call: _InflightCall) -> Result:
        """等共享任务或调用者取消，取先到者。对应 Go ``waitInflight``。"""
        if not call.done.done() and ctx is not None:
            loop = asyncio.get_running_loop()
            cancelled: asyncio.Future = loop.create_future()

            def _on_cancel() -> None:
                if not cancelled.done():
                    cancelled.set_result(None)

            remove = ctx.add_callback(_on_cancel)
            try:
                await asyncio.wait(
                    {call.done, cancelled}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                remove()
                if not cancelled.done():
                    cancelled.cancel()
            if not call.done.done():
                raise _ctx_error(ctx) or CancelledError("context canceled")
        else:
            await asyncio.shield(call.done)
        if call.err is not None:
            raise call.err
        return call.result


def _consume_task(task: "asyncio.Task[None]") -> None:
    """吃掉任务自身的异常（正常情况下为空），避免 asyncio 打「never retrieved」。"""
    if task.cancelled():
        return
    task.exception()


def _ctx_error(ctx: Any) -> BaseException | None:
    """``ctx.Err()``：未取消返回 ``None``。"""
    if ctx is None:
        return None
    error = ctx.error()
    if error is not None:
        return error
    if ctx.is_cancelled():
        return CancelledError("context canceled")
    return None
