"""Session 服务测试（创建/恢复/列表/状态/模式/Fork/命名/平台隔离）。

**夹具说明**：用已完成的真实实现 —— ``atri_x.storage.sqlite.new(":memory:")``
（标准库 ``sqlite3`` + 上游 migration，含 media 触发器/视图），因此"字段写入统一走
``SessionRepository.Mutate``"这条契约是真的：``mutate`` 是 ``BEGIN IMMEDIATE``
事务内的读-改-写，回调抛异常时事务回滚，读到的永远是事务内的最新行；``list`` 的
平台隔离、同平台 cron、归档过滤与"置顶优先"排序也由真实 SQL 负责。

本模块同时给 :mod:`tests.test_session_lifecycle` 与
:mod:`tests.test_resident_memory` 提供共享夹具（Store、Scope、等待 helper），避免
每个测试文件各写一份存储实现。

临时目录一律建在仓库根下（``os.makedirs`` + ``tearDown`` 里 ``shutil.rmtree``），
不用 ``tempfile.TemporaryDirectory()``（本机沙箱的 0o700 ACL 会让清理抛
``PermissionError``）。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import unittest
from datetime import timedelta

from atri_x.errors import ErrNotFound, InvalidError, NotFoundError
from atri_x.session import (
    ActivateModeRequest,
    Config,
    CreateRequest,
    NamingConfig,
    Scope,
    Service,
    TitleResult,
    new_service,
    new_service_with_config,
    new_service_with_naming,
)
from atri_x.storage import sqlite
from atri_x.storage.models import (
    ListSessionsRequest,
    Media,
    MediaReference,
    Message,
    RoleAssistant,
    RoleUser,
    Session,
    SessionModeChat,
    SessionModeWork,
    SessionStatusActive,
)
from atri_x.storage.sqlite import Store
from atri_x.storage.times import now

# ---------------------------------------------------------------------------
# 真实 SQLite 夹具
# ---------------------------------------------------------------------------

#: 本模块创建的 SQLite 连接（``tearDown`` 里统一关闭，避免 ResourceWarning）。
_OPEN_STORES: list[Store] = []


class SqliteTestCase(unittest.TestCase):
    """本套件的基类：用例结束后关闭它开出来的内存库。"""

    def tearDown(self) -> None:
        while _OPEN_STORES:
            _OPEN_STORES.pop().close()


def new_test_store() -> Store:
    """和 Go ``newTestStore`` 一样：每个用例一个内存库（真实 migration + 触发器）。"""
    store = sqlite.new(":memory:")
    _OPEN_STORES.append(store)
    return store


def new_test_service() -> tuple[Service, Store]:
    """和 Go ``newTestService`` 一样。"""
    store = new_test_store()
    return new_service(store), store


def cli_scope() -> Scope:
    return Scope(actor_id="u1", platform="cli", platform_scope_id="local", is_cli=True)


def session_ids(sessions) -> set[str]:
    """对应 Go ``sessionIDs``。"""
    return {session.id for session in sessions}


def create_temp_dir(name: str) -> str:
    """在仓库根下建临时目录（不用 tempfile，见模块 docstring）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    counter = create_temp_dir.counter = getattr(create_temp_dir, "counter", 0) + 1
    path = os.path.join(root, f".{name}-{os.getpid()}-{counter}")
    os.makedirs(path, exist_ok=True)
    return path


def remove_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class FakeTitleGenerator:
    """对应 Go ``fakeTitleGenerator``：记录调用，可排队标题与异常。"""

    def __init__(self, titles=None, errs=None) -> None:
        self.titles = list(titles or [])
        self.errs = list(errs or [])
        self.calls: list[list[Message]] = []
        self.called = threading.Event()

    def generate_title(self, messages: list[Message]) -> TitleResult:
        self.calls.append(list(messages))
        self.called.set()
        title = self.titles.pop(0) if self.titles else ""
        if self.errs:
            error = self.errs.pop(0)
            if error is not None:
                raise error
        return TitleResult(raw_title=title)


class FakeNamingNotifier:
    """对应 Go ``fakeNamingNotifier``：记录失败事件（用 Event 等待）。"""

    def __init__(self) -> None:
        self.failures: list = []
        self.failure_event = threading.Event()

    def notify_naming_scheduled(self, event) -> None:
        ...

    def notify_naming_completed(self, event) -> None:
        ...

    def notify_naming_failed(self, event) -> None:
        self.failures.append(event)
        self.failure_event.set()


def wait_title(store: Store, session_id: str, want: str, timeout: float = 2.0) -> None:
    """对应 Go ``waitTitle``：轮询直到标题变成期望值。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = store.sessions().get(session_id)
        if session.title == want:
            return
        time.sleep(0.01)
    raise AssertionError(f"title did not become {want!r}")


def wait_generator_call(generator: FakeTitleGenerator, timeout: float = 2.0) -> list[Message]:
    """对应 Go ``waitGeneratorCall``。"""
    if not generator.called.wait(timeout):
        raise AssertionError("timeout waiting for title generator")
    return generator.calls[0]


def ensure_no_generator_call(generator: FakeTitleGenerator, timeout: float = 0.1) -> None:
    """对应 Go ``ensureNoGeneratorCall``。"""
    if generator.called.wait(timeout):
        raise AssertionError(f"unexpected title generator call: {generator.calls}")


# ---------------------------------------------------------------------------
# 迁移自 internal/session/service_test.go
# ---------------------------------------------------------------------------


class TestServiceCreateCurrentResumeListStatus(SqliteTestCase):
    """对应 Go ``TestServiceCreateCurrentResumeListStatus``。"""

    def test_create_current_resume_list_status(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()

        first = svc.get_or_create_current(scope, "hello session")
        self.assertEqual(first.title, "hello session")

        second = svc.create(scope, CreateRequest(title="second"))
        current = svc.current(scope)
        self.assertEqual(current.id, second.id)

        aged = now() - timedelta(hours=1)
        first.updated_at = aged
        store.sessions().update(first)
        svc.resume(scope, first.id)
        current = svc.current(scope)
        self.assertEqual(current.id, first.id)
        self.assertGreater(current.updated_at, aged)

        store.messages().append(
            Message(session_id=first.id, role=RoleUser, content="hello")
        )
        store.messages().append(
            Message(session_id=first.id, role=RoleAssistant, content="hi")
        )

        status = svc.status(scope)
        self.assertEqual(status.message_count, 2)
        self.assertEqual(status.last_user_preview, "hello")
        self.assertEqual(status.last_answer_preview, "hi")

        listed = svc.list(scope, "hello", 20)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].id, first.id)


class TestPlatformScopeCanListAndResumeSamePlatformCronSessions(SqliteTestCase):
    """对应 Go ``TestPlatformScopeCanListAndResumeSamePlatformCronSessions``。"""

    def test_platform_scope_cron_visibility(self) -> None:
        svc, store = new_test_service()
        scope = Scope(actor_id="qq:user1", platform="qq-onebot", platform_scope_id="private:user1")
        other_scope = Scope(
            actor_id="qq:user1", platform="qq-onebot", platform_scope_id="group:g1"
        )

        front = svc.create(scope, CreateRequest(title="front"))
        other_front = svc.create(other_scope, CreateRequest(title="other front"))
        cron_session = Session(
            owner_id=scope.actor_id,
            platform=scope.platform,
            platform_scope_id="cron:user.cron.test",
            mode=SessionModeWork,
            status=SessionStatusActive,
            title="cron",
            metadata='{"discovered_tools":["web_search"]}',
        )
        store.sessions().create(cron_session)
        other_platform_cron = Session(
            owner_id=scope.actor_id,
            platform="cli",
            platform_scope_id="cron:user.cron.test",
            mode=SessionModeWork,
            status=SessionStatusActive,
            title="cli cron",
            metadata='{"discovered_tools":["web_search"]}',
        )
        store.sessions().create(other_platform_cron)

        ids = session_ids(svc.list(scope, "", 20))
        self.assertIn(front.id, ids)
        self.assertIn(cron_session.id, ids)
        self.assertNotIn(other_front.id, ids)
        self.assertNotIn(other_platform_cron.id, ids)

        svc.resume(scope, cron_session.id)
        atrinis_session = Session(
            owner_id=scope.actor_id,
            platform=scope.platform,
            platform_scope_id="atrinis:event-1",
            mode=SessionModeWork,
            status=SessionStatusActive,
            title="atrinis",
        )
        store.sessions().create(atrinis_session)
        svc.resume(scope, atrinis_session.id)
        with self.assertRaises(Exception):
            svc.resume(scope, other_front.id)

    def test_cron_resume_promotes_but_cross_platform_stays_hidden(self) -> None:
        """跨平台隔离：另一平台的同 owner 会话在列表里不可见、也不能恢复。"""
        svc, store = new_test_service()
        qq = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")
        other = Scope(actor_id="u1", platform="telegram", platform_scope_id="group:1")
        qq_session = svc.create(qq, CreateRequest(title="qq"))
        other_session = svc.create(other, CreateRequest(title="tg"))

        ids = session_ids(svc.list(qq, "", 20))
        self.assertIn(qq_session.id, ids)
        self.assertNotIn(other_session.id, ids)
        with self.assertRaises(Exception):
            svc.resume(qq, other_session.id)
        with self.assertRaises(Exception):
            svc.pin(qq, other_session.id)
        with self.assertRaises(Exception):
            svc.delete(qq, other_session.id)
        self.assertEqual(store.sessions().get(other_session.id).id, other_session.id)


class TestServiceCreateRequest(SqliteTestCase):
    """对应 Go ``TestServiceCreateRequest``。"""

    def test_create_request_defaults_and_metadata(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()

        default_session = svc.create(scope, CreateRequest(metadata='{"kind":"default"}'))
        self.assertEqual(default_session.title, "New session")
        self.assertEqual(default_session.mode, SessionModeWork)
        self.assertEqual(default_session.metadata, '{"kind":"default"}')

        explicit = svc.create(
            scope,
            CreateRequest(title="chat", mode=SessionModeChat, metadata='{"kind":"compact"}'),
        )
        stored = store.sessions().get(explicit.id)
        self.assertEqual(stored.mode, SessionModeChat)
        self.assertEqual(stored.metadata, '{"kind":"compact"}')

        with self.assertRaises(InvalidError):
            svc.create(scope, CreateRequest(title="invalid", mode="invalid"))
        current = svc.current(scope)
        self.assertEqual(current.id, explicit.id)


class TestServiceGetOrCreateCurrentCreatesNewWhenNoInMemoryCurrent(SqliteTestCase):
    """对应 Go ``TestServiceGetOrCreateCurrentCreatesNewWhenNoInMemoryCurrent``。"""

    def test_new_service_instance_creates_new_session(self) -> None:
        store = new_test_store()
        scope = cli_scope()
        first_svc = new_service(store)
        first = first_svc.create(scope, CreateRequest(title="old session"))

        second_svc = new_service(store)
        second = second_svc.get_or_create_current(scope, "new message")
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.title, "new message")


class TestServiceGetOrCreateCurrentReturnsStorageError(SqliteTestCase):
    """对应 Go ``TestServiceGetOrCreateCurrentReturnsStorageError``。"""

    def test_storage_error_propagates_and_does_not_create(self) -> None:
        base = new_test_store()
        want_err = RuntimeError("session read failed")

        class FailingSessions:
            def __init__(self, inner) -> None:
                self._inner = inner

            def __getattr__(self, name):
                attribute = getattr(self._inner, name)

                def wrapper(*args, **kwargs):
                    if name == "get":
                        raise want_err
                    return attribute(*args, **kwargs)

                return wrapper

        store = _StoreWithSessions(base, FailingSessions(base.sessions()))
        svc = new_service(store)
        scope = cli_scope()
        svc.set_current(scope, "current")

        with self.assertRaises(RuntimeError):
            svc.get_or_create_current(scope, "must not create")
        sessions = base.sessions().list(ListSessionsRequest(include_all_platforms=True))
        self.assertEqual(len(sessions), 0)


class _StoreWithSessions:
    """覆盖 ``sessions()`` 的 Store 包装（对应 Go ``storeWithSessionRepository``）。"""

    def __init__(self, base, sessions) -> None:
        self._base = base
        self._sessions = sessions

    def sessions(self):
        return self._sessions

    def __getattr__(self, name):
        return getattr(self._base, name)


class TestServiceListResumablePageUsesRecentNonCurrentOrder(SqliteTestCase):
    """对应 Go ``TestServiceListResumablePageUsesRecentNonCurrentOrder``。"""

    def test_resumable_page_order(self) -> None:
        svc, store = new_test_service()
        scope = cli_scope()
        moment = now()

        older = svc.create(scope, CreateRequest(title="older pinned"))
        older.updated_at = moment - timedelta(hours=2)
        older.pinned_at = moment
        store.sessions().update(older)

        recent = svc.create(scope, CreateRequest(title="recent"))
        recent.updated_at = moment - timedelta(hours=1)
        store.sessions().update(recent)

        current = svc.create(scope, CreateRequest(title="current"))
        archived = svc.create(scope, CreateRequest(title="archived"))
        svc.archive(scope, archived.id)
        svc.resume(scope, current.id)

        got = svc.list_resumable_page(scope, 10, 0)
        self.assertEqual([item.id for item in got], [recent.id, older.id])


class TestServiceNonCLICannotResumeOtherScope(SqliteTestCase):
    """对应 Go ``TestServiceNonCLICannotResumeOtherScope``。"""

    def test_non_cli_cannot_resume_other_scope(self) -> None:
        svc, _ = new_test_service()
        session = svc.create(cli_scope(), CreateRequest(title="cli"))
        qq_scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")
        with self.assertRaises(Exception):
            svc.resume(qq_scope, session.id)


class TestSharedThreadScopeUsesOneCurrentSession(SqliteTestCase):
    """对应 Go ``TestSharedThreadScopeUsesOneCurrentSession``。"""

    def test_shared_thread_scope(self) -> None:
        svc, _ = new_test_service()
        scope_a = Scope(actor_id="u1", platform="qqonebot", platform_scope_id="group:9", shared=True)
        scope_b = Scope(actor_id="u2", platform="qqonebot", platform_scope_id="group:9", shared=True)
        first = svc.get_or_create_current(scope_a, "hello")
        second = svc.get_or_create_current(scope_b, "hello")
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.owner_id, "group-thread:qqonebot:group:9")
        self.assertTrue(json.loads(first.metadata)["thread_mode"] == "group")

        svc.resume(scope_b, first.id)
        listed = svc.list(scope_a, "", 10)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].id, first.id)

        per_user = Scope(actor_id="u1", platform="qqonebot", platform_scope_id="group:9")
        with self.assertRaises(Exception):
            svc.resume(per_user, first.id)

    def test_shared_scope_key_and_owner(self) -> None:
        """``Scope.Shared`` 的内存 key 与合成 owner。"""
        svc, _ = new_test_service()
        scope_a = Scope(actor_id="u1", platform="qqonebot", platform_scope_id="group:9", shared=True)
        scope_b = Scope(actor_id="u2", platform="qqonebot", platform_scope_id="group:9", shared=True)
        self.assertEqual(svc.scope_key(scope_a), svc.scope_key(scope_b))
        self.assertEqual(svc.scope_key(scope_a), "\x00shared\x00qqonebot\x00group:9")
        self.assertEqual(svc.scope_key(scope_a), svc.scope_key(scope_a))

        one = svc.get_or_create_current(scope_a, "hello")
        # 另一个群共享作用域不会复用同一个 current
        other_group = Scope(
            actor_id="u1", platform="qqonebot", platform_scope_id="group:10", shared=True
        )
        two = svc.get_or_create_current(other_group, "other")
        self.assertNotEqual(one.id, two.id)
        self.assertEqual(two.owner_id, "group-thread:qqonebot:group:10")

        # 非共享作用域按 actor 区分
        per_user_a = Scope(actor_id="u1", platform="qqonebot", platform_scope_id="group:9")
        per_user_b = Scope(actor_id="u2", platform="qqonebot", platform_scope_id="group:9")
        self.assertNotEqual(svc.scope_key(per_user_a), svc.scope_key(per_user_b))


class TestCanAccessRules(SqliteTestCase):
    """``can_access`` 的放行矩阵（平台隔离 + 共享线程 + 后台 Session）。"""

    def setUp(self) -> None:
        super().setUp()
        self.svc, self.store = new_test_service()

    def test_cli_scope_can_access_anything(self) -> None:
        session = self.store.sessions().get(
            self.svc.create(
                Scope(actor_id="u9", platform="telegram", platform_scope_id="private:9"),
                CreateRequest(title="tg"),
            ).id
        )
        self.assertTrue(self.svc.can_access(cli_scope(), session))

    def test_normal_scope_requires_same_owner_platform_and_scope(self) -> None:
        scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:1")
        session = self.store.sessions().get(
            self.svc.create(scope, CreateRequest(title="normal")).id
        )
        self.assertTrue(self.svc.can_access(scope, session))
        for name, other in (
            ("other scope", Scope(actor_id="u1", platform="qq", platform_scope_id="group:2")),
            ("other actor", Scope(actor_id="u2", platform="qq", platform_scope_id="group:1")),
            (
                "other platform",
                Scope(actor_id="u1", platform="telegram", platform_scope_id="group:1"),
            ),
            (
                "shared scope",
                Scope(
                    actor_id="u1", platform="qq", platform_scope_id="group:1", shared=True
                ),
            ),
        ):
            with self.subTest(name):
                self.assertFalse(self.svc.can_access(other, session))

    def test_background_session_is_reachable_from_another_scope(self) -> None:
        background = Session(
            owner_id="u1",
            platform="qq",
            platform_scope_id="cron:report",
            mode=SessionModeWork,
            status=SessionStatusActive,
            title="cron",
        )
        self.store.sessions().create(background)
        other_scope = Scope(actor_id="u1", platform="qq", platform_scope_id="group:2")
        self.assertTrue(self.svc.can_access(other_scope, background))
        self.assertFalse(
            self.svc.can_access(
                Scope(actor_id="u2", platform="qq", platform_scope_id="group:2"), background
            )
        )

    def test_shared_scope_requires_thread_mode_marker(self) -> None:
        shared_scope = Scope(
            actor_id="u1", platform="qq", platform_scope_id="group:9", shared=True
        )
        marked = self.store.sessions().get(
            self.svc.create(shared_scope, CreateRequest(title="shared")).id
        )
        self.assertTrue(self.svc.can_access(shared_scope, marked))

        # owner 与 scope 都对，但 metadata 没有 thread_mode=group → 不放行
        plain = self.store.sessions().get(
            self.svc.create(
                Scope(actor_id="u1", platform="qq", platform_scope_id="group:9"),
                CreateRequest(title="plain"),
            ).id
        )
        plain.owner_id = marked.owner_id
        self.store.sessions().update(plain)
        self.assertFalse(self.svc.can_access(shared_scope, plain))


class TestServiceConfig(SqliteTestCase):
    """``Config`` / ``DefaultMode`` 的归一化（Go ``NewServiceWithConfig``）。"""

    def test_default_mode_is_used_for_new_sessions(self) -> None:
        store = new_test_store()
        svc = new_service_with_config(
            store, Config(trigger_step=2, default_mode=SessionModeChat)
        )
        self.assertEqual(svc.default_mode(), SessionModeChat)
        self.assertEqual(svc.naming_config.trigger_step, 2)
        created = svc.create(cli_scope(), CreateRequest(title="default mode"))
        self.assertEqual(created.mode, SessionModeChat)

    def test_invalid_default_mode_falls_back_to_work(self) -> None:
        svc = new_service_with_config(new_test_store(), Config(default_mode="nonsense"))
        self.assertEqual(svc.default_mode(), SessionModeWork)

    def test_trigger_step_is_normalized(self) -> None:
        svc = new_service_with_config(new_test_store(), Config(trigger_step=-3))
        self.assertEqual(svc.naming_config.trigger_step, 1)
        self.assertEqual(new_service_with_naming(new_test_store(), NamingConfig()).naming_config.trigger_step, 1)


if __name__ == "__main__":
    unittest.main()
