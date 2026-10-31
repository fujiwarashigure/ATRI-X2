"""``exec`` 动作的进程辅助：stderr 尾巴、错误包装、cwd 解析。

对应 Go：``internal/hook/rules/exec.go`` 的后半部分，以及
``exec_process_unix.go`` / ``exec_process_windows.go``。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from atri_x.hook.protocol.stream import kill_process_group
from atri_x.hook.rules.action import (
    State,
    has_strict_dir,
    render,
    source_base_dir,
    source_strict_dir,
)
from atri_x.hook.rules.types import Action

__all__ = [
    "ExecError",
    "ExecStderrTail",
    "ExecStderrLogger",
    "exec_process_error",
    "exec_context_error",
    "with_exec_stderr",
    "exec_cwd",
    "kill_process_tree",
]

#: stderr 尾巴保留的行数。对应 Go ``execStderrTail.Add`` 里的 ``maxLines``。
STDERR_TAIL_MAX_LINES = 20
#: 尾巴文本的 rune 上限。对应 Go ``execStderrTail.String`` 里的 ``maxRunes``。
STDERR_TAIL_MAX_RUNES = 2000
#: stderr 攒到多少字节就先输出。对应 Go ``execStderrLogger.Write`` 里的 ``maxPending``。
STDERR_PENDING_BYTES = 4096


class ExecError(RuntimeError):
    """``exec`` 动作失败。对应 Go 里这一层的 ``fmt.Errorf``。"""


class ExecStderrTail:
    """只保留最后 N 行 stderr。对应 Go ``execStderrTail``。"""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._lines: list[str] = []
        self._dropped = 0

    def add(self, line: str) -> None:
        """对应 Go ``execStderrTail.Add``。"""
        line = (line or "").strip()
        if not line:
            return
        with self._mu:
            if len(self._lines) >= STDERR_TAIL_MAX_LINES:
                self._lines.pop(0)
                self._lines.append(line)
                self._dropped += 1
                return
            self._lines.append(line)

    def text(self) -> str:
        """对应 Go ``execStderrTail.String``。"""
        with self._mu:
            if not self._lines:
                return ""
            out = "\n".join(self._lines)
            if len(out) > STDERR_TAIL_MAX_RUNES:
                out = out[-STDERR_TAIL_MAX_RUNES:]
            if self._dropped:
                return f"...({self._dropped} earlier stderr lines omitted)\n{out}"
            return out

    def __str__(self) -> str:  # pragma: no cover - 便捷
        return self.text()


class ExecStderrLogger:
    """边收 stderr 边记日志、边存尾巴。对应 Go ``execStderrLogger``。"""

    def __init__(self, module: Any, action: Action, tail: ExecStderrTail) -> None:
        self._mu = threading.RLock()
        self._module = module
        self._action = action
        self._tail = tail
        self._pending = ""

    def feed(self, data: bytes | str) -> int:
        """喂一段 stderr 字节。对应 Go ``execStderrLogger.Write``。"""
        if isinstance(data, bytes):
            text = data.decode("utf-8", "replace")
        else:
            text = data
        with self._mu:
            size = len(text)
            while text:
                index = text.find("\n")
                if index >= 0:
                    self._pending += text[:index]
                    self._emit_locked(self._pending)
                    self._pending = ""
                    text = text[index + 1 :]
                    continue
                self._pending += text
                if len(self._pending.encode("utf-8")) >= STDERR_PENDING_BYTES:
                    self._emit_locked(self._pending)
                    self._pending = ""
                break
            return size

    def flush(self) -> None:
        """对应 Go ``execStderrLogger.Flush``。"""
        with self._mu:
            if not self._pending:
                return
            self._emit_locked(self._pending)
            self._pending = ""

    def _emit_locked(self, line: str) -> None:
        line = line.rstrip("\r").strip()
        if self._tail is not None:
            self._tail.add(line)
        logger = getattr(self._module, "logger", None)
        if line and logger is not None:
            rule = _first_non_empty(
                self._action.source.final_name, self._action.action_name
            )
            logger.info("hook exec stderr", rule=rule, line=line)


async def pump_stderr(stream: Any, logger: ExecStderrLogger) -> None:
    """把子进程 stderr 连续喂给 logger，直到流结束。"""
    if stream is None:
        return
    while True:
        try:
            chunk = await stream.read(4096)
        except (ConnectionResetError, ValueError):
            return
        if not chunk:
            return
        logger.feed(chunk)


def exec_process_error(token: Any, action: Action, err: BaseException | None) -> ExecError:
    """把进程错误翻成 ``ExecError``。对应 Go ``execProcessError``。"""
    context_error = exec_context_error(token, action)
    if context_error is not None:
        return context_error
    if err is not None:
        return ExecError(f"hook plugin exec failed: {err}")
    return ExecError("hook plugin exec failed")


def exec_context_error(token: Any, action: Action) -> ExecError | None:
    """上下文被取消/超时时的错误。对应 Go ``execContextError``。"""
    if token is None:
        return None
    checker = getattr(token, "is_cancelled", None)
    if not callable(checker) or not checker():
        return None
    getter = getattr(token, "error", None)
    error = getter() if callable(getter) else None
    from atri_x.safecontext import CancelledError, DeadlineExceededError

    if isinstance(error, DeadlineExceededError):
        if action.timeout_seconds > 0:
            return ExecError(
                f"hook plugin exec timed out after {action.timeout_seconds}s: "
                "context deadline exceeded"
            )
        return None
    if isinstance(error, CancelledError):
        return ExecError("hook plugin exec canceled: context canceled")
    return None


def with_exec_stderr(err: BaseException | None, stderr: str) -> BaseException | None:
    """给错误补上 stderr 尾巴。对应 Go ``withExecStderr``。"""
    stderr = (stderr or "").strip()
    if err is None or not stderr:
        return err
    return ExecError(f"{err}\nstderr:\n{stderr}")


def exec_cwd(module: Any, action: Action, event: Any, state: State) -> str:
    """解析 ``exec`` 的工作目录。对应 Go ``execCwd``。"""
    base = source_base_dir(action)
    if not base:
        base = (module.opts.config_dir or "").strip()
    cwd = render(action.cwd, event, state).strip()
    if not cwd:
        return base
    if has_strict_dir(action):
        if os.path.isabs(cwd) or os.path.splitdrive(cwd)[0]:
            raise ExecError(
                f"cwd {cwd!r} must be relative inside plugin directory"
            )
        joined = os.path.normpath(os.path.join(base, os.path.normpath(cwd)))
        strict_dir = source_strict_dir(action)
        if not _path_within(strict_dir, joined):
            raise ExecError(f"cwd {cwd!r} escapes plugin directory")
        return joined
    if os.path.isabs(cwd) or not base:
        return cwd
    return os.path.join(base, cwd)


async def kill_process_tree(proc: Any) -> None:
    """杀掉整个进程树。对应 Go ``killExecHookProcessTree``。"""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if pid:
        if os.name == "nt":
            import asyncio

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
            # 只杀插件自己的进程组（插件用 detached_process_kwargs 起了新会话）；
            # 万一调用方漏了，kill_process_group 会拒绝朝宿主组开枪。
            kill_process_group(pid)
    try:
        proc.kill()
    except (ProcessLookupError, RuntimeError):
        pass


def _path_within(base: str, path: str) -> bool:
    base = os.path.normpath(base)
    path = os.path.normpath(path)
    rel = os.path.relpath(path, base)
    if rel == os.curdir:
        return True
    return rel != ".." and not rel.startswith(".." + os.sep)


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""
