"""``web_search`` 的通道链：HTTP 通道、外部 CLI 通道、配置化的尝试顺序。

覆盖：Firecrawl / Keenable / Exa / SearXNG 的请求形状与解析、通道级失败如何转成
:class:`~atri_x.tool.builtin.web_search_engines.EngineFailure`、``agy`` / ``grok`` 的
子进程信封解析、工具层的降级顺序、``[web_search]`` 配置与 ``WEB_SEARCH_ENGINES`` 覆盖。
"""

import asyncio
import json
import os
import subprocess
import unittest
from unittest import mock

from atri_x.errors import InvalidError
from atri_x.net.httpclient import HTTPError
from atri_x.processenv import Environment
from atri_x.tool.builtin import web_search_cli
from atri_x.tool.builtin.web_search import (
    WEB_SEARCH_ENGINES_ENV,
    WebSearchTool,
    format_search_content,
    format_tavily_content,
    parse_tavily_response,
)
from atri_x.tool.builtin.web_search_engines import (
    EXA_DEFAULT_ENDPOINT,
    KEENABLE_KEYED_ENDPOINT,
    KEENABLE_KEYLESS_ENDPOINT,
    PROTOCOL_ANTIGRAVITY,
    PROTOCOL_EXA,
    PROTOCOL_FIRECRAWL,
    PROTOCOL_GROK,
    PROTOCOL_KEENABLE,
    PROTOCOL_SEARXNG,
    PROTOCOL_TAVILY,
    EngineFailure,
    SearchEngineSpec,
    SearchHit,
    SearchOutcome,
    SearchQuery,
    default_specs,
    search_exa,
    search_firecrawl,
    search_keenable,
    search_searxng,
    specs_from_config,
    truncate_http_body,
)
from atri_x.tool.tool import CallRequest

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"
TAVILY_ENDPOINT = "https://api.test/search"
SEARXNG_BASE = "https://searx.example.com"
SEARXNG_ENDPOINT = SEARXNG_BASE + "/search"
_ENV_KEYS = (
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
    "KEENABLE_API_KEY",
    "EXA_API_KEY",
    WEB_SEARCH_ENGINES_ENV,
)


def _call(arguments: dict) -> CallRequest:
    return CallRequest(id="1", name="web_search", arguments=json.dumps(arguments))


def _run(coro):
    return asyncio.run(coro)


class _Response:
    """与 :class:`atri_x.net.httpclient.Response` 的字段/属性保持一致。"""

    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status = status
        self.body = text.encode("utf-8")
        self._payload = payload

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return self._payload


class _RoutingClient:
    """按 URL 分发响应；值为异常时抛出，模拟传输错误。"""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        value = self.routes.get(path)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise AssertionError(f"unexpected request: {path}")
        return value


class TestFirecrawlEngine(unittest.TestCase):
    def test_keyless_request_and_parse(self) -> None:
        client = _RoutingClient(
            {
                FIRECRAWL_ENDPOINT: _Response(
                    200,
                    {
                        "success": True,
                        "data": {
                            "web": [
                                {"title": "T1", "url": "https://a", "description": "D1"},
                                {"title": "T2", "url": "https://b"},
                            ]
                        },
                    },
                )
            }
        )
        outcome = _run(
            search_firecrawl(client, SearchQuery(query="q", max_results=3), timeout=12.0)
        )
        self.assertEqual(outcome.engine, "firecrawl")
        self.assertEqual([hit.title for hit in outcome.hits], ["T1", "T2"])
        self.assertEqual(outcome.hits[0].snippet, "D1")
        self.assertEqual(outcome.hits[1].snippet, "")
        self.assertEqual(outcome.answer, "")
        self.assertTrue(outcome.warnings)
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", FIRECRAWL_ENDPOINT))
        self.assertNotIn("Authorization", kw["headers"])
        self.assertEqual(kw["json"]["limit"], 3)
        self.assertEqual(kw["json"]["sources"], ["web"])
        self.assertEqual(kw["json"]["timeout"], 12000)

    def test_keyed_request_sends_bearer(self) -> None:
        client = _RoutingClient({FIRECRAWL_ENDPOINT: _Response(200, {"data": {"web": []}})})
        outcome = _run(
            search_firecrawl(client, SearchQuery(query="q"), api_key="fk", timeout=15.0)
        )
        self.assertEqual(outcome.hits, [])
        self.assertEqual(client.calls[0][2]["headers"]["Authorization"], "Bearer fk")

    def test_transport_error_becomes_engine_failure(self) -> None:
        client = _RoutingClient({FIRECRAWL_ENDPOINT: HTTPError("boom")})
        with self.assertRaises(EngineFailure) as ctx:
            _run(search_firecrawl(client, SearchQuery(query="q")))
        self.assertIn("Firecrawl 请求失败", str(ctx.exception))

    def test_bad_status_becomes_engine_failure(self) -> None:
        client = _RoutingClient({FIRECRAWL_ENDPOINT: _Response(429, {}, "slow down")})
        with self.assertRaises(EngineFailure) as ctx:
            _run(search_firecrawl(client, SearchQuery(query="q")))
        self.assertIn("Firecrawl HTTP 429", str(ctx.exception))
        self.assertIn("slow down", str(ctx.exception))

    def test_non_object_json_becomes_engine_failure(self) -> None:
        client = _RoutingClient({FIRECRAWL_ENDPOINT: _Response(200, ["not", "an", "object"])})
        with self.assertRaises(EngineFailure) as ctx:
            _run(search_firecrawl(client, SearchQuery(query="q")))
        self.assertIn("不是一个 JSON 对象", str(ctx.exception))


class TestKeenableEngine(unittest.TestCase):
    def test_keyless_request_and_parse(self) -> None:
        client = _RoutingClient(
            {
                KEENABLE_KEYLESS_ENDPOINT: _Response(
                    200,
                    {
                        "mode": "pro",
                        "results": [
                            {
                                "title": "T",
                                "url": "https://a",
                                "description": "",
                                "snippet": "S",
                                "published_at": "2026-10-18",
                            },
                            {"title": "U", "url": "https://b", "description": "D"},
                        ],
                    },
                )
            }
        )
        outcome = _run(search_keenable(client, SearchQuery(query="q", max_results=2)))
        self.assertEqual(outcome.engine, "keenable")
        self.assertEqual(outcome.hits[0].snippet, "S")
        self.assertEqual(outcome.hits[1].snippet, "D")
        self.assertTrue(outcome.warnings)
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", KEENABLE_KEYLESS_ENDPOINT))
        self.assertEqual(kw["headers"]["X-Keenable-Title"], "atri-x")
        self.assertNotIn("X-API-Key", kw["headers"])
        self.assertEqual(kw["json"]["max_results"], 2)

    def test_keyed_request_switches_endpoint_and_header(self) -> None:
        client = _RoutingClient({KEENABLE_KEYED_ENDPOINT: _Response(200, {"results": []})})
        _run(search_keenable(client, SearchQuery(query="q"), api_key="kk"))
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", KEENABLE_KEYED_ENDPOINT))
        self.assertEqual(kw["headers"]["X-API-Key"], "kk")
        self.assertNotIn("X-Keenable-Title", kw["headers"])

    def test_max_results_is_clamped(self) -> None:
        client = _RoutingClient({KEENABLE_KEYLESS_ENDPOINT: _Response(200, {"results": []})})
        _run(search_keenable(client, SearchQuery(query="q", max_results=999)))
        self.assertEqual(client.calls[0][2]["json"]["max_results"], 50)


class TestEngineChain(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_falls_back_from_firecrawl_to_keenable(self) -> None:
        client = _RoutingClient(
            {
                FIRECRAWL_ENDPOINT: _Response(500, {}, "down"),
                KEENABLE_KEYLESS_ENDPOINT: _Response(
                    200, {"results": [{"title": "K", "url": "https://k", "snippet": "S"}]}
                ),
            }
        )
        tool = WebSearchTool(client=client)
        result = _run(tool.call(None, _call({"query": "hi"})))
        self.assertIn("1. K", result.content)
        self.assertIn("https://k", result.content)
        self.assertEqual([call[1] for call in client.calls], [FIRECRAWL_ENDPOINT, KEENABLE_KEYLESS_ENDPOINT])

    def test_prefers_tavily_when_key_present(self) -> None:
        os.environ["TAVILY_API_KEY"] = "k"
        client = _RoutingClient(
            {
                TAVILY_ENDPOINT: _Response(
                    200, {"answer": "A", "results": [{"title": "T", "url": "u", "content": "c"}]}
                )
            }
        )
        tool = WebSearchTool(client=client, endpoint=TAVILY_ENDPOINT)
        result = _run(tool.call(None, _call({"query": "hi"})))
        self.assertIn("Answer: A", result.content)
        self.assertEqual([call[1] for call in client.calls], [TAVILY_ENDPOINT])

    def test_all_engines_fail_reports_every_reason(self) -> None:
        client = _RoutingClient(
            {
                FIRECRAWL_ENDPOINT: _Response(500, {}, "f-down"),
                KEENABLE_KEYLESS_ENDPOINT: _Response(503, {}, "k-down"),
            }
        )
        tool = WebSearchTool(client=client)
        with self.assertRaises(InvalidError) as ctx:
            _run(tool.call(None, _call({"query": "hi"})))
        message = str(ctx.exception)
        self.assertIn("所有通道均失败", message)
        self.assertIn("firecrawl", message)
        self.assertIn("keenable", message)

    def test_env_overrides_engine_order(self) -> None:
        os.environ[WEB_SEARCH_ENGINES_ENV] = "keenable"
        client = _RoutingClient(
            {KEENABLE_KEYLESS_ENDPOINT: _Response(200, {"results": [{"title": "K", "url": "u"}]})}
        )
        tool = WebSearchTool(client=client)
        _run(tool.call(None, _call({"query": "hi"})))
        self.assertEqual([call[1] for call in client.calls], [KEENABLE_KEYLESS_ENDPOINT])

    def test_explicit_tavily_without_key_is_an_error(self) -> None:
        os.environ[WEB_SEARCH_ENGINES_ENV] = "tavily"
        tool = WebSearchTool(client=_RoutingClient({}))
        with self.assertRaises(InvalidError) as ctx:
            _run(tool.call(None, _call({"query": "hi"})))
        self.assertIn("TAVILY_API_KEY", str(ctx.exception))
        self.assertIn("免注册通道", str(ctx.exception))

    def test_unknown_engine_name_is_an_error(self) -> None:
        os.environ[WEB_SEARCH_ENGINES_ENV] = "tavily,bing"
        tool = WebSearchTool(client=_RoutingClient({}))
        with self.assertRaises(InvalidError) as ctx:
            _run(tool.call(None, _call({"query": "hi"})))
        self.assertIn("bing", str(ctx.exception))


class TestFormatting(unittest.TestCase):
    def test_format_search_content(self) -> None:
        text = format_search_content(
            SearchOutcome(
                engine="keenable",
                hits=[
                    SearchHit(title="T", url="https://a", snippet="S"),
                    SearchHit(title="U", url="https://b"),
                ],
            )
        )
        self.assertIn("1. T\nhttps://a\nS", text)
        self.assertIn("2. U\nhttps://b", text)
        self.assertNotIn("Answer:", text)

    def test_format_search_content_empty(self) -> None:
        self.assertEqual(format_search_content(SearchOutcome(engine="firecrawl")), "没有搜索到结果。")

    def test_truncate_http_body(self) -> None:
        self.assertEqual(truncate_http_body(b"short"), "short")
        long_body = truncate_http_body(b"x" * 3000)
        self.assertTrue(long_body.endswith("..."))
        self.assertEqual(len(long_body), 2048 + 3)

    def test_tavily_outcome_renders_like_go(self) -> None:
        """Tavily 通道归一成 SearchOutcome 之后，文本仍与 ``format_tavily_content`` 一致。"""

        data = parse_tavily_response(
            {
                "answer": "A",
                "results": [
                    {"title": "T", "url": "https://a", "content": "C"},
                    {"title": "U", "url": "https://b"},
                ],
            }
        )
        outcome = SearchOutcome(
            engine="tavily",
            answer=data.answer,
            hits=[
                SearchHit(title=item.title, url=item.url, snippet=item.content)
                for item in data.results
            ],
        )
        self.assertEqual(format_search_content(outcome), format_tavily_content(data))


class TestExaEngine(unittest.TestCase):
    def test_requires_key(self) -> None:
        with self.assertRaises(EngineFailure) as ctx:
            _run(search_exa(_RoutingClient({}), SearchQuery(query="q")))
        self.assertIn("API key", str(ctx.exception))

    def test_request_and_parse(self) -> None:
        client = _RoutingClient(
            {
                EXA_DEFAULT_ENDPOINT: _Response(
                    200,
                    {
                        "results": [
                            {
                                "title": "T",
                                "url": "https://a",
                                "highlights": ["H1", "H2"],
                            },
                            {"title": "U", "url": "https://b"},
                        ]
                    },
                )
            }
        )
        outcome = _run(search_exa(client, SearchQuery(query="q", max_results=2), api_key="ek"))
        self.assertEqual(outcome.engine, "exa")
        self.assertEqual(outcome.hits[0].snippet, "H1")
        self.assertEqual(outcome.hits[1].snippet, "")
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", EXA_DEFAULT_ENDPOINT))
        self.assertEqual(kw["headers"]["x-api-key"], "ek")
        self.assertEqual(kw["json"]["numResults"], 2)
        self.assertEqual(kw["json"]["contents"], {"highlights": True})


class TestSearxngEngine(unittest.TestCase):
    def test_requires_base_url(self) -> None:
        with self.assertRaises(EngineFailure) as ctx:
            _run(search_searxng(_RoutingClient({}), SearchQuery(query="q")))
        self.assertIn("base_url", str(ctx.exception))

    def test_request_and_parse(self) -> None:
        client = _RoutingClient(
            {
                SEARXNG_ENDPOINT: _Response(
                    200,
                    {
                        "results": [
                            {"title": "T", "url": "https://a", "content": "C"},
                            {"title": "U", "url": "https://b"},
                        ]
                    },
                )
            }
        )
        outcome = _run(
            search_searxng(client, SearchQuery(query="hello world"), endpoint=SEARXNG_BASE)
        )
        self.assertEqual(outcome.engine, "searxng")
        self.assertEqual(len(outcome.hits), 2)
        self.assertEqual(outcome.hits[0].snippet, "C")
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("GET", SEARXNG_ENDPOINT))
        self.assertEqual(kw["params"]["q"], "hello world")
        self.assertEqual(kw["params"]["format"], "json")


class TestSpecsFromConfig(unittest.TestCase):
    class _Engine:
        def __init__(self, protocol: str = "", api_key_env: str = "", base_url: str = "", binary: str = "") -> None:
            self.protocol = protocol
            self.api_key_env = api_key_env
            self.base_url = base_url
            self.bin = binary

    class _Config:
        def __init__(self, order: list, engines: dict) -> None:
            self.engine_order = order
            self.engines = engines

    def test_absent_config_is_empty(self) -> None:
        self.assertEqual(specs_from_config(None), [])

    def test_builtin_name_defaults_protocol(self) -> None:
        specs = specs_from_config(self._Config(["firecrawl"], {"firecrawl": self._Engine()}))
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].protocol, PROTOCOL_FIRECRAWL)
        self.assertEqual(specs[0].key_env(), "FIRECRAWL_API_KEY")

    def test_custom_name_needs_protocol(self) -> None:
        specs = specs_from_config(
            self._Config(
                ["mine", "unknown"],
                {
                    "mine": self._Engine(protocol="searxng", base_url="https://s.example.com"),
                    "unknown": self._Engine(),
                },
            )
        )
        self.assertEqual([spec.name for spec in specs], ["mine"])
        self.assertEqual(specs[0].base_url, "https://s.example.com")

    def test_bin_is_carried(self) -> None:
        specs = specs_from_config(
            self._Config(["agy"], {"agy": self._Engine(protocol="antigravity", binary="agy-custom")})
        )
        self.assertEqual(specs[0].binary, "agy-custom")

    def test_default_specs_order(self) -> None:
        self.assertEqual(
            [spec.protocol for spec in default_specs()],
            [PROTOCOL_TAVILY, PROTOCOL_FIRECRAWL, PROTOCOL_KEENABLE, PROTOCOL_EXA],
        )


class TestConfiguredChain(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_configured_order_is_respected(self) -> None:
        client = _RoutingClient(
            {
                SEARXNG_ENDPOINT: _Response(
                    200, {"results": [{"title": "S", "url": "https://s", "content": "C"}]}
                )
            }
        )
        specs = [
            SearchEngineSpec(name="my-searx", protocol=PROTOCOL_SEARXNG, base_url=SEARXNG_BASE),
            SearchEngineSpec(name="firecrawl", protocol=PROTOCOL_FIRECRAWL),
        ]
        tool = WebSearchTool(client=client, specs=specs)
        result = _run(tool.call(None, _call({"query": "hi"})))
        self.assertIn("1. S", result.content)
        self.assertEqual([call[1] for call in client.calls], [SEARXNG_ENDPOINT])

    def test_engine_without_base_url_is_skipped(self) -> None:
        specs = [
            SearchEngineSpec(name="my-searx", protocol=PROTOCOL_SEARXNG),
            SearchEngineSpec(name="firecrawl", protocol=PROTOCOL_FIRECRAWL),
        ]
        client = _RoutingClient(
            {FIRECRAWL_ENDPOINT: _Response(200, {"data": {"web": [{"title": "F", "url": "u"}]}})}
        )
        tool = WebSearchTool(client=client, specs=specs)
        result = _run(tool.call(None, _call({"query": "hi"})))
        self.assertIn("1. F", result.content)
        self.assertEqual([call[1] for call in client.calls], [FIRECRAWL_ENDPOINT])

    def test_keyed_engine_with_key_env_from_specs(self) -> None:
        specs = [SearchEngineSpec(name="exa", protocol=PROTOCOL_EXA, api_key_env="MY_EXA_KEY")]
        client = _RoutingClient({EXA_DEFAULT_ENDPOINT: _Response(200, {"results": []})})
        tool = WebSearchTool(
            Environment(["MY_EXA_KEY=secret"]), client=client, specs=specs
        )
        _run(tool.call(None, _call({"query": "hi"})))
        self.assertEqual(client.calls[0][2]["headers"]["x-api-key"], "secret")

    def test_cli_engine_reports_missing_binary(self) -> None:
        specs = [SearchEngineSpec(name="agy", protocol=PROTOCOL_ANTIGRAVITY)]
        tool = WebSearchTool(client=_RoutingClient({}), specs=specs)
        with self.assertRaises(InvalidError) as ctx:
            _run(tool.call(None, _call({"query": "hi"})))
        message = str(ctx.exception)
        self.assertIn("agy", message)
        self.assertIn("所有通道均失败", message)


class TestCliEngines(unittest.TestCase):
    def _completed(self, stdout: str, code: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["cli"], returncode=code, stdout=stdout, stderr="")

    def test_antigravity_parses_structured_output(self) -> None:
        payload = {
            "status": "SUCCESS",
            "structured_output": {
                "summary": "S",
                "items": [{"title": "T", "url": "https://a", "snippet": "P"}],
            },
        }
        with mock.patch.object(web_search_cli.shutil, "which", return_value="C:/fake/agy"), mock.patch.object(
            web_search_cli.subprocess, "run", return_value=self._completed(json.dumps(payload))
        ):
            outcome = _run(web_search_cli.search_antigravity(SearchQuery(query="q")))
        self.assertEqual(outcome.engine, PROTOCOL_ANTIGRAVITY)
        self.assertEqual(outcome.answer, "S")
        self.assertEqual(outcome.hits[0].url, "https://a")

    def test_antigravity_reports_quota_status(self) -> None:
        payload = {"status": "QUOTA_EXHAUSTED", "error": "reset at 12:00"}
        with mock.patch.object(web_search_cli.shutil, "which", return_value="C:/fake/agy"), mock.patch.object(
            web_search_cli.subprocess, "run", return_value=self._completed(json.dumps(payload))
        ):
            with self.assertRaises(EngineFailure) as ctx:
                _run(web_search_cli.search_antigravity(SearchQuery(query="q")))
        self.assertIn("QUOTA_EXHAUSTED", str(ctx.exception))
        self.assertIn("reset at 12:00", str(ctx.exception))

    def test_grok_salvages_json_from_text(self) -> None:
        payload = {
            "text": 'searching...\n{"summary": "X", "items": [{"title": "T", "url": "https://x"}]}',
        }
        with mock.patch.object(web_search_cli.shutil, "which", return_value="C:/fake/grok"), mock.patch.object(
            web_search_cli.subprocess, "run", return_value=self._completed(json.dumps(payload))
        ):
            outcome = _run(web_search_cli.search_grok(SearchQuery(query="q")))
        self.assertEqual(outcome.engine, PROTOCOL_GROK)
        self.assertEqual(outcome.answer, "X")
        self.assertEqual(outcome.hits[0].url, "https://x")

    def test_grok_in_progress_placeholder_is_a_failure(self) -> None:
        result = {"summary": "正在检索…", "items": [], "uncertainty": []}
        with mock.patch.object(web_search_cli.shutil, "which", return_value="C:/fake/grok"), mock.patch.object(
            web_search_cli.subprocess, "run",
            return_value=self._completed(json.dumps({"structuredOutput": result})),
        ):
            with self.assertRaises(EngineFailure) as ctx:
                _run(web_search_cli.search_grok(SearchQuery(query="q")))
        self.assertIn("占位", str(ctx.exception))

    def test_nonzero_exit_is_a_failure(self) -> None:
        with mock.patch.object(web_search_cli.shutil, "which", return_value="C:/fake/agy"), mock.patch.object(
            web_search_cli.subprocess, "run", return_value=self._completed("", code=3)
        ):
            with self.assertRaises(EngineFailure) as ctx:
                _run(web_search_cli.search_antigravity(SearchQuery(query="q")))
        self.assertIn("退出码 3", str(ctx.exception))

    def test_missing_binary_is_a_failure(self) -> None:
        with mock.patch.object(web_search_cli.shutil, "which", return_value=None):
            with self.assertRaises(EngineFailure) as ctx:
                _run(web_search_cli.search_antigravity(SearchQuery(query="q")))
        self.assertIn("找不到可执行文件", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
