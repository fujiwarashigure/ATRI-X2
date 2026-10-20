"""Provider 级自定义请求头（``[providers.<name>].extra_headers``）的传递与过滤。

背景：OpenCode Go 之类的中转要求客户端带专有头（``x-opencode-session``），
只配 ``base_url`` / ``api_key_env`` 会被上游 400 拒绝。这里覆盖三件事：

1. 配置层把 ``extra_headers`` 解码进 ``ProviderConfig``（见 ``test_config.py``）；
2. ``new_provider_llm`` 把它交给 chat / responses 两个适配器；
3. 适配器构造请求头时剔除保留头与 CRLF。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from atri_x.app.models import new_provider_llm
from atri_x.llm.openai.openai import RESERVED_HEADER_NAMES, clean_extra_headers
from atri_x.llm.openai.responses import ResponsesAdapter
from atri_x.llm.openai.wire import RequestOptions


def fake_provider(**overrides):
    base = {
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "api_key": "sk-test",
        "extra_payload": None,
        "model_configs": {},
        "extra_headers": {"x-opencode-session": "atri-x-local"},
        "uses_responses_api": lambda: False,
        "api_protocol_for": lambda _model: None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCleanExtraHeaders(unittest.TestCase):
    def test_keeps_custom_headers(self) -> None:
        self.assertEqual(
            clean_extra_headers({"x-opencode-session": "abc", "X-Trace": "on"}),
            {"x-opencode-session": "abc", "X-Trace": "on"},
        )

    def test_drops_reserved_headers(self) -> None:
        for name in RESERVED_HEADER_NAMES:
            self.assertEqual(clean_extra_headers({name: "x"}), {}, name)
            self.assertEqual(clean_extra_headers({name.title(): "x"}), {}, name)

    def test_drops_line_breaks_and_invalid_names(self) -> None:
        self.assertEqual(clean_extra_headers({"x-bad": "a\r\nb"}), {})
        self.assertEqual(clean_extra_headers({"bad name": "v"}), {})
        self.assertEqual(clean_extra_headers({"bad:name": "v"}), {})
        self.assertEqual(clean_extra_headers({"": "v"}), {})


class TestProviderHeaderWiring(unittest.TestCase):
    def test_chat_client_carries_provider_headers(self) -> None:
        client = new_provider_llm("go", fake_provider(), RequestOptions())
        headers = client._headers(json_body=True)
        self.assertEqual(headers["x-opencode-session"], "atri-x-local")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_responses_client_carries_provider_headers(self) -> None:
        provider = fake_provider(uses_responses_api=lambda: True)
        router = new_provider_llm("go", provider, RequestOptions())
        responses = router.responses
        self.assertIsInstance(responses, ResponsesAdapter)
        self.assertEqual(responses._headers()["x-opencode-session"], "atri-x-local")

    def test_reserved_header_is_ignored_at_runtime(self) -> None:
        provider = fake_provider(extra_headers={"Authorization": "Bearer forged"})
        client = new_provider_llm("go", provider, RequestOptions())
        self.assertEqual(client._headers()["Authorization"], "Bearer sk-test")
        self.assertNotIn("Authorization", client.extra_headers)


if __name__ == "__main__":
    unittest.main()
