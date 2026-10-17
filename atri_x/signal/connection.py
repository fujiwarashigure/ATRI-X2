"""接收者连接句柄。

对应 Go：``internal/signal/connection.go``。

``Connection`` 只影响**未来**的 emission：``disconnect`` 保留已发出的
snapshot 与已在跑的任务；``begin_close`` 会额外取消已接受的工作。
"""

from __future__ import annotations

import asyncio

from atri_x.safecontext import CancelToken

__all__ = ["Connection"]


class _ConnectionTask:
    """一次已接受的任务（只持有它的取消函数）。"""

    __slots__ = ("cancel",)

    def __init__(self, cancel: CancelToken) -> None:
        self.cancel = cancel


class Connection:
    """一次 ``Connect`` 的句柄。对应 Go ``signal.Connection``。"""

    __slots__ = ("_closed", "_disconnect", "_disconnected", "_done", "_tasks")

    def __init__(self, disconnect) -> None:
        self._disconnect = disconnect
        self._disconnected = False
        self._closed = False
        self._tasks: dict[_ConnectionTask, None] = {}
        self._done = asyncio.Event()

    # -- 任务登记 -----------------------------------------------------------

    def acquire(self, cancel: CancelToken) -> _ConnectionTask | None:
        """登记一次已接受的任务；连接已关闭时返回 ``None``。"""
        if self._closed:
            return None
        task = _ConnectionTask(cancel)
        self._tasks[task] = None
        return task

    def release(self, task: _ConnectionTask) -> None:
        self._tasks.pop(task, None)
        if self._closed and not self._tasks:
            self._done.set()

    # -- 断开与关闭 ---------------------------------------------------------

    def disconnect(self) -> None:
        """断开未来的 emission（不取消已接受的工作）。"""
        if not self._disconnected:
            self._disconnected = True
            self._disconnect()

    def begin_close(self) -> None:
        """断开并取消已接受的工作。"""
        if self is None:  # pragma: no cover - 对应 Go 的 nil 接收者保护
            return
        self.disconnect()
        self._closed = True
        for task in list(self._tasks):
            task.cancel.cancel("connection closed")
        if not self._tasks:
            self._done.set()

    async def close(self, ctx: CancelToken | None = None) -> None:
        """等待所有已接受任务真正退出；``ctx`` 被取消时抛它的错误。"""
        if self is None:  # pragma: no cover
            return
        self.begin_close()
        if self._done.is_set():
            return
        if ctx is None:
            await self._done.wait()
            return
        if ctx.is_cancelled():
            raise ctx.error()
        loop = asyncio.get_running_loop()
        cancel_future: asyncio.Future = loop.create_future()

        def _on_cancel() -> None:
            if not cancel_future.done():
                cancel_future.set_result(None)

        remove = ctx.add_callback(_on_cancel)
        waiter = asyncio.ensure_future(self._done.wait())
        try:
            await asyncio.wait({waiter, cancel_future}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            remove()
            if not waiter.done():
                waiter.cancel()
        if not self._done.is_set():
            raise ctx.error()

    def done(self) -> asyncio.Event:
        """对应 Go ``Connection.Done()`` 返回的 channel。"""
        return self._done
