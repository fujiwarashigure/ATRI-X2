"""``atri_x.asr`` 的单元测试。

对应 Go：``internal/asr/asr_test.go``。

HTTP 层不 mock：用 :class:`http.server.ThreadingHTTPServer` 起本地假 provider，
真实上传 multipart 并逐字段断言。
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from atri_x.asr import (
    KIND,
    AsrError,
    BudgetLedger,
    HTTPError,
    Options,
    Request,
    ReservationRequest,
    Service,
    asr_transcription_text,
    bounded_message,
    clean_text,
    deterministic_error,
    extension_for_mime,
    new_http_error,
    provider_message,
    reservation_call_id,
    reservation_digest_input,
    retryable,
    safe_filename,
)

HARD_TIMEOUT = 15.0


def _parse_multipart(body: bytes, boundary: str) -> list[dict[str, Any]]:
    """把 multipart 请求体拆成 ``[{name, filename, content_type, data}]``。"""
    delimiter = b"--" + boundary.encode()
    parts: list[dict[str, Any]] = []
    for chunk in body.split(delimiter):
        if not chunk or chunk in (b"--\r\n", b"--"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk.startswith(b"--"):
            continue
        header_blob, _, data = chunk.partition(b"\r\n\r\n")
        headers: dict[str, str] = {}
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = ""
        filename = ""
        for piece in disposition.split(";"):
            piece = piece.strip()
            if piece.startswith("name="):
                name = piece[len("name=") :].strip('"')
            elif piece.startswith("filename="):
                filename = piece[len("filename=") :].strip('"')
        parts.append(
            {
                "name": name,
                "filename": filename,
                "content_type": headers.get("content-type", ""),
                "data": data.rstrip(b"\r\n"),
            }
        )
    return parts


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # 静音
        return None

    def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server = self.server
        server.calls += 1  # type: ignore[attr-defined]
        server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        delay = server.delay  # type: ignore[attr-defined]
        if delay:
            time.sleep(delay)
        mode = server.mode  # type: ignore[attr-defined]
        if mode == "unauthorized":
            self._send(
                401, b'{"error":{"code":"invalid_api_key","message":"bad key"}}'
            )
            return
        if mode == "server_error_once" and server.calls == 1:
            self._send(500, b'{"error":{"message":"busy"}}')
            return
        if mode == "too_many":
            self._send(429, b'{"error":{"message":"slow down"}}', content_type="application/json")
            return
        if mode == "empty":
            self._send(200, json.dumps({"text": "  \n "}).encode())
            return
        if mode == "long":
            self._send(200, json.dumps({"text": "雨" * 64}).encode())
            return
        if mode == "bad_request":
            self._send(400, b'{"error":{"code":"bad_audio","message":"cannot decode"}}')
            return
        self._send(
            200,
            json.dumps({"text": " 你好 \n世界 ", "language": "zh", "duration": 1.5}).encode(),
        )


class _Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.calls = 0  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.httpd.mode = ""  # type: ignore[attr-defined]
        self.httpd.delay = 0.0  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def calls(self) -> int:
        return self.httpd.calls  # type: ignore[attr-defined]

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.httpd.requests  # type: ignore[attr-defined]

    def set_mode(self, mode: str) -> None:
        self.httpd.mode = mode  # type: ignore[attr-defined]

    def set_delay(self, delay: float) -> None:
        self.httpd.delay = delay  # type: ignore[attr-defined]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


class _ServerTestCase(unittest.IsolatedAsyncioTestCase):
    server: _Server

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    def setUp(self) -> None:
        self.server.set_mode("")
        self.server.set_delay(0.0)
        self.server.requests.clear()
        self.server.httpd.calls = 0  # type: ignore[attr-defined]

    def service(self, **kw: Any) -> Service:
        opts = Options(
            base_url=self.server.base_url + "/v1",
            api_key="test-key",
            model="whisper-test",
            provider="test",
            language="zh",
            prompt="人名：小明",
            retry_initial_delay=0.001,
        )
        for key, value in kw.items():
            setattr(opts, key, value)
        service = Service(opts)
        self.addCleanup(service.close)
        return service

    @staticmethod
    def request(data: bytes = b"audio-bytes") -> Request:
        return Request(media_id="media:test", data=data, name="voice.amr", mime_type="audio/amr")


# ---------------------------------------------------------------------------
# 对应 Go asr_test.go
# ---------------------------------------------------------------------------

class TestTranscribe(_ServerTestCase):
    async def test_sends_multipart_and_parses_result(self) -> None:
        service = self.service()
        result = await service.transcribe(None, self.request())
        self.assertEqual("你好 世界", result.text)
        self.assertEqual("zh", result.language)
        self.assertEqual(1.5, result.duration)
        self.assertEqual("test", result.provider)
        self.assertEqual("whisper-test", result.model)
        self.assertFalse(result.cached)

        record = self.server.requests[-1]
        self.assertEqual("/v1/audio/transcriptions", record["path"])
        self.assertEqual("Bearer test-key", record["headers"]["authorization"])
        self.assertEqual("application/json", record["headers"]["accept"])
        content_type = record["headers"]["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        parts = {part["name"]: part for part in _parse_multipart(record["body"], boundary)}
        self.assertEqual({"file", "model", "language", "prompt", "response_format"}, set(parts))
        self.assertEqual("whisper-test", parts["model"]["data"].decode())
        self.assertEqual("zh", parts["language"]["data"].decode())
        self.assertEqual("人名：小明", parts["prompt"]["data"].decode())
        self.assertEqual("json", parts["response_format"]["data"].decode())
        self.assertEqual(b"audio-bytes", parts["file"]["data"])
        self.assertEqual("audio/amr", parts["file"]["content_type"])
        self.assertEqual("voice.amr", parts["file"]["filename"])

    async def test_caches_successful_result(self) -> None:
        service = self.service()
        first = await service.transcribe(None, self.request(b"audio"))
        self.assertFalse(first.cached)
        second = await service.transcribe(None, self.request(b"audio"))
        self.assertTrue(second.cached)
        self.assertEqual(first.text, second.text)
        self.assertEqual(1, self.server.calls)

    async def test_retries_transient_failure(self) -> None:
        self.server.set_mode("server_error_once")
        service = self.service(max_retries=1)
        result = await service.transcribe(None, self.request(b"audio"))
        self.assertEqual("你好 世界", result.text)
        self.assertEqual(2, self.server.calls)

    async def test_caches_deterministic_failure(self) -> None:
        self.server.set_mode("unauthorized")
        service = self.service()
        for _ in range(2):
            with self.assertRaises(HTTPError) as ctx:
                await service.transcribe(None, self.request(b"audio"))
            error = ctx.exception
            self.assertEqual(401, error.status_code)
            self.assertIn("invalid_api_key", error.message)
            self.assertIn("HTTP 401", str(error))
        self.assertEqual(1, self.server.calls)

    async def test_does_not_cache_transient_failure(self) -> None:
        self.server.set_mode("too_many")
        # Go 的 ``max_retries=0`` 会被 New 折成默认值 2；要"不重试"必须传负数。
        service = self.service(max_retries=-1)
        for _ in range(2):
            with self.assertRaises(HTTPError) as ctx:
                await service.transcribe(None, self.request(b"audio"))
            self.assertEqual(429, ctx.exception.status_code)
        self.assertEqual(2, self.server.calls)

    async def test_rejects_oversized_audio_before_upload(self) -> None:
        service = self.service(max_audio_bytes=4)
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, self.request(b"12345"))
        self.assertIn("over the 4 byte limit", str(ctx.exception))
        self.assertEqual(0, self.server.calls)

    async def test_rejects_empty_transcript(self) -> None:
        self.server.set_mode("empty")
        service = self.service()
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, self.request(b"audio"))
        self.assertIn("empty transcript", str(ctx.exception))

    async def test_bounds_response(self) -> None:
        self.server.set_mode("long")
        service = self.service(max_response_bytes=16)
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, self.request(b"audio"))
        self.assertIn("response exceeded", str(ctx.exception))

    async def test_language_override_and_filename_sanitization(self) -> None:
        service = self.service()
        request = self.request(b"audio")
        request.language = "en"
        request.name = '../../bad"name'
        await service.transcribe(None, request)
        record = self.server.requests[-1]
        boundary = record["headers"]["content-type"].split("boundary=", 1)[1]
        parts = {part["name"]: part for part in _parse_multipart(record["body"], boundary)}
        self.assertEqual("en", parts["language"]["data"].decode())
        filename = parts["file"]["filename"]
        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)
        self.assertNotIn('"', filename)
        self.assertEqual("bad_name.amr", filename)


class TestTranscribeValidation(_ServerTestCase):
    async def test_missing_configuration_and_input(self) -> None:
        with self.assertRaises(AsrError) as ctx:
            await Service(Options(model="m")).transcribe(None, self.request(b"audio"))
        self.assertIn("base URL is not configured", str(ctx.exception))

        with self.assertRaises(AsrError) as ctx:
            await Service(Options(base_url=self.server.base_url)).transcribe(None, self.request())
        self.assertIn("model is not configured", str(ctx.exception))

        service = self.service()
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, Request(data=b"audio"))
        self.assertIn("media id is required", str(ctx.exception))

        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, Request(media_id="media:1"))
        self.assertIn("audio is empty", str(ctx.exception))

    async def test_decode_failure(self) -> None:
        service = self.service()
        with self.assertRaises(AsrError):
            service._parse_response(b"<html>")
        with self.assertRaises(AsrError):
            service._parse_response(b'["not", "an", "object"]')

    async def test_non_retryable_4xx_is_not_retried(self) -> None:
        self.server.set_mode("bad_request")
        service = self.service(max_retries=2)
        with self.assertRaises(HTTPError) as ctx:
            await service.transcribe(None, self.request(b"audio"))
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("bad_audio: cannot decode", ctx.exception.message)
        self.assertEqual(1, self.server.calls)

    async def test_timeout_is_a_total_budget(self) -> None:
        self.server.set_delay(1.0)
        service = self.service(timeout=0.2, max_retries=0)
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, self.request(b"audio"))
        self.assertIn("timed out", str(ctx.exception))

    async def test_queue_rejects_when_full(self) -> None:
        self.server.set_delay(0.4)
        # ``max_job_queue <= 0`` 都是默认值 8；只有负数才是"不排队"（与 Go 一致）。
        service = self.service(
            max_concurrent_jobs=1, max_job_queue=-1, max_retries=-1
        )
        first = asyncio.ensure_future(service.transcribe(None, self.request(b"audio-1")))
        await asyncio.sleep(0.1)
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, self.request(b"audio-2"))
        self.assertIn("asr: job queue:", str(ctx.exception))
        self.server.set_delay(0.0)
        result = await asyncio.wait_for(first, HARD_TIMEOUT)
        self.assertEqual("你好 世界", result.text)

    async def test_close_rejects_new_work(self) -> None:
        service = self.service()
        service.close()
        with self.assertRaises(AsrError) as ctx:
            await service.transcribe(None, self.request(b"audio"))
        self.assertIn("closed", str(ctx.exception))
        self.assertEqual(0, self.server.calls)

    async def test_credential_epoch_and_model_change_the_cache_key(self) -> None:
        service = self.service()
        await service.transcribe(None, self.request(b"audio"))
        self.assertEqual(1, self.server.calls)
        service.opts.credential_epoch = 1
        result = await service.transcribe(None, self.request(b"audio"))
        self.assertFalse(result.cached)
        self.assertEqual(2, self.server.calls)
        service.opts.model = "whisper-other"
        await service.transcribe(None, self.request(b"audio"))
        self.assertEqual(3, self.server.calls)

    async def test_language_and_prompt_participate_in_the_key(self) -> None:
        service = self.service()
        first = service.cache_key(self.request())
        request = self.request()
        request.language = "en"
        self.assertNotEqual(first, service.cache_key(request))
        service.opts.prompt = "别的提示词"
        self.assertNotEqual(first, service.cache_key(self.request()))
        service.opts.prompt = "人名：小明"
        self.assertEqual(first, service.cache_key(self.request()))

    async def test_options_are_copied_and_defaults_applied(self) -> None:
        opts = Options(base_url="http://127.0.0.1:1/v1", model="m")
        service = Service(opts)
        self.assertEqual(0, opts.timeout)
        self.assertEqual(120.0, service.opts.timeout)
        self.assertEqual(20 * 1024 * 1024, service.opts.max_audio_bytes)
        self.assertEqual(2, service.opts.max_retries)
        self.assertEqual(1.0, service.opts.retry_initial_delay)
        self.assertEqual(10, service.limiter.config.max)
        self.assertEqual(8, service.limiter.config.queue_size)

    async def test_negative_limits_are_normalized(self) -> None:
        service = Service(Options(base_url="http://127.0.0.1:1/v1", model="m", max_retries=-1))
        self.assertEqual(0, service.opts.max_retries)
        unlimited = Service(
            Options(
                base_url="http://127.0.0.1:1/v1",
                model="m",
                max_concurrent_jobs=-1,
                max_job_queue=-1,
                job_wait_timeout=-1,
            )
        )
        self.assertEqual(0, unlimited.limiter.config.max)
        self.assertEqual(0, unlimited.limiter.config.queue_size)
        self.assertEqual(0.0, unlimited.limiter.config.wait_timeout)


# ---------------------------------------------------------------------------
# error.go / cache.go / asr.go 的纯函数
# ---------------------------------------------------------------------------

class TestErrorClassification(unittest.TestCase):
    def test_retryable(self) -> None:
        self.assertTrue(retryable(HTTPError(status_code=0, message="dial")))
        self.assertTrue(retryable(HTTPError(status_code=429)))
        self.assertTrue(retryable(HTTPError(status_code=500)))
        self.assertTrue(retryable(HTTPError(status_code=503)))
        self.assertFalse(retryable(HTTPError(status_code=400)))
        self.assertFalse(retryable(HTTPError(status_code=401)))
        self.assertFalse(retryable(AsrError("nope")))
        self.assertFalse(retryable(None))

    def test_deterministic_error(self) -> None:
        self.assertTrue(deterministic_error(HTTPError(status_code=400)))
        self.assertTrue(deterministic_error(HTTPError(status_code=401)))
        self.assertFalse(deterministic_error(HTTPError(status_code=429)))
        self.assertFalse(deterministic_error(HTTPError(status_code=0)))
        self.assertFalse(deterministic_error(HTTPError(status_code=500)))
        self.assertFalse(deterministic_error(AsrError("nope")))

    def test_http_error_rendering(self) -> None:
        self.assertEqual("asr: request failed: dial", str(HTTPError(0, "dial")))
        self.assertEqual("asr: provider returned HTTP 500", str(HTTPError(500)))
        self.assertEqual(
            "asr: provider returned HTTP 400: bad", str(HTTPError(400, "bad"))
        )

    def test_provider_message(self) -> None:
        self.assertEqual(
            "invalid_api_key: bad key",
            provider_message(b'{"error":{"code":"invalid_api_key","message":"bad key"}}'),
        )
        self.assertEqual("plain", provider_message(b'{"message":"plain"}'))
        self.assertEqual("", provider_message(b"not json"))
        self.assertEqual("", provider_message(b'{"error":{}}'))

    def test_new_http_error_retry_after_seconds(self) -> None:
        error = new_http_error(429, {"Retry-After": "5"}, b'{"error":{"message":"slow"}}')
        self.assertEqual(429, error.status_code)
        self.assertEqual(5.0, error.retry_after)
        self.assertEqual("slow", error.message)

    def test_new_http_error_retry_after_http_date(self) -> None:
        when = email.utils.formatdate(time.time() + 3, usegmt=True)
        error = new_http_error(503, {"Retry-After": when}, b"")
        self.assertGreater(error.retry_after, 0)
        self.assertLessEqual(error.retry_after, 30)

    def test_new_http_error_falls_back_to_body_text(self) -> None:
        error = new_http_error(502, {}, b"upstream exploded")
        self.assertEqual("upstream exploded", error.message)
        self.assertEqual("asr: provider returned HTTP 502: upstream exploded", str(error))

    def test_bounded_message(self) -> None:
        self.assertEqual("abc", bounded_message("abc"))
        self.assertEqual(512, len(bounded_message("x" * 600)))


class TestTextHelpers(unittest.TestCase):
    def test_clean_text(self) -> None:
        self.assertEqual("你好 世界", clean_text(" 你好 \n世界 "))
        self.assertEqual("a b", clean_text("a\x00b"))
        # 控制字符折叠成一个空格，但真正的空格照样保留（与 Go 一致）。
        self.assertEqual("a  b", clean_text("a\x01\x02 b"))
        self.assertEqual("ab", clean_text("\x00ab"))
        self.assertNotIn("\x00", clean_text("a\x00b"))
        # 全角空格不是控制字符：Go 只是把它记成 lastSpace，原样保留；紧跟在它
        # 后面的控制字符因此不再补空格。
        self.assertEqual("a\u3000b", clean_text("a\u3000b"))
        self.assertEqual("a\u3000b", clean_text("a\u3000\x01b"))
        self.assertEqual("a \u3000b", clean_text("a\x01\u3000b"))
        self.assertEqual("", clean_text("   "))

    def test_safe_filename(self) -> None:
        self.assertEqual("voice.amr", safe_filename("voice.amr", "audio/amr"))
        self.assertEqual("audio.ogg", safe_filename("", "audio/ogg"))
        self.assertEqual("audio.bin", safe_filename("   ", ""))
        self.assertEqual("audio.mp3", safe_filename(".", "audio/mpeg"))
        self.assertEqual("bad_name.amr", safe_filename('../../bad"name', "audio/amr"))
        # 反斜杠先被换成 "/"，再取基名（Go 的 filepath.Base 语义）。
        self.assertEqual("b.ogg", safe_filename("a\\b", "audio/ogg"))
        self.assertEqual("_evil_.mp3", safe_filename("'evil'", "audio/mp3"))
        long_name = "x" * 200
        self.assertLessEqual(len(safe_filename(long_name, "audio/amr").encode("utf-8")), 124)
        multi = "语" * 100
        self.assertTrue(safe_filename(multi, "audio/wav").endswith(".wav"))

    def test_extension_for_mime(self) -> None:
        self.assertEqual(".ogg", extension_for_mime("audio/ogg"))
        self.assertEqual(".ogg", extension_for_mime("audio/opus"))
        self.assertEqual(".mp3", extension_for_mime("audio/mpeg"))
        self.assertEqual(".wav", extension_for_mime("audio/x-wav"))
        self.assertEqual(".m4a", extension_for_mime("audio/mp4"))
        self.assertEqual(".amr", extension_for_mime("audio/amr"))
        self.assertEqual(".webm", extension_for_mime("audio/webm"))
        self.assertEqual(".bin", extension_for_mime("video/mp4"))
        self.assertEqual(".bin", extension_for_mime(""))

    def test_transcription_label(self) -> None:
        self.assertEqual(
            "[语音 1 自动转写（可能有误）：你好]", asr_transcription_text(1, " 你好 ")
        )
        self.assertEqual("", asr_transcription_text(1, "   "))


class TestCacheHelpers(unittest.TestCase):
    def test_success_cache_ttl_and_size(self) -> None:
        from atri_x.asr import Result, new_success_cache

        clock = [0.0]
        cache = new_success_cache(max_entries=2, ttl=10.0, max_value_size=8, now=lambda: clock[0])
        cache.put("a", Result(text="hello"))
        self.assertTrue(cache.get("a").cached)
        self.assertIsNone(cache.get("missing"))
        clock[0] = 11.0
        self.assertIsNone(cache.get("a"))

        cache.put("big", Result(text="x" * 9))
        self.assertIsNone(cache.get("big"))
        cache.put("empty", Result(text="   "))
        self.assertIsNone(cache.get("empty"))

    def test_success_cache_evicts_earliest_expiry(self) -> None:
        from atri_x.asr import Result, new_success_cache

        clock = [0.0]
        cache = new_success_cache(max_entries=2, ttl=100.0, now=lambda: clock[0])
        cache.put("a", Result(text="a"))
        clock[0] = 1.0
        cache.put("b", Result(text="b"))
        clock[0] = 2.0
        cache.put("c", Result(text="c"))
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))

    def test_negative_cache_only_stores_deterministic(self) -> None:
        from atri_x.asr import new_negative_cache

        clock = [0.0]
        cache = new_negative_cache(max_entries=4, ttl=30.0, now=lambda: clock[0])
        cache.put("transient", HTTPError(500, "boom"))
        self.assertIsNone(cache.get("transient"))
        cache.put("auth", HTTPError(401, "bad key"))
        stored = cache.get("auth")
        self.assertIsNotNone(stored)
        self.assertEqual(401, stored.status_code)
        self.assertEqual("bad key", stored.message)
        clock[0] = 31.0
        self.assertIsNone(cache.get("auth"))


class TestReservationInterface(unittest.TestCase):
    def test_call_id_and_digest_input(self) -> None:
        self.assertEqual("turn-1:media:9:asr:0", reservation_call_id("turn-1", "media:9", 0))
        self.assertEqual("asr", KIND)
        self.assertEqual("p\x00m\x00media:9", reservation_digest_input("p", "m", "media:9"))

    def test_budget_ledger_protocol(self) -> None:
        class Ledger:
            def reserve_batch(
                self, ctx: Any, kind: str, requests: list[ReservationRequest]
            ) -> tuple[bool, str]:
                return True, ""

        self.assertIsInstance(Ledger(), BudgetLedger)
        self.assertNotIsInstance(object(), BudgetLedger)
        self.assertEqual("id", ReservationRequest(call_id="id").call_id)
        self.assertEqual("", ReservationRequest(call_id="id").digest)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
