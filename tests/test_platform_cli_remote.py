"""``atri_x.platform.cli`` 远程服务端 / 客户端 / 线上协议的端到端测试。

对应 Go 测试：``internal/platform/cli/remote_server_test.go``、
``remote_write_test.go``。

用真实的回环 WebSocket（``atri_x.net.websocket``）跑通：握手鉴权、``input`` 分发、
``complete`` 往返、``chat`` / ``notice`` / ``status`` 出站与目标路由。
所有等待都带超时，``tearDown`` 里关服务端，避免留下挂起任务。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import unittest

from atri_x.agent.status import publish_runtime_status
from atri_x.agent.turn_output import start_message_stream
from atri_x.completion.completion import Item, Request as CompletionRequest
from atri_x.delivery.delivery import (
    Kind,
    MessageStream,
    Notice,
    Output,
    Source,
    Target,
    text as text_output,
)
from atri_x.errors import AtriXError, InvalidError
from atri_x.net import websocket as ws
from atri_x.platform.cli.config import Config, ClientConfig, ServerConfig
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_CHAT,
    REMOTE_MSG_COMPLETE_RESULT,
    REMOTE_MSG_ERROR,
    REMOTE_MSG_HELLO,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_INPUT,
    REMOTE_MSG_NOTICE,
    REMOTE_MSG_STATUS,
    RemoteMessage,
    level_text,
)
from atri_x.platform.cli.remote_client import (
    RemoteClient,
    RemoteClientOptions,
    new_remote_client,
)
from atri_x.platform.cli.remote_server import (
    RemoteClientConn,
    RemoteMessageStream,
    RemoteServer,
    RemoteServerOptions,
    new_remote_server,
    parse_listen,
    remote_client_from,
    with_remote_client,
)
from atri_x.runtime.status import Snapshot
from atri_x.safecontext import CancelToken

TOKEN_ENV = "ATRI_X_TEST_CLI_TOKEN"

#: 测试里所有等待的上限（秒），保证不会挂住。
WAIT = 5.0


def server_config(**overrides) -> Config:
    values = {
        "enabled": True,
        "server": ServerConfig(
            enabled=True, listen="127.0.0.1:0", tokens={"local": [TOKEN_ENV]}
        ),
        "clients": {"local": ClientConfig(id="local", token_env=[TOKEN_ENV])},
    }
    values.update(overrides)
    return Config(**values)


class RecordingHandler:
    """记录收到文本；可选地把回复经 ctx 发回去。"""

    def __init__(self, reply: str = "") -> None:
        self.received: list[str] = []
        self.reply = reply
        self.server: RemoteServer | None = None

    async def handle_message(self, ctx, value: str) -> None:
        self.received.append(value)
        if self.reply and self.server is not None:
            await self.server.send_chat(ctx, [text_output(self.reply + value)])


class StaticCompleter:
    def __init__(self, items: list[Item]) -> None:
        self.items = items
        self.calls: list[CompletionRequest] = []

    async def complete(self, ctx, req: CompletionRequest) -> list[Item]:
        self.calls.append(req)
        return list(self.items)


class FakeAgent:
    """只提供 ``platform`` 的 agent 桩（agent 侧同步调用点的取值路径）。"""

    def __init__(self, platform) -> None:
        self.platform = platform


class RemoteServerTestCase(unittest.IsolatedAsyncioTestCase):
    """起一个真实服务端的公共脚手架。"""

    def setUp(self) -> None:
        os.environ[TOKEN_ENV] = "secret-token"
        self.ctx = CancelToken()
        self.server: RemoteServer | None = None
        self.runner: asyncio.Task | None = None
        self.connections: list = []

    def tearDown(self) -> None:
        os.environ.pop(TOKEN_ENV, None)

    async def start_server(
        self, handler=None, completer=None, superadmins=(), config: Config | None = None
    ) -> RemoteServer:
        server = new_remote_server(
            RemoteServerOptions(
                config=config or server_config(),
                config_dir="",
                superadmins=superadmins,
                completer=completer,
            )
        )
        if handler is not None and isinstance(handler, RecordingHandler):
            handler.server = server
        self.server = server
        self.runner = asyncio.ensure_future(server.run(self.ctx, handler))
        for _ in range(200):
            if server.server is not None:
                return server
            await asyncio.sleep(0.01)
        raise AssertionError("server did not start")

    async def stop_server(self) -> None:
        for connection in self.connections:
            try:
                await connection.close(ws.CLOSE_NORMAL, "bye")
            except Exception:  # noqa: BLE001
                pass
        self.connections.clear()
        if self.runner is not None:
            self.ctx.cancel("test done")
            try:
                await asyncio.wait_for(self.runner, WAIT)
            except (AtriXError, asyncio.TimeoutError, asyncio.CancelledError):
                self.runner.cancel()
            self.runner = None
        if self.server is not None:
            await self.server.close_all()
            self.server = None

    async def connect(self, connection=None):
        """连上测试服务端并完成 hello 握手，返回连接对象。"""
        assert self.server is not None
        url = f"ws://127.0.0.1:{self.server.port}/cli/v1/ws"
        connection = connection or await ws.connect(url)
        self.connections.append(connection)
        return connection

    async def handshake(self, connection, client_id: str = "local", token: str = "secret-token"):
        await connection.send_text(
            _json(RemoteMessage(type=REMOTE_MSG_HELLO, client_id=client_id, token=token))
        )
        raw = await asyncio.wait_for(connection.recv(), WAIT)
        return RemoteMessage.from_json(_decode(raw))

    async def switch_handler(self, handler) -> None:
        """换一个 handler：有在跑的服务端就重启，没有就直接起一个（旧连接一并关掉）。"""
        if self.runner is not None:
            self.runner.cancel()
            self.ctx.cancel("restart")
            try:
                await asyncio.wait_for(self.runner, WAIT)
            except (AtriXError, asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self.ctx = CancelToken()
            self.server = None
            for connection in self.connections:
                try:
                    await connection.close(ws.CLOSE_NORMAL, "restart")
                except Exception:  # noqa: BLE001 - 旧连接已经在关，失败无所谓
                    pass
            self.connections.clear()
        await self.start_server(handler)

    async def start_with_captured_ctx(self):
        """起服务端 + 一条已握手的连接，并回传 handler 收到的那份消息 ctx。"""
        captured: dict = {}

        class CapturingHandler:
            async def handle_message(self, ctx, value: str) -> None:
                captured["ctx"] = ctx

        await self.switch_handler(CapturingHandler())
        connection = await self.connect()
        await self.handshake(connection)
        await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="go")))
        for _ in range(200):
            if captured:
                break
            await asyncio.sleep(0.01)
        if "ctx" not in captured:
            raise AssertionError("handler did not receive the input frame")
        return self.server, connection, captured["ctx"]

    async def read_frame_types(self, connection, count: int) -> list[str]:
        """读 ``count`` 帧并回传它们的 ``type``。"""
        types = []
        for _ in range(count):
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            types.append(RemoteMessage.from_json(_decode(raw)).type)
        return types


def _json(msg: RemoteMessage) -> str:
    import json

    return json.dumps(msg.to_json(), ensure_ascii=False)


def _decode(raw) -> object:
    import json

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 协议与配置
# ---------------------------------------------------------------------------

class TestProtocolWire(unittest.TestCase):
    def test_hello_frame_keys(self) -> None:
        raw = RemoteMessage(type=REMOTE_MSG_HELLO, client_id="local", token="t").to_json()
        self.assertEqual(set(raw), {"type", "client_id", "token", "snapshot"})
        self.assertEqual(raw["type"], "hello")

    def test_round_trip_through_json(self) -> None:
        import json

        original = RemoteMessage(
            type=REMOTE_MSG_COMPLETE_RESULT, id="c-1", items=[Item(text="/a", label="a")]
        )
        restored = RemoteMessage.from_json(json.loads(json.dumps(original.to_json())))
        self.assertEqual(restored.type, original.type)
        self.assertEqual(restored.id, "c-1")
        self.assertEqual([item.text for item in restored.items], ["/a"])

    def test_unknown_keys_and_bad_json_are_tolerated(self) -> None:
        self.assertEqual(RemoteMessage.from_json({"type": "x", "extra": 1}).type, "x")
        self.assertEqual(RemoteMessage.from_json("not a mapping").type, "")
        self.assertEqual(RemoteMessage.from_json(None).cursor, 0)

    def test_level_text_matches_logging(self) -> None:
        import logging

        self.assertEqual(level_text(logging.INFO), "INFO")
        self.assertEqual(level_text(logging.WARNING), "WARN")


class TestRemoteServerConstruction(unittest.TestCase):
    def test_disabled_server(self) -> None:
        cfg = server_config(server=ServerConfig(enabled=False, listen="127.0.0.1:0", tokens={"a": ["B"]}))
        with self.assertRaises(InvalidError) as ctx:
            new_remote_server(RemoteServerOptions(config=cfg))
        self.assertEqual(str(ctx.exception), "cli remote server is disabled")

    def test_empty_listen(self) -> None:
        cfg = server_config()
        cfg.server.listen = "127.0.0.1:0"
        server = new_remote_server(RemoteServerOptions(config=cfg))
        # apply_config_defaults 会把空 listen 补成默认地址，因此显式改空后仍可构造。
        self.assertEqual(server.name(), "cli")

    def test_no_tokens(self) -> None:
        cfg = server_config(server=ServerConfig(enabled=True, listen="127.0.0.1:0", tokens={}))
        with self.assertRaises(InvalidError) as ctx:
            new_remote_server(RemoteServerOptions(config=cfg))
        self.assertEqual(str(ctx.exception), "cli remote server tokens are not configured")

    def test_parse_listen(self) -> None:
        self.assertEqual(parse_listen("127.0.0.1:32172"), ("127.0.0.1", 32172))
        self.assertEqual(parse_listen("127.0.0.1:0"), ("127.0.0.1", 0))
        self.assertEqual(parse_listen(":8080"), ("", 8080))
        self.assertEqual(parse_listen("[::1]:9000"), ("::1", 9000))

    def test_remote_client_helpers(self) -> None:
        sentinel = RemoteClientConn(id="x")
        ctx = with_remote_client({}, sentinel)
        self.assertIs(remote_client_from(ctx), sentinel)
        self.assertIsNone(remote_client_from({}))

    def test_server_satisfies_platform_runtime_protocol(self) -> None:
        from atri_x.platform.platform import Runtime

        server = new_remote_server(RemoteServerOptions(config=server_config()))
        self.assertIsInstance(server, Runtime)

    def test_builtin_contract(self) -> None:
        """``atri_x/platform/builtin.py`` 装配远程服务端时用到的那组入口。"""
        server = new_remote_server(
            RemoteServerOptions(
                config=server_config(), config_dir=".", superadmins=["cli:local", " box "]
            )
        )
        self.assertEqual(server.name(), "cli")
        self.assertEqual(server.superadmins, {"local", "box"})
        self.assertEqual(server.connected_client_ids(), [])


# ---------------------------------------------------------------------------
# 服务端端到端
# ---------------------------------------------------------------------------

class TestRemoteServerEndToEnd(RemoteServerTestCase):
    async def test_handshake_ok_and_unauthorized(self) -> None:
        await self.start_server(RecordingHandler())
        try:
            connection = await self.connect()
            hello = await self.handshake(connection)
            self.assertEqual(hello.type, REMOTE_MSG_HELLO_OK)
            self.assertEqual(hello.client_id, "local")
            self.assertEqual(self.server.connected_client_ids(), ["local"])

            bad = await self.connect()
            message = await self.handshake(bad, token="wrong")
            self.assertEqual(message.type, REMOTE_MSG_ERROR)
            self.assertEqual(message.text, "unauthorized")

            unknown = await self.connect()
            message = await self.handshake(unknown, client_id="nobody")
            self.assertEqual(message.type, REMOTE_MSG_ERROR)
        finally:
            await self.stop_server()

    async def test_hello_must_be_first(self) -> None:
        await self.start_server(RecordingHandler())
        try:
            connection = await self.connect()
            await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="hi")))
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            self.assertEqual(RemoteMessage.from_json(_decode(raw)).type, REMOTE_MSG_ERROR)
        finally:
            await self.stop_server()

    async def test_input_reaches_handler_and_reply_comes_back(self) -> None:
        handler = RecordingHandler(reply="echo: ")
        await self.start_server(handler)
        try:
            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="  ping  ")))
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            message = RemoteMessage.from_json(_decode(raw))
            self.assertEqual(message.type, REMOTE_MSG_CHAT)
            self.assertEqual(message.text, "echo: ping")
            for _ in range(100):
                if handler.received:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(handler.received, ["ping"])
        finally:
            await self.stop_server()

    async def test_blank_input_is_ignored(self) -> None:
        handler = RecordingHandler(reply="echo: ")
        await self.start_server(handler)
        try:
            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="   ")))
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(connection.recv(), 0.2)
            self.assertEqual(handler.received, [])
        finally:
            await self.stop_server()

    async def test_complete_round_trip(self) -> None:
        completer = StaticCompleter([Item(text="/help", label="/help")])
        await self.start_server(RecordingHandler(), completer=completer)
        try:
            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(
                _json(
                    RemoteMessage(
                        type="complete", id="c-1", text="/he", cursor=3
                    )
                )
            )
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            message = RemoteMessage.from_json(_decode(raw))
            self.assertEqual(message.type, REMOTE_MSG_COMPLETE_RESULT)
            self.assertEqual(message.id, "c-1")
            self.assertEqual([item.text for item in message.items], ["/help"])
            self.assertEqual(completer.calls[0].cursor, 3)
        finally:
            await self.stop_server()

    async def test_complete_without_completer_returns_empty(self) -> None:
        await self.start_server(RecordingHandler())
        try:
            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(_json(RemoteMessage(type="complete", id="c-9")))
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            message = RemoteMessage.from_json(_decode(raw))
            self.assertEqual(message.type, REMOTE_MSG_COMPLETE_RESULT)
            self.assertEqual(message.items, [])
        finally:
            await self.stop_server()

    async def test_handler_error_is_reported(self) -> None:
        class FailingHandler:
            async def handle_message(self, ctx, value: str) -> None:
                raise RuntimeError("boom")

        await self.start_server(FailingHandler())
        try:
            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="x")))
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            message = RemoteMessage.from_json(_decode(raw))
            self.assertEqual(message.type, REMOTE_MSG_ERROR)
            self.assertIn("boom", message.text)
        finally:
            await self.stop_server()

    async def test_send_notice_to_private_user(self) -> None:
        server = await self.start_server(RecordingHandler())
        try:
            connection = await self.connect()
            await self.handshake(connection)
            notice = Notice(
                target=Target(private_user_id="local"),
                outputs=[text_output("heads up")],
            )
            await server.send_notice({}, notice)
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            message = RemoteMessage.from_json(_decode(raw))
            self.assertEqual(message.type, REMOTE_MSG_NOTICE)
            self.assertEqual(message.text, "heads up")
        finally:
            await self.stop_server()

    async def test_send_notice_superadmins(self) -> None:
        server = await self.start_server(RecordingHandler(), superadmins=["cli:local"])
        try:
            connection = await self.connect()
            await self.handshake(connection)
            await server.send_notice(
                {}, Notice(target=Target(superadmins=True), outputs=[text_output("ops")])
            )
            raw = await asyncio.wait_for(connection.recv(), WAIT)
            self.assertEqual(RemoteMessage.from_json(_decode(raw)).type, REMOTE_MSG_NOTICE)
        finally:
            await self.stop_server()

    async def test_send_notice_target_errors(self) -> None:
        server = await self.start_server(RecordingHandler())
        try:
            with self.assertRaises(InvalidError) as ctx:
                await server.send_notice(
                    {}, Notice(target=Target(platform="telegram"), outputs=[text_output("x")])
                )
            self.assertEqual(str(ctx.exception), 'cli cannot send to platform "telegram"')

            with self.assertRaises(InvalidError) as ctx:
                await server.send_notice({}, Notice(outputs=[text_output("x")]))
            self.assertEqual(str(ctx.exception), "cli target missing")

            with self.assertRaises(InvalidError) as ctx:
                await server.send_notice(
                    {}, Notice(target=Target(group_id="g"), outputs=[text_output("x")])
                )
            self.assertEqual(
                str(ctx.exception), "cli target missing private_user_id or superadmins"
            )

            with self.assertRaises(InvalidError) as ctx:
                await server.send_notice(
                    {},
                    Notice(target=Target(private_user_id="ghost"), outputs=[text_output("x")]),
                )
            self.assertEqual(str(ctx.exception), "no matching cli clients are connected")
        finally:
            await self.stop_server()

    async def test_status_and_reasoning_are_noops_without_client(self) -> None:
        server = await self.start_server(RecordingHandler())
        try:
            # ``set_runtime_status`` / ``start_stream`` 是同步方法（Go 亦然）。
            server.set_runtime_status({}, Snapshot(session_id="s"))
            await server.send_reasoning({}, "   ")
            await server.send_reasoning({}, "text")
            with self.assertRaises(InvalidError):
                server.start_stream({})
            with self.assertRaises(InvalidError):
                await server.send_chat({}, [text_output("x")])
        finally:
            await self.stop_server()

    async def test_status_and_stream_use_ctx_client(self) -> None:
        handler = RecordingHandler()
        server = await self.start_server(handler)
        try:
            connection = await self.connect()
            await self.handshake(connection)

            captured: dict = {}

            class CapturingHandler:
                async def handle_message(self, ctx, value: str) -> None:
                    captured["ctx"] = ctx
                    captured["client"] = remote_client_from(ctx)
                    server.set_runtime_status(ctx, Snapshot(session_id="s", phase="llm"))
                    await server.send_reasoning(ctx, "thinking")
                    stream = server.start_stream(ctx)
                    await stream.append(ctx, "part")
                    await stream.replace(ctx, "final")
                    await stream.finish(ctx)
                    await server.send_chat(ctx, [text_output("done")])

            await self.switch_handler(CapturingHandler())

            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="go")))

            types = []
            for _ in range(6):
                raw = await asyncio.wait_for(connection.recv(), WAIT)
                types.append(RemoteMessage.from_json(_decode(raw)).type)
            self.assertEqual(
                types,
                [
                    REMOTE_MSG_STATUS,
                    "reasoning",
                    "stream_append",
                    "stream_replace",
                    "stream_finish",
                    REMOTE_MSG_CHAT,
                ],
            )
            self.assertIsInstance(captured["client"], RemoteClientConn)
            self.assertEqual(captured["client"].id, "local")
        finally:
            await self.stop_server()

    async def test_message_context_is_attached(self) -> None:
        from atri_x.platform.platform import message_context_from

        captured: dict = {}
        server = await self.start_server(RecordingHandler())

        class CapturingHandler:
            async def handle_message(self, ctx, value: str) -> None:
                message, ok = message_context_from(ctx)
                captured["ok"] = ok
                captured["message"] = message

        try:
            self.runner.cancel()
            self.ctx.cancel("restart")
            try:
                await asyncio.wait_for(self.runner, WAIT)
            except (AtriXError, asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self.ctx = CancelToken()
            self.server = None
            self.connections.clear()
            server = await self.start_server(CapturingHandler())
            connection = await self.connect()
            await self.handshake(connection)
            await connection.send_text(_json(RemoteMessage(type=REMOTE_MSG_INPUT, text="go")))
            for _ in range(100):
                if captured:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(captured["ok"])
            message = captured["message"]
            self.assertEqual(message.platform, "cli")
            self.assertEqual(message.platform_user_id, "local")
            self.assertEqual(message.scope_id, "local")
            self.assertTrue(message.actor_id.endswith(":local"))
            self.assertIs(message.sender, server)
        finally:
            await self.stop_server()

    async def test_connected_client_ids_and_removal(self) -> None:
        config = server_config(
            server=ServerConfig(
                enabled=True,
                listen="127.0.0.1:0",
                tokens={"local": [TOKEN_ENV], "box": [TOKEN_ENV]},
            )
        )
        server = await self.start_server(RecordingHandler(), config=config)
        try:
            first = await self.connect()
            await self.handshake(first)
            second_url = f"ws://127.0.0.1:{server.port}/cli/v1/ws"
            second = await ws.connect(second_url)
            self.connections.append(second)
            await self.handshake(second, client_id="box", token="secret-token")
            self.assertEqual(server.connected_client_ids(), ["box", "local"])
            await first.close(ws.CLOSE_NORMAL, "bye")
            for _ in range(200):
                if server.connected_client_ids() == ["box"]:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(server.connected_client_ids(), ["box"])
        finally:
            await self.stop_server()

    async def test_run_raises_on_ctx_cancel(self) -> None:
        server = await self.start_server(RecordingHandler())
        self.ctx.cancel("stop now")
        with self.assertRaises(AtriXError):
            await asyncio.wait_for(self.runner, WAIT)
        self.runner = None
        self.server = None
        self.assertEqual(server.connected_client_ids(), [])

    async def test_run_reports_bind_failure(self) -> None:
        cfg = server_config(server=ServerConfig(enabled=True, listen="127.0.0.1:1", tokens={"a": ["B"]}))
        server = new_remote_server(RemoteServerOptions(config=cfg))
        with self.assertRaises(OSError):
            await asyncio.wait_for(server.run(CancelToken(), RecordingHandler()), WAIT)

    async def test_message_stream_frames(self) -> None:
        import json

        class DummyConnection:
            """记录 ``send_text`` 载荷的连接桩。"""

            def __init__(self) -> None:
                self.written: list[RemoteMessage] = []

            async def send_text(self, payload: str) -> None:
                self.written.append(RemoteMessage.from_json(json.loads(payload)))

        connection = DummyConnection()
        remote = RemoteClientConn(id="local", connection=connection)
        stream = RemoteMessageStream(client=remote)
        await stream.append({}, "")
        self.assertEqual(connection.written, [])
        await stream.append({}, "a")
        receipt = await stream.replace({}, "b")
        self.assertEqual(receipt.platform_message_ids, [])
        await stream.finish({})
        self.assertEqual(
            [msg.type for msg in connection.written],
            ["stream_append", "stream_replace", "stream_finish"],
        )
        self.assertEqual(connection.written[0].text, "a")

    async def test_valid_token_rules(self) -> None:
        server = await self.start_server(RecordingHandler())
        try:
            self.assertTrue(server.valid_token("local", "secret-token"))
            self.assertFalse(server.valid_token("local", "other"))
            self.assertFalse(server.valid_token("unknown", "secret-token"))
            os.environ[TOKEN_ENV] = "   "
            self.assertFalse(server.valid_token("local", ""))
        finally:
            await self.stop_server()

    async def test_write_clients_reports_all_failures(self) -> None:
        server = await self.start_server(RecordingHandler())
        try:
            closed = await self.connect()
            await self.handshake(closed)
            remote = RemoteClientConn(id="local", connection=closed, server=server)
            await closed.close(ws.CLOSE_NORMAL, "bye")
            with self.assertRaises(InvalidError) as ctx:
                await server.write_clients({}, [remote], RemoteMessage(type=REMOTE_MSG_CHAT))
            self.assertIn("send cli message failed", str(ctx.exception))
        finally:
            await self.stop_server()


class TestRemoteSynchronousContract(RemoteServerTestCase):
    """锁住 RemoteServer 侧 ``start_stream`` / ``set_runtime_status`` 的**同步**契约。

    Go 的 ``(*RemoteServer).StartStream`` 与 ``SetRuntimeStatus`` 都是同步方法，调用方
    （``atri_x.agent.turn_output.start_message_stream``、
    ``atri_x.agent.status.publish_runtime_status``）也是同步的：实现一旦写回
    ``async def``，调用点拿到的是协程对象，缺陷表现为「流式 append 抛 AttributeError」
    或「状态帧永不出现」，且都不会报错 —— 所以必须由回归用例从**真实调用路径**锁住。
    """

    async def test_start_stream_is_sync_and_used_by_turn_output(self) -> None:
        server, connection, ctx = await self.start_with_captured_ctx()
        try:
            self.assertFalse(inspect.iscoroutinefunction(server.start_stream))

            # 直接调用与经由 turn_output 的真实路径都必须拿到真 stream。
            direct = server.start_stream(ctx)
            self.assertFalse(inspect.iscoroutine(direct))
            self.assertIsInstance(direct, RemoteMessageStream)
            self.assertIsInstance(direct, MessageStream)

            stream = start_message_stream(FakeAgent(server), ctx)
            self.assertFalse(inspect.iscoroutine(stream))
            self.assertIsInstance(stream, MessageStream)
            await stream.append(ctx, "part")
            await stream.replace(ctx, "final")
            await stream.finish(ctx)
            self.assertEqual(
                await self.read_frame_types(connection, 3),
                ["stream_append", "stream_replace", "stream_finish"],
            )
        finally:
            await self.stop_server()

    async def test_set_runtime_status_is_sync_and_orders_before_next_frame(self) -> None:
        server, connection, ctx = await self.start_with_captured_ctx()
        try:
            self.assertFalse(inspect.iscoroutinefunction(server.set_runtime_status))

            # 经 agent 侧的同步调用点；同步函数必须立刻返回 None（不是协程）。
            self.assertIsNone(
                publish_runtime_status(
                    FakeAgent(server), ctx, Snapshot(session_id="s1", phase="llm")
                )
            )
            # 之后的异步写必须排在状态帧后面（Go 的同步写顺序）。
            await server.send_reasoning(ctx, "thinking")

            self.assertEqual(
                await self.read_frame_types(connection, 2), [REMOTE_MSG_STATUS, "reasoning"]
            )
        finally:
            await self.stop_server()

    async def test_set_runtime_status_is_silent_without_client(self) -> None:
        server = await self.start_server(RecordingHandler())
        try:
            self.assertIsNone(server.set_runtime_status({}, Snapshot(session_id="s")))
            self.assertEqual(server.pending_tasks(), set())
        finally:
            await self.stop_server()

    async def test_set_runtime_status_write_failure_is_discarded(self) -> None:
        """写失败不能抛回同步调用方（对应 Go 里被丢弃的 ``error``），只留一条 debug 日志。"""

        class RecordingLogger:
            def __init__(self) -> None:
                self.records: list[str] = []

            def debug(self, message: str, *args) -> None:
                self.records.append(message % args if args else message)

        server = await self.start_server(RecordingHandler())
        try:
            connection = await self.connect()
            await self.handshake(connection)
            logger = RecordingLogger()
            server.logger = logger
            client = RemoteClientConn(id="local", connection=connection, server=server)
            ctx = with_remote_client({}, client)
            await connection.close(ws.CLOSE_NORMAL, "bye")
            server.set_runtime_status(ctx, Snapshot(session_id="s1", phase="llm"))
            for _ in range(200):
                if not server.pending_tasks():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(server.pending_tasks(), set())
            self.assertEqual(len(logger.records), 1)
            self.assertIn("cli remote status write failed", logger.records[0])
        finally:
            await self.stop_server()


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class TestRemoteClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        os.environ[TOKEN_ENV] = "secret-token"

    def tearDown(self) -> None:
        os.environ.pop(TOKEN_ENV, None)

    def test_construction_errors(self) -> None:
        with self.assertRaises(InvalidError) as ctx:
            new_remote_client(RemoteClientOptions(config=Config(default_client="", clients={})))
        # default_client 为空时 apply_config_defaults 会补回 "local"，配置里没有该档案。
        self.assertIn("cli client", str(ctx.exception))
        self.assertIn("is not configured", str(ctx.exception))

        cfg = Config(clients={"local": ClientConfig(id="local", url="")})
        cfg.default_url = ""
        client = new_remote_client(RemoteClientOptions(config=cfg))
        # url 在 apply_config_defaults 里补成 default_url。
        self.assertTrue(client.url().startswith("ws://"))

    def test_token_resolution(self) -> None:
        client = new_remote_client(RemoteClientOptions(config=server_config()))
        self.assertEqual(client.resolve_token(), "secret-token")
        os.environ.pop(TOKEN_ENV)
        with self.assertRaises(InvalidError) as ctx:
            client.resolve_token()
        self.assertIn("token is not configured", str(ctx.exception))

    def test_no_token_env_configured(self) -> None:
        cfg = server_config(clients={"local": ClientConfig(id="local")})
        client = new_remote_client(RemoteClientOptions(config=cfg))
        with self.assertRaises(InvalidError) as ctx:
            client.resolve_token()
        self.assertIn("token is not configured", str(ctx.exception))

    async def test_handle_server_message_routing(self) -> None:
        client = new_remote_client(RemoteClientOptions(config=server_config()))
        client.handle_server_message(RemoteMessage(type=REMOTE_MSG_CHAT, text="hi"))
        client.handle_server_message(RemoteMessage(type="notice", text="n", level="WARN"))
        client.handle_server_message(RemoteMessage(type="reasoning", text="r"))
        client.handle_server_message(RemoteMessage(type=REMOTE_MSG_STATUS, snapshot=Snapshot(session_id="s")))
        client.handle_server_message(RemoteMessage(type="stream_append", text="sa"))
        client.handle_server_message(RemoteMessage(type="stream_replace", text="sr"))
        client.handle_server_message(RemoteMessage(type="stream_finish"))
        client.handle_server_message(RemoteMessage(type=REMOTE_MSG_ERROR, text="bad"))
        client.handle_server_message(RemoteMessage(type="unknown"))
        names = [type(client.output.get_nowait()).__name__ for _ in range(8)]
        self.assertEqual(
            names,
            [
                "TuiOutputMsg",
                "TuiNoticeMsg",
                "TuiReasoningMsg",
                "TuiStatusMsg",
                "TuiOutputMsg",
                "TuiReplaceAssistantMsg",
                "TuiFinishAssistantMsg",
                "TuiNoticeMsg",
            ],
        )
        self.assertTrue(client.output.empty())

    async def test_handle_server_message_does_not_block_when_full(self) -> None:
        client = new_remote_client(RemoteClientOptions(config=server_config()))
        for _ in range(client.output.maxsize):
            client.output.put_nowait(object())
        client.send_tui(RemoteMessage(type=REMOTE_MSG_CHAT))
        self.assertEqual(client.output.qsize(), client.output.maxsize)

    async def test_complete_result_resolves_pending(self) -> None:
        client = new_remote_client(RemoteClientOptions(config=server_config()))
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        client._pending["c-1"] = queue  # noqa: SLF001 - 直接构造待决请求
        client.handle_server_message(
            RemoteMessage(type=REMOTE_MSG_COMPLETE_RESULT, id="c-1", items=[Item(text="/a")])
        )
        self.assertEqual([item.text for item in queue.get_nowait()], ["/a"])

    async def test_end_to_end_with_server(self) -> None:
        os.environ[TOKEN_ENV] = "secret-token"
        ctx = CancelToken()
        handler = RecordingHandler(reply="echo: ")
        server = new_remote_server(RemoteServerOptions(config=server_config()))
        handler.server = server
        runner = asyncio.ensure_future(server.run(ctx, handler))
        for _ in range(200):
            if server.server is not None:
                break
            await asyncio.sleep(0.01)

        client = new_remote_client(RemoteClientOptions(config=server_config()))
        connection = None
        try:
            connection = await ws.connect(f"ws://127.0.0.1:{server.port}/cli/v1/ws")
            client.connection = connection
            await client.write(
                None,
                RemoteMessage(
                    type=REMOTE_MSG_HELLO, client_id="local", token=client.resolve_token()
                ),
            )
            hello = await asyncio.wait_for(client._read_hello(), WAIT)  # noqa: SLF001
            self.assertEqual(hello.type, REMOTE_MSG_HELLO_OK)

            await client.handle_message(None, "ping")
            for _ in range(200):
                if not client.output.empty():
                    break
                raw = await asyncio.wait_for(connection.recv(), WAIT)
                client.handle_server_message(RemoteMessage.from_json(_decode(raw)))
            message = client.output.get_nowait()
            self.assertEqual(message.text, "echo: ping")
        finally:
            if connection is not None:
                await connection.close(ws.CLOSE_NORMAL, "bye")
            ctx.cancel("done")
            try:
                await asyncio.wait_for(runner, WAIT)
            except (AtriXError, asyncio.CancelledError, asyncio.TimeoutError):
                runner.cancel()

    async def test_rejected_connection_message(self) -> None:
        ctx = CancelToken()
        server = new_remote_server(RemoteServerOptions(config=server_config()))
        runner = asyncio.ensure_future(server.run(ctx, RecordingHandler()))
        for _ in range(200):
            if server.server is not None:
                break
            await asyncio.sleep(0.01)
        connection = None
        try:
            connection = await ws.connect(f"ws://127.0.0.1:{server.port}/cli/v1/ws")
            client = new_remote_client(RemoteClientOptions(config=server_config()))
            client.connection = connection
            await client.write(
                None, RemoteMessage(type=REMOTE_MSG_HELLO, client_id="local", token="nope")
            )
            hello = await asyncio.wait_for(client._read_hello(), WAIT)  # noqa: SLF001
            self.assertEqual(hello.type, REMOTE_MSG_ERROR)
            self.assertEqual(hello.text, "unauthorized")
        finally:
            if connection is not None:
                await connection.close(ws.CLOSE_NORMAL, "bye")
            ctx.cancel("done")
            try:
                await asyncio.wait_for(runner, WAIT)
            except (AtriXError, asyncio.CancelledError, asyncio.TimeoutError):
                runner.cancel()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
