"""LLM 请求用的行流与缓冲请求（临时替代 ``httpclient`` 的对应能力）。

为什么存在：``atri_x/net/httpclient.py`` 当前有两处让适配器无法使用：

1. ``HTTPStream._pump`` 在工作线程里调用 ``asyncio.get_running_loop()``（第 388 行），
   工作线程没有 running loop → ``RuntimeError: no running event loop`` 在 ``try``
   之前抛出 → ``_ready`` 永不置位 → ``open_stream()`` 永久挂起；
2. ``HTTPClient._blocking_request`` 把 ``_send(...)`` 的返回值（``None``）当成状态码
   赋给 ``Response.status``（第 274 行），于是 ``Response.status is None``，
   ``request()`` 里 ``policy.retry_on_status(None)`` 直接抛
   ``TypeError: '>=' not supported between instances of 'NoneType' and 'int'``。

因此本模块自己实现两条可用的最小路径：

* :class:`LineStream`：SSE 行流（在 async 上下文捕获事件循环后再起读取线程），
  对外契约与 ``HTTPStream`` 一致：``status`` / ``headers`` / ``lines()`` /
  ``read_all()`` / ``close()``；
* :func:`buffered_request`：一次性读全的请求（``/models``、非流式 chat）。

连接、代理与 TLS 仍复用 ``httpclient`` 的 ``_open_connection`` / ``_send``，不复制
那部分逻辑。等 ``httpclient`` 修好后（见
:meth:`atri_x.llm.openai.openai.OpenAIClient.open_stream_request` /
``_buffered_request`` 的 TODO），本模块可以整体删除。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

from atri_x.net.httpclient import HTTPError, _open_connection, _send

__all__ = ["BufferedResponse", "CONNECT_TIMEOUT_SECONDS", "LineStream", "STREAM_EOF", "buffered_request"]

#: 建连超时。对应 Go ``http.DefaultTransport`` 的 30s 拨号超时：它**不**等于
#: ``RequestOptions.FirstChunkTimeout``（那个只管响应头），两者分开才不会让
#: 「首包超时 0.2s」把慢网络下的建连一起判死。
CONNECT_TIMEOUT_SECONDS = 30.0

#: 队列哨兵（与 ``httpclient._STREAM_EOF`` 同义，这里自己持有以免依赖私有名）。
STREAM_EOF = object()


@dataclass
class BufferedResponse:
    """一次读完的响应（字段与 ``httpclient.Response`` 对齐）。"""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    def header(self, name: str, default: str = "") -> str:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return default


def _blocking_buffered_request(
    url: str,
    method: str,
    headers: Mapping[str, str],
    payload: bytes | None,
    proxy: str,
    verify_tls: bool,
    timeout: float,
) -> BufferedResponse:
    conn = None
    try:
        conn = _open_connection(url, proxy, verify_tls, CONNECT_TIMEOUT_SECONDS)
        _send(conn, method, url, headers, payload)
        if conn.sock is not None:
            # 建连（30s）与「读响应」分开：非流式请求体很小，这里直接用调用方给的
            # 首包超时兜住响应头 + 正文，避免对端挂住时无限等待。
            conn.sock.settimeout(timeout)
        response = conn.getresponse()
        return BufferedResponse(
            status=response.status,
            headers={key: value for key, value in response.getheaders()},
            body=response.read(),
            url=url,
        )
    except HTTPError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一成传输层错误
        raise HTTPError(f"{method} {url} 失败: {exc}", cause=exc) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


async def buffered_request(
    *,
    url: str,
    method: str,
    headers: Mapping[str, str],
    payload: bytes | None = None,
    proxy: str = "",
    verify_tls: bool = True,
    timeout: float = 30.0,
) -> BufferedResponse:
    """发一次请求并读全正文（阻塞 I/O 放进工作线程）。"""
    return await asyncio.to_thread(
        _blocking_buffered_request, url, method, headers, payload, proxy, verify_tls, timeout
    )


class LineStream:
    """把一条 HTTP 响应的正文按行推给事件循环。

    读取线程只做阻塞 I/O，行通过 ``loop.call_soon_threadsafe`` 进入
    ``asyncio.Queue``；队列不设上限（消费端在 :class:`~atri_x.llm.openai.stream.StreamLineReader`
    里持续排空），因此不会出现「队列满 → 回调里抛异常 → 丢行」。
    """

    def __init__(
        self,
        *,
        url: str,
        method: str,
        headers: Mapping[str, str],
        payload: bytes | None,
        proxy: str = "",
        verify_tls: bool = True,
        timeout: float = 30.0,
        encoding: str = "utf-8",
    ) -> None:
        self.url = url
        self.method = method
        self.status = 0
        self.headers: dict[str, str] = {}
        self._request_headers = dict(headers)
        self._payload = payload
        self._proxy = proxy
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._encoding = encoding

        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._conn: Any = None
        self._thread: threading.Thread | None = None
        self._open_error: BaseException | None = None
        self._stream_error: BaseException | None = None

    # -- 生命周期 -----------------------------------------------------------
    async def open(self) -> "LineStream":
        """建连并等响应头就绪；连接失败抛 ``HTTPError``。"""
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._pump, args=(loop,), name="atri-x-llm-sse", daemon=True
        )
        self._thread.start()
        while not self._ready.is_set():
            if self._stop.is_set():
                break
            await asyncio.sleep(0.002)
        if self._open_error is not None:
            raise HTTPError(
                f"{self.method} {self.url} 失败: {self._open_error}", cause=self._open_error
            )
        return self

    async def lines(self) -> AsyncIterator[str]:
        """逐行产出正文（已去掉行尾 ``\\r\\n``）；读取失败抛 ``HTTPError``。"""
        while not self._stop.is_set():
            item = await self._queue.get()
            if item is STREAM_EOF:
                break
            yield item
        if self._stream_error is not None and not self._stop.is_set():
            raise HTTPError(f"流式响应中断: {self._stream_error}", cause=self._stream_error)

    async def read_all(self, limit: int = 8 * 1024 * 1024) -> bytes:
        """把剩余正文读空（错误响应诊断用）。"""
        chunks: list[bytes] = []
        total = 0
        async for line in self.lines():
            payload = line.encode(self._encoding, errors="replace") + b"\n"
            chunks.append(payload)
            total += len(payload)
            if total >= limit:
                break
        return b"".join(chunks)

    async def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响调用方
                pass
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 1.0)

    # -- 线程侧 -------------------------------------------------------------
    def _pump(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            self._conn = _open_connection(
                self.url, self._proxy, self._verify_tls, CONNECT_TIMEOUT_SECONDS
            )
            _send(self._conn, self.method, self.url, self._request_headers, self._payload)
            # 响应头用首包超时（对应 Go 的 ResponseHeaderTimeout）。必须在
            # getresponse() 之前留住 socket 引用：``Connection: close`` 的响应会被
            # http.client 交接给 response（``conn.close()`` 把 conn.sock 置 None），
            # 之后就取不到了，socket 上的读超时会一直生效到正文——而 Go 对正文没有
            # 读截止时间，正文的超时判定只属于 StreamLineReader 的 timer。
            sock = self._conn.sock
            if sock is not None:
                sock.settimeout(self._timeout)
            response = self._conn.getresponse()
            self.status = response.status
            self.headers = {key: value for key, value in response.getheaders()}
            if sock is not None:
                sock.settimeout(None)
        except BaseException as exc:  # noqa: BLE001 - 交给消费端判定
            self._open_error = exc
            self._ready.set()
            loop.call_soon_threadsafe(self._queue.put_nowait, STREAM_EOF)
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                raw = response.readline()
                if not raw:
                    break
                text = raw.decode(self._encoding, errors="replace").rstrip("\r\n")
                loop.call_soon_threadsafe(self._queue.put_nowait, text)
        except BaseException as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self._stream_error = exc
        finally:
            loop.call_soon_threadsafe(self._queue.put_nowait, STREAM_EOF)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:  # noqa: BLE001
                pass
