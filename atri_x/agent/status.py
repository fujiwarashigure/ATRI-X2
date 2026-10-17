"""运行状态快照的记录与发布。

对应 Go：``internal/agent/status.go``。

Go 的 ``runtimestatus.Snapshot`` → :class:`atri_x.runtime.status.Snapshot`；
``time.Time`` 的零值判定 → ``None``。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import datetime, timezone
from typing import Any

from atri_x.platform import message_context_from
from atri_x.runtime import PhaseDone, Snapshot

__all__ = [
    "merge_runtime_status",
    "publish_runtime_status",
    "record_runtime_status",
    "runtime_done_status",
    "runtime_status_for_session",
    "runtime_status_sender",
    "update_runtime_status",
]

#: 已排进事件循环、还没跑完的状态推送 Task（强引用，完成即移除）。
_PENDING_STATUS_TASKS: set[asyncio.Task[Any]] = set()


def _lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "status_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.status_mu = lock
    return lock


def _statuses(agent: Any) -> dict[str, Snapshot]:
    statuses = getattr(agent, "runtime_status", None)
    if statuses is None:
        statuses = {}
        agent.runtime_status = statuses
    return statuses


def update_runtime_status(agent: Any, ctx: Any, snapshot: Snapshot) -> None:
    """前台 turn 视角发布一次运行状态。对应 Go ``updateRuntimeStatus``。"""
    publish_runtime_status(agent, ctx, snapshot)


def record_runtime_status(agent: Any, snapshot: Snapshot) -> Snapshot:
    """合并进内存快照表并返回合并后的值。对应 Go ``recordRuntimeStatus``。"""
    if not snapshot.session_id:
        return snapshot
    with _lock(agent):
        previous = _statuses(agent).get(snapshot.session_id, Snapshot())
        merged = merge_runtime_status(previous, snapshot)
        _statuses(agent)[merged.session_id] = merged
    return merged


def publish_runtime_status(agent: Any, ctx: Any, snapshot: Snapshot) -> None:
    """记录并推送给平台侧的状态接收者。对应 Go ``publishRuntimeStatus``。

    Go 的 ``runtimeStatusSender.SetRuntimeStatus`` 是**同步**方法（``status.go:11``），
    调用点 ``_ = sender.SetRuntimeStatus(ctx, snapshot)`` 连错误都忽略。本仓两个 CLI
    实现（``platform/cli/adapter.py`` / ``remote_server.py``）已按 Go 改回同步；
    但**别的接收者**仍可能写成 ``async def``，直接同步调用只会造出一个没人 await
    的协程 —— 状态永远送不到，``except`` 也看不见（协程创建本身不抛）。

    因此这里对两种实现都正确分派：

    * 同步实现：直接调用，异常记一条审计告警后继续（不打断回合，与 Go 的 ``_ =`` 等价）；
    * 返回 awaitable：排进事件循环并挂异常回调 —— 不再静默丢弃协程，也不再吞掉它的异常。
    """
    merged = record_runtime_status(agent, snapshot)
    if not merged.session_id:
        return
    sender = runtime_status_sender(agent, ctx)
    if sender is None:
        return
    try:
        result = sender.set_runtime_status(ctx, merged)
    except Exception as exc:  # noqa: BLE001 - 状态推送失败不能影响回合
        _report_status_failure(agent, exc)
        return
    _consume_status_result(agent, result)


def _consume_status_result(agent: Any, result: Any) -> None:
    """消费 ``set_runtime_status`` 的返回值（可能是个协程 / task）。"""
    if result is None or not inspect.isawaitable(result):
        return
    if isinstance(result, asyncio.Future):  # 已经排进循环的 task（含 Task 子类）
        result.add_done_callback(lambda done: _report_status_task(agent, done))
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环：关掉协程（避免 "was never awaited" 的静默失败）并记一条告警。
        close = getattr(result, "close", None)
        if callable(close):
            close()
        _report_status_failure(
            agent, RuntimeError("set_runtime_status is async but no event loop is running")
        )
        return
    task = loop.create_task(result)
    # 事件循环只持**弱引用**，局部变量出栈后任务可能被回收（官方文档要求保存强引用）。
    # 用模块级集合兜底：完成即移除，既不泄漏也不依赖宿主对象有哪个属性。
    _PENDING_STATUS_TASKS.add(task)
    task.add_done_callback(_PENDING_STATUS_TASKS.discard)
    task.add_done_callback(lambda done: _report_status_task(agent, done))


def _report_status_task(agent: Any, done: Any) -> None:
    """协程落地后的回调：异常必须留下痕迹，不能只留在 task 里没人取。"""
    if done.cancelled():
        return
    exc = done.exception()
    if exc is not None:
        _report_status_failure(agent, exc)


def _report_status_failure(agent: Any, exc: BaseException) -> None:
    """记一条审计告警（状态推送永远不能打断回合，与 Go 忽略该错误等价）。"""
    warn = getattr(agent, "audit_warn", None)
    if not callable(warn):
        return
    try:
        warn(
            "runtime_status_publish_failed",
            "error",
            "%s: %s" % (type(exc).__name__, exc),
        )
    except Exception:  # noqa: BLE001 - 审计本身失败不能再往上抛
        pass


def runtime_status_for_session(agent: Any, session_id: str) -> Snapshot:
    """对应 Go ``runtimeStatusForSession``。"""
    with _lock(agent):
        return _statuses(agent).get(session_id, Snapshot())


def runtime_status_sender(agent: Any, ctx: Any) -> Any:
    """找到能接收运行状态的发送者。对应 Go ``runtimeStatusSender``。"""
    msg, ok = message_context_from(ctx)
    if ok and msg.sender is not None and hasattr(msg.sender, "set_runtime_status"):
        return msg.sender
    platform = getattr(agent, "platform", None)
    if platform is not None and hasattr(platform, "set_runtime_status"):
        return platform
    return None


def merge_runtime_status(previous: Snapshot, next: Snapshot) -> Snapshot:
    """把本次快照缺省的字段用上一次的值补齐。对应 Go ``mergeRuntimeStatus``。"""
    if not next.provider:
        next.provider = previous.provider
    if not next.model:
        next.model = previous.model
    if not next.mode:
        next.mode = previous.mode
    if next.turn_started_at is None:
        next.turn_started_at = previous.turn_started_at
    if next.stage_started_at is None:
        next.stage_started_at = previous.stage_started_at
    if next.usage is None:
        next.usage = previous.usage
    if not next.phase:
        next.phase = previous.phase
    return next


def runtime_done_status(base: Snapshot, usage_updated_at: datetime | None) -> Snapshot:
    """构造终态（done）快照。对应 Go ``runtimeDoneStatus``。"""
    if usage_updated_at is None:
        usage_updated_at = datetime.now(timezone.utc)
    base.phase = PhaseDone
    base.request_id = ""
    base.kind = ""
    base.label = ""
    base.tool_name = ""
    base.stage_started_at = base.turn_started_at
    base.finished_at = usage_updated_at
    base.error = ""
    return base
