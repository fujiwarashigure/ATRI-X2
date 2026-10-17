"""``atri_x.llm.protocol`` 与 ``atri_x.llm.origin`` 测试。

对应 Go：``internal/llm/protocol_test.go``（保守默认值与查询转发）与
``internal/llm/origin_test.go``（``CanSwitch`` 判定矩阵，逐条对齐）。
"""

from __future__ import annotations

import unittest

from atri_x.llm.origin import Origin, OriginError, can_switch
from atri_x.llm.protocol import (
    ProtocolCapabilities,
    ProtocolChatCompletions,
    ProtocolResponses,
    protocol_capabilities_of,
)


class _CapabilityFake:
    """实现了可选查询接口的适配器（记录被问到的 model）。"""

    def __init__(self, caps: ProtocolCapabilities) -> None:
        self.caps = caps
        self.model = ""

    def chat_stream(self, ctx: object, req: object) -> object:
        return None

    async def list_models(self, ctx: object) -> list[str]:
        return []

    def protocol_capabilities_for(self, model: str) -> ProtocolCapabilities:
        self.model = model
        return self.caps


class _PlainFake:
    """没实现查询接口的适配器。"""

    def chat_stream(self, ctx: object, req: object) -> object:
        return None

    async def list_models(self, ctx: object) -> list[str]:
        return []


class TestProtocolCapabilitiesOf(unittest.TestCase):
    def test_falls_back_to_chat_completions(self) -> None:
        # 没实现查询接口的适配器必须得到最保守的答案，而不是零值（零值会让
        # Protocol 变成空字符串，让「哪套协议」这个问题没有答案）。
        caps = protocol_capabilities_of(_PlainFake(), "m")
        self.assertEqual(caps.protocol, ProtocolChatCompletions)
        self.assertFalse(caps.server_side_conversation)
        self.assertFalse(caps.native_compaction)
        self.assertFalse(caps.incremental_tools)
        self.assertFalse(caps.server_side_store)

    def test_forwards_model_and_capabilities(self) -> None:
        client = _CapabilityFake(
            ProtocolCapabilities(protocol=ProtocolResponses, native_compaction=True)
        )
        caps = protocol_capabilities_of(client, "resp-model")
        self.assertEqual(client.model, "resp-model")
        self.assertEqual(caps.protocol, ProtocolResponses)
        self.assertTrue(caps.native_compaction)

    def test_fills_empty_protocol(self) -> None:
        # 报告者为空协议时补上最弱协议：空 Protocol 会让分派点的默认分支失效。
        caps = protocol_capabilities_of(
            _CapabilityFake(ProtocolCapabilities(native_compaction=True)), "m"
        )
        self.assertEqual(caps.protocol, ProtocolChatCompletions)


class TestOriginCanSwitch(unittest.TestCase):
    def test_material_compatibility_matrix(self) -> None:
        chat = ProtocolChatCompletions
        responses = ProtocolResponses
        cases = [
            ("new session", Origin(), Origin(responses, "a"), True),
            ("chat vendors", Origin(chat, "a"), Origin(chat, "b"), True),
            ("migrated chat", Origin(chat), Origin(chat, "b"), True),
            ("same vendor", Origin(responses, "a"), Origin(responses, "a"), True),
            ("response vendors", Origin(responses, "a"), Origin(responses, "b"), False),
            ("chat to response", Origin(chat, "a"), Origin(responses, "a"), False),
            ("response to chat", Origin(responses, "a"), Origin(chat, "a"), False),
            ("missing response vendor", Origin(responses), Origin(responses, "a"), False),
            ("unknown target", Origin(), Origin(), False),
            ("target without provider", Origin(), Origin(chat), False),
        ]
        for name, source, target, allowed in cases:
            with self.subTest(name=name):
                self.assertEqual(_allowed(source, target), allowed)

    def test_rejection_message_names_both_sides(self) -> None:
        with self.assertRaises(OriginError) as caught:
            can_switch(Origin(ProtocolResponses, "a"), Origin(ProtocolResponses, "b"))
        message = str(caught.exception)
        self.assertIn("a/responses", message)
        self.assertIn("b/responses", message)
        self.assertIn("start a new session", message)

    def test_missing_source_provider_is_reported_as_unknown(self) -> None:
        with self.assertRaises(OriginError) as caught:
            can_switch(Origin(ProtocolResponses), Origin(ProtocolResponses, "b"))
        self.assertIn("unknown/responses", str(caught.exception))

    def test_target_configuration_error_is_distinct(self) -> None:
        with self.assertRaises(OriginError) as caught:
            can_switch(Origin(), Origin())
        self.assertIn("missing protocol or provider", str(caught.exception))

    def test_origin_dataclass_helpers(self) -> None:
        self.assertTrue(Origin(ProtocolResponses, "a"))
        self.assertFalse(Origin())
        self.assertEqual(Origin.from_dict({"protocol": "responses", "provider": "a"}),
                         Origin(ProtocolResponses, "a"))
        self.assertEqual(Origin.from_dict(None), Origin())
        self.assertEqual(Origin(ProtocolResponses, "a").to_dict(),
                         {"protocol": "responses", "provider": "a"})


def _allowed(source: Origin, target: Origin) -> bool:
    try:
        can_switch(source, target)
    except OriginError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
