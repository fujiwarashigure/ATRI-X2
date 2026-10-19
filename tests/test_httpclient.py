"""``atri_x.net.httpclient`` 的端到端测试。

用本地 ``ThreadingHTTPServer`` 起真实 HTTP 服务端，**不 mock HTTP 层**。

包含一条针对真实 P0 的回归测试：``HTTPStream._pump`` 在工作线程里调用
``asyncio.get_running_loop()`` 会抛 ``RuntimeError``，而它发生在 ``try`` 之前，
线程静默死亡 → ``_ready`` 永不置位 → ``open_stream()`` **永久挂起**（不是超时）。
见 :class:`TestStreamRegression`。

另有两组回归：``request`` / ``request_multipart`` / ``open_stream`` 三条路径共用同一套
重定向规则（含循环检测与共享超时预算），见 :class:`TestRedirects` 与
:class:`TestStreamRedirects`；流式投递必须带背压（队列满不丢行、不丢 EOF），见
:class:`TestStreamBackpressure`。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from atri_x.net.httpclient import (
    DEFAULT_MAX_REDIRECTS,
    HTTPClient,
    HTTPError,
    build_multipart,
    iter_sse,
)

#: 所有 async 场景的硬超时：只要挂住就失败，而不是让测试卡死。
HARD_TIMEOUT = 5.0

#: 重定向测试用的路由：路径 → ``(状态码, Location)``（Location 为空表示不回该头）。
_REDIRECTS: dict[str, tuple[int, str]] = {
    "/r301": (301, "/echo-headers"),
    "/r302": (302, "/echo-headers"),
    "/r303": (303, "/echo-headers"),
    "/r307": (307, "/echo-headers"),
    "/r308": (308, "/echo-headers"),
    "/rel/a": (302, "../echo-headers"),  # 相对 Location
    "/loop": (302, "/loop"),  # 永远 302：用来验循环检测
    "/noloc": (302, ""),  # 302 但没有 Location：应原样返回
    "/bad-scheme": (302, "ftp://example.test/x"),  # 只允许 http/https
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # 静音
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _maybe_redirect(self) -> bool:
        """命中重定向路由就回 3xx 并返回 True（先把请求体读掉，保持连接状态干净）。"""
        entry = _REDIRECTS.get(self.path)
        if entry is None:
            return False
        self._read_body()
        status, location = entry
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _echo_headers(self) -> None:
        """回显方法/路径/请求头/请求体，用来观测重定向后的实际请求。"""
        body = self._read_body() if self.command in ("POST", "PUT") else b""
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body.decode("utf-8", errors="replace"),
                "body_len": len(body),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect_chain(self, path: str, delay: float = 0.0) -> None:
        """``/chain/N``：连跳 N 次才到 ``/echo-headers``（``delay`` 用于共享预算用例）。"""
        depth = int(path.rsplit("/", 1)[1])
        if delay:
            time.sleep(delay)
        self._read_body()
        target = "/echo-headers" if depth <= 0 else f"{path.rsplit('/', 1)[0]}/{depth - 1}"
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/echo-headers"):
            self._echo_headers()
            return
        if self.path.startswith("/slow-chain/"):
            self._redirect_chain(self.path, 0.15)
            return
        if self.path.startswith("/chain/"):
            self._redirect_chain(self.path)
            return
        if self._maybe_redirect():
            return
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
        if self.path.startswith("/slow-headers"):
            # 响应头故意晚到：用来验证 open() 被取消后线程/连接确实收干净。
            time.sleep(0.4)
            payload = b"slow"
            self.send_response(200)
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
        if self.path.startswith("/sse-hang"):
            # 必须排在 "/sse" 之前：只发一行就挂住，用来验证 lines() 可被取消。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(2.0)
            self.close_connection = True
            return
        if self.path.startswith("/sse-gap"):
            # 必须排在 "/sse" 之前：首行立刻到、第二行 0.6s 后到。
            # 无 Content-Length + Connection: close => http.client 把 socket 交接给
            # response，正文若被 socket 级超时约束就会丢第二行。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(0.6)
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()
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
        if self.path.startswith("/echo-headers"):
            self._echo_headers()
            return
        if self._maybe_redirect():
            return
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
        if self.path.startswith("/echo-headers"):
            self._echo_headers()
            return
        if self._maybe_redirect():
            return
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


class _RedirectingServer:
    """把 GET/POST/PUT 一律 302 到固定 ``Location`` 的极小服务端（跨源用例用）。

    跨源判定看 scheme/host/port，所以它监听另一个随机端口，天然构成跨源。
    """

    def __init__(self, target: str) -> None:
        self.target = target
        redirect_to = target

        class _RedirectHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # 静音
                return

            def _redirect(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)  # 读掉请求体，保持连接状态干净
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = _redirect
            do_POST = _redirect
            do_PUT = _redirect

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_RedirectingServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
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

    @async_test
    async def test_upload_follows_302(self) -> None:
        """multipart 与 ``request`` 共用跳转规则：302 改成 GET 并丢 body。"""
        resp = await self.client().request_multipart(
            "POST",
            "/r302",
            fields={"model": "whisper"},
            files=[("file", "a.wav", b"RIFF", "audio/wav")],
        )
        data = resp.json()
        self.assertEqual(200, resp.status)
        self.assertEqual("GET", data["method"])
        self.assertEqual("/echo-headers", data["path"])
        self.assertEqual(0, data["body_len"])

    @async_test
    async def test_upload_follows_307_keeping_body(self) -> None:
        """307 必须重放同一份 multipart body（Content-Type/boundary 一起保留）。"""
        resp = await self.client().request_multipart(
            "POST",
            "/r307",
            fields={"model": "whisper"},
            files=[("file", "a.wav", b"RIFF", "audio/wav")],
        )
        data = resp.json()
        self.assertEqual(200, resp.status)
        self.assertEqual("POST", data["method"])
        self.assertIn("whisper", data["body"])
        self.assertIn('filename="a.wav"', data["body"])
        self.assertTrue(
            data["headers"]["content-type"].startswith("multipart/form-data; boundary=")
        )

    @async_test
    async def test_upload_cross_origin_drops_authorization(self) -> None:
        """跨源跳转时不转发 Authorization，非敏感头保留。"""
        with _RedirectingServer(f"{self.server.base_url}/echo-headers") as redirector:
            client = HTTPClient(base_url=redirector.base_url, timeout=HARD_TIMEOUT)
            resp = await client.request_multipart(
                "POST",
                "/upload",
                fields={"model": "whisper"},
                headers={"Authorization": "Bearer secret", "X-Keep": "yes"},
            )
        data = resp.json()
        self.assertEqual(200, resp.status)
        self.assertNotIn("authorization", data["headers"])
        self.assertEqual("yes", data["headers"].get("x-keep"))


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


class TestRedirects(_ServerTestCase):
    """重定向跟随：对齐 Go 默认 ``http.Client``（最多 10 次、301/302/303 改 GET）。

    回归：修复前 ``request`` 完全不处理 3xx（``GET /a`` 直接拿到 302 空 body），
    而 Go 侧默认跟随 10 次 —— 任何会 3xx 的 LLM/媒体/网关端点都会失败。
    """

    @async_test
    async def test_302_post_becomes_get_without_body(self) -> None:
        resp = await self.client().post("/r302", json={"a": 1})
        data = resp.json()
        self.assertEqual(200, resp.status)
        self.assertEqual("GET", data["method"])
        self.assertEqual("/echo-headers", data["path"])
        self.assertEqual(0, data["body_len"])  # body 被丢弃

    @async_test
    async def test_301_and_303_also_switch_to_get(self) -> None:
        for path in ("/r301", "/r303"):
            with self.subTest(path=path):
                resp = await self.client().request("PUT", path, body=b"payload")
                data = resp.json()
                self.assertEqual(200, resp.status)
                self.assertEqual("GET", data["method"])
                self.assertEqual(0, data["body_len"])

    @async_test
    async def test_307_keeps_method_and_body(self) -> None:
        resp = await self.client().post("/r307", json={"a": 1})
        data = resp.json()
        self.assertEqual("POST", data["method"])
        self.assertEqual('{"a":1}', data["body"])

    @async_test
    async def test_308_keeps_method_and_body(self) -> None:
        resp = await self.client().request("PUT", "/r308", body=b"xy")
        data = resp.json()
        self.assertEqual("PUT", data["method"])
        self.assertEqual("xy", data["body"])

    @async_test
    async def test_final_url_is_reported(self) -> None:
        resp = await self.client().get("/r301")
        self.assertEqual(f"{self.server.base_url}/echo-headers", resp.url)

    @async_test
    async def test_relative_location_is_resolved(self) -> None:
        resp = await self.client().get("/rel/a")
        self.assertEqual(200, resp.status)
        self.assertEqual("/echo-headers", resp.json()["path"])

    @async_test
    async def test_same_origin_keeps_sensitive_headers(self) -> None:
        resp = await self.client().get(
            "/r302", headers={"Authorization": "Bearer keep", "Cookie": "sid=1"}
        )
        headers = resp.json()["headers"]
        self.assertEqual("Bearer keep", headers.get("authorization"))
        self.assertEqual("sid=1", headers.get("cookie"))

    @async_test
    async def test_cross_origin_drops_sensitive_headers(self) -> None:
        """跨源（端口不同即跨源）时必须丢弃 Authorization/Cookie，非敏感头保留。"""
        with _RedirectingServer(f"{self.server.base_url}/echo-headers") as redirector:
            client = HTTPClient(base_url=redirector.base_url, timeout=HARD_TIMEOUT)
            resp = await client.get(
                "/go",
                headers={
                    "Authorization": "Bearer secret",
                    "Cookie": "sid=1",
                    "Proxy-Authorization": "Basic x",
                    "Host": "evil.test",
                    "X-Keep": "yes",
                },
            )
        headers = resp.json()["headers"]
        self.assertEqual(200, resp.status)
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("proxy-authorization", headers)
        self.assertNotEqual("evil.test", headers.get("host"))  # Host 必须按新目标重算
        self.assertEqual("yes", headers.get("x-keep"))

    @async_test
    async def test_redirect_limit_raises_http_error(self) -> None:
        """/chain/N 每跳都是**不同** URL，因此由次数上限兜住。"""
        with self.assertRaises(HTTPError) as ctx:
            await self.client().get("/chain/9", max_redirects=3)
        self.assertIn("重定向次数超过上限 3", str(ctx.exception))

    @async_test
    async def test_default_limit_matches_go(self) -> None:
        self.assertEqual(10, DEFAULT_MAX_REDIRECTS)
        with self.assertRaises(HTTPError) as ctx:
            await self.client().get("/chain/30")
        self.assertIn(f"重定向次数超过上限 {DEFAULT_MAX_REDIRECTS}", str(ctx.exception))

    @async_test
    async def test_redirect_loop_is_detected(self) -> None:
        """302 指回自己：循环检测先于次数上限报出来（不会白跳满 10 次）。"""
        with self.assertRaises(HTTPError) as ctx:
            await self.client().get("/loop")
        self.assertIn("重定向循环", str(ctx.exception))

    @async_test
    async def test_redirect_target_must_be_http_or_https(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            await self.client().get("/bad-scheme")
        self.assertIn("不是 http/https", str(ctx.exception))

    @async_test
    async def test_redirect_chain_shares_one_timeout_budget(self) -> None:
        """整条链共享总超时：每跳 0.15s 的链在 0.3s 预算内必须放弃。

        旧行为是「每跳各给一次 timeout」——那要跳满 10 次（约 1.5s）才会结束。
        """
        started = time.monotonic()
        with self.assertRaises(HTTPError):
            await self.client().get("/slow-chain/9", timeout=0.3)
        self.assertLess(time.monotonic() - started, 0.8)

    @async_test
    async def test_follow_redirects_false_returns_3xx(self) -> None:
        resp = await self.client().get("/r302", follow_redirects=False)
        self.assertEqual(302, resp.status)
        self.assertEqual("/echo-headers", resp.header("Location"))

    @async_test
    async def test_missing_location_is_returned_as_is(self) -> None:
        resp = await self.client().get("/noloc", max_redirects=1)
        self.assertEqual(302, resp.status)

    def test_307_with_unreplayable_body_is_controlled_error(self) -> None:
        from atri_x.net.httpclient import _redirect_plan

        with self.assertRaises(HTTPError):
            _redirect_plan(307, "POST", iter([b"x"]), {}, "http://a.test/x", "http://b.test/y")


class TestStreamRedirects(_ServerTestCase):
    """**行为变更**：``open_stream`` 默认跟随重定向（与 ``request`` 共用同一套规则）。

    修复前 docstring 声明「不跟随」、``test_stream_does_not_follow_redirects`` 也钉着
    3xx 原样返回；现在默认跟随，要拿 3xx 必须显式 ``follow_redirects=False``。
    """

    @async_test
    async def test_stream_follows_302_by_default(self) -> None:
        stream = await self.client().open_stream("GET", "/r302")
        try:
            self.assertEqual(200, stream.status)
            self.assertTrue(stream.url.endswith("/echo-headers"))
            body = await stream.read_all()
        finally:
            await stream.close()
        self.assertEqual("/echo-headers", json.loads(body.decode())["path"])

    @async_test
    async def test_stream_follow_redirects_false_returns_3xx(self) -> None:
        stream = await self.client().open_stream("GET", "/r302", follow_redirects=False)
        try:
            self.assertEqual(302, stream.status)
            self.assertEqual("/echo-headers", stream.headers.get("Location"))
        finally:
            await stream.close()

    @async_test
    async def test_stream_without_location_returns_3xx(self) -> None:
        """3xx 但没有 Location：与 ``request`` 一致，原样返回而不是报错。"""
        stream = await self.client().open_stream("GET", "/noloc")
        try:
            self.assertEqual(302, stream.status)
        finally:
            await stream.close()

    @async_test
    async def test_stream_307_keeps_method_and_body(self) -> None:
        stream = await self.client().open_stream("POST", "/r307", json={"a": 1})
        try:
            self.assertEqual(200, stream.status)
            body = await stream.read_all()
        finally:
            await stream.close()
        data = json.loads(body.decode())
        self.assertEqual("POST", data["method"])
        self.assertEqual('{"a":1}', data["body"])

    @async_test
    async def test_stream_redirect_limit_raises(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            await self.client().open_stream("GET", "/chain/9", max_redirects=2)
        self.assertIn("重定向次数超过上限 2", str(ctx.exception))

    @async_test
    async def test_stream_loop_is_detected(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            await self.client().open_stream("GET", "/loop")
        self.assertIn("重定向循环", str(ctx.exception))

    @async_test
    async def test_stream_bad_scheme_is_rejected(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            await self.client().open_stream("GET", "/bad-scheme")
        self.assertIn("不是 http/https", str(ctx.exception))

    @async_test
    async def test_stream_cross_origin_drops_authorization(self) -> None:
        with _RedirectingServer(f"{self.server.base_url}/echo-headers") as redirector:
            client = HTTPClient(base_url=redirector.base_url, timeout=HARD_TIMEOUT)
            stream = await client.open_stream(
                "GET", "/go", headers={"Authorization": "Bearer secret", "X-Keep": "yes"}
            )
            try:
                self.assertEqual(200, stream.status)
                body = await stream.read_all()
            finally:
                await stream.close()
        data = json.loads(body.decode())
        self.assertNotIn("authorization", data["headers"])
        self.assertEqual("yes", data["headers"].get("x-keep"))


class TestStreamBackpressure(_ServerTestCase):
    """流式投递必须带背压：队列满不能丢行、不能丢 EOF。

    旧实现从事件循环回调里调 ``queue.put_nowait``，``maxsize`` 满时回调抛
    ``QueueFull`` → 行与 EOF 一起丢 → 消费端永久挂在 ``queue.get()`` 上。
    """

    @async_test
    async def test_slow_consumer_gets_every_line_and_eof(self) -> None:
        stream = await self.client().open_stream("POST", "/sse-post", json={}, queue_maxsize=1)
        try:
            await asyncio.sleep(0.2)  # 队列只有 1 格：读取线程必然卡在背压等待上
            lines = []
            async for line in stream.lines():
                lines.append(line)
                await asyncio.sleep(0.01)  # 慢消费
        finally:
            await stream.close()
        self.assertEqual(
            ["data: chunk-0", "", "data: chunk-1", "", "data: chunk-2", ""], lines
        )

    @async_test
    async def test_abandoned_delivery_leaves_no_asyncio_noise(self) -> None:
        """队列满 + ``close()``：背压投递被放弃，但不能留下 asyncio 回调异常。

        旧实现（``call_soon_threadsafe(put_nowait)``）在队列满时会让回调抛
        ``QueueFull``，事件循环把它报成 "Exception in callback"；新实现在同一个
        future 上等待、放弃时取走异常，应该一声不响。
        """
        loop = asyncio.get_running_loop()
        problems: list[str] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: problems.append(str(ctx.get("message"))))
        try:
            stream = await self.client().open_stream("GET", "/sse", queue_maxsize=1)
            try:
                await asyncio.sleep(0.2)  # 队列只有 1 格：读取线程卡在背压投递里
                await stream.close()  # 置 stop → 投递被放弃（_abandon_delivery 路径）
                await asyncio.sleep(0.1)  # 给事件循环机会把异常报出来
            finally:
                await stream.close()
        finally:
            loop.set_exception_handler(previous)
        self.assertEqual([], problems)

    @async_test
    async def test_close_wakes_consumer_blocked_on_queue(self) -> None:
        """``close()`` 必须唤醒正在 ``await queue.get()`` 的消费端。"""
        stream = await self.client().open_stream("GET", "/sse-hang", queue_maxsize=4)
        got: list[str] = []

        async def consume() -> None:
            async for line in stream.lines():
                got.append(line)

        try:
            task = asyncio.ensure_future(consume())
            await asyncio.sleep(0.1)  # 此刻消费端阻塞在 queue.get() 上
            await stream.close()
            await asyncio.wait_for(task, 1.0)
        finally:
            await stream.close()
        self.assertEqual(["data: first", ""], got)


class TestStreamCancelCleanup(_ServerTestCase):
    """取消路径：``open()`` 与 ``lines()`` 被取消后不能留下悬挂线程/连接。"""

    def _stream(self, path: str) -> "HTTPStream":
        from atri_x.net.httpclient import HTTPStream

        return HTTPStream(
            url=f"{self.server.base_url}{path}",
            method="GET",
            headers={},
            payload=None,
            proxy="",
            verify_tls=True,
            timeout=HARD_TIMEOUT,
            read_timeout=HARD_TIMEOUT,
            cancel=None,
            encoding="utf-8",
        )

    @async_test
    async def test_open_cancel_stops_thread_and_closes_connection(self) -> None:
        stream = self._stream("/slow-headers")
        task = asyncio.ensure_future(stream.open())
        await asyncio.sleep(0.05)  # 响应头还没到，open() 仍在等待
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream._stop.is_set())
        self.assertIsNotNone(stream._thread)
        await asyncio.to_thread(stream._thread.join, 1.5)
        self.assertFalse(stream._thread.is_alive())
        conn = stream._conn
        self.assertTrue(conn is None or conn.sock is None, "取消后连接必须已关闭")

    @async_test
    async def test_lines_cancel_is_responsive(self) -> None:
        stream = await self.client().open_stream("GET", "/sse-hang")
        try:

            async def consume() -> list[str]:
                return [line async for line in stream.lines()]

            task = asyncio.ensure_future(consume())
            await asyncio.sleep(0.1)  # 消费端阻塞在 queue.get() 上
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            await stream.close()
        self.assertIsNotNone(stream._thread)
        await asyncio.to_thread(stream._thread.join, 2.5)
        self.assertFalse(stream._thread.is_alive(), "取消后读取线程必须退出")


class TestStreamReadTimeout(_ServerTestCase):
    """``read_timeout`` 对**正文**的语义：正数才是 socket 级截止，``0`` 表示不设。

    对齐 Go：连接/响应头受预算约束，正文只有调用方自己的 timer（LLM 侧是
    ``StreamLineReader`` 的 first-chunk/idle timer）。
    """

    @async_test
    async def test_read_timeout_zero_leaves_body_uncapped(self) -> None:
        """``read_timeout=0``：正文不做 socket 级读截止。

        服务端首行立刻到、第二行 0.6s 后才到，``timeout=0.3`` 只管连接/响应头。
        这条用例是 ``HTTPStream._pump`` 里 ``sock.settimeout(self._read_timeout or None)``
        的守卫：一旦改回 ``if self._read_timeout:``（跳过 settimeout），socket 会保留
        建连超时，第二行在 0.3s 处就被掐断（也是 openai 适配器 read_timeout=0 的前提）。
        """
        started = time.monotonic()
        stream = await self.client().open_stream("GET", "/sse-gap", timeout=0.3, read_timeout=0)
        try:
            lines = [line async for line in stream.lines()]
        finally:
            await stream.close()
        self.assertEqual(["data: first", "", "data: second", ""], lines)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)  # 第二行确实晚于 timeout

    @async_test
    async def test_positive_read_timeout_still_caps_body(self) -> None:
        """正数 ``read_timeout`` 仍然生效：正文静默超过该值就报流中断。"""
        stream = await self.client().open_stream(
            "GET", "/sse-gap", timeout=HARD_TIMEOUT, read_timeout=0.2
        )
        received: list[str] = []
        try:
            with self.assertRaises(HTTPError) as ctx:
                async for line in stream.lines():
                    received.append(line)
        finally:
            await stream.close()
        self.assertEqual(["data: first", ""], received)
        self.assertIn("timed out", str(ctx.exception).lower())


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
