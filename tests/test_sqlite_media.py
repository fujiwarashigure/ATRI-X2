"""``atri_x/storage/sqlite`` 的媒体引用生命周期 unittest（触发器 + 清理认领 + 审计）。

这是上游最复杂的一块：引用事实全在 ``media_references``，同步全在 v13/v14/v16 的触发器上。
所以这里**不打桩、不 mock**：真实 SQLite 文件、真实 migration、真实触发器，
断言的是"插一行/删一行之后引用表长什么样"。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import unittest
from datetime import timedelta

from atri_x.errors import DisabledError, InvalidError, NotFoundError
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText
from atri_x.storage.models import (
    CreateAtrinisEventRequest,
    HistoryMedia,
    Media,
    MediaOutput,
    MediaReference,
    Message,
    RoleAssistant,
    RoleTool,
    RoleUser,
    Session,
    UpsertCronJobRequest,
    segments_json,
)
from atri_x.storage.sqlite import SQLiteError, new
from atri_x.storage.times import now

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_ROOT = os.path.join(_REPO_ROOT, "tmp_sqlite_test")


def media_id(letter: str) -> str:
    return "media:" + letter * 64


def image_segments(*ids: str) -> str:
    return segments_json([MessageSegment(type=SegmentImage, media=value) for value in ids])


def cron_delivery_state(identifier: str) -> str:
    """cron 投递状态 JSON（触发器的 JSON 路径是 ``$.report_segments[*].media``）。"""
    return json.dumps(
        {"run_id": "run", "report_segments": [MessageSegment(type=SegmentImage, media=identifier).to_json()]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class SqliteMediaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = os.path.join(_TMP_ROOT, "media", self._testMethodName)
        shutil.rmtree(self.directory, ignore_errors=True)
        os.makedirs(self.directory, exist_ok=True)
        self.path = os.path.join(self.directory, "atrix_sessions.db")
        self.store = new(self.path)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def save_media(self, letter: str, *, age: timedelta | None = None) -> str:
        identifier = media_id(letter)
        media = Media(
            id=identifier,
            name=f"{letter}.png",
            mime_type="image/png",
            size=42,
            backend="local",
            local_path=f"C:/media/{letter}.png",
            source_platform="telegram",
            source_file_id=f"file-{letter}",
        )
        if age is not None:
            media.created_at = now() - age
        self.store.media().upsert(media)
        return identifier

    def orphaned_at(self, identifier: str):
        return self.store.database.query_row(
            "SELECT orphaned_at, deleting FROM media WHERE id = ?", (identifier,)
        )

    def refs(self, identifier: str) -> list[MediaReference]:
        return self.store.media_references().list_media_ids(identifier)


class TestMediaRepositoryBasics(SqliteMediaTestCase):
    def test_upsert_get_round_trip(self) -> None:
        identifier = self.save_media("a", age=timedelta(minutes=1))
        loaded = self.store.media().get(identifier)
        self.assertEqual(identifier, loaded.id)
        self.assertEqual("a.png", loaded.name)
        self.assertEqual("image/png", loaded.mime_type)
        self.assertEqual(42, loaded.size)
        self.assertEqual("local", loaded.backend)
        self.assertEqual("C:/media/a.png", loaded.local_path)
        self.assertEqual("telegram", loaded.source_platform)
        self.assertEqual("file-a", loaded.source_file_id)
        self.assertFalse(loaded.deleting)
        self.assertIsNone(loaded.expires_at)
        self.assertIsNotNone(loaded.created_at)
        self.assertEqual(loaded.created_at, loaded.last_accessed_at)

    def test_get_missing_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.media().get(media_id("z"))

    def test_upsert_keeps_created_at_and_does_not_revive_deleting_rows(self) -> None:
        identifier = self.save_media("b", age=timedelta(minutes=5))
        created_at = self.store.media().get(identifier).created_at

        refreshed = Media(id=identifier, mime_type="image/jpeg", size=7, backend="local", name="renamed.jpg")
        self.store.media().upsert(refreshed)
        loaded = self.store.media().get(identifier)
        self.assertEqual("image/jpeg", loaded.mime_type)
        self.assertEqual(7, loaded.size)
        self.assertEqual("renamed.jpg", loaded.name)
        self.assertEqual(created_at, loaded.created_at)  # created_at 不被冲突更新覆盖

        self.assertTrue(self.store.media().claim_orphans(now() + timedelta(hours=1)))
        self.store.media().upsert(Media(id=identifier, mime_type="text/plain", backend="local"))
        self.assertEqual("image/jpeg", self.store.media().get(identifier).mime_type)

    def test_media_reference_add_is_idempotent_and_validated_by_trigger(self) -> None:
        identifier = self.save_media("c")
        reference = MediaReference(
            media_id=identifier, owner_type="message", owner_id="message-1", purpose="input", session_id="s-1"
        )
        self.store.media_references().add(reference)
        self.store.media_references().add(reference)
        self.assertEqual(1, int(self.store.database.query_row("SELECT COUNT(*) AS c FROM media_references")["c"]))
        owner_refs = self.store.media_references().list_by_owner("message", "message-1")
        self.assertEqual(1, len(owner_refs))
        self.assertEqual("s-1", owner_refs[0].session_id)
        self.assertEqual("input", owner_refs[0].purpose)
        self.assertIsNotNone(owner_refs[0].created_at)
        self.assertIsNotNone(self.store.media_references().list_media_ids(identifier)[0].created_at)

        # 引用不存在的媒体：外键直接拒绝（Go 侧同样报错）。
        with self.assertRaises(SQLiteError):
            self.store.media_references().add(
                MediaReference(media_id="media:missing", owner_type="t", owner_id="o", purpose="p")
            )

        self.store.media_references().remove(reference)  # 不存在也算成功（DELETE 0 行）
        self.assertEqual([], self.store.media_references().list_media_ids(identifier))

    def test_add_all_is_one_transaction(self) -> None:
        good = self.save_media("d")
        references = [
            MediaReference(media_id=good, owner_type="batch", owner_id="1", purpose="content"),
            MediaReference(media_id=good, owner_type="batch", owner_id="2", purpose="content"),
        ]
        self.store.media_references().add_all(references)
        self.assertEqual(2, len(self.store.media_references().list_media_ids(good)))

        broken = [
            MediaReference(media_id=good, owner_type="batch", owner_id="3", purpose="content"),
            MediaReference(media_id="media:missing", owner_type="batch", owner_id="4", purpose="content"),
        ]
        with self.assertRaises(SQLiteError):
            self.store.media_references().add_all(broken)
        self.assertEqual(2, len(self.store.media_references().list_media_ids(good)))
        self.store.media_references().add_all([])

    def test_delete_orphans_lists_without_deleting(self) -> None:
        identifier = self.save_media("e", age=timedelta(hours=2))
        orphans = self.store.media().delete_orphans(now())
        self.assertEqual([identifier], [row.id for row in orphans])
        # Go 的 DeleteOrphans 只查不删。
        self.assertEqual(identifier, self.store.media().get(identifier).id)

        self.store.media_references().add(
            MediaReference(media_id=identifier, owner_type="t", owner_id="o", purpose="content")
        )
        self.assertEqual([], self.store.media().delete_orphans(now()))


class TestMediaOrphanTriggers(SqliteMediaTestCase):
    def test_insert_marks_orphan_from_created_at(self) -> None:
        identifier = self.save_media("a", age=timedelta(hours=3))
        row = self.orphaned_at(identifier)
        expected = self.store.media().get(identifier).created_at
        self.assertIsNotNone(row["orphaned_at"])
        self.assertTrue(row["orphaned_at"].startswith(expected.strftime("%Y-%m-%dT%H:%M:%S")))

    def test_reference_added_clears_and_removed_restores_orphan_timestamp(self) -> None:
        identifier = self.save_media("b", age=timedelta(hours=3))
        reference = MediaReference(media_id=identifier, owner_type="t", owner_id="o", purpose="content")
        self.store.media_references().add(reference)
        self.assertIsNone(self.orphaned_at(identifier)["orphaned_at"])

        self.store.media_references().remove(reference)
        row = self.orphaned_at(identifier)
        self.assertIsNotNone(row["orphaned_at"])
        # 释放时间是"现在"（UTC，带毫秒），不再是 created_at。
        self.assertGreater(row["orphaned_at"], now().strftime("%Y-%m-%dT%H:%M:%S"))

    def test_claim_respects_the_grace_period(self) -> None:
        identifier = self.save_media("c")  # orphaned_at = created_at = 现在 → 宽限期内
        self.assertEqual([], self.store.media().claim_orphans(now() - timedelta(hours=1)))
        self.assertFalse(self.orphaned_at(identifier)["deleting"])

        claimed = self.store.media().claim_orphans(now() + timedelta(hours=1))
        self.assertEqual([identifier], [row.id for row in claimed])
        self.assertTrue(claimed[0].deleting)
        self.assertTrue(self.orphaned_at(identifier)["deleting"])

        # 已认领的行会被再次返回（重启后接着清理）。
        self.assertEqual([identifier], [row.id for row in self.store.media().claim_orphans(now())])

    def test_referenced_media_is_never_claimed(self) -> None:
        identifier = self.save_media("d", age=timedelta(days=1))
        self.store.media_references().add(
            MediaReference(media_id=identifier, owner_type="t", owner_id="o", purpose="content")
        )
        self.assertEqual([], self.store.media().claim_orphans(now() + timedelta(days=1)))
        self.assertFalse(self.orphaned_at(identifier)["deleting"])

    def test_reference_guard_blocks_new_references_after_claim(self) -> None:
        identifier = self.save_media("e", age=timedelta(hours=2))
        self.store.media().claim_orphans(now())
        with self.assertRaises(SQLiteError) as caught:
            self.store.media_references().add(
                MediaReference(media_id=identifier, owner_type="late", owner_id="o", purpose="input")
            )
        self.assertIn("media is being deleted", str(caught.exception))
        self.assertEqual([], self.refs(identifier))

    def test_finish_delete_removes_only_claimed_and_unreferenced_rows(self) -> None:
        identifier = self.save_media("f", age=timedelta(hours=2))
        self.store.media().finish_delete(identifier)  # 还没认领 → 不删
        self.assertEqual(identifier, self.store.media().get(identifier).id)

        self.store.media().claim_orphans(now())
        self.store.media().finish_delete(identifier)
        with self.assertRaises(NotFoundError):
            self.store.media().get(identifier)

    def test_touch_updates_access_and_rejects_deleting_media(self) -> None:
        identifier = self.save_media("g", age=timedelta(hours=2))
        accessed = now()
        self.store.media().touch(identifier, accessed)
        loaded = self.store.media().get(identifier)
        self.assertEqual(accessed, loaded.last_accessed_at)
        self.assertIsNotNone(self.orphaned_at(identifier)["orphaned_at"])  # 孤儿宽限期被续上

        self.store.media().claim_orphans(now() + timedelta(hours=1))
        with self.assertRaises(DisabledError):
            self.store.media().touch(identifier, now())
        with self.assertRaises(DisabledError):
            self.store.media().touch(media_id("missing"), now())


class TestMessageMediaReferences(SqliteMediaTestCase):
    def new_session(self) -> Session:
        session = Session(owner_id="u", platform="test", platform_scope_id="s")
        self.store.sessions().create(session)
        return session

    def test_append_creates_references_per_role_and_dedupes(self) -> None:
        identifier = self.save_media("a")
        session = self.new_session()
        for role, owner_type in ((RoleUser, "message"), (RoleTool, "tool_result")):
            message = Message(
                session_id=session.id, role=role, content="", segments=image_segments(identifier, identifier)
            )
            self.store.messages().append(message)
            refs = self.store.media_references().list_by_owner(owner_type, message.id)
            self.assertEqual([identifier], [row.media_id for row in refs], role)
            self.assertEqual("content", refs[0].purpose)
            self.assertEqual(session.id, refs[0].session_id)

    def test_append_rolls_back_message_when_media_is_missing(self) -> None:
        session = self.new_session()
        bad = Message(session_id=session.id, role=RoleUser, content="x", segments=image_segments("media:missing"))
        with self.assertRaises(SQLiteError):
            self.store.messages().append(bad)
        with self.assertRaises(NotFoundError):
            self.store.messages().get(bad.id)

    def test_delete_message_releases_references_but_keeps_media(self) -> None:
        identifier = self.save_media("b")
        session = self.new_session()
        message = Message(session_id=session.id, role=RoleUser, content="", segments=image_segments(identifier))
        self.store.messages().append(message)
        self.assertEqual(1, len(self.refs(identifier)))

        self.store.database.execute("DELETE FROM messages WHERE id = ?", (message.id,))
        self.assertEqual([], self.refs(identifier))
        self.assertEqual(identifier, self.store.media().get(identifier).id)

    def test_delete_session_releases_references(self) -> None:
        identifier = self.save_media("c")
        session = self.new_session()
        message = Message(session_id=session.id, role=RoleUser, content="", segments=image_segments(identifier))
        self.store.messages().append(message)
        self.assertEqual(1, len(self.refs(identifier)))

        self.store.sessions().delete(session.id)
        self.assertEqual([], self.refs(identifier))
        self.assertEqual(identifier, self.store.media().get(identifier).id)  # 只释放引用，不删媒体

    def test_segments_update_trigger_replaces_references_atomically(self) -> None:
        first = self.save_media("d")
        second = self.save_media("e")
        session = self.new_session()
        message = Message(session_id=session.id, role=RoleUser, content="", segments=image_segments(first))
        self.store.messages().append(message)

        self.store.database.execute(
            "UPDATE messages SET segments = ? WHERE id = ?", (image_segments(second), message.id)
        )
        self.assertEqual([second], [row.media_id for row in self.refs(second)])
        self.assertEqual([], self.refs(first))

        # 指向不存在的媒体 → 触发器/外键失败，整条 UPDATE 回滚（segments 与引用都保持原样）。
        with self.assertRaises(SQLiteError):
            self.store.database.execute(
                "UPDATE messages SET segments = ? WHERE id = ?",
                (image_segments("media:missing"), message.id),
            )
        self.assertEqual(image_segments(second), self.store.messages().get(message.id).segments)
        self.assertEqual([second], [row.media_id for row in self.refs(second)])

    def test_segments_update_to_text_releases_references(self) -> None:
        identifier = self.save_media("f")
        session = self.new_session()
        message = Message(session_id=session.id, role=RoleUser, content="", segments=image_segments(identifier))
        self.store.messages().append(message)
        self.store.database.execute(
            "UPDATE messages SET segments = ? WHERE id = ?",
            (segments_json([MessageSegment(type=SegmentText, text="纯文本")]), message.id),
        )
        self.assertEqual([], self.refs(identifier))


class TestForkMediaInheritance(SqliteMediaTestCase):
    def test_fork_references_respect_the_checkpoint(self) -> None:
        parent = Session(owner_id="u", platform="test", platform_scope_id="s")
        self.store.sessions().create(parent)
        stamp = now()
        messages: list[Message] = []
        for letter in ("a", "b", "c"):
            identifier = self.save_media(letter)
            message = Message(
                session_id=parent.id,
                role=RoleAssistant,
                content="",
                segments=image_segments(identifier),
                created_at=stamp,
            )
            self.store.messages().append(message)
            messages.append(message)

        fork = Session(
            owner_id="u",
            platform="test",
            platform_scope_id="s",
            parent_session_id=parent.id,
            fork_from_message_id=messages[1].id,
        )
        self.store.sessions().create(fork)

        refs = self.store.media_references().list_by_owner("session_fork", fork.id)
        self.assertEqual(2, len(refs))
        self.assertNotIn(media_id("c"), [row.media_id for row in refs])
        self.assertEqual("history", refs[0].purpose)
        self.assertEqual(fork.id, refs[0].session_id)

        # 嵌套 fork：把 fork 自己的一条消息当检查点，继承范围再往前收一次。
        checkpoint = Message(session_id=fork.id, role=RoleAssistant, content="checkpoint")
        self.store.messages().append(checkpoint)
        nested = Session(
            owner_id="u",
            platform="test",
            platform_scope_id="s",
            parent_session_id=fork.id,
            fork_from_message_id=checkpoint.id,
        )
        self.store.sessions().create(nested)
        nested_refs = self.store.media_references().list_by_owner("session_fork", nested.id)
        self.assertEqual(2, len(nested_refs))
        self.assertEqual([], self.store.media().check_references())

        self.store.database.execute(
            "DELETE FROM media_references WHERE owner_type='session_fork' AND owner_id=? AND media_id=?",
            (nested.id, media_id("a")),
        )
        self.assertEqual([f"missing fork reference: {nested.id}"], self.store.media().check_references())

    def test_shared_media_survives_owner_deletion_through_the_fork_reference(self) -> None:
        identifier = self.save_media("a", age=timedelta(days=1))
        sessions = []
        checkpoint = ""
        for _ in range(2):
            session = Session(owner_id="u", platform="p", platform_scope_id="s")
            self.store.sessions().create(session)
            message = Message(session_id=session.id, role=RoleUser, content="", segments=image_segments(identifier))
            self.store.messages().append(message)
            if not checkpoint:
                checkpoint = message.id
            sessions.append(session)

        fork = Session(
            owner_id="u",
            platform="p",
            platform_scope_id="s",
            parent_session_id=sessions[0].id,
            fork_from_message_id=checkpoint,
        )
        self.store.sessions().create(fork)
        for session in sessions:
            self.store.sessions().delete(session.id)

        refs = self.store.media_references().list_media_ids(identifier)
        self.assertEqual(["session_fork"], [row.owner_type for row in refs])
        self.assertEqual([], self.store.media().claim_orphans(now()))

        self.store.sessions().delete(fork.id)
        self.assertEqual([], self.store.media().claim_orphans(now() - timedelta(hours=1)))  # 宽限期未到
        claimed = self.store.media().claim_orphans(now() + timedelta(hours=1))
        self.assertEqual([identifier], [row.id for row in claimed])

        self.store.media().finish_delete(identifier)
        with self.assertRaises(NotFoundError):
            self.store.media().get(identifier)

    def test_fork_inherits_session_tool_media_only_before_the_checkpoint(self) -> None:
        parent = Session(owner_id="u", platform="test", platform_scope_id="s")
        self.store.sessions().create(parent)
        before = self.save_media("d")
        after = self.save_media("e")
        checkpoint_at = now()
        checkpoint = Message(
            session_id=parent.id, role=RoleAssistant, content="checkpoint", created_at=checkpoint_at
        )
        self.store.messages().append(checkpoint)
        self.store.media_references().add_all(
            [
                MediaReference(
                    media_id=before,
                    owner_type="session_tool",
                    owner_id=parent.id,
                    purpose="input",
                    session_id=parent.id,
                    created_at=checkpoint_at - timedelta(seconds=1),
                ),
                MediaReference(
                    media_id=after,
                    owner_type="session_tool",
                    owner_id=parent.id,
                    purpose="input",
                    session_id=parent.id,
                    created_at=checkpoint_at + timedelta(seconds=1),
                ),
            ]
        )
        fork = Session(
            owner_id="u",
            platform="test",
            platform_scope_id="s",
            parent_session_id=parent.id,
            fork_from_message_id=checkpoint.id,
        )
        self.store.sessions().create(fork)
        refs = self.store.media_references().list_by_owner("session_fork", fork.id)
        self.assertEqual([before], [row.media_id for row in refs])
        self.assertEqual([], self.store.media().check_references())


class TestMediaOutputs(SqliteMediaTestCase):
    def test_outputs_are_ordered_scoped_and_replaced(self) -> None:
        first = self.save_media("a")
        second = self.save_media("b")
        third = self.save_media("c")
        expires = now() + timedelta(hours=1)
        for output in (
            MediaOutput(platform="p", scope_id="group:1", message_id="m", segment_index=2, kind="image", media_id=first, expires_at=expires),
            MediaOutput(platform="p", scope_id="group:1", message_id="m", segment_index=0, kind="file", media_id=second, expires_at=expires),
            MediaOutput(platform="p", scope_id="group:1", message_id="m", segment_index=1, kind="image", media_id=first, expires_at=expires),
        ):
            self.store.media().save_output(output)

        found = self.store.media().find_outputs("p", "group:1", "m", now())
        self.assertEqual([0, 1, 2], [row.segment_index for row in found])
        self.assertEqual(["file", "image", "image"], [row.kind for row in found])
        self.assertEqual([second, first, first], [row.media_id for row in found])
        self.assertTrue(found[0].owner_id)
        self.assertEqual(expires, found[0].expires_at)
        for scope in ("group:2", "private:1"):
            self.assertEqual([], self.store.media().find_outputs("p", scope, "m", now()))

        # 同一个 segment_index 覆盖 → 旧引用的 cache 引用被释放。
        self.store.media().save_output(
            MediaOutput(
                platform="p",
                scope_id="group:1",
                message_id="m",
                segment_index=0,
                kind="image",
                media_id=third,
                expires_at=expires,
            )
        )
        self.assertEqual([], self.refs(second))
        self.assertEqual(1, len(self.refs(third)))

        self.store.media_references().add(
            MediaReference(media_id=first, owner_type="test", owner_id="session", purpose="content")
        )
        self.store.media().expire_outputs(now() + timedelta(hours=2))
        self.assertEqual(1, len(self.refs(first)))  # 独立 owner 不受影响
        self.assertEqual("test", self.refs(first)[0].owner_type)
        self.assertEqual([], self.refs(third))

    def test_save_output_validates_and_finds_only_unexpired(self) -> None:
        identifier = self.save_media("d")
        with self.assertRaises(InvalidError):
            self.store.media().save_output(MediaOutput(platform="", media_id=identifier, kind="image"))
        with self.assertRaises(InvalidError):
            self.store.media().save_output(
                MediaOutput(platform="p", scope_id="s", message_id="m", segment_index=-1, kind="image", media_id=identifier)
            )
        with self.assertRaises(InvalidError):
            self.store.media().save_output(
                MediaOutput(platform="p", scope_id="s", message_id="m", segment_index=0, kind="", media_id=identifier)
            )

        self.store.media().save_output(
            MediaOutput(
                platform="p",
                scope_id="s",
                message_id="m",
                segment_index=0,
                kind="image",
                media_id=identifier,
                expires_at=now() - timedelta(seconds=1),
            )
        )
        self.assertEqual([], self.store.media().find_outputs("p", "s", "m", now()))
        self.assertEqual(1, len(self.refs(identifier)))  # 还没清理就还有引用
        self.store.media().expire_outputs(now())
        self.assertEqual([], self.refs(identifier))


class TestMediaHistory(SqliteMediaTestCase):
    def test_history_references_are_ordered_and_replaced_atomically(self) -> None:
        identifier = self.save_media("a")
        item = HistoryMedia(
            history_id="history-1",
            platform="qqonebot",
            scope_id="group:1",
            message_id="message",
            kind="image",
            media_id=identifier,
        )
        for index in (3, 1, 2):
            item.media_index = index
            self.store.media().save_history(item)

        found = self.store.media().find_history("qqonebot", "group:1", "message")
        self.assertEqual([1, 2, 3], [row.media_index for row in found])
        for row in found:
            self.assertEqual(identifier, row.media_id)
            self.assertTrue(row.owner_id)
            self.assertEqual("history-1", row.history_id)
        self.assertEqual(3, len(self.refs(identifier)))
        self.assertEqual([], self.store.media().check_references())
        self.assertEqual([], self.store.media().find_history("qqonebot", "group:2", "message"))

        # 增量替换换新 owner：旧 owner 的迟到对账删不掉新关联。
        original_owner = found[0].owner_id
        item.media_index = 1
        self.store.media().save_history(item)
        refreshed = self.store.media().find_history("qqonebot", "group:1", "message")
        self.assertEqual(3, len(refreshed))
        self.assertNotEqual(original_owner, refreshed[0].owner_id)
        self.assertEqual(3, len(self.refs(identifier)))

    def test_replacing_with_a_missing_media_rolls_back_association_and_reference(self) -> None:
        identifier = self.save_media("a")
        item = HistoryMedia(
            history_id="history-1",
            platform="qqonebot",
            scope_id="group:1",
            message_id="message",
            media_index=1,
            kind="image",
            media_id=identifier,
        )
        self.store.media().save_history(item)

        item.media_id = media_id("b")
        with self.assertRaises(SQLiteError):
            self.store.media().save_history(item)
        found = self.store.media().find_history("qqonebot", "group:1", "message")
        self.assertEqual(1, len(found))
        self.assertEqual(identifier, found[0].media_id)
        self.assertEqual(1, len(self.refs(identifier)))

    def test_history_validation_paging_and_deletion(self) -> None:
        identifier = self.save_media("c")
        base = HistoryMedia(
            history_id="h",
            platform="p",
            scope_id="s",
            message_id="m",
            media_index=1,
            kind="image",
            media_id=identifier,
        )
        for index in (-1, 0):
            base.media_index = index
            with self.assertRaises(InvalidError):
                self.store.media().save_history(base)
        base.media_index = 1
        bad_kind = HistoryMedia(
            history_id="h",
            platform="p",
            scope_id="s",
            message_id="m",
            media_index=1,
            kind="video",
            media_id=identifier,
        )
        with self.assertRaises(InvalidError):
            self.store.media().save_history(bad_kind)
        for field in ("history_id", "platform", "scope_id", "message_id", "media_id"):
            broken = HistoryMedia(
                history_id="h",
                platform="p",
                scope_id="s",
                message_id="m",
                media_index=1,
                kind="image",
                media_id=identifier,
            )
            setattr(broken, field, "")
            with self.assertRaises(InvalidError):
                self.store.media().save_history(broken)

        for index in (1, 2, 3):
            entry = HistoryMedia(
                history_id=f"h{index}",
                platform="p",
                scope_id=f"scope:{index}",
                message_id="m",
                media_index=1,
                kind="file",
                media_id=identifier,
            )
            self.store.media().save_history(entry)
        self.store.media_references().add(
            MediaReference(media_id=identifier, owner_type="test", owner_id="independent", purpose="content")
        )

        page = self.store.media().list_history("", 2)
        self.assertEqual(2, len(page))
        tail = self.store.media().list_history(page[1].owner_id, 2)
        self.assertEqual(3, len(page) + len(tail))
        for row in [*page, *tail]:
            self.store.media().delete_history(row.owner_id)
        refs = self.refs(identifier)
        self.assertEqual(1, len(refs))
        self.assertEqual("independent", refs[0].owner_id)
        self.assertEqual([], self.store.media().claim_orphans(now()))

    def test_chat_history_trigger_sync_with_media_history(self) -> None:
        """``media_history`` 被删 → 触发器释放 ``media_references``（v16）。"""
        identifier = self.save_media("d")
        item = HistoryMedia(
            history_id="history-9",
            platform="qqonebot",
            scope_id="group:9",
            message_id="message-9",
            media_index=1,
            kind="record",
            media_id=identifier,
        )
        self.store.media().save_history(item)
        refs = self.refs(identifier)
        self.assertEqual(["chat_history"], [row.owner_type for row in refs])
        self.assertEqual("content", refs[0].purpose)
        self.assertEqual([], self.store.media().check_references())

        # 直接删 media_history 行（模拟历史清理）→ 引用由触发器同步删除。
        self.store.database.execute("DELETE FROM media_history WHERE owner_id = ?", (item.owner_id,))
        self.assertEqual([], self.refs(identifier))
        self.assertEqual([], self.store.media().check_references())


class TestCronMediaReferences(SqliteMediaTestCase):
    def test_delivery_cas_creates_and_replaces_cron_references(self) -> None:
        first = self.save_media("a")
        second = self.save_media("b")
        job = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(name="user.cron", handler="h", schedule="0 * * * *", enabled=True)
        )

        self.assertTrue(self.store.cron_jobs().compare_and_swap_delivery(job.id, "", "first", cron_delivery_state(first)))
        refs = self.store.media_references().list_by_owner("cron", job.id)
        self.assertEqual([first], [row.media_id for row in refs])
        self.assertEqual("report", refs[0].purpose)
        self.assertEqual([], self.store.media().check_references())

        self.assertTrue(
            self.store.cron_jobs().compare_and_swap_delivery(job.id, "first", "second", cron_delivery_state(second))
        )
        self.assertEqual([second], [row.media_id for row in self.store.media_references().list_by_owner("cron", job.id)])
        self.assertEqual([], self.refs(first))

        with self.assertRaises(SQLiteError):
            self.store.cron_jobs().compare_and_swap_delivery(
                job.id, "second", "bad", cron_delivery_state("media:missing")
            )
        self.assertEqual([second], [row.media_id for row in self.store.media_references().list_by_owner("cron", job.id)])

        # 清空投递状态 → 引用全部释放。
        self.assertTrue(self.store.cron_jobs().compare_and_swap_delivery(job.id, "second", "", ""))
        self.assertEqual([], self.store.media_references().list_by_owner("cron", job.id))
        self.assertEqual([], self.store.media().check_references())

    def test_deleting_the_job_releases_cron_references(self) -> None:
        identifier = self.save_media("c")
        job = self.store.cron_jobs().upsert(
            UpsertCronJobRequest(name="user.cron", handler="h", schedule="0 * * * *", enabled=True)
        )
        state = cron_delivery_state(identifier)
        self.assertTrue(self.store.cron_jobs().compare_and_swap_delivery(job.id, "", "run", state))
        self.assertEqual([identifier], [row.media_id for row in self.store.media_references().list_by_owner("cron", job.id)])

        self.store.cron_jobs().delete_by_name(job.name)
        self.assertEqual([], self.refs(identifier))


class TestMediaRecoveryAndAudit(SqliteMediaTestCase):
    def test_recover_interrupted_clears_temporary_references_and_fails_half_done_events(self) -> None:
        identifier = self.save_media("a")
        for owner_type in ("hook", "skill", "request", "output_send"):
            self.store.media_references().add(
                MediaReference(
                    media_id=identifier, owner_type=owner_type, owner_id=owner_type, purpose="temporary"
                )
            )
        self.store.media_references().add(
            MediaReference(media_id=identifier, owner_type="hook", owner_id="kept", purpose="content")
        )
        event = self.store.atrinis_events().create(
            CreateAtrinisEventRequest(
                event_key="k",
                token_name="t",
                atriwisp_name="demo",
                source="s",
                source_id="i",
                mode="direct",
                content_hash="h",
                status="running",
            )
        )
        self.store.media().recover_interrupted()

        remaining = self.refs(identifier)
        self.assertEqual(["kept"], [row.owner_id for row in remaining])
        recovered = self.store.atrinis_events().get(event.id)
        self.assertEqual("failed", recovered.status)
        self.assertEqual("interrupted before durable report", recovered.error)

    def test_check_references_reports_corruption_without_repairing(self) -> None:
        identifier = self.save_media("b")
        session = Session(owner_id="u", platform="p", platform_scope_id="s")
        self.store.sessions().create(session)
        message = Message(session_id=session.id, role=RoleUser, content="", segments=image_segments(identifier))
        self.store.messages().append(message)
        self.assertEqual([], self.store.media().check_references())

        self.store.database.execute("DELETE FROM media_references WHERE owner_id = ?", (message.id,))
        self.assertEqual([f"missing message reference: {message.id}"], self.store.media().check_references())
        # 审计只读：消息与媒体都还在。
        self.assertEqual(identifier, self.store.media().get(identifier).id)
        self.assertEqual(message.id, self.store.messages().get(message.id).id)

    def test_check_references_detects_orphan_output_and_history_rows(self) -> None:
        identifier = self.save_media("c")
        self.store.media().save_output(
            MediaOutput(
                platform="p",
                scope_id="s",
                message_id="m",
                segment_index=0,
                kind="image",
                media_id=identifier,
                expires_at=now() + timedelta(hours=1),
            )
        )
        output = self.store.media().find_outputs("p", "s", "m", now())[0]
        self.store.database.execute("DELETE FROM media_references WHERE owner_type='output'")
        self.assertEqual([f"missing output reference: {output.owner_id}"], self.store.media().check_references())

        self.store.media().save_history(
            HistoryMedia(
                history_id="h",
                platform="p",
                scope_id="s",
                message_id="m",
                media_index=1,
                kind="image",
                media_id=identifier,
            )
        )
        history = self.store.media().find_history("p", "s", "m")[0]
        self.store.database.execute("DELETE FROM media_references WHERE owner_type='chat_history'")
        issues = self.store.media().check_references()
        self.assertIn(f"missing history reference: {history.owner_id}", issues)

    def test_dangling_owner_reference_is_reported(self) -> None:
        identifier = self.save_media("d")
        self.store.media_references().add(
            MediaReference(media_id=identifier, owner_type="chat_history", owner_id="ghost", purpose="content")
        )
        self.assertEqual(
            ["dangling owner reference: chat_history:ghost"], self.store.media().check_references()
        )


class TestMediaClaimRace(SqliteMediaTestCase):
    def test_reference_add_race_with_claim_keeps_the_invariant(self) -> None:
        """引用插入与清理认领二者必居其一：要么引用在、要么媒体进入 deleting。"""
        for index in range(12):
            letter = format(index, "x")
            identifier = self.save_media(letter, age=timedelta(hours=2))
            ready = threading.Event()
            outcome: list[str] = []
            failure: list[BaseException] = []

            def add_reference(media: str = identifier, gate: threading.Event = ready) -> None:
                gate.wait()
                try:
                    self.store.media_references().add(
                        MediaReference(media_id=media, owner_type="test", owner_id=media, purpose="content")
                    )
                    outcome.append("added")
                except BaseException as exc:  # noqa: BLE001 - 守卫触发器拒绝也算预期结果
                    failure.append(exc)

            thread = threading.Thread(target=add_reference)
            thread.start()
            ready.set()
            claimed = self.store.media().claim_orphans(now())
            thread.join()

            loaded = self.store.media().get(identifier)
            refs = self.refs(identifier)
            if refs:
                # 引用先落地：媒体不可能在同一次认领里被标记删除。
                self.assertEqual(1, len(refs), f"index={index}")
                self.assertEqual([], claimed, f"index={index}")
                self.assertFalse(loaded.deleting, f"index={index}")
                self.assertEqual([], failure, f"index={index}")
                self.assertEqual(["added"], outcome, f"index={index}")
            else:
                # 认领先落地：守卫触发器拒绝迟到的引用。
                self.assertEqual([identifier], [row.id for row in claimed], f"index={index}")
                self.assertTrue(loaded.deleting, f"index={index}")
                self.assertEqual(1, len(failure), f"index={index}")
                self.assertIsInstance(failure[0], SQLiteError, f"index={index}")
            self.store.media().finish_delete(identifier)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
