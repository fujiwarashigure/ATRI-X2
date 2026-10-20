"""``atri_x.agent.title`` 的命名请求参数契约。

对应上游 ``internal/agent/title_test.go``（命名提示词），另加一条本仓的
模型兼容性回归（见 :class:`TitleGeneratorModelCompatTests`）。
"""

from __future__ import annotations

import unittest

from atri_x.agent.title import TitleGenerator, title_prompt
from atri_x.llm.llm import StreamChunk
from atri_x.storage.models import Message, RoleAssistant, RoleUser


class _FakeClient:
    """记录最近一次请求的 ``chat_stream`` 假客户端。"""

    def __init__(self, text: str = "生成的标题") -> None:
        self.text = text
        self.requests: list = []

    def chat_stream(self, ctx, req):
        self.requests.append(req)

        async def gen():
            yield StreamChunk(delta_content=self.text)

        return gen()


class TitlePromptTests(unittest.TestCase):
    def test_title_prompt_uses_user_and_assistant_content(self) -> None:
        messages = [
            Message(session_id="s", role=RoleUser, content="你好"),
            Message(session_id="s", role=RoleAssistant, content="在的"),
            Message(session_id="s", role=RoleUser, content="   "),
        ]
        text = title_prompt(messages)
        self.assertIn("用户：你好", text)
        self.assertIn("助手：在的", text)
        self.assertNotIn("   ", text)


class TitleGeneratorModelCompatTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_request_has_room_for_model_preamble(self) -> None:
        """回归：命名请求的 ``max_tokens`` 不能小到被模型正文前的输出吃光。

        真机实测 opencode-go/deepseek-v4.1-flash 在 ``max_tokens=32``（Go 侧的
        取值）时返回 ``finish_reason=length`` 且 ``delta_content`` 为空，自动命名
        必然以 ``invalid title`` 失败；放宽后才能拿到标题。这里锁住下限。
        """
        client = _FakeClient("查询今日天气")
        generator = TitleGenerator(
            naming=client, naming_model="deepseek-v4.1-flash", naming_source="opencode-go"
        )
        result = await generator.generate_title(None, [])
        self.assertEqual("查询今日天气", result.raw_title)
        self.assertEqual(1, len(client.requests))
        self.assertGreaterEqual(client.requests[0].max_tokens, 128)

    async def test_naming_model_failure_falls_back_to_primary(self) -> None:
        class _Boom:
            def chat_stream(self, ctx, req):
                async def gen():
                    raise RuntimeError("naming model down")
                    yield  # pragma: no cover

                return gen()

        primary = _FakeClient("兜底标题")
        generator = TitleGenerator(
            primary=primary, primary_model="main", naming=_Boom(), naming_model="naming"
        )
        result = await generator.generate_title(None, [])
        self.assertEqual("兜底标题", result.raw_title)
