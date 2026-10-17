"""按内容 ID 的对象锁。

对应 Go：``internal/media/locks.go``。

``ObjectLocks`` 串行化**同一个内容 ID** 的变更，且不阻塞其他 ID。条目同时统计
持有者与等待者，因此"同一个 ID 不会拿到两把锁"这件事由计数（``users``）而不是
锁本身保证 —— 这也是 ``concurrency_test.go`` 的 ``waitObjectUsers`` 直接读
``locks.entries[id].users`` 的原因，本仓保留同名属性便于对照。

与 Go 的差异：

* Go 用 ``chan struct{}`` 做令牌；本仓用 :class:`asyncio.Lock`，并把取消做成
  与 ``ctx`` 的竞争（``asyncio.wait`` + ``ctx.wait()``），语义与 Go 的
  ``select { case token <- ...; case <-ctx.Done() }`` 一致：**等待期间**可取消，
  拿到锁之后还会再查一次 ctx（Go 同样复查，此时先释放再返回错误）。
* 锁表用 ``threading.Lock`` 保护：条目增减是纯内存操作，且清理线程与事件循环
  线程都可能碰到它。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from atri_x.media.types import is_cancelled, raise_if_cancelled, wait_cancelled

__all__ = ["ObjectLocks", "object_locks"]


class _ObjectLock:
    """单个 ID 的令牌与用户计数。对应 Go ``objectLock``。"""

    __slots__ = ("token", "users")

    def __init__(self) -> None:
        self.token = asyncio.Lock()
        self.users = 0


class ObjectLocks:
    """按 ID 的对象锁表。对应 Go ``objectLocks``。"""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.entries: dict[str, _ObjectLock] = {}

    async def acquire(self, ctx: Any, id: str) -> Callable[[], None]:
        """获取 ``id`` 的锁，返回幂等的 ``unlock``。

        取消时抛 ctx 对应的异常（``ctx is None`` 时不检查）。
        """
        raise_if_cancelled(ctx)
        with self.mu:
            entry = self.entries.get(id)
            if entry is None:
                entry = _ObjectLock()
                self.entries[id] = entry
            entry.users += 1

        def drop() -> None:
            with self.mu:
                entry.users -= 1
                if entry.users <= 0 and self.entries.get(id) is entry:
                    del self.entries[id]

        try:
            await _acquire(entry.token, ctx)
        except BaseException:
            drop()
            raise
        try:
            raise_if_cancelled(ctx)
        except BaseException:
            entry.token.release()
            drop()
            raise

        released = False

        def unlock() -> None:
            nonlocal released
            if released:
                return
            released = True
            entry.token.release()
            drop()

        return unlock


async def _acquire(lock: asyncio.Lock, ctx: Any) -> None:
    """获取 ``lock``；返回时一定持锁，抛异常时一定没持锁。"""
    waiter = None if ctx is None else wait_cancelled(ctx)
    if waiter is None:
        await lock.acquire()
        return
    acquire_task = asyncio.ensure_future(lock.acquire())
    cancel_task = asyncio.ensure_future(waiter)
    try:
        await asyncio.wait({acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (acquire_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
    if acquire_task.done() and not acquire_task.cancelled() and acquire_task.exception() is None:
        if not is_cancelled(ctx):
            return
        # Go：拿到令牌后再查一次 ctx，取消时先 unlock 再返回错误。
        lock.release()
    raise_if_cancelled(ctx)
    raise asyncio.CancelledError("object lock acquisition aborted")


#: Go 的未导出类型名 ``objectLocks`` 的 snake_case 别名，便于交叉检索。
object_locks = ObjectLocks
