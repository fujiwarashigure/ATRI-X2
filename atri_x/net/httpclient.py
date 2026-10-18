"""HTTP 客户端：对应 Go 的 ``net/http`` 用法。

设计要点：

* 同步 ``http.client`` 在 ``asyncio.to_thread`` 里执行，避免引入第三方
  aiohttp/httpx；连接复用通过 ``http.client`` 的连接本身不可跨线程，
  因此每次请求新建连接（与 Go 侧 ``DisableKeepAlives`` 等价，本项目
  并发上限只有个位数，代价可忽略）。
* 流式读取（SSE）走独立的读取线程 + ``asyncio.Queue``，可被取消。
* 代理：``http`` 走绝对 URI，``https`` 走 CONNECT 隧道，覆盖本项目
  Telegram / LLM 在受限网络下的常见部署方式。
"""

from __future__ import annotations

import asyncio
import http.client
import json as _json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Sequence

DEFAULT_TIMEOUT = 30.0
DEFAULT_USER_AGENT = "atri-x/0.9.2 (python)"


class HTTPError(Exception):
    """传输层错误（连接失败、超时、协议错误）。"""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass
class Response:
    """缓冲响应。"""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self, encoding: str | None = None) -> str:
        enc = encoding or _charset_of(self.headers) or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body:
            return None
        return _json.loads(self.body.decode("utf-8", errors="replace"))

    def header(self, name: str, default: str = "") -> str:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return default


def _charset_of(headers: Mapping[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "content-type" and "charset=" in value.lower():
            return value.lower().split("charset=", 1)[1].split(";")[0].strip()
    return ""


@dataclass
class RetryPolicy:
    """重试策略。对应 Go 适配器里各自实现的退避逻辑，这里提供统一版本。"""

    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    # 返回 True 表示该状态码可重试
    retry_on_status: Callable[[int], bool] = field(
        default_factory=lambda: lambda status: status in (408, 409, 425, 429) or status >= 500
    )

    def delay_for(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))


class HTTPClient:
    """HTTP/1.1 客户端。

    ``cancel`` 可选，接受任何带 ``is_cancelled()`` 的对象（见
    :mod:`atri_x.safecontext`）或 ``asyncio.Event``。
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        *,
        proxy: str = "",
        verify_tls: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers: dict[str, str] = {"User-Agent": user_agent}
        if headers:
            self.headers.update({k: v for k, v in headers.items() if v is not None})
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        self.verify_tls = verify_tls

    # -- public -------------------------------------------------------------
    def resolve(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    async def get(self, path: str, **kw: Any) -> Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Response:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> Response:
        return await self.request("PUT", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Response:
        return await self.request("DELETE", path, **kw)

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | None = None,
        json: Any = None,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        timeout: float | None = None,
        retry: RetryPolicy | None = None,
        retry_notifier: Callable[[Any, Any], Any] | None = None,
        cancel: Any = None,
    ) -> Response:
        url = self.resolve(path)
        if params:
            url = _with_query(url, params)
        payload: bytes | None
        merged = dict(self.headers)
        if headers:
            merged.update({k: v for k, v in headers.items() if v is not None})
        if json is not None:
            payload = _json.dumps(json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body

        policy = retry or RetryPolicy(max_retries=0)
        attempt = 0
        while True:
            attempt += 1
            _raise_if_cancelled(cancel)
            try:
                resp = await asyncio.to_thread(
                    self._blocking_request, method, url, merged, payload, timeout or self.timeout
                )
            except HTTPError:
                if attempt > policy.max_retries:
                    raise
                delay = policy.delay_for(attempt)
                _notify_retry(retry_notifier, attempt, policy.max_retries, delay, None)
                await _sleep_cancellable(delay, cancel)
                continue
            if policy.retry_on_status(resp.status) and attempt <= policy.max_retries:
                delay = policy.delay_for(attempt)
                _notify_retry(retry_notifier, attempt, policy.max_retries, delay, resp.status)
                await _sleep_cancellable(delay, cancel)
                continue
            return resp

    async def request_multipart(
        self,
        method: str,
        path: str,
        *,
        fields: Mapping[str, str] | None = None,
        files: Sequence[tuple[str, str, bytes, str]] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        cancel: Any = None,
    ) -> Response:
        """multipart/form-data 上传（ASR 音频、生图参考图用）。"""
        boundary = "----atriX%s" % os.urandom(12).hex()
        payload = build_multipart(fields or {}, files or [], boundary)
        merged = dict(self.headers)
        merged["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        merged["Content-Length"] = str(len(payload))
        if headers:
            merged.update({k: v for k, v in headers.items() if v is not None})
        url = self.resolve(path)
        _raise_if_cancelled(cancel)
        return await asyncio.to_thread(
            self._blocking_request, method, url, merged, payload, timeout or self.timeout
        )

    async def open_stream(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | None = None,
        json: Any = None,
        timeout: float | None = None,
        read_timeout: float | None = None,
        cancel: Any = None,
        encoding: str = "utf-8",
        accept: str = "text/event-stream",
    ) -> "HTTPStream":
        """打开一条流式响应。

        返回的 :class:`HTTPStream` 在 ``await open()`` 之后才拿到状态码与响应头，
        调用方可在解析 SSE 之前先判定 4xx/5xx 并读取错误体。
        """
        url = self.resolve(path)
        merged = dict(self.headers)
        if accept:
            merged.setdefault("Accept", accept)
        if headers:
            merged.update({k: v for k, v in headers.items() if v is not None})
        payload: bytes | None
        if json is not None:
            payload = _json.dumps(json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
        stream = HTTPStream(
            url=url,
            method=method,
            headers=merged,
            payload=payload,
            proxy=self.proxy,
            verify_tls=self.verify_tls,
            timeout=timeout or self.timeout,
            read_timeout=read_timeout if read_timeout is not None else (timeout or self.timeout),
            cancel=cancel,
            encoding=encoding,
        )
        await stream.open()
        return stream

    async def stream_lines(self, method: str, path: str, **kw: Any) -> AsyncIterator[str]:
        """便捷入口：打开流并逐行产出。调用方不需要状态码时用它。"""
        stream = await self.open_stream(method, path, **kw)
        try:
            async for line in stream.lines():
                yield line
        finally:
            await stream.close()

    # -- internals ----------------------------------------------------------
    def _blocking_request(
        self, method: str, url: str, headers: Mapping[str, str], payload: bytes | None, timeout: float
    ) -> Response:
        conn = None
        try:
            conn = _open_connection(url, self.proxy, self.verify_tls, timeout)
            _send(conn, method, url, headers, payload)
            resp = conn.getresponse()
            body = resp.read()
            return Response(
                status=resp.status,
                headers={k: v for k, v in resp.getheaders()},
                body=body,
                url=url,
            )
        except HTTPError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, socket.timeout) as exc:
            raise HTTPError(f"{method} {url} 失败: {exc}", cause=exc) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


class HTTPStream:
    """流式响应句柄。读取线程把行推进 ``asyncio.Queue``，可被取消。

    用法::

        stream = await client.open_stream("POST", "/v1/chat/completions", json=body)
        if stream.status != 200:
            raise parse_error(stream.status, await stream.read_all())
        async for line in stream.lines():
            ...
    """

    def __init__(
        self,
        *,
        url: str,
        method: str,
        headers: Mapping[str, str],
        payload: bytes | None,
        proxy: str,
        verify_tls: bool,
        timeout: float,
        read_timeout: float,
        cancel: Any,
        encoding: str,
    ) -> None:
        self.url = url
        self.method = method
        self.status = 0
        self.headers: dict[str, str] = {}
        self._headers_request = dict(headers)
        self._payload = payload
        self._proxy = proxy
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._read_timeout = read_timeout
        self._cancel = cancel
        self._encoding = encoding

        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=2048)
        self._stop = threading.Event()
        self._conn: http.client.HTTPConnection | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._open_error: BaseException | None = None
        self._stream_error: BaseException | None = None
        # 事件循环必须在 **async 上下文**里捕获后交给工作线程：
        # 工作线程里没有 running loop，调 asyncio.get_running_loop() 会抛
        # RuntimeError，而它发生在 try 之前会让线程静默死亡、_ready 永不置位，
        # 于是 open() 的等待循环永久挂住（不是超时，是 hang）。
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ----------------------------------------------------------
    async def open(self) -> "HTTPStream":
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._pump, name="atri-x-http-stream", daemon=True)
        self._thread.start()
        # 等待响应头（或连接错误）就绪，避免把连接错误误报成 HTTP 状态码。
        while not self._ready.is_set():
            _raise_if_cancelled(self._cancel)
            await asyncio.sleep(0.005)
        if self._open_error is not None:
            raise HTTPError(f"{self.method} {self.url} 失败: {self._open_error}", cause=self._open_error)
        return self

    async def lines(self) -> AsyncIterator[str]:
        while not self._stop.is_set():
            _raise_if_cancelled(self._cancel)
            item = await self._queue.get()
            if item is _STREAM_EOF:
                break
            yield item
        if self._stream_error is not None:
            raise HTTPError(f"流式响应中断: {self._stream_error}", cause=self._stream_error)

    async def read_all(self, limit: int = 8 * 1024 * 1024) -> bytes:
        """把剩余响应体读空（用于错误响应诊断）。"""
        chunks: list[bytes] = []
        total = 0
        async for line in self.lines():
            data = line.encode(self._encoding, errors="replace") + b"\n"
            chunks.append(data)
            total += len(data)
            if total >= limit:
                break
        return b"".join(chunks)

    async def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 1.0)

    # -- thread side --------------------------------------------------------
    def _pump(self) -> None:
        loop = self._loop
        if loop is None:
            # open() 一定先于线程目标执行，走到这里说明调用方直接用了线程；
            # 明确报错而不是让等待方永久挂住。
            self._open_error = RuntimeError("HTTPStream.open() 未被调用（缺少事件循环）")
            self._ready.set()
            return
        try:
            self._conn = _open_connection(self.url, self._proxy, self._verify_tls, self._timeout)
            _send(self._conn, self.method, self.url, self._headers_request, self._payload)
            resp = self._conn.getresponse()
            self.status = resp.status
            self.headers = {k: v for k, v in resp.getheaders()}
            if self._read_timeout and self._conn.sock is not None:
                self._conn.sock.settimeout(self._read_timeout)
        except BaseException as exc:  # noqa: BLE001 - 交给消费端判定
            self._open_error = exc
            self._ready.set()
            loop.call_soon_threadsafe(self._queue.put_nowait, _STREAM_EOF)
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                raw = resp.readline()
                if not raw:
                    break
                text = raw.decode(self._encoding, errors="replace").rstrip("\r\n")
                loop.call_soon_threadsafe(self._queue.put_nowait, text)
        except BaseException as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self._stream_error = exc
        finally:
            loop.call_soon_threadsafe(self._queue.put_nowait, _STREAM_EOF)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:  # noqa: BLE001
                pass


_STREAM_EOF = object()


def _open_connection(
    url: str, proxy: str, verify_tls: bool, timeout: float
) -> http.client.HTTPConnection:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise HTTPError(f"不支持的 scheme: {parts.scheme}")
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    context: ssl.SSLContext | None = None
    if parts.scheme == "https":
        if verify_tls:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()  # noqa: SLF001 - 显式关闭校验是配置项

    if proxy:
        pp = urllib.parse.urlsplit(proxy)
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                pp.hostname, pp.port or 8080, timeout=timeout, context=context
            )
            conn.set_tunnel(host, port)
            return conn
        return http.client.HTTPConnection(pp.hostname, pp.port or 8080, timeout=timeout)

    if parts.scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _send(
    conn: http.client.HTTPConnection,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: bytes | None,
) -> None:
    """发送请求（不读响应）。调用方随后自行调用 ``getresponse()``。"""
    parts = urllib.parse.urlsplit(url)
    target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    for key, value in headers.items():
        conn.putheader(key, value)
    if not any(k.lower() == "host" for k in headers):
        conn.putheader("Host", parts.netloc)
    if payload is not None and not any(k.lower() == "content-length" for k in headers):
        conn.putheader("Content-Length", str(len(payload)))
    conn.endheaders()
    if payload:
        conn.send(payload)


def build_multipart(
    fields: Mapping[str, str], files: Sequence[tuple[str, str, bytes, str]], boundary: str
) -> bytes:
    """构造 multipart/form-data 请求体。

    ``files`` 每项为 ``(字段名, 文件名, 内容, MIME)``。
    """
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8")
        out += b"\r\n"
    for name, filename, content, mime in files:
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        ).encode()
        out += f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n".encode()
        out += content
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def _with_query(url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]]) -> str:
    items: Iterable[tuple[str, Any]]
    items = params.items() if isinstance(params, Mapping) else params
    encoded = urllib.parse.urlencode(
        [(k, v) for k, v in items if v is not None], doseq=True
    )
    if not encoded:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{encoded}"


def _raise_if_cancelled(cancel: Any) -> None:
    if cancel is None:
        return
    checker = getattr(cancel, "is_cancelled", None)
    if callable(checker) and checker():
        raise asyncio.CancelledError("请求已取消")
    if isinstance(cancel, asyncio.Event) and cancel.is_set():
        raise asyncio.CancelledError("请求已取消")


async def _sleep_cancellable(delay: float, cancel: Any) -> None:
    if delay <= 0:
        return
    step = 0.05
    waited = 0.0
    while waited < delay:
        _raise_if_cancelled(cancel)
        await asyncio.sleep(min(step, delay - waited))
        waited += step


def _notify_retry(
    notifier: Callable[[Any, Any], Any] | None,
    attempt: int,
    max_retries: int,
    delay: float,
    status: int | None,
) -> None:
    if notifier is None:
        return
    try:
        from atri_x.llm.llm import RetryEvent

        notifier(None, RetryEvent(attempt=attempt, max_retries=max_retries, delay=delay, err=None))
    except Exception:  # noqa: BLE001 - 通知失败不影响请求
        pass


# ---------------------------------------------------------------------------
# SSE 解析（Go 侧在适配器里手写；这里统一提供）
# ---------------------------------------------------------------------------

@dataclass
class SSEEvent:
    event: str = ""
    data: str = ""
    id: str = ""
    retry: int = 0


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """把 ``stream_lines`` 的输出解析成 SSE 事件。

    兼容 ``data:`` 后有/无空格两种写法，以及多行 ``data:`` 拼接。
    """
    event = SSEEvent()
    data_lines: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data_lines or event.event or event.id:
                event.data = "\n".join(data_lines)
                yield event
            event = SSEEvent()
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event.event = value
        elif field == "id":
            event.id = value
        elif field == "retry":
            try:
                event.retry = int(value)
            except ValueError:
                pass
    if data_lines or event.event or event.id:
        event.data = "\n".join(data_lines)
        yield event


__all__ = [
    "DEFAULT_TIMEOUT",
    "HTTPClient",
    "HTTPError",
    "HTTPStream",
    "Response",
    "RetryPolicy",
    "SSEEvent",
    "build_multipart",
    "iter_sse",
]
