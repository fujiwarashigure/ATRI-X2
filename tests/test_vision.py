"""``atri_x.vision`` 的单元测试。

对应 Go：``internal/vision/{analysis,cache,counters,fingerprint,prompt,service}_test.go``
与 ``limits_test.go``。

不追求与 Go 1:1 的函数级覆盖，只覆盖公开行为的主要路径：解析与限幅、缓存
（TTL/驱逐/负缓存）、指纹、计数快照、prompt 归一化，以及 Service 的端到端
（缓存命中、合并、输出上限、截断、错误分类）。

视觉图像的预处理与上游调用都是 I/O 适配器，测试用 fake：``_patch_prepare``
替换 :func:`atri_x.media.prepare_vision_image_context`，``FakeLLM`` 提供异步
流式块。这样测试既快又不依赖位图编解码。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from atri_x.errors import ErrTooManyWaiters, ImageAnalysisEmptyError, TooManyWaitersError
from atri_x.llm.errors import APIError
from atri_x.llm.llm import StreamChunk
from atri_x.safecontext import CancelledError, with_cancel
from atri_x.vision import (
    CacheIdentity,
    CacheSchemaVersion,
    Counters,
    ImageAnalysis,
    NegativeCache,
    NoopMetrics,
    Options,
    PreprocessVersion,
    Prompt,
    Request,
    RequestFingerprint,
    Result,
    Service,
    SuccessCache,
    chat_description,
    classify_error,
    clean_text,
    extract_json_object,
    hash_text,
    image_analysis_prompt,
    image_prompt,
    is_cancellation,
    normalize_image_prompt_language,
    normalize_image_prompt_target,
    parse_image_analysis,
    truncated,
)
from atri_x.errors import ErrImageAnalysisEmpty
from tests._vision_fakes import FakeLLM, patch_prepare


# ---------------------------------------------------------------------------
# 文本清洗与 JSON 抽取
# ---------------------------------------------------------------------------

class TestCleanText(unittest.TestCase):
    def test_strips_plain_text(self) -> None:
        self.assertEqual(clean_text("  描述  "), "描述")

    def test_removes_code_fence_with_language(self) -> None:
        self.assertEqual(clean_text("```json\n{\"a\":1}\n```"), '{"a":1}')

    def test_removes_fence_without_newline(self) -> None:
        self.assertEqual(clean_text("```描述```"), "描述")

    def test_keeps_inner_fence_like_text(self) -> None:
        self.assertEqual(clean_text("a\n```b```\nc"), "a\n```b```\nc")


class TestExtractJSONObject(unittest.TestCase):
    def test_returns_first_balanced_object(self) -> None:
        text = 'prose {"a": {"b": 1}} trailing {"c": 2}'
        self.assertEqual(extract_json_object(text), '{"a": {"b": 1}}')

    def test_braces_inside_string_do_not_close_early(self) -> None:
        text = '{"a": "} not the end", "b": 2}'
        self.assertEqual(extract_json_object(text), text)

    def test_escaped_quote_inside_string(self) -> None:
        text = '{"a": "\\"}", "b": 1}'
        self.assertEqual(extract_json_object(text), text)

    def test_returns_empty_without_object(self) -> None:
        self.assertEqual(extract_json_object("no object here"), "")

    def test_returns_empty_for_unbalanced(self) -> None:
        self.assertEqual(extract_json_object('{"a": 1'), "")


# ---------------------------------------------------------------------------
# 结构化分析
# ---------------------------------------------------------------------------

class TestParseImageAnalysis(unittest.TestCase):
    def test_parses_prose_wrapped_object(self) -> None:
        payload = {
            "positive_prompt": "  1girl, red dress  ",
            "negative_prompt": " blurry ",
            "style_description": " 平涂 ",
            "style_tags": ["fine lineart", "Fine Lineart ", "pastel colors"],
            "warnings": ["无法判断光源"],
        }
        analysis = parse_image_analysis("好的：\n" + json.dumps(payload, ensure_ascii=False))
        self.assertIsInstance(analysis, ImageAnalysis)
        self.assertEqual(analysis.positive_prompt, "1girl, red dress")
        self.assertEqual(analysis.negative_prompt, "blurry")
        self.assertEqual(analysis.style_description, "平涂")
        # 小写去重，保留首个写法。
        self.assertEqual(analysis.style_tags, ["fine lineart", "pastel colors"])
        self.assertEqual(analysis.warnings, ["无法判断光源"])

    def test_parses_code_fenced_object(self) -> None:
        raw = '```json\n{"positive_prompt": "cat"}\n```'
        self.assertEqual(parse_image_analysis(raw).positive_prompt, "cat")

    def test_requires_usable_prompt(self) -> None:
        with self.assertRaises(ImageAnalysisEmptyError) as caught:
            parse_image_analysis("no json at all")
        # 哨兵以单例实例导出，判定用 `is`（PORTING.md §1.5）。
        self.assertIs(caught.exception, ErrImageAnalysisEmpty)
        with self.assertRaises(ImageAnalysisEmptyError):
            parse_image_analysis('{"positive_prompt": "   "}')

    def test_rejects_oversized_prompt(self) -> None:
        with self.assertRaises(ImageAnalysisEmptyError):
            parse_image_analysis(json.dumps({"positive_prompt": "x" * 6001}))

    def test_malformed_json_raises_decode_error(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            parse_image_analysis('{"positive_prompt": }')

    def test_cleans_lists_with_limits(self) -> None:
        payload = {
            "positive_prompt": "ok",
            "style_tags": ["a" * 41, "  keep   me  "] + [f"tag{i}" for i in range(20)],
        }
        analysis = parse_image_analysis(json.dumps(payload))
        self.assertEqual(analysis.style_tags[0], "keep me")
        self.assertEqual(len(analysis.style_tags), 12)


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

class TestFingerprint(unittest.TestCase):
    def _identity(self, **overrides: object) -> CacheIdentity:
        base = dict(
            schema_version=CacheSchemaVersion,
            media_id="media:" + "a" * 64,
            provider="openai",
            model="gpt-4o",
            prompt_version="image-to-prompt.v1",
            prompt_hash=hash_text("sys\x00user"),
            preprocess_version=PreprocessVersion,
            request_hash="deadbeef",
        )
        base.update(overrides)
        return CacheIdentity(**base)

    def test_key_is_stable_and_deterministic(self) -> None:
        first = self._identity().key()
        second = self._identity().key()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_omitempty_fields_are_absent_when_zero(self) -> None:
        payload = self._identity().to_json()
        self.assertNotIn("endpoint", payload)
        self.assertNotIn("credential_epoch", payload)
        with_values = self._identity(endpoint="https://api.example.com", credential_epoch=7)
        self.assertIn('"endpoint":"https://api.example.com"', with_values.to_json())
        self.assertIn('"credential_epoch":7', with_values.to_json())

    def test_field_order_matches_go_struct(self) -> None:
        payload = self._identity(endpoint="e", credential_epoch=1).to_json()
        order = [
            "schema_version",
            "media_id",
            "provider",
            "endpoint",
            "model",
            "prompt_version",
            "prompt_hash",
            "preprocess_version",
            "request_hash",
            "credential_epoch",
        ]
        positions = [payload.index('"' + name + '"') for name in order]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(payload.startswith(" "))
        self.assertNotIn(", ", payload)

    def test_html_escaping_matches_go_json(self) -> None:
        payload = self._identity(media_id="<a>&b").to_json()
        self.assertIn("\\u003ca\\u003e\\u0026b", payload)
        self.assertNotIn("<a>", payload)

    def test_credential_epoch_changes_key(self) -> None:
        self.assertNotEqual(
            self._identity().key(), self._identity(credential_epoch=1).key()
        )

    def test_request_fingerprint_encodes_go_numbers(self) -> None:
        payload = RequestFingerprint(
            max_tokens=512, temperature=0.7, max_edge=1536, max_image_bytes=1024,
            output_mime_type="image/jpeg",
        ).to_json()
        self.assertEqual(
            payload,
            '{"max_tokens":512,"temperature":0.7,"max_edge":1536,'
            '"max_image_bytes":1024,"output_mime_type":"image/jpeg"}',
        )
        self.assertEqual(
            RequestFingerprint(temperature=0.0).to_json().count('"temperature":0'),
            1,
        )

    def test_hash_text_is_sha256_hex(self) -> None:
        self.assertEqual(len(hash_text("abc")), 64)
        self.assertNotEqual(hash_text("abc"), hash_text("abd"))


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestSuccessCache(unittest.IsolatedAsyncioTestCase):
    async def test_put_get_marks_cached(self) -> None:
        cache = SuccessCache(now=_Clock())
        await cache.put("k", Result(text=" 描述 ", provider="p", model="m"))
        value, ok = await cache.get("k")
        self.assertTrue(ok)
        self.assertEqual(value.text, "描述")
        self.assertTrue(value.cached)

    async def test_entry_expires(self) -> None:
        clock = _Clock()
        cache = SuccessCache(ttl=10.0, now=clock)
        await cache.put("k", Result(text="v"))
        clock.advance(10.0)
        self.assertEqual(await cache.get("k"), (Result(), False))

    async def test_skips_empty_and_oversized(self) -> None:
        cache = SuccessCache(max_value_size=4, now=_Clock())
        await cache.put("empty", Result(text="   "))
        await cache.put("big", Result(text="12345"))
        self.assertEqual(await cache.get("empty"), (Result(), False))
        self.assertEqual(await cache.get("big"), (Result(), False))

    async def test_evicts_earliest_expiry(self) -> None:
        clock = _Clock()
        cache = SuccessCache(max_entries=2, ttl=100.0, now=clock)
        await cache.put("a", Result(text="a"))
        clock.advance(1.0)
        await cache.put("b", Result(text="b"))
        clock.advance(1.0)
        await cache.put("c", Result(text="c"))
        self.assertEqual(await cache.get("a"), (Result(), False))
        self.assertTrue((await cache.get("b"))[1])
        self.assertTrue((await cache.get("c"))[1])

    async def test_rejects_oversized_utf8(self) -> None:
        cache = SuccessCache(max_value_size=3, now=_Clock())
        await cache.put("k", Result(text="中文"))  # 6 字节
        self.assertEqual(await cache.get("k"), (Result(), False))


class TestNegativeCache(unittest.IsolatedAsyncioTestCase):
    async def test_stores_deterministic_failure(self) -> None:
        cache = NegativeCache(now=_Clock())
        err = APIError(
            "model not found", status_code=404, code="model_not_found",
            type="invalid_request_error", param="model", category="model_not_found",
        )
        await cache.put("k", err)
        replayed = await cache.get("k")
        self.assertIsInstance(replayed, APIError)
        self.assertEqual(replayed.status_code, 404)
        self.assertEqual(replayed.code, "model_not_found")
        self.assertEqual(replayed.category, "model_not_found")

    async def test_ignores_transient_and_cancellation(self) -> None:
        cache = NegativeCache(now=_Clock())
        await cache.put("rate", APIError("slow down", status_code=429))
        await cache.put("cancel", CancelledError("context canceled"))
        await cache.put("plain", ValueError("nope"))
        self.assertIsNone(await cache.get("rate"))
        self.assertIsNone(await cache.get("cancel"))
        self.assertIsNone(await cache.get("plain"))

    async def test_entry_expires(self) -> None:
        clock = _Clock()
        cache = NegativeCache(ttl=5.0, now=clock)
        await cache.put("k", APIError("bad", status_code=400, code="invalid_parameter"))
        clock.advance(5.0)
        self.assertIsNone(await cache.get("k"))

    def test_cancellation_detection_walks_cause(self) -> None:
        self.assertTrue(is_cancellation(CancelledError("x")))
        self.assertTrue(is_cancellation(asyncio.CancelledError()))
        wrapped = RuntimeError("wrap")
        wrapped.__cause__ = asyncio.CancelledError()
        self.assertTrue(is_cancellation(wrapped))
        self.assertFalse(is_cancellation(ValueError("nope")))


# ---------------------------------------------------------------------------
# 计数
# ---------------------------------------------------------------------------

class TestCounters(unittest.TestCase):
    def test_records_and_snapshots(self) -> None:
        counters = Counters()
        counters.cache_lookup("miss")
        counters.cache_lookup("hit")
        counters.coalesced()
        counters.job_started()
        counters.duration("total", 0.5)
        counters.duration("total", 0.25)
        counters.error("timeout")
        snapshot = counters.snapshot()
        self.assertEqual(snapshot.cache, {"miss": 1, "hit": 1})
        self.assertEqual(snapshot.coalesced, 1)
        self.assertEqual(snapshot.jobs, 1)
        self.assertAlmostEqual(snapshot.durations["total"], 0.75)
        self.assertEqual(snapshot.errors, {"timeout": 1})

    def test_snapshot_is_a_deep_copy(self) -> None:
        counters = Counters()
        counters.error("http_500")
        snapshot = counters.metrics_snapshot()
        snapshot.errors["http_500"] = 99
        self.assertEqual(counters.snapshot().errors, {"http_500": 1})

    def test_noop_metrics_accepts_everything(self) -> None:
        metrics = NoopMetrics()
        metrics.cache_lookup("hit")
        metrics.coalesced()
        metrics.job_started()
        metrics.duration("total", 1.0)
        metrics.error("other")


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

class TestPromptTemplates(unittest.TestCase):
    def test_normalizes_target_and_language(self) -> None:
        self.assertEqual(normalize_image_prompt_target(" SDXL "), "sdxl")
        self.assertEqual(normalize_image_prompt_target("flux"), "flux")
        self.assertEqual(normalize_image_prompt_target("anything"), "general")
        self.assertEqual(normalize_image_prompt_language("EN"), "en")
        self.assertEqual(normalize_image_prompt_language("zh"), "zh")

    def test_image_prompt_carries_version_and_target_hint(self) -> None:
        prompt = image_prompt("sdxl", "en")
        self.assertEqual(prompt.version, "image-to-prompt.v1")
        self.assertIn("SDXL", prompt.user)
        self.assertIn("Output the prompt in English.", prompt.user)
        self.assertIn("你是绘图提示词工程师", prompt.system)

    def test_analysis_prompt_is_a_separate_template(self) -> None:
        prompt = image_analysis_prompt("flux", "zh")
        self.assertEqual(prompt.version, "image-to-prompt.structured.v1")
        self.assertIn("JSON", prompt.system)
        self.assertIn("Flux", prompt.user)

    def test_chat_description_is_a_separate_template(self) -> None:
        prompt = chat_description("en")
        self.assertEqual(prompt.version, "chat-image-description.v1")
        self.assertIn("English", prompt.user)

    def test_clean_text_is_shared_with_templates(self) -> None:
        self.assertEqual(clean_text("```\nx\n```"), "x")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_truncated_matches_go_finish_reasons(self) -> None:
        for value in ("length", "MAX_TOKENS", " max_output_tokens "):
            self.assertTrue(truncated(value))
        for value in ("stop", "", "tool_calls"):
            self.assertFalse(truncated(value))

    def test_classify_error_classes(self) -> None:
        self.assertEqual(classify_error(None), "none")
        self.assertEqual(classify_error(asyncio.CancelledError()), "canceled")
        self.assertEqual(classify_error(TimeoutError("late")), "timeout")
        self.assertEqual(
            classify_error(APIError("nope", status_code=429)), "http_429"
        )
        self.assertEqual(classify_error(ValueError("boom")), "other")


# ---------------------------------------------------------------------------
# Service 端到端
# ---------------------------------------------------------------------------

def _request(media_id: str = "media:" + "b" * 64) -> Request:
    return Request(
        media_id=media_id,
        data=b"fake-image-bytes",
        mime_type="image/png",
        prompt=Prompt(version="image-to-prompt.v1", system="sys", user="usr"),
    )


def _service(client: object, counters: Counters | None = None, **overrides: object) -> Service:
    options = dict(
        client=client,
        provider="openai",
        model="qwen-vl-max",
        max_tokens=512,
        temperature=0.2,
        max_edge=1536,
        max_image_bytes=1024 * 1024,
        metrics=counters if counters is not None else NoopMetrics(),
        timeout=5.0,
        shared_timeout=5.0,
    )
    options.update(overrides)
    return Service(Options(**options))


class TestService(unittest.IsolatedAsyncioTestCase):
    async def test_describe_populates_cache_and_stats(self) -> None:
        client = FakeLLM([StreamChunk(delta_content=" 一只猫 "), StreamChunk(finish_reason="stop")])
        counters = Counters()
        service = _service(client, counters)
        with patch_prepare() as prepare:
            result = await service.describe(None, _request())
        self.assertEqual(result.text, "一只猫")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.model, "qwen-vl-max")
        self.assertFalse(result.cached)
        self.assertEqual(client.calls, 1)
        self.assertEqual(prepare.call_count, 1)

        with patch_prepare():
            cached = await service.describe(None, _request())
        self.assertTrue(cached.cached)
        self.assertEqual(client.calls, 1)

        stats = service.stats()
        self.assertEqual(stats.jobs, 1)
        self.assertEqual(stats.cache, {"miss": 1, "hit": 1})
        self.assertEqual(stats.max_concurrent_jobs, 10)
        self.assertEqual(stats.max_waiters, 16)
        self.assertEqual(stats.active_jobs, 0)
        self.assertEqual(stats.queued_jobs, 0)
        self.assertIn("total", stats.durations_ms)
        self.assertEqual(stats.errors, {})
        self.assertEqual(stats.to_json()["jobs_started"], 1)

    async def test_describe_requires_model_and_media_id(self) -> None:
        service = _service(FakeLLM([]), model="")
        with self.assertRaises(ValueError):
            await service.describe(None, _request())
        service = _service(FakeLLM([]))
        with self.assertRaises(ValueError):
            await service.describe(None, Request(media_id="  "))

    async def test_describe_sends_data_url_message(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client)
        with patch_prepare():
            await service.describe(None, _request())
        request = client.requests[0]
        self.assertEqual(request.model, "qwen-vl-max")
        self.assertEqual(request.max_tokens, 512)
        self.assertEqual(request.temperature, 0.2)
        image = request.messages[1].segments[1]
        self.assertTrue(image.url.startswith("data:image/png;base64,"))

    async def test_output_cap_stops_the_answer(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="x" * 64)])
        service = _service(client, max_output_bytes=8)
        with patch_prepare():
            with self.assertRaises(ValueError) as caught:
                await service.describe(None, _request())
        self.assertIn("exceeded", str(caught.exception))

    async def test_truncated_finish_reason_is_an_error(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="partial", finish_reason="length")])
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(ValueError) as caught:
                await service.describe(None, _request())
        self.assertIn("truncated", str(caught.exception))

    async def test_empty_answer_is_an_error(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="   ", finish_reason="stop")])
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(ValueError):
                await service.describe(None, _request())

    async def test_stream_error_chunk_propagates(self) -> None:
        client = FakeLLM([StreamChunk(error=RuntimeError("upstream died"))])
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(RuntimeError):
                await service.describe(None, _request())

    async def test_deterministic_failure_is_negative_cached(self) -> None:
        api_err = APIError(
            "model not found", status_code=404, code="model_not_found",
            category="model_not_found",
        )
        client = FakeLLM(error=api_err)
        counters = Counters()
        service = _service(client, counters)
        with patch_prepare():
            with self.assertRaises(APIError):
                await service.describe(None, _request())
            with self.assertRaises(APIError):
                await service.describe(None, _request())
        # 第二次由负缓存回放，没有再次打上游。
        self.assertEqual(client.calls, 1)
        self.assertEqual(counters.snapshot().cache, {"miss": 1, "negative_hit": 1})
        self.assertEqual(counters.snapshot().errors, {"http_404": 1})

    async def test_transient_failure_is_not_cached(self) -> None:
        client = FakeLLM(error=APIError("slow down", status_code=429))
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(APIError):
                await service.describe(None, _request())
            with self.assertRaises(APIError):
                await service.describe(None, _request())
        self.assertEqual(client.calls, 2)

    async def test_concurrent_identical_jobs_are_coalesced(self) -> None:
        gate = asyncio.Event()
        client = FakeLLM([StreamChunk(delta_content="shared")], gate=gate)
        counters = Counters()
        service = _service(client, counters)
        with patch_prepare():
            tasks = [
                asyncio.ensure_future(service.describe(None, _request()))
                for _ in range(3)
            ]
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(client.calls, 1)
        self.assertTrue(all(result.text == "shared" for result in results))
        snapshot = counters.snapshot()
        self.assertEqual(snapshot.jobs, 1)
        self.assertEqual(snapshot.coalesced, 2)

    async def test_excess_waiters_fail_fast(self) -> None:
        gate = asyncio.Event()
        client = FakeLLM([StreamChunk(delta_content="shared")], gate=gate)
        service = _service(client, max_job_waiters=1, max_concurrent_jobs=1)
        with patch_prepare():
            leader = asyncio.ensure_future(service.describe(None, _request()))
            await asyncio.sleep(0)
            first = asyncio.ensure_future(service.describe(None, _request()))
            await asyncio.sleep(0)
            second = asyncio.ensure_future(service.describe(None, _request()))
            with self.assertRaises(TooManyWaitersError) as caught:
                await second
            self.assertIs(caught.exception, ErrTooManyWaiters)
            gate.set()
            self.assertEqual((await leader).text, "shared")
            self.assertEqual((await first).text, "shared")

    async def test_caller_cancellation_does_not_kill_the_shared_job(self) -> None:
        gate = asyncio.Event()
        client = FakeLLM([StreamChunk(delta_content="survives")], gate=gate)
        counters = Counters()
        service = _service(client, counters)
        with patch_prepare():
            token = with_cancel(None)
            leader = asyncio.ensure_future(service.describe(token, _request()))
            await asyncio.sleep(0)
            waiter = asyncio.ensure_future(service.describe(None, _request()))
            await asyncio.sleep(0)
            token.cancel("caller gave up")
            with self.assertRaises(BaseException):
                await leader
            gate.set()
            self.assertEqual((await waiter).text, "survives")
        # 两个调用者都先查了缓存（Go 同样在合并前记 miss），合并记在 coalesced 上。
        snapshot = counters.snapshot()
        self.assertEqual(snapshot.cache, {"miss": 2})
        self.assertEqual(snapshot.coalesced, 1)
        self.assertEqual(snapshot.jobs, 1)

    async def test_shared_timeout_bounds_the_job(self) -> None:
        gate = asyncio.Event()
        client = FakeLLM([StreamChunk(delta_content="late")], gate=gate)
        service = _service(client, shared_timeout=0.05)
        with patch_prepare():
            with self.assertRaises(BaseException) as caught:
                await service.describe(None, _request())
        self.assertTrue(is_cancellation(caught.exception))
        gate.set()

    async def test_queue_full_is_reported(self) -> None:
        gate = asyncio.Event()
        client = FakeLLM([StreamChunk(delta_content="busy")], gate=gate)
        service = _service(client, max_concurrent_jobs=1, max_job_queue=1)
        with patch_prepare():
            jobs = [
                asyncio.ensure_future(
                    service.describe(None, _request(f"media:{index:064x}"))
                )
                for index in range(3)
            ]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [item for item in results if isinstance(item, BaseException)]
        self.assertEqual(len(errors), 1)
        self.assertIn("vision: job queue", str(errors[0]))
