"""active request 的并发限制、有界等待队列与公平调度。

对应 Go：``internal/request/manager.go``。

职责与上游一致：按 kind 限制并发；超限时进入**有界**等待队列（全局、每
``fair_key``、每 ``scope_key`` 三层上限）；队列让位按 ``FairKey`` 公平调度
（当前活跃数最少的 key 先上，平局时优先最近未被放行的 key，再按入队时间）；
支持按 Session / Scope / FairKey 的定向取消，以及 ``Snapshot`` 指标。

约定：

- Go 的 ``time.Duration`` → Python ``float`` 秒；``time.Time`` →
  ``datetime.datetime``（UTC aware）。
- Go 的 ``context.Context`` → :class:`atri_x.safecontext.CancelToken`。
- ``Start`` 是唯一的异步方法（等队列需要 await），其余状态操作都是同步的；
  互斥用 ``threading.Lock``，等待唤醒用 ``asyncio.Future``。
- Go 的 ``(T, error)`` → 抛 :mod:`atri_x.errors` 里的哨兵异常。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from atri_x.errors import (
    ErrConcurrencyLimit,
    ErrQueueCancelled,
    ErrQueueFull,
    ErrQueueTimeout,
)
from atri_x.safecontext import (
    CancelToken,
    DeadlineExceededError,
    with_cancel,
    with_timeout,
)

__all__ = [
    "KindCompress",
    "KindHook",
    "KindLLM",
    "KindSubAgent",
    "KindTool",
    "KindTurn",
    "Limits",
    "Manager",
    "QueueConfig",
    "Request",
    "Snapshot",
    "StartRequest",
    "new_manager",
    "new_manager_with_limits",
    "new_manager_with_limits_and_queue",
]


class Kind:
    """请求类别。对应 Go 的 ``request.Kind`` 常量组。"""

    TURN = "turn"
    LLM = "llm"
    TOOL = "tool"
    HOOK = "hook"
    COMPRESS = "compress"
    SUB_AGENT = "sub_agent"


KindTurn = Kind.TURN
KindLLM = Kind.LLM
KindTool = Kind.TOOL
KindHook = Kind.HOOK
KindCompress = Kind.COMPRESS
KindSubAgent = Kind.SUB_AGENT

Limits = Mapping[str, int]


def _seconds(value: Any) -> float:
    """接受 float 秒或 ``timedelta``，统一成秒。"""
    if value is None:
        return 0.0
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """对应 Go ``storage.NewID``：UUID v4 十六进制无连字符。"""
    return uuid.uuid4().hex


@dataclass
class Request:
    """一个 active request 的快照。对应 Go ``request.Request``。"""

    id: str = ""
    parent_id: str = ""
    session_id: str = ""
    kind: str = ""
    label: str = ""
    stage: str = ""
    fair_key: str = ""
    scope_key: str = ""
    started_at: datetime | None = None
    progress_at: datetime | None = None
    deadline: datetime | None = None


@dataclass
class StartRequest:
    """``Manager.start`` 的入参。对应 Go ``request.StartRequest``。"""

    parent_id: str = ""
    session_id: str = ""
    kind: str = ""
    label: str = ""
    stage: str = ""
    fair_key: str = ""
    # scope_key 是可信的群/私聊桶，用于每 scope 队列上限；它与 fair_key
    # （含发言人）刻意分开，避免单个用户占满整个群的队列。
    scope_key: str = ""
    timeout: float = 0.0


@dataclass
class QueueConfig:
    """有界等待队列配置。对应 Go ``request.QueueConfig``。"""

    max_queue: int = 0
    max_queue_per_fair_key: int = 0
    max_queue_per_scope: int = 0
    wait_timeout: float = 0.0
    wait_kinds: Any = field(default_factory=frozenset)

    def normalized_wait_kinds(self) -> frozenset[str]:
        kinds = self.wait_kinds
        if kinds is None:
            return frozenset()
        if isinstance(kinds, Mapping):
            return frozenset(k for k, enabled in kinds.items() if enabled)
        if isinstance(kinds, str):
            return frozenset({kinds})
        return frozenset(kinds)


@dataclass
class _Waiter:
    ready: asyncio.Future
    fair_key: str
    scope_key: str
    queued_at: datetime
    granted: bool = False
    cancelled: bool = False


class _ActiveRequest:
    __slots__ = ("request", "token")

    def __init__(self, request: Request, token: CancelToken) -> None:
        self.request = request
        self.token = token


def _resolve(future: asyncio.Future, value: Any) -> None:
    """线程安全地完成一个等待 Future。"""
    if future.done():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is future.get_loop():
        future.set_result(value)
        return

    def _set() -> None:
        if not future.done():
            future.set_result(value)

    try:
        future.get_loop().call_soon_threadsafe(_set)
    except RuntimeError:  # pragma: no cover - 事件循环已关闭
        _set()


class Manager:
    """request registry + 有界队列 + 公平调度。对应 Go ``request.Manager``。"""

    def __init__(
        self,
        default_timeout: float = 0.0,
        limits: Limits | None = None,
        queue: QueueConfig | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._default_timeout = _seconds(default_timeout)
        self._limits: dict[str, int] = dict(limits or {})
        self._queue = queue or QueueConfig()
        self._wait_kinds = self._queue.normalized_wait_kinds()
        self._active: dict[str, _ActiveRequest] = {}
        self._pending: dict[str, int] = {}
        self._waiters: dict[str, list[_Waiter]] = {}
        self._last_fair_key: dict[str, str] = {}
        self._timeouts = 0
        self._queue_busy_rejected = 0
        self._queue_full_rejected = 0
        self._queue_timeout_rejected = 0
        self._queue_cancelled_rejected = 0
        self._queue_admitted = 0
        self._queue_wait_nanos = 0

    # -- 查询 ---------------------------------------------------------------

    def list(self) -> list[Request]:
        with self._lock:
            return [active.request for active in self._active.values()]

    def list_by_session(self, session_id: str) -> list[Request]:
        with self._lock:
            return [
                active.request
                for active in self._active.values()
                if active.request.session_id == session_id
            ]

    def get(self, request_id: str) -> tuple[Request, bool]:
        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                return Request(), False
            return active.request, True

    def session_ids(self, session_id: str) -> list[str]:
        """返回某 Session 的 active request ID（Agent 用它先标记 turn 终态）。"""
        session_id = (session_id or "").strip()
        if not session_id:
            return []
        with self._lock:
            return [
                rid
                for rid, active in self._active.items()
                if active.request.session_id == session_id
            ]

    def scope_ids(self, scope_key: str) -> list[str]:
        scope_key = (scope_key or "").strip()
        if not scope_key:
            return []
        with self._lock:
            return [
                rid
                for rid, active in self._active.items()
                if active.request.scope_key == scope_key
            ]

    def fair_key_ids(self, fair_key: str) -> list[str]:
        fair_key = (fair_key or "").strip()
        if not fair_key:
            return []
        with self._lock:
            return [
                rid
                for rid, active in self._active.items()
                if active.request.fair_key == fair_key
            ]

    # -- 启动 ---------------------------------------------------------------

    async def start(
        self, parent: CancelToken | None, start: StartRequest
    ) -> tuple[Request, CancelToken, Callable[[], None]]:
        """等一个槽位并登记 request。

        返回 ``(request, token, done)``；``done`` 幂等，调用后立即释放槽位
        并取消 token（对应 Go 的 ``sync.OnceFunc``）。
        """
        kind = start.kind or Kind.LLM
        await self._acquire(
            parent, kind, (start.fair_key or "").strip(), (start.scope_key or "").strip()
        )

        timeout = _seconds(start.timeout) or self._default_timeout
        started_at = _now()
        deadline: datetime | None = None
        if timeout > 0:
            token = with_timeout(parent, timeout)
            deadline = started_at + timedelta(seconds=timeout)
        else:
            token = with_cancel(parent)

        stage = (start.stage or "").strip() or kind
        request = Request(
            id=_new_id(),
            parent_id=start.parent_id,
            session_id=start.session_id,
            kind=kind,
            label=start.label,
            stage=stage,
            fair_key=(start.fair_key or "").strip(),
            scope_key=(start.scope_key or "").strip(),
            started_at=started_at,
            progress_at=started_at,
            deadline=deadline,
        )

        with self._lock:
            if self._pending.get(kind, 0) > 0:
                self._pending[kind] -= 1
            self._active[request.id] = _ActiveRequest(request, token)

        finished = False

        def done() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            self._finish(request.id, cancel=True)

        def _on_token_done() -> None:
            # 对应 Go 里那个 `<-ctx.Done()` 的 goroutine：任何原因的取消都会
            # 摘掉 request；只有截止时间到期才计入 timeouts 指标。
            nonlocal finished
            if isinstance(token.error(), DeadlineExceededError):
                with self._lock:
                    self._timeouts += 1
            if not finished:
                finished = True
            self._finish(request.id, cancel=False)

        token.add_callback(_on_token_done)
        return request, token, done

    # -- 取消 ---------------------------------------------------------------

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            active = self._active.pop(request_id, None)
            if active is not None:
                self._grant_waiters_locked(active.request.kind)
        if active is not None:
            active.token.cancel("request canceled")
        return active is not None

    def cancel_session(self, session_id: str) -> int:
        return self._cancel(lambda req: req.session_id == session_id)

    def cancel_scope(self, scope_key: str) -> int:
        """取消该 scope 下所有 active 与排队中的 request。"""
        scope_key = (scope_key or "").strip()
        if not scope_key:
            return 0
        active, kinds, cancelled_waiters = self._take_matching(
            lambda req: req.scope_key == scope_key, scope_key, by_fair_key=False
        )
        for kind in kinds:
            with self._lock:
                self._grant_waiters_locked(kind)
        for item in active:
            item.token.cancel("scope canceled")
        return len(active) + cancelled_waiters

    def cancel_fair_key(self, fair_key: str) -> int:
        """取消该 FairKey 下所有 active 与排队中的 request。"""
        fair_key = (fair_key or "").strip()
        if not fair_key:
            return 0
        active, kinds, cancelled_waiters = self._take_matching(
            lambda req: req.fair_key == fair_key, fair_key, by_fair_key=True
        )
        for kind in kinds:
            with self._lock:
                self._grant_waiters_locked(kind)
        for item in active:
            item.token.cancel("fair key canceled")
        return len(active) + cancelled_waiters

    def cancel_all(self) -> int:
        with self._lock:
            active = list(self._active.values())
            kinds = {item.request.kind for item in active}
            self._active.clear()
            for kind in kinds:
                self._grant_waiters_locked(kind)
        for item in active:
            item.token.cancel("all canceled")
        return len(active)

    def _cancel(self, predicate: Callable[[Request], bool]) -> int:
        active, kinds, _ = self._take_matching(predicate, "", by_fair_key=False)
        for kind in kinds:
            with self._lock:
                self._grant_waiters_locked(kind)
        for item in active:
            item.token.cancel("request canceled")
        return len(active)

    def _take_matching(
        self,
        predicate: Callable[[Request], bool],
        waiter_key: str,
        by_fair_key: bool,
    ) -> tuple[list[_ActiveRequest], set[str], int]:
        kinds: set[str] = set()
        with self._lock:
            active: list[_ActiveRequest] = []
            for rid, item in list(self._active.items()):
                if not predicate(item.request):
                    continue
                del self._active[rid]
                active.append(item)
                kinds.add(item.request.kind)
            cancelled_waiters = 0
            if waiter_key:
                for kind, waiters in self._waiters.items():
                    kept: list[_Waiter] = []
                    for waiter in waiters:
                        key = waiter.fair_key if by_fair_key else waiter.scope_key
                        if key == waiter_key:
                            waiter.cancelled = True
                            _resolve(waiter.ready, "cancelled")
                            cancelled_waiters += 1
                            continue
                        kept.append(waiter)
                    self._waiters[kind] = kept
        return active, kinds, cancelled_waiters

    # -- 更新 ---------------------------------------------------------------

    def set_fair_key(self, request_id: str, fair_key: str) -> bool:
        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                return False
            active.request.fair_key = (fair_key or "").strip()
            return True

    def touch(self, request_id: str) -> bool:
        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                return False
            active.request.progress_at = _now()
            return True

    def set_stage(self, request_id: str, stage: str) -> bool:
        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                return False
            stage = (stage or "").strip()
            if not stage:
                return False
            active.request.stage = stage
            active.request.progress_at = _now()
            return True

    # -- 队列 ---------------------------------------------------------------

    async def _acquire(
        self, ctx: CancelToken | None, kind: str, fair_key: str, scope_key: str
    ) -> None:
        if kind == "":
            kind = Kind.LLM
        if ctx is None:
            ctx = CancelToken()
        fair_key = (fair_key or "").strip()
        scope_key = (scope_key or "").strip()
        with self._lock:
            if self._can_run_locked(kind):
                self._pending[kind] = self._pending.get(kind, 0) + 1
                self._last_fair_key[kind] = fair_key
                return
            if kind not in self._wait_kinds or self._queue.max_queue <= 0:
                self._queue_busy_rejected += 1
                raise ErrConcurrencyLimit
            if len(self._waiters.get(kind, ())) >= self._queue.max_queue:
                self._queue_full_rejected += 1
                raise ErrQueueFull
            if (
                self._queue.max_queue_per_fair_key > 0
                and fair_key
                and self._queued_fair_key_locked(kind, fair_key)
                >= self._queue.max_queue_per_fair_key
            ):
                self._queue_full_rejected += 1
                raise ErrQueueFull
            if (
                self._queue.max_queue_per_scope > 0
                and scope_key
                and self._queued_scope_locked(kind, scope_key)
                >= self._queue.max_queue_per_scope
            ):
                self._queue_full_rejected += 1
                raise ErrQueueFull
            waiter = _Waiter(
                ready=asyncio.get_running_loop().create_future(),
                fair_key=fair_key,
                scope_key=scope_key,
                queued_at=_now(),
            )
            self._waiters.setdefault(kind, []).append(waiter)

        wait_timeout = self._queue.wait_timeout or None
        cancel_future: asyncio.Future = asyncio.get_running_loop().create_future()

        def _on_cancel() -> None:
            _resolve(cancel_future, "cancelled")

        remove_callback = ctx.add_callback(_on_cancel)
        wait_err: BaseException | None = None
        try:
            done, _ = await asyncio.wait(
                {waiter.ready, cancel_future},
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter.ready not in done and cancel_future in done:
                wait_err = ctx.error()
            elif waiter.ready not in done:
                wait_err = ErrQueueTimeout
        finally:
            remove_callback()
            for future in (waiter.ready, cancel_future):
                if not future.done():
                    future.cancel()

        with self._lock:
            granted = waiter.granted
            cancelled = waiter.cancelled
            if not granted and not cancelled:
                self._remove_waiter_locked(kind, waiter)
        if granted:
            with self._lock:
                self._queue_admitted += 1
                self._queue_wait_nanos += max(
                    0, int((_now() - waiter.queued_at).total_seconds() * 1e9)
                )
            return
        if cancelled:
            with self._lock:
                self._queue_cancelled_rejected += 1
            raise ErrQueueCancelled
        if wait_err is None or wait_err is ErrQueueTimeout:
            with self._lock:
                self._queue_timeout_rejected += 1
            raise ErrQueueTimeout
        with self._lock:
            self._queue_cancelled_rejected += 1
        raise wait_err

    def _can_run_locked(self, kind: str) -> bool:
        limit = self._limits.get(kind, 0)
        if limit <= 0:
            return True
        count = self._pending.get(kind, 0)
        for active in self._active.values():
            if active.request.kind == kind:
                count += 1
        return count < limit

    def _remove_waiter_locked(self, kind: str, target: _Waiter) -> None:
        waiters = self._waiters.get(kind, [])
        for index, waiter in enumerate(waiters):
            if waiter is target:
                del waiters[index]
                return

    def _queued_fair_key_locked(self, kind: str, fair_key: str) -> int:
        return sum(
            1 for waiter in self._waiters.get(kind, ()) if waiter.fair_key == fair_key
        )

    def _queued_scope_locked(self, kind: str, scope_key: str) -> int:
        return sum(
            1 for waiter in self._waiters.get(kind, ()) if waiter.scope_key == scope_key
        )

    def _grant_waiters_locked(self, kind: str) -> None:
        waiters = self._waiters.setdefault(kind, [])
        while waiters and self._can_run_locked(kind):
            index = self._next_fair_waiter_index_locked(kind)
            waiter = waiters.pop(index)
            waiter.granted = True
            self._pending[kind] = self._pending.get(kind, 0) + 1
            self._last_fair_key[kind] = waiter.fair_key
            _resolve(waiter.ready, "granted")
        if not waiters:
            self._waiters.pop(kind, None)

    def _next_fair_waiter_index_locked(self, kind: str) -> int:
        """选择活跃数最少的 FairKey 上队；平局优先非最近放行的 key，再按入队时间。"""
        waiters = self._waiters.get(kind, [])
        if not waiters:
            return 0
        active_by_key: dict[str, int] = {}
        for active in self._active.values():
            if active.request.kind != kind:
                continue
            active_by_key[active.request.fair_key] = (
                active_by_key.get(active.request.fair_key, 0) + 1
            )
        last_key = self._last_fair_key.get(kind, "")
        best_index = 0
        best_count = -1
        best_is_last = False
        best_queued_at: datetime | None = None
        for index, waiter in enumerate(waiters):
            count = active_by_key.get(waiter.fair_key, 0)
            is_last = waiter.fair_key == last_key
            better = False
            if best_count < 0 or count < best_count:
                better = True
            elif count == best_count and best_is_last and not is_last:
                better = True
            elif count == best_count and best_is_last == is_last and (
                best_queued_at is None or waiter.queued_at < best_queued_at
            ):
                better = True
            if better:
                best_index = index
                best_count = count
                best_is_last = is_last
                best_queued_at = waiter.queued_at
        return best_index

    # -- 完成与快照 ---------------------------------------------------------

    def _finish(self, request_id: str, cancel: bool) -> None:
        with self._lock:
            active = self._active.pop(request_id, None)
            if active is not None:
                self._grant_waiters_locked(active.request.kind)
        if active is not None and cancel:
            active.token.cancel("request done")

    def snapshot(self) -> "Snapshot":
        with self._lock:
            out = Snapshot(
                timeouts=self._timeouts,
                queue_busy_rejected=self._queue_busy_rejected,
                queue_full_rejected=self._queue_full_rejected,
                queue_timeout_rejected=self._queue_timeout_rejected,
                queue_cancel_rejected=self._queue_cancelled_rejected,
                queue_admitted=self._queue_admitted,
            )
            for kind, count in self._pending.items():
                if count > 0:
                    out.pending_by_kind[kind] = count
            now = _now()
            for kind, waiters in self._waiters.items():
                if not waiters:
                    continue
                out.queued_by_kind[kind] = len(waiters)
                for waiter in waiters:
                    age = (now - waiter.queued_at).total_seconds()
                    if out.oldest_queued_wait == 0.0 or age > out.oldest_queued_wait:
                        out.oldest_queued_wait = age
            if out.queue_admitted > 0:
                out.queue_average_wait_ms = int(
                    self._queue_wait_nanos / out.queue_admitted / 1_000_000
                )
            oldest: datetime | None = None
            for active in self._active.values():
                request = active.request
                out.active.append(request)
                out.count_by_kind[request.kind] = out.count_by_kind.get(request.kind, 0) + 1
                if request.started_at is not None and (
                    oldest is None or request.started_at < oldest
                ):
                    oldest = request.started_at
            # Go 遍历 map 的顺序是随机的；这里按 (started_at, id) 排序，
            # 让调用方拿到稳定结果。
            out.active.sort(key=lambda req: (req.started_at or now, req.id))
            if oldest is not None:
                out.oldest_started_at = oldest
                out.oldest_age = (now - oldest).total_seconds()
            return out


@dataclass
class Snapshot:
    """某一时刻的 active request 视图。对应 Go ``request.Snapshot``。"""

    active: list[Request] = field(default_factory=list)
    count_by_kind: dict[str, int] = field(default_factory=dict)
    pending_by_kind: dict[str, int] = field(default_factory=dict)
    queued_by_kind: dict[str, int] = field(default_factory=dict)
    timeouts: int = 0
    oldest_started_at: datetime | None = None
    oldest_age: float = 0.0
    queue_busy_rejected: int = 0
    queue_full_rejected: int = 0
    queue_timeout_rejected: int = 0
    queue_cancel_rejected: int = 0
    queue_admitted: int = 0
    queue_average_wait_ms: int = 0
    oldest_queued_wait: float = 0.0


def new_manager(default_timeout: float) -> Manager:
    """对应 Go ``request.NewManager``。"""
    return Manager(default_timeout)


def new_manager_with_limits(default_timeout: float, limits: Limits | None) -> Manager:
    """对应 Go ``request.NewManagerWithLimits``。"""
    return Manager(default_timeout, limits)


def new_manager_with_limits_and_queue(
    default_timeout: float, limits: Limits | None, queue: QueueConfig | None
) -> Manager:
    """对应 Go ``request.NewManagerWithLimitsAndQueue``。"""
    return Manager(default_timeout, limits, queue)
