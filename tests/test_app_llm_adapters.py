"""LLM 适配器包装测试。

覆盖：``atri_x/app/health_llm.py``（成功/失败计数、可选能力降级）、
``atri_x/app/breaker_llm.py``（熔断拒绝、on-error 兜底、总超时、透明短路）、
``atri_x/app/models.py``（协议路由与请求选项映射）。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from atri_x.app.breaker_llm import (
    BreakerLLM,
    with_fallback_on_error,
    with_fallback_total_timeout,
    wrap_breaker_llm,
)
from atri_x.app.health_llm import HealthLLM, wrap_health_llm
from atri_x.app.models import (
    ProtocolRouter,
    app_llm_request_options,
    model_extra_payloads,
    set_client_logger,
)
from atri_x.config import APIProtocol
from atri_x.errors import ErrFallbackTotalTimeout, InvalidError, OpenError
from atri_x.llm import ChatRequest, StreamChunk
from atri_x.llm.breaker import Breaker, Config as BreakerConfig


class FakeClient:
    """最小 LLM 替身：``chat_stream`` 同步返回异步迭代器。"""

    def __init__(self, chunks=None, error=None, models=None, metadata=None, models_error=None):
        self.chunks = list(chunks or [])
        self.error = error
        self.models = list(models or [])
        self.models_error = models_error
        self.metadata = metadata
        self.requests: list[Any] = []
        self.logger = None
        self.retry_notifier = None
        self.closed = 0

    def chat_stream(self, ctx, req):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return self._gen()

    async def _gen(self):
        for chunk in self.chunks:
            yield chunk

    async def list_models(self, ctx):
        if self.models_error is not None:
            raise self.models_error
        return list(self.models)

    async def list_model_metadata(self, ctx):
        if self.metadata is None:
            raise InvalidError("no metadata")
        return list(self.metadata)

    def set_logger(self, logger):
        self.logger = logger

    def set_retry_notifier(self, fn):
        self.retry_notifier = fn


class FakeState:
    def __init__(self):
        self.success: list[str] = []
        self.errors: list[tuple[str, Any]] = []
        self.circuits: list[tuple[str, Any]] = []

    def record_model_success(self, provider):
        self.success.append(provider)

    def record_model_error(self, provider, err):
        self.errors.append((provider, err))

    def set_model_circuit(self, provider, state):
        self.circuits.append((provider, state))


def drain(stream):
    async def run():
        out = []
        async for chunk in stream:
            out.append(chunk)
        return out

    return asyncio.run(run())


def call(obj, ctx, req):
    """调用同步的 ``chat_stream`` 并跑完迭代器。"""
    return drain(obj.chat_stream(ctx, req))


class TestHealthLLM(unittest.TestCase):
    def test_wrap_is_transparent_without_state(self):
        client = FakeClient()
        self.assertIs(wrap_health_llm("p", client, None), client)
        self.assertIsNone(wrap_health_llm("p", None, FakeState()))

    def test_success_recorded_once(self):
        state = FakeState()
        client = FakeClient(chunks=[StreamChunk(delta_content="a"), StreamChunk(delta_content="b")])
        wrapper = HealthLLM("p", client, state)
        chunks = call(wrapper, None, ChatRequest())
        self.assertEqual(len(chunks), 2)
        self.assertEqual(state.success, ["p"])
        self.assertEqual(state.errors, [])

    def test_chunk_error_recorded_and_resets_success(self):
        state = FakeState()
        boom = InvalidError("stream failed")
        client = FakeClient(
            chunks=[StreamChunk(delta_content="a"), StreamChunk(error=boom), StreamChunk(delta_content="c")]
        )
        wrapper = HealthLLM("p", client, state)
        call(wrapper, None, ChatRequest())
        self.assertEqual(state.success, ["p", "p"])
        self.assertEqual(state.errors, [("p", boom)])

    def test_sync_raise_counted_and_reraised(self):
        state = FakeState()
        wrapper = HealthLLM("p", FakeClient(error=InvalidError("connect")), state)
        with self.assertRaises(InvalidError):
            wrapper.chat_stream(None, ChatRequest())
        self.assertEqual(len(state.errors), 1)

    def test_cancellation_not_counted(self):
        state = FakeState()
        wrapper = HealthLLM("p", FakeClient(), state)
        wrapper.record_error(asyncio.CancelledError())
        self.assertEqual(state.errors, [])

    def test_list_models_records_success(self):
        state = FakeState()
        wrapper = HealthLLM("p", FakeClient(models=["m1"]), state)
        self.assertEqual(asyncio.run(wrapper.list_models(None)), ["m1"])
        self.assertEqual(state.success, ["p"])

    def test_list_model_metadata_unsupported(self):
        wrapper = HealthLLM("p", FakeClient(metadata=None), FakeState())
        with self.assertRaises(InvalidError):
            asyncio.run(wrapper.list_model_metadata(None))

    def test_list_model_metadata_forwarded(self):
        wrapper = HealthLLM("p", FakeClient(metadata=["m"]), FakeState())
        self.assertEqual(asyncio.run(wrapper.list_model_metadata(None)), ["m"])

    def test_retry_notifier_forwarded_only_if_supported(self):
        client = FakeClient()
        HealthLLM("p", client, FakeState()).set_retry_notifier("fn")
        self.assertEqual(client.retry_notifier, "fn")
        HealthLLM("p", object(), FakeState()).set_retry_notifier("fn")


class TestBreakerLLM(unittest.TestCase):
    def test_transparent_when_disabled(self):
        client = FakeClient()
        result = wrap_breaker_llm(
            "p", client, None, "", "", Breaker(BreakerConfig(failure_threshold=0))
        )
        self.assertIs(result, client)

    def test_wrapped_when_threshold_configured(self):
        client = FakeClient()
        result = wrap_breaker_llm(
            "p", client, None, "", "", Breaker(BreakerConfig(failure_threshold=2))
        )
        self.assertIsInstance(result, BreakerLLM)

    def test_options_force_wrapping(self):
        client = FakeClient()
        result = wrap_breaker_llm(
            "p",
            client,
            None,
            "",
            "",
            Breaker(BreakerConfig(failure_threshold=0)),
            None,
            with_fallback_on_error(True),
        )
        self.assertIsInstance(result, BreakerLLM)

    def test_open_breaker_rejects_without_fallback(self):
        breaker = Breaker(BreakerConfig(failure_threshold=1, open_cooldown=60))
        breaker.failure()
        wrapper = BreakerLLM("p", FakeClient(), None, "", "", breaker, None)
        with self.assertRaises(OpenError):
            wrapper.chat_stream(None, ChatRequest())

    def test_open_breaker_routes_to_fallback(self):
        breaker = Breaker(BreakerConfig(failure_threshold=1, open_cooldown=60))
        breaker.failure()
        fallback = FakeClient(chunks=[StreamChunk(delta_content="fb")])
        wrapper = BreakerLLM("p", FakeClient(), fallback, "alt", "alt-model", breaker, None)
        chunks = call(wrapper, None, ChatRequest(model="primary"))
        self.assertEqual(chunks[0].delta_content, "fb")
        self.assertEqual(fallback.requests[0].model, "alt-model")

    def test_on_error_fallback_switches_stream(self):
        fallback = FakeClient(chunks=[StreamChunk(delta_content="fb")])
        primary = FakeClient(error=InvalidError("connection refused"))
        wrapper = BreakerLLM(
            "p",
            primary,
            fallback,
            "alt",
            "",
            Breaker(BreakerConfig(failure_threshold=5)),
            None,
            fallback_on_error=True,
        )
        chunks = call(wrapper, None, ChatRequest())
        self.assertEqual([c.delta_content for c in chunks], ["fb"])

    def test_on_error_fallback_failure_reports_both(self):
        primary = FakeClient(error=InvalidError("primary down"))
        fallback = FakeClient(error=InvalidError("fallback down"))
        wrapper = BreakerLLM(
            "p", primary, fallback, "alt", "", Breaker(BreakerConfig(failure_threshold=5)), None,
            fallback_on_error=True,
        )
        with self.assertRaises(InvalidError) as ctx:
            wrapper.chat_stream(None, ChatRequest())
        self.assertIn("primary down", str(ctx.exception))
        self.assertIn("fallback down", str(ctx.exception))

    def test_chunk_error_triggers_fallback_before_content(self):
        boom = InvalidError("mid-stream")
        primary = FakeClient(chunks=[StreamChunk(error=boom), StreamChunk(delta_content="late")])
        fallback = FakeClient(chunks=[StreamChunk(delta_content="fb")])
        wrapper = BreakerLLM(
            "p", primary, fallback, "alt", "", Breaker(BreakerConfig(failure_threshold=5)), None,
            fallback_on_error=True,
        )
        chunks = call(wrapper, None, ChatRequest())
        self.assertEqual([c.delta_content for c in chunks], ["fb"])

    def test_chunk_error_after_content_is_forwarded_not_retried(self):
        """内容已交付后的错误按 Go 语义**随 chunk 转发**，不再切兜底。"""
        boom = InvalidError("mid-stream")
        primary = FakeClient(chunks=[StreamChunk(delta_content="partial"), StreamChunk(error=boom)])
        fallback = FakeClient(chunks=[StreamChunk(delta_content="fb")])
        wrapper = BreakerLLM(
            "p", primary, fallback, "alt", "", Breaker(BreakerConfig(failure_threshold=5)), None,
            fallback_on_error=True,
        )
        chunks = call(wrapper, None, ChatRequest())
        self.assertEqual(chunks[0].delta_content, "partial")
        self.assertIs(chunks[1].error, boom)
        self.assertEqual(fallback.requests, [])

    def test_total_timeout_raises_sentinel(self):
        class SlowClient(FakeClient):
            async def _gen(self):
                await asyncio.sleep(1)
                yield StreamChunk(delta_content="late")

        wrapper = BreakerLLM(
            "p", SlowClient(), None, "", "", Breaker(BreakerConfig(failure_threshold=5)), None,
            total_timeout=0.01,
        )
        with self.assertRaises(Exception) as ctx:
            call(wrapper, None, ChatRequest())
        self.assertIn(str(ErrFallbackTotalTimeout), str(ctx.exception))

    def test_list_models_falls_back(self):
        breaker = Breaker(BreakerConfig(failure_threshold=1, open_cooldown=60))
        breaker.failure()
        wrapper = BreakerLLM("p", FakeClient(models=["a"]), FakeClient(models=["b"]), "alt", "", breaker, None)
        self.assertEqual(asyncio.run(wrapper.list_models(None)), ["b"])

    def test_list_models_records_success(self):
        state = FakeState()
        breaker = Breaker(BreakerConfig(failure_threshold=2))
        wrapper = BreakerLLM("p", FakeClient(models=["a"]), None, "", "", breaker, state)
        asyncio.run(wrapper.list_models(None))
        self.assertEqual(state.circuits[-1][0], "p")

    def test_record_failure_ignores_cancellation(self):
        breaker = Breaker(BreakerConfig(failure_threshold=2))
        wrapper = BreakerLLM("p", FakeClient(), None, "", "", breaker, None)
        wrapper.record_failure(asyncio.CancelledError())
        self.assertEqual(breaker.state(), "closed")

    def test_list_model_metadata_supported(self):
        wrapper = BreakerLLM("p", FakeClient(metadata=["m"]), None, "", "", Breaker(BreakerConfig()), None)
        self.assertEqual(asyncio.run(wrapper.list_model_metadata(None)), ["m"])

    def test_list_model_metadata_unsupported(self):
        wrapper = BreakerLLM("p", object(), None, "", "", Breaker(BreakerConfig()), None)
        with self.assertRaises(InvalidError):
            asyncio.run(wrapper.list_model_metadata(None))

    def test_options_helpers_shape(self):
        self.assertEqual(with_fallback_on_error(True), ("fallback_on_error", True))
        self.assertEqual(with_fallback_total_timeout(2.5), ("total_timeout", 2.5))


class TestProtocolRouter(unittest.TestCase):
    def test_pick_defaults_to_chat(self):
        router = ProtocolRouter("p", "chat", "responses", lambda model: APIProtocol.CHAT)
        self.assertEqual(router.pick("m"), "chat")

    def test_pick_responses(self):
        router = ProtocolRouter("p", "chat", "responses", lambda model: APIProtocol.RESPONSES)
        self.assertEqual(router.pick("m"), "responses")

    def test_pick_without_responses(self):
        router = ProtocolRouter("p", "chat")
        self.assertEqual(router.pick("m"), "chat")

    def test_pick_tolerates_protocol_error(self):
        def boom(model):
            raise RuntimeError("bad config")

        router = ProtocolRouter("p", "chat", "responses", boom)
        self.assertEqual(router.pick("m"), "chat")

    def test_chat_stream_dispatches_by_model(self):
        chat = FakeClient(chunks=[StreamChunk(delta_content="c")])
        responses = FakeClient(chunks=[StreamChunk(delta_content="r")])
        router = ProtocolRouter(
            "p", chat, responses, lambda model: APIProtocol.RESPONSES if model == "r" else APIProtocol.CHAT
        )
        self.assertEqual([c.delta_content for c in call(router, None, ChatRequest(model="r"))], ["r"])
        self.assertEqual([c.delta_content for c in call(router, None, ChatRequest(model="c"))], ["c"])

    def test_list_models_falls_back_to_responses(self):
        chat = FakeClient(models_error=InvalidError("no models"))
        responses = FakeClient(models=["r"])
        router = ProtocolRouter("p", chat, responses)
        self.assertEqual(asyncio.run(router.list_models(None)), ["r"])

    def test_list_model_metadata_error_when_both_fail(self):
        router = ProtocolRouter("p", object(), object())
        with self.assertRaises(InvalidError):
            asyncio.run(router.list_model_metadata(None))

    def test_logger_and_retry_forwarded_to_both(self):
        chat = FakeClient()
        responses = FakeClient()
        router = ProtocolRouter("p", chat, responses)
        router.set_logger("logger")
        router.set_retry_notifier("fn")
        self.assertEqual((chat.logger, responses.logger), ("logger", "logger"))
        self.assertEqual((chat.retry_notifier, responses.retry_notifier), ("fn", "fn"))


class TestModelHelpers(unittest.TestCase):
    def test_app_llm_request_options_from_config(self):
        cfg = type(
            "Cfg",
            (),
            {
                "first_chunk_timeout_seconds": 5,
                "stream_idle_timeout_seconds": 6,
                "max_retries": 2,
                "retry_initial_delay_seconds": 1,
            },
        )()
        options = app_llm_request_options(cfg, "http://proxy:8080")
        self.assertEqual(options.first_chunk_timeout, 5.0)
        self.assertEqual(options.stream_idle_timeout, 6.0)
        self.assertEqual(options.max_retries, 2)
        self.assertEqual(options.proxy, "http://proxy:8080")

    def test_app_llm_request_options_without_config(self):
        options = app_llm_request_options(None, "")
        self.assertEqual(options.max_retries, 0)

    def test_model_extra_payloads_only_keeps_configured(self):
        configs = {
            "a": type("M", (), {"extra_payload": {"x": 1}})(),
            "b": type("M", (), {"extra_payload": {}})(),
        }
        self.assertEqual(model_extra_payloads(configs), {"a": {"x": 1}})

    def test_set_client_logger_ignores_non_supporting_client(self):
        set_client_logger(object(), "logger")
        client = FakeClient()
        set_client_logger(client, "logger")
        self.assertEqual(client.logger, "logger")


if __name__ == "__main__":
    unittest.main()
