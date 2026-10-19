"""HTTP 客户端：对应 Go 的 ``net/http`` 用法。

设计要点：

* 同步 ``http.client`` 在 ``asyncio.to_thread`` 里执行，避免引入第三方
  aiohttp/httpx；连接复用通过 ``http.client`` 的连接本身不可跨线程，
  因此每次请求新建连接（与 Go 侧 ``DisableKeepAlives`` 等价，本项目
  并发上限只有个位数，代价可忽略）。
* 流式读取（SSE）走独立的读取线程 + ``asyncio.Queue``，可被取消。
* 代理：``http`` 走绝对 URI，``https`` 走 CONNECT 隧道，覆盖本项目
  Telegram / LLM 在受限网络下的常见部署方式。
* 重定向：``request`` / ``request_multipart`` / ``open_stream`` **三条路径共用**
  :func:`_redirect_step` 的同一套规则——默认跟随最多 ``DEFAULT_MAX_REDIRECTS`` 次
  （与 Go 默认 ``http.Client`` 一致）、相对 ``Location``、循环检测、301/302/303 改
  GET 并丢 body、307/308 保留方法与 body（body 必须可重放）、跨源丢弃敏感头、每跳
  关闭旧连接、只允许 http/https、**整条跳转链共享一份总超时预算**。
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
#: 默认重定向次数上限（对应 Go ``http.Client`` 的默认 10 次）。
DEFAULT_MAX_REDIRECTS = 10
#: 会被跟随的状态码；其余 3xx 原样返回给调用方。
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: 跨源重定向时必须丢弃的敏感请求头（对应 Go 的 ``shouldCopyHeaderOnRedirect``）。
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "www-authenticate"}
)


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
        follow_redirects: bool = True,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> Response:
        """发一次请求并返回缓冲响应。

        默认跟随最多 ``max_redirects`` 次重定向（对应 Go 缺省 ``http.Client`` 的 10 次）：

        * 跟随 301/302/303/307/308；其余 3xx（如 304）或没有 ``Location`` 时原样返回。
        * 301/302/303 对非 GET/HEAD 改成 GET 并丢弃请求体；307/308 保留方法与请求体
          （体不可重放时抛 :class:`HTTPError`）——两条都与 Go 一致。
        * ``Location`` 相对地址按 :func:`urllib.parse.urljoin` 解析。
        * 跨源（scheme/host/port 有变）时丢弃 Authorization / Cookie /
          Proxy-Authorization / WWW-Authenticate 等敏感头。
        * 超过次数抛 :class:`HTTPError`（本项目既有的传输层错误类型）。

        ``follow_redirects=False`` 时不跟随，3xx 直接交给调用方判定。
        """
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

        return await self._send_following(
            method,
            url,
            merged,
            payload,
            timeout=timeout,
            policy=retry or RetryPolicy(max_retries=0),
            retry_notifier=retry_notifier,
            cancel=cancel,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
        )

    async def _send_following(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: bytes | None,
        *,
        timeout: float | None,
        policy: RetryPolicy,
        retry_notifier: Callable[[Any, Any], Any] | None,
        cancel: Any,
        follow_redirects: bool,
        max_redirects: int,
    ) -> Response:
        """缓冲路径共用的跳转链：每跳按重试策略发送，按共用规则决定是否跟随。"""
        # 整条链（含重试等待与每跳 I/O）共用一份预算，而不是每跳各拿一次 timeout。
        budget = _RedirectBudget(timeout or self.timeout)
        visited = {(method.upper(), url)}
        redirects = 0
        while True:
            resp = await self._send_with_retry(
                method, url, headers, payload, budget, policy, retry_notifier, cancel
            )
            location = resp.header("Location") if resp.status in _REDIRECT_STATUSES else ""
            if not follow_redirects or not location:
                return resp
            method, url, payload, headers = _redirect_step(
                status=resp.status,
                method=method,
                url=url,
                payload=payload,
                headers=headers,
                location=location,
                max_redirects=max_redirects,
                redirects=redirects,
                visited=visited,
            )
            visited.add((method.upper(), url))
            redirects += 1

    async def _send_with_retry(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: bytes | None,
        budget: "_RedirectBudget",
        policy: RetryPolicy,
        retry_notifier: Callable[[Any, Any], Any] | None,
        cancel: Any,
    ) -> Response:
        """按重试策略发一跳请求，直到拿到响应或重试耗尽；每跳从总预算里扣时间。"""
        attempt = 0
        while True:
            attempt += 1
            _raise_if_cancelled(cancel)
            hop_timeout = budget.remaining()
            try:
                resp = await asyncio.to_thread(
                    self._blocking_request, method, url, headers, payload, hop_timeout
                )
            except HTTPError:
                if attempt > policy.max_retries:
                    raise
                delay = budget.clip(policy.delay_for(attempt))
                _notify_retry(retry_notifier, attempt, policy.max_retries, delay, None)
                await _sleep_cancellable(delay, cancel)
                continue
            if policy.retry_on_status(resp.status) and attempt <= policy.max_retries:
                delay = budget.clip(policy.delay_for(attempt))
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
        follow_redirects: bool = True,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> Response:
        """multipart/form-data 上传（ASR 音频、生图参考图用）。

        与 ``request`` 走**同一套跳转规则**（默认跟随、307/308 保留方法与 body）：
        payload 已经是内存里的 bytes，任何一跳都能重放。
        """
        boundary = "----atriX%s" % os.urandom(12).hex()
        payload = build_multipart(fields or {}, files or [], boundary)
        merged = dict(self.headers)
        merged["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        merged["Content-Length"] = str(len(payload))
        if headers:
            merged.update({k: v for k, v in headers.items() if v is not None})
        url = self.resolve(path)
        return await self._send_following(
            method,
            url,
            merged,
            payload,
            timeout=timeout,
            policy=RetryPolicy(max_retries=0),  # 上传不自动重试，只跟随跳转
            retry_notifier=None,
            cancel=cancel,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
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
        follow_redirects: bool = True,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        queue_maxsize: int = 2048,
    ) -> "HTTPStream":
        """打开一条流式响应。

        返回的 :class:`HTTPStream` 在 ``await open()`` 之后才拿到状态码与响应头，
        调用方可在解析 SSE 之前先判定 4xx/5xx 并读取错误体。

        重定向与 ``request`` 共用同一套规则（默认跟随、每跳重开连接、跨源剥离敏感头、
        整条链共享 ``timeout`` 预算）。语义边界：

        * 3xx 且**没有** ``Location``：原样返回（``stream.status`` 就是 3xx）；
        * 超过 ``max_redirects`` 或检测到循环：``open()`` 抛 :class:`HTTPError`；
        * ``follow_redirects=False``：3xx 原样返回，由调用方处理。

        ``queue_maxsize`` 是行队列上限：满了之后读取线程带背压等待，不会丢行/丢 EOF。

        ``read_timeout`` 是**正文**的 socket 级读截止：正数即生效；显式传 ``0`` 表示
        **不设截止**（socket 切回阻塞模式）——Go 对正文只有 timer、没有读截止时间，
        此时首包/逐行空闲完全由调用方自己的 timer 判定（LLM 适配器用
        ``StreamLineReader`` 的 first-chunk/idle timer）。``None``（缺省）沿用 ``timeout``。
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
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            queue_maxsize=queue_maxsize,
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

    队列有上限（``queue_maxsize``，默认 2048）：满了以后读取线程**带背压等待**
    （在同一个 ``run_coroutine_threadsafe`` future 上轮询），而不是在事件循环回调里
    调 ``put_nowait`` 抛 ``QueueFull`` 把行或 EOF 丢掉——那样消费端会永久挂在
    ``queue.get()`` 上。``close()`` 除了置停止位，还会给队列补一个 EOF 唤醒正在
    等待的消费端。

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
        follow_redirects: bool = True,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        queue_maxsize: int = 2048,
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
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects

        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._stop = threading.Event()
        self._conn: http.client.HTTPConnection | None = None
        self._response: http.client.HTTPResponse | None = None
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
        try:
            while not self._ready.is_set():
                _raise_if_cancelled(self._cancel)
                if self._stop.is_set():
                    break
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            # 取消也要收干净：置停止位 + 关连接/响应后读取线程才能退出，
            # 否则线程会一直挂在 socket 上，连接留在 CLOSE_WAIT。
            await self._abort_open()
            raise
        if self._open_error is not None:
            raise HTTPError(f"{self.method} {self.url} 失败: {self._open_error}", cause=self._open_error)
        if self._stop.is_set():
            raise HTTPError(f"{self.method} {self.url} 已关闭")
        return self

    async def _abort_open(self) -> None:
        """取消路径的清理：置停止位、关闭当前连接并等读取线程退出。"""
        try:
            await self.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 清理失败不能掩盖原始取消
            pass

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
        self._close_conn()
        self._wake_consumer()
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 1.0)

    def _close_conn(self) -> None:
        """关闭响应与连接（幂等，可从任意线程调用）。"""
        for target in (self._response, self._conn):
            if target is None:
                continue
            try:
                target.close()
            except Exception:  # noqa: BLE001
                pass

    def _wake_consumer(self) -> None:
        """给正在 ``await queue.get()`` 的消费端补一个 EOF，避免 close() 之后挂死。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _put() -> None:
            try:
                self._queue.put_nowait(_STREAM_EOF)
            except asyncio.QueueFull:
                # 队列里还有行，消费端醒着；它读完后会看到 _stop。
                pass

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:  # 事件循环已关闭
            pass

    def _deliver(self, item: Any) -> bool:
        """把一项投给事件循环上的队列；队列满时等待（背压）。

        超时后**复用同一个 future** 继续等——每次超时都新建 future 会把同一行重复入队。
        ``_stop`` 置位或事件循环关闭时放弃投递并返回 False，读取线程据此退出。返回值
        False 只表示「放弃」，EOF 的可靠送达由调用方在正常收尾路径上保证。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._queue.put(item), loop)
        except RuntimeError:  # 事件循环已关闭
            return False
        while True:
            try:
                future.result(timeout=0.05)
                return True
            except TimeoutError:  # 队列满：继续等同一个 future
                if self._stop.is_set() or loop.is_closed():
                    _abandon_delivery(future)
                    return False
            except asyncio.CancelledError:  # 投递被取消（例如事件循环收尾）
                return False
            except Exception:  # noqa: BLE001 - 投递失败不能杀死读取线程
                return False

    # -- thread side --------------------------------------------------------
    def _pump(self) -> None:
        loop = self._loop
        if loop is None:
            # open() 一定先于线程目标执行，走到这里说明调用方直接用了线程；
            # 明确报错而不是让等待方永久挂住。
            self._open_error = RuntimeError("HTTPStream.open() 未被调用（缺少事件循环）")
            self._ready.set()
            return
        budget = _RedirectBudget(self._timeout)
        method, url = self.method, self.url
        headers = dict(self._headers_request)
        payload = self._payload
        visited = {(method.upper(), url)}
        redirects = 0
        try:
            while True:
                if self._stop.is_set():
                    raise HTTPError(f"{method} {url} 已关闭")
                conn = _open_connection(url, self._proxy, self._verify_tls, budget.remaining())
                self._conn = conn
                _send(conn, method, url, headers, payload)
                # 必须在 getresponse() **之前**留住 socket 引用：响应没有 Content-Length
                # 或带 ``Connection: close``（SSE 的常见形态）时，http.client 会把 socket
                # 交接给 response 并把 ``conn.sock`` 置 None，之后就设不了读截止了。
                sock = conn.sock
                resp = conn.getresponse()
                self._response = resp
                self.status = resp.status
                self.headers = {k: v for k, v in resp.getheaders()}
                location = resp.getheader("Location") or ""
                if not (
                    self._follow_redirects and resp.status in _REDIRECT_STATUSES and location
                ):
                    if sock is not None:
                        # 正数 = 正文的 socket 级读截止；0/None = **不设截止**（socket 切回
                        # 阻塞），正文超时交给调用方自己的 timer —— Go 对正文只有 timer、
                        # 没有读截止时间。不能写成 `if self._read_timeout:` 再跳过：那会让
                        # socket 保留建连时设的超时，正文被 first_chunk_timeout 提前掐断
                        # （见 tests/test_httpclient.py 的 read_timeout 用例）。
                        sock.settimeout(self._read_timeout or None)
                    break
                # 跟随下一跳：按共用规则重算请求，并关掉这一跳的连接。
                self._close_conn()
                method, url, payload, headers = _redirect_step(
                    status=resp.status,
                    method=method,
                    url=url,
                    payload=payload,
                    headers=headers,
                    location=location,
                    max_redirects=self._max_redirects,
                    redirects=redirects,
                    visited=visited,
                )
                self.method, self.url = method, url
                visited.add((method.upper(), url))
                redirects += 1
        except BaseException as exc:  # noqa: BLE001 - 交给消费端判定
            self._open_error = exc
            self._close_conn()
            self._ready.set()
            self._deliver(_STREAM_EOF)
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                raw = resp.readline()
                if not raw:
                    break
                text = raw.decode(self._encoding, errors="replace").rstrip("\r\n")
                if not self._deliver(text):
                    break
        except BaseException as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self._stream_error = exc
        finally:
            # 正常收尾（EOF/错误）要求把哨兵送到位；_stop 已置位时投递会立刻放弃，
            # 消费端此时也不再读了。
            self._deliver(_STREAM_EOF)
            self._close_conn()


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


def _origin(url: str) -> tuple[str, str, int]:
    """返回 ``(scheme, host, port)``，用于判断重定向是否跨源。"""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def _abandon_delivery(future: Any) -> None:
    """放弃一次线程→事件循环的投递：能取消就取消，并取走异常。

    ``run_coroutine_threadsafe`` 的 future 若以异常收场却没人读 ``result()``，会留下
    "exception was never retrieved"；这里挂一个 done 回调把结果取走。
    """

    def _drain(done: Any) -> None:
        if not done.cancelled():
            try:
                done.exception()
            except Exception:  # noqa: BLE001 - 只为取走异常
                pass

    future.add_done_callback(_drain)
    try:
        future.cancel()
    except Exception:  # noqa: BLE001 - 取消失败不影响线程退出
        pass


class _RedirectBudget:
    """整条跳转链共享的总超时预算（含重试等待与每跳 I/O）。

    ``timeout`` 从前是「每跳各给一次」，跳转链变长后总耗时可以无限增长；这里改成
    整条链一个 deadline，每跳只拿剩余时间（对应 Go ``http.Client.Timeout`` 语义）。
    """

    def __init__(self, seconds: float) -> None:
        self.total = max(0.0, float(seconds))
        self.deadline = time.monotonic() + self.total

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise HTTPError(f"请求总超时：{self.total:g}s 预算已耗尽")
        return left

    def clip(self, delay: float) -> float:
        """把一次退避等待裁剪进剩余预算，保证等待不会超出总预算。"""
        return max(0.0, min(delay, self.deadline - time.monotonic()))


def _redirect_step(
    *,
    status: int,
    method: str,
    url: str,
    payload: Any,
    headers: Mapping[str, str],
    location: str,
    max_redirects: int,
    redirects: int,
    visited: set[tuple[str, str]],
) -> tuple[str, str, Any, dict[str, str]]:
    """算出下一跳的 ``(method, url, payload, headers)``——三条路径共用的跳转规则。

    ``request`` / ``request_multipart`` / ``open_stream`` 都走这里，保证「相对
    Location、跳数上限、循环检测、方法转换、跨源剥离敏感头、只允许 http/https」
    只有一处实现。``visited`` 是本次链路上已经发过的 ``(方法, URL)`` 集合。
    """
    if redirects >= max_redirects:
        raise HTTPError(f"重定向次数超过上限 {max_redirects}：{url} -> {location}")
    next_url = urllib.parse.urljoin(url, location)
    scheme = urllib.parse.urlsplit(next_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise HTTPError(f"重定向目标不是 http/https：{next_url}")
    next_method, next_payload, next_headers = _redirect_plan(
        status, method, payload, headers, url, next_url
    )
    if (next_method.upper(), next_url) in visited:
        raise HTTPError(f"检测到重定向循环：{url} -> {next_url}")
    return next_method, next_url, next_payload, next_headers


def _redirect_plan(
    status: int,
    method: str,
    payload: Any,
    headers: Mapping[str, str],
    url: str,
    next_url: str,
) -> tuple[str, Any, dict[str, str]]:
    """算出跟随一次重定向后的 ``(method, payload, headers)``（语义对齐 Go ``net/http``）。"""
    out = dict(headers)
    if _origin(url) != _origin(next_url):
        # 跨源：丢弃敏感头（对应 Go 的 shouldCopyHeaderOnRedirect）以及调用方显式给的
        # ``Host``——换了主机之后它已经是错的目标（:func:`_send` 会按新 URL 重算）。
        for name in list(out):
            if name.lower() in _SENSITIVE_HEADERS or name.lower() == "host":
                del out[name]
    if status in (307, 308):
        # 307/308 必须重放同一个请求；本项目 body 是内存里的 bytes/str，总是可重放。
        if payload is not None and not isinstance(payload, (bytes, bytearray, memoryview)):
            raise HTTPError(f"{status} 重定向无法重放请求体：{url} -> {next_url}")
        return method, payload, out
    if method.upper() not in ("GET", "HEAD"):
        method = "GET"
    # 丢弃 body：显式 Content-Length 不删会让服务端等一个永远不来的请求体；
    # Content-Type 留着同样错位（空 body 却自称 multipart/json）。
    for name in [key for key in out if key.lower() in ("content-length", "content-type")]:
        del out[name]
    return method, None, out


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
    "DEFAULT_MAX_REDIRECTS",
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
