"""``long_memory`` 三件套的单元测试。

对应 Go 测试：``internal/tool/builtin/long_memory_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.errors import InvalidError
from atri_x.tool.builtin.long_memory import (
    LONG_MEMORY_SEARCH_TOOL_NAME,
    LONG_MEMORY_TOOL_NAME,
    LONG_MEMORY_WRITE_TOOL_NAME,
    new_long_memory_tools,
)
from atri_x.tool.builtin.long_memory_model import (
    LongMemoryRecord,
    LongMemoryUpdateArgs,
    marshal_long_memory_file,
    normalize_long_memory_limit,
    normalize_long_memory_match_mode,
    parse_long_memory_text,
    slug_long_memory_title,
    split_long_memory_keywords,
    tokenize_long_memory,
    validate_long_memory_record,
)
from atri_x.tool.builtin.long_memory_store import (
    LongMemoryStore,
    append_long_memory_change,
    long_memory_inline_value,
)
from atri_x.tool.tool import CallRequest

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_long_memory_test")


def _run(coro):
    return asyncio.run(coro)


def _req(name: str, arguments: dict) -> CallRequest:
    return CallRequest(id="1", name=name, arguments=json.dumps(arguments))


class LongMemoryTextTest(unittest.TestCase):
    def test_tokenize_mixes_ascii_and_cjk(self) -> None:
        self.assertEqual(tokenize_long_memory("Hello 世界"), "hello 世 世界 界")

    def test_split_keywords(self) -> None:
        self.assertEqual(
            split_long_memory_keywords("a, b，c;d\ne\tf a"),
            ["a", "b", "c", "d", "e", "f"],
        )

    def test_slug(self) -> None:
        self.assertEqual(slug_long_memory_title("Hello World!"), "Hello-World")
        self.assertEqual(slug_long_memory_title("中文 标题"), "中文-标题")
        self.assertEqual(slug_long_memory_title("!!!"), "memory")
        self.assertEqual(len(slug_long_memory_title("a" * 100)), 40)

    def test_normalize(self) -> None:
        self.assertEqual(normalize_long_memory_limit(0), 3)
        self.assertEqual(normalize_long_memory_limit(99), 10)
        self.assertEqual(normalize_long_memory_limit(2), 2)
        self.assertEqual(normalize_long_memory_match_mode(" and "), "AND")
        self.assertEqual(normalize_long_memory_match_mode(""), "OR")

    def test_inline_value_and_change(self) -> None:
        self.assertEqual(long_memory_inline_value("  "), "(空)")
        self.assertEqual(append_long_memory_change([], "标题", "a", "a"), [])
        self.assertEqual(
            append_long_memory_change([], "标题", "a", "b"), ["标题：a -> b"]
        )


class LongMemoryFrontMatterTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        record = LongMemoryRecord(
            id=7,
            category="宠物",
            title='她说"你好"',
            summary="摘要",
            keywords="猫 狗",
            content="第一行\n第二行",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        data = marshal_long_memory_file(record)
        parsed = parse_long_memory_text(data.decode("utf-8"))
        self.assertEqual(parsed.meta.id, 7)
        self.assertEqual(parsed.meta.category, "宠物")
        self.assertEqual(parsed.meta.title, '她说"你好"')
        self.assertEqual(parsed.meta.keywords, ["猫", "狗"])
        self.assertEqual(parsed.content, "第一行\n第二行")

    def test_requires_leading_separator(self) -> None:
        with self.assertRaises(InvalidError):
            parse_long_memory_text("id = 1")

    def test_requires_closing_separator(self) -> None:
        with self.assertRaises(InvalidError):
            parse_long_memory_text("+++\nid = 1\n")

    def test_invalid_toml(self) -> None:
        with self.assertRaises(InvalidError):
            parse_long_memory_text("+++\n= = =\n+++\n\nbody")

    def test_validate_required_fields(self) -> None:
        record = LongMemoryRecord(
            id=1,
            category="c",
            title="t",
            summary="s",
            content="x",
            created_at="a",
            updated_at="b",
        )
        validate_long_memory_record(record, True)
        record.content = ""
        with self.assertRaises(InvalidError):
            validate_long_memory_record(record, True)
        validate_long_memory_record(record, False)
        record.id = 0
        with self.assertRaises(InvalidError):
            validate_long_memory_record(record, False)


class LongMemoryToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tools = new_long_memory_tools(TMP_ROOT)
        self.entry, self.search, self.write = self.tools

    def tearDown(self) -> None:
        for tool in self.tools:
            store = getattr(tool, "store", None)
            if store is not None:
                store.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_names_and_visibility(self) -> None:
        self.assertEqual(self.entry.name(), LONG_MEMORY_TOOL_NAME)
        self.assertEqual(self.search.name(), LONG_MEMORY_SEARCH_TOOL_NAME)
        self.assertEqual(self.write.name(), LONG_MEMORY_WRITE_TOOL_NAME)
        self.assertFalse(self.entry.info().hidden)
        self.assertTrue(self.search.info().hidden)
        self.assertTrue(self.write.info().hidden)
        self.assertTrue(self.entry.info().superadmin_only)
        self.assertEqual(self.entry.info().depends_on, ["long_memory_search", "long_memory_write"])
        self.assertEqual(self.write.info().risk, "high")

    def test_entry_reply(self) -> None:
        result = _run(self.entry.call(None, CallRequest()))
        self.assertIn("long_memory_search", result.content)

    def test_schema_has_content_edits(self) -> None:
        schema = self.write.schema()
        properties = schema.function.parameters["properties"]
        self.assertIn("content_edits", properties)
        self.assertEqual(
            schema.function.parameters["required"], ["operation"]
        )

    def test_empty_library_categories(self) -> None:
        result = _run(self.search.call(None, CallRequest()))
        self.assertIn("长期记忆库为空", result.content)

    def test_save_search_update_delete(self) -> None:
        saved = _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "宠物",
                        "title": "小明的猫",
                        "summary": "一只橘猫",
                        "keywords": "猫, 橘",
                        "content": "第一行\n第二行",
                    },
                )
            )
        )
        self.assertIn("记忆写入成功", saved.content)
        self.assertTrue(os.path.exists(os.path.join(TMP_ROOT, "memories")))

        searched = _run(
            self.search.call(
                None, _req(LONG_MEMORY_SEARCH_TOOL_NAME, {"keywords": "橘猫"})
            )
        )
        self.assertIn("成功检索到", searched.content)
        self.assertIn("小明的猫", searched.content)
        self.assertNotIn("第一行", searched.content)

        detailed = _run(
            self.search.call(
                None,
                _req(
                    LONG_MEMORY_SEARCH_TOOL_NAME,
                    {"keywords": "橘", "brief_only": False},
                ),
            )
        )
        self.assertIn("1 | 第一行", detailed.content)

        updated = _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {"operation": "update", "id": 1, "title": "小明的橘猫"},
                ),
            )
        )
        self.assertIn("更新成功", updated.content)

        deleted = _run(
            self.write.call(
                None,
                _req(LONG_MEMORY_WRITE_TOOL_NAME, {"operation": "delete", "id": 1}),
            )
        )
        self.assertIn("已永久删除", deleted.content)
        self.assertEqual(os.listdir(os.path.join(TMP_ROOT, "memories")), [])

    def test_save_requires_fields(self) -> None:
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None,
                    _req(LONG_MEMORY_WRITE_TOOL_NAME, {"operation": "save", "title": "t"}),
                )
            )

    def test_unknown_operation(self) -> None:
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None, _req(LONG_MEMORY_WRITE_TOOL_NAME, {"operation": "nope"})
                )
            )

    def test_update_missing_id(self) -> None:
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None,
                    _req(LONG_MEMORY_WRITE_TOOL_NAME, {"operation": "delete", "id": 9}),
                )
            )

    def test_update_conflicts_and_empty_change(self) -> None:
        _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "c",
                        "title": "t",
                        "summary": "s",
                        "content": "body",
                    },
                ),
            )
        )
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None,
                    _req(
                        LONG_MEMORY_WRITE_TOOL_NAME,
                        {
                            "operation": "update",
                            "id": 1,
                            "content": "x",
                            "content_edits": [
                                {"operation": "append", "new_text": "y"}
                            ],
                        },
                    ),
                )
            )
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None, _req(LONG_MEMORY_WRITE_TOOL_NAME, {"operation": "update", "id": 1})
                )
            )
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None,
                    _req(
                        LONG_MEMORY_WRITE_TOOL_NAME,
                        {"operation": "update", "id": 1, "title": "t"},
                    ),
                )
            )

    def test_content_edits_update(self) -> None:
        _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "c",
                        "title": "t",
                        "summary": "s",
                        "content": "alpha\nbeta",
                    },
                ),
            )
        )
        result = _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "update",
                        "id": 1,
                        "content_edits": [
                            {
                                "operation": "replace_text",
                                "old_text": "beta",
                                "new_text": "gamma",
                            }
                        ],
                    },
                ),
            )
        )
        self.assertIn("更新成功", result.content)
        detailed = _run(
            self.search.call(
                None,
                _req(LONG_MEMORY_SEARCH_TOOL_NAME, {"keywords": "gamma", "brief_only": False}),
            )
        )
        self.assertIn("gamma", detailed.content)

    def test_risk_detail_and_preflight(self) -> None:
        _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "c",
                        "title": "t",
                        "summary": "s",
                        "content": "alpha",
                    },
                ),
            )
        )
        update = _req(
            LONG_MEMORY_WRITE_TOOL_NAME,
            {"operation": "update", "id": 1, "summary": "新摘要"},
        )
        _run(self.write.preflight_confirmation(None, update))
        detail = _run(self.write.risk_detail(None, update))
        self.assertIn("操作：更新长期记忆", detail)
        self.assertIn("字段变化：", detail)
        self.assertIn("摘要：s -> 新摘要", detail)
        self.assertIn("预检 diff:", detail)
        non_update = _req(LONG_MEMORY_WRITE_TOOL_NAME, {"operation": "save"})
        self.assertEqual(_run(self.write.risk_detail(None, non_update)), "")
        _run(self.write.preflight_confirmation(None, non_update))

    def test_search_requires_filter_or_category(self) -> None:
        store = LongMemoryStore(TMP_ROOT)
        with self.assertRaises(InvalidError):
            store.search(None, _search_args(keywords="", category=""))
        store.close()

    def test_broken_file_is_reported_and_not_overwritten(self) -> None:
        memories = os.path.join(TMP_ROOT, "memories")
        os.makedirs(memories, exist_ok=True)
        broken = os.path.join(memories, "000001-broken.md")
        with open(broken, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("no front matter here\n")
        result = _run(self.search.call(None, CallRequest()))
        self.assertIn("长期记忆格式警告", result.content)
        self.assertIn("broken.md", result.content)
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    None,
                    _req(
                        LONG_MEMORY_WRITE_TOOL_NAME,
                        {"operation": "update", "id": 1, "title": "x"},
                    ),
                )
            )

    def test_stale_index_entries_are_removed(self) -> None:
        _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "c",
                        "title": "t",
                        "summary": "s",
                        "content": "body",
                    },
                ),
            )
        )
        store = self.write.store
        path = store._record_by_id(1).file_path
        os.remove(path)
        store._sync(None, True)
        self.assertIsNone(store._record_by_id(1))

    def test_duplicate_id_is_rejected(self) -> None:
        memories = os.path.join(TMP_ROOT, "memories")
        os.makedirs(memories, exist_ok=True)
        record = LongMemoryRecord(
            id=5,
            category="c",
            title="t",
            summary="s",
            content="body",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        for name in ("000005-a.md", "000005-b.md"):
            with open(os.path.join(memories, name), "wb") as handle:
                handle.write(marshal_long_memory_file(record))
        store = LongMemoryStore(TMP_ROOT)
        store._ensure(None)
        store._sync(None, True)
        invalids = store._invalid_files(5)
        self.assertEqual(len(invalids), 1)
        self.assertIn("重复", invalids[0].error)
        store.close()

    def test_like_fallback_when_fts_unavailable(self) -> None:
        _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "c",
                        "title": "标题",
                        "summary": "摘要",
                        "content": "正文关键词",
                    },
                ),
            )
        )
        store = self.write.store
        store.fts_available = False
        content = store.search(None, _search_args(keywords="正文关键词"))
        self.assertIn("成功检索到", content)
        self.assertIn("标题：标题", content)
        self.assertIn("摘要：摘要", content)
        # 默认 brief_only=true，正文不出现。
        self.assertNotIn("正文关键词", content)
        detailed = store.search(
            None, _search_args(keywords="正文关键词", brief_only=False)
        )
        self.assertIn("正文关键词", detailed)
        self.assertEqual(store.search(None, _search_args(keywords="不存在")), 
                         "全库分类概览\n-c（共1条记录）\n" + "=" * 30 +
                         "\n长期记忆库中未找到相关内容。建议检查关键词是否过于严格，尝试 match_mode=OR，或参考分类概览调整分类。")

    def test_and_match_mode(self) -> None:
        store = LongMemoryStore(TMP_ROOT)
        _run(
            self.write.call(
                None,
                _req(
                    LONG_MEMORY_WRITE_TOOL_NAME,
                    {
                        "operation": "save",
                        "category": "c",
                        "title": "t",
                        "summary": "s",
                        "content": "alpha beta",
                    },
                ),
            )
        )
        store2 = self.write.store
        store2.fts_available = False
        hit = store2.search(None, _search_args(keywords="alpha beta", match_mode="AND"))
        self.assertIn("成功检索到", hit)
        miss = store2.search(
            None, _search_args(keywords="alpha gamma", match_mode="AND")
        )
        self.assertIn("未找到相关内容", miss)
        store.close()

    def test_preview_locked_requires_store(self) -> None:
        from atri_x.tool.builtin.long_memory import LongMemorySearchTool

        tool = LongMemorySearchTool(None)
        with self.assertRaises(InvalidError):
            _run(tool.call(None, CallRequest()))


def _search_args(**kwargs):
    from atri_x.tool.builtin.long_memory_model import LongMemorySearchArgs

    return LongMemorySearchArgs(**kwargs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
