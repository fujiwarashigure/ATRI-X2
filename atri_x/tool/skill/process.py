"""子进程启动、超时/取消与进程树终止。

对应 Go：``internal/tool/skill/process_unix.go`` + ``process_windows.go``
（两个 build-tag 文件合并成一个模块，按 :data:`IS_WINDOWS` 分支），外加
``runner.go`` / ``command_tool.go`` 里 ``exec.CommandContext`` 的等价物。

Go 的 ``exec.Cmd`` 由 ``asyncio.create_subprocess_exec`` 承担（Python 侧没有
跨平台的同步子进程 + 上下文取消原语）：

* ``Setpgid: true``（unix）→ ``start_new_session=True``；
* ``cmd.Cancel`` + ``WaitDelay`` → 取消时杀进程树，再给管道至多
  :data:`DEFAULT_WAIT_DELAY` 秒收尾；
* ``taskkill /T /F /PID``（windows）→ :mod:`subprocess` 直接调用。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Any

from atri_x.processenv import Command

__all__ = [
    "DEFAULT_WAIT_DELAY",
    "IS_WINDOWS",
    "MAX_RUNNER_OUTPUT",
    "CommandOutput",
    "kill_command_process_tree",
    "run_command",
    "spawn_kwargs",
    "truncate_output",
]

#: 是否 Windows（Go 用 ``runtime.GOOS``，这里用 ``os.name``）。
IS_WINDOWS = os.name == "nt"

#: 进程被取消后，留给 I/O 收尾的时间（Go ``cmd.WaitDelay = 5 * time.Second``）。
DEFAULT_WAIT_DELAY = 5.0

#: 单个子进程输出片段的上限（Go ``maxRunnerOutput``）。
MAX_RUNNER_OUTPUT = 16 * 1024


def truncate_output(text: str) -> str:
    """截断子进程输出。对应 Go ``truncateOutput``（按字节截断）。"""
    raw = (text or "").encode("utf-8")
    if len(raw) <= MAX_RUNNER_OUTPUT:
        return text or ""
    return raw[:MAX_RUNNER_OUTPUT].decode("utf-8", errors="ignore") + (
        "\n... output truncated ...\n"
    )


@dataclass
class CommandOutput:
    """一次子进程运行的结果。对应 Go 里 ``cmd.Run()`` + 两个 buffer。"""

    code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    ctx_error: BaseException | None = None

    def stdout_text(self) -> str:
        return decode_output(self.stdout)

    def stderr_text(self) -> str:
        return decode_output(self.stderr)


def decode_output(data: bytes) -> str:
    """子进程输出解码。对应 Go 的 ``buffer.String()``（字节即文本）。"""
    return (data or b"").decode("utf-8", errors="replace")


def spawn_kwargs() -> dict[str, Any]:
    """平台相关的 ``create_subprocess_exec`` 额外参数。

    Windows 分支对应 Go ``configureCommandProcess`` 的空实现；unix 分支对应
    ``SysProcAttr{Setpgid: true}``，这样取消时能杀掉整棵进程树。
    """
    if IS_WINDOWS:
        return {}
    return {"start_new_session": True}


def kill_command_process_tree(process: Any) -> None:
    """终止进程及其子进程。对应 Go ``killCommandProcessTree``。

    Windows 用 ``taskkill /T /F /PID``（Go 同样如此，且是同步调用），其它平台
    用 ``SIGKILL`` 打整个进程组。
    """
    if process is None or getattr(process, "pid", None) is None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


async def run_command(
    ctx: Any,
    command: Command,
    *,
    cwd: str = "",
    stdin_data: bytes = b"",
    wait_delay: float = DEFAULT_WAIT_DELAY,
) -> CommandOutput:
    """启动并等待一个子进程。对应 Go 的 ``cmd.Run()``（带上下文取消）。

    启动失败（二进制不存在等）原样抛 ``OSError``，由调用方包成
    ``start <label> failed``；``ctx`` 取消时杀掉进程树，把取消错误放在
    :attr:`CommandOutput.ctx_error` 里返回（对应 Go 先看 ``ctx.Err()``）。
    """
    process = await asyncio.create_subprocess_exec(
        *command.args,
        executable=command.executable or None,
        env=command.env_dict(),
        cwd=cwd or None,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **spawn_kwargs(),
    )
    communicate = asyncio.ensure_future(process.communicate(stdin_data))
    cancel_task: asyncio.Future | None = None
    if ctx is not None:
        cancel_task = asyncio.ensure_future(_wait_cancel(ctx))
    try:
        if cancel_task is None:
            stdout, stderr = await communicate
        else:
            await asyncio.wait(
                {communicate, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if communicate.done():
                stdout, stderr = communicate.result()
            else:
                error = _ctx_error(ctx)
                await _kill_and_drain(process, communicate, wait_delay)
                stdout, stderr = _partial_output(communicate)
                return CommandOutput(
                    code=_return_code(process),
                    stdout=stdout,
                    stderr=stderr,
                    ctx_error=error,
                )
    except asyncio.CancelledError:
        # 外层任务被取消：同样要收掉进程，否则会留下孤儿。
        await _kill_and_drain(process, communicate, wait_delay)
        raise
    finally:
        for task in (communicate, cancel_task):
            if task is not None and not task.done():
                task.cancel()
    return CommandOutput(
        code=_return_code(process), stdout=stdout, stderr=stderr
    )


async def _kill_and_drain(
    process: Any, communicate: "asyncio.Future[Any]", wait_delay: float
) -> None:
    """杀掉进程树并给管道至多 ``wait_delay`` 秒收尾（对应 ``WaitDelay``）。"""
    kill_command_process_tree(process)
    if wait_delay is None or wait_delay < 0:
        wait_delay = DEFAULT_WAIT_DELAY
    try:
        await asyncio.wait_for(asyncio.shield(communicate), wait_delay)
    except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾期不抛
        if not communicate.done():
            communicate.cancel()
    try:
        await asyncio.wait_for(process.wait(), wait_delay)
    except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def _return_code(process: Any) -> int:
    code = getattr(process, "returncode", None)
    if code is None:
        return -1
    return int(code)


def _partial_output(communicate: "asyncio.Future[Any]") -> tuple[bytes, bytes]:
    """收尾后尽量取出已经读到的输出（Go 的 buffer 也是这样留下部分内容）。"""
    if not communicate.done() or communicate.cancelled():
        return b"", b""
    try:
        stdout, stderr = communicate.result()
    except BaseException:  # noqa: BLE001 - 进程被杀时结果可能不可用
        return b"", b""
    return stdout or b"", stderr or b""


async def _wait_cancel(ctx: Any) -> None:
    """等 ``ctx`` 取消（对应 Go 的 ``<-ctx.Done()``）。"""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _resolve() -> None:
        if not future.done():
            future.set_result(None)

    remove = ctx.add_callback(_resolve)
    try:
        if _ctx_error(ctx) is not None:
            return
        await future
    finally:
        remove()


def _ctx_error(ctx: Any) -> BaseException | None:
    """``ctx.Err()``：未取消返回 ``None``。"""
    if ctx is None:
        return None
    error = ctx.error()
    if error is not None:
        return error
    if ctx.is_cancelled():
        from atri_x.safecontext import CancelledError

        return CancelledError("context canceled")
    return None
