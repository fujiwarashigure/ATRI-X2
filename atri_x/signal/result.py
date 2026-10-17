"""注册结果与一次 emit 的完成句柄。

对应 Go：``internal/signal/result.go``。

:class:`Emission` 按注册顺序持有每个接收者的结果；``on_complete`` 供转发
（``ConnectForward``）挂回调 —— 没有任何 worker 会等待下游操作。

Go 的 ``(results, error)`` 在这里改为：:meth:`Emission.wait` 返回结果列表，
有任一结果出错时抛 :class:`JoinedError`（多个错误）或那个错误本身（单个），
以符合 PORTING.md §1.5「(T, error) → 抛异常」；需要同时拿到结果时读
``emission.results``（结果列表始终在句柄上）。

另一处不可避免的差异：Python 回调用**异常**报错，因此失败时
``Result.value`` 为空；Go 的 ``(value, error)`` 可以在失败时仍带回部分进度。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from atri_x.safecontext import CancelToken
from atri_x.signal.errors import JoinedError, join_errors

__all__ = ["Emission", "Result", "Resolved", "map_results"]

R = TypeVar("R")
A = TypeVar("A")
B = TypeVar("B")


@dataclass
class Result(Generic[R]):
    """一次 emission 里某个注册项的不可变快照。对应 Go ``signal.Result``。"""

    value: Any = None  # 业务结果，失败时也保留部分进度
    err: BaseException | None = None  # 准入、取消、超时或回调失败
    started: bool = False  # 回调是否真的进入过


def _resolve(future: asyncio.Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)


class Emission(Generic[R]):
    """一次 emit 的完成句柄。对应 Go ``signal.Emission``。

    本类与事件循环亲和（单线程协作式调度），因此不需要 Go 的
    ``sync.Mutex``；但只要没有 ``await``，任何状态变更都是原子的。
    """

    __slots__ = (
        "_admission_errors",
        "_callbacks",
        "_cancel",
        "_done",
        "_finished",
        "_remaining",
        "_results",
        "_started",
    )

    def __init__(self, cancel: Callable[[], None] | None = None, count: int = 0) -> None:
        self._results: list[Result] = [Result() for _ in range(count)]
        self._finished: list[bool] = [False] * count
        self._started: list[bool] = [False] * count
        self._remaining = count
        self._done = asyncio.Event()
        self._callbacks: list[Callable[[list[Result]], None]] = []
        self._cancel = cancel or (lambda: None)
        #: 部分接收者准入失败时的并列错误（对应 Go ``EmitAsync`` 的第二个返回值）。
        self._admission_errors: list[BaseException] = []
        if count == 0:
            self._done.set()
            self._cancel()

    # -- 结果 ---------------------------------------------------------------

    @property
    def admission_errors(self) -> list[BaseException]:
        """部分接收者准入失败的错误（Go 里由 ``EmitAsync`` 直接返回）。"""
        return self._admission_errors

    @property
    def results(self) -> list[Result]:
        """按注册顺序的结果列表（长度固定）。"""
        return self._results

    @property
    def remaining(self) -> int:
        return self._remaining

    def is_done(self) -> bool:
        return self._done.is_set()

    def done(self) -> asyncio.Event:
        """对应 Go ``Emission.Done()`` 返回的 channel。"""
        return self._done

    def start(self, index: int) -> None:
        """标记某个注册项的回调已经进入（对应 Go ``Emission.start``）。"""
        self._started[index] = True

    def finish(self, index: int, result: Result) -> None:
        """记录某个注册项的结果；重复调用只生效一次。"""
        if self._finished[index]:
            return
        result.started = bool(result.started or self._started[index])
        self._results[index] = result
        self._finished[index] = True
        self._remaining -= 1
        if self._remaining != 0:
            return
        callbacks, self._callbacks = self._callbacks, []
        self._done.set()
        for callback in callbacks:
            callback(self._results)
        self._cancel()

    def on_complete(self, callback: Callable[[list[Result]], None]) -> None:
        """挂完成回调：已完成则立即调用（对应 Go ``Emission.onComplete``）。"""
        if self._remaining != 0:
            self._callbacks.append(callback)
            return
        callback(self._results)

    # -- 等待 ---------------------------------------------------------------

    async def wait(self, ctx: CancelToken | None = None) -> list[Result]:
        """等待完成并返回结果列表。

        任一结果带错误时抛 :class:`JoinedError`（``exc.errors`` 是并列错误，
        ``exc.results`` 是结果列表）。``ctx`` 被取消时抛该 ctx 的错误。
        """
        if not self._done.is_set():
            if ctx is None:
                await self._done.wait()
            else:
                if ctx.is_cancelled():
                    raise ctx.error()
                loop = asyncio.get_running_loop()
                cancel_future: asyncio.Future = loop.create_future()
                remove = ctx.add_callback(lambda: _resolve(cancel_future, None))
                waiter = asyncio.ensure_future(self._done.wait())
                try:
                    await asyncio.wait(
                        {waiter, cancel_future}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    remove()
                    if not waiter.done():
                        waiter.cancel()
                if not self._done.is_set():
                    raise ctx.error()
        results = list(self._results)
        error = join_errors([item.err for item in results])
        if error is not None:
            raise error
        return results


def Resolved(result: Result) -> Emission:
    """构造一个只含一个已完成结果的 emission。对应 Go ``signal.Resolved``。"""
    emission: Emission = Emission(lambda: None, 1)
    emission.finish(0, result)
    return emission


def new_emission(cancel: Callable[[], None] | None, count: int) -> Emission:
    """对应 Go ``newEmission``。"""
    return Emission(cancel, count)


class SignalResultMapperError(Exception):
    """结果映射器抛错时的包装（对应 Go ``signal: result mapper panic``）。"""


def map_results(
    source: Emission, mapper: Callable[[list[Result]], Result]
) -> Emission:
    """在不占用执行 worker 的前提下改用结果（mapper 必须短且不阻塞）。"""
    emission: Emission = Emission(lambda: None, 1)

    def _complete(results: list[Result]) -> None:
        try:
            emission.finish(0, mapper(results))
        except BaseException as exc:  # 对应 Go 的 panic 恢复
            emission.finish(0, Result(err=SignalResultMapperError(str(exc))))

    source.on_complete(_complete)
    return emission
