"""平台连接信号的订阅与按平台执行队列。

对应 Go：``internal/agent/platform_signals.go``。

Agent 自己持有平台连接订阅：信号连接、每个平台的执行队列和关闭都归消费者，
发布方只负责 Emit。这样平台适配器的连接回调不会被 hook 规则或 cron 补投递拖住。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from atri_x.platform import ConnectedEvent, PlatformConnected
from atri_x.signal import CancelPending, ErrClosed, Queue, QueueOptions, Task

__all__ = [
    "PlatformSubscriptions",
    "begin_close_platform_events",
    "close_platform_events",
    "enqueue_platform_connected",
    "start_platform_events",
]


class PlatformSubscriptions:
    """Agent 的平台连接订阅状态。对应 Go ``platformSubscriptions``。"""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.connection: Any = None
        self.queues: dict[str, Queue] = {}
        self.parent: Any = None
        self.stop_parent: Any = None
        self.closed = False
        self.done: asyncio.Event | None = None


def _subs(agent: Any) -> PlatformSubscriptions:
    subs = getattr(agent, "platform_events", None)
    if subs is None:
        subs = PlatformSubscriptions()
        agent.platform_events = subs
    return subs


async def start_platform_events(agent: Any, ctx: Any) -> None:
    """订阅全局平台连接信号。对应 Go ``Agent.StartPlatformEvents``。

    依赖装配完成后、适配器启动前调用：重复调用不会重复订阅，已关闭的订阅者不能
    重新启动。
    """
    if agent is None:
        raise RuntimeError("agent unavailable")
    subs = _subs(agent)
    with subs.mu:
        if subs.closed:
            raise ErrClosed
        if ctx is not None:
            token = _as_token(ctx)
            if token is not None:
                token.raise_if_cancelled()
        if subs.connection is not None:
            return
        handler = _make_signal_handler(agent)
        PlatformConnected.connect(handler)
        subs.connection = handler
        subs.queues = {}
        subs.parent = ctx
        token = _as_token(ctx)
        if token is not None:
            subs.stop_parent = token.add_callback(lambda: begin_close_platform_events(agent))


def _make_signal_handler(agent: Any) -> Any:
    """信号 handler 必须是同步可调用的：``emit`` 不接受协程返回值。

    因此这里只在事件循环线程上把"入队"排成任务；没有事件循环时（同步 emit）
    丢弃该事实，调用方应改用 ``emit_async``。
    """

    def _handler(ctx: Any, event: ConnectedEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.warn(
                    "platform connected signal dropped: no running loop",
                    platform=getattr(event, "platform", ""),
                )
            return
        loop.create_task(_enqueue_platform_connected(agent, ctx, event))

    return _handler


async def _enqueue_platform_connected(agent: Any, ctx: Any, event: ConnectedEvent) -> None:
    try:
        await enqueue_platform_connected(agent, ctx, event)
    except BaseException:  # noqa: BLE001 - 入队失败不应冲掉事件循环
        pass


async def enqueue_platform_connected(agent: Any, ctx: Any, event: ConnectedEvent) -> None:
    """只做准入与排队：把平台连接事实交给它自己那条按平台的队列。

    对应 Go ``Agent.enqueuePlatformConnected``。
    """
    subs = _subs(agent)
    with subs.mu:
        # Disconnect 收不回已经发出的信号快照，所以准入判断必须和队列创建、关闭
        # 共用同一把锁，覆盖取消回调还没跑到的窗口。
        if subs.closed or subs.connection is None:
            return
        parent = subs.parent
        token = _as_token(parent)
        if token is not None and token.is_cancelled():
            return
        name = (getattr(event, "platform", "") or "").strip()
        if not name:
            raise ValueError("agent: platform connection has no platform name")
        queue = subs.queues.get(name)
        if queue is None:
            # Capacity / Concurrency 默认 256 / 1：同一平台按到达顺序串行补做，
            # 平台之间互不排队。
            queue = Queue(QueueOptions(name=name + ".platform.connected"))
            subs.queues[name] = queue

    # 保留事件值，但取消权归消费者：连接用的 ctx 会随适配器启动阶段结束，而补做
    # 必须在适配器生命周期之外继续。
    async def _run(run_ctx: Any) -> None:
        await agent.handle_platform_connected(run_ctx, name)

    await queue.submit(_detached_token(ctx), Task(shutdown=CancelPending, run=_run))


def begin_close_platform_events(agent: Any) -> None:
    """断开订阅并关闭各平台队列，不等待回调退出。对应 Go ``beginClosePlatformEvents``。"""
    subs = _subs(agent)
    with subs.mu:
        if subs.closed:
            return
        subs.closed = True
        if subs.done is None:
            subs.done = _new_event()
        stopper = subs.stop_parent
        subs.stop_parent = None
        connection = subs.connection
        subs.connection = None
        queues = list(subs.queues.values())
        for queue in queues:
            queue.begin_close()
    if stopper is not None:
        stopper()
    if connection is not None:
        PlatformConnected.disconnect(connection)
    if not queues:
        subs.done.set()
        return
    loop = _current_loop()
    if loop is None:  # pragma: no cover - 关闭路径通常在事件循环内
        return

    async def _wait_all() -> None:
        for queue in queues:
            await queue.done().wait()
        subs.done.set()

    loop.create_task(_wait_all())


async def close_platform_events(agent: Any, ctx: Any) -> None:
    """停止接纳新的连接事实并等待已接纳的回调真实退出。

    对应 Go ``Agent.ClosePlatformEvents``：超出 ctx 预算时抛 ctx 的错误，不宣称仍在
    跑的回调已经结束。
    """
    if agent is None:
        return
    begin_close_platform_events(agent)
    subs = _subs(agent)
    done = subs.done if subs.done is not None else _new_event()
    if done.is_set():
        return
    await _wait_event(done, ctx)


def _wait_event(event: asyncio.Event, ctx: Any) -> Any:
    async def _inner() -> None:
        token = _as_token(ctx)
        if token is None:
            await event.wait()
            return
        waiter = asyncio.ensure_future(event.wait())
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
            if not waiter.done():
                waiter.cancel()
        if cancel_future in done and waiter not in done:
            from atri_x.safecontext import CancelledError

            raise token.error() or CancelledError("context canceled")

    return _inner()


def _new_event() -> asyncio.Event:
    """``asyncio.Event`` 必须先绑定事件循环才能跨线程使用，这里尽量早绑定。"""
    event = asyncio.Event()
    loop = _current_loop()
    if loop is not None:
        event._loop = loop  # noqa: SLF001 - 让 set()/wait() 在无运行循环时也可用
    return event


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _detached_token(ctx: Any) -> Any:
    """``context.WithoutCancel`` 的等价物：只保留值，不带取消。"""
    token = _as_token(ctx)
    if token is None:
        return None
    fresh = token.__class__()
    fresh._values = dict(token._values)  # noqa: SLF001 - 见 safecontext 的值语义
    return fresh


def _as_token(ctx: Any) -> Any:
    from atri_x.safecontext import CancelToken

    return ctx if isinstance(ctx, CancelToken) else None
