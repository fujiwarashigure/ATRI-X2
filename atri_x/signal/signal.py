"""类型化信号：每个信号独立调度、独立限额。

对应 Go：``internal/signal/signal.go``。

发布者必须在 emit 之前冻结可变负载。每个 signal 自带 :class:`Queue`
（独立容量与并发上限），支持：

- ``connect`` / ``observe`` / ``connect_forward`` 注册接收者；
- ``once`` 只认领一次；``single`` 最多一个接收者；
- ``key`` 让相同 key 的接收者按准入顺序串行；
- ``before`` 作为所有者的执行期守卫；
- ``begin_close`` / ``close`` 先停止接纳、再按策略排空；
- 递归 emit 检测（同一个 signal 在自己的回调里 emit）。

与 Go 的差异（仅返回形式）：Go 的 ``EmitAsync`` 同时返回 ``(*Emission, error)``
—— 部分接收者准入失败时两者都非 nil。Python 不能返回"值 + 异常"，因此
admission 失败记录在 ``emission.admission_errors`` 上，且逐个接收者的错误
仍在 ``emission.results[i].err``；``emit``（= Go 的 ``Emit``）行为不变，仍然
抛汇总错误。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Generic, TypeVar

from atri_x.errors import ClosedError, QueueFullError
from atri_x.safecontext import (
    CancelToken,
    CancelledError,
    DeadlineExceededError,
    with_cancel,
    with_timeout,
)
from atri_x.signal.connection import Connection
from atri_x.signal.diagnostics import report_failure
from atri_x.signal.errors import (
    ErrClosed,
    ErrNoReceiver,
    ErrRecursive,
    SignalError,
)
from atri_x.signal.executor import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT,
    ConnectOptions,
    Options,
)
from atri_x.signal.queue import Queue, QueueOptions
from atri_x.signal.result import Emission, Result
from atri_x.signal.shutdown import SHUTDOWN_POLICIES, Task

__all__ = ["Signal", "new"]

T = TypeVar("T")
R = TypeVar("R")

_PATH_KEY = object()
_START_KEY = object()


class _CallPath:
    """emit 调用链，用于检测同一 signal 的递归调用。"""

    __slots__ = ("parent", "signal")

    def __init__(self, signal: "Signal", parent: "_CallPath | None") -> None:
        self.signal = signal
        self.parent = parent


class _StartPath:
    """转发链上的"是否真的进入过回调"标记。"""

    __slots__ = ("_mark", "parent")

    def __init__(self, mark: Callable[[], None], parent: "_StartPath | None") -> None:
        self._mark = mark
        self.parent = parent

    def mark(self) -> None:
        self._mark()
        if self.parent is not None:
            self.parent.mark()


class _Slot(Generic[T, R]):
    __slots__ = ("claimed", "connection", "forward", "handler", "options")

    def __init__(
        self,
        options: ConnectOptions,
        handler: Callable[[CancelToken, T], Any] | None = None,
        forward: Callable[[CancelToken, T], Any] | None = None,
    ) -> None:
        self.handler = handler
        self.forward = forward
        self.options = options
        self.claimed = False
        self.connection: Connection | None = None


class _RunState:
    __slots__ = ("forwarded", "value")

    def __init__(self) -> None:
        self.value: Any = None
        self.forwarded = False


class Signal(Generic[T, R]):
    """一个有独立调度队列的类型化信号。对应 Go ``signal.Signal``。"""

    def __init__(self, name: str, options: Options | None = None) -> None:
        options = options or Options()
        if options.concurrency == 0:
            options.concurrency = DEFAULT_CONCURRENCY
        if options.timeout == 0:
            options.timeout = DEFAULT_TIMEOUT
        if options.concurrency < 0 or options.capacity < 0 or options.timeout < 0:
            raise ValueError("signal: invalid options")
        self._name = name
        self._options = options
        self._queue = Queue(
            QueueOptions(
                name=name,
                capacity=options.capacity,
                concurrency=options.concurrency,
            )
        )
        self._slots: list[_Slot] = []
        self._closed = False

    # -- 注册 ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def queue(self) -> Queue:
        return self._queue

    def connect(
        self,
        handler: Callable[[CancelToken, T], Any],
        options: ConnectOptions | None = None,
    ) -> Connection:
        """注册一个业务接收者。"""
        if handler is None:
            raise ValueError("signal: nil handler")
        return self._connect(_Slot(options or ConnectOptions(), handler=handler))

    def observe(
        self,
        handler: Callable[[CancelToken, T], Any],
        options: ConnectOptions | None = None,
    ) -> Connection:
        """注册一个只做事实观察、没有业务返回值的接收者。"""
        if handler is None:
            raise ValueError("signal: nil handler")

        async def _wrapped(ctx: CancelToken, event: T) -> None:
            outcome = handler(ctx, event)
            if inspect.isawaitable(outcome):
                await outcome
            return None

        return self.connect(_wrapped, options)

    def connect_forward(
        self,
        handler: Callable[[CancelToken, T], Any],
        options: ConnectOptions | None = None,
    ) -> Connection:
        """采纳唯一一个下游接收者的完成结果（必须用 ctx 发起下游 emit）。"""
        if handler is None:
            raise ValueError("signal: nil forwarder")
        return self._connect(_Slot(options or ConnectOptions(), forward=handler))

    def _connect(self, entry: _Slot) -> Connection:
        if entry.options.shutdown not in SHUTDOWN_POLICIES:
            raise ValueError("signal: invalid shutdown policy")
        if self._closed:
            raise ErrClosed
        if self._options.single and self._slots:
            raise SignalError("signal: receiver already connected")
        entry.connection = Connection(lambda entry=entry: self._remove(entry))
        self._slots.append(entry)
        return entry.connection

    def _remove(self, entry: _Slot) -> None:
        try:
            self._slots.remove(entry)
        except ValueError:
            pass

    # -- 发布 ---------------------------------------------------------------

    async def emit(self, ctx: CancelToken | None, event: T) -> list[Result]:
        """同步等待所有接收者完成；任一失败则抛汇总错误。"""
        if not self._slots and not self._options.required:
            if self._closed:
                raise ErrClosed
            if ctx is not None:
                ctx.raise_if_cancelled()
            return []
        emission = await self.emit_async(ctx, event)
        return await emission.wait(None)

    async def emit_async(self, ctx: CancelToken | None, event: T) -> Emission:
        """准入一次 emission 并立即返回句柄（不等待接收者）。"""
        if self._closed:
            raise ErrClosed
        if ctx is None:
            ctx = CancelToken()
        ctx.raise_if_cancelled()
        path = ctx.value(_PATH_KEY)
        node = path
        while node is not None:
            if node.signal is self:
                raise ErrRecursive
            node = node.parent
        emit_ctx = with_timeout(
            ctx.with_value(_PATH_KEY, _CallPath(self, path)), self._options.timeout
        )
        entries = list(self._slots)
        if self._closed:
            emit_ctx.cancel("signal closed")
            raise ErrClosed
        if not entries and self._options.required:
            emit_ctx.cancel("signal: no receiver")
            raise ErrNoReceiver
        emission: Emission = Emission(emit_ctx.cancel, len(entries))
        for index, entry in enumerate(entries):
            if entry.options.once:
                if entry.claimed:
                    emission.finish(index, Result())
                    continue
                entry.claimed = True
                self._remove(entry)
            error = await self._dispatch(emit_ctx, emission, index, entry, event)
            if error is not None:
                emission.admission_errors.append(error)
        return emission

    async def _dispatch(
        self,
        emit_ctx: CancelToken,
        emission: Emission,
        index: int,
        entry: _Slot,
        event: T,
    ) -> BaseException | None:
        task_ctx = with_cancel(emit_ctx)
        parent_start = emit_ctx.value(_START_KEY)
        if entry.forward is not None:
            task_ctx = task_ctx.with_value(
                _START_KEY,
                _StartPath(lambda: emission.start(index), parent_start),
            )
        assert entry.connection is not None
        owned = entry.connection.acquire(task_ctx)
        if owned is None:
            task_ctx.cancel("connection closed")
            emission.finish(index, Result(err=ErrClosed))
            return ErrClosed
        stop_task = task_ctx.add_callback(
            lambda: emission.finish(index, Result(err=task_ctx.error()))
        )
        state = _RunState()

        def finish(err: BaseException | None) -> None:
            entry.connection.release(owned)
            if state.forwarded:
                return
            stop_task()
            if err is not None and not isinstance(
                err,
                (
                    CancelledError,
                    DeadlineExceededError,
                    ClosedError,
                    QueueFullError,
                ),
            ):
                report_failure(
                    "signal callback failed", signal=self._name, error=str(err)
                )
            emission.finish(index, Result(value=state.value, err=err))
            task_ctx.cancel("task finished")

        async def run(run_ctx: CancelToken) -> None:
            if run_ctx.is_cancelled():
                raise run_ctx.error()
            before = self._options.before
            if before is not None:
                guard = before(task_ctx, event)
                if inspect.isawaitable(guard):
                    await guard
            if entry.forward is not None:
                child = entry.forward(task_ctx, event)
                if inspect.isawaitable(child):
                    child = await child
                if child is None:
                    raise SignalError("signal: nil forwarded emission")
                if len(child.results) != 1:
                    raise SignalError(
                        "signal: forwarding requires one downstream receiver"
                    )
                state.forwarded = True

                def _complete(results: list[Result]) -> None:
                    stop_task()
                    emission.finish(index, results[0])
                    task_ctx.cancel("forwarded")

                child.on_complete(_complete)
                return
            emission.start(index)
            node = parent_start
            while node is not None:
                node.mark()
                node = node.parent
            value = entry.handler(task_ctx, event)
            if inspect.isawaitable(value):
                value = await value
            state.value = value

        task = Task(
            run=run,
            shutdown=entry.options.shutdown,
            key="",
            finish=finish,
            cancel=task_ctx,
        )
        if self._options.key is not None:
            task.key = self._options.key(event)
        try:
            await self._queue.submit(task_ctx, task)
        except BaseException as err:  # 准入失败：结果里带上错误
            finish(err)
            return err
        return None

    # -- 关闭 ---------------------------------------------------------------

    def begin_close(self) -> None:
        """停止接纳新 emission，并按策略处置队列（对应 Go ``BeginClose``）。"""
        self._closed = True
        self._queue.begin_close()

    async def close(self, ctx: CancelToken | None = None) -> None:
        """关闭并排空；``ctx`` 到点抛错误。"""
        self.begin_close()
        await self._queue.close(ctx)

    def done(self):
        """对应 Go ``Signal.Done()`` 返回的 channel。"""
        return self._queue.done()


def new(name: str, options: Options | None = None) -> Signal:
    """对应 Go ``signal.New``（错误改为抛 ``ValueError``）。"""
    return Signal(name, options)
