"""``atri_x.media`` 的生命周期与清理测试。

对应上游：``internal/media/lifecycle_test.go``、``concurrency_test.go``、
``history_test.go``（对账部分）、``internal/storage/sqlite/media_lifecycle_test.go``
（认领 / 输出过期 / 只读一致性检查）。

覆盖的语义（逐条来自上游文档与源码）：

* 孤儿宽限期**固定 1 小时**，从最后一个引用释放开始；宽限期内不清理，
  再次使用会**刷新** ``orphaned_at``；
* **认领与引用添加在 SQLite 写事务中互斥**：认领（``deleting=1``）之后新引用被拒；
* 按记录里的**实际存储位置**选后端；双副本**先删远端再删本地**；
* 所需后端不可用或任一删除失败时保留 ``deleting``，供幂等重试；
* 删除与同 ID 重建**不交叉**（共享 Manager 的对象锁串行化）；
* 清理前**只读检查**消息/输出/历史/fork 的缺失引用与悬空 owner，
  异常时**保守停止并报告**；
* 输出缓存到期释放引用；待重试与 Session 引用**独立保留**。

测试基础设施从 :mod:`tests.test_media_manager` import（见那里的 docstring）。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import timedelta
from typing import Any

from atri_x.errors import ConflictError, NotFoundError
from atri_x.media import (
    CLEANUP_CONCURRENCY,
    ORPHAN_GRACE,
    FileDeliveryConfig,
    Input,
    LocalBackend,
    MediaError,
    MediaOutput,
    MediaReference,
    new_s3_backend,
    valid_id,
)
from atri_x.storage.models import HistoryMedia, Media
from atri_x.storage.times import now as storage_now
from tests.test_media_manager import AsyncMediaTestCase, FakeS3Server


class TrackingDeleteBackend:
    """对应 Go ``trackingDeleteBackend``：按顺序记录 ``remove`` 调用，可选失败。"""

    def __init__(
        self,
        name: str,
        calls: list[str],
        backing: Any = None,
        fail: bool = False,
    ) -> None:
        self.name = name
        self.calls = calls
        self.backing = backing
        self.fail = fail

    async def remove(self, ctx: Any, media: Any) -> None:
        self.calls.append(self.name)
        if self.fail:
            raise MediaError(f"temporary {self.name} backend error")
        if self.backing is not None:
            await self.backing.remove(ctx, media)

    async def put(self, ctx: Any, id: str, data: bytes, size: int, content_type: str) -> str:
        if self.backing is not None:
            return await self.backing.put(ctx, id, data, size, content_type)
        return "media/" + id

    async def open(self, ctx: Any, media: Any) -> Any:
        if self.backing is None:
            raise MediaError(f"{self.name} backend cannot open objects")
        return await self.backing.open(ctx, media)

    async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
        return f"https://{self.name}.test/" + media.id


class TestOrphanGraceConstant(unittest.TestCase):
    def test_grace_is_fixed_one_hour(self) -> None:
        self.assertEqual(ORPHAN_GRACE, timedelta(hours=1))
        self.assertEqual(CLEANUP_CONCURRENCY, 10)


class TestCleanupUsesStoredLocations(AsyncMediaTestCase):
    """``lifecycle_test.go`` 的 TestCleanupUsesStoredLocationsAcrossBackendChanges。"""

    async def test_s3_to_hybrid_removes_remote_object(self) -> None:
        manager, store, _root = self.make_manager()
        calls: list[str] = []
        remote = TrackingDeleteBackend("remote", calls)
        manager.remote = remote
        id = "media:" + "a" * 64
        store.upsert_media(
            Media(
                id=id,
                name="remote.bin",
                mime_type="application/octet-stream",
                size=1,
                backend="s3",
                object_key="media/remote",
            )
        )
        manager.now = lambda: storage_now() + timedelta(hours=2)
        await self.guard(manager.cleanup(None))
        self.assertEqual(calls, ["remote"], "s3 记录只删远端")
        with self.assertRaises(NotFoundError):
            store.media().get(id)

    def _manager_with_tracking_local(
        self, calls: list[str], *, fail: bool = False
    ) -> tuple[Any, Any, TrackingDeleteBackend]:
        root = self.make_dir("media")
        local = TrackingDeleteBackend("local", calls, backing=LocalBackend(root), fail=fail)
        manager, store, _root = self.make_manager()
        manager.local = local
        manager.backend = local
        return manager, store, local

    async def test_remote_failure_retries_remote_before_local(self) -> None:
        calls: list[str] = []
        manager, store, _local = self._manager_with_tracking_local(calls)
        remote = TrackingDeleteBackend("remote", calls, fail=True)
        item = await self.guard(manager.import_bytes(None, b"dual", Input(name="dual.bin")))
        row = store.media().get(item.id)
        row.object_key = "media/dual"
        store.media().upsert(row)
        manager.backend = remote
        manager.remote = remote
        manager.now = lambda: storage_now() + timedelta(hours=2)

        with self.assertRaises(MediaError):
            await self.guard(manager.cleanup(None))
        self.assertEqual(calls, ["remote"], "远端失败后不能先删本地")
        self.assertTrue(os.path.exists(row.local_path), "本地副本必须保留")
        self.assertTrue(store.media().get(item.id).deleting, "失败后保留 deleting 供重试")

        remote.fail = False
        await self.guard(manager.cleanup(None))
        self.assertEqual(calls, ["remote", "remote", "local"])
        self.assertFalse(os.path.exists(row.local_path))
        with self.assertRaises(NotFoundError):
            store.media().get(item.id)

    async def test_local_failure_after_remote_retries_idempotently(self) -> None:
        calls: list[str] = []
        manager, store, local = self._manager_with_tracking_local(calls, fail=True)
        remote = TrackingDeleteBackend("remote", calls)
        item = await self.guard(manager.import_bytes(None, b"partial delete", Input()))
        row = store.media().get(item.id)
        row.object_key = "media/partial"
        store.media().upsert(row)
        manager.remote = remote
        manager.now = lambda: storage_now() + timedelta(hours=2)

        with self.assertRaises(MediaError):
            await self.guard(manager.cleanup(None))
        self.assertEqual(calls, ["remote", "local"])
        self.assertTrue(os.path.exists(row.local_path), "本地删除失败时副本必须还在")
        self.assertTrue(store.media().get(item.id).deleting)

        local.fail = False
        await self.guard(manager.cleanup(None))
        self.assertEqual(calls, ["remote", "local", "remote", "local"], "重试幂等")
        with self.assertRaises(NotFoundError):
            store.media().get(item.id)

    async def test_missing_remote_preserves_copies_and_retry_metadata(self) -> None:
        calls: list[str] = []
        manager, store, _local = self._manager_with_tracking_local(calls)
        item = await self.guard(manager.import_bytes(None, b"dual without remote", Input()))
        row = store.media().get(item.id)
        row.object_key = "media/dual"
        store.media().upsert(row)
        manager.now = lambda: storage_now() + timedelta(hours=2)

        with self.assertRaises(MediaError):
            await self.guard(manager.cleanup(None))
        self.assertEqual(calls, [], "所需后端不可用时不能删任何物理副本")
        self.assertTrue(os.path.exists(row.local_path))
        self.assertTrue(store.media().get(item.id).deleting)


class TestCleanupWithRealS3(AsyncMediaTestCase):
    """双副本清理：远端（真 S3 服务端）→ 本地，行序与副作用都要成立。"""

    async def test_remote_then_local_with_real_s3(self) -> None:
        server = FakeS3Server()
        self.addCleanup(server.close)
        calls: list[str] = []
        root = self.make_dir("media")
        local = TrackingDeleteBackend("local", calls, backing=LocalBackend(root))
        s3 = new_s3_backend(
            FileDeliveryConfig(s3_endpoint=server.url, s3_bucket="bucket"),
            {"access_key": "access", "secret_key": "secret"},
        )
        remote = TrackingDeleteBackend("remote", calls, backing=s3)
        manager, store, _root = self.make_manager()
        manager.local = local
        manager.backend = local
        item = await self.guard(manager.import_bytes(None, b"dual copy", Input(name="dual.bin")))
        row = store.media().get(item.id)
        row.object_key = await self.guard(
            s3.put(None, item.id, b"dual copy", 9, "application/octet-stream")
        )
        store.media().upsert(row)
        manager.remote = remote
        manager.now = lambda: storage_now() + timedelta(hours=2)

        await self.guard(manager.cleanup(None))
        self.assertEqual(calls, ["remote", "local"], "必须先删远端再删本地")
        self.assertIn("DELETE " + row.object_key, server.calls)
        self.assertNotIn((server.bucket, row.object_key), server.objects)
        self.assertFalse(os.path.exists(row.local_path))
        with self.assertRaises(NotFoundError):
            store.media().get(item.id)


class TestOrphanGrace(AsyncMediaTestCase):
    """1 小时宽限期、宽限期内不清理、再次使用刷新时间。"""

    async def test_grace_window_and_refresh_on_reuse(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"kept around", Input(name="k.bin")))
        real = storage_now()
        store.orphaned_at[item.id] = real - timedelta(hours=2)  # 假装它已经孤儿很久

        # 再次使用（Open + Touch）会把 orphaned_at 刷到现在。
        reader, _media = await self.guard(manager.open(None, item.id))
        reader.close()

        manager.now = lambda: real + timedelta(minutes=30)
        await self.guard(manager.cleanup(None))
        self.assertTrue(valid_id((await self.guard(manager.metadata(None, item.id))).id))

        manager.now = lambda: real + timedelta(minutes=49)
        await self.guard(manager.cleanup(None))
        self.assertTrue(
            valid_id((await self.guard(manager.metadata(None, item.id))).id),
            "1 小时宽限期内不得清理",
        )

        manager.now = lambda: real + timedelta(hours=2)
        await self.guard(manager.cleanup(None))
        with self.assertRaises(NotFoundError):
            store.media().get(item.id)

    async def test_reference_lifecycle_sets_and_clears_orphaned_at(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"ref lifecycle", Input()))
        release = manager.hold(item.id)
        self.assertIsNone(store.orphaned_at[item.id], "有引用时不是孤儿")
        release()
        self.assertIsNotNone(store.orphaned_at[item.id], "最后一条引用释放时记 orphaned_at")

    async def test_claim_blocks_new_references(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"claimed", Input()))
        real = storage_now()
        store.orphaned_at[item.id] = real - timedelta(hours=2)
        claimed = store.media().claim_orphans(real)
        self.assertEqual([row.id for row in claimed], [item.id])
        self.assertTrue(store.media().get(item.id).deleting)
        with self.assertRaises(ConflictError):
            manager.hold(item.id)
        with self.assertRaises(ConflictError):
            manager.add_reference(
                MediaReference(
                    media_id=item.id,
                    owner_type="session_tool",
                    owner_id="s",
                    purpose="input",
                    session_id="s",
                )
            )


class TestOutputExpiryKeepsOtherReferences(AsyncMediaTestCase):
    """输出缓存到期释放引用；待重试与 Session 引用独立保留。"""

    async def test_expired_output_releases_only_its_reference(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"output cache", Input()))
        real = storage_now()
        store.save_output(
            MediaOutput(
                platform="p",
                scope_id="s",
                message_id="m",
                segment_index=0,
                kind="image",
                media_id=item.id,
                expires_at=real + timedelta(minutes=30),
            )
        )
        session_id = store.create_session()
        manager.retain_session_tool_arguments(session_id, '{"source":"%s"}' % item.id)
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 2)

        manager.now = lambda: real + timedelta(hours=2)
        await self.guard(manager.cleanup(None))
        refs = store.media_references().list_media_ids(item.id)
        self.assertEqual([ref.owner_type for ref in refs], ["session_tool"])
        self.assertTrue(valid_id((await self.guard(manager.metadata(None, item.id))).id))

    async def test_pending_output_survives_expiry_pass(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"pending", Input()))
        real = storage_now()
        store.save_output(
            MediaOutput(
                platform="p",
                scope_id="s",
                message_id="m",
                segment_index=1,
                kind="file",
                media_id=item.id,
                expires_at=real + timedelta(days=1),
            )
        )
        manager.now = lambda: real + timedelta(hours=2)
        await self.guard(manager.cleanup(None))
        refs = store.media_references().list_media_ids(item.id)
        self.assertEqual([ref.owner_type for ref in refs], ["output"])
        self.assertTrue(valid_id((await self.guard(manager.metadata(None, item.id))).id))


class TestStartupRecovery(AsyncMediaTestCase):
    """启动恢复：临时引用清空，可持久化的 owner 引用保留。"""

    async def test_recover_drops_only_temporary_owner_types(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"startup", Input()))
        session_id = store.create_session()
        manager.retain_session_tool_arguments(session_id, '{"source":"%s"}' % item.id)
        for owner_type in ("hook", "skill", "request", "output_send"):
            store.add_references(
                [
                    MediaReference(
                        media_id=item.id,
                        owner_type=owner_type,
                        owner_id=owner_type + "-1",
                        purpose="temporary",
                    )
                ]
            )
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 5)
        store.media().recover_interrupted()
        refs = store.media_references().list_media_ids(item.id)
        self.assertEqual([ref.owner_type for ref in refs], ["session_tool"], "持久化 owner 保留")


class TestConsistencyCheckStopsCleanup(AsyncMediaTestCase):
    """清理前只读检查：缺失引用 / 悬空 owner → 保守停止并报告。"""

    async def test_dangling_owner_stops_cleanup(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"dangling", Input()))
        store.add_references(
            [
                MediaReference(
                    media_id=item.id,
                    owner_type="session_tool",
                    owner_id="ghost-session",
                    purpose="input",
                    session_id="",  # session_id 必须等于 owner_id
                )
            ]
        )
        row = store.media().get(item.id)
        manager.now = lambda: storage_now() + timedelta(hours=2)
        with self.assertRaises(MediaError) as ctx:
            await self.guard(manager.cleanup(None))
        self.assertIn("inconsistencies", str(ctx.exception))
        self.assertIn("session_tool:ghost-session", str(ctx.exception))
        self.assertTrue(os.path.exists(row.local_path), "保守停止：不能删任何对象")
        self.assertFalse(store.media().get(item.id).deleting)

    async def test_missing_reference_stops_cleanup(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"history", Input()))
        store.save_history(
            HistoryMedia(
                history_id="h1",
                platform="p",
                scope_id="s",
                message_id="m",
                media_index=1,
                kind="image",
                media_id=item.id,
            )
        )
        stored = store.history[-1]
        # 直接抽掉引用行，模拟"关联存在但引用丢了"。
        store.remove_reference(
            MediaReference(
                media_id=item.id,
                owner_type="chat_history",
                owner_id=stored.owner_id,
                purpose="content",
            )
        )
        with self.assertRaises(MediaError) as ctx:
            await self.guard(manager.cleanup(None))
        self.assertIn("missing history reference", str(ctx.exception))


class TestCleanupConcurrency(AsyncMediaTestCase):
    """``concurrency_test.go`` 的并发语义。"""

    async def test_concurrent_imports_of_same_content_write_once(self) -> None:
        manager, _store, root = self.make_manager()
        gate = asyncio.Event()
        started = asyncio.Event()
        inner = LocalBackend(root)
        puts = 0

        class GatedLocal:
            async def put(self, ctx: Any, id: str, data: bytes, size: int, ct: str) -> str:
                nonlocal puts
                if ct == "application/x-block-test":
                    puts += 1
                    started.set()
                    await gate.wait()
                return await inner.put(ctx, id, data, size, ct)

            async def open(self, ctx: Any, media: Any) -> Any:
                return await inner.open(ctx, media)

            async def remove(self, ctx: Any, media: Any) -> None:
                await inner.remove(ctx, media)

            async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
                return await inner.presign_get(ctx, media, expiry)

        manager.backend = GatedLocal()
        manager.local = inner

        async def import_same() -> str:
            item = await manager.import_bytes(
                None, b"same new content", Input(mime_type="application/x-block-test")
            )
            return item.id

        first = asyncio.ensure_future(import_same())
        await asyncio.wait_for(started.wait(), 5.0)
        second = asyncio.ensure_future(import_same())
        await asyncio.sleep(0.05)
        # 不同内容不能被同一个 ID 的锁挡住。
        other = await asyncio.wait_for(
            manager.import_bytes(None, b"different content", Input()), 5.0
        )
        gate.set()
        first_id, second_id = await asyncio.wait_for(asyncio.gather(first, second), 5.0)
        self.assertEqual(first_id, second_id)
        self.assertEqual(puts, 1, "同内容并发导入只写一次")
        data, _media = await self.guard(manager.read(None, first_id))
        self.assertEqual(data, b"same new content")
        self.assertTrue(valid_id(other.id))

    async def test_delete_does_not_cross_same_id_rebuild(self) -> None:
        manager, store, _root = self.make_manager()
        data = b"rebuilt content"
        item = await self.guard(manager.import_bytes(None, data, Input(name="r.bin")))
        row = store.media().get(item.id)
        row.object_key = "media/" + item.id
        store.media().upsert(row)
        real = storage_now()

        gate = asyncio.Event()
        started = asyncio.Event()

        class GatedRemote:
            async def remove(self, ctx: Any, media: Any) -> None:
                started.set()
                await gate.wait()

            async def put(self, ctx: Any, id: str, payload: bytes, size: int, ct: str) -> str:
                return "media/" + id

            async def open(self, ctx: Any, media: Any) -> Any:
                raise MediaError("unused")

            async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
                return "https://remote.test/" + media.id

        manager.remote = GatedRemote()
        manager.now = lambda: real + timedelta(hours=2)
        cleanup_task = asyncio.ensure_future(manager.cleanup(None))
        await asyncio.wait_for(started.wait(), 5.0)

        rebuild = asyncio.ensure_future(manager.import_bytes(None, data, Input(name="r.bin")))
        await asyncio.sleep(0.05)
        self.assertFalse(rebuild.done(), "同 ID 的重建必须等同 ID 的删除结束")

        gate.set()
        await asyncio.wait_for(cleanup_task, 5.0)
        rebuilt = await asyncio.wait_for(rebuild, 5.0)
        self.assertEqual(rebuilt.id, item.id)
        again, _media = await self.guard(manager.read(None, item.id))
        self.assertEqual(again, data, "重建后的对象必须还在")

    async def test_concurrent_cleanup_rounds_and_partial_failure(self) -> None:
        manager, store, _root = self.make_manager()
        items: dict[str, bytes] = {}
        for index in range(5):
            body = b"cleanup media %d" % index
            item = await self.guard(manager.import_bytes(None, body, Input(name="sample.bin")))
            row = store.media().get(item.id)
            row.object_key = "media/" + item.id
            store.media().upsert(row)
            items[item.id] = body

        failed_id = next(iter(items))
        failure = MediaError("remote delete failed")
        calls: dict[str, int] = {}

        class FlakyRemote:
            async def remove(self, ctx: Any, media: Any) -> None:
                calls[media.id] = calls.get(media.id, 0) + 1
                if media.id == failed_id and calls[media.id] == 1:
                    raise failure

            async def put(self, ctx: Any, id: str, payload: bytes, size: int, ct: str) -> str:
                return "media/" + id

            async def open(self, ctx: Any, media: Any) -> Any:
                raise MediaError("unused")

            async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
                return "https://remote.test/" + media.id

        manager.remote = FlakyRemote()
        manager.now = lambda: storage_now() + timedelta(hours=2)
        rounds = await asyncio.wait_for(
            asyncio.gather(
                manager.cleanup(None), manager.cleanup(None), return_exceptions=True
            ),
            10.0,
        )
        failures = [item for item in rounds if isinstance(item, BaseException)]
        self.assertEqual(len(failures), 1, rounds)
        self.assertIs(failures[0], failure, "失败原因必须原样透传（对应 errors.Is）")
        for id in items:
            with self.assertRaises(NotFoundError):
                store.media().get(id)
            self.assertEqual(calls[id], 2 if id == failed_id else 1)

    async def test_cleanup_parallel_peak_and_other_media_progress(self) -> None:
        manager, store, _root = self.make_manager()
        count = 8
        items: dict[str, bytes] = {}
        for index in range(count):
            body = b"parallel cleanup %d" % index
            item = await self.guard(manager.import_bytes(None, body, Input(name="p.bin")))
            row = store.media().get(item.id)
            row.object_key = "media/" + item.id
            store.media().upsert(row)
            items[item.id] = body

        gate = asyncio.Event()
        started: list[str] = []
        active = 0
        peak = 0

        class CountingRemote:
            async def remove(self, ctx: Any, media: Any) -> None:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                started.append(media.id)
                try:
                    await gate.wait()
                finally:
                    active -= 1

            async def put(self, ctx: Any, id: str, payload: bytes, size: int, ct: str) -> str:
                return "media/" + id

            async def open(self, ctx: Any, media: Any) -> Any:
                raise MediaError("unused")

            async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
                return "https://remote.test/" + media.id

        manager.remote = CountingRemote()
        manager.now = lambda: storage_now() + timedelta(hours=2)
        task = asyncio.ensure_future(manager.cleanup(None))
        for _ in range(200):
            if len(started) >= min(CLEANUP_CONCURRENCY, count):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(started), min(CLEANUP_CONCURRENCY, count), "并发上限")

        # 清理期间无关媒体必须照常可用。
        other = await asyncio.wait_for(
            manager.import_bytes(None, b"unrelated", Input(name="o.bin")), 5.0
        )
        body, _media = await asyncio.wait_for(manager.read(None, other.id), 5.0)
        self.assertEqual(body, b"unrelated")
        gate.set()
        await asyncio.wait_for(task, 10.0)
        self.assertEqual(peak, min(CLEANUP_CONCURRENCY, count))
        for id in items:
            with self.assertRaises(NotFoundError):
                store.media().get(id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
