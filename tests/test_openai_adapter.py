"""``atri_x.llm.openai``（Chat Completions）端到端测试。

对应 Go：``internal/llm/openai/openai_test.go`` 与 ``errors_test.go``。
全部用本地假服务端（``http.server.ThreadingHTTPServer``）跑真实 HTTP/SSE，
不 mock 传输层。本模块同时给 ``test_openai_responses.py`` 提供假服务端 helper。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Sequence

from atri_x.llm.errors import APIError, ErrorCategory, as_api_error
from atri_x.llm.llm import (
    ChatRequest,
    LLMMessage,
    MessageSegment,
    Role,
    SegmentType,
    ToolCallRequest,
    ToolFunctionSchema,
    ToolSchema,
    text_segments,
)
from atri_x.llm.openai.openai import (
    CHAT_RESERVED_REQUEST_FIELDS,
    OpenAIClient,
    RequestOptions,
    parse_error,
    to_openai_messages,
)
from atri_x.llm.openai.stream import MAX_STREAM_LINE_BYTES
from atri_x.llm.openai.wire import retry_delay, to_usage

# ---------------------------------------------------------------------------
# 假服务端
# ---------------------------------------------------------------------------


def sse(*payloads: str) -> list[bytes]:
    """按 SSE 拼 ``data: <payload>`` 事件（每条后跟空行）。"""
    return [f"data: {payload}\n\n".encode("utf-8") for payload in payloads]


def raw(*parts: bytes | str) -> list[bytes]:
    """原样吐字节，用于 ``\\r\\n``、半个 UTF-8 字符、注释行等边界。"""
    return [part.encode("utf-8") if isinstance(part, str) else part for part in parts]


def write_sse(
    handler: BaseHTTPRequestHandler,
    chunks: Sequence[bytes],
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
    delay: float = 0.0,
    before: float = 0.0,
) -> None:
    """写一条无 Content-Length 的流式响应（靠 Connection: close 收尾）。

    ``before``：发完响应头后先等一会儿再发正文（首包超时/晚到首包用）；
    ``delay``：每块之间等待（流空闲超时用）。
    """
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.flush()
    if before > 0:
        time.sleep(before)
    if delay > 0:
        for chunk in chunks:
            handler.wfile.write(chunk)
            handler.wfile.flush()
            time.sleep(delay)
        return
    body = b"".join(chunks)
    if body:
        handler.wfile.write(body)
    handler.wfile.flush()


def write_body(
    handler: BaseHTTPRequestHandler,
    body: bytes | str,
    *,
    status: int = 200,
    content_type: str = "application/json",
) -> None:
    payload = body.encode("utf-8") if isinstance(body, str) else body
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.write(payload)
    handler.wfile.flush()


def write_headers_only(
    handler: BaseHTTPRequestHandler,
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
    hold: float = 0.6,
) -> None:
    """只发响应头再挂住：用于首包超时。"""
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.flush()
    time.sleep(hold)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        self.server.records.append(record)  # type: ignore[attr-defined]
        try:
            self.server.responder(self, record)  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *args: Any) -> None:
        pass


class FakeServer:
    """本地假服务端。``responder(handler, record)`` 负责写响应。"""

    def __init__(self, responder: Callable[[BaseHTTPRequestHandler, dict[str, Any]], None]) -> None:
        self.records: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.records = self.records  # type: ignore[attr-defined]
        self._server.responder = responder  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def attempts(self) -> int:
        return len(self.records)

    def last_body(self) -> dict[str, Any]:
        return json.loads(self.records[-1]["body"].decode("utf-8"))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _StreamCase(unittest.IsolatedAsyncioTestCase):
    """公共夹具：假服务端 + 客户端构造 + 流收集。"""

    def start_server(
        self, responder: Callable[[BaseHTTPRequestHandler, dict[str, Any]], None]
    ) -> FakeServer:
        server = FakeServer(responder)
        self.addCleanup(server.close)
        return server

    def sse_server(self, chunks: Sequence[bytes], **kwargs: Any) -> FakeServer:
        return self.start_server(lambda handler, record: write_sse(handler, chunks, **kwargs))

    def make_client(self, *args: Any, **kwargs: Any) -> OpenAIClient:
        options = kwargs.pop(
            "options",
            RequestOptions(
                first_chunk_timeout=2.0,
                stream_idle_timeout=2.0,
                max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        return OpenAIClient(*args, options=options, **kwargs)

    async def collect(self, stream: Any) -> tuple[str, str, str, list[Any], Any, BaseException | None]:
        """跑完一条流，返回 (content, reasoning, finish_reason, tool_calls, usage, error)。"""
        content: list[str] = []
        reasoning: list[str] = []
        finish = ""
        tool_calls: list[Any] = []
        usage = None
        error: BaseException | None = None
        async for chunk in stream:
            content.append(chunk.delta_content)
            reasoning.append(chunk.delta_reasoning_content)
            tool_calls.extend(chunk.tool_call_deltas)
            if chunk.finish_reason:
                finish = chunk.finish_reason
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.error is not None:
                error = chunk.error
        return "".join(content), "".join(reasoning), finish, tool_calls, usage, error

    def request(self, **kwargs: Any) -> ChatRequest:
        kwargs.setdefault("model", "test")
        kwargs.setdefault("messages", [LLMMessage(role=Role.USER, segments=text_segments("Hi"))])
        return ChatRequest(**kwargs)


# ---------------------------------------------------------------------------
# 基础流式
# ---------------------------------------------------------------------------


class TestChatStreamBasic(_StreamCase):
    async def test_basic_content_finish_reason_usage_and_envelope(self) -> None:
        server = self.sse_server(
            sse(
                '{"id":"1","object":"chat.completion.chunk","choices":[{"index":0,'
                '"delta":{"content":"Hello"},"finish_reason":null}]}',
                '{"id":"2","object":"chat.completion.chunk","choices":[{"index":0,'
                '"delta":{"content":" world"},"finish_reason":null}]}',
                '{"id":"3","object":"chat.completion.chunk","choices":[{"index":0,'
                '"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,'
                '"completion_tokens":2,"total_tokens":7}}',
                "[DONE]",
            )
        )
        client = self.make_client(server.url + "/", "test-key")
        content, _, finish, _, usage, error = await self.collect(
            client.chat_stream(None, self.request())
        )

        self.assertIsNone(error)
        self.assertEqual(content, "Hello world")
        self.assertEqual(finish, "stop")
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.total_tokens), (5, 2, 7))

        record = server.records[0]
        self.assertEqual(record["path"], "/chat/completions")
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(record["headers"]["Content-Type"], "application/json")
        self.assertTrue(record["headers"]["User-Agent"])
        body = server.last_body()
        self.assertEqual(body["model"], "test")
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertNotIn("temperature", body)

    async def test_multiline_crlf_comment_and_split_utf8(self) -> None:
        payload = '{"choices":[{"index":0,"delta":{"content":"中文"},"finish_reason":null}]}'
        encoded = f"data: {payload}".encode("utf-8")
        server = self.sse_server(
            raw(
                b": keep-alive comment\r\n",
                b"\r\n",
                encoded[: len(encoded) - 6],  # 切在半个 UTF-8 字符中间
                encoded[len(encoded) - 6 :] + b"\r\n\r\n",
                b'data: {"choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}\r\n\r\n',
                b"data: [DONE]\r\n\r\n",
            )
        )
        client = self.make_client(server.url, "k")
        content, _, finish, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "中文!")
        self.assertEqual(finish, "stop")

    async def test_usage_only_chunk_with_cached_tokens(self) -> None:
        server = self.sse_server(
            sse(
                '{"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
                '{"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4,'
                '"total_tokens":7,"prompt_tokens_details":{"cached_tokens":2}}}',
                "[DONE]",
            )
        )
        client = self.make_client(server.url, "k")
        content, _, _, _, usage, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "Hello")
        assert usage is not None
        self.assertEqual(usage.total_tokens, 7)
        self.assertEqual(usage.cache_hit_tokens, 2)

    async def test_reasoning_content_and_alias(self) -> None:
        server = self.sse_server(
            sse(
                '{"choices":[{"index":0,"delta":{"reasoning_content":"think"},"finish_reason":null}]}',
                '{"choices":[{"index":0,"delta":{"reasoning":"more"},"finish_reason":null}]}',
                '{"choices":[{"index":0,"delta":{"content":"answer"},"finish_reason":"stop"}]}',
                "[DONE]",
            )
        )
        client = self.make_client(server.url, "k")
        content, reasoning, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(reasoning, "thinkmore")
        self.assertEqual(content, "answer")

    async def test_tool_call_deltas_are_accumulated_by_index(self) -> None:
        server = self.sse_server(
            sse(
                '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                '"function":{"name":"get","arguments":"wea"}}]},"finish_reason":null}]}',
                '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"call_2",'
                '"function":{"name":"put","arguments":"x"}}]},"finish_reason":null}]}',
                '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"",'
                '"function":{"name":"","arguments":"ther"}}]},"finish_reason":"tool_calls"}]}',
                "[DONE]",
            )
        )
        client = self.make_client(server.url, "k")
        _, _, finish, tool_calls, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(finish, "tool_calls")
        self.assertEqual([(c.index, c.id, c.name, c.args) for c in tool_calls],
                         [(0, "call_1", "get", "weather"), (1, "call_2", "put", "x")])

    async def test_missing_done_reports_unexpected_eof(self) -> None:
        server = self.sse_server(
            sse('{"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}')
        )
        client = self.make_client(server.url, "k")
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertEqual(content, "partial")
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("unexpected EOF", str(error))

    async def test_malformed_chunk_is_reported(self) -> None:
        server = self.sse_server(raw("data: {not-json}\n\n"))
        client = self.make_client(server.url, "k")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("parse stream chunk", str(error))

    async def test_overlong_sse_line_returns_bounded_error(self) -> None:
        server = self.sse_server(raw("data: " + "A" * MAX_STREAM_LINE_BYTES + "\n\n"))
        client = self.make_client(server.url, "k")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("SSE line exceeds maximum size", str(error))
        self.assertNotIn("token too long", str(error))

    async def test_cancelled_token_aborts_the_stream(self) -> None:
        class _Cancelled:
            def is_cancelled(self) -> bool:
                return True

        server = self.sse_server(
            sse('{"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}', "[DONE]")
        )
        client = self.make_client(server.url, "k")
        with self.assertRaises(BaseException) as caught:
            await self.collect(client.chat_stream(_Cancelled(), self.request()))
        import asyncio

        self.assertIsInstance(caught.exception, asyncio.CancelledError)

    def test_retry_delay_matches_go_shift(self) -> None:
        self.assertEqual([retry_delay(2.0, attempt) for attempt in range(3)], [2.0, 4.0, 8.0])

    def test_usage_cache_hit_aliases(self) -> None:
        cases = [
            {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "prompt_cache_hit_tokens": 5},
            {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7, "input_tokens_details": {"cache_hit_tokens": 6}},
            {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "prompt_tokens_details": {"cached_tokens": 2}},
        ]
        for raw_usage in cases:
            with self.subTest(raw=raw_usage):
                usage = to_usage(raw_usage)
                self.assertEqual(usage.total_tokens, 7)
                self.assertGreater(usage.cache_hit_tokens, 0)
                self.assertEqual(usage.prompt_tokens, 3)
                self.assertEqual(usage.completion_tokens, 4)


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------


class TestStreamTimeouts(_StreamCase):
    async def test_first_chunk_timeout_when_body_never_arrives(self) -> None:
        server = self.start_server(lambda handler, record: write_headers_only(handler))
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=0.15, stream_idle_timeout=0.15, max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        with self.assertRaises(APIError) as caught:
            await self.collect(client.chat_stream(None, self.request()))
        self.assertIn("first stream chunk timeout", str(caught.exception))
        self.assertEqual(caught.exception.category, ErrorCategory.TIMEOUT)

    async def test_first_chunk_can_arrive_after_idle_timeout(self) -> None:
        # 首包由 first_chunk_timeout 兜底，不能被 stream_idle_timeout 提前判死。
        server = self.sse_server(
            sse(
                '{"choices":[{"index":0,"delta":{"content":"late"},"finish_reason":null}]}',
                "[DONE]",
            ),
            before=0.05,
        )
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0, stream_idle_timeout=0.02, max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "late")

    async def test_stream_idle_timeout_is_a_timeout_category(self) -> None:
        server = self.sse_server(
            sse('{"choices":[{"index":0,"delta":{"content":"start"},"finish_reason":null}]}')
            + raw(b"") ,
            delay=0.4,
        )
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0, stream_idle_timeout=0.15, max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertEqual(content, "start")
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("idle timeout", str(error))
        self.assertIsInstance(error, APIError)
        self.assertEqual(as_api_error(error).category, ErrorCategory.TIMEOUT)  # type: ignore[union-attr]

    async def test_idle_timer_resets_on_each_chunk(self) -> None:
        server = self.sse_server(
            sse(
                '{"choices":[{"index":0,"delta":{"content":"a"},"finish_reason":null}]}',
                '{"choices":[{"index":0,"delta":{"content":"b"},"finish_reason":null}]}',
                '{"choices":[{"index":0,"delta":{"content":"c"},"finish_reason":"stop"}]}',
                "[DONE]",
            ),
            delay=0.02,
        )
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0, stream_idle_timeout=1.0, max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "abc")


# ---------------------------------------------------------------------------
# 非 SSE / HTML / 网关页
# ---------------------------------------------------------------------------


class TestBadUpstreamResponses(_StreamCase):
    async def _expect_error(self, server: FakeServer) -> BaseException:
        """取流式失败：首包阶段的失败按 Go 的同步返回语义直接抛，流内失败走 error chunk。"""
        client = self.make_client(server.url, "k")
        try:
            _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        except BaseException as exc:  # noqa: BLE001 - 测试要拿到原始异常
            return exc
        if error is None:
            self.fail("expected a stream error")
        return error

    async def test_html_content_type(self) -> None:
        server = self.start_server(
            lambda handler, record: write_sse(
                handler,
                raw("<!doctype html><html><body>" + "A" * 4096 + "</body></html>"),
                content_type="text/html; charset=utf-8",
            )
        )
        error = await self._expect_error(server)
        self.assertIn("上游返回 HTML 而不是 API 响应", str(error))
        self.assertNotIn("AAAA", str(error))
        self.assertLess(len(str(error)), 200)

    async def test_html_body_with_sse_content_type(self) -> None:
        server = self.sse_server(raw("<html><body>gateway error</body></html>"))
        self.assertIn("上游返回 HTML 而不是 API 响应", str(await self._expect_error(server)))

    async def test_json_body_with_sse_content_type_is_rejected(self) -> None:
        server = self.sse_server(raw('{"message":"not a stream"}'))
        self.assertIn("上游返回非 SSE API 响应", str(await self._expect_error(server)))

    async def test_non_sse_body_is_bounded_and_redacted(self) -> None:
        server = self.sse_server(
            raw("secret response " * 1000), content_type="text/plain"
        )
        error = await self._expect_error(server)
        self.assertIn("上游返回非 SSE API 响应", str(error))
        self.assertLess(len(str(error)), 400)
        self.assertLessEqual(str(error).count("secret response"), 20)

    async def test_http_error_status_raises_api_error(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler,
                '{"error":{"message":"invalid api key","type":"auth_error"}}',
                status=401,
            )
        )
        client = self.make_client(server.url, "bad-key")
        with self.assertRaises(APIError) as caught:
            await self.collect(client.chat_stream(None, self.request()))
        self.assertIn("invalid api key", str(caught.exception))
        self.assertEqual(caught.exception.category, ErrorCategory.AUTH)
        self.assertEqual(caught.exception.status_code, 401)


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


class TestRetry(_StreamCase):
    async def test_retries_retryable_status_and_notifies(self) -> None:
        events: list[Any] = []

        def responder(handler: BaseHTTPRequestHandler, record: dict[str, Any]) -> None:
            if len(server.records) < 3:
                write_body(handler, '{"error":{"message":"temporary upstream error"}}', status=502)
                return
            write_sse(handler, sse("[DONE]"))

        server = self.start_server(responder)
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0,
                stream_idle_timeout=2.0,
                max_retries=3,
                retry_initial_delay=0.01,
                on_retry=lambda ctx, event: events.append(event),
            ),
        )
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(server.attempts, 3)
        self.assertEqual([event.attempt for event in events], [1, 2])
        self.assertEqual([event.max_retries for event in events], [3, 3])
        self.assertEqual([event.delay for event in events], [0.01, 0.02])
        self.assertTrue(all(event.err is not None for event in events))

    async def test_rate_limit_then_success(self) -> None:
        def responder(handler: BaseHTTPRequestHandler, record: dict[str, Any]) -> None:
            if len(server.records) == 1:
                write_body(handler, '{"error":{"message":"slow down"}}', status=429)
                return
            write_sse(
                handler,
                sse('{"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}', "[DONE]"),
            )

        server = self.start_server(responder)
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0, stream_idle_timeout=2.0, max_retries=2,
                retry_initial_delay=0.01,
            ),
        )
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "ok")
        self.assertEqual(server.attempts, 2)

    async def test_retry_exhaustion_raises_last_error(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler, '{"error":{"message":"boom"}}', status=500
            )
        )
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0, stream_idle_timeout=2.0, max_retries=2,
                retry_initial_delay=0.01,
            ),
        )
        with self.assertRaises(APIError) as caught:
            await self.collect(client.chat_stream(None, self.request()))
        self.assertEqual(caught.exception.category, ErrorCategory.SERVER)
        self.assertEqual(server.attempts, 3)

    async def test_non_retryable_status_is_not_retried(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(handler, '{"error":{"message":"bad"}}', status=400)
        )
        client = self.make_client(server.url, "k")
        with self.assertRaises(APIError):
            await self.collect(client.chat_stream(None, self.request()))
        self.assertEqual(server.attempts, 1)

    async def test_set_retry_notifier_is_used(self) -> None:
        events: list[Any] = []

        def responder(handler: BaseHTTPRequestHandler, record: dict[str, Any]) -> None:
            if len(server.records) == 1:
                write_body(handler, '{"error":{"message":"busy"}}', status=429)
                return
            write_sse(handler, sse("[DONE]"))

        server = self.start_server(responder)
        client = self.make_client(
            server.url,
            "k",
            options=RequestOptions(
                first_chunk_timeout=2.0, stream_idle_timeout=2.0, max_retries=2,
                retry_initial_delay=0.01,
            ),
        )
        client.set_retry_notifier(lambda ctx, event: events.append(event))
        await self.collect(client.chat_stream(None, self.request()))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].attempt, 1)


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------


class TestRequestPayload(_StreamCase):
    def _server(self) -> FakeServer:
        return self.sse_server(sse("[DONE]"))

    async def test_extra_body_overrides_extra_payload(self) -> None:
        server = self._server()
        client = OpenAIClient(
            server.url,
            "k",
            {"custom_provider_field": "from_provider", "overridden": "provider_value"},
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
        )
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    model="test-model",
                    temperature=0.7,
                    max_tokens=100,
                    extra_body={"custom_request_field": "from_request", "overridden": "request_value"},
                ),
            )
        )
        body = server.last_body()
        self.assertEqual(body["custom_provider_field"], "from_provider")
        self.assertEqual(body["custom_request_field"], "from_request")
        self.assertEqual(body["overridden"], "request_value")
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["max_tokens"], 100)

    async def test_model_extra_payload_wins_over_provider_and_loses_to_request(self) -> None:
        server = self._server()
        client = OpenAIClient(
            server.url,
            "k",
            {"provider_field": "provider", "overridden": "provider_value"},
            {"test-model": {"thinking": {"type": "disabled"}, "overridden": "model_value"}},
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
        )
        await self.collect(
            client.chat_stream(
                None, self.request(model="test-model", extra_body={"overridden": "request_value"})
            )
        )
        body = server.last_body()
        self.assertEqual(body["provider_field"], "provider")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["overridden"], "request_value")

    async def test_reserved_fields_cannot_be_overridden(self) -> None:
        server = self._server()
        client = OpenAIClient(
            server.url,
            "k",
            {"provider_field": "provider", "stream": False},
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
        )
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    model="real-model",
                    extra_body={
                        "model": "hijacked-model",
                        "messages": [{"role": "user", "content": "other"}],
                        "stream": False,
                        "stream_options": {"include_usage": False},
                        "custom_field": "kept",
                    },
                ),
            )
        )
        body = server.last_body()
        self.assertEqual(body["model"], "real-model")
        self.assertEqual(body["stream"], True)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["custom_field"], "kept")
        self.assertEqual(body["provider_field"], "provider")
        self.assertEqual(
            CHAT_RESERVED_REQUEST_FIELDS, frozenset({"model", "messages", "stream", "stream_options"})
        )

    async def test_reserved_field_keys_are_logged_sorted_and_deduplicated(self) -> None:
        logs: list[str] = []

        class _Capture:
            def warning(self, message: str, *args: Any) -> None:
                logs.append(message % args)

            def debug(self, *args: Any) -> None:
                pass

            def info(self, *args: Any) -> None:
                pass

        server = self._server()
        client = OpenAIClient(
            server.url,
            "k",
            {"stream": False, "custom": "kept", "messages": [], "model": "hijacked", "other_key": 1},
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
        )
        client.set_logger(_Capture())  # type: ignore[arg-type]
        await self.collect(client.chat_stream(None, self.request(model="test-model")))
        body = server.last_body()
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["custom"], "kept")
        self.assertEqual(body["other_key"], 1)
        # 与 Go 一致：只记字段名，排序去重，绝不记值。
        self.assertTrue(logs)
        self.assertIn("ignored fields=messages,model,stream", logs[0])
        self.assertIn("reserved request fields", logs[0])

    async def test_reserved_field_matching_is_case_and_space_insensitive(self) -> None:
        server = self._server()
        client = OpenAIClient(
            server.url,
            "k",
            {" Stream ": False, "Model": "hijacked", "stream_options": {"include_usage": False}},
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
        )
        await self.collect(client.chat_stream(None, self.request(model="real-model")))
        body = server.last_body()
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["model"], "real-model")
        self.assertEqual(body["stream_options"], {"include_usage": True})

    async def test_omits_empty_tools_and_keeps_tool_envelope(self) -> None:
        server = self._server()
        client = self.make_client(server.url, "k")
        await self.collect(client.chat_stream(None, self.request()))
        self.assertNotIn("tools", server.last_body())

        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    tools=[
                        ToolSchema(
                            function=ToolFunctionSchema(
                                name="discover_tool",
                                description="Discover tools.",
                                parameters={"type": "object"},
                            )
                        )
                    ]
                ),
            )
        )
        tools = server.last_body()["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["name"], "discover_tool")

    async def test_multimodal_content_parts(self) -> None:
        server = self._server()
        client = self.make_client(server.url, "k")
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[
                        LLMMessage(
                            role=Role.USER,
                            segments=[
                                MessageSegment(type=SegmentType.TEXT, text="看图"),
                                MessageSegment(
                                    type=SegmentType.IMAGE,
                                    url="https://example.com/a.png",
                                    name="a.png",
                                ),
                            ],
                        )
                    ]
                ),
            )
        )
        parts = server.last_body()["messages"][0]["content"]
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], {"type": "text", "text": "看图"})
        self.assertEqual(
            parts[1],
            {"type": "text", "text": "[图片 1；名称：a.png；引用 URL：https://example.com/a.png]"},
        )
        self.assertEqual(
            parts[2], {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        )

    async def test_empty_content_field_is_present(self) -> None:
        server = self._server()
        client = self.make_client(server.url, "k")
        await self.collect(
            client.chat_stream(None, self.request(messages=[LLMMessage(role=Role.ASSISTANT)]))
        )
        message = server.last_body()["messages"][0]
        self.assertIn("content", message)
        self.assertEqual(message["content"], "")

    async def test_tool_messages_payload_and_image_derivation(self) -> None:
        server = self._server()
        client = self.make_client(server.url, "k")
        messages = [
            LLMMessage(
                role=Role.ASSISTANT,
                segments=text_segments("I will check."),
                tool_calls=[ToolCallRequest(id="call_1", name="shell", arguments='{"cmd":"ls"}')],
            ),
            LLMMessage(role=Role.TOOL, name="shell", tool_call_id="call_1", segments=text_segments("AGENT.md\n")),
        ]
        await self.collect(client.chat_stream(None, self.request(messages=messages)))
        body = server.last_body()
        self.assertEqual(len(body["messages"]), 2)
        call = body["messages"][0]["tool_calls"][0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"], {"name": "shell", "arguments": '{"cmd":"ls"}'})
        tool_message = body["messages"][1]
        self.assertEqual((tool_message["role"], tool_message["tool_call_id"], tool_message["name"]),
                         ("tool", "call_1", "shell"))

    def test_tool_images_become_one_user_message(self) -> None:
        messages = to_openai_messages(
            [
                LLMMessage(
                    role=Role.ASSISTANT,
                    tool_calls=[
                        ToolCallRequest(id="call_1", name="screenshot"),
                        ToolCallRequest(id="call_2", name="camera"),
                    ],
                ),
                LLMMessage(
                    role=Role.TOOL,
                    name="screenshot",
                    tool_call_id="call_1",
                    segments=[
                        MessageSegment(type=SegmentType.TEXT, text="截图完成"),
                        MessageSegment(type=SegmentType.IMAGE, url="https://example.com/a.png"),
                    ],
                ),
                LLMMessage(
                    role=Role.TOOL,
                    name="camera",
                    tool_call_id="call_2",
                    segments=[
                        MessageSegment(type=SegmentType.IMAGE, url="data:image/png;base64,aGVsbG8=")
                    ],
                ),
                LLMMessage(role=Role.ASSISTANT, segments=text_segments("我看到了")),
            ]
        )
        self.assertEqual(len(messages), 5)
        self.assertEqual((messages[1]["role"], messages[1]["content"]), ("tool", "截图完成"))
        self.assertEqual(
            (messages[2]["role"], messages[2]["content"]),
            ("tool", "工具返回了图片，见下一条多模态消息。"),
        )
        self.assertEqual(messages[3]["role"], "user")
        parts = messages[3]["content"]
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("screenshot", parts[0]["text"])
        self.assertIn("call_1", parts[0]["text"])
        self.assertEqual(parts[1]["text"], "[图片 1；引用 URL：https://example.com/a.png]")
        self.assertEqual(parts[2]["image_url"]["url"], "https://example.com/a.png")
        self.assertIn("camera", parts[3]["text"])
        self.assertEqual(parts[4]["text"], "[图片 1；内嵌图片，无可复用 URL]")
        self.assertNotIn("aGVsbG8=", parts[4]["text"])
        self.assertEqual(parts[5]["image_url"]["url"], "data:image/png;base64,aGVsbG8=")
        self.assertEqual((messages[4]["role"], messages[4]["content"]), ("assistant", "我看到了"))

    def test_image_numbers_restart_per_message_and_input_is_not_mutated(self) -> None:
        original = [
            LLMMessage(
                role=Role.USER,
                segments=[
                    MessageSegment(type=SegmentType.IMAGE, url="https://example.com/a.png"),
                    MessageSegment(type=SegmentType.IMAGE, url="https://example.com/b.png"),
                ],
            ),
            LLMMessage(
                role=Role.USER,
                segments=[MessageSegment(type=SegmentType.IMAGE, url="https://example.com/c.png")],
            ),
        ]
        messages = to_openai_messages(original)
        self.assertEqual(len(messages), 2)
        first = messages[0]["content"]
        self.assertEqual(len(first), 4)
        self.assertIn("图片 1", first[0]["text"])
        self.assertIn("图片 2", first[2]["text"])
        second = messages[1]["content"]
        self.assertEqual(len(second), 2)
        self.assertIn("图片 1", second[0]["text"])
        self.assertEqual(len(original[0].segments), 2)
        self.assertEqual(original[0].segments[0].type, SegmentType.IMAGE)

    async def test_resolve_image_is_injected_for_empty_urls(self) -> None:
        server = self._server()
        seen: list[str] = []

        def resolver(segment: MessageSegment) -> str:
            seen.append(segment.media)
            return f"https://cdn.example.com/{segment.media}.png"

        client = OpenAIClient(
            server.url,
            "k",
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
            resolve_image=resolver,
        )
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[
                        LLMMessage(
                            role=Role.USER,
                            segments=[
                                MessageSegment(type=SegmentType.IMAGE, media="m-1"),
                                MessageSegment(
                                    type=SegmentType.IMAGE, url="https://example.com/x.png"
                                ),
                            ],
                        )
                    ]
                ),
            )
        )
        self.assertEqual(seen, ["m-1"])
        parts = server.last_body()["messages"][0]["content"]
        self.assertEqual(parts[1]["image_url"]["url"], "https://cdn.example.com/m-1.png")
        self.assertIn("媒体 ID：m-1", parts[0]["text"])
        self.assertEqual(parts[3]["image_url"]["url"], "https://example.com/x.png")

    async def test_missing_base_url_is_rejected_eagerly(self) -> None:
        client = OpenAIClient("", "k")
        with self.assertRaises(ValueError):
            client.chat_stream(None, self.request())
        with self.assertRaises(ValueError):
            await client.list_models(None)

    def test_invalid_proxy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OpenAIClient("https://example.invalid/v1", "k", options=RequestOptions(proxy="://bad proxy"))

    def test_environment_proxy_is_disabled_without_configuration(self) -> None:
        import os

        previous = os.environ.get("HTTPS_PROXY")
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        try:
            client = OpenAIClient("https://example.invalid/v1", "k")
            self.assertEqual(client._http.proxy, "")
            proxied = OpenAIClient(
                "https://example.invalid/v1", "k", options=RequestOptions(proxy="http://127.0.0.1:7890")
            )
            self.assertEqual(proxied._http.proxy, "http://127.0.0.1:7890")
        finally:
            if previous is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = previous


# ---------------------------------------------------------------------------
# 模型列表 / 非流式 / 协议能力
# ---------------------------------------------------------------------------


class TestModelsAndPlainChat(_StreamCase):
    async def test_list_models_preserves_order_and_duplicates(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler, '{"data":[{"id":"beta"},{"id":"alpha"},{"id":"beta"}]}'
            )
        )
        client = self.make_client(server.url, "k")
        models = await client.list_models(None)
        self.assertEqual(models, ["beta", "alpha", "beta"])
        self.assertEqual(server.records[0]["path"], "/models")
        self.assertEqual(server.records[0]["headers"]["Authorization"], "Bearer k")

    async def test_list_models_error_status_is_structured(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler, '{"error":{"message":"nope","code":"model_not_found"}}', status=404
            )
        )
        client = self.make_client(server.url, "k")
        with self.assertRaises(APIError) as caught:
            await client.list_models(None)
        self.assertEqual(caught.exception.category, ErrorCategory.MODEL_NOT_FOUND)

    async def test_list_model_metadata_reads_context_window_aliases(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler,
                '{"data":[{"id":"alpha","context_window":32000},'
                '{"id":"beta","max_input_tokens":16000}]}',
            )
        )
        client = self.make_client(server.url, "k")
        models = await client.list_model_metadata(None)
        self.assertEqual([(m.id, m.context_window) for m in models],
                         [("alpha", 32000), ("beta", 16000)])

    async def test_non_streaming_chat(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler,
                '{"choices":[{"message":{"role":"assistant","content":"hi there",'
                '"tool_calls":[{"id":"call_9","type":"function","function":'
                '{"name":"shell","arguments":"{}"}}]}}],'
                '"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}',
            )
        )
        client = self.make_client(server.url, "k")
        response = await client.chat(None, self.request())
        self.assertEqual(response.message.content_text(), "hi there")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].id, "call_9")
        self.assertEqual(response.tool_calls[0].name, "shell")
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 3)
        body = server.last_body()
        self.assertNotIn("stream", body)
        self.assertNotIn("stream_options", body)

    async def test_protocol_capabilities_report_chat_completions(self) -> None:
        client = OpenAIClient("https://example.invalid/v1", "k")
        caps = client.protocol_capabilities_for("m")
        self.assertEqual(caps.protocol, "chat_completions")
        self.assertFalse(
            caps.server_side_conversation or caps.native_compaction
            or caps.incremental_tools or caps.server_side_store
        )

    async def test_satisfies_frozen_llm_protocols(self) -> None:
        # 冻结契约（atri_x/llm/llm.py、protocol.py）里的可选接口必须能被识别。
        from atri_x.llm.llm import LLM, RetryNotifier
        from atri_x.llm.protocol import (
            ProtocolCapabilityReporter,
            protocol_capabilities_of,
        )

        client = OpenAIClient("https://example.invalid/v1", "k")
        self.assertIsInstance(client, LLM)
        self.assertIsInstance(client, RetryNotifier)
        self.assertIsInstance(client, ProtocolCapabilityReporter)
        self.assertEqual(protocol_capabilities_of(client, "m").protocol, "chat_completions")

    async def test_stream_is_awaitable_and_iterable(self) -> None:
        server = self.sse_server(sse("[DONE]"))
        client = self.make_client(server.url, "k")
        stream = await client.chat_stream(None, self.request())
        async for _ in stream:
            pass


# ---------------------------------------------------------------------------
# 调试日志（对应 Go 的 logRequest / logFirstSystemMessage）
# ---------------------------------------------------------------------------


class _LogCapture:
    """最小 logger：记录 `message % args`。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def debug(self, message: str, *args: Any) -> None:
        self.records.append(("debug", message % args))

    def info(self, message: str, *args: Any) -> None:
        self.records.append(("info", message % args))

    def warning(self, message: str, *args: Any) -> None:
        self.records.append(("warning", message % args))

    def text(self) -> str:
        return "\n".join(f"{level} {message}" for level, message in self.records)


class TestRequestLogging(_StreamCase):
    async def test_debug_log_summarizes_latest_message_without_leaking(self) -> None:
        server = self.sse_server(sse("[DONE]"))
        client = OpenAIClient(
            server.url,
            "secret-key",
            options=RequestOptions(first_chunk_timeout=2.0, stream_idle_timeout=2.0),
        )
        capture = _LogCapture()
        client.set_logger(capture)  # type: ignore[arg-type]

        data_url = "data:image/png;base64,log-secret-base64-payload"
        request = self.request(
            session_id="session-1",
            messages=[
                LLMMessage(role=Role.SYSTEM, segments=text_segments("system prompt")),
                LLMMessage(role=Role.USER, segments=text_segments("旧消息")),
                LLMMessage(role=Role.ASSISTANT, segments=text_segments("旧回答")),
                LLMMessage(
                    role=Role.USER,
                    segments=[
                        MessageSegment(type=SegmentType.TEXT, text="看图"),
                        MessageSegment(type=SegmentType.IMAGE, url=data_url),
                        MessageSegment(
                            type=SegmentType.IMAGE, url="https://example.com/a.png"
                        ),
                    ],
                ),
            ],
        )
        # 两次调用：system prompt 只记一次。
        for _ in range(2):
            await self.collect(client.chat_stream(None, request))

        text = capture.text()
        self.assertIn("latest_message_json=", text)
        self.assertIn("https://example.com/a.png", text)
        self.assertIn("message_count=4", text)
        self.assertIn("message_roles=system,user,assistant,user", text)
        self.assertIn("latest_message=user", text)
        self.assertIn("body_hash=", text)
        # 旧历史不进默认调试日志，凭据永不进日志。
        self.assertNotIn("旧消息", text)
        self.assertNotIn("旧回答", text)
        self.assertNotIn("secret-key", text)
        self.assertNotIn("Authorization", text)
        # data: URL 只留前缀，但请求体里仍是完整内容。
        self.assertIn("data:image/png;base64,…", text)
        self.assertNotIn("log-secret-base64-payload", text)
        self.assertIn(data_url, server.records[-1]["body"].decode("utf-8"))
        # system prompt 每个会话只打一次。
        self.assertEqual(text.count("first_system_message_json="), 1)


# ---------------------------------------------------------------------------
# parse_error（对应 openai/errors_test.go）
# ---------------------------------------------------------------------------


class TestParseError(unittest.TestCase):
    def test_keeps_structured_fields(self) -> None:
        error = parse_error(
            404,
            '{"error":{"message":"The model gpt-x does not exist","type":'
            '"invalid_request_error","param":null,"code":"model_not_found"}}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(error.status_code, 404)
        self.assertEqual(error.code, "model_not_found")
        self.assertEqual(error.type, "invalid_request_error")
        self.assertEqual(error.param, "")
        self.assertTrue(error.deterministic())
        self.assertTrue(str(error).startswith("HTTP 404: "))

    def test_normalizes_untyped_code(self) -> None:
        error = parse_error(
            400,
            '{"error":{"message":"invalid value","type":"invalid_request_error",'
            '"code":123,"param":"messages"}}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(error.code, "123")

    def test_html_leaves_no_code(self) -> None:
        error = parse_error(502, "<html><body>gateway</body></html>", {"Content-Type": "text/html"})
        self.assertEqual(error.code, "")
        self.assertFalse(error.deterministic())
        self.assertIn("上游返回 HTML", str(error))

    def test_classifies_vision_unsupported_from_json_prose(self) -> None:
        error = parse_error(
            400,
            '{"error":{"message":"<400> InternalError.Algo.InvalidParameter: The provided '
            'messages input is invalid. The error info is [Unexpected item type in content.]",'
            '"type":"invalid_parameter_error","code":"invalid_parameter","param":null}}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(error.category, ErrorCategory.VISION_UNSUPPORTED)

    def test_classifies_vision_unsupported_from_bare_body(self) -> None:
        error = parse_error(
            400,
            "InternalError.Algo.InvalidParameter: unexpected item type in content.",
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(error.category, ErrorCategory.VISION_UNSUPPORTED)

    def test_vision_category_vetoed_by_unrelated_param(self) -> None:
        error = parse_error(
            400,
            '{"error":{"message":"provider image-lab: temperature out of range",'
            '"type":"invalid_request_error","code":"invalid_parameter","param":"temperature"}}',
            {"Content-Type": "application/json"},
        )
        self.assertNotEqual(error.category, ErrorCategory.VISION_UNSUPPORTED)
        self.assertEqual(error.category, ErrorCategory.INVALID_REQUEST)

    def test_model_name_is_not_a_vision_signal(self) -> None:
        error = parse_error(
            404,
            '{"error":{"message":"model image-chat-v2 not found","type":'
            '"invalid_request_error","code":"","param":null}}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(error.category, ErrorCategory.MODEL_NOT_FOUND)

    def test_classifies_statuses(self) -> None:
        cases = [
            (400, '{"error":{"message":"nope","code":"model_not_found"}}', ErrorCategory.MODEL_NOT_FOUND),
            (401, '{"error":{"message":"bad key"}}', ErrorCategory.AUTH),
            (429, '{"error":{"message":"slow down"}}', ErrorCategory.RATE_LIMIT),
            (504, '{"error":{"message":"gateway"}}', ErrorCategory.TIMEOUT),
            (500, '{"error":{"message":"boom"}}', ErrorCategory.SERVER),
            (
                400,
                '{"error":{"message":"bad field","type":"invalid_request_error","param":"max_tokens"}}',
                ErrorCategory.INVALID_REQUEST,
            ),
            (400, '{"error":{"message":"something went wrong"}}', ""),
            (429, '{"error":{"message":"rate limited while sending image"}}', ErrorCategory.RATE_LIMIT),
        ]
        for status, body, category in cases:
            with self.subTest(status=status, body=body):
                error = parse_error(status, body, {"Content-Type": "application/json"})
                self.assertEqual(error.category, category)

    def test_empty_and_plain_bodies(self) -> None:
        empty = parse_error(503, b"", {"Content-Type": "application/json"})
        self.assertEqual(empty.message, "响应内容为空或无法识别")
        self.assertEqual(empty.category, ErrorCategory.SERVER)
        plain = parse_error(400, "invalid image reference", {"Content-Type": "text/plain"})
        self.assertEqual(plain.category, ErrorCategory.VISION_UNSUPPORTED)

    def test_body_is_truncated_like_go(self) -> None:
        error = parse_error(500, "x" * 100000, {"Content-Type": "text/plain"})
        self.assertLessEqual(len(error.message), 300)


if __name__ == "__main__":
    unittest.main()
