"""独立的健康 HTTP 服务端。

对应 Go：``internal/health/server.go``。

暴露 ``/live`` 与 ``/ready``；``/healthz`` 只在有 ops token 保护、或调用方显式
选择 ``allow_unauthenticated_extra_handlers`` 时才注册（**安全默认**：没 token
就不注册，而不是默默泄漏 checker 错误）。

JSON 形状刻意逐键对齐 Go 的 struct tag，包括一个容易漏掉的 Go 细节：
``time.Time`` 是结构体，``omitempty`` 对它**无效**，所以非指针时间字段在零值时
仍会输出 ``"0001-01-01T00:00:00Z"``；只有 ``*time.Time`` 才会被省略。watchdog
与 healthcheck 脚本会解析这些字段，形状必须一致。
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json as _json
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from atri_x.health.state import Snapshot, State
from atri_x.redact import secrets as redact_secrets

#: 对应 Go ``defaultReadHeaderTimeout``。
DEFAULT_READ_HEADER_TIMEOUT_SECONDS = 5.0

#: Go 的 ``time.Time{}`` 序列化结果。
GO_ZERO_TIME = "0001-01-01T00:00:00Z"


@dataclass
class ServerOptions:
    """对应 Go ``ServerOptions``。"""

    addr: str = ""
    state: State | None = None
    checkers: list[Any] = field(default_factory=list)
    #: 路径 → 处理函数 ``fn(ctx, request) -> (status, body_bytes_or_str)``
    extra_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    extra_handler_token: str = ""
    allow_unauthenticated_extra_handlers: bool = False
    logger: Any = None
    read_header_timeout: float = 0.0


@dataclass
class HTTPResponse:
    """一个已经成型、可被测试直接断言的响应。"""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        return _json.loads(self.body.decode("utf-8"))


@dataclass
class _RequestView:
    """交给 Go 风格处理函数的请求视图（``r *http.Request`` 的最小等价物）。"""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    #: 查询参数（``r.URL.Query()`` 的等价物，取每个键的第一个值）。
    query: dict[str, str] = field(default_factory=dict)


class _ByteSink:
    """``wfile`` 的最小替身：接口与 ``BaseHTTPRequestHandler.wfile`` 一致。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    def flush(self) -> None:
        return


class _ResponseCaptor:
    """把 Go 风格 ``handler(w, r)`` 的写入捕获成 :class:`HTTPResponse`。

    真实事故（进程级冒烟抓到）：``atri_x/app/ops_health.py`` 的 ``LazyJSONProvider`` /
    ``LazyHTTPHandler`` 是 Go 形状的 ``http.Handler``（``(w, r)``，自己写响应），而
    :meth:`Server.dispatch` 只按 ``handler(ctx)`` 调 —— ``/metrics``、``/tasks`` 等
    端点一访问就 ``TypeError: __call__() missing 1 required positional argument``，
    线程里抛异常、客户端拿到断开的连接。
    """

    def __init__(self, request: _RequestView) -> None:
        self.request = request
        self.status = 200
        self.headers: dict[str, str] = {}
        self.wfile = _ByteSink()

    def send_response(self, status: int, message: str | None = None) -> None:
        self.status = int(status)

    def send_header(self, key: str, value: Any) -> None:
        self.headers[str(key)] = str(value)

    def end_headers(self) -> None:
        return

    def to_response(self) -> HTTPResponse:
        return HTTPResponse(
            status=self.status,
            headers=dict(self.headers),
            body=b"".join(self.wfile.chunks),
        )


def _accepts_request(handler: Callable[..., Any]) -> bool:
    """处理函数是否要求第二个 ``request`` 实参（Go 风格 ``(w, r)``）。

    ``LazyJSONProvider`` / ``LazyHTTPHandler`` 这类可调用对象按 ``(w, r)`` 定义，
    所以要透过 ``__call__`` 看真实签名；``*args`` 与签名不可知的按 Go 风格处理。
    """
    target: Any = handler
    if not (inspect.isfunction(handler) or inspect.ismethod(handler)):
        target = getattr(handler, "__call__", handler)
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return True
    parameters = list(signature.parameters.values())
    if any(p.kind == p.VAR_POSITIONAL for p in parameters):
        return True
    positional = [
        p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def _consume_awaitable(result: Any) -> Any:
    """驱动处理器返回的协程并返回其最终值；不是协程就原样返回。

    真实事故（第三轮对抗性验证抓到，并用故障注入反证过）：``extra_handlers`` 直接收下
    ``async`` 处理器时协程被丢弃，两个分支的症状不同 ——

    - Go 风格 ``async def handler(w, r)``：响应体**恒空**（``200`` + ``b""``）；
    - 函数式 ``async def handler(ctx)``：``None`` 被当作返回值 → ``200`` + ``null``。

    两种情况下进程里都只剩一条 ``RuntimeWarning: coroutine ... was never awaited``。
    生产装配里这类处理器通常被 :class:`atri_x.app.ops_health.LazyHTTPHandler` 包住
    （同步 ``__call__`` 内部 ``asyncio.run``），所以真实启动路径上看不出来。

    这里补上同样的履约语义：没有运行中的事件循环就用 ``asyncio.run`` 跑完，有的话与
    ``LazyHTTPHandler`` 保持一致地直接报错（而不是把请求挂死）。
    """
    if result is None or not hasattr(result, "__await__"):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    raise RuntimeError(
        "health extra handler is async and cannot be awaited inside a running loop"
    )


def _iso(value: datetime | None) -> str:
    """按 Go ``time.RFC3339Nano`` 的形状输出 UTC 时间。

    Go 在 ``time.Time`` 零值上输出 ``0001-01-01T00:00:00Z``；Python 没有
    "零值 datetime"，所以未设置一律映射到 :data:`GO_ZERO_TIME`。
    """
    if value is None:
        return GO_ZERO_TIME
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    text = value.astimezone(timezone.utc).isoformat()
    # Go 输出 Z 而不是 +00:00；并把微秒写成 9 位纳秒
    text = text.replace("+00:00", "Z")
    if "." in text:
        head, _, frac = text.partition(".")
        frac = frac.rstrip("Z")
        text = f"{head}.{frac.ljust(9, '0')[:9]}Z"
    return text


def marshal_model_status(status: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"provider": status.provider, "status": status.status}
    if status.circuit:
        out["circuit"] = status.circuit
    if status.last_error:
        out["last_error"] = status.last_error
    if status.last_failure_at is not None:
        out["last_failure_at"] = _iso(status.last_failure_at)
    if status.last_success_at is not None:
        out["last_success_at"] = _iso(status.last_success_at)
    return out


def marshal_platform_status(status: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": status.name,
        "connected": status.connected,
        "connect_count": status.connect_count,
    }
    if status.last_connected_at is not None:
        out["last_connected_at"] = _iso(status.last_connected_at)
    if status.last_disconnected_at is not None:
        out["last_disconnected_at"] = _iso(status.last_disconnected_at)
    if status.last_error:
        out["last_error"] = status.last_error
    # Python 侧新增：把"传输层连上"与"QQ 已登录"分开（只在有信息时输出）。
    backend = str(getattr(status, "backend", "") or "")
    if backend:
        out["backend"] = backend
    if getattr(status, "login_checked_at", None) is not None:
        out["login_ok"] = bool(getattr(status, "login_ok", False))
        out["login_checked_at"] = _iso(status.login_checked_at)
    self_id = str(getattr(status, "self_id", "") or "")
    if self_id:
        out["self_id"] = self_id
    nickname = str(getattr(status, "nickname", "") or "")
    if nickname:
        out["nickname"] = nickname
    app_name = str(getattr(status, "app_name", "") or "")
    if app_name:
        out["app_name"] = app_name
    return out


def marshal_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    """按 Go struct tag 的形状序列化（含零值时间的处理）。"""
    out: dict[str, Any] = {
        "status": snapshot.status,
        "live": snapshot.live,
        "ready": snapshot.ready,
        "degraded": snapshot.degraded,
        "scheduler_live": snapshot.scheduler_live,
        "scheduler_known": snapshot.scheduler_known,
        # last_heartbeat 在 Go 里是非指针 time.Time，零值也会被输出
        "last_heartbeat": _iso(snapshot.last_heartbeat),
        "started_at": _iso(snapshot.started_at),
        "uptime_seconds": snapshot.uptime_seconds,
        "messages_ok": snapshot.messages_ok,
        "messages_failed": snapshot.messages_failed,
    }
    if snapshot.version:
        out["version"] = snapshot.version
    if snapshot.last_message_at is not None:
        out["last_message_at"] = _iso(snapshot.last_message_at)
    if snapshot.checks:
        out["checks"] = dict(snapshot.checks)
    if snapshot.models:
        out["models"] = [marshal_model_status(m) for m in snapshot.models]
    if snapshot.platforms:
        out["platforms"] = [marshal_platform_status(p) for p in snapshot.platforms]
    if snapshot.last_restart_reason:
        out["last_restart_reason"] = snapshot.last_restart_reason
    return out


class Server:
    """对应 Go ``Server``。"""

    def __init__(self, opts: ServerOptions) -> None:
        addr = (opts.addr or "").strip()
        if not addr:
            raise ValueError("health server address is empty")
        if opts.state is None:
            raise ValueError("health server state is nil")
        timeout = opts.read_header_timeout
        if timeout <= 0:
            timeout = DEFAULT_READ_HEADER_TIMEOUT_SECONDS
        self._state = opts.state
        self._checkers = list(opts.checkers)
        self._token = (opts.extra_handler_token or "").strip()
        self._allow_unauth = opts.allow_unauthenticated_extra_handlers
        self._logger = opts.logger
        self._timeout = timeout
        self._extra: dict[str, Callable[..., Any]] = {}
        for path, handler in (opts.extra_handlers or {}).items():
            if handler is None:
                continue
            if not path.startswith("/"):
                raise ValueError(f'health extra handler path must start with /: "{path}"')
            self._extra[path] = handler

        self._addr = addr
        self._host, self._port = _split_addr(addr)
        self._bound_port: int | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 只读 ---------------------------------------------------------------
    @property
    def state(self) -> State:
        return self._state

    @property
    def address(self) -> str:
        return self._addr

    @property
    def bound_port(self) -> int | None:
        """实际绑定的端口（``addr`` 用 0 时由内核分配）。"""
        return self._bound_port

    @property
    def extra_handler_token(self) -> str:
        return self._token

    # -- 路由（纯函数，便于单测；等价于 Go 的 `Handler()`） -----------------
    def registered_paths(self) -> list[str]:
        """实际注册的路径。``/healthz`` 与 extra handler 受 token 策略影响。"""
        paths = ["/live", "/ready"]
        if self._token or self._allow_unauth:
            paths.append("/healthz")
        for path in self._extra:
            if self._token or self._allow_unauth:
                paths.append(path)
        return paths

    def dispatch(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        ctx: Any = None,
    ) -> HTTPResponse:
        """处理一次请求。返回已成型响应，不做任何 I/O。

        ``path`` 允许带查询串（HTTP 层会原样传进来）：路由只看路径部分，查询参数
        放进 :class:`_RequestView` 的 ``query``（``r.URL.Query()`` 的等价物）。
        历史事故：这里以前把查询串丢掉，``/plugins/memory?platform=cli&scope_id=local``
        的平台/作用域过滤**静默失效**（端点照回 200，但永远是空 scope 的统计）。

        判定顺序是 **路由 → 鉴权 → 方法**，与 Go 的 ``ServeMux`` + 中间件一致
        （历史事故：以前先判方法，``POST /nope`` 会回 405 而 Go 回 404）。
        """
        header_map = {k.lower(): v for k, v in (headers or {}).items()}

        raw_path = path or ""
        path_only, _, raw_query = raw_path.partition("?")
        query = {
            key: values[0] if values else ""
            for key, values in urllib.parse.parse_qs(
                raw_query, keep_blank_values=True
            ).items()
        }

        # 路由 → 鉴权 → 方法：顺序逐字对齐 Go 的 ``ServeMux`` + 中间件。
        # 未注册路径无论用什么方法都是 404（``POST /nope`` 不是 405）；受 token
        # 保护的路径先判 401（``requireExtraHandlerToken`` 在方法判定之前）；
        # 最后才由处理函数自己的 ``allowGet`` 回 405 + ``Allow: GET``。
        handler: Callable[..., Any] | None = None
        protected = False
        if path_only == "/live":
            target = "live"
        elif path_only == "/ready":
            target = "ready"
        elif path_only == "/healthz":
            if not (self._token or self._allow_unauth):
                return _json_response(404, {"error": "not found"})
            target = "healthz"
            protected = True
        else:
            handler = self._extra.get(path_only)
            if handler is None or not (self._token or self._allow_unauth):
                return _json_response(404, {"error": "not found"})
            target = "extra"
            protected = True

        if protected and not self._authorized(header_map):
            return _json_response(401, {"error": "unauthorized"})

        if method != "GET":
            response = _json_response(405, {"error": "method not allowed"})
            response.headers["Allow"] = "GET"
            return response

        if target == "live":
            return self._handle_live()
        if target == "ready":
            return self._handle_ready(ctx)
        if target == "healthz":
            return self._handle_healthz(ctx)

        if handler is None:  # pragma: no cover - 路由已保证非空
            return _json_response(404, {"error": "not found"})
        request = _RequestView(
            method=method, path=path_only, headers=dict(header_map), query=query
        )
        if _accepts_request(handler):
            # Go 风格 ``http.Handler``：自己写响应，这里把写入捕获成 HTTPResponse。
            captor = _ResponseCaptor(request)
            _consume_awaitable(handler(captor, request))
            return captor.to_response()
        result = _consume_awaitable(handler(ctx))
        if isinstance(result, HTTPResponse):
            return result
        if isinstance(result, tuple):
            status, body = result[0], result[1]
            return _json_response(int(status), body)
        return _json_response(200, result)

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        """绑定地址并在后台线程开始服务。对应 Go ``Start``。"""
        server = self._build_httpd()
        self._httpd = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="atri-x-health", daemon=True
        )
        self._thread.start()

    def close(self, ctx: Any = None) -> None:
        """优雅关闭。对应 Go ``Close``。"""
        if self._httpd is None:
            return None
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._httpd = None
        self._thread = None

    # -- 内部 ---------------------------------------------------------------
    def _build_httpd(self) -> ThreadingHTTPServer:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "atri-x-health"

            def log_message(self, *args: Any) -> None:  # 静音：日志由 logger 统一管
                return

            def version_string(self) -> str:
                """``Server`` 响应头只留服务名。

                Go 的 ``net/http`` 默认不写 ``Server`` 头，Python 的
                ``BaseHTTPRequestHandler`` 必写且默认形如
                ``atri-x-health Python/3.14.0`` —— 白送解释器版本。这里去掉版本号。
                """
                return self.server_version

            def __getattr__(self, name: str) -> Any:
                """把任意 HTTP 动词路由进 :meth:`_serve`。

                Go 的 ``ServeMux`` 对**已注册路径**上的任何方法都会进处理函数，
                由 ``allowGet`` 回 405 + ``Allow: GET``；Python 的
                ``BaseHTTPRequestHandler`` 只认显式定义的 ``do_XXX``，其余一律
                ``501`` + HTML（第三轮验证抓到的偏差）。这里让 ``do_GET`` 之外的
                名字也解析得到，动词原样交给 :meth:`dispatch` 判定。
                """
                if name.startswith("do_"):
                    verb = name[3:].upper()
                    return lambda: self._serve(verb)
                raise AttributeError(name)

            def _serve(self, method: str) -> None:
                # 原样传 path（含查询串）：dispatch 负责拆路径与查询参数，
                # 否则 /plugins/* 的 platform / scope_id 过滤会静默失效。
                response = outer.dispatch(method, self.path, dict(self.headers))
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                # HEAD 不写实体（Go 的 net/http 对 HEAD 响应同样丢弃 body），
                # Content-Length 仍按 GET 的形状给出。
                if response.body and method != "HEAD":
                    self.wfile.write(response.body)

        httpd = ThreadingHTTPServer(
            (self._host, self._port), _Handler
        )
        httpd.daemon_threads = True
        self._bound_port = httpd.server_address[1]
        return httpd

    def _authorized(self, header_map: Mapping[str, str]) -> bool:
        token = _bearer_token(header_map.get("authorization", ""))
        if not token:
            token = (header_map.get("x-atrix-ops-token", "") or "").strip()
        # 常量时间比较，避免用响应时间试探 token
        return hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8"))

    def _handle_live(self) -> HTTPResponse:
        snapshot = self._state.snapshot()
        body: dict[str, Any] = {
            "status": "live" if snapshot.live else "not_live",
            "uptime_seconds": snapshot.uptime_seconds,
            "last_heartbeat": _iso(snapshot.last_heartbeat),
        }
        if snapshot.version:
            body["version"] = snapshot.version
        return _json_response(200 if snapshot.live else 503, body)

    def _handle_ready(self, ctx: Any) -> HTTPResponse:
        ready, checks = self.evaluate_ready(ctx)
        body = {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "checks": checks,
        }
        return _json_response(200 if ready else 503, body)

    def _handle_healthz(self, ctx: Any) -> HTTPResponse:
        snapshot = self.snapshot(ctx)
        status = 200
        if snapshot.status in ("not_live", "not_ready"):
            status = 503
        return _json_response(status, marshal_snapshot(snapshot))

    def snapshot(self, ctx: Any = None) -> Snapshot:
        """``/healthz`` 用的聚合视图。对应 Go ``Server.snapshot``。"""
        snapshot = self._state.snapshot()
        ready, checks = self.evaluate_ready(ctx)
        snapshot.ready = ready
        merged = dict(snapshot.checks or {})
        merged.update(checks)
        snapshot.checks = merged
        if not snapshot.live:
            snapshot.status = "not_live"
        elif not ready:
            snapshot.status = "not_ready"
        elif snapshot.degraded:
            snapshot.status = "degraded"
        else:
            snapshot.status = "ok"
        return snapshot

    def evaluate_ready(self, ctx: Any = None) -> tuple[bool, dict[str, str]]:
        """readiness 判定。对应 Go ``Server.evaluateReady``。

        注意：**平台连通性与模型健康不参与** —— 这是文档承诺的语义。
        """
        checks: dict[str, str] = {}
        ready = True
        if self._state.shutting_down():
            ready = False
            checks["process"] = "shutting down"
        elif not self._state.is_ready():
            ready = False
            checks["process"] = "not ready"
        else:
            checks["process"] = "ready"
        if self._state.scheduler_known():
            if self._state.scheduler_live():
                checks["scheduler"] = "ok"
            else:
                ready = False
                checks["scheduler"] = "heartbeat stale"
        else:
            checks["scheduler"] = "not started"
        for checker in self._checkers:
            if checker is None:
                continue
            name = checker.name()
            try:
                checker.check(ctx)
            except BaseException as exc:  # noqa: BLE001 - 检查失败就是 not ready
                ready = False
                checks[name] = redact_secrets(str(exc))
                continue
            checks[name] = "ok"
        return ready, checks


def _json_response(status: int, body: Any) -> HTTPResponse:
    # Go 的 json.Encoder.Encode 会追加一个换行，保持一致
    payload = (_json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    return HTTPResponse(
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        },
        body=payload,
    )


def _bearer_token(header: str) -> str:
    header = (header or "").strip()
    prefix = "bearer "
    if len(header) < len(prefix) or header[: len(prefix)].lower() != prefix:
        return ""
    return header[len(prefix):].strip()


def _split_addr(addr: str) -> tuple[str, int]:
    """解析 ``host:port``；``:8080`` 视为监听所有接口，与 Go ``net.Listen`` 一致。"""
    text = addr.strip()
    if text.startswith("["):  # IPv6 字面量
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port or "0")
    host, sep, port = text.rpartition(":")
    if not sep:
        return "", int(text or "0")
    return host, int(port or "0")


def new_server(opts: ServerOptions) -> Server:
    """对应 Go ``NewServer``（错误改为抛 ``ValueError``）。"""
    return Server(opts)


__all__ = [
    "DEFAULT_READ_HEADER_TIMEOUT_SECONDS",
    "GO_ZERO_TIME",
    "HTTPResponse",
    "Server",
    "ServerOptions",
    "marshal_model_status",
    "marshal_platform_status",
    "marshal_snapshot",
    "new_server",
]
