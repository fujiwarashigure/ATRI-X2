"""``state.toml`` 的外部编辑热加载与状态查询。

对应 Go：``internal/agent/state_watch.go``。

真正的"合并 → 快照 → 落盘"在 ``internal/agent/model.go`` 的
``refreshRuntimeState`` / ``loadRuntimeStateAtStartup`` 里（Python 侧属同僚
``atri_x/agent/model.py``）；本模块只做轮询、报告与用户提示，通过
``agent.refresh_runtime_state(force)`` 鸭子类型调用，未装配时安静降级。
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.agent.commands.deps import RuntimeStateReloadReport, RuntimeStateStatus
from atri_x.delivery import Notice, text as text_output
from atri_x.logging import ResultSucceeded

from atri_x.agent.logging import audit

__all__ = [
    "RUNTIME_STATE_WATCH_EVERY",
    "RuntimeStateReload",
    "poll_runtime_state",
    "reload_runtime_state",
    "runtime_state_reload_text",
    "runtime_state_status",
    "start_runtime_state_watch",
]

#: 外部编辑最长多久被发现（不依赖操作者手动 ``/state reload``）。
#: 对应 Go ``runtimeStateWatchEvery``。
RUNTIME_STATE_WATCH_EVERY = 15.0


@dataclass
class RuntimeStateReload:
    """一次 ``state.toml`` 刷新的结果。对应 Go ``runtimeStateReload``。"""

    path: str = ""
    #: 文件确实被读入并合并进内存。
    applied: bool = False
    #: 生效值发生变化的运行时分区。
    changed: list[str] = field(default_factory=list)
    #: 本次应用的文件 mtime。
    mod_time: datetime | None = None


def start_runtime_state_watch(agent: Any, ctx: Any = None) -> None:
    """轮询 ``state.toml``，让进程外的编辑不必重启就生效。

    对应 Go ``Agent.StartRuntimeStateWatch``：可以重复调用；ctx 结束时退出。
    """
    if agent is None or not getattr(agent, "state_path", ""):
        return
    lock = _state_watch_lock(agent)
    with lock:
        if getattr(agent, "state_watch_started", False):
            return
        agent.state_watch_started = True
    loop = _current_loop()
    if loop is None:  # pragma: no cover - 监视器必须在事件循环里启动
        return
    agent.state_watch_task = loop.create_task(_watch_loop(agent, ctx))


async def _watch_loop(agent: Any, ctx: Any) -> None:
    token = _as_token(ctx)
    while True:
        if token is not None and token.is_cancelled():
            return
        await asyncio.sleep(RUNTIME_STATE_WATCH_EVERY)
        if token is not None and token.is_cancelled():
            return
        await poll_runtime_state(agent, ctx)


async def poll_runtime_state(agent: Any, ctx: Any) -> None:
    """对应 Go ``Agent.pollRuntimeState``。"""
    try:
        reload_result = await _refresh(agent, False)
    except Exception as exc:  # noqa: BLE001 - 热加载失败只记日志
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.warn(
                "reload externally edited state config",
                path=getattr(agent, "state_path", ""),
                error=str(exc),
            )
        return
    if not reload_result.applied or not reload_result.changed:
        return
    audit(
        agent,
        "runtime_state_reloaded",
        "path",
        reload_result.path,
        "sections",
        ",".join(reload_result.changed),
        "trigger",
        "watch",
        "result",
        ResultSucceeded,
    )
    try:
        await agent.send_notice(
            ctx,
            Notice(outputs=[text_output(runtime_state_reload_text(reload_result))]),
        )
    except Exception:  # noqa: BLE001 - 通知失败不影响热加载
        pass


async def reload_runtime_state(agent: Any, ctx: Any) -> RuntimeStateReloadReport:
    """立刻应用 ``state.toml``（忽略 mtime 闸门）。对应 Go ``Agent.ReloadRuntimeState``。"""
    reload_result = await _refresh(agent, True)
    report = RuntimeStateReloadReport(
        path=reload_result.path,
        applied=reload_result.applied,
        changed=list(reload_result.changed),
        mod_time=reload_result.mod_time,
    )
    if reload_result.applied and reload_result.changed:
        audit(
            agent,
            "runtime_state_reloaded",
            "path",
            reload_result.path,
            "sections",
            ",".join(reload_result.changed),
            "trigger",
            "command",
            "result",
            ResultSucceeded,
        )
    return report


def runtime_state_status(agent: Any) -> RuntimeStateStatus:
    """报告已加载的 state 文件以及磁盘文件是否更新。对应 Go ``Agent.RuntimeStateStatus``。"""
    status = RuntimeStateStatus()
    if agent is None or not getattr(agent, "state_path", ""):
        return status
    status.path = agent.state_path
    with _state_lock(agent):
        status.loaded_mod_time = getattr(agent, "state_mod_time", None)
    try:
        info = os.stat(agent.state_path)
    except OSError:
        return status
    status.file_mod_time = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    loaded = status.loaded_mod_time
    status.pending = loaded is None or status.file_mod_time > loaded
    return status


def runtime_state_reload_text(reload_result: RuntimeStateReload) -> str:
    """热加载生效提示。对应 Go ``runtimeStateReloadText``。"""
    sections = "、".join(reload_result.changed) or "（无变化）"
    message = "state.toml 已被外部修改并生效：" + sections
    if reload_result.mod_time is not None:
        message += "（" + reload_result.mod_time.strftime("%Y-%m-%d %H:%M:%S") + "）"
    return message


async def _refresh(agent: Any, force: bool) -> RuntimeStateReload:
    """调用同僚模块提供的真实热加载；未装配时返回"无变化"。"""
    refresher = getattr(agent, "refresh_runtime_state", None)
    if not callable(refresher):
        return RuntimeStateReload(path=getattr(agent, "state_path", ""), applied=False)
    result = refresher(force)
    if asyncio.iscoroutine(result):
        result = await result
    return _coerce_reload(result, getattr(agent, "state_path", ""))


def _coerce_reload(result: Any, path: str) -> RuntimeStateReload:
    if isinstance(result, RuntimeStateReload):
        return result
    if isinstance(result, tuple):  # 兼容 ``(reload, error)`` 双返回
        result = result[0]
    if result is None:
        return RuntimeStateReload(path=path, applied=False)
    return RuntimeStateReload(
        path=getattr(result, "path", path) or path,
        applied=bool(getattr(result, "applied", False)),
        changed=list(getattr(result, "changed", []) or []),
        mod_time=getattr(result, "mod_time", None),
    )


def _state_lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "state_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.state_mu = lock
    return lock


def _state_watch_lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "state_watch_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.state_watch_mu = lock
    return lock


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _as_token(ctx: Any) -> Any:
    from atri_x.safecontext import CancelToken

    return ctx if isinstance(ctx, CancelToken) else None
