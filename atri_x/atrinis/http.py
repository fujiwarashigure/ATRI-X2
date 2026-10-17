"""Atrinis 的 HTTP 入口与后台运行时。

对应 Go：``internal/atrinis/http.go``。

对外只有三个端点：

* ``POST /atriena/v2/events`` 与 ``POST /atriena/v3/events`` —— 事件入口，令牌走
  ``Authorization: Bearer <token>``，缺失时回落到 ``X-Atrinis-Token``；
* ``GET|POST /healthz`` —— 存活探针（Go 的 ``HandleFunc`` 不限定方法，故这里
  也不限定）。

``Runtime`` 同时负责后台消费：``workers`` 个协程从有界队列里取 ``llm`` 事件执行，
另一个协程每 30 秒重投一次"待投递汇报"（:meth:`Service.recover_reports`），使
进程重启或发送中途失败都不会丢汇报。

与 Go 的差异（Python 侧的等价物）：

* Go 用 ``net/http``；Python 标准库没有等价的生产级服务器，这里用
  :class:`http.server.ThreadingHTTPServer` + :class:`http.server.BaseHTTPRequestHandler`
  自建 **HTTP/1.1** 端点。请求体大小上限由 ``max_body_bytes`` 自己把关
  （对应 Go 的 ``http.MaxBytesReader``）。
* 处理函数是同步的（``BaseHTTPRequestHandler`` 的线程模型），因此用
  :func:`asyncio.run_coroutine_threadsafe` 把请求桥到事件循环里的异步服务；
  桥的等待上限取 ``write_timeout_seconds``（对应 Go 的 ``WriteTimeout``）。
* 事件路由上的非 POST 请求与未知路径分别返回 405 / 404（与 Go 的 ``ServeMux``
  一致）；服务内部抛出的非请求级异常回 500（Go 会把它们当 400 写出去，这里
  刻意区分"客户端错了"和"我们错了"，是本仓的有意偏差）。

本模块**不使用** ``urllib``：它是服务端，``atri_x/net/httpclient.py`` 是客户端封装。
"""

from __future__ import annotations

import asyncio
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from atri_x.atriena import Response, StatusFailed
from atri_x.atrinis.service import Service, UNAUTHORIZED_MESSAGE
from atri_x.config import AtrinisHTTPConfig
from atri_x.errors import QueueFullError

__all__ = [
    "DEFAULT_ADDR",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_QUEUE_SIZE",
    "DEFAULT_READ_HEADER_TIMEOUT",
    "DEFAULT_READ_TIMEOUT",
    "DEFAULT_WRITE_TIMEOUT",
    "DEFAULT_WORKERS",
    "EVENT_PATHS",
    "HEALTH_PATH",
    "REPORT_RECOVERY_INTERVAL_SECONDS",
    "Runtime",
    "bearer_token",
    "configured_timeout",
    "decode_single_json",
    "enqueue_message",
    "new_runtime",
    "response_status",
]

#: 默认监听地址（与 Go 逐字一致）。
DEFAULT_ADDR = "127.0.0.1:32170"
#: 默认队列长度 / 工作协程数 / 请求体上限。对应 Go 的三个兜底值。
DEFAULT_QUEUE_SIZE = 128
DEFAULT_WORKERS = 1
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
#: 默认超时（秒）。对应 Go 的 ``default*Timeout``。
DEFAULT_READ_HEADER_TIMEOUT = 5
DEFAULT_READ_TIMEOUT = 30
DEFAULT_WRITE_TIMEOUT = 300
DEFAULT_IDLE_TIMEOUT = 60
#: 汇报重投间隔（秒）。对应 Go ``reportRecoveryInterval``。
REPORT_RECOVERY_INTERVAL_SECONDS = 30

HEALTH_PATH = "/healthz"
EVENT_PATHS = ("/atriena/v2/events", "/atriena/v3/events")


def configured_timeout(seconds: int, fallback: int) -> int:
    """``seconds <= 0`` 时用兜底值。对应 Go ``configuredTimeout``（返回秒数）。"""
    return fallback if seconds <= 0 else seconds


def bearer_token(headers: Any) -> str:
    """从请求头取令牌。对应 Go ``bearerToken``。

    ``Authorization`` 存在但**不是** ``Bearer `` 前缀时不算命中，继续看
    ``X-Atrinis-Token``（与 Go 的 fallthrough 一致）。
    """
    value = (headers.get("Authorization") or "").strip()
    if value:
        prefix = "Bearer "
        if value.startswith(prefix):
            return value[len(prefix) :].strip()
    return (headers.get("X-Atrinis-Token") or "").strip()


def decode_single_json(text: str) -> Any:
    """解析**恰好一个** JSON 值。对应 Go ``decodeSingleJSON``。

    尾部还有内容（``{}`` / ``[]`` / ``true`` / 非法片段都算）时抛
    :class:`ValueError`，文案与 Go 的 ``unexpected trailing JSON value`` 一致。
    """
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("EOF")
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(stripped)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if stripped[end:].strip():
        raise ValueError("unexpected trailing JSON value")
    return value


def response_status(resp: Response) -> int:
    """由响应推 HTTP 状态码。对应 Go 的 ``status`` 判定。

    ``accepted`` → 202；``error == "unauthorized"`` → 401；其余失败 → 400。
    """
    if resp.accepted:
        return 202
    if resp.error == UNAUTHORIZED_MESSAGE:
        return 401
    return 400


def enqueue_message(queue: asyncio.Queue, ctx: Any, message: Any) -> None:
    """非阻塞入队。对应 Go ``Runtime.enqueueLLM``（队列满即报错）。

    先看 ``ctx`` 是否已取消（Go 的 ``select`` 里 ``ctx.Done()`` 与入队同权），
    再尝试 ``put_nowait``。
    """
    checker = getattr(ctx, "raise_if_cancelled", None)
    if callable(checker):
        checker()
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull as exc:
        raise QueueFullError("atrinis llm queue is full") from exc


class Runtime:
    """Atrinis 的 HTTP + 后台运行时。对应 Go ``Runtime``。"""

    def __init__(self, cfg: AtrinisHTTPConfig, service: Service) -> None:
        self.cfg = cfg
        self.service = service
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_size or DEFAULT_QUEUE_SIZE)
        self.workers = cfg.workers if cfg.workers > 0 else DEFAULT_WORKERS
        self.max_body_bytes = (
            cfg.max_body_bytes if cfg.max_body_bytes > 0 else DEFAULT_MAX_BODY_BYTES
        )
        self.addr = (cfg.addr or "").strip() or DEFAULT_ADDR
        self.read_header_timeout = configured_timeout(
            cfg.read_header_timeout_seconds, DEFAULT_READ_HEADER_TIMEOUT
        )
        self.read_timeout = configured_timeout(cfg.read_timeout_seconds, DEFAULT_READ_TIMEOUT)
        self.write_timeout = configured_timeout(cfg.write_timeout_seconds, DEFAULT_WRITE_TIMEOUT)
        self.idle_timeout = configured_timeout(cfg.idle_timeout_seconds, DEFAULT_IDLE_TIMEOUT)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        if service is not None:
            service.set_llm_enqueuer(self.enqueue_llm)

    # -- 入队 ---------------------------------------------------------------

    def enqueue_llm(self, ctx: Any, event: Any) -> None:
        """入队一个 LLM 事件。对应 Go ``enqueueLLM``（满即抛 :class:`QueueFullError`）。"""
        enqueue_message(self.queue, ctx, event)

    # -- 生命周期 -----------------------------------------------------------

    async def run(self, ctx: Any = None) -> None:
        """启动 HTTP 服务与后台协程，直到 ``ctx`` 取消或任务被取消。

        对应 Go ``Runtime.Run``：先复位中断的投递，再起 worker 与汇报重投协程，
        最后监听。取消时优雅关停（Go 给 5 秒 ``Shutdown``；Python 用
        :meth:`http.server.HTTPServer.shutdown` + 取消任务）。
        """
        if self.service is not None:
            try:
                self.service.reset_delivering_reports(ctx)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"reset interrupted atrinis reports: {exc}") from exc
        self._loop = asyncio.get_running_loop()
        self._server = self._make_server()
        server = self._server
        serve_task = self._loop.run_in_executor(None, server.serve_forever)
        tasks = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        tasks.append(asyncio.create_task(self._report_worker()))
        try:
            await _wait_cancelled(ctx, serve_task)
        finally:
            await self._loop.run_in_executor(None, server.shutdown)
            server.server_close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(serve_task, return_exceptions=True)
            self._server = None
        return None

    def _make_server(self) -> ThreadingHTTPServer:
        """绑定并构造 HTTP 服务器（绑定失败即抛，等价 Go 的 ListenAndServe 错误）。"""
        host, port = _split_addr(self.addr)

        # 用 type() 而不是 class 语句：class 体内的名字查找不会落到外层函数的局部
        # 变量（LOAD_NAME 只查类命名空间 → 全局 → 内建），``runtime = runtime`` 会
        # NameError。
        handler = type("_AtrinisRequestHandler", (_AtrinisRequestHandler,), {"runtime": self})
        server = ThreadingHTTPServer((host, port), handler)
        server.daemon_threads = True
        return server

    def port(self) -> int:
        """实际监听端口（``addr`` 端口为 0 时用于测试）。"""
        if self._server is None:
            raise RuntimeError("atrinis runtime is not running")
        return int(self._server.server_address[1])

    def is_running(self) -> bool:
        """HTTP 服务是否已经绑定并正在监听。"""
        return self._server is not None

    async def _worker(self) -> None:
        """消费 LLM 队列。对应 Go ``worker``。"""
        while True:
            item = await self.queue.get()
            try:
                if self.service is not None:
                    await self.service.run_llm_event(None, item.event, item.event_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单次失败不终止 worker（同 Go 的 `_ =`）
                pass
            finally:
                self.queue.task_done()

    async def _report_worker(self) -> None:
        """周期性重投待投递汇报。对应 Go ``reportWorker``。"""
        if self.service is None:
            return
        try:
            first_err = await self.service.recover_reports(None, False)
            if first_err is not None:
                self.service.log_warn(
                    "initial atrinis report recovery failed", "error", str(first_err)
                )
        except Exception as exc:  # noqa: BLE001
            self.service.log_warn(
                "initial atrinis report recovery failed", "error", str(exc)
            )
        while True:
            await asyncio.sleep(REPORT_RECOVERY_INTERVAL_SECONDS)
            try:
                err = await self.service.recover_reports(None, False)
                if err is not None:
                    self.service.log_warn("atrinis report retry failed", "error", str(err))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.service.log_warn("atrinis report retry failed", "error", str(exc))

    # -- 请求处理（由 handler 线程调用） ------------------------------------

    def handle_event_body(self, body: bytes, token: str) -> tuple[int, dict[str, Any]]:
        """同步处理一次事件请求，返回 ``(状态码, 响应体)``。

        由 :class:`_AtrinisRequestHandler` 在**线程**里调用；内部把异步服务桥到
        事件循环上等结果。桥的等待上限是 ``write_timeout`` 秒。
        """
        if self._loop is None:
            raise RuntimeError("atrinis runtime is not running")
        try:
            payload = decode_single_json(body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            return 400, Response(
                accepted=False, status=StatusFailed, error=f"invalid json: {exc}"
            ).to_json()
        try:
            from atri_x.atriena import Request

            request = Request.from_json(payload)
        except ValueError as exc:
            return 400, Response(
                accepted=False, status=StatusFailed, error=f"invalid json: {exc}"
            ).to_json()
        future = asyncio.run_coroutine_threadsafe(
            self.service.handle(None, token, request), self._loop
        )
        try:
            resp = future.result(timeout=self.write_timeout)
        except Exception as exc:  # noqa: BLE001 - 服务内部故障 → 500（有意偏差）
            return 500, Response(
                accepted=False, status=StatusFailed, error=f"internal error: {exc}"
            ).to_json()
        return response_status(resp), resp.to_json()

    def log(self, message: str) -> None:
        """把 HTTP 层事件写进 atrinis 日志（未配置 logger 时静默）。"""
        if self.service is None:
            return
        self.service.log_warn(message)

    def address(self) -> tuple[str, int]:
        """配置里的 ``(host, port)`` 二元组。"""
        return _split_addr(self.addr)


async def _wait_cancelled(ctx: Any, serve_task: Any) -> None:
    """等到 ``ctx`` 取消，或 serve 线程自己结束。

    ``ctx`` 为 :class:`atri_x.safecontext.CancelToken` 时用它的 ``wait()``；
    否则退化为"只在任务被取消时退出"。
    """
    waiter: asyncio.Future | None = None
    wait = getattr(ctx, "wait", None)
    if callable(wait):
        waiter = asyncio.ensure_future(wait())
    try:
        watched = [serve_task]
        if waiter is not None:
            watched.append(waiter)
        done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        for item in done:
            if item is serve_task:
                exc = item.exception() if not item.cancelled() else None
                if exc is not None:
                    raise exc
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
    return None


def _split_addr(addr: str) -> tuple[str, int]:
    """``host:port`` → ``(host, port)``；``:port`` → 监听所有接口。"""
    text = (addr or "").strip()
    host, _, port_text = text.rpartition(":")
    if not port_text:
        raise ValueError(f"invalid atrinis http addr {addr!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"invalid atrinis http addr {addr!r}") from exc
    return host, port


class _AtrinisRequestHandler(BaseHTTPRequestHandler):
    """Atrinis 的 HTTP/1.1 处理器。对应 Go 的 ``ServeMux`` + handler 组合。"""

    protocol_version = "HTTP/1.1"
    server_version = "ATRI-X-Atrinis/1"
    sys_version = ""
    runtime: Runtime

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        path = (self.path or "").split("?", 1)[0]
        if path == HEALTH_PATH:
            self._write_json(200, {"ok": True})
            return
        if path not in EVENT_PATHS:
            self._write_json(
                404,
                Response(
                    accepted=False, status=StatusFailed, error="not found"
                ).to_json(),
            )
            return
        if self.command != "POST":
            self._write_json(
                405,
                Response(
                    accepted=False, status=StatusFailed, error="method not allowed"
                ).to_json(),
            )
            return
        body, too_large = self._read_body()
        if too_large:
            self._write_json(
                400,
                Response(
                    accepted=False,
                    status=StatusFailed,
                    error="invalid json: request body too large",
                ).to_json(),
            )
            return
        status, payload = self.runtime.handle_event_body(body, bearer_token(self.headers))
        self._write_json(status, payload)

    def _read_body(self) -> tuple[bytes, bool]:
        """按 ``Content-Length`` 读请求体；超限返回 ``(b"", True)``。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        limit = self.runtime.max_body_bytes
        if length > limit:
            return b"", True
        if length <= 0:
            return b"", False
        body = self.rfile.read(length)
        if len(body) > limit:
            return b"", True
        return body, False

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        """把访问日志交给 atrinis logger（默认实现会写 stderr，太吵）。"""
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.log(format % args)

    def handle_one_request(self) -> None:
        """单次请求处理；连接级异常（含超时）静默关闭，与 Go 的服务器行为一致。"""
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError, ConnectionError, OSError):
            self.close_connection = True


def new_runtime(cfg: AtrinisHTTPConfig, service: Service) -> Runtime:
    """构造运行时并接管服务的 LLM 入队。对应 Go ``NewRuntime``。"""
    return Runtime(cfg, service)
