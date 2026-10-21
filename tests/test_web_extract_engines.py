"""``web_extract`` 的抓取通道链：DeepSeek 官方抓取、Jina、直接抓取与配置化顺序。

覆盖：``deepseek`` 请求形状与解析、``jina`` 的 Reader URL / 请求头、``direct`` 的
HTML 清洗、通道级失败如何转成 :class:`~atri_x.tool.builtin.web_extract_engines.ExtractEngineFailure`
并降级、密钥回落口径（``DEEPSEEK_SEARCH_KEY`` → ``DEEPSEEK_API_KEY``）、
``[web_extract]`` 配置解析、以及按通道分别缓存。
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

from atri_x.errors import InvalidError
from atri_x.net.httpclient import HTTPError
from atri_x.processenv import Environment
from atri_x.tool.builtin.web_extract import WebExtractTool, new_web_extract_tool
from atri_x.tool.builtin.web_extract_engines import (
    DEEPSEEK_DEFAULT_ENDPOINT,
    JINA_DEFAULT_ENDPOINT,
    PROTOCOL_DEEPSEEK,
    PROTOCOL_DIRECT,
    PROTOCOL_JINA,
    ExtractEngineFailure,
    ExtractEngineSpec,
    default_specs,
    extract_deepseek,
    extract_direct,
    extract_jina,
    key_env_names,
    specs_from_config,
)
from atri_x.tool.tool import CallRequest

_ENV_KEYS = ("DEEPSEEK_SEARCH_KEY", "DEEPSEEK_API_KEY", "JINA_API_KEY")


def _call(arguments: dict) -> CallRequest:
    return CallRequest(id="1", name="web_extract", arguments=json.dumps(arguments))


def _run(coro):
    return asyncio.run(coro)


class _Response:
    """对齐 :class:`atri_x.net.httpclient.Response` 的字段/属性。"""

    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status = status
        self.body = text.encode("utf-8")
        self._payload = payload
        self._text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return self._payload

    def text(self) -> str:
        return self._text


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


class DeepSeekFetchCase(unittest.TestCase):
    def test_request_and_parse(self) -> None:
        client = _RoutingClient(
            {
                DEEPSEEK_DEFAULT_ENDPOINT: _Response(
                    200, {"title": "T", "content": "# Title\n\nbody"}
                )
            }
        )
        page = _run(
            extract_deepseek(
                client,
                "dk",
                "https://example.com/a",
                timeout=12.0,
            )
        )
        self.assertEqual(page.source, PROTOCOL_DEEPSEEK)
        self.assertEqual(page.title, "T")
        self.assertEqual(page.content, "# Title\n\nbody")
        self.assertEqual(page.url, "https://example.com/a")
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("POST", DEEPSEEK_DEFAULT_ENDPOINT))
        self.assertEqual(kw["headers"]["Authorization"], "Bearer dk")
        self.assertEqual(kw["json"], {"url": "https://example.com/a"})
        self.assertEqual(kw["timeout"], 12.0)

    def test_custom_base_url(self) -> None:
        endpoint = "https://relay.example.com/v1/tools/fetch"
        client = _RoutingClient({endpoint: _Response(200, {"title": "", "content": "x"})})
        _run(extract_deepseek(client, "k", "https://a", timeout=15.0, endpoint=endpoint))
        self.assertEqual(client.calls[0][1], endpoint)

    def test_missing_key_is_engine_failure(self) -> None:
        with self.assertRaises(ExtractEngineFailure) as ctx:
            _run(extract_deepseek(_RoutingClient({}), "", "https://a", timeout=15.0))
        self.assertIn("DEEPSEEK_SEARCH_KEY", str(ctx.exception))

    def test_http_404_is_engine_failure(self) -> None:
        client = _RoutingClient({DEEPSEEK_DEFAULT_ENDPOINT: _Response(404, {}, "no page")})
        with self.assertRaises(ExtractEngineFailure) as ctx:
            _run(extract_deepseek(client, "k", "https://a", timeout=15.0))
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("no page", str(ctx.exception))

    def test_empty_content_is_engine_failure(self) -> None:
        client = _RoutingClient({DEEPSEEK_DEFAULT_ENDPOINT: _Response(200, {"content": ""})})
        with self.assertRaises(ExtractEngineFailure) as ctx:
            _run(extract_deepseek(client, "k", "https://a", timeout=15.0))
        self.assertIn("没有返回正文", str(ctx.exception))

    def test_transport_error_is_engine_failure(self) -> None:
        client = _RoutingClient({DEEPSEEK_DEFAULT_ENDPOINT: HTTPError("boom")})
        with self.assertRaises(ExtractEngineFailure) as ctx:
            _run(extract_deepseek(client, "k", "https://a", timeout=15.0))
        self.assertIn("请求失败", str(ctx.exception))


class JinaFetchCase(unittest.TestCase):
    def test_reader_url_and_headers(self) -> None:
        reader = JINA_DEFAULT_ENDPOINT.rstrip("/") + "/http://a.com/p"
        client = _RoutingClient(
            {reader: _Response(200, {"data": {"title": "JT", "content": "jc"}})}
        )
        page = _run(
            extract_jina(
                client,
                "jk",
                "http://a.com/p",
                "header",
                timeout=15.0,
            )
        )
        self.assertEqual(page.source, PROTOCOL_JINA)
        self.assertEqual(page.title, "JT")
        self.assertEqual(page.content, "jc")
        method, path, kw = client.calls[0]
        self.assertEqual((method, path), ("GET", reader))
        self.assertEqual(kw["headers"]["Authorization"], "Bearer jk")
        self.assertEqual(kw["headers"]["X-Remove-Selector"], "header")

    def test_missing_key_is_engine_failure(self) -> None:
        with self.assertRaises(ExtractEngineFailure):
            _run(extract_jina(_RoutingClient({}), "", "https://a", "header", timeout=15.0))


class DirectFetchCase(unittest.TestCase):
    def test_html_is_cleaned(self) -> None:
        client = _RoutingClient(
            {
                "https://a": _Response(
                    200, None, "<html><title>T</title><body><p>hi <b>there</b></p></body></html>"
                )
            }
        )
        page = _run(extract_direct(client, "https://a", timeout=15.0))
        self.assertEqual(page.source, PROTOCOL_DIRECT)
        self.assertEqual(page.title, "T")
        self.assertIn("hi", page.content)
        self.assertNotIn("<b>", page.content)


class SpecsAndKeysCase(unittest.TestCase):
    class _Engine:
        def __init__(self, protocol: str = "", api_key_env: str = "", base_url: str = "") -> None:
            self.protocol = protocol
            self.api_key_env = api_key_env
            self.base_url = base_url

    class _Config:
        def __init__(self, order: list, engines: dict) -> None:
            self.engine_order = order
            self.engines = engines

    def test_default_specs_order(self) -> None:
        self.assertEqual(
            [spec.protocol for spec in default_specs()],
            [PROTOCOL_JINA, PROTOCOL_DIRECT],
        )

    def test_absent_config_is_empty(self) -> None:
        self.assertEqual(specs_from_config(None), [])

    def test_builtin_name_infers_protocol(self) -> None:
        specs = specs_from_config(self._Config(["direct"], {"direct": self._Engine()}))
        self.assertEqual(specs[0].protocol, PROTOCOL_DIRECT)
        self.assertEqual(specs[0].key_envs(), ())

    def test_custom_name_needs_protocol(self) -> None:
        specs = specs_from_config(
            self._Config(
                ["mine", "unknown"],
                {
                    "mine": self._Engine(protocol="deepseek", base_url="https://relay"),
                    "unknown": self._Engine(),
                },
            )
        )
        self.assertEqual([spec.name for spec in specs], ["mine"])
        self.assertEqual(specs[0].base_url, "https://relay")

    def test_deepseek_key_fallback(self) -> None:
        self.assertEqual(
            key_env_names(PROTOCOL_DEEPSEEK),
            ("DEEPSEEK_SEARCH_KEY", "DEEPSEEK_API_KEY"),
        )
        self.assertEqual(
            key_env_names(PROTOCOL_DEEPSEEK, "DEEPSEEK_SEARCH_KEY"),
            ("DEEPSEEK_SEARCH_KEY", "DEEPSEEK_API_KEY"),
        )
        self.assertEqual(
            key_env_names(PROTOCOL_DEEPSEEK, "MY_DEEPSEEK_KEY"), ("MY_DEEPSEEK_KEY",)
        )
        self.assertEqual(key_env_names(PROTOCOL_JINA), ("JINA_API_KEY",))
        self.assertEqual(key_env_names(PROTOCOL_DIRECT), ())


class ToolChainCase(unittest.TestCase):
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

    def test_deepseek_only_uses_official_key(self) -> None:
        client = _RoutingClient(
            {DEEPSEEK_DEFAULT_ENDPOINT: _Response(200, {"title": "T", "content": "body"})}
        )
        specs = [ExtractEngineSpec(name="deepseek", protocol=PROTOCOL_DEEPSEEK)]
        tool = WebExtractTool(Environment(["DEEPSEEK_SEARCH_KEY=sk-1"]), client=client, specs=specs)
        result = _run(tool.call(None, _call({"url": "https://a"})))
        self.assertIn("Source: deepseek", result.content)
        self.assertEqual(client.calls[0][2]["headers"]["Authorization"], "Bearer sk-1")

    def test_fallback_key_env_is_used(self) -> None:
        client = _RoutingClient(
            {DEEPSEEK_DEFAULT_ENDPOINT: _Response(200, {"title": "", "content": "body"})}
        )
        specs = [ExtractEngineSpec(name="deepseek", protocol=PROTOCOL_DEEPSEEK)]
        tool = WebExtractTool(Environment(["DEEPSEEK_API_KEY=sk-2"]), client=client, specs=specs)
        _run(tool.call(None, _call({"url": "https://a"})))
        self.assertEqual(client.calls[0][2]["headers"]["Authorization"], "Bearer sk-2")

    def test_failed_first_engine_falls_back(self) -> None:
        client = _RoutingClient(
            {
                DEEPSEEK_DEFAULT_ENDPOINT: _Response(404, {}, "no page"),
                "https://a": _Response(200, None, "<title>D</title><p>hi</p>"),
            }
        )
        specs = [
            ExtractEngineSpec(name="deepseek", protocol=PROTOCOL_DEEPSEEK),
            ExtractEngineSpec(name="direct", protocol=PROTOCOL_DIRECT),
        ]
        tool = WebExtractTool(Environment(["DEEPSEEK_SEARCH_KEY=k"]), client=client, specs=specs)
        result = _run(tool.call(None, _call({"url": "https://a"})))
        self.assertIn("Source: direct", result.content)
        self.assertEqual([call[1] for call in client.calls], [DEEPSEEK_DEFAULT_ENDPOINT, "https://a"])

    def test_no_available_channel(self) -> None:
        specs = [ExtractEngineSpec(name="deepseek", protocol=PROTOCOL_DEEPSEEK)]
        tool = WebExtractTool(client=_RoutingClient({}), specs=specs)
        with self.assertRaises(InvalidError) as ctx:
            _run(tool.call(None, _call({"url": "https://a"})))
        self.assertIn("没有可用抓取通道", str(ctx.exception))

    def test_jina_false_skips_jina(self) -> None:
        reader = JINA_DEFAULT_ENDPOINT.rstrip("/") + "/http://a.com/p"
        client = _RoutingClient({"https://a.com/p": _Response(200, None, "<p>hi</p>")})
        specs = [
            ExtractEngineSpec(name="jina", protocol=PROTOCOL_JINA),
            ExtractEngineSpec(name="direct", protocol=PROTOCOL_DIRECT),
        ]
        tool = WebExtractTool(Environment(["JINA_API_KEY=jk"]), client=client, specs=specs)
        result = _run(tool.call(None, _call({"url": "https://a.com/p", "jina": False})))
        self.assertIn("Source: direct", result.content)
        self.assertEqual([call[1] for call in client.calls], ["https://a.com/p"])

    def test_cache_is_per_engine(self) -> None:
        client = _RoutingClient(
            {DEEPSEEK_DEFAULT_ENDPOINT: _Response(200, {"title": "", "content": "body"})}
        )
        specs = [ExtractEngineSpec(name="deepseek", protocol=PROTOCOL_DEEPSEEK)]
        tool = WebExtractTool(Environment(["DEEPSEEK_SEARCH_KEY=k"]), client=client, specs=specs)
        _run(tool.call(None, _call({"url": "https://a"})))
        second = _run(tool.call(None, _call({"url": "https://a"})))
        self.assertEqual(len(client.calls), 1)
        self.assertIn("Cached: true", second.content)

    def test_new_tool_accepts_specs(self) -> None:
        tool = new_web_extract_tool(
            Environment(), specs=[ExtractEngineSpec(name="direct", protocol=PROTOCOL_DIRECT)]
        )
        self.assertEqual([spec.protocol for spec in tool.specs], [PROTOCOL_DIRECT])


if __name__ == "__main__":
    unittest.main()
