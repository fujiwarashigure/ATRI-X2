"""聊天漫画的取材测试（P0-2）。

要钉住的行为：

* 只从**当前会话**取消息（假仓储会记录请求参数，跨会话的请求根本发不出去）；
* 锚点不存在时**报错**，不悄悄退回"最近几条"；
* 取最近 N 条时要的是**最近**而不是最早的 N 条（真实仓储 ``list_range`` 是 ASC，
  用它当"最近聊天"会画成几天前的对话）；
* 消息保留稳定 ID，供计划引用。
"""

from __future__ import annotations

import unittest

from atri_x.comic import (
    MAX_CONTEXT_MESSAGES,
    ComicContextError,
    collect_comic_context,
    format_context_material,
)
from tests._comic_fakes import FakeHistory, build_context, make_rows


class CollectContextTest(unittest.TestCase):
    def test_recent_messages_take_the_tail(self) -> None:
        history = FakeHistory(make_rows([f"消息{i}" for i in range(1, 11)]))
        context = collect_comic_context(history, build_context(), limit=3)
        self.assertEqual(
            [item.text for item in context.messages], ["消息8", "消息9", "消息10"]
        )
        self.assertEqual(context.message_ids()[0], "msg-8")
        self.assertTrue(context.messages[0].message_id.startswith("msg-"))

    def test_scope_comes_from_message_context_only(self) -> None:
        rows = [
            *make_rows(["本群消息"], platform="qqonebot", scope_id="group-1"),
            *make_rows(["别的群消息"], platform="qqonebot", scope_id="group-2", start_seq=10),
        ]
        history = FakeHistory(rows)
        context = collect_comic_context(history, build_context(), limit=10)
        self.assertEqual([item.text for item in context.messages], ["本群消息"])
        self.assertEqual(context.scope_id, "group-1")
        request = history.requests[-1]
        self.assertEqual(request.platform_scope_id, "group-1")

    def test_anchor_window_is_used(self) -> None:
        history = FakeHistory(make_rows([f"消息{i}" for i in range(1, 7)]))
        context = collect_comic_context(
            history, build_context(), anchor_message_id="msg-3", limit=5
        )
        self.assertEqual(context.anchor_message_id, "msg-3")
        self.assertIn("消息3", [item.text for item in context.messages])
        request = history.requests[-1]
        self.assertEqual(request.platform_message_id, "msg-3")

    def test_missing_anchor_is_an_error(self) -> None:
        history = FakeHistory(make_rows(["消息1"]))
        with self.assertRaises(ComicContextError) as caught:
            collect_comic_context(history, build_context(), anchor_message_id="msg-404")
        self.assertIn("锚点", str(caught.exception))

    def test_query_search_keeps_terms(self) -> None:
        history = FakeHistory(make_rows(["今天吃火锅", "明天考试", "火锅真好吃"]))
        context = collect_comic_context(history, build_context(), query="火锅")
        self.assertEqual(
            [item.text for item in context.messages], ["今天吃火锅", "火锅真好吃"]
        )
        request = history.requests[-1]
        self.assertIn("火锅", request.query_terms)

    def test_empty_query_result_is_an_error(self) -> None:
        history = FakeHistory(make_rows(["今天吃火锅"]))
        with self.assertRaises(ComicContextError):
            collect_comic_context(history, build_context(), query="完全不存在的关键词")

    def test_stop_word_only_query_is_rejected(self) -> None:
        history = FakeHistory(make_rows(["随便"]))
        with self.assertRaises(ComicContextError) as caught:
            collect_comic_context(history, build_context(), query="刚才 那群 聊天")
        self.assertIn("关键词", str(caught.exception))

    def test_limit_is_clamped(self) -> None:
        history = FakeHistory(make_rows([f"消息{i}" for i in range(1, 50)]))
        context = collect_comic_context(history, build_context(), limit=999)
        self.assertLessEqual(len(context.messages), MAX_CONTEXT_MESSAGES)
        self.assertTrue(context.truncated)

    def test_missing_history_is_an_error(self) -> None:
        with self.assertRaises(ComicContextError):
            collect_comic_context(None, build_context())

    def test_missing_platform_route_is_an_error(self) -> None:
        history = FakeHistory(make_rows(["消息1"]))
        with self.assertRaises(ComicContextError):
            collect_comic_context(history, None)

    def test_media_only_message_keeps_a_placeholder(self) -> None:
        rows = make_rows([""])
        rows[0].segments = '[{"type":"image","media":"media:%s"}]' % ("a" * 64)
        history = FakeHistory(rows)
        context = collect_comic_context(history, build_context())
        self.assertEqual(context.messages[0].text, "[图片消息]")
        self.assertTrue(context.messages[0].has_media)

    def test_duplicate_ids_are_deduped_and_ordered(self) -> None:
        rows = make_rows(["第一条", "第二条".strip(), "第一条"])
        history = FakeHistory(rows)
        context = collect_comic_context(history, build_context(), limit=10)
        ids = context.message_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item.seq for item in context.messages], sorted(item.seq for item in context.messages))

    def test_format_material_carries_message_ids(self) -> None:
        history = FakeHistory(make_rows(["今天吃火锅", "明天考试"]))
        context = collect_comic_context(history, build_context())
        text = format_context_material(context)
        self.assertIn("[聊天素材]", text)
        self.assertIn("[msg-1]", text)
        self.assertIn("小明", text)

    def test_format_material_truncates_with_count(self) -> None:
        history = FakeHistory(make_rows([f"消息{i}" for i in range(1, 6)]))
        context = collect_comic_context(history, build_context(), limit=5)
        text = format_context_material(context, max_runes=10)
        self.assertIn("未展开", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
