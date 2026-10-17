"""会话收件箱：串行化一个 Session 的普通回合，并合并静默窗口内的连续消息。

对应 Go：``internal/agent/inbox.go``。

收件箱是"可选启用"的：只有群开了合并窗口或共享会话模式才走它。提交者**故意**
阻塞到自己的批次跑完，因此平台适配器看到的仍是"一条入站消息一次同步
HandleMessage"，既有的错误与撤回语义不变。

与 Go 的差异（仅形式）：goroutine + channel → ``asyncio.Task`` +
:class:`asyncio.Future`；``sync.Mutex`` → :class:`threading.Lock`（临界区里没有
``await``，用同步锁最贴近 Go 的语义，也不怕别的线程进来读计数）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from atri_x.delivery import ctx_error
from atri_x.errors import NotFoundError
from atri_x.logging import ResultSkipped
from atri_x.platform import message_context_from
from atri_x.turn import Input, PhaseIdle, merge_inputs

from atri_x.agent.errors import mark_user_notified
from atri_x.agent.events import message_work_key, with_message_work_keys
from atri_x.agent.logging import audit
from atri_x.agent.segments import with_inbound_turn_input

__all__ = [
    "MAX_INBOX_ITEMS_PER_SESSION",
    "InboxBatch",
    "InboxItem",
    "InboxQueue",
    "SessionInbox",
    "cancel_inbox_fair_key",
    "cancel_inbox_message",
    "cancel_inbox_scope",
    "cancel_inbox_session",
    "inbox_pending_for_fair_key",
    "inbox_submit",
    "new_session_inbox",
    "submit_inbound",
    "use_inbound_inbox",
    "wait_inbox_batch",
    "_inbox_session_impl",
    "_wait_inbox_idle_impl",
]

#: 每个 Session 最多排队的消息条数。对应 Go ``maxInboxItemsPerSession``。
MAX_INBOX_ITEMS_PER_SESSION = 64


@dataclass
class InboxItem:
    """收件箱里的一条待处理消息。对应 Go ``inboxItem``。"""

    input: Input = field(default_factory=Input)
    ctx: Any = None
    message_key: str = ""


@dataclass
class InboxBatch:
    """一个待跑（或正在跑）的批次。对应 Go ``inboxBatch``。"""

    actor_id: str = ""
    fair_key: str = ""
    scope_key: str = ""
    items: list[InboxItem] = field(default_factory=list)
    deadline: float = 0.0
    sealed: bool = False
    waiters: int = 0
    err: BaseException | None = None
    done: asyncio.Future | None = None
    finished: bool = False

    def finish(self, err: BaseException | None = None) -> None:
        """完成批次（幂等）。对应 Go ``inboxBatch.finish``。"""
        if self.finished:
            return
        self.finished = True
        self.err = err
        future = self.done
        if future is not None and not future.done():
            future.set_result(err)


class InboxQueue:
    """一个 Session 的批次队列。对应 Go ``inboxQueue``。"""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.batches: list[InboxBatch] = []
        self.running = False
        self.full_notified = False
        self.task: asyncio.Task | None = None


class SessionInbox:
    """所有 Session 的收件箱。对应 Go ``sessionInbox``。"""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.queues: dict[str, InboxQueue] = {}


def new_session_inbox() -> SessionInbox:
    """对应 Go ``newSessionInbox``。"""
    return SessionInbox()


def _ensure_inbox(agent: Any) -> SessionInbox | None:
    """对应 Go ``Agent.ensureInbox``。"""
    if agent is None:
        return None
    lock = getattr(agent, "inbox_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.inbox_mu = lock
    with lock:
        if getattr(agent, "inbox", None) is None:
            agent.inbox = new_session_inbox()
        return agent.inbox


def _inbox_queue(agent: Any, session_id: str) -> InboxQueue:
    """对应 Go ``Agent.inboxQueue``。"""
    inbox = _ensure_inbox(agent)
    if inbox is None:
        return InboxQueue()
    with inbox.mu:
        queue = inbox.queues.get(session_id)
        if queue is None:
            queue = InboxQueue()
            inbox.queues[session_id] = queue
        return queue


async def inbox_submit(
    agent: Any,
    ctx: Any,
    session_id: str,
    input_value: Input,
    window: float,
    fair_key: str,
    scope_key: str,
    message_key: str,
) -> None:
    """把一条入站消息排进收件箱并等它跑完。对应 Go ``Agent.inboxSubmit``。"""
    if agent is None:
        raise RuntimeError("agent is nil")
    inbox = _ensure_inbox(agent)
    if inbox is None:
        raise RuntimeError("agent inbox is unavailable")
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session id is required")
    actor_id = (input_value.speaker.actor_id or "").strip()
    if not actor_id:
        actor_id = (input_value.speaker.user_id or "").strip()
    if not actor_id:
        # 绝不合并没有稳定发言人身份的消息；这个兜底也让批次的公平键/撤回记账确定。
        from atri_x.storage import new_id

        actor_id = "anonymous:" + new_id()

    queue = _inbox_queue(agent, session_id)
    batch = InboxBatch(
        actor_id=actor_id,
        fair_key=(fair_key or "").strip(),
        scope_key=(scope_key or "").strip(),
        deadline=time.monotonic() + max(window, 0.0),
        done=asyncio.get_running_loop().create_future(),
    )
    item = InboxItem(input=input_value, ctx=ctx, message_key=(message_key or "").strip())

    with queue.mu:
        queued_items = sum(len(pending.items) for pending in queue.batches if pending)
        if queued_items >= MAX_INBOX_ITEMS_PER_SESSION:
            notify = not queue.full_notified
            queue.full_notified = True
            full = True
        else:
            full = False
        if not full:
            queue.full_notified = False
            merged = False
            if window > 0 and queue.batches:
                tail = queue.batches[-1]
                if tail is not None and not tail.sealed and tail.actor_id == actor_id:
                    tail.items.append(item)
                    tail.deadline = time.monotonic() + window
                    batch = tail
                    merged = True
            if merged:
                batch.waiters += 1
                waiter = batch.waiters
            else:
                batch.items.append(item)
                batch.waiters = 1
                queue.batches.append(batch)
                waiter = 1
                if not queue.running:
                    queue.running = True
                    queue.task = asyncio.get_running_loop().create_task(
                        _run_inbox_queue(agent, session_id, queue)
                    )
    if full:
        audit(
            agent,
            "inbox_queue_full",
            "session_id",
            session_id,
            "actor_id",
            actor_id,
            "queued",
            queued_items,
            "limit",
            MAX_INBOX_ITEMS_PER_SESSION,
            "result",
            ResultSkipped,
        )
        if notify:
            await agent.send_chat(ctx, "当前会话待处理消息过多，请稍后再试。")
        raise mark_user_notified(RuntimeError("session inbox queue is full"))
    await wait_inbox_batch(ctx, batch, waiter)


async def wait_inbox_batch(ctx: Any, batch: InboxBatch, waiter: int) -> None:
    """等一个批次跑完。对应 Go ``waitInboxBatch``。"""
    future = batch.done
    if future is None:
        return
    token = _as_token(ctx)
    if token is None:
        err = await future
    else:
        err = await _await_future(future, token)
    if err is None:
        return
    if waiter > 1:
        # 只有第一个等待者把回合错误报给用户；被合并进来的后续消息不得重复提示。
        raise mark_user_notified(err)
    raise err


async def _await_future(future: asyncio.Future, token: Any) -> Any:
    if token.is_cancelled():
        raise token.error() or _cancelled()
    cancel_future = asyncio.get_running_loop().create_future()

    def _on_cancel() -> None:
        if not cancel_future.done():
            cancel_future.set_result(None)

    remove = token.add_callback(_on_cancel)
    try:
        done, _pending = await asyncio.wait(
            {future, cancel_future}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        remove()
    if future in done:
        return future.result()
    raise token.error() or _cancelled()


async def _run_inbox_queue(agent: Any, session_id: str, queue: InboxQueue) -> None:
    """收件箱队列主循环。对应 Go ``Agent.runInboxQueue``。"""
    while True:
        with queue.mu:
            if not queue.batches:
                queue.running = False
                return
            head = queue.batches[0]
            wait = head.deadline - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
            continue
        with queue.mu:
            if not queue.batches or queue.batches[0] is not head:
                continue
            head.sealed = True
            queue.batches.pop(0)
        await _run_inbox_batch(agent, session_id, head)


async def _run_inbox_batch(agent: Any, session_id: str, batch: InboxBatch) -> None:
    """跑一个批次。对应 Go ``Agent.runInboxBatch``。"""
    if batch is None:
        return
    try:
        items = [
            item for item in batch.items if item.ctx is None or ctx_error(item.ctx) is None
        ]
        if not items:
            batch.finish(_cancelled())
            return
        merged = merge_inputs([item.input for item in items])
        run_ctx = items[0].ctx
        message_keys = [item.message_key for item in items if item.message_key]
        run_ctx = with_message_work_keys(run_ctx, message_keys)

        session = await agent.inbox_session(run_ctx, session_id)
        if session is None:
            batch.finish(NotFoundError("not found"))
            return
        await agent.wait_inbox_idle(run_ctx, session.id)
        run_ctx = with_inbound_turn_input(run_ctx, merged)
        await agent.handle_session_input(run_ctx, session, merged.text)
    except asyncio.CancelledError as exc:  # 取消是正常终态，不当成失败
        batch.finish(exc)
        return
    except BaseException as exc:  # noqa: BLE001 - 批次错误回给等待者
        batch.finish(exc)
        return
    batch.finish(None)


async def use_inbound_inbox(agent: Any, ctx: Any) -> bool:
    """当前消息是否该走收件箱。对应 Go ``Agent.useInboundInbox``。"""
    if agent is None:
        return False
    if agent.scope(ctx).shared:
        return True
    return agent.group_merge_window(ctx) > 0


async def submit_inbound(agent: Any, ctx: Any, session_id: str, text: str) -> None:
    """把当前入站消息提交给收件箱。对应 Go ``Agent.submitInbound``。"""
    input_value = agent.turn_input_for_message(ctx, text)
    window = agent.group_merge_window(ctx)
    if window > 0:
        from atri_x.agent.segments import inbound_reply_message_id

        if inbound_reply_message_id(ctx):
            window = 0
        msg, ok = message_context_from(ctx)
        if ok and ((msg.resume_session_id or "").strip() or (msg.fork_from_message_id or "").strip()):
            window = 0
    try:
        await inbox_submit(
            agent,
            ctx,
            session_id,
            input_value,
            window,
            agent.request_fair_key(ctx),
            agent.request_scope_key(ctx),
            inbound_message_work_key(ctx),
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - 提交失败原样冒泡
        raise exc


def inbound_message_work_key(ctx: Any) -> str:
    """当前入站消息的工作键。对应 Go ``inboundMessageWorkKey``。"""
    msg, ok = message_context_from(ctx)
    if not ok:
        return ""
    return message_work_key(msg.platform, msg.scope_id, msg.platform_message_id)


def inbox_pending_for_fair_key(agent: Any, fair_key: str) -> int:
    """某个公平键在收件箱里还有多少条待处理。对应 Go ``Agent.inboxPendingForFairKey``。"""
    fair_key = (fair_key or "").strip()
    if agent is None or not fair_key:
        return 0
    inbox = _ensure_inbox(agent)
    if inbox is None:
        return 0
    with inbox.mu:
        queues = list(inbox.queues.values())
    total = 0
    for queue in queues:
        with queue.mu:
            for batch in queue.batches:
                if batch is None or batch.fair_key != fair_key:
                    continue
                total += len(batch.items)
    return total


def cancel_inbox_session(agent: Any, session_id: str) -> int:
    """取消整个 Session 的排队消息。对应 Go ``Agent.cancelInboxSession``。"""
    session_id = (session_id or "").strip()
    if agent is None or not session_id:
        return 0
    return _cancel_inbox_queues(agent, lambda _sid, _batch: True, session_id)


def cancel_inbox_fair_key(agent: Any, fair_key: str) -> int:
    """对应 Go ``Agent.cancelInboxFairKey``。"""
    fair_key = (fair_key or "").strip()
    if agent is None or not fair_key:
        return 0
    return _cancel_inbox_queues(agent, lambda _sid, batch: batch.fair_key == fair_key, "")


def cancel_inbox_scope(agent: Any, scope_key: str) -> int:
    """对应 Go ``Agent.cancelInboxScope``。"""
    scope_key = (scope_key or "").strip()
    if agent is None or not scope_key:
        return 0
    return _cancel_inbox_queues(agent, lambda _sid, batch: batch.scope_key == scope_key, "")


def cancel_inbox_message(agent: Any, message_key: str) -> int:
    """撤回一条还在排队的消息。对应 Go ``Agent.cancelInboxMessage``。"""
    message_key = (message_key or "").strip()
    if agent is None or not message_key:
        return 0
    return _cancel_inbox_queues(
        agent, lambda _sid, batch: _batch_has_message(batch, message_key), ""
    )


def _batch_has_message(batch: InboxBatch | None, message_key: str) -> bool:
    if batch is None:
        return False
    return any(item.message_key == message_key for item in batch.items)


def _cancel_inbox_queues(agent: Any, match: Any, only_session_id: str) -> int:
    inbox = _ensure_inbox(agent)
    if agent is None or inbox is None:
        return 0
    with inbox.mu:
        queues = dict(inbox.queues)
    canceled = 0
    for session_id, queue in queues.items():
        if only_session_id and session_id != only_session_id:
            continue
        with queue.mu:
            kept: list[InboxBatch] = []
            for batch in queue.batches:
                if batch is None or batch.sealed or not match(session_id, batch):
                    kept.append(batch)
                    continue
                batch.finish(_cancelled())
                canceled += 1
            queue.batches = kept
    return canceled


def _cancelled() -> BaseException:
    from atri_x.safecontext import CancelledError

    return CancelledError("context canceled")


async def _inbox_session_impl(agent: Any, ctx: Any, session_id: str) -> Any:
    """排队消息该落到哪个 Session。对应 Go ``Agent.inboxSession``。

    优先用作用域当前 Session，这样上一轮跑过的压缩能被排队消息接上；当前指针被重置
    （例如会话被删）时退回批次排队时记下的那个 Session。
    """
    from atri_x.errors import ErrNotFound, NotFoundError

    if agent is None:
        raise NotFoundError("session not found")
    scope = agent.scope(ctx)
    try:
        current = agent.sessions.current(scope)
    except NotFoundError:
        current = None
    if current is not None:
        return current
    store = getattr(agent, "store", None)
    if store is None:
        raise ErrNotFound
    session = store.sessions().get(session_id)
    if hasattr(session, "__await__"):
        session = await session
    if session is None:
        raise ErrNotFound
    return session


async def _wait_inbox_idle_impl(agent: Any, ctx: Any, session_id: str) -> None:
    """等这个 Session 的回合跑完（退避轮询）。对应 Go ``Agent.waitInboxIdle``。"""
    from atri_x.delivery import ctx_error
    from atri_x.turn import PhaseIdle

    delay = 0.01
    while True:
        turns = getattr(agent, "turns", None)
        if turns is None or turns.snapshot(session_id).phase == PhaseIdle:
            return
        error = ctx_error(ctx)
        if error is not None:
            raise error
        await asyncio.sleep(delay)
        if delay < 0.1:
            delay *= 2


def _as_token(ctx: Any) -> Any:
    from atri_x.safecontext import CancelToken

    return ctx if isinstance(ctx, CancelToken) else None
