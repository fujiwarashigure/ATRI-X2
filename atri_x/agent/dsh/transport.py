"""``atri_x.agent.dsh.transport``：ACP 子进程的 NDJSON stdio JSON-RPC 传输。

职责：拉起 ``dsh --profile acp``、按行收发 JSON-RPC 2.0 帧、维护未决请求、
把服务端请求交给处理器、收集 stderr 尾部、处理超时与进程退出。本模块只做
管道与帧，不懂 ACP 语义（会话、prompt、权限策略）——这些在
:mod:`atri_x.agent.dsh.client`。

帧格式：stdin/stdout 上**每行一个** UTF-8 JSON 对象（NDJSON）。DSH 的 stdout
只承载协议流量，诊断日志走 stderr，因此本模块把 stderr 尾部留在内存里供报错。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections import deque
from typing import Any, Callable, Mapping, Sequence

from atri_x.agent.dsh.types import (
    AcpError,
    AcpProcessError,
    AcpProtocolError,
    AcpTimeoutError,
    MAX_FRAME_BYTES,
    as_code,
    as_text,
)
from atri_x.redact import secrets

__all__ = ["JsonRpcStdio"]

#: 出错信息里保留的 stderr 尾部行数。
STDERR_TAIL_LINES = 200


class _Pending:
    """一次出站请求的等待槽位（由读线程填结果）。"""

    __slots__ = ("error", "event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: AcpError | None = None

    def done(self, result: Any) -> None:
        """填充成功结果并唤醒等待方。"""
        self.result = result
        self.event.set()

    def fail(self, error: AcpError) -> None:
        """填充失败并唤醒等待方。"""
        self.error = error
        self.event.set()


class JsonRpcStdio:
    """一行一帧的 JSON-RPC 2.0 客户端传输（线程安全）。

    :param command: 启动命令（已拆好的 argv）。
    :param env: 叠加到子进程环境上的变量；``isolate_env=True`` 时它就是**完整**环境。
    :param isolate_env: 只用 ``env``（不再继承 ``os.environ``）。用于调用方已经把
        环境收敛过（过滤凭据、只放回明确注入的那一项）的场景，默认 ``False`` 保持
        「继承 + 叠加」的老语义。
    :param on_notification: 服务端通知处理器，参数 ``(method, params)``；在读线程调用。
    :param on_request: 服务端请求处理器，返回 ``result`` 字典；``None`` 表示用
        ``-32601 method not found`` 应答。在读线程调用，必须快速返回。
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout: float = 30.0,
        on_notification: Callable[[str, Mapping[str, Any]], None] | None = None,
        on_request: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        stderr_tail_lines: int = STDERR_TAIL_LINES,
        isolate_env: bool = False,
    ) -> None:
        parts = [str(item) for item in command]
        if not parts:
            raise AcpError("dsh acp command is empty")
        self._command = parts
        self._cwd = cwd
        self._env = dict(env or {})
        self._isolate_env = bool(isolate_env)
        self._request_timeout = float(request_timeout)
        self._on_notification = on_notification
        self._on_request = on_request
        self._stderr_tail: deque[str] = deque(maxlen=max(1, int(stderr_tail_lines)))

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._closed = False
        self._stopped = False

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """拉起子进程；已在运行则直接返回。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
        env = dict(self._env) if self._isolate_env else {**dict(os.environ), **self._env}
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                env=env,
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise AcpProcessError(f"dsh acp command not found: {self._command[0]}") from exc
        except OSError as exc:
            raise AcpProcessError(f"failed to spawn dsh acp server: {exc}") from exc
        with self._lock:
            self._proc = proc
            self._closed = False
            self._stopped = False
        self._reader = threading.Thread(target=self._read_loop, name="dsh-acp-reader", daemon=True)
        self._stderr_reader = threading.Thread(target=self._stderr_loop, name="dsh-acp-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def stop(self) -> None:
        """终止子进程并回收线程（幂等）。"""
        with self._lock:
            self._closed = True
        self._terminate()
        self._close_streams()
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)

    @property
    def alive(self) -> bool:
        """子进程是否仍在运行。"""
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        """子进程退出码；还在跑或从未启动给 ``None``。"""
        proc = self._proc
        return None if proc is None else proc.poll()

    def stderr_tail(self) -> str:
        """DSH 诊断日志的尾部（报错时带出去）。"""
        with self._lock:
            return "\n".join(self._stderr_tail)

    # ------------------------------------------------------------------ 收发

    def request(self, method: str, params: Mapping[str, Any], timeout: float | None = None) -> Any:
        """发一条请求并等响应；超时抛 :class:`AcpTimeoutError`。"""
        with self._lock:
            if self._stopped or self._proc is None:
                raise AcpProcessError(f"dsh acp server is not running (method {method})")
            self._next_id += 1
            request_id = self._next_id
            slot = _Pending()
            self._pending[request_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        except AcpError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        limit = self._request_timeout if timeout is None else float(timeout)
        if not slot.event.wait(limit):
            with self._lock:
                self._pending.pop(request_id, None)
            raise AcpTimeoutError(f"{method} timed out after {limit:.1f}s")
        if slot.error is not None:
            raise slot.error
        return slot.result

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        """发一条通知（无响应）。"""
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _send(self, payload: Mapping[str, Any]) -> None:
        """写一帧；管道已断按进程错误处理。"""
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME_BYTES:
            raise AcpProtocolError(f"outbound acp frame too large: {len(data)} bytes")
        proc = self._proc
        with self._write_lock:
            if proc is None or proc.stdin is None:
                raise AcpProcessError("dsh acp server is not running")
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AcpProcessError(f"dsh acp server pipe closed: {exc}") from exc

    # ------------------------------------------------------------------ 读线程

    def _stderr_loop(self) -> None:
        """消费子进程 stderr，保留尾部若干行，避免管道写满。"""
        proc = self._proc
        stream = None if proc is None else proc.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    with self._lock:
                        self._stderr_tail.append(line)
        except (OSError, ValueError):
            return

    def _read_loop(self) -> None:
        """按行解析 NDJSON 帧并分发：响应填槽位、通知回调、请求就地应答。"""
        proc = self._proc
        stream = None if proc is None else proc.stdout
        try:
            if stream is not None:
                for raw in stream:
                    if self._closed:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    if len(line) > MAX_FRAME_BYTES:
                        self._fail_pending(AcpProtocolError(f"acp frame too large: {len(line)} bytes"))
                        break
                    try:
                        payload = json.loads(line.decode("utf-8", "replace"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(payload, dict):
                        self._dispatch(payload)
        except (OSError, ValueError):
            pass
        finally:
            self._on_reader_exit()

    def _on_reader_exit(self) -> None:
        """读线程结束意味着子进程不能再服务：所有等待中的请求统一失败。"""
        proc = self._proc
        code = None if proc is None else proc.poll()
        # stderr 是内核自己的诊断日志，它的 startup 横幅明确写着
        # "may contain configuration or credential values"，带出去之前先脱敏。
        tail = secrets(self.stderr_tail())
        self._fail_pending(
            AcpProcessError(f"dsh acp server exited (exit code {code}); stderr tail: {tail}")
        )
        with self._lock:
            self._stopped = True

    def _fail_pending(self, error: AcpError) -> None:
        """把当前所有未决请求标记为失败。"""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot.fail(error)

    def _dispatch(self, payload: Mapping[str, Any]) -> None:
        """按 JSON-RPC 形状分发一帧。"""
        method = payload.get("method")
        if method is not None:
            request_id = payload.get("id")
            params = payload.get("params")
            mapping = params if isinstance(params, Mapping) else {}
            if request_id is None:
                if self._on_notification is not None:
                    self._on_notification(as_text(method), mapping)
            else:
                self._answer(request_id, as_text(method), mapping)
            return
        request_id = payload.get("id")
        if not isinstance(request_id, int):
            return
        with self._lock:
            slot = self._pending.pop(request_id, None)
        if slot is None:
            return
        error = payload.get("error")
        if isinstance(error, Mapping):
            slot.fail(
                AcpError(
                    as_text(error.get("message")) or "acp error",
                    code=as_code(error.get("code")),
                    data=error.get("data"),
                )
            )
            return
        result = payload.get("result")
        slot.done(result if result is not None else {})

    def _answer(self, request_id: Any, method: str, params: Mapping[str, Any]) -> None:
        """应答服务端请求；处理器返回 ``None`` 或抛异常时一律 method not found。"""
        result: Mapping[str, Any] | None = None
        if self._on_request is not None:
            try:
                result = self._on_request(method, params)
            except Exception:  # noqa: BLE001 - 处理器异常不能弄死读线程
                result = None
        if result is None:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"method not supported by client: {method}"},
                }
            )
            return
        self._send({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})

    # ------------------------------------------------------------------ 终止

    def _terminate(self) -> None:
        """先 SIGTERM / terminate，超时再强杀；POSIX 上连进程组一起收。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        self._close_streams()
        with self._lock:
            self._stopped = True

    def _close_streams(self) -> None:
        """关闭三条管道；进程已退出时 :meth:`_terminate` 会提前返回，靠这里兜底。"""
        proc = self._proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
