"""DSH 出站网关头代理：给内核补上第三方网关要求的请求头。

**为什么需要**：内置内核的 DeepSeek 适配器（``dsh-llm-deepseek``）只会往
``<DEEPSEEK_BASE_URL>/v1/messages`` 发 Anthropic Messages 请求，请求头固定为
``x-api-key`` / ``anthropic-version`` / ``anthropic-beta`` / UA —— 没有"自定义请求头"
入口。而部分 Anthropic 兼容网关（例如 OpenCode Go）要求每个请求都带额外头
（``x-opencode-session``），缺了直接 400 ``MissingSessionID``；内核侧既不能改请求头，
也不该为了某一家网关去改内核。

**做法**：在回环地址上起一个最小反向代理，内核的 ``DEEPSEEK_BASE_URL`` 指向
``http://127.0.0.1:<port>``，代理把请求原样转发到 provider 的 ``base_url``，并补上
``[providers.<name>.extra_headers]`` 里声明的请求头。于是：

- 官方 DeepSeek 等"不需要额外头"的端点**完全不经过代理**（``gateway_spec`` 为空）；
- 要换任何需要自定义头的 Anthropic 兼容网关，只改 provider 配置、不用改代码；
- 纯标准库、只监听回环、不落日志、不在状态里回显头值（只报名字）。

**安全边界**：只绑定 ``127.0.0.1``；请求头透传（含内核的 ``x-api-key``）但保留头
（``Host`` / ``Content-Length`` / 逐跳头）一律由代理自己重算；不缓存、不写盘；
:class:`GatewaySpec` 拒绝非 http(s)、带 query/fragment 或带凭据的 ``upstream``。
"""

from __future__ import annotations

import http.client
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_ANTHROPIC_BASE_URL",
    "GatewaySpec",
    "HeaderProxy",
    "sanitize_headers",
]

#: 内核默认的 Anthropic Messages 端点（provider 没有 base_url 时用它兜底）。
DEFAULT_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

#: 逐跳头：不转发给上游，也不回写给客户端。
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
#: 代理必须自己重算的保留头（Host 由 http.client 按上游主机补）。
_RESERVED = _HOP_BY_HOP | {"host", "content-length", "expect"}

#: 单次读上游的最大块（流式转发时逐块 flush，SSE 才不会被攒住）。
_CHUNK = 65536
#: 上游空闲读超时（秒）；内核自己的 prompt 超时更短，这里只防永久挂住。
_DEFAULT_TIMEOUT = 600.0


def sanitize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """过滤 provider 级自定义请求头：保留头/空名/空值直接丢弃。"""
    out: dict[str, str] = {}
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()
        if not name or not value:
            continue
        if name.lower() in _RESERVED:
            continue
        out[name] = value
    return out


@dataclass(frozen=True)
class GatewaySpec:
    """一次代理转发的目标：上游 Messages 根地址 + 要补的请求头。"""

    #: provider 的 ``base_url``（含协议与主机，可带路径；不带 query/fragment/凭据）。
    upstream: str
    #: 要补到每个上游请求上的头（值不外显；状态里只报名字）。
    headers: Mapping[str, str] = field(default_factory=dict)
    #: 上游 socket 超时（秒）。
    timeout: float = _DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        text = str(self.upstream or "").strip()
        parsed = urlsplit(text)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("网关代理的 upstream 必须是 http(s) 地址")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("网关代理的 upstream 不能带 query/fragment/凭据")
        object.__setattr__(self, "upstream", text.rstrip("/"))
        object.__setattr__(self, "headers", sanitize_headers(self.headers))
        object.__setattr__(self, "timeout", max(1.0, float(self.timeout)))

    @property
    def header_names(self) -> list[str]:
        """要补的请求头名字（排序后；供状态/审计展示）。"""
        return sorted(self.headers)

    @property
    def host(self) -> str:
        """上游主机（状态里只报主机，不报路径）。"""
        return str(urlsplit(self.upstream).hostname or "")


class _ProxyServer(ThreadingHTTPServer):
    """回环上的多线程 HTTP 服务端：连接异常不往 stderr 打堆栈。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: ANN401
        """单个连接出错（客户端断开/上游超时）不应污染日志。"""


def _connection(upstream: str, timeout: float) -> http.client.HTTPConnection:
    """按上游 scheme 建一条短连接（一次请求一条，内核并发不大）。"""
    parsed = urlsplit(upstream)
    host = str(parsed.hostname or "")
    port = parsed.port
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _handler_class(spec: GatewaySpec) -> type[BaseHTTPRequestHandler]:
    """给本次代理生成 handler：闭包持有 :class:`GatewaySpec`，无全局状态。"""
    base_path = urlsplit(spec.upstream).path.rstrip("/")

    class _ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
            """不记访问日志：请求头里有凭据，落日志等于泄漏。"""

        def _forward(self, method: str) -> None:
            body = _read_body(self)
            if body is None:
                return
            conn: http.client.HTTPConnection | None = None
            try:
                conn = _connection(spec.upstream, spec.timeout)
                path = base_path + (self.path or "/")
                conn.request(
                    method, path, body=body or None, headers=_request_headers(self, spec)
                )
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                if conn is not None:
                    conn.close()
                _fail(self, f"网关代理转发失败：{type(exc).__name__}: {exc}")
                return
            try:
                _relay(self, response, method)
            finally:
                conn.close()

    def _do(method: str):  # noqa: ANN202 - 生成 do_* 方法
        def call(handler: _ProxyHandler, _method: str = method) -> None:
            handler._forward(_method)  # noqa: SLF001 - 同一闭包内部

        return call

    for _name in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        setattr(_ProxyHandler, f"do_{_name}", _do(_name))
    return _ProxyHandler


def _read_body(handler: BaseHTTPRequestHandler) -> bytes | None:
    """读请求体；长度非法/读失败返回 ``None``（handler 已回 400）。"""
    raw_length = handler.headers.get("Content-Length")
    if raw_length:
        try:
            length = int(raw_length)
        except ValueError:
            _fail(handler, "无效的 Content-Length", status=400)
            return None
        if length < 0:
            _fail(handler, "无效的 Content-Length", status=400)
            return None
        return handler.rfile.read(length) if length else b""
    if str(handler.headers.get("Transfer-Encoding") or "").lower() == "chunked":
        return _read_chunked(handler)
    return b""


def _read_chunked(handler: BaseHTTPRequestHandler) -> bytes | None:
    """解析 chunked 请求体（内核一般发定长，这里只做兼容）。"""
    chunks: list[bytes] = []
    while True:
        line = handler.rfile.readline(4096)
        if not line:
            return b"".join(chunks)
        try:
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        except ValueError:
            _fail(handler, "无效的 chunked 请求体", status=400)
            return None
        if size <= 0:
            handler.rfile.readline(4096)  # CRLF
            return b"".join(chunks)
        chunks.append(handler.rfile.read(size))
        handler.rfile.readline(4096)


def _request_headers(handler: BaseHTTPRequestHandler, spec: GatewaySpec) -> dict[str, str]:
    """透传客户端请求头（剔除逐跳/保留头），再叠上网关要求的自定义头。"""
    headers: dict[str, str] = {}
    for name, value in handler.headers.items():
        key = str(name or "")
        if not key or key.lower() in _RESERVED:
            continue
        headers[key] = str(value)
    for name, value in spec.headers.items():
        headers[name] = value
    return headers


def _relay(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse, method: str) -> None:
    """把上游响应（含流式）转发给客户端；中途断连只关连接、不抛。"""
    length = response.getheader("Content-Length")
    no_body = method == "HEAD" or response.status in (204, 304)
    try:
        handler.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            if str(name or "").lower() in (_HOP_BY_HOP | {"content-length"}):
                continue
            handler.send_header(name, value)
        if no_body:
            handler.end_headers()
            handler.close_connection = True
            return
        if length is not None:
            handler.send_header("Content-Length", length)
            handler.end_headers()
            _copy_stream(handler, response, int(length))
        else:
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            _copy_chunked(handler, response)
    except (OSError, http.client.HTTPException):
        pass
    finally:
        handler.close_connection = True


def _copy_stream(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse, length: int) -> None:
    """定长响应：按块搬运，写完为止（避免把大响应攒在内存里）。"""
    remaining = length
    while remaining > 0:
        chunk = response.read(min(_CHUNK, remaining))
        if not chunk:
            break
        handler.wfile.write(chunk)
        handler.wfile.flush()
        remaining -= len(chunk)


def _copy_chunked(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse) -> None:
    """未知长度响应（SSE）：逐块读、逐块 flush，客户端能立刻看到增量。"""
    while True:
        chunk = response.read1(_CHUNK)
        if not chunk:
            break
        handler.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        handler.wfile.flush()
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def _fail(handler: BaseHTTPRequestHandler, message: str, *, status: int = 502) -> None:
    """把代理自身故障以 Anthropic 风格的 JSON 错误回给内核（内核按 TRANSPORT 处理）。"""
    payload = json.dumps(
        {"type": "error", "error": {"type": "gateway_proxy_error", "message": message}},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        handler.wfile.flush()
    except OSError:
        pass
    handler.close_connection = True


class HeaderProxy:
    """回环上的最小反向代理：把内核请求转发到上游并补自定义请求头。

    生命周期跟随 :class:`~atri_x.agent.dsh.runtime.DshRuntime`：内核第一次拉起前
    ``start()``（幂等），运行时可随时 ``describe()``；关停时 ``close()``。
    """

    def __init__(self, spec: GatewaySpec) -> None:
        self.spec = spec
        self._lock = threading.Lock()
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._url = ""

    @property
    def url(self) -> str:
        """代理根地址（``http://127.0.0.1:<port>``）；未启动时为空串。"""
        with self._lock:
            return self._url

    @property
    def running(self) -> bool:
        """代理线程是否在跑。"""
        with self._lock:
            server = self._server
        return bool(server is not None and self._thread is not None and self._thread.is_alive())

    def start(self) -> str:
        """启动代理并返回根地址；重复调用返回同一个地址。"""
        with self._lock:
            if self._server is not None:
                return self._url
            handler = _handler_class(self.spec)
            try:
                server = _ProxyServer(("127.0.0.1", 0), handler)
            except OSError as exc:
                raise RuntimeError(f"DSH 网关头代理启动失败：{exc}") from exc
            port = int(server.server_address[1])
            thread = threading.Thread(
                target=server.serve_forever, name="dsh-gateway", daemon=True
            )
            thread.start()
            self._server = server
            self._thread = thread
            self._url = f"http://127.0.0.1:{port}"
            return self._url

    def close(self) -> None:
        """停止代理（幂等；可在任意线程调用）。"""
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            self._url = ""
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - 关停路径不抛
                pass
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def describe(self) -> dict[str, Any]:
        """状态快照：只报上游主机与头名字，**不回显头值**。"""
        return {
            "running": self.running,
            "upstreamHost": self.spec.host,
            "headerNames": self.spec.header_names,
        }
