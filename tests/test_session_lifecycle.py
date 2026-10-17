"""Session 生命周期、模式、Fork、后台接管、命名与闲置过期测试。

覆盖上游 ``internal/session`` 的 ``lifecycle_test.go`` / ``mode_test.go`` /
``fork_test.go`` / ``promotion_test.go`` / ``naming_test.go``，以及上游没有单测、但
``devdocs/architecture.md``（locator:session）明确要求的闲置过期 TTL 矩阵与
"过期只清 current、不删持久行"语义。

夹具（真实 SQLite 的 Store）与等待 helper 复用 :mod:`tests.test_session`，避免复制
一份存储实现。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.errors import ErrChatModeRequiresEmptySession, InvalidError
from atri_x.session import (
    ActivateModeRequest,
    CreateRequest,
    ExpireIdleRequest,
    IdleExpirationConfig,
    NamingConfig,
    Scope,
    TitleResult,
    is_background,
    new_service_with_naming,
    was_promoted,
)
from atri_x.session.promotion import session_metadata_fields
from atri_x.storage.models import (
    ListSessionsRequest,
    Media,
    MediaReference,
    Message,
    RoleAssistant,
    RoleTool,
    RoleUser,
    Session,
    SessionModeChat,
    SessionModeWork,
    SessionStatusActive,
)
from atri_x.storage.times import now
from tests.test_session import (
    FakeNamingNotifier,
    FakeTitleGenerator,
    SqliteTestCase,
    Store,
    cli_scope,
    ensure_no_generator_call,
    new_test_service,
    new_test_store,
    session_ids,
    wait_generator_call,
    wait_title,
)


def new_background_session(
    store: Store, scope_id: str, kind: str, metadata: str = ""
) -> Session:
    """对应 Go ``newBackgroundSession``。"""
    if not metadata:
        metadata = json.dumps({"background_kind": kind}, separators=(",", ":"))
    row = Session(
        owner_id="u1",
        platform="qq",
        platform_scope_id=scope_id,
        mode=SessionModeWork,
        status=SessionStatusActive,
        title="background",
        metadata=metadata,
    )
    store.sessions().create(row)
    return row


class TouchingStore:
    """包装 Store：每次 ``mutate`` 之前先由"另一个写入者"刷新一次活跃时间。

    用来复现 ``ExpireIdleCurrent`` 的"事务内基于最新行重新判定"分支。
    """

    def __init__(self, base: Store) -> None:
        self._base = base
        self._sessions = _TouchingSessions(base.sessions(), base)

    def sessions(self):
        return self._sessions

    def __getattr__(self, name):
        return getattr(self._base, name)


class _TouchingSessions:
    def __init__(self, inner, base: Store) -> None:
        self._inner = inner
        self._base = base

    def mutate(self, session_id, update):
        self._base.sessions().mutate(
            session_id, lambda current: setattr(current, "updated_at", now())
        )
        return self._inner.mutate(session_id, update)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestServiceLifecycleOperations(SqliteTestCase):
    """对应 Go ``TestServiceLifecycleOperations``。"""

    def test_lifecycle_operations(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()

        session = svc.create(scope, CreateRequest(title="lifecycle"))
        archived = svc.archive(scope, "")
        self.assertEqual(archived.id, session.id)
        self.assertIsNotNone(archived.archived_at)

        unarchived = svc.unarchive(scope, "")
        self.assertIsNone(unarchived.archived_at)

        pinned = svc.pin(scope, session.id)
        self.assertIsNotNone(pinned.pinned_at)
        unpinned = svc.unpin(scope, session.id)
        self.assertIsNone(unpinned.pinned_at)

        svc.delete(scope, session.id)
        with self.assertRaises(Exception):
            store.sessions().get(session.id)
        new_session = svc.get_or_create_current(scope, "after delete")
        self.assertNotEqual(new_session.id, session.id)

    def test_archive_and_pin_are_idempotent_inside_transaction(self) -> None:
        """归档/置顶第二次调用不会改写时间戳（事务内短路）。"""
        svc, _ = new_test_service()
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="idempotent"))

        first = svc.archive(scope, session.id)
        second = svc.archive(scope, session.id)
        self.assertEqual(first.archived_at, second.archived_at)

        pinned_first = svc.pin(scope, session.id)
        pinned_second = svc.pin(scope, session.id)
        self.assertEqual(pinned_first.pinned_at, pinned_second.pinned_at)

    def test_list_page_pagination_and_archived_only(self) -> None:
        """分页 + 归档过滤 + SQLite 侧的 message_count / 预览。"""
        svc, store = new_test_service()
        scope = cli_scope()
        for index in range(3):
            created = svc.create(scope, CreateRequest(title=f"session {index}"))
            store.messages().append(
                Message(session_id=created.id, role=RoleUser, content=f"user {index}")
            )
            store.messages().append(
                Message(session_id=created.id, role=RoleAssistant, content=f"bot {index}")
            )
        archived = svc.create(scope, CreateRequest(title="archived only"))
        store.messages().append(
            Message(session_id=archived.id, role=RoleUser, content="archived user")
        )
        svc.archive(scope, archived.id)

        first_page = svc.list_page(scope, "", 2, 0, False)
        second_page = svc.list_page(scope, "", 2, 2, False)
        all_rows = svc.list_page(scope, "", 20, 0, False)
        self.assertEqual(len(first_page), 2)
        self.assertEqual(len(second_page), 1)
        self.assertEqual(
            session_ids(first_page) | session_ids(second_page), session_ids(all_rows)
        )
        newest = next(item for item in all_rows if item.title == "session 2")
        self.assertEqual(newest.message_count, 2)
        self.assertEqual(newest.last_user_preview, "user 2")
        self.assertEqual(newest.last_bot_preview, "bot 2")

        archived_only = svc.list_page(scope, "", 20, 0, True)
        self.assertEqual([item.id for item in archived_only], [archived.id])
        self.assertNotIn(archived.id, session_ids(svc.list_page(scope, "", 20, 0, False)))

    def test_cleanup_expired_deletes_only_unpinned_unarchived(self) -> None:
        """``CleanupExpired`` 只删未归档、未置顶且 updated_at 早于 cutoff 的行。"""
        svc, store = new_test_service()
        scope = cli_scope()
        moment = now()
        old = moment - timedelta(days=30)

        expired = svc.create(scope, CreateRequest(title="expired"))
        pinned = svc.create(scope, CreateRequest(title="pinned"))
        archived = svc.create(scope, CreateRequest(title="archived"))
        fresh = svc.create(scope, CreateRequest(title="fresh"))
        for session in (expired, pinned, archived):
            session.updated_at = old
            store.sessions().update(session)
        svc.pin(scope, pinned.id)
        svc.archive(scope, archived.id)

        deleted = svc.cleanup_expired(moment - timedelta(days=1))
        self.assertEqual(deleted, 1)
        with self.assertRaises(Exception):
            store.sessions().get(expired.id)
        for session in (pinned, archived, fresh):
            self.assertEqual(store.sessions().get(session.id).id, session.id)


class TestServiceActivateMode(SqliteTestCase):
    """对应 Go ``mode_test.go`` 的三个用例。"""

    def test_activate_mode_creates_and_reuses_mode_session(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        activated = svc.activate_mode(
            scope, ActivateModeRequest(mode=SessionModeChat, new_session_title="chat")
        )
        self.assertFalse(activated.already_active)
        self.assertEqual(activated.session.mode, SessionModeChat)
        self.assertEqual(activated.session.title, "chat")

        again = svc.activate_mode(scope, ActivateModeRequest(mode=SessionModeChat))
        self.assertTrue(again.already_active)
        self.assertEqual(again.session.id, activated.session.id)
        self.assertEqual(store.sessions().get(activated.session.id).mode, SessionModeChat)

        with self.assertRaises(InvalidError):
            svc.activate_mode(scope, ActivateModeRequest(mode="invalid"))

    def test_activate_mode_changes_empty_session(self) -> None:
        svc, _ = new_test_service()
        scope = cli_scope()
        work = svc.create(scope, CreateRequest(title="mode test"))
        activated = svc.activate_mode(scope, ActivateModeRequest(mode=SessionModeChat))
        self.assertFalse(activated.already_active)
        self.assertEqual(activated.session.id, work.id)

    def test_activate_chat_rejects_work_session_with_history(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        work = svc.create(scope, CreateRequest(title="mode test"))
        store.messages().append(
            Message(session_id=work.id, role=RoleUser, content="history")
        )
        with self.assertRaises(Exception) as caught:
            svc.activate_mode(scope, ActivateModeRequest(mode=SessionModeChat))
        self.assertIs(caught.exception, ErrChatModeRequiresEmptySession)
        self.assertEqual(store.sessions().get(work.id).mode, SessionModeWork)


class TestServiceFork(SqliteTestCase):
    """对应 Go ``fork_test.go`` 的三个用例。"""

    def test_fork_creates_current_branch(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        parent = svc.create(
            scope, CreateRequest(title="parent session", mode=SessionModeChat)
        )
        assistant = Message(session_id=parent.id, role=RoleAssistant, content="answer")
        store.messages().append(assistant)

        fork = svc.fork(scope, assistant.id)
        self.assertEqual(fork.parent_session_id, parent.id)
        self.assertEqual(fork.fork_from_message_id, assistant.id)
        self.assertEqual(fork.mode, SessionModeChat)
        self.assertEqual(fork.title, "Fork: parent session")
        self.assertEqual(fork.owner_id, scope.actor_id)
        self.assertEqual(fork.platform, scope.platform)
        self.assertEqual(fork.platform_scope_id, scope.platform_scope_id)
        self.assertEqual(svc.current(scope).id, fork.id)

    def test_fork_rejects_non_assistant_message(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="parent"))
        user = Message(session_id=session.id, role=RoleUser, content="question")
        store.messages().append(user)
        with self.assertRaises(Exception):
            svc.fork(scope, user.id)
        with self.assertRaises(InvalidError):
            svc.fork(scope, "   ")

    def test_fork_checks_scope(self) -> None:
        svc, store = new_test_service()
        owner_scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")
        parent = svc.create(owner_scope, CreateRequest(title="parent"))
        assistant = Message(session_id=parent.id, role=RoleAssistant, content="answer")
        store.messages().append(assistant)
        other_scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:2")
        with self.assertRaises(Exception):
            svc.fork(other_scope, assistant.id)
        svc.fork(cli_scope(), assistant.id)

    def test_fork_message_range_and_media_inheritance_follow_checkpoint(self) -> None:
        """Fork 的消息范围、检查点边界与媒体引用继承。

        ``Fork`` 只把检查点写进 repository（``parent_session_id`` +
        ``fork_from_message_id``）；范围与媒体继承由存储侧支持：Go/Python 都是
        ``ListBySessionUpTo`` 与 migrations v17 的 ``media_fork_history`` 视图 +
        ``media_fork_created`` 触发器（本套件用真实的 ``atri_x.storage.sqlite``，
        验的是真实触发器行为）。断言三件事：
        1. 传给 ``sessions().create`` 的 Fork 行字段（检查点）；
        2. ``list_by_session_up_to`` / ``list_by_session_after`` 的范围边界；
        3. 继承到的媒体引用不含检查点之后的媒体，且嵌套 Fork 继续继承。
        """
        svc, store = new_test_service()
        scope = cli_scope()
        parent = svc.create(scope, CreateRequest(title="parent", mode=SessionModeWork))

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        media_ids = []
        messages = []
        for index, mark in enumerate(("a", "b", "c")):
            media_id = "media:" + mark * 64
            store.media().upsert(Media(id=media_id, backend="local", mime_type="image/png"))
            media_ids.append(media_id)
            message = Message(
                session_id=parent.id,
                role=RoleAssistant,
                content=f"answer {mark}",
                created_at=base + timedelta(seconds=index),
            )
            store.messages().append(message)
            messages.append(message)
            store.media_references().add(
                MediaReference(
                    media_id=media_id,
                    owner_type="message",
                    owner_id=message.id,
                    purpose="content",
                    session_id=parent.id,
                    created_at=message.created_at,
                )
            )
        # session_tool 引用：检查点前后各一条（v17 的 ``created_at <= checkpoint`` 分支）。
        tool_before = "media:" + "d" * 64
        tool_after = "media:" + "e" * 64
        for media_id in (tool_before, tool_after):
            store.media().upsert(Media(id=media_id, backend="local", mime_type="image/png"))
        checkpoint_at = messages[1].created_at
        store.media_references().add_all(
            [
                MediaReference(
                    media_id=tool_before,
                    owner_type="session_tool",
                    owner_id=parent.id,
                    purpose="input",
                    session_id=parent.id,
                    created_at=checkpoint_at - timedelta(seconds=1),
                ),
                MediaReference(
                    media_id=tool_after,
                    owner_type="session_tool",
                    owner_id=parent.id,
                    purpose="input",
                    session_id=parent.id,
                    created_at=checkpoint_at + timedelta(seconds=1),
                ),
            ]
        )

        fork = svc.fork(scope, messages[1].id)

        # 1. 检查点字段就是 Fork 落库的参数（fork 对象即传给 create 的那一行）
        stored = store.sessions().get(fork.id)
        self.assertEqual(stored.parent_session_id, parent.id)
        self.assertEqual(stored.fork_from_message_id, messages[1].id)

        # 2. 消息范围：截至检查点（含）与检查点之后
        up_to = store.messages().list_by_session_up_to(parent.id, messages[1].id)
        self.assertEqual([item.id for item in up_to], [messages[0].id, messages[1].id])
        after = store.messages().list_by_session_after(parent.id, messages[1].id)
        self.assertEqual([item.id for item in after], [messages[2].id])

        # 3. 媒体引用继承：检查点之后的消息媒体不继承
        inherited = {
            reference.media_id
            for reference in store.media_references().list_by_owner("session_fork", fork.id)
        }
        self.assertEqual(
            inherited, {media_ids[0], media_ids[1], tool_before}
        )
        self.assertNotIn(media_ids[2], inherited)
        self.assertNotIn(tool_after, inherited)
        for reference in store.media_references().list_by_owner("session_fork", fork.id):
            self.assertEqual(reference.session_id, fork.id)
            self.assertEqual(reference.purpose, "history")

        # 嵌套 Fork 继续继承上一层的 session_fork 引用
        checkpoint = Message(session_id=fork.id, role=RoleAssistant, content="checkpoint")
        store.messages().append(checkpoint)
        nested = svc.fork(scope, checkpoint.id)
        nested_refs = {
            reference.media_id
            for reference in store.media_references().list_by_owner("session_fork", nested.id)
        }
        self.assertTrue(inherited.issubset(nested_refs))


class TestIsBackgroundDetectsScopeAndMetadata(SqliteTestCase):
    """对应 Go ``TestIsBackgroundDetectsScopeAndMetadata``。"""

    def test_is_background(self) -> None:
        store = new_test_store()
        by_kind = new_background_session(
            store, "group:1", "cron", '{"background_kind":"cron"}'
        )
        by_scope = new_background_session(store, "atrinis:watcher", "atrinis", "{}")
        plain = Session(
            owner_id="u1", platform="qq", platform_scope_id="group:1", mode=SessionModeWork
        )
        store.sessions().create(plain)

        for name, row, want in (
            ("metadata kind", by_kind, True),
            ("scope prefix", by_scope, True),
            ("plain", plain, False),
        ):
            with self.subTest(name):
                self.assertEqual(is_background(row), want)
                self.assertFalse(was_promoted(row))

    def test_cron_scope_prefix_is_detected(self) -> None:
        store = new_test_store()
        cron = new_background_session(store, "cron:report", "cron", "{}")
        self.assertTrue(is_background(cron))


class TestResumePromotesBackgroundSessionIntoForegroundScope(SqliteTestCase):
    """对应 Go ``TestResumePromotesBackgroundSessionIntoForegroundScope``。"""

    def test_resume_promotes(self) -> None:
        svc, store = new_test_service()
        background = new_background_session(
            store,
            "cron:report",
            "cron",
            '{"background_kind":"cron","cron_job_name":"report","unknown_key":{"nested":1}}',
        )
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")
        self.assertTrue(is_background(background))

        promoted = svc.resume(scope, background.id)
        self.assertTrue(was_promoted(promoted))
        self.assertFalse(is_background(promoted))
        self.assertEqual(promoted.owner_id, "u1")
        self.assertEqual(promoted.platform, "qq")
        self.assertEqual(promoted.platform_scope_id, "group:1")
        self.assertEqual(promoted.mode, SessionModeWork)

        fields = session_metadata_fields(promoted.metadata)
        self.assertNotIn("background_kind", fields)
        origin = json.loads(fields["foreground_origin"])
        self.assertEqual(
            origin,
            {"kind": "cron", "owner_id": "u1", "platform": "qq", "scope_id": "cron:report"},
        )
        self.assertEqual(fields["cron_job_name"], '"report"')
        self.assertEqual(fields["unknown_key"], '{"nested":1}')

        reloaded = store.sessions().get(background.id)
        self.assertTrue(was_promoted(reloaded))
        self.assertEqual(reloaded.platform_scope_id, "group:1")
        self.assertEqual(svc.current(scope).id, background.id)

    def test_can_access_allows_background_session_across_scope(self) -> None:
        """``can_access`` 用 ``is_background`` 放行后台 Session 的跨 scope 恢复。"""
        svc, store = new_test_service()
        background = new_background_session(store, "cron:report", "cron")
        other_scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:9")
        self.assertTrue(svc.can_access(other_scope, background))
        svc.resume(other_scope, background.id)
        self.assertFalse(is_background(store.sessions().get(background.id)))
        # 提升之后就属于 group:9 了：别的 scope 不再放行
        third = Scope(actor_id="u1", platform="qq", platform_scope_id="group:8")
        self.assertFalse(svc.can_access(third, store.sessions().get(background.id)))
        with self.assertRaises(Exception):
            svc.resume(third, background.id)


class TestResumePromotionIsIdempotentAndScopeBound(SqliteTestCase):
    """对应 Go ``TestResumePromotionIsIdempotentAndScopeBound``。"""

    def test_promotion_idempotent_and_scope_bound(self) -> None:
        svc, store = new_test_service()
        background = new_background_session(store, "cron:report", "cron")
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")

        first = svc.resume(scope, background.id)
        second = svc.resume(scope, background.id)
        first_origin = json.loads(session_metadata_fields(first.metadata)["foreground_origin"])
        second_origin = json.loads(session_metadata_fields(second.metadata)["foreground_origin"])
        self.assertEqual(first_origin, second_origin)
        self.assertEqual(second_origin["scope_id"], "cron:report")

        other_group = Scope(actor_id="u1", platform="qq", platform_scope_id="group:2")
        with self.assertRaises(Exception):
            svc.resume(other_group, background.id)
        other_actor = Scope(actor_id="u2", platform="qq", platform_scope_id="group:1")
        with self.assertRaises(Exception):
            svc.resume(other_actor, background.id)


class TestResumeKeepsNormalSessionMetadata(SqliteTestCase):
    """对应 Go ``TestResumeKeepsNormalSessionMetadata``。"""

    def test_plain_session_untouched(self) -> None:
        svc, store = new_test_service()
        plain = Session(
            owner_id="u1",
            platform="cli",
            platform_scope_id="local",
            mode=SessionModeChat,
            status=SessionStatusActive,
            title="plain",
            metadata='{"tool_tags":["web"]}',
        )
        store.sessions().create(plain)
        resumed = svc.resume(cli_scope(), plain.id)
        self.assertFalse(was_promoted(resumed))
        self.assertFalse(is_background(resumed))
        self.assertEqual(resumed.metadata, '{"tool_tags":["web"]}')
        self.assertEqual(resumed.mode, SessionModeChat)


class TestUnarchivePromotesBackgroundSession(SqliteTestCase):
    """对应 Go ``TestUnarchivePromotesBackgroundSession``。"""

    def test_unarchive_promotes(self) -> None:
        svc, store = new_test_service()
        background = new_background_session(store, "atrinis:watcher", "atrinis")
        background.archived_at = now()
        store.sessions().update(background)
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")

        restored = svc.unarchive(scope, background.id)
        self.assertIsNone(restored.archived_at)
        self.assertTrue(was_promoted(restored))
        self.assertFalse(is_background(restored))
        self.assertEqual(restored.platform_scope_id, "group:1")
        self.assertEqual(restored.mode, SessionModeWork)
        self.assertEqual(svc.current(scope).id, background.id)


class TestServiceRename(SqliteTestCase):
    """对应 Go ``TestServiceRenameMarksManualTitleAndPreservesMetadata``。"""

    def test_rename_marks_manual_title_and_preserves_metadata(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="old title"))
        session.metadata = '{"discovered_tools":["web_search"]}'
        store.sessions().update(session)

        renamed = svc.rename(scope, session.id, "new title")
        self.assertEqual(renamed.title, "new title")
        metadata = json.loads(renamed.metadata)
        self.assertEqual(metadata["discovered_tools"], ["web_search"])
        self.assertTrue(metadata["title_renamed"])
        self.assertEqual(metadata["title_source"], "manual")
        with self.assertRaises(InvalidError):
            svc.rename(scope, session.id, "   ")

    def test_rename_rejects_broken_metadata(self) -> None:
        """metadata 不是合法 JSON 时重命名在事务内失败并回滚。"""
        svc, store = new_test_service()
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="old title"))
        session.metadata = "{not json"
        store.sessions().update(session)
        with self.assertRaises(InvalidError):
            svc.rename(scope, session.id, "new title")
        self.assertEqual(store.sessions().get(session.id).title, "old title")


class TestServiceNaming(SqliteTestCase):
    """对应 Go ``naming_test.go`` 的用例集合。"""

    def test_rename_skips_automatic_naming(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, None)
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="old title"))
        svc.rename(scope, session.id, "manual title")
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="hello")
        )
        svc.maybe_schedule_naming(session.id)
        ensure_no_generator_call(generator)

    def test_naming_trigger_steps(self) -> None:
        cases = (
            (
                "after first user message",
                1,
                [(RoleUser, "hello")],
            ),
            (
                "after first assistant answer",
                2,
                [(RoleUser, "hello"), (RoleAssistant, "hi")],
            ),
            (
                "after second user message",
                3,
                [(RoleUser, "hello"), (RoleAssistant, "hi"), (RoleUser, "again")],
            ),
        )
        for name, trigger_step, messages in cases:
            with self.subTest(name):
                store = new_test_store()
                generator = FakeTitleGenerator(titles=["generated title"])
                svc = new_service_with_naming(
                    store, NamingConfig(trigger_step=trigger_step), generator, None
                )
                session = svc.create(cli_scope(), CreateRequest(title="fallback"))
                for index, (role, content) in enumerate(messages):
                    store.messages().append(
                        Message(session_id=session.id, role=role, content=content)
                    )
                    if index < trigger_step - 1:
                        svc.maybe_schedule_naming(session.id)
                        ensure_no_generator_call(generator)
                svc.maybe_schedule_naming(session.id)
                seen = wait_generator_call(generator)
                self.assertEqual(len(seen), trigger_step)
                wait_title(store, session.id, "generated title")

    def test_maybe_schedule_naming_skips_renamed_title(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, None)
        session = svc.create(cli_scope(), CreateRequest(title="Cron title"))
        session.metadata = '{"title_renamed":true,"title_source":"cron"}'
        store.sessions().update(session)
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="hello")
        )
        svc.maybe_schedule_naming(session.id)
        ensure_no_generator_call(generator)

    def test_naming_failure_notifies_and_keeps_fallback_title(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator(errs=[RuntimeError("boom")])
        notifier = FakeNamingNotifier()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, notifier)
        session = svc.create(cli_scope(), CreateRequest(title="fallback"))
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="hello")
        )
        svc.maybe_schedule_naming(session.id)
        wait_generator_call(generator)
        self.assertTrue(notifier.failure_event.wait(2.0))
        event = notifier.failures[0]
        self.assertEqual(event.session_id, session.id)
        self.assertEqual(event.reason, "generate title")
        self.assertEqual(store.sessions().get(session.id).title, "fallback")

    def test_naming_failure_can_retry_and_success_stops_repeats(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator(
            titles=["", "retry title"], errs=[RuntimeError("first failure"), None]
        )
        notifier = FakeNamingNotifier()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, notifier)
        session = svc.create(cli_scope(), CreateRequest(title="fallback"))
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="hello")
        )
        svc.maybe_schedule_naming(session.id)
        wait_generator_call(generator)
        self.assertTrue(notifier.failure_event.wait(2.0))

        store.messages().append(
            Message(session_id=session.id, role=RoleAssistant, content="hi")
        )
        generator.called.clear()
        svc.maybe_schedule_naming(session.id)
        wait_generator_call(generator)
        wait_title(store, session.id, "retry title")

        generator.called.clear()
        svc.maybe_schedule_naming(session.id)
        ensure_no_generator_call(generator)

    def test_naming_uses_only_trigger_step_messages(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator(titles=["generated title"])
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, None)
        session = svc.create(cli_scope(), CreateRequest(title="fallback"))
        for role, content in (
            (RoleUser, "first user"),
            (RoleAssistant, "first assistant"),
            (RoleUser, "second user"),
        ):
            store.messages().append(
                Message(session_id=session.id, role=role, content=content)
            )
        svc.maybe_schedule_naming(session.id)
        seen = wait_generator_call(generator)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].content, "first user")

    def test_naming_placeholder_fallbacks_immediately(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator(titles=["New session", "should not be used"])
        notifier = FakeNamingNotifier()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, notifier)
        session = svc.create(cli_scope(), CreateRequest(title="New session"))
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="first user title")
        )
        svc.maybe_schedule_naming(session.id)
        wait_generator_call(generator)
        self.assertTrue(notifier.failure_event.wait(2.0))
        event = notifier.failures[0]
        self.assertEqual(event.reason, "invalid title")
        self.assertEqual(event.generated_title_normalized, "New session")
        self.assertEqual(event.generated_title_raw, "New session")
        self.assertTrue(event.fallback_applied)
        wait_title(store, session.id, "first user title")

        generator.called.clear()
        svc.maybe_schedule_naming(session.id)
        ensure_no_generator_call(generator)

    def test_naming_skips_blank_messages(self) -> None:
        store = new_test_store()
        generator = FakeTitleGenerator(titles=["generated title"])
        svc = new_service_with_naming(store, NamingConfig(trigger_step=2), generator, None)
        session = svc.create(cli_scope(), CreateRequest(title="fallback"))
        for role, content in (
            (RoleUser, ""),
            (RoleUser, "hello"),
            (RoleAssistant, "  \n\t  "),
            (RoleAssistant, "hi"),
        ):
            store.messages().append(
                Message(session_id=session.id, role=role, content=content)
            )
        svc.maybe_schedule_naming(session.id)
        seen = wait_generator_call(generator)
        self.assertEqual([item.content for item in seen], ["hello", "hi"])

    def test_late_naming_does_not_override_manual_rename(self) -> None:
        """迟到的命名写入必须在事务内让位给手动改名（``ErrTitleRenamed`` 语义）。

        上游没有直接测这条事务内守卫：这里在模型返回之后、写入之前插入一次手动改名，
        然后同步调用 ``generate_title``，断言标题仍是手动值、且不派发"命名完成"。
        """
        store = new_test_store()
        completions: list = []

        class RecordingNotifier(FakeNamingNotifier):
            def notify_naming_completed(self, event) -> None:
                completions.append(event)

        generator = FakeTitleGenerator(titles=["auto title"])
        notifier = RecordingNotifier()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, notifier)
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="old title"))
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="hello")
        )
        svc.rename(scope, session.id, "manual title")

        svc.generate_title(session.id, store.messages().list_by_session(session.id))

        stored = store.sessions().get(session.id)
        self.assertEqual(stored.title, "manual title")
        self.assertEqual(completions, [])
        self.assertTrue(svc.naming_states[session.id].done)

    def test_late_fallback_title_does_not_override_manual_rename(self) -> None:
        """fallback 标题同样要在事务内让位给手动改名。"""
        store = new_test_store()
        generator = FakeTitleGenerator(titles=["New session", "New session", "New session"])
        notifier = FakeNamingNotifier()
        svc = new_service_with_naming(store, NamingConfig(trigger_step=1), generator, notifier)
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="New session"))
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="first user title")
        )
        svc.rename(scope, session.id, "manual title")
        svc.generate_title(session.id, store.messages().list_by_session(session.id))
        self.assertEqual(store.sessions().get(session.id).title, "manual title")


class TestIdleExpiration(SqliteTestCase):
    """闲置过期 TTL 矩阵与"只清 current、不删持久行"语义。

    上游 ``internal/session`` 没有 expiration 的单测（``expiration_test.go`` 不存在），
    这些用例按 ``devdocs/architecture.md``（locator:session）与
    ``internal/session/expiration.go`` 的行为逐条补齐。
    """

    def _aged_service(self, scope: Scope, age: timedelta):
        svc, store = new_test_service()
        session = svc.create(scope, CreateRequest(title="idle"))
        session.updated_at = now() - age
        store.sessions().update(session)
        return svc, store, session

    def test_ttl_matrix_selects_expected_slot(self) -> None:
        cases = (
            ("group user", "group:9", False, "group_user_ttl_minutes", 10),
            ("group superadmin", "group:9", True, "group_superadmin_ttl_minutes", 20),
            ("supergroup user", "supergroup:9", False, "group_user_ttl_minutes", 10),
            ("supergroup superadmin", "supergroup:9", True, "group_superadmin_ttl_minutes", 20),
            ("private user", "private:9", False, "private_user_ttl_minutes", 30),
            ("private superadmin", "private:9", True, "private_superadmin_ttl_minutes", 40),
        )
        for name, scope_id, is_superadmin, expected_slot, ttl in cases:
            with self.subTest(name):
                config = IdleExpirationConfig(
                    group_user_ttl_minutes=10,
                    group_superadmin_ttl_minutes=20,
                    private_user_ttl_minutes=30,
                    private_superadmin_ttl_minutes=40,
                )
                scope = Scope(actor_id="u1", platform="qq", platform_scope_id=scope_id)
                self.assertEqual(config.ttl_minutes(scope, is_superadmin), ttl)
                self.assertEqual(getattr(config, expected_slot), ttl)

    def test_expire_idle_four_combinations(self) -> None:
        cases = (
            ("group user", "group:9", False, IdleExpirationConfig(group_user_ttl_minutes=15)),
            (
                "group superadmin",
                "group:9",
                True,
                IdleExpirationConfig(group_superadmin_ttl_minutes=15),
            ),
            (
                "private user",
                "private:9",
                False,
                IdleExpirationConfig(private_user_ttl_minutes=15),
            ),
            (
                "private superadmin",
                "private:9",
                True,
                IdleExpirationConfig(private_superadmin_ttl_minutes=15),
            ),
        )
        for name, scope_id, is_superadmin, config in cases:
            with self.subTest(name):
                scope = Scope(actor_id="u1", platform="qq", platform_scope_id=scope_id)
                svc, store, session = self._aged_service(scope, timedelta(minutes=20))
                result = svc.expire_idle_current(
                    ExpireIdleRequest(
                        scope=scope,
                        is_superadmin=is_superadmin,
                        config=config,
                        now=now(),
                    )
                )
                self.assertTrue(result.expired)
                self.assertEqual(result.session_id, session.id)
                self.assertEqual(result.ttl_minutes, 15)
                with self.assertRaises(Exception):
                    svc.current(scope)
                # 持久行仍在：过期只清内存 current
                self.assertEqual(store.sessions().get(session.id).id, session.id)

    def test_expire_idle_disabled_and_not_yet_idle(self) -> None:
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:9")

        # TTL 为 0 → 该组合禁用（默认配置里私聊超管就是 0）
        svc, _, session = self._aged_service(scope, timedelta(days=30))
        disabled = svc.expire_idle_current(
            ExpireIdleRequest(
                scope=scope, is_superadmin=True, config=IdleExpirationConfig(), now=now()
            )
        )
        self.assertFalse(disabled.expired)
        self.assertEqual(disabled.ttl_minutes, 0)
        self.assertEqual(svc.current(scope).id, session.id)

        # 未到 TTL → 不过期，也不改活跃时间
        svc, store, session = self._aged_service(scope, timedelta(minutes=5))
        fresh = svc.expire_idle_current(
            ExpireIdleRequest(
                scope=scope,
                is_superadmin=False,
                config=IdleExpirationConfig(group_user_ttl_minutes=15),
                now=now(),
            )
        )
        self.assertFalse(fresh.expired)
        self.assertEqual(store.sessions().get(session.id).updated_at, session.updated_at)

        # 另一个组合的配置不会串台（私聊 TTL 对群作用域无效）
        other = svc.expire_idle_current(
            ExpireIdleRequest(
                scope=scope,
                is_superadmin=False,
                config=IdleExpirationConfig(private_user_ttl_minutes=15),
                now=now(),
            )
        )
        self.assertFalse(other.expired)

        # 没有 current 时静默返回
        svc.reset_current(scope)
        self.assertFalse(
            svc.expire_idle_current(
                ExpireIdleRequest(
                    scope=scope,
                    config=IdleExpirationConfig(group_user_ttl_minutes=15),
                    now=now(),
                )
            ).expired
        )

    def test_expire_idle_rechecks_inside_transaction(self) -> None:
        """事务内基于最新行重新判定（``errSessionNotIdle`` 语义）。

        模拟并发写入者：包装后的 ``mutate`` 在被调用时先刷新一次 ``updated_at``，
        此时过期写入必须放弃（返回未过期并保留 current）。
        """
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:9")
        svc, store, session = self._aged_service(scope, timedelta(minutes=20))
        svc.store = TouchingStore(store)

        result = svc.expire_idle_current(
            ExpireIdleRequest(
                scope=scope,
                config=IdleExpirationConfig(group_user_ttl_minutes=15),
                now=now(),
            )
        )
        self.assertFalse(result.expired)
        self.assertEqual(svc.current(scope).id, session.id)

    def test_expired_then_next_message_creates_new_persisted_session(self) -> None:
        """过期与 ``/new`` 都只清 current；首条普通消息才创建并持久化新 Session。"""
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:9")
        svc, store, old = self._aged_service(scope, timedelta(minutes=20))
        result = svc.expire_idle_current(
            ExpireIdleRequest(
                scope=scope,
                config=IdleExpirationConfig(group_user_ttl_minutes=15),
                now=now(),
            )
        )
        self.assertTrue(result.expired)
        rows = store.sessions().list(
            ListSessionsRequest(
                actor_id="u1", platform="qq", platform_scope_id="group:9"
            )
        )
        self.assertEqual(len(rows), 1)

        created = svc.get_or_create_current(scope, "new message")
        self.assertNotEqual(created.id, old.id)
        self.assertEqual(created.title, "new message")
        self.assertEqual(store.sessions().get(old.id).id, old.id)

        # /new 语义：只清 current，持久行不变，随后新建
        svc.reset_current(scope)
        with self.assertRaises(Exception):
            svc.current(scope)
        again = svc.get_or_create_current(scope, "another")
        self.assertNotEqual(again.id, created.id)
        self.assertEqual(store.sessions().get(created.id).id, created.id)

    def test_resume_refreshes_last_active_time(self) -> None:
        """恢复历史 Session 时刷新活跃时间（架构文档要求）。"""
        svc, store = new_test_service()
        scope = cli_scope()
        first = svc.create(scope, CreateRequest(title="first"))
        svc.create(scope, CreateRequest(title="second"))
        aged = now() - timedelta(hours=3)
        first.updated_at = aged
        store.sessions().update(first)

        resumed = svc.resume(scope, first.id)
        self.assertGreater(resumed.updated_at, aged)
        self.assertEqual(store.sessions().get(first.id).updated_at, resumed.updated_at)

        touched = store.sessions().get(first.id)
        touched.updated_at = aged
        store.sessions().update(touched)
        svc.touch(touched)
        self.assertGreater(store.sessions().get(first.id).updated_at, aged)


class TestStatusAndStoreErrorPaths(SqliteTestCase):
    """``Status`` 与"没有 current"的边界（上游 ``query.go`` 行为）。"""

    def test_status_without_current(self) -> None:
        svc, _ = new_test_service()
        with self.assertRaises(Exception):
            svc.status(cli_scope())

    def test_status_previews_only_first_of_each_role(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        session = svc.create(scope, CreateRequest(title="status"))
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="old question")
        )
        store.messages().append(
            Message(session_id=session.id, role=RoleTool, content="tool noise")
        )
        store.messages().append(
            Message(session_id=session.id, role=RoleAssistant, content="old answer")
        )
        store.messages().append(
            Message(session_id=session.id, role=RoleUser, content="new question\nline2")
        )
        status = svc.status(scope)
        self.assertEqual(status.message_count, 4)
        self.assertEqual(status.last_user_preview, "new question line2")
        self.assertEqual(status.last_answer_preview, "old answer")

    def test_touch_requires_session_id(self) -> None:
        svc, _ = new_test_service()
        with self.assertRaises(Exception):
            svc.touch(None)
        with self.assertRaises(Exception):
            svc.touch(Session())


if __name__ == "__main__":
    unittest.main()
