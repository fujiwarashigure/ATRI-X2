"""``atri_x.imagegen`` 的单元测试。

对应 Go：``internal/imagegen/{client,prompt,optimize,search,library}_test.go``。

HTTP 层不 mock：用 :class:`http.server.ThreadingHTTPServer` 起本地假服务端，
真实走一遍请求 / 回包 / 参考图 / 错误分支。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from atri_x.imagegen import (
    MAX_IMAGE_BYTES,
    Client,
    Config,
    ImageGenError,
    NegativePromptMode,
    NegativePromptModeField,
    NegativePromptModeInline,
    NegativePromptModeOff,
    OptimizeOptions,
    PromptBlock,
    PromptPriorityCore,
    PromptPriorityOptional,
    PromptPriorityProtected,
    PromptPriorityStyle,
    Reference,
    Request,
    compose_blocks,
    filter_reserved_headers,
    filter_reserved_payload,
    normalize_negative_prompt_mode,
    prompt_lib,
)
from atri_x.imagegen import _extract_library
from atri_x.imagegen.search import LibraryScopes, library_hints, search

#: 上游 ``prompts/library.json`` 的条目数（抽取脚本 ``--check`` 的口径）。
UPSTREAM_COUNTS = {
    "templates": 15,
    "entries": 226,
    "negatives": 10,
    "presets": 10,
    "tips": 50,
    "vocabulary": 1096,
    "keywords": 15,
    "keyword_terms": 15,
}

PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
HARD_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# 假生图服务端
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # 静音
        return None

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self) -> dict[str, Any]:
        body = self._read_body()
        record = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "raw": body,
        }
        try:
            record["json"] = json.loads(body.decode("utf-8"))
        except ValueError:
            record["json"] = None
        self.server.records.append(record)  # type: ignore[attr-defined]
        return record

    def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        record = self._record()
        if self.path.endswith("/fail"):
            self._send(400, json.dumps({"error": {"message": "bad prompt"}}).encode())
            return
        if self.path.endswith("/no-image"):
            self._send(200, b'{"data": []}')
            return
        if self.path.endswith("/inline-error"):
            self._send(200, b'{"data": [], "error": {"message": "quota exhausted"}}')
            return
        if self.path.endswith("/data-url"):
            encoded = base64.b64encode(PNG_BYTES).decode()
            self._send(
                200,
                json.dumps({"data": [{"b64_json": "data:image/webp;base64," + encoded}]}).encode(),
            )
            return
        if self.path.endswith("/url"):
            host = self.headers.get("Host")
            self._send(200, json.dumps({"data": [{"url": f"http://{host}/image.png"}]}).encode())
            return
        if self.path.endswith("/huge"):
            encoded = base64.b64encode(PNG_BYTES * 16).decode()
            self._send(200, json.dumps({"data": [{"b64_json": encoded}]}).encode())
            return
        if self.path.endswith("/not-json"):
            self._send(200, b"<html>nope</html>")
            return
        if self.path.endswith("/not-base64"):
            self._send(200, json.dumps({"data": [{"b64_json": "!!!not base64!!!"}]}).encode())
            return
        self._send(
            200,
            json.dumps(
                {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode(), "revised_prompt": "  revised  "}]}
            ).encode(),
        )

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        if self.path == "/image.png":
            self._send(200, b"jpeg-bytes", content_type="image/jpeg")
            return
        if self.path == "/image-missing":
            self._send(404, b"gone", content_type="text/plain")
            return
        self._send(500, b"boom", content_type="text/plain")


class _Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.records = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.httpd.records  # type: ignore[attr-defined]

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
        self.server.records.clear()

    def client(self, **kw: Any) -> Client:
        kw.setdefault("api_key", "k")
        kw.setdefault("base_url", self.server.base_url + "/v1")
        return Client(Config(enabled=True, **kw))


# ---------------------------------------------------------------------------
# prompt.go
# ---------------------------------------------------------------------------

class TestComposeBlocks(unittest.TestCase):
    """对应 Go ``prompt_test.go``。"""

    def test_drops_optional_blocks_first(self) -> None:
        blocks = [
            PromptBlock(label="scene", text="a scene", priority=PromptPriorityCore),
            PromptBlock(label="artist", text="(artist_a:1.2)", priority=PromptPriorityProtected),
            PromptBlock(label="padding", text="x" * 200, priority=PromptPriorityStyle),
        ]
        result = compose_blocks(blocks, "\n\n", 60)
        self.assertFalse(result.over_limit)
        self.assertEqual(["padding"], result.dropped)
        self.assertEqual("a scene\n\n(artist_a:1.2)", result.prompt)

    def test_never_splits_a_protected_expression(self) -> None:
        blocks = [
            PromptBlock(label="artist", text="(artist_a:1.2)", priority=PromptPriorityProtected),
            PromptBlock(label="scene", text="z" * 50, priority=PromptPriorityCore),
        ]
        result = compose_blocks(blocks, "\n\n", 20)
        self.assertEqual("(artist_a:1.2)", result.prompt)
        self.assertIn("(", result.prompt)
        self.assertIn(")", result.prompt)

    def test_reports_over_limit_for_protected_only(self) -> None:
        blocks = [
            PromptBlock(label="artist", text="a" * 100, priority=PromptPriorityProtected),
            PromptBlock(label="scene", text="scene", priority=PromptPriorityCore),
        ]
        result = compose_blocks(blocks, "\n\n", 10)
        self.assertTrue(result.over_limit)
        self.assertEqual(100, result.runes)
        self.assertEqual(10, result.limit)

    def test_without_limit_keeps_everything(self) -> None:
        blocks = [
            PromptBlock(label="a", text="one", priority=PromptPriorityCore),
            PromptBlock(label="b", text="  ", priority=PromptPriorityProtected),
            PromptBlock(label="c", text="two", priority=PromptPriorityOptional),
        ]
        result = compose_blocks(blocks, "\n\n", 0)
        self.assertEqual([], result.dropped)
        self.assertFalse(result.over_limit)
        self.assertEqual("one\n\ntwo", result.prompt)

    def test_keeps_a_weighted_artist_string_whole_at_a_tight_limit(self) -> None:
        """(artist:1.2) 必须整段保留或整段丢弃，不能被切成半截。"""
        artist = "(artist_a:1.2), (artist_b:0.8)"
        result = compose_blocks(
            [
                PromptBlock(label="scene", text="s" * 40, priority=PromptPriorityCore),
                PromptBlock(label="artist", text=artist, priority=PromptPriorityProtected),
                PromptBlock(label="style", text="q" * 40, priority=PromptPriorityStyle),
            ],
            ", ",
            len(artist),
        )
        self.assertEqual(artist, result.prompt)
        self.assertNotIn("(artist_a:1.", result.prompt.replace(artist, ""))


class TestReservedFieldsAndModes(unittest.TestCase):
    """对应 Go ``prompt_test.go`` 的 filter / mode 用例。"""

    def test_filter_reserved_payload_refuses_core_keys(self) -> None:
        payload = {
            "prompt": "hijacked",
            "model": "other",
            "seed": 42,
            "style": "anime",
            "image": "ref",
            "QUALITY": "low",
        }
        kept, dropped = filter_reserved_payload(payload, "image", "negative_prompt")
        for key in ("prompt", "model", "image", "QUALITY"):
            self.assertNotIn(key, kept)
        self.assertEqual({"seed": 42, "style": "anime"}, kept)
        self.assertEqual(["QUALITY", "image", "model", "prompt"], dropped)

    def test_filter_reserved_payload_without_hits_returns_none(self) -> None:
        kept, dropped = filter_reserved_payload({"seed": 1}, "image", "negative_prompt")
        self.assertEqual({"seed": 1}, kept)
        self.assertIsNone(dropped)

    def test_filter_reserved_headers_refuses_authorization(self) -> None:
        kept, dropped = filter_reserved_headers({"Authorization": "Bearer other", "X-Trace": "1"})
        self.assertNotIn("Authorization", kept)
        self.assertEqual({"X-Trace": "1"}, kept)
        self.assertEqual(["Authorization"], dropped)

    def test_filter_reserved_headers_refuses_content_type(self) -> None:
        kept, dropped = filter_reserved_headers({"content-type": "text/plain", "X-Trace": "1"})
        self.assertEqual({"X-Trace": "1"}, kept)
        self.assertEqual(["content-type"], dropped)

    def test_negative_prompt_mode_normalization(self) -> None:
        cases = {
            "": NegativePromptMode.FIELD,
            "field": NegativePromptMode.FIELD,
            "inline": NegativePromptMode.INLINE,
            "OFF": NegativePromptMode.OFF,
            "junk": NegativePromptMode.FIELD,
        }
        for value, want in cases.items():
            self.assertEqual(want, normalize_negative_prompt_mode(value))

    def test_config_normalize_defaults_and_dropped_keys(self) -> None:
        config = Config(
            enabled=True,
            extra_payload={"seed": 1, "Prompt": "hijacked", "MODEL": "other"},
            extra_headers={"Authorization": "Bearer other", "X-Trace": "1", "content-type": "x"},
        )
        normalized = config.normalize()
        self.assertEqual("gpt-image-2.5", normalized.model)
        self.assertEqual("1024x1024", normalized.size)
        self.assertEqual("high", normalized.quality)
        self.assertEqual("png", normalized.output_format)
        self.assertEqual(180, normalized.timeout_seconds)
        self.assertEqual(4000, normalized.max_prompt_runes)
        self.assertEqual("image", normalized.reference_field)
        self.assertEqual("negative_prompt", normalized.negative_prompt_field)
        self.assertEqual("IMAGE_API_KEY", normalized.api_key_env)
        self.assertEqual("rules", normalized.optimize)
        self.assertEqual("phrase", normalized.optimize_term_mode)
        self.assertEqual("auto", normalized.optimize_rewrite)
        self.assertEqual("naming", normalized.optimize_rewrite_model)
        self.assertEqual(6, normalized.context_default_limit)
        # 逐键断言被剔除的键（已排序，与 Go 的 sort.Strings 一致）。
        self.assertEqual(["MODEL", "Prompt"], normalized.dropped_extra_payload)
        self.assertEqual(["Authorization", "content-type"], normalized.dropped_extra_headers)
        self.assertEqual({"seed": 1}, normalized.extra_payload)
        self.assertEqual({"X-Trace": "1"}, normalized.extra_headers)
        # Go 的 Normalize 是值接收者：调用方持有的配置不受影响。
        self.assertEqual(3, len(config.extra_payload))
        self.assertEqual([], config.dropped_extra_payload)

    def test_config_normalize_is_idempotent(self) -> None:
        once = Config(enabled=True, extra_payload={"prompt": "x", "seed": 2}).normalize()
        twice = once.normalize()
        self.assertEqual(once.extra_payload, twice.extra_payload)
        self.assertEqual(once.model, twice.model)
        # 第二次 Normalize 看到的 map 里已经没有 ``prompt``，所以「被剔除键」
        # 记录清空 —— Go 的 ``filterReservedPayload`` 此时也返回 nil，行为一致。
        self.assertEqual(["prompt"], once.dropped_extra_payload)
        self.assertEqual([], twice.dropped_extra_payload)


# ---------------------------------------------------------------------------
# client.go
# ---------------------------------------------------------------------------

class TestClientEndpoints(_ServerTestCase):
    async def test_base64_response(self) -> None:
        client = self.client(model="gpt-image-2.5", api_key_env="TEST_IMAGE_KEY")
        result = await client.generate(None, Request(prompt="a cat"))
        self.assertEqual(PNG_BYTES, result.data)
        self.assertEqual("image/png", result.mime_type)
        self.assertEqual("revised", result.revised_prompt)
        self.assertEqual("gpt-image-2.5", result.model)
        self.assertEqual("1024x1024", result.size)

        record = self.server.records[-1]
        self.assertEqual("/v1/images/generations", record["path"])
        self.assertEqual("Bearer k", record["headers"]["authorization"])
        self.assertEqual("application/json", record["headers"]["content-type"])
        self.assertEqual("a cat", record["json"]["prompt"])
        self.assertEqual("gpt-image-2.5", record["json"]["model"])
        self.assertEqual(1, record["json"]["n"])

    async def test_api_key_lookup(self) -> None:
        seen: list[str] = []

        def lookup(name: str) -> tuple[str, bool]:
            seen.append(name)
            return ("from-env", True)

        client = Client(
            Config(enabled=True, base_url=self.server.base_url + "/v1", api_key_env="TEST_IMAGE_KEY"),
            lookup,
        )
        await client.generate(None, Request(prompt="x"))
        self.assertEqual(["TEST_IMAGE_KEY"], seen)
        self.assertEqual("Bearer from-env", self.server.records[-1]["headers"]["authorization"])

    async def test_url_fallback_and_content_type(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/url", output_format="jpeg")
        result = await client.generate(None, Request(prompt="x"))
        self.assertEqual(b"jpeg-bytes", result.data)
        self.assertEqual("image/jpeg", result.mime_type)

    async def test_url_download_failure_reports_status(self) -> None:
        client = self.client()
        with self.assertRaises(ImageGenError) as ctx:
            await client._download(None, self.server.base_url + "/image-missing")
        self.assertIn("下载生图结果失败", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    async def test_data_url_base64_keeps_embedded_mime(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/data-url")
        result = await client.generate(None, Request(prompt="x"))
        self.assertEqual(PNG_BYTES, result.data)
        self.assertEqual("image/webp", result.mime_type)

    async def test_http_error_body_is_surfaced(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/fail")
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("生图服务返回 400", str(ctx.exception))
        self.assertIn("bad prompt", str(ctx.exception))

    async def test_inline_error_message(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/inline-error")
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("quota exhausted", str(ctx.exception))

    async def test_missing_data(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/no-image")
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("没有返回图片", str(ctx.exception))

    async def test_non_json_response(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/not-json")
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("解析生图响应失败", str(ctx.exception))

    async def test_oversized_image_is_rejected(self) -> None:
        from atri_x.imagegen import client as client_module

        original = client_module.MAX_IMAGE_BYTES
        client_module.MAX_IMAGE_BYTES = 4
        try:
            client = self.client(endpoint=self.server.base_url + "/v1/huge")
            with self.assertRaises(ImageGenError) as ctx:
                await client.generate(None, Request(prompt="x"))
        finally:
            client_module.MAX_IMAGE_BYTES = original
        self.assertIn("bytes 上限", str(ctx.exception))

    async def test_oversized_response_is_rejected(self) -> None:
        from atri_x.imagegen import client as client_module

        original = client_module.MAX_RESPONSE_BYTES
        client_module.MAX_RESPONSE_BYTES = 8
        try:
            client = self.client()
            with self.assertRaises(ImageGenError) as ctx:
                await client.generate(None, Request(prompt="x"))
        finally:
            client_module.MAX_RESPONSE_BYTES = original
        self.assertIn("解析生图响应失败", str(ctx.exception))

    async def test_invalid_base64_is_rejected(self) -> None:
        client = self.client(endpoint=self.server.base_url + "/v1/not-base64")
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("解码生图结果失败", str(ctx.exception))

    async def test_requires_api_key_and_prompt(self) -> None:
        client = Client(Config(enabled=True, base_url=self.server.base_url + "/v1"))
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("API Key", str(ctx.exception))
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="  "))
        self.assertIn("prompt 不能为空", str(ctx.exception))

    async def test_disabled_client_and_missing_base_url(self) -> None:
        with self.assertRaises(ImageGenError) as ctx:
            await Client(Config()).generate(None, Request(prompt="x"))
        self.assertIn("未启用", str(ctx.exception))
        client = Client(Config(enabled=True, api_key="k"))
        with self.assertRaises(ImageGenError) as ctx:
            await client.generate(None, Request(prompt="x"))
        self.assertIn("base_url 未配置", str(ctx.exception))


class TestClientExtras(_ServerTestCase):
    async def test_reserved_extra_payload_never_reaches_the_server(self) -> None:
        client = self.client(
            extra_payload={"prompt": "hijacked", "model": "other", "seed": 7},
            extra_headers={"Authorization": "Bearer other", "X-Trace": "1"},
        )
        self.assertEqual(["model", "prompt"], client.config.dropped_extra_payload)
        self.assertEqual(["Authorization"], client.config.dropped_extra_headers)
        await client.generate(None, Request(prompt="a cat"))
        record = self.server.records[-1]
        self.assertEqual("a cat", record["json"]["prompt"])
        self.assertEqual("gpt-image-2.5", record["json"]["model"])
        self.assertEqual(7, record["json"]["seed"])
        self.assertEqual("Bearer k", record["headers"]["authorization"])
        self.assertEqual("1", record["headers"]["x-trace"])

    async def test_extra_payload_can_add_new_fields(self) -> None:
        client = self.client(extra_payload={"style": "anime", "watermark": False})
        await client.generate(None, Request(prompt="x"))
        record = self.server.records[-1]
        self.assertEqual("anime", record["json"]["style"])
        self.assertIs(False, record["json"]["watermark"])

    async def test_negative_prompt_mode_field(self) -> None:
        client = self.client(negative_prompt_mode=NegativePromptModeField)
        await client.generate(None, Request(prompt="x", negative_prompt="blurry, low quality"))
        self.assertEqual("blurry, low quality", self.server.records[-1]["json"]["negative_prompt"])

    async def test_negative_prompt_mode_field_with_custom_key(self) -> None:
        client = self.client(negative_prompt_mode="field", negative_prompt_field="negative")
        await client.generate(None, Request(prompt="x", negative_prompt="blurry"))
        record = self.server.records[-1]
        self.assertEqual("blurry", record["json"]["negative"])
        self.assertNotIn("negative_prompt", record["json"])

    async def test_negative_prompt_mode_inline_does_not_send_a_field(self) -> None:
        """inline 由调用方折进正向提示词，客户端不额外发字段。"""
        client = self.client(negative_prompt_mode=NegativePromptModeInline)
        await client.generate(None, Request(prompt="avoid: blurry", negative_prompt="blurry"))
        record = self.server.records[-1]
        self.assertNotIn("negative_prompt", record["json"])
        for key, value in record["json"].items():
            if key == "prompt":
                continue
            self.assertNotEqual("blurry", value)

    async def test_negative_prompt_mode_off_drops_it(self) -> None:
        client = self.client(negative_prompt_mode=NegativePromptModeOff)
        await client.generate(None, Request(prompt="x", negative_prompt="blurry"))
        record = self.server.records[-1]
        self.assertNotIn("negative_prompt", record["json"])
        self.assertNotIn("blurry", json.dumps(record["json"]))

    async def test_references_are_data_uris(self) -> None:
        client = self.client(supports_reference=True)
        await client.generate(
            None,
            Request(prompt="one", references=[Reference(data=b"a", mime_type="image/png")]),
        )
        single = self.server.records[-1]["json"]["image"]
        self.assertTrue(single.startswith("data:image/png;base64,"))

        await client.generate(
            None,
            Request(
                prompt="two",
                references=[
                    Reference(data=b"a", mime_type="image/png"),
                    Reference(data=b"b", mime_type="image/jpeg"),
                ],
            ),
        )
        multiple = self.server.records[-1]["json"]["image"]
        self.assertIsInstance(multiple, list)
        self.assertEqual(2, len(multiple))
        for value in multiple:
            self.assertTrue(value.startswith("data:image/"))

    async def test_reference_defaults_to_png_mime_and_custom_field(self) -> None:
        client = self.client(supports_reference=True, reference_field="ref_image")
        await client.generate(None, Request(prompt="x", reference_data=b"a"))
        record = self.server.records[-1]
        self.assertTrue(record["json"]["ref_image"].startswith("data:image/png;base64,"))
        self.assertNotIn("image", record["json"])

    async def test_references_are_ignored_when_unsupported(self) -> None:
        client = self.client(supports_reference=False)
        await client.generate(
            None, Request(prompt="x", references=[Reference(data=b"a", mime_type="image/png")])
        )
        self.assertNotIn("image", self.server.records[-1]["json"])

    async def test_request_overrides_size_quality_and_n(self) -> None:
        client = self.client()
        await client.generate(None, Request(prompt="x", size="512x512", quality="low", n=3))
        record = self.server.records[-1]["json"]
        self.assertEqual("512x512", record["size"])
        self.assertEqual("low", record["quality"])
        self.assertEqual(3, record["n"])

    async def test_response_format_is_forwarded_when_configured(self) -> None:
        client = self.client(response_format="b64_json")
        await client.generate(None, Request(prompt="x"))
        self.assertEqual("b64_json", self.server.records[-1]["json"]["response_format"])

    async def test_endpoint_appends_images_generations_once(self) -> None:
        client = Client(Config(enabled=True, base_url=self.server.base_url + "/v1/images/generations", api_key="k"))
        await client.generate(None, Request(prompt="x"))
        self.assertEqual("/v1/images/generations", self.server.records[-1]["path"])

    async def test_config_and_enabled(self) -> None:
        client = self.client()
        self.assertTrue(client.enabled())
        self.assertEqual("gpt-image-2.5", client.config.model)
        self.assertFalse(Client(Config()).enabled())


# ---------------------------------------------------------------------------
# library.go / search.go / optimize.go
# ---------------------------------------------------------------------------

class TestPromptLibrary(unittest.TestCase):
    def test_library_loads_and_matches_upstream_counts(self) -> None:
        library = prompt_lib()
        self.assertEqual(UPSTREAM_COUNTS["entries"], len(library.entries))
        self.assertEqual(UPSTREAM_COUNTS["templates"], len(library.templates))
        self.assertEqual(UPSTREAM_COUNTS["negatives"], len(library.negatives))
        self.assertEqual(UPSTREAM_COUNTS["presets"], len(library.presets))
        self.assertEqual(UPSTREAM_COUNTS["tips"], len(library.tips))
        self.assertEqual(UPSTREAM_COUNTS["vocabulary"], len(library.vocabulary))
        self.assertEqual(UPSTREAM_COUNTS["keywords"], len(library.keywords))
        self.assertEqual(UPSTREAM_COUNTS["keyword_terms"], len(library.keyword_terms))
        self.assertGreater(library.version, 0)
        self.assertTrue(library.source)

    def test_library_is_cached(self) -> None:
        self.assertIs(prompt_lib(), prompt_lib())

    def test_library_splits_tags_to_words(self) -> None:
        """对应 Go ``TestLibrarySplitsTagsToWords``。"""
        library = prompt_lib()
        for name, items in library.keywords.items():
            for item in items:
                self.assertNotIn(",", item, name)
        for group in library.negatives:
            for item in group.items:
                self.assertNotIn(",", item, group.category)
        for entry in library.entries:
            for term in entry.terms:
                self.assertNotIn(" ", term)
                self.assertNotIn(",", term)
        self.assertTrue(library.keyword_terms)
        self.assertGreaterEqual(len(library.vocabulary), 100)

    def test_vocabulary_count_and_groups(self) -> None:
        library = prompt_lib()
        self.assertGreater(library.vocabulary_count("background"), 0)
        self.assertEqual(0, library.vocabulary_count("__nope__"))
        self.assertTrue(library.keyword_group("画质"))
        self.assertEqual([], library.keyword_group("__nope__"))
        self.assertTrue(library.negative_items("quality"))
        self.assertEqual([], library.negative_items("__nope__"))

    def test_extract_script_check_matches_upstream(self) -> None:
        """用抽取脚本 ``--check`` 的口径断言：本地库与上游逐字节一致。"""
        report = _extract_library.check()
        if not report.upstream_available:
            self.skipTest(f"上游不可用：{report.upstream}（无法比对，不能算作一致）")
        self.assertTrue(report.ok, report.describe())
        self.assertEqual(report.upstream_sha256, report.target_sha256)
        # ``--check`` 的退出码口径（输出重定向掉，避免测试日志里混入报表）。
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = _extract_library.main(["--check"])
        self.assertEqual(0, code, buffer.getvalue())
        for name, count in UPSTREAM_COUNTS.items():
            self.assertEqual(count, report.upstream_counts.get(name), name)

    def test_extract_check_detects_drift(self) -> None:
        target = _extract_library.default_paths()[1]
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        broken_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".imagegen-test-drift"
        )
        os.makedirs(broken_dir, mode=0o777, exist_ok=True)
        broken = os.path.join(broken_dir, "library.json")
        try:
            payload = dict(payload)
            payload["entries"] = payload["entries"][:-1]
            with open(broken, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            report = _extract_library.check(target=broken)
            self.assertFalse(report.ok)
            self.assertTrue(any("entries" in item for item in report.problems), report.problems)
            self.assertEqual(_extract_library.CHECK_DRIFT, report.exit_code)
        finally:
            import shutil

            shutil.rmtree(broken_dir, ignore_errors=True)


class TestLibrarySearch(unittest.TestCase):
    def test_search_all_scopes_and_limit(self) -> None:
        library = prompt_lib()
        hits = search(library, "赛博朋克 城市")
        self.assertTrue(hits)
        self.assertLessEqual(len(hits), 5)
        scopes = {hit.scope for hit in hits}
        self.assertTrue(scopes.issubset(set(LibraryScopes)))
        scores = [hit.score for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_search_scope_filter_and_empty_query(self) -> None:
        library = prompt_lib()
        self.assertEqual([], search(library, "   "))
        only_negative = search(library, "blurry", ["negative"])
        self.assertTrue(only_negative)
        for hit in only_negative:
            self.assertEqual("negative", hit.scope)
        self.assertEqual([], search(library, "blurry", ["tip"]))

    def test_search_limit_is_clamped(self) -> None:
        library = prompt_lib()
        hits = search(library, "portrait", None, 100)
        self.assertLessEqual(len(hits), 20)

    def test_entry_hits_carry_anchors_and_terms(self) -> None:
        library = prompt_lib()
        # 注意 token 至少要 3 个 rune（Go ``splitTagWords`` 的口径），
        # 所以「头像」这种两字查询不算 token，查不到东西是预期行为。
        hits = search(library, "赛博朋克", ["entry"], 5)
        self.assertTrue(hits)
        self.assertTrue(hits[0].anchors or hits[0].terms)

    def test_library_hints_render_compact_lines(self) -> None:
        library = prompt_lib()
        hints = library_hints(library, "赛博朋克", 3)
        self.assertTrue(hints.startswith("- ["))
        for line in hints.splitlines():
            self.assertTrue(line.startswith("- ["))

    def test_library_methods_delegate(self) -> None:
        library = prompt_lib()
        self.assertEqual(search(library, "赛博朋克", None, 3), library.search("赛博朋克", None, 3))
        self.assertEqual(
            library_hints(library, "赛博朋克", 2), library.library_hints("赛博朋克", 2)
        )


class TestOptimize(unittest.TestCase):
    """对应 Go ``optimize_test.go``。"""

    def setUp(self) -> None:
        self.library = prompt_lib()

    def test_avatar_use_case(self) -> None:
        result = self.library.optimize("给猫娘画一个头像")
        self.assertTrue(result.prompt.startswith("给猫娘画一个头像"))
        self.assertIn("use=头像", result.matched)
        self.assertIn("1:1", result.prompt)
        self.assertIn("Square avatar", result.prompt)
        self.assertIn("low quality", result.negative)
        self.assertIn("extra fingers", result.negative)

    def test_keeps_existing_ratio(self) -> None:
        result = self.library.optimize("赛博朋克城市夜景，16:9")
        self.assertNotIn("widescreen composition", result.prompt)
        self.assertIn("neon signs", result.prompt)

    def test_poster_uses_print_preset(self) -> None:
        result = self.library.optimize("一张海报，标题是 SUMMER")
        self.assertIn("印刷海报", result.matched)
        self.assertIn("Print-ready", result.prompt)
        self.assertIn("watermark", result.negative)

    def test_edit_guard(self) -> None:
        result = self.library.optimize("局部修改：把背景换成沙滩")
        self.assertIn("do not redraw the whole image", result.prompt)

    def test_truncates_added_text(self) -> None:
        result = self.library.optimize("水彩风格的森林小路", OptimizeOptions(max_added_runes=20))
        self.assertTrue(result.prompt.startswith("水彩风格的森林小路"))
        added = result.prompt[len("水彩风格的森林小路") :].strip()
        self.assertLessEqual(len(added), 20)

    def test_empty_input(self) -> None:
        result = self.library.optimize("   ")
        self.assertEqual("", result.prompt)
        self.assertEqual("", result.negative)

    def test_nil_library_and_none_options(self) -> None:
        from atri_x.imagegen.optimize import optimize

        result = optimize(None, "x")
        self.assertEqual("x", result.prompt)
        self.assertEqual("", result.negative)

    def test_matches_english_word_level(self) -> None:
        result = self.library.optimize("a cyberpunk street at night")
        self.assertIn("赛博朋克城市", result.matched)
        self.assertIn("neon signs", result.prompt)

    def test_tag_mode(self) -> None:
        result = self.library.optimize(
            "a cyberpunk street at night", OptimizeOptions(term_mode="tag", max_tags=8)
        )
        self.assertTrue(result.prompt.startswith("a cyberpunk street at night"))
        self.assertTrue(result.tags)
        self.assertLessEqual(len(result.tags), 8)
        for tag in result.tags:
            self.assertNotIn(" ", tag)
            self.assertNotIn(",", tag)
        for want in ("neon", "ultra-detailed"):
            self.assertIn(want, result.prompt)

        avatar = self.library.optimize("给猫娘画一个头像", OptimizeOptions(term_mode="tag", max_tags=6))
        self.assertIn("1:1", ",".join(avatar.tags))

        edited = self.library.optimize("局部修改：把背景换成沙滩", OptimizeOptions(term_mode="tag"))
        self.assertIn("do not redraw the whole image", edited.prompt)
        self.assertEqual([], edited.tags)

    def test_anchor_limits_are_respected(self) -> None:
        result = self.library.optimize(
            "赛博朋克城市夜景", OptimizeOptions(max_anchors=1, max_negatives=2)
        )
        self.assertLessEqual(len(result.anchors), 1)
        self.assertLessEqual(len(result.negative.split(", ")), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
