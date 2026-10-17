"""``atri_x.net.httpclient`` 的端到端测试。

用本地 ``ThreadingHTTPServer`` 起真实 HTTP 服务端，**不 mock HTTP 层**。

包含一条针对真实 P0 的回归测试：``HTTPStream._pump`` 在工作线程里调用
``asyncio.get_running_loop()`` 会抛 ``RuntimeError``，而它发生在 ``try`` 之前，
线程静默死亡 → ``_ready`` 永不置位 → ``open_stream()`` **永久挂起**（不是超时）。
见 :class:`TestStreamRegression`。
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from atri_x.net.httpclient import HTTPClient, HTTPError, build_multipart, iter_sse

#: 所有 async 场景的硬超时：只要挂住就失败，而不是让测试卡死。
HARD_TIMEOUT = 5.0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # 静音
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/query"):
            payload = json.dumps({"path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/json"):
            payload = json.dumps({"ok": True, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/text"):
            payload = "你好".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/status/"):
            code = int(self.path.rsplit("/", 1)[1])
            payload = b"boom"
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/bt"):
            payload = "炸了".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/sse-error"):
            # 必须排在 "/sse" 之前：更具体的路径先判，否则会被 /sse 吞掉
            payload = b'{"error": {"message": "bad key", "type": "auth"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for line in (
                ": 注释行\r\n",
                "data: 第一块\r\n",
                "\r\n",
                "data: 第二块\r\n",
                "data: 拼接行\r\n",
                "\r\n",
                "data: [DONE]\r\n",
                "\r\n",
            ):
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        if self.path.startswith("/echo"):
            payload = json.dumps(
                {
                    "len": len(body),
                    "content_type": self.headers.get("Content-Type", ""),
                    "body": body.decode("utf-8", errors="replace"),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/sse-post"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: chunk-{i}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True
            return
        if self.path.startswith("/query"):
            payload = json.dumps({"path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        self._read_body()
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class _Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


class _ServerTestCase(unittest.TestCase):
    server: _Server

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    def client(self, **kw: object) -> HTTPClient:
        return HTTPClient(base_url=self.server.base_url, timeout=HARD_TIMEOUT, **kw)  # type: ignore[arg-type]


def async_test(coro_fn):
    """把 async 测试函数跑在受硬超时保护的 :func:`asyncio.run` 里。"""

    def wrapper(self):
        async def guarded():
            return await asyncio.wait_for(coro_fn(self), HARD_TIMEOUT)

        return asyncio.run(guarded())

    wrapper.__name__ = coro_fn.__name__
    return wrapper


class TestBufferedRequests(_ServerTestCase):
    @async_test
    async def test_get_json(self) -> None:
        resp = await self.client().get("/json")
        self.assertEqual(200, resp.status)
        self.assertTrue(resp.ok)
        self.assertEqual({"ok": True, "path": "/json"}, resp.json())

    @async_test
    async def test_utf8_text(self) -> None:
        resp = await self.client().get("/text")
        self.assertEqual("你好", resp.text())

    @async_test
    async def test_post_json_sets_content_type(self) -> None:
        resp = await self.client().post("/echo", json={"a": 1})
        data = resp.json()
        self.assertEqual("application/json", data["content_type"])
        self.assertEqual('{"a":1}', data["body"])

    @async_test
    async def test_query_params(self) -> None:
        resp = await self.client().get("/query", params={"a": "1", "b": "中"})
        self.assertIn("a=1", resp.json()["path"])

    @async_test
    async def test_status_not_raised(self) -> None:
        resp = await self.client().get("/status/503")
        self.assertEqual(503, resp.status)
        self.assertFalse(resp.ok)
        self.assertEqual("boom", resp.text())

    @async_test
    async def test_utf8_error_body(self) -> None:
        resp = await self.client().get("/bt")
        self.assertEqual("炸了", resp.text())

    @async_test
    async def test_put_with_body(self) -> None:
        resp = await self.client().put("/anything", body=b"x")
        self.assertEqual(200, resp.status)

    @async_test
    async def test_connection_error_is_http_error(self) -> None:
        client = HTTPClient(base_url="http://127.0.0.1:1", timeout=1.0)
        with self.assertRaises(HTTPError):
            await client.get("/nope")

    @async_test
    async def test_resolve_keeps_absolute_url(self) -> None:
        client = self.client()
        self.assertEqual("https://x.test/a", client.resolve("https://x.test/a"))


class TestMultipart(_ServerTestCase):
    @async_test
    async def test_upload(self) -> None:
        resp = await self.client().request_multipart(
            "POST",
            "/echo",
            fields={"model": "whisper"},
            files=[("file", "a.wav", b"\x00\x01abc", "audio/wav")],
        )
        data = resp.json()
        self.assertTrue(data["content_type"].startswith("multipart/form-data; boundary="))
        self.assertIn("whisper", data["body"])
        self.assertIn('filename="a.wav"', data["body"])
        self.assertIn("audio/wav", data["body"])

    def test_build_multipart_shape(self) -> None:
        body = build_multipart({"k": "v"}, [("f", "n.bin", b"\xff", "application/octet-stream")], "B")
        self.assertTrue(body.startswith(b"--B\r\n"))
        self.assertTrue(body.endswith(b"--B--\r\n"))
        self.assertIn(b'name="k"', body)
        self.assertIn(b"\xff", body)


class TestStreamRegression(_ServerTestCase):
    """回归：``open_stream`` 必须真的能开流，且**不得挂起**。

    修复前的表现是：线程抛 ``RuntimeError: no running event loop`` → ``_ready``
    永不置位 → ``open_stream`` 永久 hang（而不是抛错或超时）。
    """

    @async_test
    async def test_post_sse_stream(self) -> None:
        stream = await self.client().open_stream("POST", "/sse-post", json={})
        try:
            self.assertEqual(200, stream.status)
            self.assertIn("text/event-stream", stream.headers.get("Content-Type", ""))
            lines = [line async for line in stream.lines()]
        finally:
            await stream.close()
        self.assertEqual(["data: chunk-0", "", "data: chunk-1", "", "data: chunk-2", ""], lines)

    @async_test
    async def test_get_sse_stream_keeps_crlf_stripped(self) -> None:
        stream = await self.client().open_stream("GET", "/sse")
        try:
            self.assertEqual(200, stream.status)
            lines = [line async for line in stream.lines()]
        finally:
            await stream.close()
        self.assertNotIn(": 注释行\r", lines)
        self.assertIn(": 注释行", lines)

    @async_test
    async def test_stream_error_status_is_visible(self) -> None:
        stream = await self.client().open_stream("GET", "/sse-error")
        try:
            self.assertEqual(401, stream.status)
            # 状态码先可见，再读错误体 —— 这正是 HTTPStream 存在的理由
            body = await stream.read_all()
        finally:
            await stream.close()
        self.assertIn(b"bad key", body)

    @async_test
    async def test_iter_sse_parses_events(self) -> None:
        stream = await self.client().open_stream("GET", "/sse")
        try:
            events = [event async for event in iter_sse(stream.lines())]
        finally:
            await stream.close()
        data = [e.data for e in events]
        self.assertIn("第一块", data)
        # 连续两行 data 会用一个空行结束，拼成多行 data
        self.assertIn("第二块\n拼接行", data)
        self.assertIn("[DONE]", data)

    @async_test
    async def test_stream_lines_convenience(self) -> None:
        lines = [
            line
            async for line in self.client().stream_lines("POST", "/sse-post", json={})
        ]
        self.assertEqual(6, len(lines))

    @async_test
    async def test_open_stream_connection_error_raises_not_hangs(self) -> None:
        client = HTTPClient(base_url="http://127.0.0.1:1", timeout=0.5)
        with self.assertRaises(HTTPError):
            await client.open_stream("GET", "/x")


class TestCancelAndTimeout(_ServerTestCase):
    @async_test
    async def test_cancel_token_aborts_stream(self) -> None:
        class Cancel:
            def __init__(self) -> None:
                self.calls = 0

            def is_cancelled(self) -> bool:
                self.calls += 1
                return self.calls > 3

        stream = await self.client().open_stream("GET", "/sse", cancel=Cancel())
        try:
            with self.assertRaises(asyncio.CancelledError):
                async for _ in stream.lines():
                    pass
        finally:
            await stream.close()

    @async_test
    async def test_retry_policy_retries_server_errors(self) -> None:
        from atri_x.net.httpclient import RetryPolicy

        seen: list[int] = []
        client = HTTPClient(base_url=self.server.base_url, timeout=HARD_TIMEOUT)
        resp = await client.get(
            "/status/503",
            retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.02),
            retry_notifier=lambda ctx, ev: seen.append(ev.attempt),
        )
        self.assertEqual(503, resp.status)
        self.assertEqual([1, 2], seen)

    @async_test
    async def test_no_retry_by_default(self) -> None:
        seen: list[int] = []
        client = HTTPClient(base_url=self.server.base_url, timeout=HARD_TIMEOUT)
        resp = await client.get(
            "/status/500", retry_notifier=lambda ctx, ev: seen.append(ev.attempt)
        )
        self.assertEqual(500, resp.status)
        self.assertEqual([], seen)


if __name__ == "__main__":
    unittest.main()
