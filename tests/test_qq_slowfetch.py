"""引用 / 转发慢取（:mod:`atri_x.platform.qq_onebot.slowfetch`）与 ``fetch_message`` 的测试。

覆盖：分级 TTL 缓存、LRU 上限、单飞、预算内等待 / 超时延后、失败短缓存、
引用与转发的缓存复用、``fetch_message`` 工具与注册。
"""

from __future__ import annotations

import asyncio
import json
import unittest

from atri_x.errors import AtriXError, InvalidError
from atri_x.llm import ToolCallRequest
from atri_x.platform.platform import MessageContext, with_message_context
from atri_x.platform.qq_onebot import preprocess
from atri_x.platform.qq_onebot import slowfetch
from atri_x.platform.qq_onebot.adapter import Adapter
from atri_x.platform.qq_onebot.config import Config
from atri_x.platform.qq_onebot.transport import GetMessageData
from atri_x.platform.qq_onebot.types import NormalizedMessage, Sender
from atri_x.tool.builtin.fetch_message import (
    FETCH_MESSAGE_TOOL_NAME,
    FetchMessageTool,
    new_fetch_message_tool,
)

FORWARD_RAW = {
    "messages": [
        {
            "type": "node",
            "data": {
                "user_id": 7,
                "nickname": "小明",
                "content": [{"type": "text", "data": {"text": "转发内容"}}],
            },
        }
    ]
}


class _FakeTransport:
    """假传输层：记录调用、按脚本返回、支持失败与延迟。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.message_delay = 0.0
        self.forward_delay = 0.0
        self.fail_message = False
        self.fail_forward = False
        self.message_text = "被引用的原文"
        self.forward_raw = FORWARD_RAW

    async def get_message(self, ctx: object, message_id: str) -> GetMessageData:  # noqa: ARG002
        self.calls.append(("get_message", message_id))
        if self.message_delay > 0:
            await asyncio.sleep(self.message_delay)
        if self.fail_message:
            raise AtriXError("get_msg failed")
        return GetMessageData(
            user_id=7,
            message_id=int(message_id) if message_id.isdigit() else 1,
            message=[{"type": "text", "data": {"text": self.message_text}}],
            raw_message=self.message_text,
            sender=Sender(user_id=7, nickname="小明", card="小明"),
        )

    async def get_forward_msg(self, ctx: object, message_id: str) -> object:  # noqa: ARG002
        self.calls.append(("get_forward_msg", message_id))
        if self.forward_delay > 0:
            await asyncio.sleep(self.forward_delay)
        if self.fail_forward:
            raise AtriXError("get_forward_msg failed")
        return self.forward_raw


def _adapter(*, budget: float = 1.5, success_ttl: float = 300.0) -> Adapter:
    adapter = Adapter(Config(enabled=True), None, None, None)
    adapter.transport = _FakeTransport()
    adapter.fetch_cache = slowfetch.new_fetch_cache(
        budget=budget, success_ttl=success_ttl, failure_ttl=30.0, max_entries=16
    )
    return adapter


class CacheTests(unittest.TestCase):
    def test_success_and_failure_entries(self) -> None:
        cache = slowfetch.FetchCache(success_ttl=60.0, failure_ttl=60.0)
        cache.put("k1", True, "value")
        cache.put("k2", False, None)
        self.assertEqual(cache.get("k1"), (True, "value"))
        self.assertEqual(cache.get("k2"), (False, None))
        self.assertIsNone(cache.get("missing"))
        self.assertEqual(cache.stats["hits"], 2)
        self.assertEqual(cache.stats["misses"], 1)

    def test_ttl_expiry_and_lru_eviction(self) -> None:
        cache = slowfetch.FetchCache(success_ttl=0.01, failure_ttl=0.01, max_entries=2)
        cache.put("a", True, 1)
        cache.put("b", True, 2)
        cache.put("c", True, 3)  # 触发 LRU 淘汰 a
        self.assertEqual(len(cache._entries), 2)  # noqa: SLF001 - 断言内部容量
        self.assertEqual(cache.stats["evictions"], 1)
        self.assertIsNone(cache.get("a"))
        cache.clear()
        self.assertEqual(cache.get("b"), None)

    def test_zero_ttl_does_not_cache(self) -> None:
        cache = slowfetch.FetchCache(success_ttl=0.0, failure_ttl=0.0)
        cache.put("k", True, 1)
        self.assertIsNone(cache.get("k"))


class FetchWithBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_skips_loader(self) -> None:
        cache = slowfetch.FetchCache()
        calls: list[int] = []

        async def loader() -> str:  # noqa: ANN202
            calls.append(1)
            return "fresh"

        cache.put("k", True, "cached")
        outcome = await slowfetch.fetch_with_budget(cache, "k", loader)
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.cached)
        self.assertEqual(outcome.value, "cached")
        self.assertEqual(calls, [])

    async def test_timeout_defers_and_background_fills_cache(self) -> None:
        cache = slowfetch.FetchCache(budget=0.01)

        async def loader() -> str:  # noqa: ANN202
            await asyncio.sleep(0.05)
            return "slow"

        outcome = await slowfetch.fetch_with_budget(cache, "k", loader)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.deferred)
        self.assertEqual(cache.stats["deferred"], 1)
        await asyncio.sleep(0.08)
        self.assertEqual(cache.get("k"), (True, "slow"))
        self.assertEqual(cache.inflight(), 0)
        again = await slowfetch.fetch_with_budget(cache, "k", loader)
        self.assertTrue(again.ok)
        self.assertEqual(again.value, "slow")

    async def test_single_flight_coalesces(self) -> None:
        cache = slowfetch.FetchCache(budget=1.0)
        calls: list[int] = []

        async def loader() -> str:  # noqa: ANN202
            calls.append(1)
            await asyncio.sleep(0.02)
            return "shared"

        first, second = await asyncio.gather(
            slowfetch.fetch_with_budget(cache, "k", loader),
            slowfetch.fetch_with_budget(cache, "k", loader),
        )
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(first.value, "shared")
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.stats["coalesced"], 1)

    async def test_loader_failure_is_cached_with_short_ttl(self) -> None:
        cache = slowfetch.FetchCache(budget=1.0, failure_ttl=60.0)
        calls: list[int] = []

        async def loader() -> str:  # noqa: ANN202
            calls.append(1)
            raise AtriXError("boom")

        outcome = await slowfetch.fetch_with_budget(cache, "k", loader)
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.deferred)
        self.assertEqual(cache.get("k"), (False, None))
        outcome = await slowfetch.fetch_with_budget(cache, "k", loader)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.cached)
        self.assertEqual(len(calls), 1)

    async def test_zero_budget_waits(self) -> None:
        cache = slowfetch.FetchCache(budget=0.0)

        async def loader() -> str:  # noqa: ANN202
            await asyncio.sleep(0.01)
            return "done"

        outcome = await slowfetch.fetch_with_budget(cache, "k", loader)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, "done")

    async def test_fetch_now_returns_cached_and_missing(self) -> None:
        cache = slowfetch.FetchCache()

        async def loader() -> str:  # noqa: ANN202
            return "now"

        outcome = await slowfetch.fetch_now(cache, "k", loader)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, "now")
        cached = await slowfetch.fetch_now(cache, "k", loader)
        self.assertTrue(cached.cached)


class ReferenceFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_is_cached(self) -> None:
        adapter = _adapter()
        first = await preprocess.fetch_referenced(adapter, None, None, "101")
        self.assertTrue(first[1])
        self.assertIn("被引用的原文", first[0].text)
        second = await preprocess.fetch_referenced(adapter, None, None, "101")
        self.assertIn("被引用的原文", second[0].text)
        self.assertEqual(len(adapter.transport.calls), 1)

    async def test_reference_timeout_returns_empty_then_cache_hit(self) -> None:
        adapter = _adapter(budget=0.001)
        adapter.transport.message_delay = 0.05
        first = await preprocess.fetch_referenced(adapter, None, None, "102")
        self.assertFalse(first[1])
        await asyncio.sleep(0.08)
        second = await preprocess.fetch_referenced(adapter, None, None, "102")
        self.assertTrue(second[1])
        self.assertEqual(len(adapter.transport.calls), 1)

    async def test_reference_failure_is_cached(self) -> None:
        adapter = _adapter()
        adapter.transport.fail_message = True
        self.assertFalse((await preprocess.fetch_referenced(adapter, None, None, "103"))[1])
        self.assertFalse((await preprocess.fetch_referenced(adapter, None, None, "103"))[1])
        self.assertEqual(len(adapter.transport.calls), 1)

    async def test_empty_reply_id(self) -> None:
        adapter = _adapter()
        self.assertFalse((await preprocess.fetch_referenced(adapter, None, None, " "))[1])
        self.assertEqual(adapter.transport.calls, [])

    async def test_missing_transport(self) -> None:
        adapter = _adapter()
        adapter.transport = None
        self.assertFalse((await preprocess.fetch_referenced(adapter, None, None, "1"))[1])


class ForwardFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_is_cached(self) -> None:
        adapter = _adapter()
        msg = NormalizedMessage(text="看这个", forward_ids=["f1"])
        first = await preprocess.resolve_forward_segments(adapter, None, None, msg)
        self.assertIn("转发内容", first.text)
        second = await preprocess.resolve_forward_segments(
            adapter, None, None, NormalizedMessage(text="看这个", forward_ids=["f1"])
        )
        self.assertIn("转发内容", second.text)
        self.assertEqual(
            [call for call in adapter.transport.calls if call[0] == "get_forward_msg"],
            [("get_forward_msg", "f1")],
        )

    async def test_forward_timeout_marks_text(self) -> None:
        adapter = _adapter(budget=0.001)
        adapter.transport.forward_delay = 0.05
        msg = await preprocess.resolve_forward_segments(
            adapter, None, None, NormalizedMessage(text="看这个", forward_ids=["f2"])
        )
        self.assertIn("拉取超时", msg.text)

    async def test_forward_failure_marks_text(self) -> None:
        adapter = _adapter()
        adapter.transport.fail_forward = True
        msg = await preprocess.resolve_forward_segments(
            adapter, None, None, NormalizedMessage(text="看这个", forward_ids=["f3"])
        )
        self.assertIn("拉取失败", msg.text)


class FetchMessageTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_message(self) -> None:
        adapter = _adapter()
        text = await preprocess.fetch_message_text(adapter, None, "201")
        self.assertIn("被引用的原文", text)

    async def test_forward_kind(self) -> None:
        adapter = _adapter()
        text = await preprocess.fetch_message_text(adapter, None, "202", "forward")
        self.assertIn("转发内容", text)

    async def test_auto_falls_back_to_forward(self) -> None:
        adapter = _adapter()
        adapter.transport.fail_message = True
        text = await preprocess.fetch_message_text(adapter, None, "203", "auto")
        self.assertIn("转发内容", text)

    async def test_empty_id_and_bad_kind(self) -> None:
        adapter = _adapter()
        self.assertIn("message_id 不能为空", await preprocess.fetch_message_text(adapter, None, " "))
        self.assertIn(
            "kind 只能是", await preprocess.fetch_message_text(adapter, None, "1", "nope")
        )

    async def test_missing_transport(self) -> None:
        adapter = _adapter()
        adapter.transport = None
        self.assertIn("不支持", await preprocess.fetch_message_text(adapter, None, "1"))

    async def test_not_found(self) -> None:
        adapter = _adapter()
        adapter.transport.fail_message = True
        adapter.transport.fail_forward = True
        self.assertIn("没有取到", await preprocess.fetch_message_text(adapter, None, "204", "auto"))
        self.assertIn(
            "没有取到该消息", await preprocess.fetch_message_text(adapter, None, "205", "message")
        )


class _FakeAdapter:
    """只实现 ``fetch_message_text`` 的适配器替身。"""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def fetch_message_text(self, ctx: object, message_id: str, kind: str) -> str:  # noqa: ARG002
        self.asked.append((message_id, kind))
        return f"{message_id}:{kind} 的正文"


def _ctx_with_adapter(adapter: object) -> object:
    return with_message_context(None, MessageContext(platform="qqonebot", media_resolver=adapter))


class FetchMessageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata(self) -> None:
        tool = new_fetch_message_tool()
        self.assertEqual(tool.name(), FETCH_MESSAGE_TOOL_NAME)
        self.assertEqual(tool.info().risk, "low")
        self.assertIn("message_id", tool.schema().function.parameters["required"])

    async def test_unsupported_platform(self) -> None:
        tool = FetchMessageTool()
        result = await tool.call(
            None, ToolCallRequest(name="fetch_message", arguments='{"message_id":"1"}')
        )
        self.assertIn("当前平台不支持", result.content)

    async def test_calls_adapter(self) -> None:
        adapter = _FakeAdapter()
        tool = FetchMessageTool()
        result = await tool.call(
            _ctx_with_adapter(adapter),
            ToolCallRequest(name="fetch_message", arguments='{"message_id":"9","kind":"forward"}'),
        )
        self.assertEqual(result.content, "9:forward 的正文")
        self.assertEqual(adapter.asked, [("9", "forward")])

    async def test_default_kind_auto(self) -> None:
        adapter = _FakeAdapter()
        tool = FetchMessageTool()
        await tool.call(
            _ctx_with_adapter(adapter),
            ToolCallRequest(name="fetch_message", arguments='{"message_id":"9"}'),
        )
        self.assertEqual(adapter.asked, [("9", "auto")])

    async def test_requires_message_id(self) -> None:
        tool = FetchMessageTool()
        with self.assertRaises(InvalidError):
            await tool.call(_ctx_with_adapter(_FakeAdapter()), ToolCallRequest(name="fetch_message", arguments="{}"))

    async def test_bad_arguments(self) -> None:
        tool = FetchMessageTool()
        for raw in ("{bad", "[1]"):
            with self.assertRaises(InvalidError):
                await tool.call(
                    _ctx_with_adapter(_FakeAdapter()),
                    ToolCallRequest(name="fetch_message", arguments=raw),
                )

    async def test_non_string_fields_ignored(self) -> None:
        adapter = _FakeAdapter()
        tool = FetchMessageTool()
        result = await tool.call(
            _ctx_with_adapter(adapter),
            ToolCallRequest(
                name="fetch_message",
                arguments=json.dumps({"message_id": "7", "kind": 1}),
            ),
        )
        self.assertEqual(result.content, "7:auto 的正文")


class RegistrationTests(unittest.TestCase):
    def test_registered_in_builtin_registry(self) -> None:
        from atri_x.tool.builtin.register import RegisterOptions, register_all
        from atri_x.tool.tool import Registry

        registry = Registry()
        register_all(registry, RegisterOptions())
        tool, ok = registry.get(FETCH_MESSAGE_TOOL_NAME)
        self.assertTrue(ok)
        self.assertIsNotNone(tool)


if __name__ == "__main__":
    unittest.main()
