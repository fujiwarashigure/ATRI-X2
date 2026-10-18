"""内置工具：web_search / web_extract / chat_history / resident_memory /
prompt_library / register 的单元测试。

对应 Go 测试：``web_tools_test.go``、``chat_history_dependencies_test.go``、
``resident_memory_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone

from atri_x.errors import InvalidError
from atri_x.ratelimit import new as new_rate_limit
from atri_x.tool import CallRequest, Registry
from atri_x.tool.builtin.angel_memory import (
    ANGEL_FORGET_TOOL_NAME,
    AngelForgetTool,
    AngelMemoryToolOptions,
    AngelRecallTool,
    AngelRememberTool,
    angel_forget_inline,
    angel_forget_rate_key,
    angel_forget_source_text,
    memory_trace_suffix,
    new_angel_memory_tools,
    short_angel_memory_id,
)
from atri_x.tool.builtin.chat_history import (
    ChatHistoryContext,
    SearchChatHistoryTool,
    chat_history_result_lines,
    chat_history_time_range_at,
    chat_history_user_filter,
    clamp_chat_history_limit,
    clamp_chat_history_window,
    format_chat_history_line,
    history_media_label,
    parse_chat_history_message_id,
    parse_chat_history_time,
    split_chat_history_terms,
    truncate_chat_history_message,
)
from atri_x.tool.builtin.prompt_library import (
    PromptLibrarySearchTool,
    truncate_prompt_lib_line,
)
from atri_x.tool.builtin.register import (
    ALWAYS_REGISTERED_TOOLS,
    CONDITIONAL_TOOLS,
    REGISTERED_TOOLS,
    UNPORTED_TOOLS,
    RegisterOptions,
    register_all,
)
from atri_x.tool.builtin.resident_memory import (
    RESIDENT_MEMORY_CORE_TOOL_NAME,
    RESIDENT_MEMORY_NORMAL_TOOL_NAME,
    RESIDENT_MEMORY_READ_TOOL_NAME,
    RESIDENT_MEMORY_TOOL_NAME,
    ResidentMemoryCoreTool,
    ResidentMemoryNormalTool,
    ResidentMemoryReadTool,
    ResidentMemoryTool,
    core_description,
    memory_json_result,
    normal_description,
    new_resident_memory_tools,
)
from atri_x.tool.builtin.web_extract import (
    DEFAULT_REMOVE_SELECTOR,
    MAX_EXTRACT_CHARS,
    CachedExtract,
    ExtractProxyConfig,
    WebExtractTool,
    extract_cache_key,
    extract_simple_html_text,
    extract_tag_content,
    format_extract_content,
    jina_reader_url,
    parse_web_extract_args,
    remove_html_comments,
    remove_tag_blocks,
    resolve_extract_proxy,
    slice_extract,
    strip_tags,
)
from atri_x.tool.builtin.web_search import (
    WebSearchTool,
    format_tavily_content,
    new_web_search_tool,
    normalize_tavily_request,
    parse_tavily_response,
    parse_web_search_args,
    trim_string_list,
    truncate_http_body,
)
from atri_x.tool.builtin.workspace import WorkspaceTool

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_tools2_test")


def _call(name: str, arguments: dict) -> CallRequest:
    return CallRequest(id="1", name=name, arguments=json.dumps(arguments))


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status = status
        self.body = text.encode("utf-8")
        self._payload = payload

    @property
    def ok(self) -> bool:
        """与 :class:`atri_x.net.httpclient.Response` 一致：这是属性，不是方法。"""

        return 200 <= self.status < 300

    def text(self, encoding: str | None = None) -> str:
        return self.body.decode(encoding or "utf-8", "replace")

    def json(self):
        return self._payload


class _FakeClient:
    """替换 HTTPClient 的假客户端。"""

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple] = []

    async def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class TestWebSearch(unittest.TestCase):
    def test_builder_metadata(self) -> None:
        tool = new_web_search_tool()
        self.assertEqual(tool.name(), "web_search")
        info = tool.info()
        self.assertEqual(info.risk, "low")
        self.assertEqual(info.tags, ["web"])
        self.assertEqual(info.depends_on, ["web_extract"])
        schema = tool.schema()
        self.assertIn("query", schema.function.parameters["required"])

    def test_normalize_request(self) -> None:
        args = parse_web_search_args(
            json.dumps(
                {
                    "query": "  hi ",
                    "max_results": 99,
                    "topic": " news ",
                    "include_domains": [" a.com ", ""],
                    "exact_match": True,
                }
            )
        )
        payload = normalize_tavily_request(args, "hi")
        self.assertEqual(payload.max_results, 20)
        self.assertEqual(payload.search_depth, "basic")
        self.assertEqual(payload.include_domains, ["a.com"])
        body = payload.to_payload()
        self.assertEqual(body["query"], "hi")
        self.assertEqual(body["topic"], "news")
        self.assertTrue(body["exact_match"])
        self.assertNotIn("time_range", body)
        self.assertNotIn("country", body)
        self.assertFalse(body["include_images"])

    def test_max_results_default(self) -> None:
        args = parse_web_search_args(json.dumps({"query": "x"}))
        self.assertEqual(normalize_tavily_request(args, "x").max_results, 5)

    def test_format_content(self) -> None:
        data = parse_tavily_response(
            {
                "answer": "42",
                "results": [
                    {"title": "T", "url": "https://a", "content": "C", "score": 0.5}
                ],
            }
        )
        text = format_tavily_content(data)
        self.assertIn("Answer: 42", text)
        self.assertIn("1. T", text)
        self.assertIn("https://a", text)

    def test_call_requires_query(self) -> None:
        tool = new_web_search_tool()
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _call("web_search", {"query": " "})))

    def test_call_without_tavily_key_uses_keyless_engine(self) -> None:
        """没有 TAVILY_API_KEY 时不再直接报错，而是走免注册通道（见 web_search_engines）。"""

        client = _FakeClient(
            _FakeResponse(
                200,
                {"data": {"web": [{"title": "T", "url": "u", "description": "d"}]}},
            )
        )
        tool = WebSearchTool(client=client, engines="firecrawl")
        os.environ.pop("TAVILY_API_KEY", None)
        os.environ.pop("FIRECRAWL_API_KEY", None)
        result = _run(tool.call(None, _call("web_search", {"query": "hi"})))
        self.assertIn("1. T", result.content)
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", "https://api.firecrawl.dev/v2/search"))
        self.assertNotIn("Authorization", kw["headers"])

    def test_call_success(self) -> None:
        client = _FakeClient(
            _FakeResponse(
                200,
                {
                    "answer": "A",
                    "results": [{"title": "T", "url": "u", "content": "c"}],
                },
            )
        )
        tool = WebSearchTool(client=client, endpoint="https://api.test/search")
        os.environ["TAVILY_API_KEY"] = "k"
        try:
            result = _run(tool.call(None, _call("web_search", {"query": "hi"})))
        finally:
            os.environ.pop("TAVILY_API_KEY", None)
        self.assertIn("Answer: A", result.content)
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", "https://api.test/search"))
        self.assertEqual(kw["headers"]["Authorization"], "Bearer k")
        self.assertEqual(kw["json"]["query"], "hi")

    def test_call_http_error(self) -> None:
        """Tavily 失败后继续降级；所有通道都失败才把各自的原因汇总成 InvalidError。"""

        client = _FakeClient(_FakeResponse(500, {}, "boom"))
        tool = WebSearchTool(client=client, engines="tavily,firecrawl,keenable")
        os.environ["TAVILY_API_KEY"] = "k"
        try:
            with self.assertRaises(InvalidError) as ctx:
                _run(tool.call(None, _call("web_search", {"query": "hi"})))
        finally:
            os.environ.pop("TAVILY_API_KEY", None)
        message = str(ctx.exception)
        self.assertIn("Tavily HTTP 500", message)
        self.assertIn("Firecrawl HTTP 500", message)
        self.assertIn("Keenable HTTP 500", message)
        self.assertEqual(len(client.calls), 3)

    def test_helpers(self) -> None:
        self.assertEqual(trim_string_list([" a ", "", "b"]), ["a", "b"])
        self.assertEqual(truncate_http_body(b"short"), "short")
        self.assertTrue(truncate_http_body(b"x" * 3000).endswith("..."))


class TestWebExtract(unittest.TestCase):
    def test_builder_metadata(self) -> None:
        tool = WebExtractTool()
        self.assertEqual(tool.name(), "web_extract")
        self.assertEqual(tool.info().depends_on, ["web_search"])
        self.assertIn("url", tool.schema().function.parameters["required"])

    def test_html_helpers(self) -> None:
        raw = (
            "<html><head><title>T &amp; T</title></head><body>"
            "<script>var x=1;</script><style>p{}</style>"
            "<!-- note --><p>Hello   world</p></body></html>"
        )
        title, text = extract_simple_html_text(raw)
        self.assertEqual(title, "T & T")
        # Go 的 extractSimpleHTMLText 不会剥掉 title 块，只剥 script/style/noscript。
        self.assertEqual(text, "T & T Hello world")
        self.assertEqual(extract_tag_content("<title>ab</title>", "title"), "ab")
        self.assertEqual(extract_tag_content("no tag", "title"), "")
        self.assertEqual(remove_tag_blocks("<a><b>x</b></a>", "b"), "<a> </a>")
        self.assertEqual(remove_html_comments("a<!-- c -->b"), "a b")
        # Go 的 stripTags 对 '<' 与 '>' 各写一个空格，故两侧都有两个空格。
        self.assertEqual(strip_tags("<p>x</p>"), "  x  ")

    def test_jina_reader_url(self) -> None:
        url = jina_reader_url("https://r.jina.ai/", "https://a.com/p?q=1")
        self.assertEqual(url, "https://r.jina.ai/http://a.com/p?q=1")
        with self.assertRaises(InvalidError):
            jina_reader_url("https://r.jina.ai/", "not-a-url")

    def test_slice_extract(self) -> None:
        entry = CachedExtract(url="u", title="t", source="direct", content="0123456789")
        data = slice_extract(entry, 0, 4, False)
        self.assertEqual(data.content, "0123")
        self.assertEqual(data.next_offset, 4)
        self.assertTrue(data.truncated)
        self.assertEqual(data.total_chars, 10)
        tail = slice_extract(entry, 8, 100, True)
        self.assertEqual(tail.content, "89")
        self.assertIsNone(tail.next_offset)
        self.assertFalse(tail.truncated)
        capped = slice_extract(entry, 0, 999999, False)
        self.assertEqual(capped.returned_chars, 10)
        self.assertLessEqual(MAX_EXTRACT_CHARS, 16000)
        self.assertEqual(slice_extract(entry, -5, 2, False).offset, 0)
        self.assertEqual(slice_extract(entry, 99, 2, False).offset, 10)

    def test_format_content(self) -> None:
        entry = CachedExtract(url="u", title="t", source="jina", content="abcdef")
        text = format_extract_content(slice_extract(entry, 1, 2, True))
        self.assertIn("URL: u", text)
        self.assertIn("Source: jina", text)
        self.assertIn("Title: t", text)
        self.assertIn("Range: 1-3 / 6", text)
        self.assertIn("Next offset: 3", text)
        self.assertIn("Cached: true", text)

    def test_cache_key_and_proxy(self) -> None:
        key = extract_cache_key("direct", "u", DEFAULT_REMOVE_SELECTOR, ExtractProxyConfig(cache_key="env"))
        self.assertEqual(key.count("\x00"), 3)
        self.assertTrue(key.startswith("direct\x00u\x00"))
        self.assertTrue(resolve_extract_proxy(None, "disabled").disabled)
        self.assertEqual(resolve_extract_proxy(None, "http://p:1").cache_key, "http://p:1")
        with self.assertRaises(InvalidError):
            resolve_extract_proxy(None, "not a url")

    def test_parse_args(self) -> None:
        args = parse_web_extract_args(
            json.dumps({"url": "u", "jina": False, "force_refresh": True, "offset": 3})
        )
        self.assertFalse(args.jina)
        self.assertTrue(args.force_refresh)
        self.assertEqual(args.offset, 3)
        self.assertIsNone(parse_web_extract_args(json.dumps({"url": "u"})).jina)

    def test_call_direct(self) -> None:
        client = _FakeClient(_FakeResponse(200, None, "<title>T</title><p>hi</p>"))
        tool = WebExtractTool(client=client)
        result = _run(tool.call(None, _call("web_extract", {"url": "https://a", "jina": False})))
        self.assertIn("Source: direct", result.content)
        self.assertIn("Title: T", result.content)
        self.assertIn("hi", result.content)

    def test_call_is_cached(self) -> None:
        client = _FakeClient(_FakeResponse(200, None, "<p>hi</p>"))
        tool = WebExtractTool(client=client)
        _run(tool.call(None, _call("web_extract", {"url": "https://a", "jina": False})))
        second = _run(
            tool.call(None, _call("web_extract", {"url": "https://a", "jina": False}))
        )
        self.assertEqual(len(client.calls), 1)
        self.assertIn("Cached: true", second.content)

    def test_call_requires_url(self) -> None:
        with self.assertRaises(InvalidError):
            _run(WebExtractTool().call(None, _call("web_extract", {"url": "  "})))


class TestChatHistoryHelpers(unittest.TestCase):
    def test_message_id(self) -> None:
        self.assertEqual(parse_chat_history_message_id(" #abc "), "abc")
        self.assertEqual(parse_chat_history_message_id("abc"), "abc")

    def test_terms(self) -> None:
        self.assertEqual(split_chat_history_terms("a, b，c|d\ne"), ["a", "b", "c", "d", "e"])
        self.assertEqual(split_chat_history_terms("   "), [])

    def test_clamps(self) -> None:
        self.assertEqual(clamp_chat_history_limit(0), 10)
        self.assertEqual(clamp_chat_history_limit(999), 50)
        self.assertEqual(clamp_chat_history_limit(7), 7)
        self.assertEqual(clamp_chat_history_window(-3), 0)

    def test_time_range(self) -> None:
        now = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        since, until = chat_history_time_range_at("", "", 2, 0, now)
        self.assertEqual(since, datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc))
        self.assertIsNone(until)
        since, _ = chat_history_time_range_at("", "", 0, 40, now)
        self.assertEqual(since.date(), datetime(2026, 2, 3, 12, 0, tzinfo=timezone.utc).date())
        parsed = parse_chat_history_time("1700000000")
        self.assertEqual(parsed, datetime.fromtimestamp(1700000000, tz=timezone.utc))
        self.assertIsNone(parse_chat_history_time(""))
        self.assertIsNone(parse_chat_history_time("nonsense"))

    def test_user_filter(self) -> None:
        ctx = ChatHistoryContext(platform="p", scope_id="s", platform_user_id="42")
        self.assertEqual(chat_history_user_filter("", ctx), ("", "", ""))
        self.assertEqual(chat_history_user_filter("我", ctx), ("42", "", ""))
        self.assertEqual(chat_history_user_filter("@7", ctx), ("7", "", ""))
        self.assertEqual(chat_history_user_filter("123", ctx), ("123", "", ""))
        self.assertEqual(chat_history_user_filter("nick", ctx), ("", "nick", ""))
        empty = ChatHistoryContext()
        self.assertNotEqual(chat_history_user_filter("我", empty)[2], "")

    def test_formatting(self) -> None:
        row = _chat_message()
        line = format_chat_history_line(row, row.platform_message_id)
        self.assertTrue(line.startswith("=> [#m1] "))
        self.assertIn("name(u1): hello", line)
        self.assertEqual(history_media_label("image", ""), "image (待入库)")
        self.assertEqual(history_media_label("image", "media:x"), "image media:x")
        long_text = "x" * 400
        self.assertTrue(truncate_chat_history_message(long_text).endswith("已截断]"))
        self.assertEqual(truncate_chat_history_message("a\nb"), "a b")

    def test_result_lines_truncates_at_boundary(self) -> None:
        text = chat_history_result_lines(["y" * 7000])
        self.assertTrue(text.endswith("[查询结果过长，已截断]"))
        self.assertEqual(chat_history_result_lines(["a", "b"]), "a\nb")

    def test_search_tool_requires_storage(self) -> None:
        with self.assertRaises(InvalidError):
            _run(SearchChatHistoryTool().call(None, _call("search_chat_history", {})))

    def test_search_tool_without_platform_context(self) -> None:
        tool = SearchChatHistoryTool(_FakeHistory())
        result = _run(tool.call(None, _call("search_chat_history", {})))
        self.assertIn("没有平台聊天信息", result.content)

    def test_search_tool_call(self) -> None:
        history = _FakeHistory()
        tool = SearchChatHistoryTool(history)
        ctx = _platform_ctx()
        result = _run(tool.call(ctx, _call("search_chat_history", {"query": "hi"})))
        self.assertIn("返回 1 条", result.content)
        self.assertIn("[#m1]", result.content)
        self.assertEqual(history.search_requests[0].platform, "demo")
        self.assertEqual(history.search_requests[0].query_terms, ["hi"])

    def test_search_tool_empty(self) -> None:
        history = _FakeHistory(rows=[])
        tool = SearchChatHistoryTool(history)
        result = _run(tool.call(_platform_ctx(), _call("search_chat_history", {})))
        self.assertEqual(result.content, "没有查到符合条件的当前聊天历史记录。")

    def test_around_tool(self) -> None:
        history = _FakeHistory()
        from atri_x.tool.builtin.chat_history import GetChatHistoryAroundTool

        tool = GetChatHistoryAroundTool(history)
        result = _run(
            tool.call(_platform_ctx(), _call("get_chat_history_around", {"message_id": "#m1"}))
        )
        self.assertIn("附近上下文", result.content)
        self.assertIn("=> [#m1]", result.content)

    def test_reply_tool(self) -> None:
        history = _FakeHistory()
        from atri_x.tool.builtin.chat_history import ReplyToChatHistoryMessageTool

        tool = ReplyToChatHistoryMessageTool(history)
        result = _run(
            tool.call(
                _platform_ctx(),
                _call(
                    "reply_to_chat_history_message",
                    {"message_id": "m1", "message": "ok"},
                ),
            )
        )
        self.assertIn("已引用回复 [#m1]", result.content)
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.outputs[0].reply_to_platform_message_id, "m1")

    def test_reply_tool_rejects_empty_message(self) -> None:
        from atri_x.tool.builtin.chat_history import ReplyToChatHistoryMessageTool

        tool = ReplyToChatHistoryMessageTool(_FakeHistory())
        result = _run(
            tool.call(
                _platform_ctx(),
                _call("reply_to_chat_history_message", {"message_id": "m1", "message": " "}),
            )
        )
        self.assertEqual(result.content, "回复内容不能为空。")


class TestResidentMemory(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_tool_names_and_metadata(self) -> None:
        tools = new_resident_memory_tools(None)
        self.assertEqual(len(tools), 4)
        names = [tool.name() for tool in tools]
        self.assertEqual(
            names,
            [
                RESIDENT_MEMORY_TOOL_NAME,
                RESIDENT_MEMORY_READ_TOOL_NAME,
                RESIDENT_MEMORY_NORMAL_TOOL_NAME,
                RESIDENT_MEMORY_CORE_TOOL_NAME,
            ],
        )
        self.assertEqual(ResidentMemoryCoreTool(None).info().risk, "high")
        self.assertTrue(ResidentMemoryReadTool(None).info().hidden)
        self.assertTrue(ResidentMemoryReadTool(None).info().owner_scoped)

    def test_entry_tool_without_store(self) -> None:
        tool = ResidentMemoryTool(None)
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _call(RESIDENT_MEMORY_TOOL_NAME, {})))

    def test_entry_tool_with_store(self) -> None:
        tool = ResidentMemoryTool(_FakeResidentStore())
        result = _run(tool.call(None, _call(RESIDENT_MEMORY_TOOL_NAME, {})))
        self.assertIn("resident_memory_read", result.content)

    def test_read_requires_actor(self) -> None:
        tool = ResidentMemoryReadTool(_FakeResidentStore())
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _call(RESIDENT_MEMORY_READ_TOOL_NAME, {})))

    def test_read_and_write_cycle(self) -> None:
        store = _FakeResidentStore()
        ctx = _actor_ctx()
        read = ResidentMemoryReadTool(store)
        normal = ResidentMemoryNormalTool(store)
        core = ResidentMemoryCoreTool(store)

        payload = _run(read.call(ctx, _call(RESIDENT_MEMORY_READ_TOOL_NAME, {})))
        self.assertIn('"status":"ok"', payload.content)
        self.assertIn('"limits"', payload.content)

        appended = _run(
            normal.call(
                ctx,
                _call(RESIDENT_MEMORY_NORMAL_TOOL_NAME, {"action": "append", "content": "a"}),
            )
        )
        self.assertEqual(appended.content, "已追加普通常驻记忆。")
        self.assertEqual(store.memory.normal, "a")

        deleted = _run(
            normal.call(ctx, _call(RESIDENT_MEMORY_NORMAL_TOOL_NAME, {"action": "delete"}))
        )
        self.assertEqual(deleted.content, "已清空普通常驻记忆。")

        written = _run(
            core.call(ctx, _call(RESIDENT_MEMORY_CORE_TOOL_NAME, {"content": "c"}))
        )
        self.assertEqual(written.content, "已更新核心常驻记忆。")
        self.assertEqual(store.memory.core, "c")

        section = _run(
            read.call(ctx, _call(RESIDENT_MEMORY_READ_TOOL_NAME, {"section": "core"}))
        )
        self.assertIn('"core":"c"', section.content)
        self.assertNotIn('"normal":"', section.content)

    def test_invalid_section_and_action(self) -> None:
        store = _FakeResidentStore()
        ctx = _actor_ctx()
        with self.assertRaises(InvalidError):
            _run(
                ResidentMemoryReadTool(store).call(
                    ctx, _call(RESIDENT_MEMORY_READ_TOOL_NAME, {"section": "nope"})
                )
            )
        with self.assertRaises(InvalidError):
            _run(
                ResidentMemoryNormalTool(store).call(
                    ctx, _call(RESIDENT_MEMORY_NORMAL_TOOL_NAME, {"action": "nope"})
                )
            )
        with self.assertRaises(InvalidError):
            _run(
                ResidentMemoryNormalTool(store).call(
                    ctx, _call(RESIDENT_MEMORY_NORMAL_TOOL_NAME, {"action": "append"})
                )
            )
        with self.assertRaises(InvalidError):
            _run(
                ResidentMemoryCoreTool(store).call(
                    ctx, _call(RESIDENT_MEMORY_CORE_TOOL_NAME, {})
                )
            )

    def test_descriptions_include_limits(self) -> None:
        self.assertIn("normal 上限", normal_description(None))
        self.assertIn("core 上限", core_description(None))
        self.assertIn("第三人称", normal_description(None))

    def test_memory_json_result(self) -> None:
        from atri_x.tool.builtin.resident_memory import MemoryReadResult
        from atri_x.memory.resident import Limits

        result = memory_json_result(MemoryReadResult(limits=Limits(core=1, normal=2), status="ok"))
        self.assertEqual(result.content, '{"limits":{"core":1,"normal":2},"status":"ok"}')


class TestPromptLibrary(unittest.TestCase):
    def test_metadata(self) -> None:
        tool = PromptLibrarySearchTool()
        self.assertEqual(tool.name(), "prompt_library_search")
        self.assertEqual(tool.info().tags, ["image"])
        self.assertIn("query", tool.schema().function.parameters["required"])

    def test_empty_query(self) -> None:
        tool = PromptLibrarySearchTool()
        result = _run(tool.call(None, _call("prompt_library_search", {"query": " "})))
        self.assertEqual(result.content, "query 不能为空。")

    def test_search_runs(self) -> None:
        tool = PromptLibrarySearchTool()
        result = _run(
            tool.call(None, _call("prompt_library_search", {"query": "cyberpunk", "limit": 3}))
        )
        self.assertIsInstance(result.content, str)
        self.assertTrue(result.content)

    def test_truncate_helper(self) -> None:
        self.assertEqual(truncate_prompt_lib_line("  a   b ", 10), "a b")
        self.assertTrue(truncate_prompt_lib_line("x" * 50, 5).endswith("..."))


class TestAngelMemory(unittest.TestCase):
    def test_tool_set_without_forget(self) -> None:
        tools = new_angel_memory_tools(_FakeAngelService())
        self.assertEqual(
            [tool.name() for tool in tools], ["angel_remember", "angel_recall"]
        )
        self.assertEqual(AngelRecallTool(_FakeAngelService()).info().risk, "low")

    def test_tool_set_with_forget(self) -> None:
        tools = new_angel_memory_tools(
            _FakeAngelService(), AngelMemoryToolOptions(allow_forget=True)
        )
        self.assertEqual(
            [tool.name() for tool in tools],
            ["angel_remember", "angel_recall", "angel_forget"],
        )
        forget = tools[2]
        self.assertEqual(forget.info().risk, "high")
        self.assertTrue(forget.info().hidden)
        self.assertTrue(forget.info().owner_scoped)

    def test_service_not_configured(self) -> None:
        result = _run(
            AngelRememberTool(None).call(_platform_ctx(), _call("angel_remember", {}))
        )
        self.assertEqual(result.content, "angel memory 服务未配置。")

    def test_remember_without_platform_context(self) -> None:
        result = _run(
            AngelRememberTool(_FakeAngelService()).call(
                None, _call("angel_remember", {"content": "x"})
            )
        )
        self.assertIn("没有平台聊天信息", result.content)

    def test_remember_empty_content(self) -> None:
        result = _run(
            AngelRememberTool(_FakeAngelService()).call(
                _platform_ctx(), _call("angel_remember", {"content": "  "})
            )
        )
        self.assertEqual(result.content, "content 不能为空。")

    def test_remember_success(self) -> None:
        service = _FakeAngelService()
        result = _run(
            AngelRememberTool(service).call(
                _actor_ctx(),
                _call("angel_remember", {"content": " 喜欢猫 ", "tags": "pet"}),
            )
        )
        self.assertIn("已记住（强度 1）", result.content)
        self.assertEqual(service.remembered[0][3], "喜欢猫")
        self.assertEqual(service.remembered[0][4].actor_id, "u1")
        self.assertEqual(service.remembered[0][4].kind, "tool")

    def test_recall(self) -> None:
        service = _FakeAngelService()
        result = _run(
            AngelRecallTool(service).call(
                _platform_ctx(), _call("angel_recall", {"query": "猫"})
            )
        )
        self.assertIn("找到 1 条相关记忆", result.content)
        self.assertIn("source=tool", result.content)
        self.assertIn("actor=u1", result.content)

    def test_recall_empty(self) -> None:
        service = _FakeAngelService(memories=[])
        result = _run(
            AngelRecallTool(service).call(_platform_ctx(), _call("angel_recall", {}))
        )
        self.assertEqual(result.content, "没有找到符合条件的长期记忆。")

    def test_forget_preview_then_confirm(self) -> None:
        service = _FakeAngelService()
        tool = AngelForgetTool(service)
        ctx = _actor_ctx()
        preview = _run(tool.call(ctx, _call("angel_forget", {"id": "abc"})))
        self.assertIn("待删除的长期记忆（尚未删除）", preview.content)
        self.assertEqual(service.deleted, [])
        confirmed = _run(
            tool.call(ctx, _call("angel_forget", {"id": "abc", "confirm": True}))
        )
        self.assertIn("已删除长期记忆", confirmed.content)
        self.assertEqual(service.deleted, [("demo", "scope", "abcdef")])

    def test_forget_rejects_empty_id(self) -> None:
        tool = AngelForgetTool(_FakeAngelService())
        result = _run(tool.call(_actor_ctx(), _call("angel_forget", {})))
        self.assertIn("id 不能为空", result.content)

    def test_forget_requires_actor(self) -> None:
        tool = AngelForgetTool(_FakeAngelService())
        result = _run(tool.call(_platform_actorless_ctx(), _call("angel_forget", {"id": "a"})))
        self.assertIn("没有足够的用户信息", result.content)

    def test_forget_rejects_other_actor(self) -> None:
        # 库层不按来源过滤时，工具层必须自己再挡一次（对应 Go 的 resolve 二次校验）。
        service = _FakeAngelService(owner="other", enforce_filter=False)
        tool = AngelForgetTool(service)
        result = _run(
            tool.call(_actor_ctx(), _call("angel_forget", {"id": "abc", "confirm": True}))
        )
        self.assertIn("来源成员不是当前用户", result.content)
        self.assertEqual(service.deleted, [])

    def test_forget_reports_missing_prefix(self) -> None:
        tool = AngelForgetTool(_FakeAngelService())
        result = _run(
            tool.call(_actor_ctx(), _call("angel_forget", {"id": "zzz", "confirm": True}))
        )
        self.assertIn("没有找到 id 前缀", result.content)

    def test_forget_rate_limit(self) -> None:
        from atri_x.tool.builtin.angel_memory import ANGEL_FORGET_MAX_PER_MINUTE

        service = _FakeAngelService()
        tool = AngelForgetTool(
            service, deletions=new_rate_limit(ANGEL_FORGET_MAX_PER_MINUTE, 60.0)
        )
        ctx = _actor_ctx()
        for _ in range(ANGEL_FORGET_MAX_PER_MINUTE):
            _run(tool.call(ctx, _call("angel_forget", {"id": "abc", "confirm": True})))
        blocked = _run(
            tool.call(ctx, _call("angel_forget", {"id": "abc", "confirm": True}))
        )
        self.assertIn("最多通过工具删除", blocked.content)

    def test_forget_preflight_and_risk_detail(self) -> None:
        service = _FakeAngelService()
        tool = AngelForgetTool(service)
        ctx = _actor_ctx()
        _run(tool.preflight_confirmation(ctx, _call("angel_forget", {"id": "abc"})))
        detail = _run(tool.risk_detail(ctx, _call("angel_forget", {"id": "abc"})))
        self.assertIn("删除后无法恢复。", detail)
        with self.assertRaises(InvalidError):
            _run(tool.preflight_confirmation(ctx, _call("angel_forget", {})))

    def test_helpers(self) -> None:
        self.assertEqual(angel_forget_inline("  a   b "), "a b")
        self.assertTrue(angel_forget_inline("x" * 300).endswith("…"))
        self.assertEqual(short_angel_memory_id("0123456789abcdef"), "0123456789ab")
        self.assertEqual(short_angel_memory_id("short"), "short")
        memory = _FakeAngelService().memories[0]
        self.assertEqual(memory_trace_suffix(memory), " (source=tool actor=u1)")
        self.assertEqual(angel_forget_source_text(memory), "kind=tool")
        self.assertEqual(angel_forget_rate_key(_platform_ctx(), "u1"), "demo\x00scope\x00u1")

    def test_recall_exposes_forget_dependency(self) -> None:
        tool = AngelRecallTool(_FakeAngelService(), expose_forget=True)
        self.assertIn("angel_forget", tool.info().depends_on)
        self.assertNotIn(
            "angel_forget", AngelRecallTool(_FakeAngelService()).info().depends_on
        )


class TestRegister(unittest.TestCase):
    #: ``test_register_all`` 只给这三个依赖；其余条件组必须一个都不注册。
    GIVEN_OPTIONS = ("resident_memory_store", "angel_memory", "chat_history")

    def test_registered_names_are_unique(self) -> None:
        self.assertEqual(len(REGISTERED_TOOLS), len(set(REGISTERED_TOOLS)))
        self.assertIn("read_file", REGISTERED_TOOLS)
        self.assertIn("shell", REGISTERED_TOOLS)
        # skill 一族接线后，Go ``RegisterAll`` 里的每一条都有对应语句：
        # 没有「Go 有、本仓未接线」的工具了。
        self.assertEqual(UNPORTED_TOOLS, ())
        # ``REGISTERED_TOOLS`` = 无条件 + 条件注册；两个集合不重叠。
        conditional = [name for group in CONDITIONAL_TOOLS.values() for name in group]
        self.assertEqual(len(conditional), len(set(conditional)))
        self.assertFalse(set(ALWAYS_REGISTERED_TOOLS) & set(conditional))
        self.assertEqual(
            set(REGISTERED_TOOLS), set(ALWAYS_REGISTERED_TOOLS) | set(conditional)
        )

    def test_register_all(self) -> None:
        """只给三个依赖：无条件工具 + 这三组条件工具注册，其余整组缺席。

        Go ``internal/tool/builtin/register.go:64-224`` 里，除 discover/workspace/
        web_search/web_extract/artist_extract/prompt_library_search/read_file/edit_file/
        rollback_file/shell/atriwisp_creator 之外**每一条都有 if 守卫**；本仓必须一致。
        """
        registry = Registry()
        opts = RegisterOptions(
            resident_memory_store=_FakeResidentStore(),
            angel_memory=_FakeAngelService(),
            chat_history=_FakeHistory(),
        )
        register_all(registry, opts)
        names = {info.name for info in registry.list()}
        for name in ALWAYS_REGISTERED_TOOLS:
            self.assertIn(name, names, name)
        for field_name, group in CONDITIONAL_TOOLS.items():
            for name in group:
                if field_name in self.GIVEN_OPTIONS:
                    self.assertIn(name, names, name)
                else:
                    self.assertNotIn(name, names, name)
        # ``discover_tool`` 是入口工具：注册了，但不在模型可见面（Go ``Registry.ToolNames``）。
        self.assertIn("discover_tool", names)
        self.assertNotIn("discover_tool", registry.tool_names())

    def test_register_all_with_forget(self) -> None:
        registry = Registry()
        register_all(
            registry,
            RegisterOptions(
                angel_memory=_FakeAngelService(), angel_memory_allow_forget=True
            ),
        )
        names = {info.name for info in registry.list()}
        self.assertIn("angel_forget", names)
        self.assertIn("angel_forget", CONDITIONAL_TOOLS["angel_memory_allow_forget"])
        recall = registry.get("angel_recall")[0]
        self.assertIn("angel_forget", recall.info().depends_on)

    def test_register_all_minimal(self) -> None:
        registry = Registry()
        register_all(registry, RegisterOptions())
        names = {info.name for info in registry.list()}
        self.assertIn("workspace", names)
        self.assertIn("web_search", names)
        self.assertNotIn("resident_memory", names)
        # 一个依赖都没给：所有条件组都不注册。
        for group in CONDITIONAL_TOOLS.values():
            for name in group:
                self.assertNotIn(name, names, name)

    def test_register_rejects_duplicates(self) -> None:
        registry = Registry()
        register_all(registry, RegisterOptions())
        with self.assertRaises(InvalidError):
            register_all(registry, RegisterOptions())


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
def _chat_message(**overrides):
    from atri_x.storage import ChatMessage

    base = ChatMessage(
        platform="demo",
        platform_scope_id="scope",
        platform_message_id="m1",
        sender_id="u1",
        sender_name="name",
        text="hello",
        segments="[]",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class _FakeHistory:
    def __init__(self, rows=None) -> None:
        self.rows = rows if rows is not None else [_chat_message()]
        self.search_requests: list = []
        self.around_requests: list = []

    def search(self, req):
        self.search_requests.append(req)
        return self.rows

    def around(self, req):
        self.around_requests.append(req)
        return self.rows

    def get_by_platform_message(self, platform, scope_id, message_id):
        from atri_x.errors import NotFoundError

        for row in self.rows:
            if row.platform_message_id == message_id:
                return row
        raise NotFoundError("not found")


class _FakeResidentStore:
    def __init__(self) -> None:
        from atri_x.memory.resident import Limits, Memory, NormalWritePolicy

        self.memory = Memory(core="", normal="")
        self.path = os.path.join(TMP_ROOT, "memories.toml")
        self.normal_policy = NormalWritePolicy()

    def read(self, scope):
        from atri_x.errors import NotFoundError

        if not self.memory.core and not self.memory.normal:
            raise NotFoundError("no memory")
        return self.memory

    def write_core(self, scope, content):
        self.memory.core = content

    def write_normal(self, scope, content):
        self.memory.normal = content

    def append_normal(self, scope, content):
        self.memory.normal = (self.memory.normal + "\n" + content).strip()

    def delete_normal(self, scope):
        self.memory.normal = ""

    def limits_or_default(self):
        from atri_x.memory.resident import Limits

        return Limits(core=200, normal=300)


def _platform_ctx():
    from atri_x.platform import MessageContext, with_message_context

    return with_message_context(
        None,
        MessageContext(
            platform="demo", scope_id="scope", platform_user_id="42"
        ),
    )


def _actor_ctx():
    from atri_x.platform import MessageContext, with_message_context
    from atri_x.security import Actor, RoleUser, with_actor

    ctx = with_actor(None, Actor(id="u1", role=RoleUser, platform="demo"))
    return with_message_context(
        ctx,
        MessageContext(platform="demo", scope_id="scope", platform_user_id="42"),
    )


def _platform_actorless_ctx():
    from atri_x.platform import MessageContext, with_message_context

    return with_message_context(
        None, MessageContext(platform="demo", scope_id="scope", platform_user_id="42")
    )


class _FakeAngelService:
    """``angelmemory.Service`` 的替身。"""

    def __init__(self, memories=None, owner: str = "u1", enforce_filter: bool = True) -> None:
        from atri_x.angelmemory import Memory

        self.memories = (
            memories
            if memories is not None
            else [
                Memory(
                    id="abcdef",
                    platform="demo",
                    scope_id="scope",
                    content="喜欢猫",
                    strength=1,
                    source="tool",
                    source_kind="tool",
                    source_actor_id=owner,
                )
            ]
        )
        self.remembered: list = []
        self.deleted: list = []
        self.owner = owner
        self.enforce_filter = enforce_filter

    def ready(self) -> bool:
        return True

    def remember_with_source(self, platform, scope_id, content, tags, source=None):
        from atri_x.angelmemory import Memory

        self.remembered.append((platform, scope_id, tags, content, source))
        return Memory(
            id="newid",
            platform=platform,
            scope_id=scope_id,
            content=content,
            tags=tags,
            strength=1,
            source="tool",
            source_kind="tool",
            source_actor_id=getattr(source, "actor_id", ""),
        )

    def recall(self, platform, scope_id, query, limit):
        return list(self.memories)

    def resolve_id(self, platform, scope_id, prefix, filter):
        from atri_x.angelmemory import ErrAmbiguousID, ErrNotFound

        matches = [
            memory
            for memory in self.memories
            if memory.id.startswith(prefix)
            and (
                not self.enforce_filter
                or filter is None
                or not filter.actor_id
                or memory.source_actor_id == filter.actor_id
            )
        ]
        if not matches:
            raise ErrNotFound
        if len(matches) > 1:
            raise ErrAmbiguousID
        return matches[0].id

    def get(self, platform, scope_id, id):
        from atri_x.errors import NotFoundError

        for memory in self.memories:
            if memory.id == id:
                return memory
        raise NotFoundError("not found")

    def delete(self, platform, scope_id, id):
        self.deleted.append((platform, scope_id, id))


if __name__ == "__main__":
    unittest.main()
