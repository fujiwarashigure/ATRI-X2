"""``python -m atri_x.platform.qq_onebot.smoke`` 的离线端到端用例。

用一个**真的走 WebSocket** 的假 OneBot v11 协议端（:mod:`atri_x.net.websocket` 的
服务端实现，纯标准库），覆盖冒烟命令的四层语义：

1. 连接 + ``get_login_info`` 动作通道；
2. ``send_group_msg`` / ``send_private_msg`` 发送通道与 ``message_id`` 回读；
3. 上报事件监听（协议端主动推事件）；
4. 连不上时的失败退出码与 ``--json`` 错误结构。

不依赖 QQ、不依赖 NapCat，因此可以进 CI；真机联调时把 ``--url`` 指向 NapCat 即可。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import threading
import unittest
from typing import Any

from atri_x.net.websocket import WebSocketConnection, serve
from atri_x.platform.qq_onebot import smoke

#: 假协议端上报的一条群消息事件（字段贴近 OneBot v11 真实上报）。
GROUP_MESSAGE_EVENT: dict[str, Any] = {
    "post_type": "message",
    "message_type": "group",
    "sub_type": "normal",
    "time": 1700000000,
    "self_id": 10001,
    "message_id": 77,
    "user_id": 20002,
    "group_id": 123456,
    "raw_message": "hello atri",
    "message": [{"type": "text", "data": {"text": "hello atri"}}],
    "sender": {"user_id": 20002, "nickname": "tester"},
}


class FakeOneBot:
    """最小 OneBot v11 正向 WS 协议端。

    * ``get_login_info`` → 返回固定的机器人身份；
    * ``send_group_msg`` / ``send_private_msg`` → 返回 ``message_id=42``；
    * ``push_event`` 非空时，在登录动作之后立刻推一条上报事件。
    """

    def __init__(self, push_event: dict[str, Any] | None = None) -> None:
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.connections = 0
        self.push_event = push_event
        #: 每次握手带的 Authorization 头（用来验证 token 透传）。
        self.auth_headers: list[str] = []

    async def handle(self, conn: WebSocketConnection) -> None:
        """一条连接的完整生命周期。"""
        self.connections += 1
        self.auth_headers.append(_authorization(conn))
        while True:
            raw = await conn.recv()
            frame = json.loads(raw)
            action = str(frame.get("action") or "")
            params = frame.get("params") or {}
            self.actions.append((action, params))
            echo = frame.get("echo") or ""
            if action == "get_login_info":
                await conn.send_text(
                    _reply(echo, {"user_id": 10001, "nickname": "fake-bot"})
                )
                if self.push_event is not None:
                    await conn.send_text(json.dumps(self.push_event))
            elif action in ("send_group_msg", "send_private_msg"):
                await conn.send_text(_reply(echo, {"message_id": 42}))
            else:
                await conn.send_text(
                    json.dumps(
                        {"status": "failed", "retcode": 1404, "data": None, "echo": echo}
                    )
                )


def _reply(echo: str, data: Any) -> str:
    """构造一条成功的 OneBot 动作响应。"""
    return json.dumps(
        {"status": "ok", "retcode": 0, "data": data, "echo": echo}, ensure_ascii=False
    )


def _authorization(conn: WebSocketConnection) -> str:
    """取握手时的 ``Authorization`` 头（大小写不敏感）。"""
    headers = dict(getattr(conn, "request_headers", {}) or {})
    if not headers:
        request = getattr(conn, "request", None)
        headers = dict(getattr(request, "headers", {}) or {}) if request is not None else {}
    lowered = {str(key).lower(): value for key, value in headers.items()}
    return lowered.get("authorization", "")


class ServerThread:
    """把假协议端跑在独立事件循环线程里，方便同步测试直接调 ``smoke.main``。"""

    def __init__(self, fake: FakeOneBot) -> None:
        self.fake = fake
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run, name="fake-onebot-loop", daemon=True
        )
        self.server = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> str:
        """启动监听，返回客户端应连的 ``ws://`` 地址（端口由内核分配）。"""
        self.thread.start()
        future = asyncio.run_coroutine_threadsafe(
            serve(self.fake.handle, host="127.0.0.1", port=0), self.loop
        )
        self.server = future.result(timeout=10)
        return "ws://127.0.0.1:%d/" % self.server.port

    def stop(self) -> None:
        """关闭服务端并停掉事件循环线程。"""
        if self.server is not None:
            asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(timeout=10)
            self.server = None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        with contextlib.suppress(RuntimeError):
            self.loop.close()


class SmokeTestCase(unittest.TestCase):
    """对着假协议端跑冒烟命令本体（同步 API + 真实 WebSocket）。"""

    def setUp(self) -> None:
        self.fake = FakeOneBot(push_event=GROUP_MESSAGE_EVENT)
        self.server = ServerThread(self.fake)
        self.url = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def run_smoke(self, *argv: str) -> tuple[int, dict[str, Any], str]:
        """跑一次 ``smoke.main``，返回 ``(退出码, 解析后的 JSON, 原始 stdout)``。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = smoke.main(["--url", self.url, "--json", *argv])
        text = out.getvalue()
        report = json.loads(text.strip().splitlines()[-1])
        return code, report, text

    def test_connect_and_login(self) -> None:
        code, report, _ = self.run_smoke()
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["self_id"], 10001)
        self.assertEqual(report["nickname"], "fake-bot")
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(self.fake.actions[0][0], "get_login_info")

    def test_send_group_message(self) -> None:
        code, report, _ = self.run_smoke("--group", "123456", "--text", "ping")
        self.assertEqual(code, 0)
        self.assertEqual(report["sent"][0]["message_id"], "42")
        sent = [params for action, params in self.fake.actions if action == "send_group_msg"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(int(sent[0]["group_id"]), 123456)
        self.assertIn("ping", json.dumps(sent[0], ensure_ascii=False))

    def test_send_private_message(self) -> None:
        code, report, _ = self.run_smoke("--private", "20002")
        self.assertEqual(code, 0)
        self.assertEqual(report["sent"][0]["target"], "private")
        actions = [action for action, _ in self.fake.actions]
        self.assertIn("send_private_msg", actions)

    def test_listen_collects_reported_events(self) -> None:
        code, report, _ = self.run_smoke("--listen", "1")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(report["event_count"], 1)
        first = report["events"][0]
        self.assertEqual(first["post_type"], "message")
        self.assertEqual(first["message_type"], "group")
        self.assertEqual(first["group_id"], 123456)
        self.assertIn("hello atri", first["raw_message"])

    def test_token_is_sent_as_bearer_header(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = smoke.main(["--url", self.url, "--json", "--access-token", "secret-token"])
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.auth_headers[0].lower(), "bearer secret-token")

    def test_connection_failure_returns_nonzero(self) -> None:
        dead = _closed_port()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = smoke.main(["--url", "ws://127.0.0.1:%d/" % dead, "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertFalse(report["ok"])
        self.assertTrue(report["error"])


class TokenResolutionTestCase(unittest.TestCase):
    """令牌解析优先级：显式参数 > 环境变量 > 空。"""

    def test_explicit_token_wins(self) -> None:
        import os

        old = os.environ.get("ATRIX_SMOKE_TOKEN")
        os.environ["ATRIX_SMOKE_TOKEN"] = "from-env"
        try:
            self.assertEqual(smoke.resolve_access_token("explicit", "ATRIX_SMOKE_TOKEN"), "explicit")
        finally:
            if old is None:
                os.environ.pop("ATRIX_SMOKE_TOKEN", None)
            else:
                os.environ["ATRIX_SMOKE_TOKEN"] = old

    def test_env_token_used_when_explicit_absent(self) -> None:
        import os

        old = os.environ.get("ATRIX_SMOKE_TOKEN2")
        os.environ["ATRIX_SMOKE_TOKEN2"] = " from-env "
        try:
            self.assertEqual(smoke.resolve_access_token("", "ATRIX_SMOKE_TOKEN2"), "from-env")
        finally:
            if old is None:
                os.environ.pop("ATRIX_SMOKE_TOKEN2", None)
            else:
                os.environ["ATRIX_SMOKE_TOKEN2"] = old

    def test_missing_env_is_empty(self) -> None:
        self.assertEqual(smoke.resolve_access_token("", "ATRIX_SMOKE_DEFINITELY_MISSING"), "")
        self.assertEqual(smoke.resolve_access_token("", ""), "")


class EventSummaryTestCase(unittest.TestCase):
    """事件摘要渲染是纯函数，单独覆盖边界。"""

    def test_format_event_includes_scope_and_text(self) -> None:
        item = smoke.summarize_event(_event(GROUP_MESSAGE_EVENT))
        line = smoke.format_event(item)
        self.assertIn("post_type=message", line)
        self.assertIn("type=group", line)
        self.assertIn("group=123456", line)
        self.assertIn("text=hello atri", line)

    def test_long_text_is_clipped(self) -> None:
        event = dict(GROUP_MESSAGE_EVENT, raw_message="x" * 500)
        item = smoke.summarize_event(_event(event))
        self.assertTrue(item["raw_message"].endswith("..."))
        self.assertLessEqual(len(item["raw_message"]), smoke.TEXT_LIMIT + 3)

    def test_newlines_are_collapsed(self) -> None:
        event = dict(GROUP_MESSAGE_EVENT, raw_message="a\nb")
        item = smoke.summarize_event(_event(event))
        self.assertEqual(item["raw_message"], "a b")


def _event(raw: dict[str, Any]) -> Any:
    """按被测代码同一条路径解析事件，避免手搓字段。"""
    from atri_x.platform.qq_onebot.types import Event

    return Event.from_json(raw)


def _closed_port() -> int:
    """占一个端口再释放，得到一个大概率没人监听的端口号。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
