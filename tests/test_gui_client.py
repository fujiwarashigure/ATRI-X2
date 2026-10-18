"""GUI WS 客户端用例：握手、收发、断线/鉴权失败、幂等停止。

对着一个**真走 WebSocket** 的假 CLI 服务端跑（复用 :func:`atri_x.net.websocket.serve`），
不依赖真实 Agent，也不碰模型。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import unittest
from typing import Any

from atri_x.gui.client import (
    STATE_AUTH_FAILED,
    STATE_CONNECTED,
    RemoteClient,
)
from atri_x.net.websocket import WebSocketConnection, serve
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_CHAT,
    REMOTE_MSG_HELLO,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_HISTORY,
    REMOTE_MSG_HISTORY_RESULT,
    REMOTE_MSG_INPUT,
    REMOTE_MSG_SESSIONS,
    REMOTE_MSG_SESSIONS_RESULT,
    RemoteMessage,
)


class FakeCliServer:
    """最小 CLI 服务端：``hello`` 鉴权后收 ``input``、推帧；也应答会话浏览扩展帧。"""

    def __init__(self, token: str = "secret") -> None:
        self.token = token
        self.inputs: list[str] = []
        self.hellos: list[dict[str, Any]] = []
        self.connections = 0
        self.session_requests: list[dict[str, Any]] = []
        self.history_requests: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = [
            {
                "id": "sess-1",
                "title": "历史会话",
                "mode": "work",
                "status": "active",
                "updated_at": "2026-07-01T12:00:00Z",
                "current": True,
            }
        ]
        self.history: list[dict[str, Any]] = [
            {"role": "user", "text": "旧提问", "at": "2026-07-01T11:00:00Z"},
            {"role": "assistant", "text": "旧回答", "at": "2026-07-01T11:00:01Z"},
        ]

    async def handle(self, conn: WebSocketConnection) -> None:
        self.connections += 1
        raw = await conn.recv()
        hello = json.loads(raw)
        self.hellos.append(hello)
        if hello.get("type") != REMOTE_MSG_HELLO or hello.get("token") != self.token:
            await conn.send_text(json.dumps({"type": "error", "text": "bad token"}))
            return
        await conn.send_text(json.dumps({"type": REMOTE_MSG_HELLO_OK, "client_id": hello.get("client_id")}))
        while True:
            try:
                frame = json.loads(await conn.recv())
            except Exception:  # noqa: BLE001 - 客户端断开
                return
            kind = frame.get("type")
            if kind == REMOTE_MSG_INPUT:
                self.inputs.append(str(frame.get("text") or ""))
                await conn.send_text(json.dumps({"type": "stream_append", "text": "收到："}))
                await conn.send_text(json.dumps({"type": "stream_append", "text": frame.get("text")}))
                await conn.send_text(json.dumps({"type": "stream_finish"}))
                await conn.send_text(json.dumps({"type": REMOTE_MSG_CHAT, "text": "收到：" + str(frame.get("text"))}))
            elif kind == REMOTE_MSG_SESSIONS:
                self.session_requests.append(frame)
                await conn.send_text(
                    json.dumps(
                        {
                            "type": REMOTE_MSG_SESSIONS_RESULT,
                            "id": frame.get("id") or "",
                            "sessions": self.sessions,
                        },
                        ensure_ascii=False,
                    )
                )
            elif kind == REMOTE_MSG_HISTORY:
                self.history_requests.append(frame)
                await conn.send_text(
                    json.dumps(
                        {
                            "type": REMOTE_MSG_HISTORY_RESULT,
                            "id": frame.get("id") or "",
                            "session_id": frame.get("session_id") or "",
                            "history": self.history,
                        },
                        ensure_ascii=False,
                    )
                )

    async def push(self, conn: WebSocketConnection, payload: dict[str, Any]) -> None:
        await conn.send_text(json.dumps(payload, ensure_ascii=False))


class ServerThread:
    """后台事件循环 + WS 服务端。"""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.server: Any = None
        self.errors: list[BaseException] = []

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> str:
        self.thread.start()
        future = asyncio.run_coroutine_threadsafe(
            serve(
                self.handler,
                host="127.0.0.1",
                port=0,
                path="/cli/v1/ws",
                on_error=self.errors.append,
            ),
            self.loop,
        )
        self.server = future.result(timeout=10)
        return "ws://127.0.0.1:%d/cli/v1/ws" % self.server.port

    def stop(self) -> None:
        if self.server is not None:
            asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(timeout=10)
            self.server = None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        with contextlib.suppress(RuntimeError):
            self.loop.close()


class RemoteClientTestCase(unittest.TestCase):
    """客户端行为。"""

    def setUp(self) -> None:
        self.fake = FakeCliServer()
        self.server = ServerThread(self.fake.handle)
        self.url = self.server.start()
        self.messages: list[RemoteMessage] = []
        self.states: list[tuple[str, str]] = []
        self.received = threading.Event()

    def tearDown(self) -> None:
        self.server.stop()
        if self.server.errors:  # 服务端异常要暴露出来，否则只会看到客户端超时
            raise AssertionError("fake server raised: %r" % (self.server.errors,))

    def make_client(self, *, token: str = "secret", reconnect: bool = False) -> RemoteClient:
        return RemoteClient(
            self.url,
            "local",
            token,
            on_message=self._on_message,
            on_state=lambda state, detail: self.states.append((state, detail)),
            reconnect=reconnect,
            reconnect_delay=0.2,
        )

    def _on_message(self, message: RemoteMessage) -> None:
        self.messages.append(message)
        if message.type == "chat":
            self.received.set()

    def wait_connected(self, client: RemoteClient, timeout: float = 10.0) -> None:
        deadline = threading.Event()
        for _ in range(int(timeout / 0.05)):
            if client.connected:
                return
            deadline.wait(0.05)
        self.fail(f"client did not connect; states={self.states}")

    def test_connect_handshake_and_send_input(self) -> None:
        client = self.make_client()
        try:
            client.start()
            self.wait_connected(client)
            self.assertIn(STATE_CONNECTED, [state for state, _ in self.states])
            self.assertEqual(self.fake.hellos[0]["client_id"], "local")
            self.assertTrue(client.send_input("你好"))
            self.assertTrue(self.received.wait(10.0), "没有收到 chat 帧")
            self.assertEqual(self.fake.inputs[0], "你好")
            kinds = [message.type for message in self.messages]
            self.assertIn("stream_append", kinds)
            self.assertIn("chat", kinds)
            chat = [message for message in self.messages if message.type == "chat"][0]
            self.assertEqual(chat.text, "收到：你好")
        finally:
            client.stop()

    def test_send_before_connect_returns_false(self) -> None:
        client = self.make_client()
        self.assertFalse(client.send_input("no connection"))

    def test_auth_failure_sets_state_and_stops(self) -> None:
        client = self.make_client(token="wrong")
        try:
            client.start()
            for _ in range(200):
                if any(state == STATE_AUTH_FAILED for state, _ in self.states):
                    break
                threading.Event().wait(0.05)
            self.assertIn(STATE_AUTH_FAILED, [state for state, _ in self.states])
            self.assertFalse(client.connected)
        finally:
            client.stop()

    def test_request_complete_sends_frame(self) -> None:
        client = self.make_client()
        try:
            client.start()
            self.wait_connected(client)
            self.assertTrue(client.request_complete("c-1", "/mod", 4))
        finally:
            client.stop()

    def test_request_sessions_roundtrip(self) -> None:
        client = self.make_client()
        try:
            client.start()
            self.wait_connected(client)
            self.assertTrue(client.request_sessions(limit=15))
            deadline = threading.Event()
            for _ in range(200):
                if any(m.type == REMOTE_MSG_SESSIONS_RESULT for m in self.messages):
                    break
                deadline.wait(0.05)
            result = [m for m in self.messages if m.type == REMOTE_MSG_SESSIONS_RESULT]
            self.assertTrue(result, "没有收到 sessions_result")
            self.assertEqual(result[0].sessions[0].id, "sess-1")
            self.assertEqual(result[0].sessions[0].title, "历史会话")
            self.assertTrue(result[0].sessions[0].current)
            self.assertEqual(self.fake.session_requests[0]["limit"], 15)
        finally:
            client.stop()

    def test_request_history_roundtrip(self) -> None:
        client = self.make_client()
        try:
            client.start()
            self.wait_connected(client)
            self.assertTrue(client.request_history("sess-1", limit=50))
            for _ in range(200):
                if any(m.type == REMOTE_MSG_HISTORY_RESULT for m in self.messages):
                    break
                threading.Event().wait(0.05)
            result = [m for m in self.messages if m.type == REMOTE_MSG_HISTORY_RESULT]
            self.assertTrue(result, "没有收到 history_result")
            self.assertEqual(result[0].session_id, "sess-1")
            self.assertEqual([item.role for item in result[0].history], ["user", "assistant"])
            self.assertEqual(result[0].history[1].text, "旧回答")
            self.assertEqual(self.fake.history_requests[0]["limit"], 50)
        finally:
            client.stop()

    def test_stop_is_idempotent(self) -> None:
        client = self.make_client()
        client.start()
        self.wait_connected(client)
        client.stop()
        client.stop()
        self.assertFalse(client.connected)

    def test_connection_failure_reports_disconnected(self) -> None:
        client = RemoteClient(
            "ws://127.0.0.1:1/cli/v1/ws",
            "local",
            "x",
            on_state=lambda state, detail: self.states.append((state, detail)),
            reconnect=False,
        )
        client.start()
        for _ in range(200):
            if any(state == "disconnected" for state, _ in self.states):
                break
            threading.Event().wait(0.05)
        client.stop()
        self.assertIn("disconnected", [state for state, _ in self.states])


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
