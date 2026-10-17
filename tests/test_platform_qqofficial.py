"""``atri_x.platform.qqofficial`` 的单元测试。

对应 Go：``internal/platform/qqofficial/{config,message,send,receipt,inbound_media}_test.go``。

测试**不连真实 QQ 服务**：HTTP 用假客户端（路由表），WebSocket 用假连接
（脚本化回放帧）。目录型临时文件统一用 ``os.makedirs`` 建在仓库根下并在
``tearDown`` 里 ``shutil.rmtree``（PORTING §7.2）。
"""

from __future__ import annotations

import asyncio
import base64
import json as _json_module
import os
import shutil
import unittest
from datetime import datetime, timezone

from atri_x.delivery.delivery import (
    DeliveryError,
    Notice,
    Output,
    Receipt,
    Source,
    Target as DeliveryTarget,
)
from atri_x.errors import AtriXError, InvalidError
from atri_x.net.httpclient import Response
from atri_x.net.websocket import ConnectionClosed
from atri_x.platform.platform import (
    ConversationKind,
    MessageContext,
    MessageSegmentType,
    ReplyContext,
    message_context_from,
    with_message_context,
)
from atri_x.platform.qqofficial import adapter as adapter_module
from atri_x.platform.qqofficial import gateway as gateway_module
from atri_x.platform.qqofficial.adapter import Adapter, new_adapter
from atri_x.platform.qqofficial.attachment import (
    attachment_declared_mime_type,
    content_disposition_filename,
    extension_from_content_type,
    inbound_attachment_name,
    inbound_attachment_segments,
    is_image_attachment,
    looks_like_qq_download_name,
    sanitize_filename,
)
from atri_x.platform.qqofficial.client import (
    ApiClient,
    PreparedSource,
    prepare_source,
    target_api_path,
)
from atri_x.platform.qqofficial.config import (
    Config,
    apply_defaults,
    new_from_platform_config,
)
from atri_x.platform.qqofficial.gateway import (
    RECONNECT_IDENTIFY,
    RECONNECT_RESUME,
    GatewayState,
    classify_gateway_error,
    handle_dispatch,
    reconnect_mode_text,
    run_gateway_once,
    send_identify,
)
from atri_x.platform.qqofficial.keyboard import (
    risk_keyboard,
    should_attach_risk_keyboard,
)
from atri_x.platform.qqofficial.message import (
    META_EVENT_ID,
    META_MSG_ID,
    inbound_reference_fetcher,
    inbound_reply_id,
    inbound_route,
    is_configured_superadmin,
    normalized_inbound_text,
    parse_rfc3339_nano,
    record_chat_message,
)
from atri_x.platform.qqofficial.send import (
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_MEDIA,
    MSG_TYPE_TEXT,
    QQ_OFFICIAL_TEXT_PAGE_RUNES,
    context_target,
    meta_string,
    qq_official_media_receipt,
    qq_official_text_pages,
    receipt_with_message_id,
    send_text,
)
from atri_x.platform.qqofficial.target import (
    TARGET_C2C,
    TARGET_GROUP,
    SendTarget,
    target_scope,
    with_target,
)
from atri_x.platform.qqofficial.token import (
    TokenSource,
    parse_expires_in,
    parse_expires_text,
)
from atri_x.platform.qqofficial.types import (
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_MESSAGE_CREATE,
    EVENT_READY,
    EVENT_RESUMED,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    InboundMessage,
    MessageArk,
    MessageToCreate,
    Payload,
    ReadyData,
    UploadFileRequest,
)

TMP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_qqofficial_test"
)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class _Ctx:
    """最小上下文替身：支持 ``error()`` 与 ``add_callback``。"""

    def __init__(self) -> None:
        self._error: BaseException | None = None
        self._callbacks: list = []

    def error(self):
        return self._error

    def cancel(self, error: BaseException | None = None) -> None:
        self._error = error or asyncio.CancelledError("cancelled")
        for callback in list(self._callbacks):
            callback()

    def add_callback(self, fn):
        self._callbacks.append(fn)

        def remove() -> None:
            if fn in self._callbacks:
                self._callbacks.remove(fn)

        return remove


def _path_of(url: str) -> str:
    if "://" not in url:
        return url
    rest = url.split("://", 1)[1]
    slash = rest.find("/")
    return rest[slash:] if slash >= 0 else "/"


class _FakeHTTP:
    """假 HTTP 客户端：按 (method, path) 路由。"""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.calls: list[dict] = []
        self.token_body: dict = {"access_token": "token-1", "expires_in": 7200}

    def route(self, method: str, path: str, status: int, body: object) -> None:
        self.routes[(method, path)] = (status, body)

    async def request(
        self,
        method: str,
        url: str,
        *,
        body_json: object = None,
        json: object = None,
        headers: object = None,
        timeout: float | None = None,
        cancel: object = None,
    ) -> Response:
        path = _path_of(url)
        payload_json = json if json is not None else body_json
        self.calls.append(
            {"method": method, "path": path, "json": payload_json, "headers": dict(headers or {})}
        )
        if path.endswith("/getAppAccessToken"):
            status, body = 200, self.token_body
        else:
            status, body = self.routes.get((method, path), (404, {"message": "not found"}))
        payload = body if isinstance(body, str) else _json_module.dumps(body, ensure_ascii=False)
        return Response(status=status, headers={}, body=payload.encode("utf-8"), url=url)


def _config(**kwargs) -> Config:
    base = {"app_id": "app", "client_secret": "secret"}
    base.update(kwargs)
    cfg = Config(**base)
    apply_defaults(cfg)
    return cfg


def _adapter(**kwargs) -> Adapter:
    adapter = new_adapter(_config(**kwargs), None, None, None)
    http = _FakeHTTP()
    adapter.client = ApiClient(adapter.cfg, http=http)
    return adapter


def _client(adapter: Adapter) -> _FakeHTTP:
    return adapter.client.http


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_apply_defaults_matches_go(self) -> None:
        cfg = Config()
        apply_defaults(cfg)
        self.assertEqual(cfg.http_timeout_seconds, 15)
        self.assertEqual(cfg.reconnect_interval_seconds, 3)
        self.assertTrue(cfg.allow_proactive)
        self.assertTrue(cfg.markdown_by_default)
        self.assertTrue(cfg.enable_keyboard)
        self.assertEqual(cfg.max_receive_file_bytes, 100 * 1024 * 1024)
        self.assertEqual(cfg.download_timeout_secs, 60)
        self.assertFalse(cfg.enable_ark)

    def test_predicates(self) -> None:
        cfg = _config(allow_proactive=False, markdown_by_default=False, enable_keyboard=False)
        self.assertFalse(cfg.allow_proactive_messages())
        self.assertFalse(cfg.markdown_by_default_enabled())
        self.assertFalse(cfg.keyboard_enabled())
        self.assertEqual(cfg.http_timeout(), 15.0)
        self.assertEqual(cfg.reconnect_interval(), 3.0)

    def test_enabled_requires_app_id_and_secret(self) -> None:
        with self.assertRaises(InvalidError):
            new_from_platform_config({"enabled": True, "client_secret": "s"}, None, None, None)
        with self.assertRaises(InvalidError):
            new_from_platform_config({"enabled": True, "app_id": "a"}, None, None, None)

    def test_disabled_config_skips_validation(self) -> None:
        adapter = new_from_platform_config({"enabled": False}, None, None, None)
        self.assertFalse(adapter.enabled())
        self.assertEqual(adapter.name(), "qqofficial")

    def test_secret_from_dotenv(self) -> None:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(os.path.join(TMP_DIR, ".env"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("QQ_SECRET=from-dotenv\n")
        previous = os.environ.pop("QQ_SECRET", None)
        try:
            adapter = new_from_platform_config(
                {"enabled": True, "app_id": "a", "client_secret_env": "QQ_SECRET"},
                None,
                None,
                None,
                config_env_dir=TMP_DIR,
            )
            self.assertEqual(adapter.cfg.client_secret, "from-dotenv")
        finally:
            if previous is not None:
                os.environ["QQ_SECRET"] = previous
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_explicit_secret_wins(self) -> None:
        adapter = new_from_platform_config(
            {"enabled": True, "app_id": "a", "client_secret": "inline"},
            None,
            None,
            None,
        )
        self.assertEqual(adapter.cfg.client_secret, "inline")


# ---------------------------------------------------------------------------
# 线上类型（omitempty 保真）
# ---------------------------------------------------------------------------

class TestWireTypes(unittest.TestCase):
    def test_payload_omitempty(self) -> None:
        self.assertEqual(Payload(op=OP_HELLO).to_json(), {"op": OP_HELLO})
        self.assertEqual(
            Payload(id="i", op=OP_DISPATCH, data={"a": 1}, seq=0, type="T").to_json(),
            {"id": "i", "op": OP_DISPATCH, "d": {"a": 1}, "s": 0, "t": "T"},
        )
        self.assertEqual(Payload(op=OP_HEARTBEAT, data=5).to_json(), {"op": OP_HEARTBEAT, "d": 5})
        self.assertEqual(Payload(op=1, data=0).to_json(), {"op": 1, "d": 0})

    def test_message_to_create_keeps_msg_type(self) -> None:
        payload = MessageToCreate(content="hi", msg_type=MSG_TYPE_TEXT).to_json()
        self.assertEqual(payload, {"content": "hi", "msg_type": 0})
        media = MessageToCreate(msg_type=MSG_TYPE_MEDIA)
        media.media = None
        self.assertEqual(media.to_json(), {"msg_type": MSG_TYPE_MEDIA})

    def test_upload_file_request_keeps_srv_send_msg(self) -> None:
        payload = UploadFileRequest(file_type=1, srv_send_msg=False).to_json()
        self.assertEqual(payload, {"file_type": 1, "srv_send_msg": False})

    def test_inbound_message_from_json(self) -> None:
        msg = InboundMessage.from_json(
            {
                "id": "1",
                "group_openid": "g",
                "author": {"member_openid": "m"},
                "content": "hi",
                "timestamp": "2024-01-01T00:00:00Z",
                "attachments": [{"url": "https://x/a.png", "content_type": "image/png"}],
                "message_reference": {"message_id": "9", "content": "ref"},
            }
        )
        self.assertEqual(msg.author.member_openid, "m")
        self.assertEqual(msg.message_reference.message_id, "9")
        self.assertEqual(len(msg.attachments), 1)
        self.assertIsNone(InboundMessage.from_json({"id": "2"}).message_reference)

    def test_ready_data(self) -> None:
        ready = ReadyData.from_json(
            {"version": 1, "session_id": "s", "user": {"id": "b", "username": "bot", "bot": True},
             "shard": [1, 2]}
        )
        self.assertEqual(ready.session_id, "s")
        self.assertEqual(ready.user.username, "bot")
        self.assertEqual(ready.shard, [1, 2])


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

class TestToken(unittest.TestCase):
    def test_parse_expires_in(self) -> None:
        self.assertEqual(parse_expires_in(None), 0)
        self.assertEqual(parse_expires_in(7200), 7200)
        self.assertEqual(parse_expires_in(7200.9), 7200)
        self.assertEqual(parse_expires_in("7200"), 7200)
        self.assertEqual(parse_expires_in('"7200"'), 7200)
        self.assertEqual(parse_expires_text(""), 0)
        with self.assertRaises(InvalidError):
            parse_expires_in("abc")
        with self.assertRaises(InvalidError):
            parse_expires_in(True)

    def test_token_cached_and_forced(self) -> None:
        async def run() -> None:
            http = _FakeHTTP()
            source = TokenSource(_config(), http)
            self.assertEqual(await source.token_value(None), "token-1")
            self.assertEqual(await source.token_value(None), "token-1")
            token_calls = [call for call in http.calls if call["path"].endswith("getAppAccessToken")]
            self.assertEqual(len(token_calls), 1)
            http.token_body = {"access_token": "token-2", "expires_in": 7200}
            self.assertEqual(await source.force_refresh(None), "token-2")
            self.assertTrue(source.valid)

        asyncio.run(run())

    def test_expired_token_is_refreshed(self) -> None:
        async def run() -> None:
            http = _FakeHTTP()
            source = TokenSource(_config(), http)
            await source.token_value(None)
            source.expires_at = 0.0
            self.assertFalse(source.valid)
            self.assertEqual(await source.token_value(None), "token-1")

        asyncio.run(run())

    def test_token_error_status(self) -> None:
        async def run() -> None:
            http = _FakeHTTP()
            source = TokenSource(_config(), http)
            original = http.request

            async def failing(*args, **kwargs):
                return Response(status=500, headers={}, body=b"boom", url=TOKEN_URL)

            http.request = failing
            try:
                with self.assertRaises(AtriXError):
                    await source.token_value(None)
            finally:
                http.request = original

        asyncio.run(run())

    def test_token_code_error(self) -> None:
        async def run() -> None:
            http = _FakeHTTP()
            http.token_body = {"code": 100007, "message": "bad secret"}
            source = TokenSource(_config(), http)
            with self.assertRaises(AtriXError):
                await source.token_value(None)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class TestApiClient(unittest.IsolatedAsyncioTestCase):
    async def test_target_api_path(self) -> None:
        self.assertEqual(
            target_api_path(SendTarget(kind=TARGET_C2C, openid="user-1"), "messages"),
            "/v2/users/user-1/messages",
        )
        self.assertEqual(
            target_api_path(SendTarget(kind=TARGET_GROUP, openid="group-1"), "messages"),
            "/v2/groups/group-1/messages",
        )
        self.assertEqual(
            target_api_path(SendTarget(kind=TARGET_GROUP, openid="a/b"), "files"),
            "/v2/groups/a%2Fb/files",
        )
        with self.assertRaises(InvalidError):
            target_api_path(SendTarget(kind=TARGET_GROUP, openid=""), "files")
        with self.assertRaises(InvalidError):
            target_api_path(SendTarget(kind="unknown", openid="x"), "files")

    async def test_gateway_and_auth_header(self) -> None:
        adapter = _adapter()
        _client(adapter).route("GET", "/gateway", 200, {"url": "wss://gw.example"})
        self.assertEqual(await adapter.client.gateway(None), "wss://gw.example")
        call = [item for item in _client(adapter).calls if item["path"] == "/gateway"][0]
        self.assertEqual(call["headers"]["Authorization"], "QQBot token-1")

    async def test_gateway_empty_url(self) -> None:
        adapter = _adapter()
        _client(adapter).route("GET", "/gateway", 200, {"url": ""})
        with self.assertRaises(AtriXError):
            await adapter.client.gateway(None)

    async def test_api_error_mapping(self) -> None:
        adapter = _adapter()
        _client(adapter).route("POST", "/v2/groups/g/messages", 400, {"message": "too long"})
        with self.assertRaises(AtriXError) as raised:
            await adapter.client.send_message(
                None, SendTarget(kind=TARGET_GROUP, openid="g"), MessageToCreate(msg_type=0)
            )
        self.assertIn("too long", str(raised.exception))

    async def test_api_error_with_code(self) -> None:
        adapter = _adapter()
        _client(adapter).route(
            "POST", "/v2/groups/g/messages", 403, {"code": 11253, "message": "forbidden"}
        )
        with self.assertRaises(AtriXError) as raised:
            await adapter.client.send_message(
                None, SendTarget(kind=TARGET_GROUP, openid="g"), MessageToCreate(msg_type=0)
            )
        self.assertIn("code 11253", str(raised.exception))

    async def test_unauthorized_retries_once_after_refresh(self) -> None:
        adapter = _adapter()
        http = _client(adapter)
        http.route("GET", "/gateway", 401, {"message": "unauthorized"})
        with self.assertRaises(AtriXError):
            await adapter.client.gateway(None)
        paths = [call["path"] for call in http.calls]
        self.assertEqual(paths.count("/gateway"), 2)
        # 两次 /gateway 之间必须夹着一次强制刷新（forceRefresh）。
        gateways = [index for index, path in enumerate(paths) if path == "/gateway"]
        refreshed = [
            index
            for index, path in enumerate(paths)
            if "getAppAccessToken" in path and gateways[0] < index < gateways[1]
        ]
        self.assertEqual(len(refreshed), 1)

    async def test_transport_error_becomes_atrix_error(self) -> None:
        adapter = _adapter()

        async def failing(*args, **kwargs):
            raise OSError("connection reset")

        adapter.client.http.request = failing
        with self.assertRaises(AtriXError):
            await adapter.client.gateway(None)

    async def test_upload_file(self) -> None:
        adapter = _adapter()
        http = _client(adapter)
        http.route("POST", "/v2/groups/g/files", 200, {"file_info": "info"})
        http.route("POST", "/v2/groups/g/messages", 200, {"id": "sent-media"})
        out = Output(kind="image", source=Source(url="https://example.com/image.png"))
        receipt = await adapter.send_notice(
            None,
            Notice(target=DeliveryTarget(group_id="g"), outputs=[out]),
        )
        self.assertEqual(receipt.platform_message_ids, ["sent-media"])
        self.assertEqual(receipt.sent_messages[0].scope_id, "group:g")
        self.assertEqual(receipt.sent_messages[0].output_indexes, [0])
        paths = [call["path"] for call in http.calls if "messages" in call["path"] or "files" in call["path"]]
        self.assertEqual(paths, ["/v2/groups/g/files", "/v2/groups/g/messages"])

    async def test_upload_file_errors(self) -> None:
        adapter = _adapter()
        _client(adapter).route("POST", "/v2/groups/g/files", 200, {"file_info": ""})
        with self.assertRaises(AtriXError):
            await adapter.client.upload_file(
                None,
                SendTarget(kind=TARGET_GROUP, openid="g"),
                1,
                PreparedSource(url="https://x/a.png"),
            )
        with self.assertRaises(InvalidError):
            await adapter.client.upload_file(
                None, SendTarget(kind=TARGET_GROUP, openid="g"), 1, PreparedSource()
            )

    async def test_prepare_source(self) -> None:
        self.assertEqual(prepare_source("https://x/a.png", "", b"").name, "a.png")
        self.assertEqual(prepare_source("", "", b"data").data, b"data")
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, "f.bin")
        with open(path, "wb") as handle:
            handle.write(b"xy")
        try:
            prepared = prepare_source("", path, b"")
            self.assertEqual(prepared.data, b"xy")
            self.assertEqual(prepared.name, "f.bin")
        finally:
            shutil.rmtree(TMP_DIR, ignore_errors=True)
        with self.assertRaises(InvalidError):
            prepare_source("", "", b"")
        with self.assertRaises(AtriXError):
            prepare_source("", os.path.join(TMP_DIR, "missing.bin"), b"")


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------

class _FakeWS:
    """假 WebSocket：脚本化回放帧。"""

    def __init__(self, frames: list) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self, timeout=None):
        if not self.frames:
            raise ConnectionClosed(1000, "done")
        return _json_module.dumps(self.frames.pop(0))

    async def send_text(self, text: str) -> None:
        self.sent.append(_json_module.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _CaptureHandler:
    def __init__(self) -> None:
        self.messages: list[tuple[object, str]] = []

    async def handle_message(self, ctx, text):
        self.messages.append((ctx, text))


class TestGateway(unittest.IsolatedAsyncioTestCase):
    def test_classify_gateway_error(self) -> None:
        self.assertEqual(classify_gateway_error(ConnectionClosed(4009)).mode, RECONNECT_RESUME)
        self.assertEqual(classify_gateway_error(ConnectionClosed(4006)).mode, RECONNECT_IDENTIFY)
        self.assertEqual(classify_gateway_error(ConnectionClosed(4007)).mode, RECONNECT_IDENTIFY)
        for code in (4013, 4014, 4914, 4915):
            reason = classify_gateway_error(ConnectionClosed(code))
            self.assertTrue(reason.fatal, code)
        self.assertEqual(classify_gateway_error(OSError("x")).mode, RECONNECT_IDENTIFY)
        self.assertEqual(reconnect_mode_text(RECONNECT_RESUME), "resume")
        self.assertEqual(reconnect_mode_text(RECONNECT_IDENTIFY), "identify")

    def _patch_ws(self, frames: list) -> _FakeWS:
        fake = _FakeWS(frames)

        async def fake_connect(url, **kwargs):
            return fake

        self._original_connect = gateway_module.ws_connect
        gateway_module.ws_connect = fake_connect
        return fake

    def tearDown(self) -> None:
        original = getattr(self, "_original_connect", None)
        if original is not None:
            gateway_module.ws_connect = original

    async def _prepare(self, frames: list) -> tuple[Adapter, _FakeWS, _CaptureHandler]:
        adapter = _adapter()
        http = _client(adapter)
        http.route("GET", "/gateway", 200, {"url": "wss://gw.example"})
        fake = self._patch_ws(frames)
        return adapter, fake, _CaptureHandler()

    async def test_identify_and_dispatch(self) -> None:
        adapter, fake, handler = await self._prepare(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}},
                {
                    "op": OP_DISPATCH,
                    "t": EVENT_C2C_MESSAGE_CREATE,
                    "s": 3,
                    "d": {
                        "id": "m1",
                        "author": {"user_openid": "u1"},
                        "content": "hi",
                        "timestamp": "2024-01-01T00:00:00Z",
                    },
                },
            ]
        )
        state = GatewayState()
        reason, error = await run_gateway_once(adapter, None, handler, state)
        self.assertIsInstance(error, ConnectionClosed)
        self.assertEqual(reason.mode, RECONNECT_IDENTIFY)
        self.assertEqual(state.seq, 3)
        self.assertEqual(fake.sent[0]["op"], OP_IDENTIFY)
        self.assertEqual(fake.sent[0]["d"]["token"], "QQBot token-1")
        self.assertEqual(fake.sent[0]["d"]["intents"], 1 << 25)
        self.assertEqual(fake.sent[0]["d"]["shard"], [0, 1])
        await asyncio.gather(*adapter.pending_tasks(), return_exceptions=True)
        self.assertEqual(len(handler.messages), 1)
        self.assertEqual(handler.messages[0][1], "hi")
        self.assertTrue(fake.closed)

    async def test_ready_spawns_notifier_and_resumes(self) -> None:
        adapter, fake, handler = await self._prepare(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}},
                {
                    "op": OP_DISPATCH,
                    "t": EVENT_READY,
                    "d": {"session_id": "sess-1", "user": {"id": "bot", "username": "n"}},
                },
                {"op": OP_DISPATCH, "t": EVENT_RESUMED},
                {"op": OP_RECONNECT},
            ]
        )
        seen: list[str] = []
        adapter.set_connect_notifier(lambda ctx, platform: seen.append(platform))
        state = GatewayState()
        reason, error = await run_gateway_once(adapter, None, handler, state)
        self.assertIsNone(error)
        self.assertEqual(reason.mode, RECONNECT_RESUME)
        self.assertEqual(state.session_id, "sess-1")
        self.assertTrue(state.resume)
        await asyncio.gather(*adapter.pending_tasks(), return_exceptions=True)
        self.assertEqual(seen, ["qqofficial"])
        self.assertTrue(fake.closed)

    async def test_resume_path_uses_op_resume(self) -> None:
        adapter, fake, handler = await self._prepare(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}},
                {"op": OP_RECONNECT},
            ]
        )
        state = GatewayState(session_id="sess-1", seq=7, resume=True)
        reason, _error = await run_gateway_once(adapter, None, handler, state)
        self.assertEqual(reason.mode, RECONNECT_RESUME)
        self.assertEqual(fake.sent[0]["op"], OP_RESUME)
        self.assertEqual(fake.sent[0]["d"]["session_id"], "sess-1")
        self.assertEqual(fake.sent[0]["d"]["seq"], 7)

    async def test_invalid_session_clears_state(self) -> None:
        adapter, _fake, handler = await self._prepare(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}},
                {"op": OP_INVALID_SESSION},
            ]
        )
        state = GatewayState(session_id="sess-1", resume=True)
        reason, error = await run_gateway_once(adapter, None, handler, state)
        self.assertEqual(reason.mode, RECONNECT_IDENTIFY)
        self.assertFalse(state.resume)
        self.assertEqual(state.session_id, "")
        self.assertIsInstance(error, AtriXError)

    async def test_missing_hello_is_error(self) -> None:
        adapter, _fake, handler = await self._prepare([{"op": OP_DISPATCH, "t": EVENT_RESUMED}])
        reason, error = await run_gateway_once(adapter, None, handler, GatewayState())
        self.assertEqual(reason.mode, RECONNECT_IDENTIFY)
        self.assertIsInstance(error, AtriXError)

    async def test_gateway_fetch_failure(self) -> None:
        adapter = _adapter()
        _client(adapter).route("GET", "/gateway", 500, {"message": "boom"})
        reason, error = await run_gateway_once(adapter, None, _CaptureHandler(), GatewayState())
        self.assertEqual(reason.mode, RECONNECT_IDENTIFY)
        self.assertIsInstance(error, AtriXError)

    async def test_dispatch_ignores_unknown_event(self) -> None:
        adapter = _adapter()
        state = GatewayState()
        await handle_dispatch(
            adapter, None, _CaptureHandler(), Payload(op=OP_DISPATCH, type="UNKNOWN"), state
        )
        self.assertEqual(adapter.pending_tasks(), set())

    async def test_group_message_dispatch(self) -> None:
        adapter, _fake, handler = await self._prepare(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 45000}},
                {
                    "op": OP_DISPATCH,
                    "t": EVENT_GROUP_MESSAGE_CREATE,
                    "d": {
                        "id": "m1",
                        "group_openid": "g1",
                        "author": {"member_openid": "u1"},
                        "content": "hi",
                    },
                },
            ]
        )
        await run_gateway_once(adapter, None, handler, GatewayState())
        await asyncio.gather(*adapter.pending_tasks(), return_exceptions=True)
        self.assertEqual(len(handler.messages), 1)

    async def test_send_identify_requires_token(self) -> None:
        adapter = _adapter()
        http = _client(adapter)
        http.token_body = {"access_token": "", "expires_in": 1}
        with self.assertRaises(AtriXError):
            await send_identify(adapter, None, _FakeWS([]))

    async def test_heartbeat_loop_sends_frames(self) -> None:
        adapter = _adapter()
        conn = _FakeWS([])
        task = asyncio.ensure_future(
            gateway_module.heartbeat_loop(adapter, None, conn, GatewayState(seq=4), 0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertGreaterEqual(len(conn.sent), 2)
        self.assertEqual(conn.sent[0]["op"], 1)
        self.assertEqual(conn.sent[0]["d"], 4)


# ---------------------------------------------------------------------------
# 入站消息
# ---------------------------------------------------------------------------

class TestInboundMessage(unittest.TestCase):
    def test_inbound_route(self) -> None:
        group = InboundMessage.from_json(
            {"id": "1", "group_openid": "g", "author": {"member_openid": "m"}}
        )
        self.assertEqual(
            inbound_route(group, ConversationKind.GROUP),
            ("m", "group:g", TARGET_GROUP, "g"),
        )
        private = InboundMessage.from_json({"id": "1", "author": {"user_openid": "u"}})
        self.assertEqual(
            inbound_route(private, ConversationKind.PRIVATE),
            ("u", "c2c:u", TARGET_C2C, "u"),
        )

    def test_normalized_text(self) -> None:
        msg = InboundMessage.from_json({"content": "<faceType=1>你好 <@!123> 世界"})
        # ``<faceType=...>`` 一律剔除；@ 前缀规则只在群 @ 事件上生效。
        self.assertEqual(normalized_inbound_text(msg, False), "你好 <@!123> 世界")
        self.assertEqual(normalized_inbound_text(msg, True), "你好 <@!123> 世界")
        at_msg = InboundMessage.from_json({"content": "<@!123> 继续"})
        self.assertEqual(normalized_inbound_text(at_msg, True), "继续")
        self.assertEqual(normalized_inbound_text(at_msg, False), "<@!123> 继续")
        # Go 的 ``^`` 只锚定文本开头：剥掉第一段后不会再匹配第二段。
        two_ats = InboundMessage.from_json({"content": "  @ATRI <@!123>  继续"})
        self.assertEqual(normalized_inbound_text(two_ats, True), "<@!123>  继续")

    def test_reply_id_and_fetcher(self) -> None:
        msg = InboundMessage.from_json(
            {"id": "1", "message_reference": {"message_id": "9", "content": "原文"}}
        )
        self.assertEqual(inbound_reply_id(msg), "9")
        referenced, ok = inbound_reference_fetcher(msg)(None, "9")
        self.assertTrue(ok)
        self.assertEqual(referenced.text, "原文")
        self.assertEqual(referenced.label, "引用")
        self.assertEqual(
            [segment.type for segment in referenced.segments], [MessageSegmentType.TEXT]
        )
        self.assertEqual(inbound_reference_fetcher(msg)(None, "8")[1], False)
        empty = InboundMessage.from_json({"id": "1"})
        self.assertFalse(inbound_reference_fetcher(empty)(None, "9")[1])

    def test_superadmin_matching(self) -> None:
        self.assertTrue(is_configured_superadmin(["qqofficial:u1"], "u1"))
        self.assertFalse(is_configured_superadmin(None, "u1"))
        self.assertFalse(is_configured_superadmin(["u1"], ""))

    def test_parse_rfc3339_nano(self) -> None:
        self.assertEqual(
            parse_rfc3339_nano("2024-01-02T03:04:05Z"),
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        parsed = parse_rfc3339_nano("2024-01-02T03:04:05.123456789+08:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.microsecond, 123456)
        self.assertEqual(parsed.hour, 19)
        self.assertIsNone(parse_rfc3339_nano("not a time"))
        self.assertIsNone(parse_rfc3339_nano("2024-01-02T03:04:05"))

    def test_record_chat_message(self) -> None:
        class _History:
            def __init__(self) -> None:
                self.rows = []

            def append(self, row) -> None:
                self.rows.append(row)

        adapter = _adapter()
        adapter.chat_history = _History()
        msg = InboundMessage.from_json(
            {
                "id": "m1",
                "group_openid": "g",
                "author": {"member_openid": "u"},
                "content": "<faceType=1>hi",
                "timestamp": "2024-01-02T03:04:05Z",
                "attachments": [{"url": "https://x/a.png", "content_type": "image/png"}],
            }
        )
        record_chat_message(
            adapter,
            None,
            msg,
            ConversationKind.GROUP,
            "u",
            "group:g",
            "hi",
            "9",
            ReplyContext(),
        )
        row = adapter.chat_history.rows[0]
        self.assertEqual(row.platform, "qqofficial")
        self.assertEqual(row.platform_scope_id, "group:g")
        self.assertEqual(row.scope_type, "group")
        self.assertEqual(row.platform_message_id, "m1")
        self.assertEqual(row.raw, "<faceType=1>hi")
        self.assertEqual(row.created_at, datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertIn("image", row.segments)

    def test_record_chat_message_skips(self) -> None:
        class _History:
            def __init__(self) -> None:
                self.rows = []

            def append(self, row) -> None:
                self.rows.append(row)

        adapter = _adapter()
        adapter.chat_history = _History()
        record_chat_message(
            adapter, None, InboundMessage(), ConversationKind.PRIVATE, "u", "c2c:u", "", "", ReplyContext()
        )
        self.assertEqual(adapter.chat_history.rows, [])


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------

class TestAttachment(unittest.TestCase):
    def test_sanitize_and_download_name(self) -> None:
        self.assertEqual(sanitize_filename('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i")
        self.assertEqual(sanitize_filename("  x.  "), "x")
        self.assertTrue(looks_like_qq_download_name("aQQDownloadb"))
        self.assertTrue(looks_like_qq_download_name("robot1.0_x"))
        self.assertTrue(looks_like_qq_download_name("x" * 97))
        self.assertFalse(looks_like_qq_download_name("photo.png"))

    def test_content_disposition_and_extension(self) -> None:
        self.assertEqual(
            content_disposition_filename('attachment; filename="photo.png"'), "photo.png"
        )
        self.assertEqual(content_disposition_filename("attachment"), "")
        self.assertEqual(content_disposition_filename(""), "")
        self.assertEqual(extension_from_content_type("image/jpeg; charset=binary"), ".jpg")
        self.assertEqual(extension_from_content_type("application/octet-stream"), "")
        self.assertEqual(extension_from_content_type("FILE"), "")

    def test_is_image_attachment(self) -> None:
        from atri_x.platform.qqofficial.types import MessageAttachment

        self.assertTrue(
            is_image_attachment(MessageAttachment(url="https://x/a.png", content_type="image/png"))
        )
        self.assertFalse(
            is_image_attachment(MessageAttachment(url="https://x/a.bin", content_type="file"))
        )
        self.assertTrue(is_image_attachment(MessageAttachment(url="https://x/a", width=10)))
        self.assertFalse(is_image_attachment(MessageAttachment(url="https://x/a")))

    def test_inbound_attachment_segments(self) -> None:
        from atri_x.platform.qqofficial.types import MessageAttachment

        image = MessageAttachment(url="https://x/i.png", content_type="image/png", filename="i.png")
        plain = MessageAttachment(url="https://x/f", content_type="file", filename="f.txt", size=5)
        segments = inbound_attachment_segments([image, plain, image])
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0].type, MessageSegmentType.IMAGE)
        self.assertEqual(segments[1].type, MessageSegmentType.FILE)
        self.assertEqual(segments[1].name, "f.txt")
        self.assertEqual(segments[1].size, 5)
        self.assertEqual(segments[2].to_json(), segments[0].to_json())
        # 声明类型 ``file`` 是"没有 MIME"的约定写法：段上落成空串。
        self.assertEqual(attachment_declared_mime_type(plain), "file")
        self.assertEqual(segments[1].mime_type, "")

    def test_inbound_attachment_name_fallbacks(self) -> None:
        from atri_x.platform.qqofficial.types import MessageAttachment

        self.assertEqual(
            inbound_attachment_name(MessageAttachment(url="https://x/dir/a.png")), "a.png"
        )
        self.assertEqual(
            inbound_attachment_name(
                MessageAttachment(url="https://x/QQDownload"), None, 2
            ),
            "attachment-2",
        )
        # URL 路径为空 → 退回 attachment-N；此时才用 Content-Type 补扩展名。
        self.assertEqual(
            inbound_attachment_name(
                MessageAttachment(url="https://x/"),
                {"Content-Type": "image/png"},
                3,
            ),
            "attachment-3.png",
        )
        # URL 路径有名字就用它（Go 的 filepath.Base 分支）。
        self.assertEqual(
            inbound_attachment_name(
                MessageAttachment(url="https://x/a"),
                {"Content-Type": "image/png"},
                3,
            ),
            "a",
        )
        self.assertEqual(
            inbound_attachment_name(
                MessageAttachment(url="https://x/a"),
                {"Content-Disposition": 'attachment; filename="from-header.txt"'},
                1,
            ),
            "from-header.txt",
        )


# ---------------------------------------------------------------------------
# 出站
# ---------------------------------------------------------------------------

class TestSend(unittest.IsolatedAsyncioTestCase):
    def test_text_pages(self) -> None:
        pages = qq_official_text_pages("界" * (QQ_OFFICIAL_TEXT_PAGE_RUNES + 1))
        self.assertEqual(len(pages), 2)
        for page in pages:
            self.assertLessEqual(len(page), QQ_OFFICIAL_TEXT_PAGE_RUNES)
        self.assertIn("1/2", pages[0])
        self.assertIn("2/2", pages[1])

    def test_receipt_helpers(self) -> None:
        self.assertEqual(receipt_with_message_id(" 7 ").platform_message_ids, ["7"])
        self.assertEqual(receipt_with_message_id("").platform_message_ids, [])
        target = SendTarget(kind=TARGET_GROUP, openid="g")
        receipt = qq_official_media_receipt(
            Receipt(platform_message_ids=["1"]), target, Output(kind="image"), 2
        )
        self.assertEqual(receipt.sent_messages[0].scope_id, "group:g")
        self.assertEqual(receipt.sent_messages[0].output_indexes, [2])
        self.assertEqual(
            qq_official_media_receipt(Receipt(), target, Output(kind="image"), 0).sent_messages, []
        )
        self.assertEqual(target_scope(SendTarget(kind=TARGET_C2C, openid="u")), "c2c:u")
        self.assertEqual(target_scope(SendTarget()), "")
        self.assertEqual(meta_string({"a": " x "}, "a"), "x")
        self.assertEqual(meta_string(None, "a"), "")

    async def test_send_chat_uses_group_api(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        http = _client(adapter)
        http.route("POST", "/v2/groups/group-1/messages", 200, {"id": "sent-1"})
        ctx = with_target(None, SendTarget(kind=TARGET_GROUP, openid="group-1", msg_id="incoming-1"))
        receipt = await adapter.send_chat(ctx, [Output(kind="text", text="hello")])
        self.assertEqual(receipt.platform_message_ids, ["sent-1"])
        body = [call for call in http.calls if call["path"].endswith("/messages")][0]["json"]
        self.assertEqual(body["content"], "hello")
        self.assertEqual(body["msg_id"], "incoming-1")
        self.assertEqual(body["msg_seq"], 1)
        self.assertEqual(body["msg_type"], MSG_TYPE_TEXT)

    async def test_markdown_default(self) -> None:
        adapter = _adapter()
        http = _client(adapter)
        http.route("POST", "/v2/users/u/messages", 200, {"id": "1"})
        ctx = with_target(None, SendTarget(kind=TARGET_C2C, openid="u"))
        await adapter.send_chat(ctx, [Output(kind="text", text="hi")])
        body = http.calls[-1]["json"]
        self.assertEqual(body["msg_type"], MSG_TYPE_MARKDOWN)
        self.assertEqual(body["markdown"], {"content": "hi"})

    async def test_markdown_failure_falls_back_to_text(self) -> None:
        adapter = _adapter()
        http = _client(adapter)
        http.route("POST", "/v2/users/u/messages", 400, {"message": "bad"})
        ctx = with_target(None, SendTarget(kind=TARGET_C2C, openid="u"))
        with self.assertRaises(AtriXError):
            await adapter.send_chat(ctx, [Output(kind="text", text="hi")])
        # 第一次 markdown 失败、且没有已投递分页 → 降级纯文本再试一次。
        sent = [call for call in http.calls if call["path"] == "/v2/users/u/messages"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["json"]["msg_type"], MSG_TYPE_MARKDOWN)
        self.assertEqual(sent[1]["json"]["msg_type"], MSG_TYPE_TEXT)

    async def test_partial_page_failure_keeps_receipt(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        http = _client(adapter)
        http.route("POST", "/v2/groups/g/messages", 200, {"id": "sent-1"})
        ctx = with_target(None, SendTarget(kind=TARGET_GROUP, openid="g"))
        text = "界" * (QQ_OFFICIAL_TEXT_PAGE_RUNES + 1)
        calls = {"n": 0}

        original = adapter.client.send_message

        async def flaky(ctx_arg, target, msg):
            calls["n"] += 1
            if calls["n"] == 2:
                raise AtriXError("too long")
            return await original(ctx_arg, target, msg)

        adapter.client.send_message = flaky
        with self.assertRaises(DeliveryError) as raised:
            await adapter.send_chat(ctx, [Output(kind="text", text=text)])
        receipt = raised.exception.receipt
        self.assertTrue(receipt.failed)
        self.assertEqual(receipt.platform_message_ids, ["sent-1"])
        self.assertIn("page 2/2", receipt.failure)
        self.assertEqual(calls["n"], 2)

    async def test_media_send(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        http = _client(adapter)
        http.route("POST", "/v2/users/u/files", 200, {"file_info": "info"})
        http.route("POST", "/v2/users/u/messages", 200, {"id": "m"})
        ctx = with_target(None, SendTarget(kind=TARGET_C2C, openid="u"))
        receipt = await adapter.send_chat(
            ctx, [Output(kind="image", source=Source(data=b"png"))]
        )
        self.assertEqual(receipt.platform_message_ids, ["m"])
        upload = [call for call in http.calls if call["path"].endswith("/files")][0]
        self.assertEqual(upload["json"]["file_type"], 1)
        self.assertEqual(upload["json"]["file_data"], base64.b64encode(b"png").decode())
        self.assertFalse(upload["json"]["srv_send_msg"])
        message = [call for call in http.calls if call["path"].endswith("/messages")][0]
        self.assertEqual(message["json"]["msg_type"], MSG_TYPE_MEDIA)
        self.assertEqual(message["json"]["media"], {"file_info": "info"})

    async def test_group_tool_preview_skipped_and_plain_kept(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        http = _client(adapter)
        http.route("POST", "/v2/groups/g/messages", 200, {"id": "sent"})
        ctx = with_target(None, SendTarget(kind=TARGET_GROUP, openid="g"))
        dropped = await adapter.send_notice(
            ctx, Notice(outputs=[Output(kind="text", text="[tool] 调用中")])
        )
        self.assertEqual(dropped.platform_message_ids, [])
        self.assertEqual(http.calls, [])
        kept = await adapter.send_notice(ctx, Notice(outputs=[Output(kind="text", text="普通通知")]))
        self.assertEqual(kept.platform_message_ids, ["sent"])
        self.assertEqual(http.calls[-1]["json"]["content"], "普通通知")

    async def test_private_tool_preview_kept(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        http = _client(adapter)
        http.route("POST", "/v2/users/u/messages", 200, {"id": "sent"})
        ctx = with_target(None, SendTarget(kind=TARGET_C2C, openid="u"))
        receipt = await adapter.send_notice(
            ctx, Notice(outputs=[Output(kind="text", text="[tool] 调用中")])
        )
        self.assertEqual(receipt.platform_message_ids, ["sent"])

    async def test_proactive_disabled(self) -> None:
        adapter = _adapter(allow_proactive=False, markdown_by_default=False)
        _client(adapter).route("POST", "/v2/groups/g/messages", 200, {"id": "x"})
        with self.assertRaises(DeliveryError):
            await adapter.send_notice(
                None,
                Notice(target=DeliveryTarget(group_id="g"), outputs=[Output(kind="text", text="hi")],
            ))

    async def test_proactive_allowed_by_default(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        _client(adapter).route("POST", "/v2/groups/g/messages", 200, {"id": "x"})
        receipt = await adapter.send_notice(
            None, Notice(target=DeliveryTarget(group_id="g"), outputs=[Output(kind="text", text="hi")])
        )
        self.assertEqual(receipt.platform_message_ids, ["x"])

    async def test_targets_expansion(self) -> None:
        adapter = _adapter()
        adapter.cfg.superadmins = ["qqofficial:u1", " u2 ", ""]
        targets = adapter.targets(DeliveryTarget(superadmins=True))
        self.assertEqual(
            [(item.kind, item.openid) for item in targets],
            [(TARGET_C2C, "u1"), (TARGET_C2C, "u2")],
        )
        self.assertEqual(
            adapter.targets(DeliveryTarget(private_user_id=" u "))[0].openid, "u"
        )
        self.assertEqual(
            adapter.targets(DeliveryTarget(group_id=" g "))[0].kind, TARGET_GROUP
        )
        self.assertEqual(adapter.targets(DeliveryTarget(scope_id="c2c:u"))[0].kind, TARGET_C2C)
        self.assertEqual(
            adapter.targets(DeliveryTarget(scope_id="group:g"))[0].openid, "g"
        )
        with self.assertRaises(AtriXError):
            adapter.targets(DeliveryTarget())
        with self.assertRaises(InvalidError):
            adapter.targets(DeliveryTarget(platform="telegram", group_id="g"))

    async def test_superadmin_send_fans_out(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        adapter.cfg.superadmins = ["u1", "u2"]
        http = _client(adapter)
        http.route("POST", "/v2/users/u1/messages", 200, {"id": "1"})
        http.route("POST", "/v2/users/u2/messages", 200, {"id": "2"})
        receipt = await adapter.send_notice(
            None,
            Notice(target=DeliveryTarget(superadmins=True), outputs=[Output(kind="text", text="a")]),
        )
        self.assertEqual(receipt.platform_message_ids, ["1", "2"])
        calls = [call for call in http.calls if call["path"].endswith("/messages")]
        self.assertEqual([call["path"] for call in calls], ["/v2/users/u1/messages", "/v2/users/u2/messages"])

    async def test_superadmin_partial_failure(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        adapter.cfg.superadmins = ["u1", "u2"]
        http = _client(adapter)
        http.route("POST", "/v2/users/u1/messages", 200, {"id": "1"})
        http.route("POST", "/v2/users/u2/messages", 500, {"message": "boom"})
        with self.assertRaises(DeliveryError) as raised:
            await adapter.send_notice(
                None,
                Notice(
                    target=DeliveryTarget(superadmins=True),
                    outputs=[Output(kind="text", text="a")],
                ),
            )
        self.assertEqual(raised.exception.receipt.platform_message_ids, ["1"])
        self.assertTrue(raised.exception.receipt.failed)

    async def test_context_target_from_message_context(self) -> None:
        adapter = _adapter()
        message_ctx = MessageContext(
            platform="qqofficial",
            scope_id="group:g",
            platform_message_id="m1",
            meta={META_MSG_ID: "m1", META_EVENT_ID: "e1"},
        )
        ctx = with_message_context(None, message_ctx)
        target = await context_target(adapter, ctx)
        self.assertEqual((target.kind, target.openid), (TARGET_GROUP, "g"))
        self.assertEqual(target.msg_id, "m1")
        self.assertEqual(target.event_id, "e1")
        with self.assertRaises(InvalidError):
            await context_target(adapter, None)

    async def test_next_msg_seq_increments(self) -> None:
        adapter = _adapter()
        self.assertEqual(adapter.next_msg_seq(" "), 0)
        self.assertEqual(adapter.next_msg_seq("m"), 1)
        self.assertEqual(adapter.next_msg_seq("m"), 2)
        self.assertEqual(adapter.next_msg_seq("other"), 1)

    async def test_base_message_prefers_msg_id(self) -> None:
        adapter = _adapter()
        msg = adapter.base_message(SendTarget(msg_id="m", event_id="e"))
        self.assertEqual(msg.msg_id, "m")
        self.assertEqual(msg.msg_seq, 1)
        self.assertEqual(msg.event_id, "")
        event_only = adapter.base_message(SendTarget(event_id="e"))
        self.assertEqual(event_only.event_id, "e")
        self.assertEqual(event_only.msg_id, "")

    async def test_ark_disabled_and_enabled(self) -> None:
        adapter = _adapter(markdown_by_default=False)
        _client(adapter).route("POST", "/v2/groups/g/messages", 200, {"id": "ark"})
        with self.assertRaises(InvalidError):
            await adapter.send_ark(None, SendTarget(kind=TARGET_GROUP, openid="g"), MessageArk(1))
        adapter.cfg.enable_ark = True
        receipt = await adapter.send_ark(
            None, SendTarget(kind=TARGET_GROUP, openid="g"), MessageArk(template_id=1)
        )
        self.assertEqual(receipt.platform_message_ids, ["ark"])
        self.assertEqual(_client(adapter).calls[-1]["json"]["msg_type"], 3)

    async def test_keyboard_attach_rules(self) -> None:
        self.assertFalse(should_attach_risk_keyboard("高风险工具调用等待确认", "/"))
        self.assertTrue(should_attach_risk_keyboard("高风险工具调用等待确认 /confirm", "/"))
        self.assertFalse(should_attach_risk_keyboard("高风险工具调用等待确认 !confirm", "/"))
        keyboard = risk_keyboard("app", "/")
        rows = keyboard.content.rows
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].buttons[0].action.data, "/detail")
        self.assertEqual(rows[0].buttons[1].action.data, "/confirm")
        self.assertEqual(rows[2].buttons[1].action.data, "/stop")
        payload = keyboard.to_json()
        self.assertEqual(payload["content"]["bot_appid"], "app")
        self.assertEqual(payload["content"]["rows"][0]["buttons"][0]["render_data"]["label"], "详情")

    async def test_keyboard_attached_on_first_page_only(self) -> None:
        adapter = _adapter()
        http = _client(adapter)
        http.route("POST", "/v2/groups/g/messages", 200, {"id": "1"})
        ctx = with_target(None, SendTarget(kind=TARGET_GROUP, openid="g"))
        text = "高风险工具调用等待确认 /confirm " + "界" * QQ_OFFICIAL_TEXT_PAGE_RUNES
        await adapter.send_chat(ctx, [Output(kind="text", text=text)])
        bodies = [call["json"] for call in http.calls if call["path"].endswith("/messages")]
        self.assertGreaterEqual(len(bodies), 2)
        self.assertIn("keyboard", bodies[0])
        self.assertNotIn("keyboard", bodies[1])


# ---------------------------------------------------------------------------
# 适配器生命周期
# ---------------------------------------------------------------------------

class TestAdapterLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_run_is_noop(self) -> None:
        adapter = _adapter()
        await adapter.run(None, _CaptureHandler())

    async def test_run_loops_until_ctx_cancelled(self) -> None:
        adapter = _adapter(enabled=True, reconnect_interval_seconds=1)
        calls: list[str] = []

        async def fake_once(ctx, handler, state):
            calls.append("once")
            return gateway_module.ReconnectReason(mode=RECONNECT_IDENTIFY), AtriXError("boom")

        adapter.run_gateway_once = fake_once
        ctx = _Ctx()
        task = asyncio.ensure_future(adapter.run(ctx, _CaptureHandler()))
        await asyncio.sleep(0.05)
        ctx.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, ["once"])

    async def test_fatal_reason_raises(self) -> None:
        adapter = _adapter(enabled=True)

        async def fatal_once(ctx, handler, state):
            return (
                gateway_module.ReconnectReason(mode=RECONNECT_IDENTIFY, fatal=True),
                AtriXError("bad token"),
            )

        adapter.run_gateway_once = fatal_once
        with self.assertRaises(AtriXError):
            await adapter.run(None, _CaptureHandler())

    async def test_fatal_reason_without_error(self) -> None:
        adapter = _adapter(enabled=True)

        async def fatal_once(ctx, handler, state):
            return gateway_module.ReconnectReason(mode=RECONNECT_IDENTIFY, fatal=True), None

        adapter.run_gateway_once = fatal_once
        with self.assertRaises(AtriXError):
            await adapter.run(None, _CaptureHandler())

    async def test_notify_connected(self) -> None:
        adapter = _adapter()
        seen: list[str] = []
        adapter.set_connect_notifier(lambda ctx, platform: seen.append(platform))
        await adapter.notify_connected(None)
        self.assertEqual(seen, ["qqofficial"])
        adapter.set_connect_notifier(None)
        await adapter.notify_connected(None)

    async def test_inbound_handler_receives_context(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        msg = InboundMessage.from_json(
            {
                "id": "m1",
                "group_openid": "g",
                "author": {"member_openid": "u"},
                "content": "<faceType=1>@ATRI hi",
            }
        )
        from atri_x.platform.qqofficial.types import Payload

        payload = Payload(
            id="e1", op=0, type="GROUP_AT_MESSAGE_CREATE",
            data={"id": "m1", "group_openid": "g", "author": {"member_openid": "u"},
                  "content": "<faceType=1>@ATRI hi"},
        )
        await adapter.handle_group_message(None, handler, payload, msg)
        self.assertEqual(len(handler.messages), 1)
        ctx, text = handler.messages[0]
        self.assertEqual(text, "hi")
        message_ctx, found = message_context_from(ctx)
        self.assertTrue(found)
        self.assertEqual(message_ctx.scope_id, "group:g")
        self.assertEqual(message_ctx.actor_id, "qqofficial:u")
        self.assertEqual(message_ctx.bot.user_id, "app")
        self.assertEqual(
            [mention.user_id for mention in message_ctx.mentions], ["app"]
        )
        self.assertEqual(message_ctx.meta[META_EVENT_ID], "e1")
        self.assertEqual(
            _json_module.loads(message_ctx.platform_message)["group_openid"], "g"
        )
        from atri_x.platform.qqofficial.target import target_from_context

        send_target, has_target = target_from_context(ctx)
        self.assertTrue(has_target)
        self.assertEqual(send_target.openid, "g")

    async def test_inbound_missing_sender_is_dropped(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        payload = Payload(type=EVENT_GROUP_MESSAGE_CREATE)
        msg = InboundMessage.from_json({"id": "m1", "group_openid": "g"})
        await adapter.handle_group_message(None, handler, payload, msg)
        self.assertEqual(handler.messages, [])

    async def test_inbound_empty_content_is_dropped(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        payload = Payload(type=EVENT_GROUP_MESSAGE_CREATE)
        msg = InboundMessage.from_json(
            {"id": "m1", "group_openid": "g", "author": {"member_openid": "u"}, "content": ""}
        )
        await adapter.handle_group_message(None, handler, payload, msg)
        self.assertEqual(handler.messages, [])

    async def test_inbound_c2c_sets_c2c_scope(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        payload = Payload(type=EVENT_C2C_MESSAGE_CREATE)
        msg = InboundMessage.from_json(
            {"id": "m1", "author": {"user_openid": "u"}, "content": "hi"}
        )
        await adapter.handle_c2c_message(None, handler, payload, msg)
        message_ctx, _found = message_context_from(handler.messages[0][0])
        self.assertEqual(message_ctx.scope_id, "c2c:u")
        self.assertEqual(message_ctx.conversation_kind, ConversationKind.PRIVATE)
        self.assertEqual(message_ctx.mentions, [])

    async def test_inbound_reply_builds_context(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        payload = Payload(
            type=EVENT_C2C_MESSAGE_CREATE,
            data={
                "id": "m1",
                "author": {"user_openid": "u"},
                "content": "继续",
                "message_reference": {"message_id": "9", "content": "上文"},
            },
        )
        msg = InboundMessage.from_json(payload.data)
        await adapter.handle_c2c_message(None, handler, payload, msg)
        message_ctx, _found = message_context_from(handler.messages[0][0])
        self.assertEqual(message_ctx.reply_to_message_id, "9")
        self.assertIn("上文", message_ctx.context_text)
        # Go 的引用标签是裸 "引用"，referenceSenderName 因此给出空的名字。
        self.assertEqual(message_ctx.reply.sender_name, "")
        self.assertEqual(message_ctx.reply.message_id, "9")

    async def test_sleep_context_and_raise(self) -> None:
        from atri_x.platform.qqofficial.adapter import raise_context_error, sleep_context

        ctx = _Ctx()
        self.assertTrue(await sleep_context(ctx, 0.01))
        ctx.cancel()
        self.assertFalse(await sleep_context(ctx, 0.05))
        with self.assertRaises(asyncio.CancelledError):
            raise_context_error(ctx)


if __name__ == "__main__":
    unittest.main()
