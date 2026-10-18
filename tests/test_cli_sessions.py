"""会话浏览扩展（``sessions`` / ``history`` 帧）的测试。

分四层：

1. 线上协议（:mod:`atri_x.platform.cli.protocol`）：新帧的 JSON 往返；
2. 查询层（:mod:`atri_x.session.browse`）：真实 SQLite 上的列表/历史/鉴权；
3. 服务端（:mod:`atri_x.platform.cli.remote_server`）：帧分发与出站编码；
4. 桌面端（:mod:`atri_x.gui.protocol` / :mod:`atri_x.gui.sessions`）：解码与侧边栏。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from atri_x.gui.protocol import (
    ROLE_ASSISTANT,
    ROLE_USER,
    Transcript,
    decode_history,
    decode_sessions,
    format_clock,
)
from atri_x.gui.sessions import SessionSidebar, load_history_into
from atri_x.platform.cli.config import ClientConfig, Config, ServerConfig
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_HISTORY,
    REMOTE_MSG_HISTORY_RESULT,
    REMOTE_MSG_SESSIONS,
    REMOTE_MSG_SESSIONS_RESULT,
    HistoryEntry,
    RemoteMessage,
    SessionInfo,
)
from atri_x.platform.cli.remote_server import (
    RemoteClientConn,
    RemoteServerOptions,
    new_remote_server,
)
from atri_x.session import CreateRequest, Scope, new_service
from atri_x.session.browse import SessionBrowser, cli_scope
from atri_x.storage import sqlite
from atri_x.storage.models import Message, RoleAssistant, RoleUser


def _make_service():
    """内存 SQLite + 会话服务（真实 migration）。"""
    store = sqlite.new(":memory:")
    return store, new_service(store)


def _add_message(store, session_id: str, role: str, text: str) -> None:
    store.messages().append(Message(session_id=session_id, role=role, content=text))


class ProtocolRoundTripTest(unittest.TestCase):
    """新帧的序列化/反序列化。"""

    def test_sessions_result_roundtrip(self) -> None:
        message = RemoteMessage(
            type=REMOTE_MSG_SESSIONS_RESULT,
            id="req-1",
            sessions=[
                SessionInfo(
                    id="s1",
                    title="标题",
                    mode="work",
                    status="active",
                    updated_at="2026-07-01T12:00:00Z",
                    current=True,
                )
            ],
        )
        raw = message.to_json()
        self.assertEqual(raw["type"], REMOTE_MSG_SESSIONS_RESULT)
        self.assertEqual(raw["sessions"][0]["title"], "标题")
        decoded = RemoteMessage.from_json(json.loads(json.dumps(raw, ensure_ascii=False)))
        self.assertEqual(decoded.sessions[0].id, "s1")
        self.assertTrue(decoded.sessions[0].current)
        self.assertEqual(decoded.sessions[0].updated_at, "2026-07-01T12:00:00Z")

    def test_history_frame_roundtrip(self) -> None:
        message = RemoteMessage(
            type=REMOTE_MSG_HISTORY,
            session_id="s1",
            limit=50,
            history=[HistoryEntry(role="user", text="问", at="2026-07-01T12:00:00Z")],
        )
        raw = message.to_json()
        self.assertEqual(raw["session_id"], "s1")
        self.assertEqual(raw["limit"], 50)
        decoded = RemoteMessage.from_json(raw)
        self.assertEqual(decoded.session_id, "s1")
        self.assertEqual(decoded.limit, 50)
        self.assertEqual(decoded.history[0].text, "问")

    def test_plain_frame_stays_unchanged(self) -> None:
        raw = RemoteMessage(type="input", text="你好").to_json()
        self.assertNotIn("sessions", raw)
        self.assertNotIn("history", raw)
        self.assertNotIn("session_id", raw)
        self.assertNotIn("limit", raw)


class SessionBrowserTest(unittest.TestCase):
    """真实存储上的列表/历史/鉴权。"""

    def setUp(self) -> None:
        self.store, self.service = _make_service()
        self.scope = cli_scope("local")
        self.browser = SessionBrowser(self.service)
        self.first = self.service.create(self.scope, CreateRequest(title="第一个"))
        self.second = self.service.create(self.scope, CreateRequest(title="第二个"))
        _add_message(self.store, self.first.id, RoleUser, "旧提问")
        _add_message(self.store, self.first.id, RoleAssistant, "旧回答")
        _add_message(self.store, self.first.id, "tool", "工具中间消息")

    def tearDown(self) -> None:
        self.store.close()

    def test_list_marks_current(self) -> None:
        views = self.browser.list(self.scope, 10)
        ids = {view.id for view in views}
        self.assertIn(self.first.id, ids)
        self.assertIn(self.second.id, ids)
        current = [view for view in views if view.current]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].id, self.second.id)

    def test_list_query_filters(self) -> None:
        views = self.browser.list(self.scope, 10, "第一个")
        self.assertEqual([view.title for view in views], ["第一个"])

    def test_history_only_chat_roles_and_ordered(self) -> None:
        views = self.browser.history(self.scope, self.first.id, 100)
        self.assertEqual([view.role for view in views], [RoleUser, RoleAssistant])
        self.assertEqual(views[0].text, "旧提问")
        self.assertEqual(views[-1].text, "旧回答")
        self.assertIsInstance(views[0].at, datetime)

    def test_history_respects_limit_tail(self) -> None:
        views = self.browser.history(self.scope, self.first.id, 1)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].text, "旧回答")

    def test_history_denied_for_other_scope(self) -> None:
        other = Scope(actor_id="other", platform="qqonebot", platform_scope_id="group:1")
        self.assertEqual(self.browser.history(other, self.first.id, 10), [])

    def test_history_unknown_session_is_empty(self) -> None:
        self.assertEqual(self.browser.history(self.scope, "missing", 10), [])
        self.assertEqual(self.browser.history(self.scope, "", 10), [])

    def test_current_missing_returns_no_current(self) -> None:
        store = sqlite.new(":memory:")
        try:
            fresh = SessionBrowser(new_service(store))
            self.assertEqual(fresh.session_ids(cli_scope("x")), [])
        finally:
            store.close()

    def test_cli_scope_is_cross_platform(self) -> None:
        scope = self.scope
        self.assertTrue(scope.is_cli)
        self.assertEqual(scope.platform, "cli")


class FakeConnection:
    """收集服务端写出的 JSON 帧。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))


class FakeBrowser:
    """鸭子类型的浏览实现（替代真实会话服务）。"""

    def __init__(self) -> None:
        self.updated = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    def scope(self, client_id: str) -> Scope:
        return cli_scope(client_id)

    def list(self, scope, limit, query):
        return [
            SimpleNamespace(
                id="s1",
                title="标题",
                mode="work",
                status="active",
                platform="cli",
                updated_at=self.updated,
                created_at=self.updated,
                current=True,
            )
        ]

    def history(self, scope, session_id, limit):
        return [
            SimpleNamespace(role="user", text="问", at=self.updated),
            SimpleNamespace(role="assistant", text="答", at=self.updated),
        ]


def _server_config() -> Config:
    return Config(
        enabled=True,
        server=ServerConfig(enabled=True, listen="127.0.0.1:0", tokens={"local": ["X"]}),
        clients={"local": ClientConfig(id="local", token_env=["X"])},
    )


class RemoteServerBrowseTest(unittest.TestCase):
    """服务端帧分发与出站编码。"""

    def make_server(self, browser):
        return new_remote_server(
            RemoteServerOptions(config=_server_config(), session_browser=browser)
        )

    def test_sessions_frame_writes_result(self) -> None:
        async def run():
            server = self.make_server(FakeBrowser())
            connection = FakeConnection()
            client = RemoteClientConn(id="local", connection=connection)
            await server.handle_client_message(
                None, None, client, RemoteMessage(type=REMOTE_MSG_SESSIONS, id="r1", limit=5)
            )
            return connection

        connection = asyncio.run(run())
        frame = connection.frames[0]
        self.assertEqual(frame["type"], REMOTE_MSG_SESSIONS_RESULT)
        self.assertEqual(frame["id"], "r1")
        self.assertEqual(frame["sessions"][0]["id"], "s1")
        self.assertEqual(frame["sessions"][0]["updated_at"], "2026-07-01T12:00:00Z")
        self.assertTrue(frame["sessions"][0]["current"])

    def test_history_frame_writes_result(self) -> None:
        async def run():
            server = self.make_server(FakeBrowser())
            connection = FakeConnection()
            client = RemoteClientConn(id="local", connection=connection)
            await server.handle_client_message(
                None,
                None,
                client,
                RemoteMessage(type=REMOTE_MSG_HISTORY, session_id="s1", limit=7),
            )
            return connection

        connection = asyncio.run(run())
        frame = connection.frames[0]
        self.assertEqual(frame["type"], REMOTE_MSG_HISTORY_RESULT)
        self.assertEqual(frame["session_id"], "s1")
        self.assertEqual([item["role"] for item in frame["history"]], ["user", "assistant"])

    def test_no_browser_returns_empty_result(self) -> None:
        async def run():
            server = self.make_server(None)
            connection = FakeConnection()
            client = RemoteClientConn(id="local", connection=connection)
            await server.handle_client_message(
                None, None, client, RemoteMessage(type=REMOTE_MSG_SESSIONS)
            )
            return connection

        connection = asyncio.run(run())
        self.assertEqual(connection.frames[0]["type"], REMOTE_MSG_SESSIONS_RESULT)
        self.assertNotIn("sessions", connection.frames[0])

    def test_set_session_browser_replaces(self) -> None:
        server = self.make_server(None)
        self.assertIsNone(server.session_browser)
        browser = FakeBrowser()
        server.set_session_browser(browser)
        self.assertIs(server.session_browser, browser)

    def test_broken_browser_does_not_raise(self) -> None:
        class Broken:
            def scope(self, client_id):
                raise RuntimeError("boom")

        async def run():
            server = self.make_server(Broken())
            connection = FakeConnection()
            client = RemoteClientConn(id="local", connection=connection)
            await server.handle_client_message(
                None, None, client, RemoteMessage(type=REMOTE_MSG_SESSIONS)
            )
            return connection

        connection = asyncio.run(run())
        self.assertEqual(connection.frames[0]["type"], REMOTE_MSG_SESSIONS_RESULT)


class GuiDecodeTest(unittest.TestCase):
    """桌面端的解码与历史渲染。"""

    def test_decode_sessions(self) -> None:
        message = RemoteMessage(
            type=REMOTE_MSG_SESSIONS_RESULT,
            sessions=[SessionInfo(id="s1", title="标题", updated_at="2026-07-01T12:00:00Z")],
        )
        items = decode_sessions(message)
        self.assertEqual(len(items), 1)
        self.assertIn("标题", items[0].label_text)
        self.assertEqual(decode_sessions(RemoteMessage(type="chat")), [])

    def test_decode_history(self) -> None:
        message = RemoteMessage(
            type=REMOTE_MSG_HISTORY_RESULT,
            session_id="s1",
            history=[
                HistoryEntry(role="user", text="问"),
                HistoryEntry(role="assistant", text="答"),
                HistoryEntry(role="tool", text="忽略"),
            ],
        )
        items = decode_history(message)
        self.assertEqual([item.role for item in items], [ROLE_USER, ROLE_ASSISTANT])
        self.assertEqual(decode_history(RemoteMessage(type="chat")), [])

    def test_format_clock(self) -> None:
        self.assertTrue(format_clock("2026-07-01T12:00:00Z"))
        self.assertEqual(format_clock(""), "")
        self.assertEqual(format_clock("not-a-time"), "")

    def test_load_history_into_transcript(self) -> None:
        transcript = Transcript()
        count = load_history_into(
            transcript,
            [
                SimpleNamespace(role=ROLE_USER, text="旧问", at="2026-07-01T12:00:00Z"),
                SimpleNamespace(role=ROLE_ASSISTANT, text="旧答", at="2026-07-01T12:00:01Z"),
                SimpleNamespace(role=ROLE_USER, text="  ", at=""),
            ],
        )
        self.assertEqual(count, 2)
        self.assertEqual([line.role for line in transcript.lines], [ROLE_USER, ROLE_ASSISTANT])
        self.assertIn("旧答", transcript.text())


class SessionSidebarTest(unittest.TestCase):
    """侧边栏控件（需要 Tk；无显示环境时跳过）。"""

    def setUp(self) -> None:
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover - 无 Tk 的解释器
            self.skipTest("tkinter 不可用")
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:  # pragma: no cover - 无显示环境
            self.skipTest("没有可用的 Tk 显示")
        self.opened: list[str] = []
        self.refreshed: list[bool] = []
        self.created: list[bool] = []
        self.sidebar = SessionSidebar(
            self.root,
            on_open=self.opened.append,
            on_refresh=lambda: self.refreshed.append(True),
            on_new=lambda: self.created.append(True),
        )

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001 - 已经销毁
            pass

    def test_set_sessions_and_select(self) -> None:
        from atri_x.gui.protocol import SessionItem

        self.sidebar.set_sessions(
            [
                SessionItem(id="s1", title="甲", current=True),
                SessionItem(id="s2", title="乙"),
            ]
        )
        self.assertEqual(self.sidebar.count(), 2)
        self.sidebar.select("s2")
        self.assertEqual(self.sidebar.selected_id(), "s2")
        self.sidebar._open_clicked()
        self.assertEqual(self.opened, ["s2"])

    def test_refresh_and_new_buttons(self) -> None:
        self.sidebar._refresh_clicked()
        self.sidebar._new_clicked()
        self.assertEqual(self.refreshed, [True])
        self.assertEqual(self.created, [True])

    def test_clear(self) -> None:
        from atri_x.gui.protocol import SessionItem

        self.sidebar.set_sessions([SessionItem(id="s1")])
        self.sidebar.clear()
        self.assertEqual(self.sidebar.count(), 0)
        self.assertEqual(self.sidebar.selected_id(), "")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
