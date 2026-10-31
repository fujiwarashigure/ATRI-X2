"""worker 的协议传输层：读写帧、请求/响应配对、进程强杀。

对应 Go：``internal/hook/runtime/worker.go`` 里的 ``request`` / ``write`` /
``readLoop`` / ``deliverResponse`` / ``handlePluginEvent`` / ``notifyCancel`` /
``kill`` 与 ``stderrLogger``。

Go 把这些挂在 ``*worker`` 上（同包跨文件）；Python 一个类只能有一个类体，
所以这里以"接收 worker 的函数"形式实现，``Worker`` 再薄薄地委托一层。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from atri_x.errors import AtriXError
from atri_x.hook.protocol import (
    MAX_PROTOCOL_FRAME_BYTES,
    LineReader,
    ProtocolFrameError,
    decode_frame,
    encode_frame,
    kill_process_group,
    new_request,
    validate_id,
)
from atri_x.hook.protocol.frames import Frame
from atri_x.hook.runtime.config import Status

__all__ = [
    "Pending",
    "params_json",
    "request",
    "write",
    "read_loop",
    "deliver_response",
    "spawn_kill",
    "handle_plugin_event",
    "stderr_loop",
    "kill",
    "notify_cancel",
]


@dataclass
class Pending:
    """一个在途请求。对应 Go ``pending`` 里的 channel。"""

    future: "asyncio.Future[Frame]"
    method: str


def params_json(params: Any) -> Any:
    """把参数收敛成可序列化对象。对应 Go 里直接 ``json.Marshal(params)``。"""
    if params is None or isinstance(params, (str, bytes, bytearray)):
        return params
    to_json = getattr(params, "to_json", None)
    if callable(to_json):
        return to_json()
    return params


async def request(
    worker: Any,
    method: str,
    params: Any,
    timeout: float | None = None,
    ctx: Any = None,
) -> str | None:
    """发一个请求并等响应。对应 Go ``worker.request``。"""
    from atri_x.hook.runtime.routes import random_id
    from atri_x.hook.runtime.worker import (
        WorkerError,
        WorkerProcessError,
        WorkerTimeoutError,
    )
    from atri_x.hook.protocol.stream import await_cancelable

    ident = random_id("host")
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[Frame]" = loop.create_future()
    with worker.mu:
        if worker.stdin is None:
            raise WorkerProcessError("hook process is not running")
        worker.pending[ident] = Pending(future=future, method=method)
    try:
        frame = new_request(ident, method, params_json(params))
        await write(worker, frame)
        wait: Any = future
        if timeout is not None and timeout > 0:
            wait = asyncio.ensure_future(
                asyncio.wait_for(asyncio.shield(future), timeout)
            )
        try:
            value = await await_cancelable(wait, ctx)
        except asyncio.TimeoutError as exc:
            raise WorkerTimeoutError(
                f"hook request {method!r} timed out after {timeout}s"
            ) from exc
        if value.ok is not True:
            raise WorkerError(value.error or "hook request failed")
        return value.result
    finally:
        with worker.mu:
            worker.pending.pop(ident, None)


async def write(worker: Any, frame: Frame) -> None:
    """写一帧到插件 stdin。对应 Go ``worker.write``。"""
    from atri_x.hook.runtime.worker import WorkerError, WorkerProcessError

    data = encode_frame(frame)
    if len(data) + 1 > MAX_PROTOCOL_FRAME_BYTES:
        raise WorkerError("hook protocol stdin frame exceeds 16 MiB limit")
    async with worker.write_mu:
        with worker.mu:
            stdin = worker.stdin
        if stdin is None:
            raise WorkerProcessError("hook process stdin is closed")
        try:
            stdin.write(data + b"\n")
            await stdin.drain()
        except (OSError, ConnectionError, RuntimeError) as exc:
            raise WorkerProcessError(f"write hook protocol frame: {exc}") from exc


async def read_loop(worker: Any, proc: Any) -> None:
    """读插件 stdout。对应 Go ``worker.readLoop``。"""
    reader = LineReader(proc.stdout)
    while True:
        try:
            line = await reader.read_line()
        except ProtocolFrameError as exc:
            worker.set_status(Status.DEGRADED, f"read hook stdout: {exc}")
            return
        except (ConnectionResetError, ValueError) as exc:
            worker.set_status(Status.DEGRADED, f"read hook stdout: {exc}")
            return
        if line is None:
            return
        text = line.strip()
        if not text:
            continue
        try:
            value = decode_frame(text)
        except ValueError as exc:
            worker.set_status(
                Status.DEGRADED, f"invalid hook.v2 stdout frame: {exc}"
            )
            await kill(worker)
            return
        if value.type == "response":
            deliver_response(worker, value)
        elif value.type == "request":
            invalid = False
            try:
                validate_id(value.id, "plugin:")
            except ValueError:
                invalid = True
            if not invalid and not (value.method or "").strip():
                invalid = True
            if invalid:
                worker.set_status(Status.DEGRADED, "invalid hook request frame")
                await kill(worker)
                return
            asyncio.ensure_future(worker.handle_plugin_request(value))
        elif value.type == "event":
            handle_plugin_event(worker, value)
        else:
            worker.set_status(
                Status.DEGRADED,
                f"unsupported hook.v2 frame type {value.type}",
            )
            await kill(worker)
            return


def deliver_response(worker: Any, value: Frame) -> None:
    """把响应交给等待中的请求。对应 Go ``worker.deliverResponse``。"""
    try:
        validate_id(value.id, "host:")
    except ValueError:
        worker.set_status(Status.DEGRADED, "response id must use host: prefix")
        spawn_kill(worker)
        return
    with worker.mu:
        pending = worker.pending.get(value.id)
    if pending is not None and not pending.future.done():
        pending.future.set_result(value)


def spawn_kill(worker: Any) -> None:
    """后台强杀（供同步上下文调用）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover
        return
    loop.create_task(kill(worker))


def handle_plugin_event(worker: Any, value: Frame) -> None:
    """插件主动上报的事件（目前只有 ``hook.log``）。对应 Go ``handlePluginEvent``。"""
    logger = worker.manager.opts.logger
    if value.method == "hook.log" and logger is not None:
        logger.info(
            "stateful hook event",
            hook=worker.config.id,
            params=value.params or "",
        )


async def stderr_loop(worker: Any, proc: Any) -> None:
    """把子进程 stderr 逐行写进日志。对应 Go 的 ``stderrLogger``。"""
    logger = worker.manager.opts.logger
    stream = proc.stderr
    if stream is None:
        return
    while True:
        try:
            line = await stream.readline()
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            return
        if not line:
            return
        text = line.decode("utf-8", "replace").strip()
        if text and logger is not None:
            logger.info("stateful hook stderr", hook=worker.config.id, line=text)


async def kill(worker: Any) -> None:
    """强杀进程树。对应 Go ``worker.kill`` + ``killHookProcessTree``。"""
    with worker.mu:
        proc = worker.proc
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if pid:
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/T",
                    "/F",
                    "/PID",
                    str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except OSError:
                pass
        else:
            # 只杀 worker 自己的进程组（worker 用 detached_process_kwargs 起了新会话）。
            kill_process_group(pid)
    try:
        proc.kill()
    except (ProcessLookupError, RuntimeError):
        pass


async def notify_cancel(worker: Any, conversation_id: str) -> None:
    """通知插件事件已取消。对应 Go ``worker.notifyCancel``。"""
    from atri_x.hook.runtime.protocol import must_json

    params: dict[str, str] = {}
    if (conversation_id or "").strip():
        params["conversation_id"] = conversation_id
    try:
        await write(
            worker,
            Frame(type="event", method="event.cancel", params=must_json(params)),
        )
    except AtriXError:
        pass
