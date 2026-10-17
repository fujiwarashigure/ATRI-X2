"""有界并发限制器（带可选等待队列）。

对应 Go：``internal/ops/concurrency/limiter.go``。

``max <= 0`` 表示不限流。异步等待队列按 FIFO 让位：释放时优先把槽位直接
交给队首等待者（不减少 active 计数），队列空时才把 active 减一。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from atri_x.errors import QueueFullError, QueueTimeoutError
from atri_x.safecontext import CancelToken

__all__ = ["Config", "ErrFull", "ErrTimeout", "Limiter", "new"]

#: 等待队列已满（对应 Go ``concurrency.ErrFull``）。
ErrFull = QueueFullError("concurrency queue full")
#: 等待槽位超时（对应 Go ``concurrency.ErrTimeout``）。
ErrTimeout = QueueTimeoutError("concurrency wait timeout")


@dataclass
class Config:
    """限制器配置。对应 Go ``concurrency.Config``。"""

    max: int = 0
    queue_size: int = 0
    wait_timeout: float = 0.0


class _Waiter:
    __slots__ = ("granted", "ready")

    def __init__(self, ready: asyncio.Future) -> None:
        self.ready = ready
        self.granted = False


class Limiter:
    """限制并发并可选排队。对应 Go ``concurrency.Limiter``。"""

    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg or Config()
        self._active = 0
        self._waiters: list[_Waiter] = []

    @property
    def config(self) -> Config:
        return self._cfg

    async def acquire(self, ctx: CancelToken | None = None) -> Callable[[], None]:
        """等一个槽位并返回释放函数（幂等）。

        不限流时立即返回空释放函数；队满抛 :data:`ErrFull`，等待超时抛
        :data:`ErrTimeout`，``ctx`` 取消时抛该 ctx 的错误。
        """
        if self._cfg.max <= 0:
            return _noop
        if ctx is None:
            ctx = CancelToken()
        if self._active < self._cfg.max:
            self._active += 1
            return self._release_func()
        if self._cfg.queue_size <= 0 or len(self._waiters) >= self._cfg.queue_size:
            raise ErrFull
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop.create_future())
        self._waiters.append(waiter)

        cancel_future: asyncio.Future = loop.create_future()

        def _on_cancel() -> None:
            if not cancel_future.done():
                cancel_future.set_result(None)

        remove = ctx.add_callback(_on_cancel)
        wait_err: BaseException | None = None
        try:
            done, _ = await asyncio.wait(
                {waiter.ready, cancel_future},
                timeout=self._cfg.wait_timeout or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter.ready in done:
                return self._release_func()
            if cancel_future in done:
                wait_err = ctx.error()
            else:
                wait_err = ErrTimeout
        finally:
            remove()
            for future in (waiter.ready, cancel_future):
                if not future.done():
                    future.cancel()
        if waiter.granted:
            return self._release_func()
        self._remove_waiter(waiter)
        if wait_err is None:
            wait_err = ErrTimeout
        raise wait_err

    def stats(self) -> tuple[int, int]:
        """返回 ``(active, waiting)``（对应 Go ``Limiter.Stats``）。"""
        return self._active, len(self._waiters)

    # -- 内部 ---------------------------------------------------------------

    def _release_func(self) -> Callable[[], None]:
        released = False

        def _release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._release()

        return _release

    def _release(self) -> None:
        if self._waiters:
            waiter = self._waiters.pop(0)
            waiter.granted = True
            if not waiter.ready.done():
                waiter.ready.set_result("granted")
            return
        if self._active > 0:
            self._active -= 1

    def _remove_waiter(self, target: _Waiter) -> None:
        for index, waiter in enumerate(self._waiters):
            if waiter is target:
                del self._waiters[index]
                return


def _noop() -> None:
    return None


def new(cfg: Config | None = None) -> Limiter:
    """对应 Go ``concurrency.New``。"""
    return Limiter(cfg)
