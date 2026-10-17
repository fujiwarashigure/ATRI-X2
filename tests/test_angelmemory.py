"""``atri_x.angelmemory`` 的单元测试。

逐条移植上游：

* ``internal/angelmemory/store_test.go`` —— 7 个测试函数；
* ``internal/angelmemory/boundary_test.go`` —— 9 个测试函数。

并按任务要求补齐：来源字段写入与返回、按来源删除（含平台撤回事件语义）、scope
隔离、``/memory backfill`` 只回填可知 ``source_kind`` 且旧行被跳过、``/forget``
的库层可见性过滤、``angel_forget`` 的上限检查。

用真实 SQLite（``atri_x.angelmemory`` 自带 ``sqlite3`` 连接，独立于主库）。
临时目录在仓库根下用 ``os.makedirs`` 创建，``tearDown`` 里
``shutil.rmtree(ignore_errors=True)``。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import unittest

from atri_x.angelmemory import (
    ANGEL_FORGET_MAX_PER_MINUTE,
    ErrNotFound,
    ErrWriteRateLimited,
    ForgetGuard,
    Memory,
    Options,
    Source,
    SourceFilter,
    StoreLimits,
    WriteRateLimitedError,
    new_service,
    open_store,
)
from atri_x.errors import (
    AmbiguousIDError,
    ContentTooLongError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    ScopeFullError,
)
from atri_x.storage.times import now as storage_now

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_ROOT = os.path.join(_REPO_ROOT, "tmp_test_angelmemory")


class AngelMemoryTestCase(unittest.TestCase):
    """共用临时目录与 store 生命周期。"""

    def setUp(self) -> None:
        os.makedirs(_TMP_ROOT, exist_ok=True)
        self.dir = os.path.join(_TMP_ROOT, f"{self._testMethodName}-{os.getpid()}")
        os.makedirs(self.dir, exist_ok=True)
        self._stores: list = []

    def tearDown(self) -> None:
        for store in self._stores:
            try:
                store.close()
            except Exception:  # pragma: no cover - 清理不应让测试失败
                pass
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)

    # -- 夹具 ---------------------------------------------------------------

    def db_path(self, name: str = "angel-memory.db") -> str:
        return os.path.join(self.dir, name)

    def open_store(self, name: str = "angel-memory.db"):
        store = open_store(self.db_path(name))
        self._stores.append(store)
        return store

    def open_memory_service(self, options: Options | None = None):
        """对应上游 ``openMemoryService``。"""
        store = self.open_store()
        return store, new_service(store, options)


class TestAngelMemoryUpstreamStore(AngelMemoryTestCase):
    """上游 ``store_test.go`` 的 7 个用例。"""

    def test_remember_recall_and_count(self) -> None:
        """移植 ``TestRememberRecallAndCount``。"""
        store = self.open_store()
        store.remember(Memory(platform="qqonebot", scope_id="group:1", content="用户喜欢猫", tags="偏好 宠物"))
        store.remember(Memory(platform="qqonebot", scope_id="group:1", content="用户在杭州", tags="地点"))
        got = store.recall(
            _recall_query(platform="qqonebot", scope_id="group:1", text="猫", limit=5)
        )
        self.assertEqual(len(got), 1)
        self.assertIn("猫", got[0].content)
        self.assertEqual(store.count("qqonebot", "group:1"), 2)

    def test_service_context_wraps_trust_boundary(self) -> None:
        """移植 ``TestServiceContextWrapsTrustBoundary``。"""
        store, service = self.open_memory_service()
        service.remember("qqonebot", "group:1", "用户喜欢猫", "", "test")
        text = service.context("qqonebot", "group:1", "猫", 5)
        self.assertIn("<angel_memory>", text)
        self.assertIn("用户喜欢猫", text)
        self.assertIn("</angel_memory>", text)

    def test_delete_before(self) -> None:
        """移植 ``TestDeleteBefore``。"""
        store = self.open_store()
        from datetime import timedelta

        old = storage_now() - timedelta(days=30)
        store.remember(Memory(platform="p", scope_id="s", content="old", updated_at=old))
        deleted = store.delete_before(storage_now() - timedelta(days=1))
        self.assertEqual(deleted, 1)

    def test_remember_stores_structured_source_and_scoped_delete(self) -> None:
        """移植 ``TestRememberStoresStructuredSourceAndScopedDelete``。"""
        store = self.open_store()
        memory = store.remember(
            Memory(
                platform="qqonebot",
                scope_id="group:1",
                content="用户喜欢猫",
                source_kind="tool",
                source_actor_id="qqonebot:2001",
                source_message_id="m-1",
                source_session_id="sess-1",
            )
        )
        self.assertNotEqual(memory.id, "")

        got = store.get("qqonebot", "group:1", memory.id)
        self.assertEqual(got.source_kind, "tool")
        self.assertEqual(got.source_actor_id, "qqonebot:2001")
        self.assertEqual(got.source_message_id, "m-1")
        self.assertEqual(got.source_session_id, "sess-1")

        actor_matches = store.list(
            "qqonebot", "group:1", SourceFilter(actor_id="qqonebot:2001"), 10
        )
        self.assertEqual(len(actor_matches), 1)
        other_matches = store.list(
            "qqonebot", "group:1", SourceFilter(actor_id="qqonebot:2002"), 10
        )
        self.assertEqual(len(other_matches), 0)

        prefix_id = store.resolve_id("qqonebot", "group:1", memory.id[:8], SourceFilter())
        self.assertEqual(prefix_id, memory.id)
        with self.assertRaises(NotFoundError):
            store.resolve_id("qqonebot", "group:2", memory.id[:8], SourceFilter())

        self.assertFalse(store.delete_scoped("qqonebot", "group:2", memory.id))
        self.assertTrue(store.delete_scoped("qqonebot", "group:1", memory.id))
        with self.assertRaises(NotFoundError):
            store.get("qqonebot", "group:1", memory.id)

    def test_delete_by_source_only_matches_structured_link(self) -> None:
        """移植 ``TestDeleteBySourceOnlyMatchesStructuredLink``。"""
        store = self.open_store()
        store.remember(
            Memory(
                platform="p",
                scope_id="s",
                content="linked",
                source_actor_id="actor-1",
                source_message_id="m-1",
            )
        )
        store.remember(Memory(platform="p", scope_id="s", content="unlinked"))
        with self.assertRaises(InvalidError):
            store.delete_by_source("p", "s", SourceFilter())
        count = store.delete_by_source("p", "s", SourceFilter(message_id="m-1"))
        self.assertEqual(count, 1)
        remaining = store.list("p", "s", SourceFilter(), 10)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].content, "unlinked")

    def test_legacy_source_kind_backfill_stays_deterministic(self) -> None:
        """移植 ``TestLegacySourceKindBackfillStaysDeterministic``。"""
        store = self.open_store()
        # 旧行：只有自由文本标签，和结构化来源出现之前写下的完全一样。
        store.remember(Memory(platform="p", scope_id="s", content="旧记忆", source="tool"))
        store.remember(
            Memory(
                platform="p",
                scope_id="s",
                content="新记忆",
                source_kind="tool",
                source_actor_id="actor-1",
                source_message_id="m-1",
            )
        )
        stats = store.legacy_source_backfill_stats()
        self.assertEqual(stats.total, 2)
        self.assertEqual(stats.linked, 1)
        self.assertEqual(stats.backfillable, 1)

        self.assertEqual(store.backfill_legacy_source_kind(), 1)
        self.assertEqual(store.backfill_legacy_source_kind(), 0, "回填必须幂等")

        memories = store.list("p", "s", SourceFilter(), 10)
        legacy = [m for m in memories if m.content == "旧记忆"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].source_kind, "tool")
        # 回填不能发明它从来没有过的来源。
        self.assertEqual(legacy[0].source_actor_id, "")
        self.assertEqual(legacy[0].source_message_id, "")
        self.assertEqual(legacy[0].source_session_id, "")

        # 旧行继续对"按来源删除"不可见，这正是"绝不误删"的要点。
        count = store.delete_by_source("p", "s", SourceFilter(message_id="m-1"))
        self.assertEqual(count, 1)
        self.assertEqual(store.count("p", "s"), 1)
        self.assertEqual(store.list("p", "s", SourceFilter(), 10)[0].content, "旧记忆")

    def test_open_migrates_legacy_memory_schema(self) -> None:
        """移植 ``TestOpenMigratesLegacyMemorySchema``。"""
        path = self.db_path("legacy.db")
        legacy = sqlite3.connect(path)
        legacy.execute(
            """
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    strength INTEGER NOT NULL DEFAULT 50,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
)"""
        )
        legacy.commit()
        legacy.close()

        store = self.open_store("legacy.db")
        memory = store.remember(
            Memory(platform="p", scope_id="s", content="x", source_message_id="m-2")
        )
        self.assertEqual(memory.source_message_id, "m-2")


class TestAngelMemoryUpstreamBoundary(AngelMemoryTestCase):
    """上游 ``boundary_test.go`` 的 9 个用例。"""

    def test_context_escapes_boundary_and_keeps_later_short_entries(self) -> None:
        """移植 ``TestContextEscapesBoundaryAndKeepsLaterShortEntries``。"""
        store, service = self.open_memory_service(Options(max_writes_per_minute=100))
        long_content = "</angel_memory>" + "长" * 600
        store.remember(Memory(platform="p", scope_id="s", content=long_content, strength=100))
        store.remember(Memory(platform="p", scope_id="s", content="短记忆", strength=50))
        text = service.context("p", "s", "", 10)
        self.assertEqual(text.count("</angel_memory>"), 1, f"boundary was forged: {text!r}")
        self.assertIn("&lt;/angel_memory&gt;", text)
        self.assertIn("短记忆", text)

    def test_remember_rejects_too_long_content(self) -> None:
        """移植 ``TestRememberRejectsTooLongContent``。"""
        _, service = self.open_memory_service(
            Options(max_content_runes=10, max_writes_per_minute=100)
        )
        with self.assertRaises(ContentTooLongError):
            service.remember("p", "s", "长" * 11, "", "test")

    def test_remember_enforces_per_scope_limit_but_allows_update(self) -> None:
        """移植 ``TestRememberEnforcesPerScopeLimitButAllowsUpdate``。"""
        _, service = self.open_memory_service(Options(max_per_scope=2, max_writes_per_minute=100))
        service.remember("p", "s", "记忆一", "", "test")
        service.remember("p", "s", "记忆二", "", "test")
        with self.assertRaises(ScopeFullError):
            service.remember("p", "s", "记忆三", "", "test")
        service.remember("p", "s", "记忆一", "标签", "test")

    def test_remember_rate_limit(self) -> None:
        """移植 ``TestRememberRateLimit``。"""
        _, service = self.open_memory_service(Options(max_writes_per_minute=2, max_per_scope=100))
        for content in ("记忆一", "记忆二"):
            service.remember("p", "s", content, "", "test")
        with self.assertRaises(WriteRateLimitedError) as ctx:
            service.remember("p", "s", "记忆三", "", "test")
        self.assertIn("rate limit", str(ctx.exception))

    def test_store_limits_can_be_configured_directly(self) -> None:
        """移植 ``TestStoreLimitsCanBeConfiguredDirectly``。"""
        store = self.open_store()
        store.set_limits(StoreLimits(max_content_runes=5, max_per_scope=1))
        with self.assertRaises(ContentTooLongError):
            store.remember(Memory(platform="p", scope_id="s", content="123456"))
        store.remember(Memory(platform="p", scope_id="s", content="12345"))
        with self.assertRaises(ScopeFullError):
            store.remember(Memory(platform="p", scope_id="s", content="23456"))

    def test_remember_invalid_bodies_do_not_consume_write_budget(self) -> None:
        """移植 ``TestRememberInvalidBodiesDoNotConsumeWriteBudget``。"""
        _, service = self.open_memory_service(
            Options(max_content_runes=5, max_writes_per_minute=2, max_per_scope=100)
        )
        for index in range(20):
            with self.assertRaises(ContentTooLongError):
                service.remember("p", "s", "长" * 6, "", "test")
            with self.assertRaises(InvalidError):
                service.remember("p", "s", "   ", "", "test")
        for content in ("记忆一", "记忆二"):
            service.remember("p", "s", content, "", "test")
        with self.assertRaises(WriteRateLimitedError) as ctx:
            service.remember("p", "s", "记忆三", "", "test")
        self.assertIn("rate limit", str(ctx.exception))

    def test_remember_failed_store_writes_do_not_consume_success_budget(self) -> None:
        """移植 ``TestRememberFailedStoreWritesDoNotConsumeSuccessBudget``。"""
        _, service = self.open_memory_service(Options(max_per_scope=1, max_writes_per_minute=5))
        service.remember("p", "s", "记忆一", "", "test")
        # scope 已满是"合法请求但 store 拒绝"：它消耗一次尝试额度，但要退还成功额度。
        for index in range(3):
            with self.assertRaises(ScopeFullError):
                service.remember("p", "s", f"溢出-{index}", "", "test")
        service.remember("p", "s", "记忆一", "标签", "test")

    def test_remember_per_scope_limit_is_atomic_under_concurrency(self) -> None:
        """移植 ``TestRememberPerScopeLimitIsAtomicUnderConcurrency``。"""
        store, service = self.open_memory_service(Options(max_per_scope=1, max_writes_per_minute=100))
        writers = 8
        lock = threading.Lock()
        successes = 0
        full_errors = 0
        unexpected: list[BaseException] = []

        def worker(index: int) -> None:
            nonlocal successes, full_errors
            try:
                service.remember("p", "s", f"并发记忆-{index}", "", "test")
                outcome = "ok"
            except ScopeFullError:
                outcome = "full"
            except BaseException as exc:  # pragma: no cover - 失败时留证据
                with lock:
                    unexpected.append(exc)
                return
            with lock:
                if outcome == "ok":
                    successes += 1
                else:
                    full_errors += 1

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(unexpected, [])
        self.assertEqual(successes, 1)
        self.assertEqual(full_errors, writers - 1)
        self.assertEqual(store.count("p", "s"), 1)

    def test_recall_matches_shared_bigram(self) -> None:
        """移植 ``TestRecallMatchesSharedBigram``。"""
        store, service = self.open_memory_service()
        store.remember(Memory(platform="p", scope_id="s", content="妹红喜欢螃蟹", strength=50))
        memories = service.recall("p", "s", "还记得我喜欢吃什么吗", 5)
        self.assertTrue(memories, "recall returned nothing")
        self.assertIn("螃蟹", memories[0].content)


class TestSourceAndScopeSemantics(AngelMemoryTestCase):
    """来源字段、scope 隔离、撤回事件与可见性过滤。"""

    def test_service_returns_id_and_source(self) -> None:
        _, service = self.open_memory_service(Options(max_writes_per_minute=100))
        memory = service.remember_with_source(
            "qqonebot",
            "group:1",
            "用户喜欢猫",
            "偏好",
            Source(kind="tool", actor_id="qqonebot:2001", message_id="m-1", session_id="sess-1"),
        )
        self.assertNotEqual(memory.id, "")
        self.assertEqual(memory.source_kind, "tool")
        self.assertEqual(memory.source_actor_id, "qqonebot:2001")
        self.assertEqual(memory.source_message_id, "m-1")
        self.assertEqual(memory.source_session_id, "sess-1")

    def test_scope_isolation(self) -> None:
        store = self.open_store()
        store.remember(
            Memory(platform="p", scope_id="s1", content="一", source_actor_id="a", source_message_id="m")
        )
        store.remember(
            Memory(platform="p", scope_id="s2", content="二", source_actor_id="a", source_message_id="m")
        )
        store.remember(
            Memory(platform="p2", scope_id="s1", content="三", source_actor_id="a", source_message_id="m")
        )
        self.assertEqual(store.count("p", "s1"), 1)
        self.assertEqual(len(store.list("p", "s1", SourceFilter(), 10)), 1)
        # 按来源删除只作用于给定 platform + scope。
        self.assertEqual(store.delete_by_source("p", "s1", SourceFilter(actor_id="a")), 1)
        self.assertEqual(store.count("p", "s2"), 1)
        self.assertEqual(store.count("p2", "s1"), 1)

    def test_withdrawal_event_deletes_by_platform_scope_and_message(self) -> None:
        """平台撤回：按「平台 + scope + source_message_id」删除派生记忆。"""
        store = self.open_store()
        store.remember(
            Memory(platform="p", scope_id="s", content="被撤回的消息", source_actor_id="a", source_message_id="m-9")
        )
        store.remember(
            Memory(platform="p", scope_id="s", content="另一条", source_actor_id="a", source_message_id="m-9")
        )
        store.remember(
            Memory(platform="p", scope_id="other", content="别的群", source_actor_id="a", source_message_id="m-9")
        )
        store.remember(Memory(platform="p", scope_id="s", content="旧版记忆", source="tool"))
        deleted = store.delete_by_source("p", "s", SourceFilter(message_id="m-9"))
        self.assertEqual(deleted, 2)
        remaining = store.list("p", "s", SourceFilter(), 10)
        self.assertEqual([m.content for m in remaining], ["旧版记忆"])
        self.assertEqual(store.count("p", "other"), 1)

    def test_visibility_filter_for_ordinary_member_and_admin(self) -> None:
        """``/forget`` 的库层可见性过滤：成员只看得到来源为自己的记忆。"""
        store = self.open_store()
        store.remember(
            Memory(platform="p", scope_id="s", content="我的", source_actor_id="member-1", source_message_id="m1")
        )
        store.remember(
            Memory(platform="p", scope_id="s", content="别人的", source_actor_id="member-2", source_message_id="m2")
        )
        store.remember(Memory(platform="p", scope_id="s", content="旧版记忆", source="tool"))
        # 普通成员：SourceFilter(actor_id=自己)。
        mine = store.list("p", "s", SourceFilter(actor_id="member-1"), 10)
        self.assertEqual([m.content for m in mine], ["我的"])
        # 群主 / 管理员 / 超管：空过滤器 = 整个 scope。
        everything = store.list("p", "s", SourceFilter(), 10)
        self.assertEqual(len(everything), 3)
        # 没有来源的旧行对成员过滤永远不可见 —— 不会被"猜"给任何人。
        self.assertEqual(store.list("p", "s", SourceFilter(actor_id="member-3"), 10), [])

    def test_delete_scoped_reports_missing_and_foreign_ids(self) -> None:
        _, service = self.open_memory_service(Options(max_writes_per_minute=100))
        memory = service.remember("p", "s", "内容", "", "test")
        with self.assertRaises(NotFoundError):
            service.delete("p", "other", memory.id)
        with self.assertRaises(NotFoundError):
            service.delete("p", "s", "does-not-exist")
        service.delete("p", "s", memory.id)
        with self.assertRaises(NotFoundError):
            service.get("p", "s", memory.id)

    def test_resolve_id_is_ambiguous_for_shared_prefix(self) -> None:
        store = self.open_store()
        store.remember(
            Memory(id="shared-prefix-1", platform="p", scope_id="s", content="一", source_actor_id="a")
        )
        store.remember(
            Memory(id="shared-prefix-2", platform="p", scope_id="s", content="二", source_actor_id="a")
        )
        with self.assertRaises(AmbiguousIDError):
            store.resolve_id("p", "s", "shared-prefix", SourceFilter())
        # 前缀解析也受来源过滤约束。
        with self.assertRaises(NotFoundError):
            store.resolve_id("p", "s", "shared", SourceFilter(actor_id="other"))

    def test_prefix_resolution_stays_inside_scope_and_source(self) -> None:
        store = self.open_store()
        memory = store.remember(
            Memory(platform="p", scope_id="s", content="内容", source_actor_id="actor-1")
        )
        with self.assertRaises(NotFoundError):
            store.resolve_id("p", "s", memory.id[:8], SourceFilter(actor_id="actor-2"))
        self.assertEqual(
            store.resolve_id("p", "s", memory.id[:8], SourceFilter(actor_id="actor-1")),
            memory.id,
        )


class TestRecallAndContextExtras(AngelMemoryTestCase):
    """召回兜底、上下文预算与就绪状态。"""

    def test_recall_fallback_returns_high_strength_memories(self) -> None:
        store, service = self.open_memory_service(Options(max_writes_per_minute=100))
        store.remember(Memory(platform="p", scope_id="s", content="完全无关的旧事实", strength=80))
        store.remember(Memory(platform="p", scope_id="s", content="低强度事实", strength=10))
        # 关键词一个有命中都没有 → 兜底到 strength >= 60。
        memories = service._recall("p", "s", "螃蟹怎么吃", 5, True)
        self.assertEqual([m.content for m in memories], ["完全无关的旧事实"])
        # 不启用兜底时返回空。
        self.assertEqual(service.recall("p", "s", "螃蟹怎么吃", 5), [])

    def test_context_returns_empty_when_nothing_written(self) -> None:
        _, service = self.open_memory_service(Options(max_writes_per_minute=100))
        self.assertEqual(service.context("p", "s", "任何问题", 5), "")
        # 只有 NULL/空白内容时也不写块。
        service.remember("p", "s", "可见内容", "", "test")
        self.assertEqual(service.context("p", "s", "", 5).count("<angel_memory>"), 1)

    def test_context_respects_rune_budget(self) -> None:
        _, service = self.open_memory_service(
            Options(max_writes_per_minute=100, max_context_runes=30)
        )
        for index in range(5):
            service.remember("p", "s", f"第{index}条记忆内容很长很长很长", "", "test")
        text = service.context("p", "s", "", 10)
        # 头部两行 + 若干行 + 尾行；正文部分不得超出预算。
        body = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertTrue(body)
        self.assertLessEqual(sum(len(line) for line in body), 30)

    def test_strength_is_clamped_and_max_strength_wins_on_update(self) -> None:
        store = self.open_store()
        low = store.remember(Memory(platform="p", scope_id="s", content="一", strength=-5))
        self.assertEqual(low.strength, 50)
        high = store.remember(Memory(platform="p", scope_id="s", content="二", strength=500))
        self.assertEqual(high.strength, 100)
        updated = store.remember(Memory(platform="p", scope_id="s", content="二", strength=10))
        self.assertEqual(updated.strength, 100, "冲突更新取 MAX(strength)")
        self.assertEqual(updated.id, high.id, "同内容更新保留原 id")

    def test_remember_normalizes_tags_and_source_fields(self) -> None:
        store = self.open_store()
        memory = store.remember(
            Memory(
                platform=" p ",
                scope_id=" s ",
                content="  内容  ",
                tags="a,b，c a\nb\t x",
                source="x" * 300,
                source_kind="k" * 100,
                source_actor_id="actor",
            )
        )
        self.assertEqual(memory.platform, "p")
        self.assertEqual(memory.scope_id, "s")
        self.assertEqual(memory.content, "内容")
        self.assertEqual(memory.tags, "a,b,c,x")
        self.assertEqual(len(memory.source), 200)
        self.assertEqual(len(memory.source_kind), 40)

    def test_service_is_disabled_without_store(self) -> None:
        from atri_x.errors import DisabledError

        service = new_service(None)
        self.assertFalse(service.ready())
        with self.assertRaises(DisabledError):
            service.remember("p", "s", "内容", "", "test")
        self.assertEqual(service.count("p", "s"), 0)
        self.assertEqual(service.delete_before(storage_now()), 0)


class TestAngelForgetLibraryLayer(AngelMemoryTestCase):
    """``angel_forget`` 的库层权限边界与每分钟上限。"""

    def test_resolve_own_only_accepts_own_structured_source(self) -> None:
        store, service = self.open_memory_service(Options(max_writes_per_minute=100))
        own = service.remember_with_source(
            "qqonebot", "group:1", "我自己的记忆", "", Source(kind="tool", actor_id="qqonebot:2001")
        )
        other = service.remember_with_source(
            "qqonebot", "group:1", "别人的记忆", "", Source(kind="tool", actor_id="qqonebot:2002")
        )
        legacy = service.remember_with_source(
            "qqonebot", "group:1", "旧版记忆", "", Source(label="tool")
        )
        resolved = service.resolve_own("qqonebot", "group:1", own.id[:8], "qqonebot:2001")
        self.assertEqual(resolved.id, own.id)
        # 别人的记忆：过滤器就把它挡在外面。
        with self.assertRaises(NotFoundError):
            service.resolve_own("qqonebot", "group:1", other.id[:8], "qqonebot:2001")
        # 没有结构化来源的旧行永远不匹配。
        with self.assertRaises(NotFoundError):
            service.resolve_own("qqonebot", "group:1", legacy.id[:8], "qqonebot:2001")
        # 别的 scope 也不行。
        with self.assertRaises(NotFoundError):
            service.resolve_own("qqonebot", "group:2", own.id[:8], "qqonebot:2001")
        # 没有发言人身份时直接拒绝。
        with self.assertRaises(InvalidError):
            service.resolve_own("qqonebot", "group:1", own.id[:8], "")

    def test_resolve_own_rechecks_actor_link(self) -> None:
        store, service = self.open_memory_service(Options(max_writes_per_minute=100))
        memory = service.remember_with_source(
            "p", "s", "内容", "", Source(kind="tool", actor_id="actor-1", message_id="m-1")
        )
        # 过滤器就是第一道边界：把来源成员改掉后，前缀解析直接看不到它。
        store.connection.execute(
            "UPDATE memories SET source_actor_id = 'actor-2' WHERE id = ?", (memory.id,)
        )
        with self.assertRaises(NotFoundError):
            store.resolve_own("p", "s", memory.id[:8], "actor-1")

        # 第二道（防 TOCTOU）：解析与读取是两条语句，行可能在中间被改写。
        # 这里把读取替换成"来源成员已经变了"的行，验证再核对确实存在。
        store.connection.execute(
            "UPDATE memories SET source_actor_id = 'actor-1' WHERE id = ?", (memory.id,)
        )
        original_get = store.get
        store.get = lambda platform, scope_id, id: Memory(
            id=id, platform=platform, scope_id=scope_id, content="内容", source_actor_id="actor-2"
        )
        try:
            with self.assertRaises(ForbiddenError):
                store.resolve_own("p", "s", memory.id[:8], "actor-1")
            store.get = lambda platform, scope_id, id: Memory(
                id=id, platform=platform, scope_id=scope_id, content="内容", source_actor_id=""
            )
            with self.assertRaises(ForbiddenError):
                store.resolve_own("p", "s", memory.id[:8], "actor-1")
        finally:
            store.get = original_get

    def test_forget_guard_rate_limits_per_scope_and_actor(self) -> None:
        guard = ForgetGuard()
        self.assertEqual(guard.max_per_minute, ANGEL_FORGET_MAX_PER_MINUTE)
        self.assertEqual(ANGEL_FORGET_MAX_PER_MINUTE, 3)
        for _ in range(ANGEL_FORGET_MAX_PER_MINUTE):
            self.assertTrue(guard.allow("p", "s", "actor-1"))
        self.assertFalse(guard.allow("p", "s", "actor-1"))
        # 换成员或换 scope 是各自的预算。
        self.assertTrue(guard.allow("p", "s", "actor-2"))
        self.assertTrue(guard.allow("p", "other", "actor-1"))
        self.assertEqual(
            ForgetGuard.rate_key("p", "s", "actor-1"), "p\x00s\x00actor-1"
        )

    def test_forget_guard_default_budget_is_three_per_minute(self) -> None:
        guard = ForgetGuard(ANGEL_FORGET_MAX_PER_MINUTE)
        allowed = sum(1 for _ in range(10) if guard.allow("p", "s", "a"))
        self.assertEqual(allowed, 3)


def _recall_query(**kwargs):
    from atri_x.angelmemory import RecallQuery

    return RecallQuery(**kwargs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
