"""入站预处理的去重、限流与事件分发。

对应 Go：``internal/platform/qq-onebot/inbound.go``。

上游的两条硬边界（逐条保留）：

1. **入站预处理独立于 Agent 请求队列**：解析 @、引用消息与合并转发都要发网络请求，
   所以一波恶意或误触的转发既不能生成无界协程，也不能把撤回/管理事件堵在聊天消息
   后面。
2. **去重与 Chat History 无关**：``history=off`` 时 OneBot 重连重放也不能让模型
   被唤醒两次。``failed`` 在 TTL 内是终态 —— 首次失败的消息不会被重放，
   这正是"重连风暴不放大付费调用"的实现。

形式差异：

* ``sync.Mutex`` → :class:`threading.Lock`：:class:`InboundDeduper` 的方法是同步的
  （Go 亦然），改成 ``asyncio.Lock`` 会强迫调用方 ``await``，与 Go 的调用点不符。
* Go 的 ``chan`` + ``WaitGroup`` → :class:`asyncio.Queue` + worker task；
  ``go func()`` 派发的整轮处理 → ``asyncio.create_task``，句柄登记在
  :meth:`EventDispatcher.pending_tasks`（Python 侧测试接缝）。
* ``time.Time`` → aware UTC ``datetime``。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.platform.qq_onebot.target import event_scope_id
from atri_x.platform.qq_onebot.types import Event

#: 去重条目的三种状态。对应 Go 的 ``inboundDedupState*``。
INBOUND_DEDUP_STATE_PROCESSING = "processing"
INBOUND_DEDUP_STATE_COMPLETED = "completed"
INBOUND_DEDUP_STATE_FAILED = "failed"

#: 默认值（Go 常量块）。
DEFAULT_INBOUND_DEDUP_TTL = 30 * 60.0
DEFAULT_INBOUND_DEDUP_MAX = 4096
DEFAULT_PREPROCESS_WORKERS = 4
DEFAULT_PREPROCESS_QUEUE = 256
DEFAULT_HIGH_PRIORITY_WORKERS = 1
DEFAULT_HIGH_PRIORITY_QUEUE = 128


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class InboundDedupEntry:
    """一条去重记录。对应 Go ``inboundDedupEntry``。"""

    state: str = ""
    at: datetime = field(default_factory=_utcnow)
    note: str = ""


class InboundDeduper:
    """有界 TTL 去重表，键是 ``platform|bot|scope|message-id``。

    对应 Go ``inboundDeduper``。
    """

    def __init__(self, ttl: float = 0.0, max_entries: int = 0) -> None:
        import threading

        self.ttl = ttl if ttl > 0 else DEFAULT_INBOUND_DEDUP_TTL
        self.max_entries = max_entries if max_entries > 0 else DEFAULT_INBOUND_DEDUP_MAX
        self.now = _utcnow
        self.entries: dict[str, InboundDedupEntry] = {}
        self._lock = threading.Lock()

    def begin(self, key: str) -> tuple[str, bool]:
        """开始处理：返回 ``(state, duplicate)``。对应 Go ``begin``。"""
        key = (key or "").strip()
        if key == "":
            return "", False
        now = self.now()
        with self._lock:
            self._prune_locked(now)
            entry = self.entries.get(key)
            if entry is not None:
                return entry.state, True
            self.entries[key] = InboundDedupEntry(state=INBOUND_DEDUP_STATE_PROCESSING, at=now)
            if len(self.entries) > self.max_entries:
                self._evict_oldest_locked()
            return INBOUND_DEDUP_STATE_PROCESSING, False

    def finish(self, key: str, state: str, note: str) -> None:
        """标记终态。对应 Go ``finish``。"""
        key = (key or "").strip()
        if key == "":
            return
        if state == "":
            state = INBOUND_DEDUP_STATE_COMPLETED
        with self._lock:
            entry = self.entries.get(key)
            if entry is None:
                return
            entry.state = state
            entry.note = (note or "").strip()
            entry.at = self.now()

    def _prune_locked(self, now: datetime) -> None:
        """对应 Go ``pruneLocked``。"""
        expired = [key for key, entry in self.entries.items() if now - entry.at > timedelta(seconds=self.ttl)]
        for key in expired:
            del self.entries[key]

    def _evict_oldest_locked(self) -> None:
        """对应 Go ``evictOldestLocked``。"""
        while len(self.entries) > self.max_entries:
            oldest_key = ""
            oldest: datetime | None = None
            for key, entry in self.entries.items():
                if oldest_key == "" or (oldest is not None and entry.at < oldest):
                    oldest_key = key
                    oldest = entry.at
            if oldest_key == "":
                return
            del self.entries[oldest_key]


def inbound_key(adapter: Any, event: Event) -> str:
    """把一条平台消息绑定到稳定身份。对应 Go ``(*Adapter).inboundKey``。

    用适配器可见的 bot id，使同一个账号布局上的两个 bot 不会互相压制。
    """
    if adapter is None or event.message_id == 0:
        return ""
    scope = event_scope_id(event)
    if scope == "":
        return ""
    return "|".join([adapter.name(), str(event.self_id), scope, str(event.message_id)])


@dataclass
class InboundStats:
    """入站预处理路径的只读小视图。对应 Go ``InboundStats``。"""

    dedup_hits: int = 0
    queue_rejected: int = 0
    last_queue_reject_at: datetime | None = None


class InboundRuntime:
    """去重运行时的可变部分。对应 Go ``inboundRuntime``。"""

    def __init__(self, dedup: InboundDeduper | None = None) -> None:
        self.dedup = dedup
        self.dedup_hits = 0
        self.queue_rejected = 0
        self.last_reject_at: datetime | None = None

    def record_reject(self, now: datetime) -> None:
        """记一次队列拒收。对应 Go ``recordReject``。"""
        self.queue_rejected += 1
        self.last_reject_at = now


def new_inbound_runtime(cfg: Any) -> InboundRuntime:
    """对应 Go ``newInboundRuntime``：``inbound_dedup_enabled`` 为假时不建表。"""
    if cfg.inbound_dedup_enabled is not None and not cfg.inbound_dedup_enabled:
        return InboundRuntime()
    return InboundRuntime(
        InboundDeduper(
            ttl=float(cfg.inbound_dedup_ttl_seconds) * 1.0,
            max_entries=int(cfg.inbound_dedup_max_entries),
        )
    )


def inbound_stats(adapter: Any) -> InboundStats:
    """对应 Go ``(*Adapter).InboundStats``。"""
    runtime = getattr(adapter, "inbound", None) if adapter is not None else None
    if runtime is None:
        return InboundStats()
    return InboundStats(
        dedup_hits=runtime.dedup_hits,
        queue_rejected=runtime.queue_rejected,
        last_queue_reject_at=runtime.last_reject_at,
    )


def begin_inbound_job(adapter: Any, key: str) -> tuple[bool, str]:
    """对应 Go ``(*Adapter).beginInboundJob``：返回 ``(duplicate, state)``。"""
    runtime = getattr(adapter, "inbound", None) if adapter is not None else None
    if runtime is None or runtime.dedup is None or (key or "").strip() == "":
        return False, ""
    state, duplicate = runtime.dedup.begin(key)
    if duplicate:
        runtime.dedup_hits += 1
    return duplicate, state


def finish_inbound_job(adapter: Any, key: str, state: str, note: str) -> None:
    """对应 Go ``(*Adapter).finishInboundJob``。"""
    runtime = getattr(adapter, "inbound", None) if adapter is not None else None
    if runtime is None or runtime.dedup is None:
        return
    runtime.dedup.finish(key, state, note)


@dataclass
class OutboundEventJob:
    """一条待处理事件。对应 Go ``outboundEventJob``。"""

    event: Event = field(default_factory=Event)
    message_key: str = ""


class EventDispatcher:
    """有界普通预处理池 + 独立高优先级 notice/request 通道。

    对应 Go ``eventDispatcher``。高优先级事件永远不会排在普通聊天消息后面；
    高优先级队列也满时，读循环就地执行该事件而不是丢弃它。
    """

    def __init__(
        self,
        adapter: Any,
        life_ctx: Any,
        handler: Any,
        workers: int = 0,
        queue_size: int = 0,
        high_workers: int = 0,
        high_queue_size: int = 0,
    ) -> None:
        self.adapter = adapter
        self.handler = handler
        self.life_ctx = life_ctx
        self.normal: asyncio.Queue = asyncio.Queue(
            maxsize=queue_size if queue_size > 0 else DEFAULT_PREPROCESS_QUEUE
        )
        self.high: asyncio.Queue = asyncio.Queue(
            maxsize=high_queue_size if high_queue_size > 0 else DEFAULT_HIGH_PRIORITY_QUEUE
        )
        self._workers: list[asyncio.Task] = []
        self._turn_tasks: set[asyncio.Task] = set()
        self._stopping = False
        for _ in range(workers if workers > 0 else DEFAULT_PREPROCESS_WORKERS):
            self._workers.append(asyncio.ensure_future(self._normal_worker()))
        for _ in range(high_workers if high_workers > 0 else DEFAULT_HIGH_PRIORITY_WORKERS):
            self._workers.append(asyncio.ensure_future(self._high_worker()))

    async def stop(self) -> None:
        """停掉 worker。对应 Go ``stop``。

        Go 关 channel 并等 WaitGroup；Python 侧取消 worker task 并等待其收敛。
        已经派发出去的整轮处理（Go 的 goroutine）不被取消。
        """
        self._stopping = True
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def pending_tasks(self) -> set[asyncio.Task]:
        """尚未结束的整轮处理任务（Python 侧测试接缝）。"""
        return set(self._turn_tasks)

    async def wait_turns(self) -> None:
        """等待所有已派发的整轮处理结束（Python 侧测试接缝）。"""
        while self._turn_tasks:
            await asyncio.gather(*list(self._turn_tasks), return_exceptions=True)

    def enqueue_normal(self, job: OutboundEventJob) -> bool:
        """非阻塞入队。对应 Go ``enqueueNormal``。"""
        return self._offer(self.normal, job)

    def enqueue_high(self, job: OutboundEventJob) -> bool:
        """非阻塞入队。对应 Go ``enqueueHigh``。"""
        return self._offer(self.high, job)

    @staticmethod
    def _offer(queue: asyncio.Queue, job: OutboundEventJob) -> bool:
        try:
            queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            return False

    async def _normal_worker(self) -> None:
        """对应 Go ``normalWorker``。"""
        while True:
            job = await self.normal.get()
            if self._stopping:
                return
            await self.prepare_and_dispatch(job)

    async def _high_worker(self) -> None:
        """对应 Go ``highWorker``。"""
        while True:
            job = await self.high.get()
            if self._stopping:
                return
            await self.adapter.handle_platform_event(self.life_ctx, self.handler, job.event)

    async def prepare_and_dispatch(self, job: OutboundEventJob) -> None:
        """在有界 worker 里做预处理，再把整轮处理交给独立 task。

        对应 Go ``prepareAndDispatch``：长模型回合不占用预处理 worker。
        """
        adapter = self.adapter
        _msg_ctx, msg_ctx, text, ok = await adapter.prepare_inbound(
            self.life_ctx, self.life_ctx, job.event
        )
        if not ok:
            finish_inbound_job(
                adapter, job.message_key, INBOUND_DEDUP_STATE_COMPLETED, "preprocess_empty"
            )
            return
        task = asyncio.ensure_future(self._run_turn(msg_ctx, text, job))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_turn(self, msg_ctx: Any, text: str, job: OutboundEventJob) -> None:
        """对应 Go ``prepareAndDispatch`` 里 ``go func()`` 包住的那段。"""
        adapter = self.adapter
        try:
            result = self.handler.handle_message(msg_ctx, text)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 处理失败只记日志并标失败态
            finish_inbound_job(adapter, job.message_key, INBOUND_DEDUP_STATE_FAILED, str(exc))
            adapter.log_warn(
                "handle qq message failed", error=exc, message_id=job.event.message_id
            )
            return
        finish_inbound_job(adapter, job.message_key, INBOUND_DEDUP_STATE_COMPLETED, "")


async def sleep_context(ctx: Any, delay: float) -> bool:
    """可被 ctx 取消的 sleep。对应 Go ``sleepContext``。

    返回 False 表示等待期间 ctx 被取消。
    """
    from atri_x.delivery.delivery import ctx_error

    if ctx_error(ctx) is not None:
        return False
    if delay <= 0:
        await asyncio.sleep(0)
        return ctx_error(ctx) is None
    event = asyncio.Event()
    remove = None
    adder = getattr(ctx, "add_callback", None)
    if callable(adder):
        remove = adder(event.set)
    try:
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(event.wait(), delay)
    finally:
        if remove is not None:
            remove()
    return ctx_error(ctx) is None


def raise_context_error(ctx: Any) -> None:
    """抛出 ctx 的取消原因。对应 Go 的 ``return ctx.Err()``。

    与 ``sleep_context`` 一起放在本模块（Python 侧把 ctx 小工具集中在这里，
    Go 侧它们分散在 ``adapter.go``；两个适配器包各自持有一份，互不共享）。
    """
    from atri_x.delivery.delivery import ctx_error

    err = ctx_error(ctx)
    if err is not None:
        raise err
    raise asyncio.CancelledError("qq onebot adapter context cancelled")


__all__ = [name for name in dir() if not name.startswith("_")]
