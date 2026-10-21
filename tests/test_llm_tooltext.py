"""``atri_x.llm.tooltext``：文本形态工具调用的判定、还原与清洗。

夹具直接取自真实事故（2026-10-21 线上会话 ``atrix_sessions.db`` 里 assistant 正文），
四种形态分别对应「正常 DSML」「两批 web_search」「``</`` 被打碎」「``parameter name=`` 被吞」。
"""

import json
import unittest

from atri_x.llm.tooltext import (
    TEXT_TOOL_CALL_NOTICE,
    looks_like_text_tool_call,
    parse_text_tool_calls,
    recover_text_tool_calls,
    strip_text_tool_calls,
)


def _dsml(*body: str) -> str:
    return "\n".join(body)


#: 真实事故 1：discover_tool，数组参数 ``string="false"``。
LEAK_DISCOVER = _dsml(
    "<｜｜DSML｜｜ calls>",
    '<｜｜DSML｜｜ invoke name="discover_tool">',
    '<｜｜DSML｜｜ parameter name="names" string="false">'
    '["web_search", "web_extract", "view_image", "angel_remember"]'
    "</｜｜DSML｜｜ parameter>",
    "</｜｜DSML｜｜ invoke>",
    "</｜｜DSML｜｜ calls>",
)

#: 真实事故 2：一批 web_search，中间一条 ``invoke`` 的 name 丢了。
LEAK_BATCH = _dsml(
    "<｜｜DSML｜｜ calls>",
    '<｜｜DSML｜｜ invoke name="web_search">',
    '<｜｜DSML｜｜ parameter name="query" string="true">2026年最新游戏显卡型号 NVIDIA AMD 新品'
    "</｜｜DSML｜｜ parameter>",
    "<｜｜DSML｜｜ invoke>",
    '<｜｜DSML｜｜ invoke name="web_search">',
    '<｜｜DSML｜｜ parameter name="query" string="true">最新显卡 RTX 50系列 发布时间 性能评测'
    "</｜｜DSML｜｜ parameter>",
    "</｜｜DSML｜｜ invoke>",
    "</｜｜DSML｜｜ calls>",
)

#: 真实事故 3：最常见形态（两条并行查询）。
LEAK_PARALLEL = _dsml(
    "<｜｜DSML｜｜ calls>",
    '<｜｜DSML｜｜ invoke name="web_search">',
    '<｜｜DSML｜｜ parameter name="query" string="true">最新游戏显卡 2026 型号</｜｜DSML｜｜ parameter>',
    "</｜｜DSML｜｜ invoke>",
    '<｜｜DSML｜｜ invoke name="web_search">',
    '<｜｜DSML｜｜ parameter name="query" string="true">latest gaming graphics cards 2026 NVIDIA AMD'
    "</｜｜DSML｜｜ parameter>",
    "</｜｜DSML｜｜ invoke>",
    "</｜｜DSML｜｜ calls>",
)

#: 真实事故 4：``</`` 被打成 ``<｜place▁holder▁no▁73｜>``、``parameter name=`` 被吞、外层标签缺失。
LEAK_CORRUPTED = _dsml(
    '<｜｜DSML｜｜ invoke name="web_search">',
    '<｜｜DSML｜｜ query" string="true">今日新闻 2026年10月21日 热点</｜｜DSML｜｜>',
    "<｜place▁holder▁no▁73｜>invoke>",
    '<｜place▁holder▁no▁73｜>invoke name="web_search">',
    '<｜place▁holder▁no▁73｜>query" string="true">today top news October 21 2026</｜>',
    "<｜place▁holder▁no▁73｜>invoke>",
    "</｜｜DSML｜｜>",
)

ALL_LEAKS = (LEAK_DISCOVER, LEAK_BATCH, LEAK_PARALLEL, LEAK_CORRUPTED)


class TestDetection(unittest.TestCase):
    def test_real_leaks_are_detected(self) -> None:
        for leaked in ALL_LEAKS:
            self.assertTrue(looks_like_text_tool_call(leaked))

    def test_plain_answer_is_not_detected(self) -> None:
        plain = "现在（2026年10月）市面上能买到的最新游戏显卡仍是 2025 年发布的那两代。"
        self.assertFalse(looks_like_text_tool_call(plain))
        self.assertEqual(recover_text_tool_calls(plain), ([], plain))

    def test_words_alone_are_not_detected(self) -> None:
        # 只有 invoke / parameter 这些英文单词不该误判成协议标记。
        self.assertFalse(looks_like_text_tool_call("请 invoke 一下这个函数，parameter 传 1"))

    def test_empty_text(self) -> None:
        self.assertFalse(looks_like_text_tool_call(""))
        self.assertEqual(recover_text_tool_calls(""), ([], ""))


class TestRecoverMarkup(unittest.TestCase):
    def test_discover_tool_array_argument(self) -> None:
        calls, cleaned = recover_text_tool_calls(LEAK_DISCOVER)
        self.assertEqual([call.name for call in calls], ["discover_tool"])
        self.assertEqual(
            json.loads(calls[0].arguments),
            {"names": ["web_search", "web_extract", "view_image", "angel_remember"]},
        )
        self.assertEqual(cleaned, "")

    def test_parallel_web_search(self) -> None:
        calls, cleaned = recover_text_tool_calls(LEAK_PARALLEL)
        self.assertEqual([call.name for call in calls], ["web_search", "web_search"])
        self.assertEqual(json.loads(calls[0].arguments), {"query": "最新游戏显卡 2026 型号"})
        self.assertEqual(
            json.loads(calls[1].arguments),
            {"query": "latest gaming graphics cards 2026 NVIDIA AMD"},
        )
        self.assertEqual(cleaned, "")

    def test_batch_tolerates_invoke_without_name(self) -> None:
        calls, _cleaned = recover_text_tool_calls(LEAK_BATCH)
        self.assertEqual([call.name for call in calls], ["web_search", "web_search"])
        self.assertIn("RTX 50", json.loads(calls[1].arguments)["query"])

    def test_corrupted_tokens_still_recover(self) -> None:
        calls, cleaned = recover_text_tool_calls(LEAK_CORRUPTED)
        self.assertEqual([call.name for call in calls], ["web_search", "web_search"])
        self.assertEqual(json.loads(calls[0].arguments), {"query": "今日新闻 2026年10月21日 热点"})
        self.assertEqual(
            json.loads(calls[1].arguments), {"query": "today top news October 21 2026"}
        )
        self.assertEqual(cleaned, "")

    def test_calls_carry_no_id_yet(self) -> None:
        calls, _cleaned = recover_text_tool_calls(LEAK_PARALLEL)
        # id 由接线层（chat_anomaly）补，解析层保持纯函数、只产出真实信息。
        self.assertEqual([call.id for call in calls], ["", ""])

    def test_mixed_text_keeps_natural_language(self) -> None:
        mixed = "我先查一下。" + LEAK_PARALLEL
        calls, cleaned = recover_text_tool_calls(mixed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(cleaned, "我先查一下。")

    def test_function_calls_wrapper(self) -> None:
        text = (
            "<function_calls>"
            '<invoke name="web_search">'
            '<parameter name="query" string="true">最新显卡</parameter>'
            "</invoke>"
            "</function_calls>"
        )
        calls, cleaned = recover_text_tool_calls(text)
        self.assertEqual([call.name for call in calls], ["web_search"])
        self.assertEqual(json.loads(calls[0].arguments), {"query": "最新显卡"})
        self.assertEqual(cleaned, "")


class TestRecoverJson(unittest.TestCase):
    def test_single_object(self) -> None:
        text = '<tool-call>{"name": "web_search", "arguments": {"query": "机器人 新闻"}}</tool-call>'
        calls, cleaned = recover_text_tool_calls(text)
        self.assertEqual([call.name for call in calls], ["web_search"])
        self.assertEqual(json.loads(calls[0].arguments), {"query": "机器人 新闻"})
        self.assertEqual(cleaned, "")

    def test_array_payload(self) -> None:
        text = (
            "<tool-call>"
            '[{"name": "web_search", "arguments": {"query": "a"}},'
            ' {"name": "web_extract", "arguments": {"url": "https://example.com"}}]'
            "</tool-call>"
        )
        calls, _cleaned = recover_text_tool_calls(text)
        self.assertEqual([call.name for call in calls], ["web_search", "web_extract"])
        self.assertEqual(json.loads(calls[0].arguments), {"query": "a"})
        self.assertEqual(json.loads(calls[1].arguments), {"url": "https://example.com"})

    def test_arguments_as_json_string(self) -> None:
        text = '<tool_call>{"name": "web_search", "arguments": "{\\"query\\": \\"x\\"}"}</tool_call>'
        calls, _cleaned = recover_text_tool_calls(text)
        self.assertEqual(json.loads(calls[0].arguments), {"query": "x"})

    def test_missing_closing_tag(self) -> None:
        text = '<tool-call>{"name": "web_search", "arguments": {"query": "x"}}'
        calls, cleaned = recover_text_tool_calls(text)
        self.assertEqual([call.name for call in calls], ["web_search"])
        self.assertEqual(cleaned, "")

    def test_bad_json_is_not_a_call(self) -> None:
        text = "<tool-call>{not json at all}</tool-call>"
        calls, cleaned = recover_text_tool_calls(text)
        self.assertEqual(calls, [])
        self.assertEqual(cleaned, "")


class TestStrip(unittest.TestCase):
    def test_strip_keeps_prefix(self) -> None:
        text = "好的，我查一下。" + LEAK_PARALLEL
        self.assertEqual(strip_text_tool_calls(text), "好的，我查一下。")

    def test_strip_leaves_plain_text_untouched(self) -> None:
        plain = "<b>你好</b> 这不是工具调用"
        self.assertEqual(strip_text_tool_calls(plain), plain)

    def test_notice_text_is_defined(self) -> None:
        self.assertTrue(TEXT_TOOL_CALL_NOTICE)
        self.assertNotIn("｜DSML｜", TEXT_TOOL_CALL_NOTICE)


class TestParseEntryPoint(unittest.TestCase):
    def test_parse_returns_calls_only(self) -> None:
        calls = parse_text_tool_calls(LEAK_PARALLEL)
        self.assertEqual([call.name for call in calls], ["web_search", "web_search"])

    def test_parse_plain_returns_empty(self) -> None:
        self.assertEqual(parse_text_tool_calls("普通回答"), [])


if __name__ == "__main__":
    unittest.main()
