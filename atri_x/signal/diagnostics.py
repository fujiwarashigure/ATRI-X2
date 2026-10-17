"""signal 内部诊断输出。

对应 Go：``internal/signal/diagnostics.go``。

诊断 I/O 有**独立**的有界 worker：阻塞的 stderr 不会占住执行槽位，溢出只
计数并在恢复后合并上报。测试可以读取 :func:`diagnostics_written` 里的内容。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from atri_x.safecontext import CancelToken

__all__ = [
    "flush_diagnostics",
    "report_failure",
    "diagnostics_written",
    "dropped_diagnostics",
]

_CAPACITY = 64
_QUEUE: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=_CAPACITY)
_WORKER: asyncio.Task | None = None
_FLUSH_WAITERS: list[asyncio.Future] = []
_DROPPED = 0
#: 已写出的诊断行（便于测试断言，等价于上游把 stderr 重定向到文件）。
_WRITTEN: list[str] = []


def diagnostics_written() -> list[str]:
    """返回已写出的诊断行。"""
    return list(_WRITTEN)


def dropped_diagnostics() -> int:
    """返回因队列满而被丢弃（尚未合并上报）的诊断数。"""
    return _DROPPED


def reset_diagnostics() -> None:
    """清空已写出的诊断（测试用；不影响队列里的待写项）。"""
    _WRITTEN.clear()


def _format(message: str, attrs: dict[str, Any]) -> str:
    if not attrs:
        return message
    extra = " ".join(f"{key}={value}" for key, value in attrs.items())
    return f"{message} {extra}"


def _emit(text: str) -> None:
    _WRITTEN.append(text)
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover - stderr 不可写时只保留内存副本
        pass


def report_failure(message: str, **attrs: Any) -> None:
    """上报一次失败；队列满时只计数（对应 Go ``reportFailure``）。"""
    global _DROPPED
    try:
        _QUEUE.put_nowait((message, attrs))
    except asyncio.QueueFull:
        _DROPPED += 1
        return
    _ensure_worker()


def _ensure_worker() -> None:
    global _WORKER
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环：退化为同步写出（Go 里是独立 goroutine）。
        _drain_now()
        return
    # worker 与事件循环亲和：上一个循环留下的 task 不能服务新循环
    # （测试里每个用例都是一个新循环）。
    if _WORKER is not None and not _WORKER.done() and _WORKER.get_loop() is loop:
        return
    _WORKER = loop.create_task(_worker())


def _drain_now() -> None:
    global _DROPPED
    while True:
        try:
            item = _QUEUE.get_nowait()
        except asyncio.QueueEmpty:
            _resolve_flush_waiters()
            return
        _handle(item)


def _handle(item: Any) -> None:
    global _DROPPED
    if isinstance(item, tuple) and item and item[0] is None:
        _, future = item
        if not future.done():
            future.set_result(None)
        return
    message, attrs = item
    dropped = _DROPPED
    if dropped:
        _DROPPED = 0
        _emit(_format("signal diagnostics dropped", {"count": dropped}))
    _emit(_format(message, attrs))


async def _worker() -> None:
    while True:
        try:
            item = _QUEUE.get_nowait()
        except asyncio.QueueEmpty:
            break
        _handle(item)
    _resolve_flush_waiters()


def _resolve_flush_waiters() -> None:
    """队列排空后唤醒 ``flush_diagnostics`` 的等待者。"""
    waiters, _FLUSH_WAITERS[:] = list(_FLUSH_WAITERS), []
    for future in waiters:
        if not future.done():
            future.set_result(None)


async def flush_diagnostics(ctx: CancelToken | None = None) -> None:
    """等待此前已准入的诊断写出去（调用方必须给 ``ctx`` 设界）。

    与 Go 的差异：Go 往有界 channel 里塞一个 barrier，channel 满时会阻塞；
    这里改为把等待者登记在独立的表里，由 worker 排空后唤醒，因此不会被
    诊断积压拖住。
    """
    loop = asyncio.get_running_loop()
    if _QUEUE.empty():
        return
    if ctx is not None and ctx.is_cancelled():
        raise ctx.error()
    future: asyncio.Future = loop.create_future()
    _FLUSH_WAITERS.append(future)
    _ensure_worker()
    if ctx is None:
        await future
        return
    cancel_future: asyncio.Future = loop.create_future()

    def _on_cancel() -> None:
        if not cancel_future.done():
            cancel_future.set_result(None)

    remove = ctx.add_callback(_on_cancel)
    try:
        await asyncio.wait({future, cancel_future}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        remove()
        if not cancel_future.done():
            cancel_future.cancel()
    if not future.done():
        try:
            _FLUSH_WAITERS.remove(future)
        except ValueError:
            pass
        future.cancel()
        raise ctx.error()
