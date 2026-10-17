"""按平台的通知发送队列与背压。

对应 Go：``internal/agent/notice_queue.go``。

平台之间互不排队（每个平台一条 :class:`atri_x.signal.Queue`），同一目标
（平台 + scope + 收件人）按到达顺序串行。调用方语义不变：提交后等队列里的真实
结果并原样返回 Receipt / 抛异常，受 ctx 约束；队列满时立刻失败，而不是无限堆在
一个慢平台上。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Callable

from atri_x.delivery import Notice, Receipt
from atri_x.safecontext import CancelToken, CancelledError
from atri_x.signal import CancelPending, ErrClosed, Queue, QueueOptions, Task

__all__ = [
    "NOTICE_QUEUE_CAPACITY",
    "NOTICE_QUEUE_CONCURRENCY",
    "NoticeQueueResult",
    "NoticeQueues",
    "notice_target_key",
]

#: 每个平台最多同时发 10 条、最多 256 条在等。对应 Go 常量。
NOTICE_QUEUE_CAPACITY = 256
NOTICE_QUEUE_CONCURRENCY = 10


@dataclass
class NoticeQueueResult:
    """一次排队发送的结果。对应 Go ``noticeQueueResult``。"""

    receipt: Receipt | None = None
    err: BaseException | None = None


class NoticeQueues:
    """Agent 自己持有的按平台发送队列。对应 Go ``noticeQueues``。"""

    def __init__(self, options: QueueOptions | None = None) -> None:
        self._mu = threading.Lock()
        self._queues: dict[str, Queue] = {}
        self._closed = False
        #: 留空即为默认值；测试用它构造小容量队列。
        self.options = options or QueueOptions()

    @property
    def closed(self) -> bool:
        return self._closed

    def queue(self, platform_name: str) -> Queue:
        """取（或建）某个平台的发送队列。对应 Go ``noticeQueues.queue``。"""
        with self._mu:
            if self._closed:
                raise ErrClosed
            existing = self._queues.get(platform_name)
            if existing is not None:
                return existing
            options = QueueOptions(
                name=platform_name + ".notice",
                capacity=self.options.capacity or NOTICE_QUEUE_CAPACITY,
                concurrency=self.options.concurrency or NOTICE_QUEUE_CONCURRENCY,
                wait_for_capacity=self.options.wait_for_capacity,
                admission_timeout=self.options.admission_timeout,
            )
            queue = Queue(options)
            self._queues[platform_name] = queue
            return queue

    async def send(
        self,
        ctx: Any,
        platform_name: str,
        notice: Notice,
        send: Callable[[Any], Any],
    ) -> Receipt:
        """把一次发送排进对应平台的队列，并等待真实结果。

        对应 Go ``noticeQueues.send``。``send`` 是 ``async (run_ctx) -> Receipt``。
        """
        queue = self.queue(platform_name)
        loop = asyncio.get_running_loop()
        result: asyncio.Future[NoticeQueueResult] = loop.create_future()

        def _resolve(value: NoticeQueueResult) -> None:
            if not result.done():
                result.set_result(value)

        async def _run(run_ctx: Any) -> None:
            try:
                receipt = await send(run_ctx)
            except BaseException as exc:  # noqa: BLE001 - 结果原样回给等待者
                _resolve(NoticeQueueResult(err=exc))
                raise
            _resolve(NoticeQueueResult(receipt=receipt))

        def _finish(err: BaseException | None) -> None:
            # Run 可能根本没执行（关闭或调用方取消），保证等待者一定拿到结果。
            _resolve(NoticeQueueResult(err=err))

        await queue.submit(
            _as_token(ctx),
            Task(shutdown=CancelPending, key=notice_target_key(notice), run=_run, finish=_finish),
        )
        got = await _await_result(result, ctx)
        if got.err is not None:
            raise got.err
        return got.receipt if got.receipt is not None else Receipt()

    async def close(self, ctx: Any) -> None:
        """停止接纳新的发送，并等待已接纳的发送真实退出。对应 Go ``noticeQueues.close``。

        超出 ctx 预算时抛 ctx 的错误，不宣称仍在跑的发送已经结束。
        """
        with self._mu:
            if self._closed:
                return
            self._closed = True
            queues = list(self._queues.values())
            for queue in queues:
                queue.begin_close()
        token = _as_token(ctx)
        for queue in queues:
            await _wait_event(queue.done(), ctx, token)


async def _await_result(
    result: asyncio.Future[NoticeQueueResult], ctx: Any
) -> NoticeQueueResult:
    """等待队列结果，同时尊重 ctx 的取消。"""
    token = _as_token(ctx)
    if token is None:
        return await result
    if token.is_cancelled():
        raise token.error() or CancelledError("context canceled")
    loop = asyncio.get_running_loop()
    cancel_future: asyncio.Future[None] = loop.create_future()

    def _on_cancel() -> None:
        if not cancel_future.done():
            cancel_future.set_result(None)

    remove = token.add_callback(_on_cancel)
    try:
        done, _pending = await asyncio.wait(
            {result, cancel_future}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        remove()
    if result in done:
        return result.result()
    raise token.error() or CancelledError("context canceled")


async def _wait_event(event: asyncio.Event, ctx: Any, token: CancelToken | None) -> None:
    if event.is_set():
        return
    waiter = asyncio.ensure_future(event.wait())
    try:
        if token is None:
            await waiter
            return
        cancel_future = asyncio.get_running_loop().create_future()

        def _on_cancel() -> None:
            if not cancel_future.done():
                cancel_future.set_result(None)

        remove = token.add_callback(_on_cancel)
        try:
            done, _pending = await asyncio.wait(
                {waiter, cancel_future}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            remove()
        if cancel_future in done and waiter not in done:
            raise token.error() or CancelledError("context canceled")
    finally:
        if not waiter.done():
            waiter.cancel()


def _as_token(ctx: Any) -> CancelToken | None:
    """ctx 是 CancelToken 时用它做取消；其他鸭子类型 ctx 不参与取消。"""
    return ctx if isinstance(ctx, CancelToken) else None


def notice_target_key(notice: Notice) -> str:
    """同一目标的串行键：平台 + scope + 收件人。对应 Go ``noticeTargetKey``。

    没有显式目标的通知走当前会话的 sender，按上下文串行即可。
    """
    target = notice.target
    parts = [target.platform, target.scope_id, target.private_user_id, target.group_id]
    if target.superadmins:
        parts.append("superadmins")
    key = "\x00".join(parts).strip("\x00")
    if not key.strip():
        return "context"
    return key


async def close_notice_queues(agent: Any, ctx: Any) -> None:
    """对应 Go ``Agent.CloseNoticeQueues``（由 App 的运行时关闭步骤调用）。"""
    if agent is None:
        return
    await agent.notice_queues.close(ctx)
