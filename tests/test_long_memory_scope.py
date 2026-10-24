"""Long Memory「按会话隔离 + 超管跨会话检索」的行为契约测试。

对应评估文档 §A2 / 决策 4（long_memory 定位：按 scope 隔离 + 超管可跨 scope 检索）。

覆盖三类行为：

* **归属**：新记忆写文件与索引时都带 ``source_scope`` / ``source_actor``；
* **隔离**：检索 / 分类目录 / 更新 / 删除默认只看本会话与未归属的旧数据；
* **跨会话**：仅超级管理员，且必须显式传 ``scope``；非超管即使调用也拒绝。
"""

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from atri_x.errors import InvalidError
from atri_x.platform import MessageContext, with_message_context
from atri_x.security import ACTOR_CONTEXT_KEY, Actor, RoleSuperadmin, RoleUser
from atri_x.tool.builtin.long_memory import new_long_memory_tools
from atri_x.tool.builtin.long_memory_model import (
    LongMemoryRecord,
    LongMemorySaveArgs,
    LongMemorySearchArgs,
    LongMemoryUpdateArgs,
    marshal_long_memory_file,
    parse_long_memory_text,
)
from atri_x.tool.builtin.long_memory_scope import (
    GLOBAL_SCOPE,
    SCOPE_ALL,
    check_record_access,
    current_scope_key,
    is_superadmin,
    resolve_scope_filter,
    scope_allows,
    scope_key,
    scope_label,
    scope_origin_line,
)
from atri_x.tool.builtin.long_memory_store import LongMemoryStore
from atri_x.tool.tool import CallRequest

TMP_ROOT = os.path.join(os.getcwd(), "tmp_long_memory_scope_test")


def _run(coro):
    return asyncio.run(coro)


def _req(arguments: dict, name: str = "long_memory_write") -> CallRequest:
    return CallRequest(id="1", name=name, arguments=json.dumps(arguments))


def _ctx(
    scope_id: str,
    role: str = RoleSuperadmin,
    actor_id: str = "qq_onebot:10000",
    platform: str = "qq_onebot",
):
    """造一个带 Actor + 会话作用域的 ctx（真实的工具调用形状）。"""
    actor = Actor(
        id=actor_id, platform=platform, platform_user_id="10000", role=role
    )
    msg = MessageContext(platform=platform, scope_id=scope_id)
    return with_message_context({ACTOR_CONTEXT_KEY: actor}, msg)


class LongMemoryScopeHelperTest(unittest.TestCase):
    def test_scope_key_join(self) -> None:
        self.assertEqual(scope_key("qq_onebot", "group:1"), "qq_onebot:group:1")
        self.assertEqual(scope_key("", "group:1"), "group:1")
        self.assertEqual(scope_key("qq_onebot", ""), GLOBAL_SCOPE)

    def test_current_scope_key(self) -> None:
        self.assertEqual(current_scope_key(_ctx("group:1")), "qq_onebot:group:1")
        self.assertEqual(current_scope_key(None), GLOBAL_SCOPE)

    def test_is_superadmin(self) -> None:
        self.assertTrue(is_superadmin(_ctx("group:1")))
        self.assertFalse(is_superadmin(_ctx("group:1", role=RoleUser)))
        self.assertFalse(is_superadmin(None))

    def test_resolve_scope_filter_current(self) -> None:
        self.assertEqual(resolve_scope_filter(_ctx("group:1"), ""), ("qq_onebot:group:1", False))
        self.assertEqual(
            resolve_scope_filter(_ctx("group:1"), "current"),
            ("qq_onebot:group:1", False),
        )

    def test_resolve_scope_filter_all_and_explicit(self) -> None:
        self.assertEqual(resolve_scope_filter(_ctx("group:1"), SCOPE_ALL), (GLOBAL_SCOPE, True))
        self.assertEqual(
            resolve_scope_filter(_ctx("group:1"), "qq_onebot:group:2"),
            ("qq_onebot:group:2", True),
        )

    def test_resolve_scope_filter_requires_superadmin(self) -> None:
        ctx = _ctx("group:1", role=RoleUser)
        with self.assertRaises(InvalidError) as caught:
            resolve_scope_filter(ctx, SCOPE_ALL)
        self.assertIn("超级管理员", str(caught.exception))

    def test_scope_allows_matrix(self) -> None:
        self.assertTrue(scope_allows("a:1", GLOBAL_SCOPE, True))
        self.assertFalse(scope_allows("a:1", "a:2", True))
        self.assertTrue(scope_allows("a:1", GLOBAL_SCOPE, False))
        self.assertTrue(scope_allows(GLOBAL_SCOPE, "a:2", False))
        self.assertTrue(scope_allows("a:1", "a:1", False))
        self.assertFalse(scope_allows("a:1", "a:2", False))

    def test_scope_label_and_origin_line(self) -> None:
        self.assertEqual(scope_label(GLOBAL_SCOPE), "全局（旧数据）")
        self.assertEqual(scope_label("qq_onebot:group:1"), "group:1（qq_onebot）")
        self.assertEqual(scope_origin_line("qq_onebot:group:1", False), "")
        self.assertIn("来源会话", scope_origin_line("qq_onebot:group:1", True))

    def test_check_record_access(self) -> None:
        ctx = _ctx("group:1")
        check_record_access(ctx, "qq_onebot:group:1")
        check_record_access(ctx, GLOBAL_SCOPE)
        check_record_access(ctx, "qq_onebot:group:2", "qq_onebot:group:2")
        with self.assertRaises(InvalidError) as caught:
            check_record_access(ctx, "qq_onebot:group:2")
        self.assertIn("scope=qq_onebot:group:2", str(caught.exception))

    def test_check_record_access_rejects_user_even_with_scope(self) -> None:
        ctx = _ctx("qq_onebot:group:1", role=RoleUser)
        with self.assertRaises(InvalidError):
            check_record_access(ctx, "qq_onebot:group:2", "qq_onebot:group:2")


class LongMemoryScopeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="lm_scope_", dir=TMP_ROOT)
        self.store = LongMemoryStore(self.root)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _save(self, ctx, title: str, content: str = "正文") -> LongMemoryRecord:
        return self.store.save(
            ctx,
            LongMemorySaveArgs(
                category="测试",
                title=title,
                summary=f"{title}的摘要",
                keywords=title,
                content=content,
            ),
        )

    def _search(self, ctx, keywords: str, scope: str = "") -> str:
        return self.store.search(
            ctx, LongMemorySearchArgs(keywords=keywords, brief_only=True, limit=5, scope=scope)
        )

    def test_save_records_source_scope_and_actor(self) -> None:
        record = self._save(_ctx("group:1"), "群一记忆")
        self.assertEqual(record.source_scope, "qq_onebot:group:1")
        self.assertEqual(record.source_actor, "qq_onebot:10000")
        with open(record.file_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('source_scope = "qq_onebot:group:1"', text)
        self.assertIn('source_actor = "qq_onebot:10000"', text)

    def test_save_without_scope_stays_global(self) -> None:
        record = self._save(None, "全局记忆")
        self.assertEqual(record.source_scope, GLOBAL_SCOPE)
        self.assertEqual(record.source_actor, "")

    def test_search_isolated_between_scopes(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        self.assertIn("群一秘密", self._search(_ctx("group:1"), "群一秘密"))
        self.assertIn("未找到相关内容", self._search(_ctx("group:2"), "群一秘密"))
        self.assertIn("未找到相关内容", self._search(_ctx("private:10000"), "群一秘密"))

    def test_unscoped_search_sees_everything(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        self._save(_ctx("group:2"), "群二秘密")
        text = self._search(None, "秘密")
        self.assertIn("群一秘密", text)
        self.assertIn("群二秘密", text)

    def test_legacy_global_record_visible_everywhere(self) -> None:
        legacy = self._save(None, "旧全局记忆")
        self.assertIn("旧全局记忆", self._search(_ctx("group:1"), "旧全局记忆"))
        self.assertIn("旧全局记忆", self._search(_ctx("group:2"), "旧全局记忆"))
        self.assertEqual(legacy.source_scope, GLOBAL_SCOPE)

    def test_cross_scope_search_all_by_superadmin(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        self._save(_ctx("group:2"), "群二秘密")
        text = self._search(_ctx("group:1"), "秘密", SCOPE_ALL)
        self.assertIn("群一秘密", text)
        self.assertIn("群二秘密", text)
        self.assertIn("来源会话", text)
        self.assertIn("group:2（qq_onebot）", text)

    def test_cross_scope_search_specific_scope(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        self._save(_ctx("group:2"), "群二秘密")
        text = self._search(_ctx("group:1"), "秘密", "qq_onebot:group:2")
        self.assertNotIn("群一秘密", text)
        self.assertIn("群二秘密", text)
        self.assertIn("group:2（qq_onebot）", text)

    def test_cross_scope_search_rejected_for_user(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        with self.assertRaises(InvalidError):
            self._search(_ctx("group:1", role=RoleUser), "秘密", SCOPE_ALL)

    def test_default_search_output_has_no_origin_line(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        self.assertNotIn("来源会话", self._search(_ctx("group:1"), "群一秘密"))

    def test_cross_scope_update_requires_explicit_scope(self) -> None:
        record = self._save(_ctx("group:1"), "群一秘密")
        ctx = _ctx("group:2")
        with self.assertRaises(InvalidError) as caught:
            self.store.update(
                ctx, LongMemoryUpdateArgs(id=record.id, summary="新摘要")
            )
        self.assertIn("scope=qq_onebot:group:1", str(caught.exception))
        updated = self.store.update(
            ctx,
            LongMemoryUpdateArgs(
                id=record.id, summary="新摘要", scope="qq_onebot:group:1"
            ),
        )
        self.assertEqual(updated.summary, "新摘要")
        self.assertEqual(updated.source_scope, "qq_onebot:group:1")

    def test_cross_scope_delete_requires_explicit_scope(self) -> None:
        record = self._save(_ctx("group:1"), "群一秘密")
        ctx = _ctx("group:2")
        with self.assertRaises(InvalidError):
            self.store.delete(ctx, record.id)
        path = self.store.delete(ctx, record.id, "qq_onebot:group:1")
        self.assertFalse(os.path.exists(path))

    def test_cross_scope_mutation_rejected_for_user(self) -> None:
        record = self._save(_ctx("group:1"), "群一秘密")
        ctx = _ctx("group:2", role=RoleUser)
        with self.assertRaises(InvalidError):
            self.store.delete(ctx, record.id, "qq_onebot:group:1")

    def test_categories_header_is_scoped(self) -> None:
        self._save(_ctx("group:1"), "群一记忆")
        scoped = self.store.categories(_ctx("group:1"))
        self.assertIn("测试", scoped)
        self.assertIn("共1条记录", scoped)
        other = self.store.categories(_ctx("group:2"))
        self.assertIn("长期记忆库为空", other)

    def test_categories_cross_scope_header(self) -> None:
        self._save(_ctx("group:1"), "群一记忆")
        cross = self.store.categories(_ctx("group:1"), SCOPE_ALL)
        self.assertIn("共1条记录", cross)
        self.assertNotIn("长期记忆库为空", cross)

    def test_like_fallback_respects_scope(self) -> None:
        self._save(_ctx("group:1"), "群一秘密")
        self.store._ensure(None)
        self.store.fts_available = False
        self.assertIn("未找到相关内容", self._search(_ctx("group:2"), "群一秘密"))
        self.assertIn("群一秘密", self._search(_ctx("group:1"), "群一秘密"))

    def test_legacy_index_is_migrated(self) -> None:
        root = os.path.join(self.root, "legacy")
        os.makedirs(root, exist_ok=True)
        db = sqlite3.connect(os.path.join(root, "index.db"))
        db.execute(
            """CREATE TABLE memory_files (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                keywords TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                file_mtime_ns INTEGER NOT NULL,
                file_size INTEGER NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE memory_invalid_files (
                file_path TEXT PRIMARY KEY,
                error TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                file_mtime_ns INTEGER NOT NULL,
                file_size INTEGER NOT NULL
            )"""
        )
        db.execute(
            "INSERT INTO memory_files VALUES "
            "(1, 'memories/000001-old.md', 'c', 't', 's', 'k', '', '', 0, 0)"
        )
        db.commit()
        db.close()
        legacy = LongMemoryStore(root)
        try:
            legacy._ensure(None)
            columns = {
                row[1] for row in legacy.db.execute("PRAGMA table_info(memory_files)")
            }
            self.assertIn("source_scope", columns)
            self.assertIn("source_actor", columns)
            row = legacy.db.execute(
                "SELECT source_scope, source_actor FROM memory_files WHERE id = 1"
            ).fetchone()
            self.assertEqual(tuple(row), ("", ""))
        finally:
            legacy.close()

    def test_front_matter_roundtrip_keeps_source(self) -> None:
        record = LongMemoryRecord(
            id=7,
            file_path="x",
            category="c",
            title="t",
            summary="s",
            keywords="k",
            content="正文",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source_scope="qq_onebot:group:1",
            source_actor="qq_onebot:10000",
        )
        parsed = parse_long_memory_text(marshal_long_memory_file(record).decode("utf-8"))
        self.assertEqual(parsed.meta.source_scope, "qq_onebot:group:1")
        self.assertEqual(parsed.meta.source_actor, "qq_onebot:10000")

    def test_legacy_record_serializes_without_source_keys(self) -> None:
        record = LongMemoryRecord(
            id=8,
            file_path="x",
            category="c",
            title="t",
            summary="s",
            keywords="k",
            content="正文",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        text = marshal_long_memory_file(record).decode("utf-8")
        self.assertNotIn("source_scope", text)
        self.assertNotIn("source_actor", text)


class LongMemoryScopeToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="lm_scope_tool_")
        self.tools = new_long_memory_tools(self.root)
        self.entry, self.search, self.write = self.tools

    def tearDown(self) -> None:
        for tool in self.tools:
            store = getattr(tool, "store", None)
            if store is not None:
                store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _save(self, ctx, title: str) -> str:
        result = _run(
            self.write.call(
                ctx,
                _req(
                    {
                        "operation": "save",
                        "category": "测试",
                        "title": title,
                        "summary": f"{title}摘要",
                        "keywords": title,
                        "content": "正文",
                    }
                ),
            )
        )
        return result.content

    def test_tool_save_and_search_use_session_scope(self) -> None:
        content = self._save(_ctx("group:1"), "群一记忆")
        self.assertIn("归属会话：group:1（qq_onebot）", content)
        hit = _run(
            self.search.call(
                _ctx("group:1"), _req({"keywords": "群一记忆"}, "long_memory_search")
            )
        )
        self.assertIn("成功检索到", hit.content)
        miss = _run(
            self.search.call(
                _ctx("group:2"), _req({"keywords": "群一记忆"}, "long_memory_search")
            )
        )
        self.assertIn("未找到相关内容", miss.content)

    def test_tool_search_cross_scope_needs_scope_arg(self) -> None:
        self._save(_ctx("group:1"), "群一记忆")
        cross = _run(
            self.search.call(
                _ctx("group:2"),
                _req({"keywords": "群一记忆", "scope": "all"}, "long_memory_search"),
            )
        )
        self.assertIn("群一记忆", cross.content)
        self.assertIn("来源会话", cross.content)

    def test_tool_delete_across_scope(self) -> None:
        content = self._save(_ctx("group:1"), "群一记忆")
        memory_id = int(content.split("ID：")[1].split("\n")[0])
        with self.assertRaises(InvalidError):
            _run(
                self.write.call(
                    _ctx("group:2"),
                    _req({"operation": "delete", "id": memory_id}),
                )
            )
        result = _run(
            self.write.call(
                _ctx("group:2"),
                _req(
                    {
                        "operation": "delete",
                        "id": memory_id,
                        "scope": "qq_onebot:group:1",
                    }
                ),
            )
        )
        self.assertIn("已永久删除", result.content)


if __name__ == "__main__":
    unittest.main()
