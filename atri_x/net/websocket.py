"""WebSocket（RFC 6455）客户端与服务端：对应 Go 的 ``github.com/coder/websocket``。

Python 标准库没有 WebSocket，本模块补齐 ATRI-X 需要的路径：OneBot 网关、QQ 官方网关、
远程 CLI 与健康/ops 端口的服务端。帧编解码（:func:`encode_frame` / :class:`FrameDecoder`）
不涉及 I/O，可单独单测；异步 API 是 ``await connect(url)`` 与 ``await serve(handler, ...)``；
同步 API（:func:`connect_sync` / :func:`accept_sync` / :class:`SyncWebSocket`）用后台事件循环
线程托管同一套异步实现；:func:`accept` 走 ``http.server.BaseHTTPRequestHandler`` 兼容路径
（须在 handler 内跑完会话）。客户端必须掩码、服务端不得掩码；控制帧不得分片且 ≤125 字节；
消息上限默认 8 MiB，超限以 1009 关闭。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import secrets
import ssl
import struct
import threading
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Mapping, Protocol

__all__ = [
    "OP_CONT", "OP_TEXT", "OP_BINARY", "OP_CLOSE", "OP_PING", "OP_PONG", "DEFAULT_MAX_SIZE",
    "CLOSE_NORMAL", "CLOSE_GOING_AWAY", "CLOSE_PROTOCOL_ERROR", "CLOSE_UNSUPPORTED",
    "CLOSE_POLICY_VIOLATION", "CLOSE_TOO_LARGE", "CLOSE_INTERNAL_ERROR", "ConnectionClosed",
    "Frame", "FrameDecoder", "HandshakeError", "HandshakeRequest", "ProtocolError",
    "SyncWebSocket", "WebSocketClient", "WebSocketConnection", "WebSocketError",
    "WebSocketServer", "accept", "accept_key", "accept_sync", "apply_mask",
    "build_client_request", "connect", "connect_sync", "encode_frame", "parse_http_response",
    "server_handshake", "serve"]

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
CLOSE_NORMAL, CLOSE_GOING_AWAY, CLOSE_PROTOCOL_ERROR, CLOSE_UNSUPPORTED = 1000, 1001, 1002, 1003
CLOSE_POLICY_VIOLATION, CLOSE_TOO_LARGE, CLOSE_INTERNAL_ERROR = 1008, 1009, 1011
DEFAULT_MAX_SIZE = 8 * 1024 * 1024
_READ_CHUNK = 65536
_FORBIDDEN_CLOSE_CODES = frozenset({1004, 1005, 1006, 1015})
_DATA_OPCODES = frozenset({OP_CONT, OP_TEXT, OP_BINARY})
_CONTROL_OPCODES = frozenset({OP_CLOSE, OP_PING, OP_PONG})
_STATUS_TEXT = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                426: "Upgrade Required", 500: "Server Error"}


# --- 异常 -------------------------------------------------------------------
class WebSocketError(Exception):
    """WebSocket 层基类异常。"""


class HandshakeError(WebSocketError):
    """握手失败；``status`` 是应回（或收到）的 HTTP 状态码。"""

    def __init__(self, message: str, *, status: int = 0, response: bytes = b"") -> None:
        super().__init__(message)
        self.status, self.response = status, response


class ProtocolError(WebSocketError):
    """对端违反 RFC 6455；``code`` 是应发送的关闭码。"""

    def __init__(self, message: str, *, code: int = CLOSE_PROTOCOL_ERROR) -> None:
        super().__init__(message)
        self.code = code


class ConnectionClosed(WebSocketError):
    """连接已关闭；``code`` 1000 为正常关闭、1006 为异常断开。"""

    def __init__(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        super().__init__(f"WebSocket 连接已关闭（code={code}，reason={reason!r}）")
        self.code, self.reason = code, reason


# --- 帧编解码（无 I/O，可单测） ---------------------------------------------
def apply_mask(data: bytes, key: bytes) -> bytes:
    """按 RFC 6455 用 4 字节掩码异或；``key`` 为空时原样返回。"""
    if not key:
        return data
    return bytes(byte ^ key[index & 3] for index, byte in enumerate(data))


def encode_frame(opcode: int, payload: bytes = b"", *, mask: bool, fin: bool = True) -> bytes:
    """编码一帧；``mask=True``（客户端）时随机生成 4 字节掩码键。

    载荷长度按 7/16/64 位三种形式编码（大端）。
    """
    header = bytearray([(0x80 if fin else 0x00) | (opcode & 0x0F)])
    length, flag = len(payload), 0x80 if mask else 0x00
    if length < 126:
        header.append(flag | length)
    elif length < 65536:
        header.append(flag | 126)
        header += struct.pack("!H", length)
    else:
        header.append(flag | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = secrets.token_bytes(4)
    return bytes(header) + key + apply_mask(payload, key)


@dataclass
class Frame:
    """一个已解码的帧。"""

    fin: bool
    opcode: int
    payload: bytes
    rsv: int = 0
    masked: bool = False


class FrameDecoder:
    """增量帧解码器：``feed()`` 可喂入任意字节切片，产出完整帧。

    ``expect_mask=True`` 表示本端是服务端（客户端帧必须掩码），``False`` 表示本端是
    客户端（服务端帧不得掩码）。
    """

    def __init__(self, *, expect_mask: bool = False, max_frame_size: int = DEFAULT_MAX_SIZE):
        self.expect_mask, self.max_frame_size = expect_mask, max_frame_size
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """喂入字节，返回本次能解析出的全部完整帧。"""
        self._buffer += data
        frames: list[Frame] = []
        while (frame := self._parse_one()) is not None:
            frames.append(frame)
        return frames

    def _parse_one(self) -> Frame | None:
        """解析缓冲里的第一帧；数据不足返回 ``None``。"""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        fin, rsv, opcode = bool(first & 0x80), first & 0x70, first & 0x0F
        masked, length, offset = bool(second & 0x80), second & 0x7F, 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length, offset = struct.unpack_from("!H", buffer, 2)[0], 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length, offset = struct.unpack_from("!Q", buffer, 2)[0], 10
        if length > self.max_frame_size:
            raise ProtocolError(f"帧长度 {length} 超过上限 {self.max_frame_size}",
                                code=CLOSE_TOO_LARGE)
        if masked != self.expect_mask:
            raise ProtocolError("掩码不符合本端角色（客户端必须掩码、服务端不得掩码）")
        key = b""
        if masked:
            if len(buffer) < offset + 4:
                return None
            key, offset = bytes(buffer[offset : offset + 4]), offset + 4
        if len(buffer) < offset + length:
            return None
        payload = bytes(buffer[offset : offset + length])
        del buffer[: offset + length]
        return Frame(fin, opcode, apply_mask(payload, key), rsv, masked)


# --- 握手（纯函数） ---------------------------------------------------------
def accept_key(key: str) -> str:
    """由 ``Sec-WebSocket-Key`` 计算 ``Sec-WebSocket-Accept``。"""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()  # noqa: S324 - RFC 6455 规定
    return base64.b64encode(digest).decode("ascii")


@dataclass
class HandshakeRequest:
    """一次握手请求（客户端发出的或服务端收到的）。"""

    method: str = "GET"
    path: str = "/"
    http_version: str = "HTTP/1.1"
    headers: dict[str, str] = field(default_factory=dict)
    origin: str = ""

    def header(self, name: str, default: str = "") -> str:
        """取一个小写头名的值。"""
        return self.headers.get(name.lower(), default)

    def subprotocols(self) -> list[str]:
        """客户端提供的子协议列表。"""
        return [item.strip() for item in self.header("sec-websocket-protocol").split(",")
                if item.strip()]


def _parse_head(lines: list[str]) -> dict[str, str]:
    """把 ``Name: value`` 行合并成小写键字典（重复头用逗号连接）。"""
    headers: dict[str, str] = {}
    for line in lines:
        name, _, value = line.partition(":")
        key = name.strip().lower()
        if key:
            headers[key] = f"{headers[key]}, {value.strip()}" if key in headers else value.strip()
    return headers


def parse_http_response(raw: bytes) -> tuple[int, str, dict[str, str]]:
    """解析 HTTP 响应头，返回 ``(status, reason, headers)``。"""
    lines = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise HandshakeError(f"响应状态行非法：{lines[0]!r}")
    return int(parts[1]), (parts[2] if len(parts) > 2 else ""), _parse_head(lines[1:])


def _has_token(value: str, token: str) -> bool:
    return any(part.strip().lower() == token for part in value.split(","))


def server_handshake(
    request: HandshakeRequest,
    *,
    subprotocols: list[str] | tuple[str, ...] = (),
    origins: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, dict[str, str]]:
    """校验客户端握手并生成 101 响应，返回 ``(响应文本, 元信息)``。

    校验方法/版本、``Upgrade: websocket``、``Connection: Upgrade``、
    ``Sec-WebSocket-Version: 13`` 与非空 ``Sec-WebSocket-Key``；``origins`` 非空时还要求
    ``Origin`` 命中（支持 ``"*"``）；子协议取双方都支持的第一个。失败抛
    :class:`HandshakeError`（带 ``status``）。
    """
    checks = [
        (request.method.upper() == "GET", "握手必须是 GET 请求", 405),
        (request.http_version in ("HTTP/1.1", "HTTP/1.0"),
         f"不支持的 HTTP 版本：{request.http_version}", 400),
        (_has_token(request.header("upgrade"), "websocket"), "缺少 Upgrade: websocket 头", 400),
        (_has_token(request.header("connection"), "upgrade"), "缺少 Connection: Upgrade 头", 400),
        (request.header("sec-websocket-version") == "13", "Sec-WebSocket-Version 必须为 13", 426),
        (bool(request.header("sec-websocket-key")), "缺少 Sec-WebSocket-Key 头", 400),
    ]
    for ok, message, status in checks:
        if not ok:
            raise HandshakeError(message, status=status)
    origin = request.origin or request.header("origin")
    if origins and "*" not in origins and origin not in origins:
        raise HandshakeError(f"Origin 不被允许：{origin!r}", status=403)
    supported = list(subprotocols)
    chosen = next((item for item in request.subprotocols() if item in supported), "")
    lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Accept: {accept_key(request.header('sec-websocket-key'))}"]
    if chosen:
        lines.append(f"Sec-WebSocket-Protocol: {chosen}")
    return "\r\n".join(lines) + "\r\n\r\n", {"subprotocol": chosen, "origin": origin}


def build_client_request(
    *,
    host: str,
    target: str,
    key: str,
    subprotocols: list[str] | tuple[str, ...] = (),
    headers: Mapping[str, str] | None = None,
    origin: str = "",
) -> str:
    """构造客户端握手请求文本。"""
    lines = [f"GET {target} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if subprotocols:
        lines.append("Sec-WebSocket-Protocol: " + ", ".join(subprotocols))
    if origin:
        lines.append(f"Origin: {origin}")
    lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())
    return "\r\n".join(lines) + "\r\n\r\n"


def error_response(status: int, reason: str) -> bytes:
    """握手失败时的 HTTP 响应（426 附带 ``Sec-WebSocket-Version: 13``）。"""
    body = reason.encode("utf-8")
    lines = [f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}",
             "Content-Type: text/plain; charset=utf-8", f"Content-Length: {len(body)}",
             "Connection: close"]
    if status == 426:
        lines.append("Sec-WebSocket-Version: 13")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


# --- 传输 -------------------------------------------------------------------
class _Transport(Protocol):
    async def read(self, limit: int) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class StreamTransport:
    """``asyncio`` 流（``open_connection`` / ``start_server``）上的传输。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    async def read(self, limit: int) -> bytes:
        return await self.reader.read(limit)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (OSError, asyncio.CancelledError, RuntimeError):
            pass


# --- 连接 -------------------------------------------------------------------
class WebSocketConnection:
    """一条已握手的 WebSocket 连接（客户端与服务端共用）。

    ``recv()`` 自动处理 PING/PONG 与关闭握手；``close_code`` / ``close_reason`` 在连接
    结束后可读。``max_size`` 是单条消息上限（默认 8 MiB），超限以 1009 关闭。
    """

    def __init__(
        self,
        transport: _Transport,
        *,
        is_client: bool,
        max_size: int = DEFAULT_MAX_SIZE,
        read_timeout: float | None = None,
        ping_interval: float | None = None,
        ping_timeout: float = 20.0,
        close_timeout: float = 5.0,
        subprotocol: str = "",
    ) -> None:
        self.is_client, self.max_size, self.read_timeout = is_client, max_size, read_timeout
        self.ping_interval, self.ping_timeout = ping_interval, ping_timeout
        self.close_timeout, self.subprotocol = close_timeout, subprotocol
        self.request: HandshakeRequest | None = None
        self.request_headers: dict[str, str] = {}
        self.response_headers: dict[str, str] = {}
        self.close_code, self.close_reason = 0, ""
        self._transport = transport
        self._decoder = FrameDecoder(expect_mask=not is_client, max_frame_size=max_size)
        self._pending: deque[Frame] = deque()
        self._write_lock = asyncio.Lock()
        self._close_sent = self._close_received = self._closed = False
        self._keepalive: asyncio.Task[None] | None = None
        self._last_rx = 0.0

    async def send_text(self, text: str) -> None:
        """发送文本消息（客户端自动掩码）。"""
        self._ensure_open()
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        """发送二进制消息。"""
        self._ensure_open()
        await self._send_frame(OP_BINARY, bytes(data))

    async def ping(self, payload: bytes = b"") -> None:
        """发送 PING；对端应回 PONG（由 :meth:`recv` 自动处理）。"""
        if len(payload) > 125:
            raise WebSocketError("控制帧载荷不得超过 125 字节")
        self._ensure_open()
        await self._send_frame(OP_PING, payload)

    async def _send_frame(self, opcode: int, payload: bytes = b"", *, fin: bool = True) -> None:
        frame = encode_frame(opcode, payload, mask=self.is_client, fin=fin)
        async with self._write_lock:
            await self._transport.write(frame)

    async def recv(self, timeout: float | None = None) -> str | bytes:
        """接收一条完整消息：文本返回 ``str``、二进制返回 ``bytes``。
        自动回 PONG、自动完成关闭握手（对端 close 后抛 :class:`ConnectionClosed`）。
        ``timeout`` 缺省用连接的 ``read_timeout``；读超时视为连接已死（关闭并抛 1006）。
        """
        limit = timeout if timeout is not None else self.read_timeout
        try:
            if limit is None:
                return await self._next_message()
            try:
                return await asyncio.wait_for(self._next_message(), limit)
            except TimeoutError:
                self.close_code, self.close_reason = 1006, "读超时"
                await self._shutdown()
                raise ConnectionClosed(1006, "读超时") from None
        except ProtocolError as exc:
            # 协议违规：按 RFC 6455 用对应码（1002/1007/1009）关闭后再抛错。
            await self._fail(exc.code, str(exc))
            raise ConnectionClosed(exc.code, str(exc)) from None

    async def _next_message(self) -> str | bytes:
        """读取下一条数据消息；控制帧就地处理，分片在此重组。"""
        fragments: list[bytes] = []
        opcode = OP_TEXT
        while True:
            frame = await self._read_frame()
            if frame.opcode in _CONTROL_OPCODES:
                await self._handle_control(frame)
                continue
            if frame.opcode == OP_CONT:
                if not fragments:
                    raise ProtocolError("收到没有起始帧的 continuation 帧")
                fragments.append(frame.payload)
            else:
                if fragments:
                    raise ProtocolError("上一条分片消息尚未结束")
                opcode, fragments = frame.opcode, [frame.payload]
                if frame.fin:
                    return self._decode(opcode, fragments)
            if sum(len(chunk) for chunk in fragments) > self.max_size:
                raise ProtocolError(f"消息超过上限 {self.max_size} 字节", code=CLOSE_TOO_LARGE)
            if frame.fin:
                return self._decode(opcode, fragments)

    async def _handle_control(self, frame: Frame) -> None:
        if frame.opcode == OP_PING:
            await self._send_frame(OP_PONG, frame.payload)
            return
        if frame.opcode == OP_PONG:
            return
        code, reason = self._parse_close(frame.payload)
        self._close_received = True
        self.close_code, self.close_reason = code, reason
        if not self._close_sent:
            await self._send_close(code, reason)
        await self._shutdown()
        raise ConnectionClosed(code, reason)

    def _decode(self, opcode: int, fragments: list[bytes]) -> str | bytes:
        payload = b"".join(fragments)
        if opcode == OP_BINARY:
            return payload
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("文本消息不是合法 UTF-8", code=1007) from exc

    def _parse_close(self, payload: bytes) -> tuple[int, str]:
        if not payload:
            return 1005, ""
        if len(payload) == 1:
            raise ProtocolError("close 帧载荷非法")
        code = struct.unpack("!H", payload[:2])[0]
        if code < 1000 or code > 4999 or code in _FORBIDDEN_CLOSE_CODES:
            raise ProtocolError(f"close 状态码非法：{code}")
        try:
            return code, payload[2:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("close reason 不是合法 UTF-8", code=1007) from exc

    async def _read_frame(self) -> Frame:
        """取下一个帧（必要时读传输），并做 RSV/控制帧/opcode 校验。"""
        while not self._pending:
            data = await self._transport.read(_READ_CHUNK)
            if not data:
                self.close_code = self.close_code or 1006
                await self._shutdown()
                raise ConnectionClosed(self.close_code, self.close_reason or "对端断开 TCP 连接")
            for frame in self._decoder.feed(data):
                if frame.rsv:
                    raise ProtocolError("RSV 位非零（未协商扩展）")
                if frame.opcode in _CONTROL_OPCODES:
                    if not frame.fin:
                        raise ProtocolError("控制帧不得分片")
                    if len(frame.payload) > 125:
                        raise ProtocolError("控制帧载荷不得超过 125 字节")
                elif frame.opcode not in _DATA_OPCODES:
                    raise ProtocolError(f"未知 opcode {frame.opcode}")
                self._pending.append(frame)
        self._last_rx = asyncio.get_running_loop().time()
        return self._pending.popleft()

    async def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """发起关闭握手：发 close 帧、等对端回应（``close_timeout`` 后强制断开）。"""
        if self._closed:
            return
        if not self._close_sent:
            await self._send_close(code, reason)
            self.close_code, self.close_reason = code, reason
        try:
            await asyncio.wait_for(self._drain_until_close(), self.close_timeout)
        except (TimeoutError, ConnectionClosed, WebSocketError, OSError):
            pass
        await self._shutdown()

    async def _send_close(self, code: int, reason: str) -> None:
        self._close_sent = True
        payload = struct.pack("!H", code) + reason.encode("utf-8") if code else b""
        try:
            await self._send_frame(OP_CLOSE, payload)
        except (OSError, WebSocketError):
            pass

    async def _drain_until_close(self) -> None:
        """等待对端的 close 帧（顺带回 PING）。"""
        while not self._close_received:
            frame = await self._read_frame()
            if frame.opcode == OP_CLOSE:
                self.close_code, self.close_reason = self._parse_close(frame.payload)
                self._close_received = True
                return
            if frame.opcode == OP_PING:
                await self._send_frame(OP_PONG, frame.payload)

    async def _shutdown(self) -> None:
        """取消保活任务并关闭传输（幂等）。"""
        if self._closed:
            return
        self._closed = True
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None
        await self._transport.close()

    async def _fail(self, code: int, reason: str) -> None:
        """发 close 帧后断开（本函数总会抛 :class:`ConnectionClosed`）。"""
        await self._send_close(code, reason)
        self.close_code, self.close_reason = code, reason
        await self._shutdown()
        raise ConnectionClosed(code, reason)

    def _ensure_open(self) -> None:
        if self._close_sent or self._closed:
            raise ConnectionClosed(self.close_code or 1006, "连接已关闭")

    def start_keepalive(self) -> None:
        """启动 ping/pong 保活任务（``ping_interval`` 非空时才有实际动作）。"""
        if self.ping_interval and self._keepalive is None:
            self._last_rx = asyncio.get_running_loop().time()
            self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = float(self.ping_interval or 0)
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if self._closed:
                    return
                if loop.time() - self._last_rx > interval + self.ping_timeout:
                    self.close_code, self.close_reason = 1006, "保活超时（未收到 PONG）"
                    await self._shutdown()
                    return
                await self._send_frame(OP_PING, b"")
        except asyncio.CancelledError:
            raise
        except (OSError, WebSocketError):
            await self._shutdown()

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[str | bytes]:
        """迭代消息；对端正常关闭时结束迭代。"""
        while True:
            try:
                yield await self.recv()
            except ConnectionClosed:
                return


class WebSocketClient(WebSocketConnection):
    """客户端连接（``await connect(...)`` 的返回值），可作异步上下文管理器。"""

    def __init__(self, transport: _Transport, **kwargs: Any) -> None:
        super().__init__(transport, is_client=True, **kwargs)
        self.start_keepalive()

    async def __aenter__(self) -> "WebSocketClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# --- 客户端 -----------------------------------------------------------------
def _default_proxy(secure: bool) -> str:
    names = ("HTTPS_PROXY", "https_proxy") if secure else ("HTTP_PROXY", "http_proxy")
    return next((os.environ[name] for name in names if os.environ.get(name)), "")


async def _open_stream(
    host: str, port: int, *, secure: bool, context: ssl.SSLContext | None,
    proxy_url: str, target: str, open_timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """建立到服务端（或经代理）的字节流，返回 ``(reader, writer, 请求目标)``。"""
    if proxy_url:
        parts = urllib.parse.urlsplit(proxy_url)
        reader, writer = await asyncio.open_connection(parts.hostname, parts.port or 8080)
        if not secure:  # 明文代理：用绝对 URI 请求行
            return reader, writer, target
        writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        await writer.drain()
        status, _, _ = parse_http_response(
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), open_timeout))
        if status != 200:
            writer.close()
            raise HandshakeError(f"代理 CONNECT 失败：HTTP {status}", status=status)
        await writer.start_tls(context, server_hostname=host)  # type: ignore[arg-type]
        return reader, writer, target
    reader, writer = await asyncio.open_connection(
        host, port, ssl=context, server_hostname=host if secure else None)
    return reader, writer, target


async def connect(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    subprotocols: list[str] | tuple[str, ...] = (),
    origin: str = "",
    proxy: str = "",
    verify_tls: bool = True,
    ssl_context: ssl.SSLContext | None = None,
    open_timeout: float = 10.0,
    max_size: int = DEFAULT_MAX_SIZE,
    read_timeout: float | None = None,
    ping_interval: float | None = None,
    ping_timeout: float = 20.0,
    close_timeout: float = 5.0,
) -> WebSocketClient:
    """建立 WebSocket 客户端连接（``ws://`` / ``wss://``）。

    ``proxy`` 为空时读 ``HTTPS_PROXY``/``HTTP_PROXY``：``wss://`` 走 CONNECT 隧道，
    ``ws://`` 用绝对 URI 请求行（与 :mod:`atri_x.net.httpclient` 一致）。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise HandshakeError(f"不支持的 scheme：{parts.scheme!r}（应为 ws/wss）")
    host = parts.hostname or ""
    if not host:
        raise HandshakeError(f"URL 缺少主机名：{url!r}")
    secure = parts.scheme == "wss"
    port = parts.port or (443 if secure else 80)
    target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    proxy_url = proxy or _default_proxy(secure)
    if proxy_url and not secure:
        target = urllib.parse.urlunsplit(
            (f"http{parts.scheme[2:]}", parts.netloc, parts.path or "/", parts.query, ""))
    context = ssl_context
    if secure and context is None:
        context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()  # noqa: SLF001
    try:
        reader, writer, target = await asyncio.wait_for(
            _open_stream(host, port, secure=secure, context=context, proxy_url=proxy_url,
                         target=target, open_timeout=open_timeout), open_timeout)
    except HandshakeError:
        raise
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
        raise HandshakeError(f"连接 {url} 失败：{exc}") from exc
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    try:
        writer.write(build_client_request(host=parts.netloc, target=target, key=key,
                                          subprotocols=subprotocols, headers=headers,
                                          origin=origin).encode("ascii"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), open_timeout)
        status, reason, response_headers = parse_http_response(raw)
        if status != 101:
            raise HandshakeError(f"握手被拒绝：HTTP {status} {reason}", status=status, response=raw)
        if not _has_token(response_headers.get("upgrade", ""), "websocket"):
            raise HandshakeError("响应缺少 Upgrade: websocket", status=101, response=raw)
        if not _has_token(response_headers.get("connection", ""), "upgrade"):
            raise HandshakeError("响应缺少 Connection: Upgrade", status=101, response=raw)
        expected = accept_key(key)
        if response_headers.get("sec-websocket-accept", "") != expected:
            raise HandshakeError(f"Sec-WebSocket-Accept 校验失败：期望 {expected}",
                                 status=101, response=raw)
        chosen = response_headers.get("sec-websocket-protocol", "")
        if chosen and chosen not in list(subprotocols):
            raise HandshakeError(f"服务端选择了未提供的子协议：{chosen!r}", response=raw)
    except HandshakeError:
        writer.close()
        raise
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        writer.close()
        raise HandshakeError(f"握手响应失败：{exc}") from exc
    client = WebSocketClient(
        StreamTransport(reader, writer), max_size=max_size, read_timeout=read_timeout,
        ping_interval=ping_interval, ping_timeout=ping_timeout, close_timeout=close_timeout,
        subprotocol=chosen)
    client.request_headers = {"Sec-WebSocket-Key": key, "Host": parts.netloc}
    client.response_headers = response_headers
    return client


# --- 服务端 -----------------------------------------------------------------
async def _read_http_request(reader: asyncio.StreamReader, timeout: float) -> HandshakeRequest:
    """从一个裸字节流读取 HTTP 握手请求。"""
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        raise HandshakeError(f"请求行非法：{lines[0]!r}", status=400)
    headers = _parse_head(lines[1:])
    return HandshakeRequest(parts[0], parts[1], parts[2], headers, headers.get("origin", ""))


def _make_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, request: HandshakeRequest,
    subprotocol: str, options: Mapping[str, Any],
) -> WebSocketConnection:
    """构造服务端连接对象并绑定握手元信息。"""
    connection = WebSocketConnection(
        StreamTransport(reader, writer), is_client=False, subprotocol=subprotocol, **options)
    connection.request = request
    connection.request_headers = dict(request.headers)
    connection.response_headers = {"Sec-WebSocket-Protocol": subprotocol}
    connection.start_keepalive()
    return connection


class WebSocketServer:
    """``await serve(...)`` 的返回值：包一层 ``asyncio`` 服务端。"""

    def __init__(self, server: asyncio.AbstractServer) -> None:
        self.server = server

    @property
    def port(self) -> int:
        """实际监听端口（``port=0`` 时由内核分配）。"""
        for sock in self.server.sockets or []:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError):  # pragma: no cover - 非 TCP socket
                continue
        return 0

    async def close(self) -> None:
        """停止监听并等待关闭完成。"""
        self.server.close()
        await self.server.wait_closed()


async def serve(
    handler: Callable[[WebSocketConnection], Awaitable[None] | None],
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    path: str = "/",
    subprotocols: list[str] | tuple[str, ...] = (),
    origins: list[str] | tuple[str, ...] | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    read_timeout: float | None = None,
    ping_interval: float | None = None,
    ping_timeout: float = 20.0,
    close_timeout: float = 5.0,
    handshake_timeout: float = 10.0,
    ssl_context: ssl.SSLContext | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> WebSocketServer:
    """启动 WebSocket 服务端；``handler(connection)`` 可以是协程也可以是普通函数。

    ``path`` 是必须匹配的路径（忽略查询串）；``origins`` 非空时校验 Origin；握手失败回
    4xx（版本不符回 426 并带 ``Sec-WebSocket-Version: 13``）。
    """
    options = {"max_size": max_size, "read_timeout": read_timeout,
               "ping_interval": ping_interval, "ping_timeout": ping_timeout,
               "close_timeout": close_timeout}

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection: WebSocketConnection | None = None
        try:
            request = await _read_http_request(reader, handshake_timeout)
            request_path = urllib.parse.urlsplit(request.path).path or "/"
            if request_path != path:
                writer.write(error_response(404, f"未知路径 {request_path}"))
                await writer.drain()
                return
            try:
                response, meta = server_handshake(request, subprotocols=subprotocols,
                                                  origins=origins)
            except HandshakeError as exc:
                writer.write(error_response(exc.status or 400, str(exc)))
                await writer.drain()
                return
            writer.write(response.encode("ascii"))
            await writer.drain()
            connection = _make_connection(reader, writer, request=request,
                                          subprotocol=meta["subprotocol"], options=options)
            result = handler(connection)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except (HandshakeError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            pass
        except BaseException as exc:  # noqa: BLE001 - 交给 on_error，避免静默吞错
            if on_error is not None:
                on_error(exc)
        finally:
            if connection is not None:
                await connection.close(CLOSE_GOING_AWAY, "server closing")
            else:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, RuntimeError):
                    pass

    return WebSocketServer(await asyncio.start_server(on_connect, host, port, ssl=ssl_context))


# --- http.server.BaseHTTPRequestHandler 兼容路径 ----------------------------
async def accept(
    handler: Any,
    *,
    subprotocols: list[str] | tuple[str, ...] = (),
    origins: list[str] | tuple[str, ...] | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    read_timeout: float | None = None,
    ping_interval: float | None = None,
    ping_timeout: float = 20.0,
    close_timeout: float = 5.0,
) -> WebSocketConnection:
    """把 ``BaseHTTPRequestHandler`` 的当前请求升级成 WebSocket，返回服务端连接。

    必须在 handler 方法里把会话跑完再返回（``socketserver`` 会在 handler 返回后关闭
    socket）；本函数接管该 socket（改为非阻塞并交给事件循环）。用法::

        class Ops(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                with accept_sync(self, subprotocols=["atri-x.v1"]) as connection:
                    for message in connection:
                        connection.send_text(f"echo: {message}")

    注：客户端若在 101 之前抢发帧，这些字节可能仍压在 handler 的 ``rfile`` 缓冲里而被
    丢弃（RFC 6455 §4.1 禁止客户端这么做，实践中不触发）。
    """
    headers = {str(name).lower(): str(value) for name, value in handler.headers.items()}
    request = HandshakeRequest(
        getattr(handler, "command", "GET"), getattr(handler, "path", "/"),
        getattr(handler, "request_version", "HTTP/1.1"), headers, headers.get("origin", ""))
    response, meta = server_handshake(request, subprotocols=subprotocols, origins=origins)
    handler.wfile.write(response.encode("ascii"))
    handler.wfile.flush()
    handler.close_connection = True  # 让 BaseHTTPRequestHandler 不再复用这条连接
    sock = handler.connection
    sock.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=sock)
    return _make_connection(
        reader, writer, request=request, subprotocol=meta["subprotocol"],
        options={"max_size": max_size, "read_timeout": read_timeout,
                 "ping_interval": ping_interval, "ping_timeout": ping_timeout,
                 "close_timeout": close_timeout})


# --- 同步 API ---------------------------------------------------------------
class _LoopThread:
    """后台事件循环线程：把协程提交到它上面执行。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="atri-x-ws-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine: Awaitable[Any], timeout: float | None = None) -> Any:
        """在后台循环上跑协程并阻塞等待结果。"""
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)  # type: ignore[arg-type]

    def shutdown(self) -> None:
        """停止循环并回收线程。"""
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)
        self.loop.close()


class SyncWebSocket:
    """同步 WebSocket：后台事件循环线程托管异步连接，成员按需代理。

    ``send_text`` / ``send_bytes`` / ``ping`` / ``recv`` / ``close`` 等协程方法会**同步**
    执行，可多传 ``timeout`` 关键字作为同步等待上限；其余属性（``connection``、
    ``close_code``、``close_reason``、``subprotocol``…）直接透传。支持 ``with`` 与
    ``for message in ws``。
    """

    def __init__(self, connection: WebSocketConnection, loop: _LoopThread) -> None:
        self.connection, self._loop = connection, loop

    @classmethod
    def connect(cls, url: str, **kwargs: Any) -> "SyncWebSocket":
        """同步建立客户端连接（参数同 :func:`connect`）。"""
        return cls._wrap(connect(url, **kwargs))

    @classmethod
    def _wrap(cls, coroutine: Awaitable[WebSocketConnection]) -> "SyncWebSocket":
        loop = _LoopThread()
        try:
            return cls(loop.call(coroutine, 30.0), loop)
        except BaseException:
            loop.shutdown()
            raise

    def __getattr__(self, name: str) -> Any:
        """协程成员包成同步调用，其余属性直接透传。"""
        attribute = getattr(self.connection, name)
        if not inspect.iscoroutinefunction(attribute):
            return attribute
        def sync_call(*args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
            return self._loop.call(attribute(*args, **kwargs), timeout)
        return sync_call

    def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """关闭连接并回收后台事件循环。"""
        try:
            self._loop.call(self.connection.close(code, reason), 10.0)
        finally:
            self._loop.shutdown()

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        try:
            return self.recv()
        except ConnectionClosed:
            raise StopIteration from None

    def __enter__(self) -> "SyncWebSocket":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def connect_sync(url: str, **kwargs: Any) -> SyncWebSocket:
    """同步版 :func:`connect`。"""
    return SyncWebSocket.connect(url, **kwargs)


def accept_sync(handler: Any, **kwargs: Any) -> SyncWebSocket:
    """同步版 :func:`accept`（在 ``BaseHTTPRequestHandler`` 内使用，见其 docstring）。"""
    return SyncWebSocket._wrap(accept(handler, **kwargs))  # noqa: SLF001
