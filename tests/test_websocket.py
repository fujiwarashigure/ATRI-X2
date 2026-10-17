"""``atri_x.net.websocket`` 的单元测试。

分三层：纯函数（帧编解码 / 握手）、真实回环（本地 ``asyncio`` 服务端 ↔ 客户端，
覆盖文本/二进制/分片/ping-pong/关闭/超限/子协议）、以及 ``BaseHTTPRequestHandler``
与同步 API 路径。回环测试真的起 TCP 服务端，不打桩。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import http.server
import itertools
import os
import shutil
import socket
import struct
import threading
import unittest
from collections.abc import Iterator

from atri_x.net.websocket import (
    CLOSE_GOING_AWAY,
    CLOSE_NORMAL,
    CLOSE_POLICY_VIOLATION,
    CLOSE_PROTOCOL_ERROR,
    CLOSE_TOO_LARGE,
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    ConnectionClosed,
    FrameDecoder,
    HandshakeError,
    HandshakeRequest,
    ProtocolError,
    SyncWebSocket,
    WebSocketConnection,
    accept_key,
    accept_sync,
    apply_mask,
    build_client_request,
    connect,
    connect_sync,
    encode_frame,
    error_response,
    parse_http_response,
    server_handshake,
    serve,
)
from atri_x.net.websocket import _LoopThread  # 测试同步 API 时要复用后台循环线程

_TMP_BASE = os.path.dirname(os.path.abspath(__file__))
_TMP_SEQ = itertools.count()


@contextlib.contextmanager
def _tempdir() -> Iterator[str]:
    """工作区内临时目录（不用 tempfile.mkdtemp：它建的 0o700 目录在本机沙箱下不可写）。"""
    path = os.path.join(_TMP_BASE, f"tmp-ws-{os.getpid()}-{next(_TMP_SEQ)}")
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run(coro):
    return asyncio.run(coro)


def _valid_request(**overrides: str) -> HandshakeRequest:
    headers = {
        "host": "127.0.0.1:9000",
        "upgrade": "websocket",
        "connection": "Upgrade",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
        "sec-websocket-version": "13",
    }
    headers.update(overrides)
    return HandshakeRequest("GET", "/ws", "HTTP/1.1", headers)


class TestFrameCodec(unittest.TestCase):
    """帧编解码是纯函数，逐字节喂入也必须能正确重组。"""

    def test_apply_mask_roundtrip(self) -> None:
        data = b"hello world"
        key = b"\x01\x02\x03\x04"
        masked = apply_mask(data, key)
        self.assertNotEqual(masked, data)
        self.assertEqual(apply_mask(masked, key), data)
        self.assertEqual(apply_mask(data, b""), data)

    def test_length_forms_roundtrip(self) -> None:
        for size in (0, 1, 125, 126, 127, 65535, 65536):
            with self.subTest(size=size):
                payload = bytes(range(256)) * (size // 256) + bytes(size % 256)
                for mask in (False, True):
                    frame = encode_frame(OP_BINARY, payload, mask=mask)
                    decoder = FrameDecoder(expect_mask=mask)
                    decoded = decoder.feed(frame)
                    self.assertEqual(len(decoded), 1)
                    self.assertEqual(decoded[0].payload, payload)
                    self.assertEqual(decoded[0].opcode, OP_BINARY)
                    self.assertTrue(decoded[0].fin)
                    self.assertEqual(decoded[0].masked, mask)
                    if mask:
                        self.assertEqual(frame[1], 0x80 | 126 if size == 126 else frame[1])

    def test_length_encoding_widths(self) -> None:
        self.assertEqual(len(encode_frame(OP_TEXT, b"x" * 125, mask=False)), 127)
        self.assertEqual(encode_frame(OP_TEXT, b"", mask=False)[:2], b"\x81\x00")
        header126 = encode_frame(OP_TEXT, b"x" * 126, mask=False)[:4]
        self.assertEqual(header126[1], 126)
        self.assertEqual(struct.unpack("!H", header126[2:4])[0], 126)
        header127 = encode_frame(OP_TEXT, b"x" * 65536, mask=False)[:10]
        self.assertEqual(header127[1], 127)
        self.assertEqual(struct.unpack("!Q", header127[2:10])[0], 65536)

    def test_fin_and_continuation_flags(self) -> None:
        self.assertEqual(encode_frame(OP_TEXT, b"a", mask=False, fin=False)[0], 0x01)
        self.assertEqual(encode_frame(OP_CONT, b"a", mask=False)[0], 0x80)
        self.assertEqual(encode_frame(OP_CLOSE, b"", mask=False)[0], 0x88)

    def test_incremental_feeding(self) -> None:
        payload = b"fragmented across reads" * 10
        frame = encode_frame(OP_TEXT, payload, mask=True)
        decoder = FrameDecoder(expect_mask=True)
        produced = []
        for index in range(len(frame)):
            for item in decoder.feed(frame[index : index + 1]):
                produced.append(item)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].payload, payload)

    def test_multiple_frames_in_one_feed(self) -> None:
        blob = (
            encode_frame(OP_TEXT, b"one", mask=False)
            + encode_frame(OP_PING, b"p", mask=False)
            + encode_frame(OP_BINARY, b"two", mask=False, fin=False)
        )
        frames = FrameDecoder().feed(blob)
        self.assertEqual([(f.opcode, f.payload, f.fin) for f in frames],
                         [(OP_TEXT, b"one", True), (OP_PING, b"p", True), (OP_BINARY, b"two", False)])

    def test_mask_expectation_is_enforced(self) -> None:
        masked = encode_frame(OP_TEXT, b"x", mask=True)
        unmasked = encode_frame(OP_TEXT, b"x", mask=False)
        with self.assertRaises(ProtocolError):
            FrameDecoder(expect_mask=False).feed(masked)  # 服务端帧不得掩码
        with self.assertRaises(ProtocolError):
            FrameDecoder(expect_mask=True).feed(unmasked)  # 客户端帧必须掩码

    def test_rsv_and_oversize_are_rejected(self) -> None:
        bad_rsv = bytes([0x80 | 0x40 | OP_TEXT, 0x01]) + b"x"
        frames = FrameDecoder().feed(bad_rsv)
        self.assertEqual(frames[0].rsv, 0x40)  # 解码器只报告，由连接层裁决
        big = encode_frame(OP_BINARY, b"x" * 2000, mask=False)
        with self.assertRaises(ProtocolError) as ctx:
            FrameDecoder(max_frame_size=100).feed(big)
        self.assertEqual(ctx.exception.code, CLOSE_TOO_LARGE)


class TestHandshakeHelpers(unittest.TestCase):
    def test_accept_key_rfc6455_vector(self) -> None:
        # RFC 6455 §1.3 的示例
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_server_handshake_accepts_and_negotiates(self) -> None:
        request = _valid_request(**{"sec-websocket-protocol": "chat, atri-x.v1"})
        response, meta = server_handshake(request, subprotocols=["atri-x.v1"])
        self.assertTrue(response.startswith("HTTP/1.1 101 Switching Protocols\r\n"))
        self.assertIn("Upgrade: websocket\r\n", response)
        self.assertIn("Connection: Upgrade\r\n", response)
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n", response)
        self.assertIn("Sec-WebSocket-Protocol: atri-x.v1\r\n", response)
        self.assertEqual(meta["subprotocol"], "atri-x.v1")

    def test_server_handshake_without_common_subprotocol(self) -> None:
        request = _valid_request(**{"sec-websocket-protocol": "chat"})
        response, meta = server_handshake(request, subprotocols=["atri-x.v1"])
        self.assertNotIn("Sec-WebSocket-Protocol", response)
        self.assertEqual(meta["subprotocol"], "")

    def test_server_handshake_rejections(self) -> None:
        cases = [
            (_valid_request(), {"origins": ["https://evil.example"]}, 403),
            (HandshakeRequest("POST", "/ws", "HTTP/1.1", _valid_request().headers), {}, 405),
            (_valid_request(upgrade="h2c"), {}, 400),
            (_valid_request(connection="keep-alive"), {}, 400),
            (_valid_request(**{"sec-websocket-version": "8"}), {}, 426),
            (_valid_request(**{"sec-websocket-key": ""}), {}, 400),
            (HandshakeRequest("GET", "/ws", "HTTP/1.0", {"upgrade": "websocket"}), {}, 400),
        ]
        for request, kwargs, status in cases:
            with self.subTest(status=status):
                with self.assertRaises(HandshakeError) as ctx:
                    server_handshake(request, **kwargs)
                self.assertEqual(ctx.exception.status, status)

    def test_origin_allow_list(self) -> None:
        request = HandshakeRequest("GET", "/ws", "HTTP/1.1", _valid_request().headers,
                                   origin="https://ok.example")
        self.assertTrue(server_handshake(request, origins=["https://ok.example"])[0])
        self.assertTrue(server_handshake(request, origins=["*"])[0])

    def test_build_client_request(self) -> None:
        text = build_client_request(host="h:1", target="/ws?a=1", key="K",
                                    subprotocols=["a", "b"], headers={"X-Trace": "t"},
                                    origin="https://o")
        self.assertTrue(text.startswith("GET /ws?a=1 HTTP/1.1\r\n"))
        self.assertIn("Host: h:1\r\n", text)
        self.assertIn("Sec-WebSocket-Key: K\r\n", text)
        self.assertIn("Sec-WebSocket-Version: 13\r\n", text)
        self.assertIn("Sec-WebSocket-Protocol: a, b\r\n", text)
        self.assertIn("X-Trace: t\r\n", text)
        self.assertIn("Origin: https://o\r\n", text)
        self.assertTrue(text.endswith("\r\n\r\n"))

    def test_parse_http_response(self) -> None:
        raw = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nX-A: 1\r\n\r\nbody"
        status, reason, headers = parse_http_response(raw)
        self.assertEqual((status, reason), (101, "Switching Protocols"))
        self.assertEqual(headers["x-a"], "1")
        with self.assertRaises(HandshakeError):
            parse_http_response(b"garbage\r\n\r\n")

    def test_error_response(self) -> None:
        response = error_response(426, "need 13")
        self.assertIn(b"HTTP/1.1 426 Upgrade Required", response)
        self.assertIn(b"Sec-WebSocket-Version: 13", response)
        self.assertTrue(response.endswith(b"need 13"))


class TestLoopback(unittest.TestCase):
    """真实回环：本地 asyncio 服务端 ↔ 客户端。"""

    def _echo_server(self, *, subprotocols=(), path="/"):
        """返回 (handler, seen, serve 选项)。"""
        seen: dict[str, object] = {}

        async def handler(connection: WebSocketConnection) -> None:
            seen["request"] = connection.request
            seen["subprotocol"] = connection.subprotocol
            seen["request_headers"] = connection.request_headers
            async for message in connection:
                if isinstance(message, str):
                    await connection.send_text(f"echo:{message}")
                else:
                    await connection.send_bytes(b"echo:" + message)
            # ``async for`` 在对端关闭时正常结束，关闭码从连接上读。
            seen["server_close"] = (connection.close_code, connection.close_reason)

        return handler, seen, {"subprotocols": subprotocols, "path": path}

    def test_text_binary_and_close_roundtrip(self) -> None:
        handler, seen, options = self._echo_server()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0, **options)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/")
                self.assertEqual(await self._echo(client, "你好 ATRI"), "echo:你好 ATRI")
                self.assertEqual(await self._echo(client, b"\x00\x01\xff"), b"echo:\x00\x01\xff")
                await client.close(CLOSE_NORMAL, "done")
            finally:
                await server.close()

        _run(scenario())
        self.assertEqual(seen["server_close"], (CLOSE_NORMAL, "done"))
        self.assertEqual(seen["request"].path, "/")  # type: ignore[union-attr]

    async def _echo(self, client, payload):
        if isinstance(payload, str):
            await client.send_text(payload)
        else:
            await client.send_bytes(payload)
        return await asyncio.wait_for(client.recv(), 5)

    def test_client_masks_and_server_rejects_unmasked(self) -> None:
        """服务端必须拒绝未掩码的客户端帧（1002），反证客户端确实掩码。"""
        handler, _, _ = self._echo_server()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                sock = await asyncio.to_thread(self._raw_connect, server.port)
                try:
                    sock.sendall(encode_frame(OP_TEXT, b"nope", mask=False))
                    blob = await asyncio.to_thread(sock.recv, 4096)
                    frames = FrameDecoder(expect_mask=False).feed(blob)
                    self.assertEqual(frames[0].opcode, OP_CLOSE)
                    self.assertEqual(struct.unpack("!H", frames[0].payload[:2])[0],
                                     CLOSE_PROTOCOL_ERROR)
                finally:
                    sock.close()
            finally:
                await server.close()

        _run(scenario())

    def _raw_connect(self, port: int, path: str = "/") -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(build_client_request(host=f"127.0.0.1:{port}", target=path, key=key)
                     .encode("ascii"))
        raw = sock.recv(4096)
        status, _, headers = parse_http_response(raw)
        assert status == 101, raw
        assert headers["sec-websocket-accept"] == accept_key(key), headers
        return sock

    def test_fragmented_message_is_reassembled(self) -> None:
        handler, _, _ = self._echo_server()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/")
                await client._send_frame(OP_TEXT, "分".encode(), fin=False)
                await client._send_frame(OP_PING, b"mid")
                await client._send_frame(OP_CONT, "片".encode(), fin=False)
                await client._send_frame(OP_CONT, "消息".encode(), fin=True)
                self.assertEqual(await asyncio.wait_for(client.recv(), 5), "echo:分片消息")
                await client.close()
            finally:
                await server.close()

        _run(scenario())

    def test_ping_pong_and_keepalive(self) -> None:
        handler, _, _ = self._echo_server()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/", ping_interval=0.05,
                                       ping_timeout=1.0)
                await client.ping(b"probe")
                self.assertEqual(await self._echo(client, "after-ping"), "echo:after-ping")
                await asyncio.sleep(0.2)  # 保活任务在此期间发过 PING/PONG
                self.assertEqual(await self._echo(client, "still-alive"), "echo:still-alive")
                await client.close()
            finally:
                await server.close()

        _run(scenario())

    def test_server_initiated_ping_gets_pong(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:
            await connection.ping(b"from-server")
            await connection.send_text("ready")
            await asyncio.sleep(0.05)

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/")
                self.assertEqual(await asyncio.wait_for(client.recv(), 5), "ready")
                await client.close()
            finally:
                await server.close()

        _run(scenario())

    def test_async_iteration_stops_on_close(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:
            for index in range(3):
                await connection.send_text(str(index))
            await connection.close(CLOSE_NORMAL, "bye")

        async def scenario() -> list[str]:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/")
                received = [message async for message in client]
                self.assertEqual(client.close_code, CLOSE_NORMAL)
                self.assertEqual(client.close_reason, "bye")
                return list(received)
            finally:
                await server.close()

        self.assertEqual(_run(scenario()), ["0", "1", "2"])

    def test_max_size_closes_with_1009(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:
            with contextlib.suppress(ConnectionClosed):
                await connection.recv()

        async def scenario() -> ConnectionClosed:
            server = await serve(handler, "127.0.0.1", 0, max_size=64)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/")
                await client.send_text("x" * 200)
                with self.assertRaises(ConnectionClosed) as ctx:
                    await asyncio.wait_for(client.recv(), 5)
                return ctx.exception
            finally:
                await server.close()

        error = _run(scenario())
        self.assertEqual(error.code, CLOSE_TOO_LARGE)

    def test_read_timeout_closes_connection(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:
            await asyncio.sleep(1.0)

        async def scenario() -> ConnectionClosed:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/", read_timeout=0.1)
                with self.assertRaises(ConnectionClosed) as ctx:
                    await client.recv()
                return ctx.exception
            finally:
                await server.close()

        self.assertEqual(_run(scenario()).code, 1006)

    def test_subprotocol_custom_headers_and_path(self) -> None:
        handler, seen, _ = self._echo_server(subprotocols=["atri-x.v1"], path="/ws")

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0, subprotocols=["atri-x.v1"], path="/ws")
            try:
                client = await connect(f"ws://127.0.0.1:{server.port}/ws?token=1",
                                       subprotocols=["other", "atri-x.v1"],
                                       headers={"X-Trace": "abc"})
                self.assertEqual(client.subprotocol, "atri-x.v1")
                self.assertEqual(await self._echo(client, "ok"), "echo:ok")
                await client.close()
            finally:
                await server.close()

        _run(scenario())
        self.assertEqual(seen["subprotocol"], "atri-x.v1")
        self.assertEqual(seen["request_headers"]["x-trace"], "abc")  # type: ignore[index]
        self.assertEqual(seen["request"].path, "/ws?token=1")  # type: ignore[union-attr]

    def test_wrong_path_and_origin_are_rejected(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:  # pragma: no cover
            await connection.recv()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0, path="/ws",
                                 origins=["https://ok.example"])
            try:
                with self.assertRaises(HandshakeError) as ctx:
                    await connect(f"ws://127.0.0.1:{server.port}/nope")
                self.assertEqual(ctx.exception.status, 404)
                with self.assertRaises(HandshakeError) as ctx:
                    await connect(f"ws://127.0.0.1:{server.port}/ws",
                                  origin="https://evil.example")
                self.assertEqual(ctx.exception.status, 403)
            finally:
                await server.close()

        _run(scenario())

    def test_connection_refused_and_bad_scheme(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(HandshakeError):
                await connect("ws://127.0.0.1:1/", open_timeout=2)
            with self.assertRaises(HandshakeError):
                await connect("http://127.0.0.1:1/")

        _run(scenario())

    def test_unmasked_or_oversized_control_frame_is_protocol_error(self) -> None:
        handler, _, _ = self._echo_server()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                sock = await asyncio.to_thread(self._raw_connect, server.port)
                try:
                    sock.sendall(encode_frame(OP_PING, b"x" * 200, mask=True))
                    blob = await asyncio.to_thread(sock.recv, 4096)
                    frames = FrameDecoder(expect_mask=False).feed(blob)
                    self.assertEqual(frames[0].opcode, OP_CLOSE)
                    self.assertEqual(struct.unpack("!H", frames[0].payload[:2])[0],
                                     CLOSE_PROTOCOL_ERROR)
                finally:
                    sock.close()
            finally:
                await server.close()

        _run(scenario())

    def test_handshake_requires_get_and_websocket_upgrade(self) -> None:
        handler, _, _ = self._echo_server()

        async def scenario() -> None:
            server = await serve(handler, "127.0.0.1", 0)
            try:
                raw = await asyncio.to_thread(self._plain_get, server.port, "GET / HTTP/1.1\r\n\r\n")
                self.assertIn(b"400", raw.split(b"\r\n")[0])
                raw = await asyncio.to_thread(
                    self._plain_get, server.port,
                    "GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    "Sec-WebSocket-Version: 8\r\nSec-WebSocket-Key: k\r\n\r\n")
                self.assertIn(b"426", raw.split(b"\r\n")[0])
                self.assertIn(b"Sec-WebSocket-Version: 13", raw)
            finally:
                await server.close()

        _run(scenario())

    def _plain_get(self, port: int, request: str) -> bytes:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(request.encode())
            return sock.recv(4096)
        finally:
            sock.close()


class TestSyncApi(unittest.TestCase):
    """同步 API：后台事件循环线程托管异步连接。

    服务端也必须跑在**长期存活**的循环上——``asyncio.run(serve(...))`` 一返回循环就关了。
    """

    @contextlib.contextmanager
    def _background_server(self, handler, **kwargs) -> Iterator[object]:
        loop = _LoopThread()
        server = None
        try:
            server = loop.call(serve(handler, "127.0.0.1", 0, **kwargs), 10.0)
            yield server
        finally:
            if server is not None:
                with contextlib.suppress(Exception):
                    loop.call(server.close(), 5.0)
            loop.shutdown()

    def test_connect_sync_roundtrip(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:
            async for message in connection:
                if isinstance(message, str):
                    await connection.send_text(f"echo:{message}")
                else:
                    await connection.send_bytes(b"echo:" + message)

        with self._background_server(handler) as server:
            with connect_sync(f"ws://127.0.0.1:{server.port}/") as client:  # type: ignore[attr-defined]
                self.assertIsInstance(client, SyncWebSocket)
                client.send_text("sync-hello")
                self.assertEqual(client.recv(timeout=5), "echo:sync-hello")
                client.send_bytes(b"bin")
                self.assertEqual(client.recv(timeout=5), b"echo:bin")
                client.ping(b"p")

    def test_sync_iteration_ends_on_close(self) -> None:
        async def handler(connection: WebSocketConnection) -> None:
            await connection.send_text("only")
            await connection.close(CLOSE_NORMAL, "bye")

        with self._background_server(handler) as server:
            client = connect_sync(f"ws://127.0.0.1:{server.port}/")  # type: ignore[attr-defined]
            self.assertEqual([message for message in client], ["only"])
            self.assertEqual(client.close_code, CLOSE_NORMAL)
            client.close()


class TestBaseHTTPRequestHandlerPath(unittest.TestCase):
    """``http.server`` 兼容路径：真起 ThreadingHTTPServer 做回环。"""

    def test_accept_sync_inside_handler(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with connect_sync(f"ws://127.0.0.1:{port}/ops", subprotocols=["atri-x.v1"],
                              origin="https://ok.example") as client:
                self.assertEqual(client.subprotocol, "atri-x.v1")
                client.send_text("ping-text")
                self.assertEqual(client.recv(timeout=5), "echo:ping-text")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_accept_sync_returns_the_handler_connection(self) -> None:
        _EchoHandler.seen.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with connect_sync(f"ws://127.0.0.1:{server.server_address[1]}/ops",
                              origin="https://ok.example") as client:
                client.send_text("hi")
                self.assertEqual(client.recv(timeout=5), "echo:hi")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(_EchoHandler.seen["origin"], "https://ok.example")
        self.assertEqual(_EchoHandler.seen["path"], "/ops")

    def test_origin_check_rejects_other_origin(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HandshakeError) as ctx:
                connect_sync(f"ws://127.0.0.1:{server.server_address[1]}/ops",
                             origin="https://evil.example")
            self.assertEqual(ctx.exception.status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """把 ``/ops`` 升级成 WebSocket 并原样回显（演示 accept_sync 用法）。"""

    seen: dict[str, str] = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server 规定的方法名
        try:
            connection = accept_sync(self, subprotocols=["atri-x.v1"],
                                     origins=["https://ok.example"])
        except HandshakeError as exc:
            # 不能用 self.send_error：它按 latin-1 编码，中文原因会直接抛 UnicodeEncodeError。
            self.wfile.write(error_response(exc.status or 400, str(exc)))
            self.wfile.flush()
            self.close_connection = True
            return
        _EchoHandler.seen["origin"] = connection.connection.request.origin  # type: ignore[union-attr]
        _EchoHandler.seen["path"] = connection.connection.request.path  # type: ignore[union-attr]
        with connection:
            for message in connection:
                if isinstance(message, str):
                    connection.send_text(f"echo:{message}")
                else:
                    connection.send_bytes(b"echo:" + message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
