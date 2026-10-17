"""按 key 分道的调度队列。

对应 Go：``internal/signal/queue.go``。

与上游的结构一一对应：``lanes`` 是 key → 队列，``ready`` 是可运行的分道，
``active`` 是正在跑的 job。**等待 key 永远不占用 worker**；空闲 worker 会
退休；准入时最多启动 ``concurrency`` 个 worker。

语义要点（不得简化）：

- ``capacity`` 只算等待中的回调，正在运行的不占容量；满了返回
  :data:`atri_x.signal.errors.ErrQueueFull`（背压，不是故障），除非
  ``wait_for_capacity`` 打开。
- 非空 key 的 job 串行执行，空 key（``""``）不串行。
- 关闭时先 ``closed = True`` 停止接纳，再按 ``ShutdownPolicy`` 排空：
  ``CancelPending`` 直接取消，``Drain`` 等它跑完。
- ``done`` 只在"已关闭 + 没有 worker + 没有 pending + 没有正在收尾的
  Finish"时才置位。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from atri_x.safecontext import CancelToken, with_cancel, with_timeout
from atri_x.signal.diagnostics import report_failure
from atri_x.signal.errors import ErrClosed, ErrQueueFull
from atri_x.signal.shutdown import SHUTDOWN_POLICIES, CancelPending, Drain, Task

__all__ = ["Queue", "QueueOptions", "new_queue"]


@dataclass
class QueueOptions:
    """对应 Go ``signal.QueueOptions``。"""

    name: str = ""
    capacity: int = 0  # 等待中的回调上限；正在运行的有独立上限
    concurrency: int = 0  # 业务队列缺省 1；signal 显式用 8/10
    wait_for_capacity: bool = False  # 等准入，由 ctx 或 admission_timeout 兜底
    admission_timeout: float = 0.0  # 不会变成被准入任务的截止时间


class _Lane:
    __slots__ = ("active", "jobs", "key", "ready")

    def __init__(self, key: str) -> None:
        self.key = key
        self.jobs: list[_Job] = []
        self.ready = False
        self.active = 0


class _Job:
    __slots__ = ("cancel", "ctx", "lane", "pending", "stop", "task")

    def __init__(self, ctx: CancelToken, task: Task, lane: _Lane) -> None:
        self.ctx = ctx
        self.task = task
        self.lane = lane
        self.pending = True
        self.stop = None
        self.cancel: CancelToken | None = None


def _resolve(future: asyncio.Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)


class Queue:
    """有界、按 key 分道的执行队列。对应 Go ``signal.Queue``。"""

    def __init__(self, options: QueueOptions | None = None) -> None:
        options = options or QueueOptions()
        if (
            options.capacity < 0
            or options.concurrency < 0
            or options.admission_timeout < 0
        ):
            raise ValueError("signal: negative queue option")
        capacity = options.capacity or 256
        concurrency = options.concurrency or 1
        self._capacity = capacity
        self._concurrency = concurrency
        self._name = options.name
        self._wait_for_capacity = options.wait_for_capacity
        self._admission_timeout = options.admission_timeout
        self._lanes: dict[str, _Lane] = {}
        self._ready: list[_Lane] = []
        self._active: list[_Job] = []
        self._pending = 0
        self._workers = 0
        self._finishing = 0
        self._closed = False
        self._done = asyncio.Event()
        self._space_waiters: list[asyncio.Future] = []

    # -- 只读状态（对应 Go 的结构体字段，测试与诊断会读） --------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def workers(self) -> int:
        return self._workers

    @property
    def active(self) -> list[_Job]:
        return list(self._active)

    @property
    def closed(self) -> bool:
        return self._closed

    def done(self) -> asyncio.Event:
        """对应 Go ``Queue.Done()`` 返回的 channel。"""
        return self._done

    # -- 提交 ---------------------------------------------------------------

    async def submit(self, ctx: CancelToken | None, task: Task) -> None:
        """准入一个任务。队满抛 ``ErrQueueFull``，已关闭抛 ``ErrClosed``。"""
        if task.run is None:
            raise ValueError("signal: nil task")
        if task.shutdown not in SHUTDOWN_POLICIES:
            raise ValueError("signal: invalid shutdown policy")
        if self._closed:
            raise ErrClosed
        if ctx is None:
            ctx = CancelToken()
        ctx.raise_if_cancelled()
        if self._pending == self._capacity:
            if not self._wait_for_capacity:
                raise ErrQueueFull
            admission = ctx
            if self._admission_timeout > 0:
                admission = with_timeout(ctx, self._admission_timeout)
            await self._wait_for_space(admission)
            if self._closed:
                raise ErrClosed
            if admission.is_cancelled():
                raise admission.error() if admission.error() else ErrClosed

        lane = self._lanes.get(task.key)
        if lane is None:
            lane = _Lane(task.key)
            self._lanes[task.key] = lane
        job = _Job(ctx, task, lane)
        lane.jobs.append(job)
        self._pending += 1
        job.stop = ctx.add_callback(
            lambda job=job, ctx=ctx: self._cancel_pending(job, ctx.error())
        )
        self._make_ready(lane)
        self._start_workers()

    async def _wait_for_space(self, admission: CancelToken) -> None:
        loop = asyncio.get_running_loop()
        while (
            self._pending == self._capacity
            and not self._closed
            and not admission.is_cancelled()
        ):
            future: asyncio.Future = loop.create_future()
            self._space_waiters.append(future)

            def _on_cancel() -> None:
                _resolve(future, None)

            remove = admission.add_callback(_on_cancel)
            try:
                await future
            finally:
                remove()
                if future in self._space_waiters:
                    self._space_waiters.remove(future)

    def _broadcast_space(self) -> None:
        waiters, self._space_waiters = self._space_waiters, []
        for future in waiters:
            _resolve(future, None)

    # -- 分道调度 -----------------------------------------------------------

    def _make_ready(self, lane: _Lane) -> None:
        if lane.jobs and not lane.ready and (lane.key == "" or lane.active == 0):
            lane.ready = True
            self._ready.append(lane)
        if lane.key != "" and not lane.jobs and lane.active == 0:
            self._lanes.pop(lane.key, None)

    def _start_workers(self) -> None:
        if self._workers == self._concurrency:
            return
        # 数"可运行的分道"，而不是被占用 key 后面排队的 job。已经启动但还没
        # 真正跑起来的 worker 会消耗掉其中一部分。
        runnable = len(self._active) - self._workers
        for lane in self._ready:
            if runnable >= self._concurrency - self._workers:
                break
            runnable += len(lane.jobs) if lane.key == "" else 1
        while self._workers < self._concurrency and runnable > 0:
            self._workers += 1
            runnable -= 1
            asyncio.ensure_future(self._work())

    async def _work(self) -> None:
        while True:
            if not self._ready:
                self._workers -= 1
                self._finish_closed()
                return
            lane = self._ready.pop(0)
            lane.ready = False
            job = lane.jobs[0]
            lane.active += 1
            self._remove_pending(job)
            token = job.task.cancel
            owned = False
            if token is None:
                token = with_cancel(job.ctx)
                owned = True
            job.cancel = token
            self._active.append(job)
            err = await _run_task(token, job.task)
            if job.task.finish is not None:
                job.task.finish(err)
            elif err is not None and not token.is_cancelled():
                report_failure("signal task failed", queue=self._name, error=err)
            if owned:
                token.cancel("task finished")
            self._active.remove(job)
            lane.active -= 1
            self._make_ready(lane)

    # -- 取消与关闭 ---------------------------------------------------------

    def _cancel_pending(self, job: _Job, err: BaseException | None) -> None:
        if not job.pending:
            return
        self._remove_pending(job)
        self._finishing += 1
        if job.task.finish is not None:
            job.task.finish(err)
        self._finishing -= 1
        self._finish_closed()

    def _remove_pending(self, job: _Job) -> None:
        lane = job.lane
        if job.pending:
            job.pending = False
            try:
                lane.jobs.remove(job)
            except ValueError:  # pragma: no cover - 结构不变量被破坏
                pass
            self._pending -= 1
        if job.stop is not None:
            job.stop()
            job.stop = None
        if not lane.jobs and lane.ready:
            try:
                self._ready.remove(lane)
            except ValueError:  # pragma: no cover
                pass
            lane.ready = False
        self._make_ready(lane)
        self._broadcast_space()

    def _finish_closed(self) -> None:
        if (
            self._closed
            and self._workers == 0
            and self._pending == 0
            and self._finishing == 0
        ):
            self._done.set()

    def _stop(self, all_: bool) -> tuple[int, bool]:
        """停止接纳并排空。返回 ``(移除的 pending 数, 是否还有 Drain 的活任务)``。"""
        self._closed = True
        removed: list[_Job] = []
        for lane in list(self._lanes.values()):
            for job in list(lane.jobs):
                if all_ or job.task.shutdown == CancelPending:
                    self._remove_pending(job)
                    removed.append(job)
        active = False
        for job in list(self._active):
            if job.task.shutdown == Drain:
                active = True
            if all_ or job.task.shutdown == CancelPending:
                if job.cancel is not None:
                    job.cancel.cancel("queue closed")
        self._broadcast_space()
        self._finishing += len(removed)
        self._finish_closed()
        for job in removed:
            if job.task.finish is not None:
                job.task.finish(ErrClosed)
        self._finishing -= len(removed)
        self._finish_closed()
        return len(removed), active

    def begin_close(self) -> None:
        """停止接纳并按策略处置 pending（对应 Go ``Queue.BeginClose``）。"""
        self._stop(False)

    async def close(self, ctx: CancelToken | None = None) -> None:
        """彻底关闭：先停止接纳，再排空；``ctx`` 到点则强制取消并报错。"""
        self.begin_close()
        if self._done.is_set():
            return
        if ctx is None:
            await self._done.wait()
            return
        if ctx.is_cancelled():
            self._abort_close(ctx)
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
        if self._done.is_set():
            return
        self._abort_close(ctx)

    def _abort_close(self, ctx: CancelToken) -> None:
        """ctx 到点时强制取消所有剩余工作并上报（对应 Go ``stop(true)`` 分支）。"""
        pending, active = self._stop(True)
        if pending > 0 or active:
            report_failure(
                "signal drain incomplete",
                queue=self._name,
                pending=pending,
                active=active,
                error=str(ctx.error()),
            )
        raise ctx.error() if ctx.error() else ErrClosed


async def _run_task(token: CancelToken, task: Task) -> BaseException | None:
    """跑一个任务并返回错误（对应 Go ``runTask``，含 panic 恢复）。"""
    if token.is_cancelled():
        return token.error()
    try:
        result = task.run(token)
        if inspect.isawaitable(result):
            await result
        return None
    except Exception as exc:  # 对应 Go 的 recover()
        return exc


def new_queue(options: QueueOptions | None = None) -> Queue:
    """对应 Go ``signal.NewQueue``（错误改为抛 ``ValueError``）。"""
    return Queue(options)
