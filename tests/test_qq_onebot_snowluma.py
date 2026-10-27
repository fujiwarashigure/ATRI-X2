"""SnowLuma / NapCat 协议差异的用例（批次②：协议与媒体适配）。

覆盖四块"两端真的不一样"的地方：

1. ``get_forward_msg`` 的参数键（SnowLuma 优先 res_id ``id``，NapCat 用 ``message_id``）；
2. 群/私聊文件下载（SnowLuma 的 ``get_file`` 取不到文件，必须走专有动作，私聊还要
   ``file_hash``）；
3. 登录态握手（``get_login_info`` = "QQ 已登录"，与"WS 连上"不同）与后端识别；
4. 令牌被拒（HTTP 401/403）要能与"服务没起"区分。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from atri_x.errors import AtriXError, ForbiddenError
from atri_x.platform.platform import MessageSegment, MessageSegmentType
from atri_x.platform.qq_onebot import transport as transport_module
from atri_x.platform.qq_onebot.adapter import new_adapter
from atri_x.platform.qq_onebot.attachment import resolve_chat_file, resolve_media
from atri_x.platform.qq_onebot.backend import backend_profile
from atri_x.platform.qq_onebot.config import Config
from atri_x.platform.qq_onebot.message import file_segment
from atri_x.platform.qq_onebot.preprocess import attach_media_scope
from atri_x.platform.qq_onebot.transport import (
    GetFileData,
    LoginInfo,
    Response,
    Transport,
    VersionInfo,
)
from atri_x.platform.qq_onebot.types import Event


def _ok(data: Any = None) -> Response:
    return Response(status="ok", retcode=0, data=data, echo="e")


class _RecorderLogger:
    """收集日志（适配器只在有 logger 时告警）。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append(("info", message, fields))

    def warn(self, message: str, **fields: Any) -> None:
        self.lines.append(("warn", message, fields))

    def debug(self, message: str, **fields: Any) -> None:
        self.lines.append(("debug", message, fields))

    def messages(self, level: str | None = None) -> list[str]:
        return [line[1] for line in self.lines if level is None or line[0] == level]


class ForwardIdParamCase(unittest.IsolatedAsyncioTestCase):
    async def _capture(self, transport: Transport, message_id: str = "res-1"):
        calls: list[tuple[str, dict]] = []

        async def fake_call(ctx, action, params, limit=0):
            calls.append((action, dict(params or {})))
            return _ok({"messages": []})

        transport.call_with_limit = fake_call  # type: ignore[method-assign]
        await transport.get_forward_msg(None, message_id)
        return calls

    async def test_snowluma_prefers_res_id(self) -> None:
        calls = await self._capture(Transport(url="ws://x/", backend="snowluma"))
        self.assertEqual(calls[0][1], {"id": "res-1"})

    async def test_napcat_uses_message_id(self) -> None:
        calls = await self._capture(Transport(url="ws://x/", backend="napcat"))
        self.assertEqual(calls[0][1], {"message_id": "res-1"})

    async def test_missing_key_falls_back_to_the_other(self) -> None:
        transport = Transport(url="ws://x/", backend="snowluma")
        calls: list[dict] = []

        async def fake_call(ctx, action, params, limit=0):
            calls.append(dict(params or {}))
            if len(calls) == 1:
                raise AtriXError("onebot action get_forward_msg failed: retcode=1400")
            return _ok({"messages": [{"data": {}}]})

        transport.call_with_limit = fake_call  # type: ignore[method-assign]
        data = await transport.get_forward_msg(None, "12345")
        self.assertEqual(calls, [{"id": "12345"}, {"message_id": "12345"}])
        self.assertEqual(data, {"messages": [{"data": {}}]})


class _FakeHandshake(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class AuthFailureCase(unittest.IsolatedAsyncioTestCase):
    async def test_401_is_reported_as_forbidden(self) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise _FakeHandshake("handshake rejected: HTTP 401 Unauthorized", 401)

        with mock.patch.object(transport_module, "ws_connect", boom):
            with self.assertRaises(ForbiddenError) as ctx:
                await Transport(url="ws://127.0.0.1:3001/").connect(None)
        message = str(ctx.exception)
        self.assertIn("401", message)
        self.assertIn("access_token", message)

    async def test_403_is_reported_as_forbidden(self) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise _FakeHandshake("handshake rejected: HTTP 403 Forbidden", 403)

        with mock.patch.object(transport_module, "ws_connect", boom):
            with self.assertRaises(ForbiddenError):
                await Transport(url="ws://127.0.0.1:3001/").connect(None)

    async def test_other_connect_errors_stay_generic(self) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise _FakeHandshake("connection refused", 0)

        with mock.patch.object(transport_module, "ws_connect", boom):
            with self.assertRaises(AtriXError) as ctx:
                await Transport(url="ws://127.0.0.1:3001/").connect(None)
        self.assertNotIsInstance(ctx.exception, ForbiddenError)
        self.assertIn("connect onebot websocket", str(ctx.exception))


class _HandshakeTransport:
    """只有握手动作的假传输。"""

    def __init__(self, login: LoginInfo, version: VersionInfo) -> None:
        self.login = login
        self.version = version

    async def get_login_info(self, ctx: Any = None) -> LoginInfo:
        return self.login

    async def get_version_info(self, ctx: Any = None) -> VersionInfo:
        return self.version


class HandshakeCase(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, backend: str = "napcat", logger: Any = None):
        return new_adapter(Config(enabled=True, protocol_backend=backend), None, None, logger)

    async def test_login_ok_marks_logged_in(self) -> None:
        adapter = self._adapter()
        adapter.transport = _HandshakeTransport(
            LoginInfo(user_id=10001, nickname="bot"), VersionInfo(app_name="NapCat")
        )
        await adapter.handshake(None)
        self.assertTrue(adapter.login_ok)
        self.assertEqual(adapter.login_info.user_id, 10001)
        self.assertEqual(adapter.detected_backend, "napcat")

    async def test_not_logged_in_is_not_an_error(self) -> None:
        logger = _RecorderLogger()
        adapter = self._adapter(logger=logger)
        adapter.transport = _HandshakeTransport(
            LoginInfo(user_id=0, nickname=""), VersionInfo(app_name="SnowLuma")
        )
        await adapter.handshake(None)
        self.assertFalse(adapter.login_ok)
        self.assertIn("onebot not logged in", logger.messages("warn"))

    async def test_backend_mismatch_is_warned(self) -> None:
        logger = _RecorderLogger()
        adapter = self._adapter(backend="napcat", logger=logger)
        adapter.transport = _HandshakeTransport(
            LoginInfo(user_id=1, nickname="b"), VersionInfo(app_name="SnowLuma v2.2.0")
        )
        await adapter.handshake(None)
        self.assertEqual(adapter.detected_backend, "snowluma")
        self.assertIn("onebot backend mismatch", logger.messages("warn"))

    async def test_matching_backend_does_not_warn(self) -> None:
        logger = _RecorderLogger()
        adapter = self._adapter(backend="snowluma", logger=logger)
        adapter.transport = _HandshakeTransport(
            LoginInfo(user_id=1, nickname="b"), VersionInfo(app_name="SnowLuma")
        )
        await adapter.handshake(None)
        self.assertNotIn("onebot backend mismatch", logger.messages())

    async def test_transport_without_handshake_actions_is_skipped(self) -> None:
        adapter = self._adapter(logger=_RecorderLogger())
        adapter.transport = type("_Empty", (), {})()
        await adapter.handshake(None)
        self.assertFalse(adapter.login_ok)
        self.assertEqual(adapter.detected_backend, "")


class _FileTransport:
    """假传输：记录专有动作调用；``get_file`` 按配置是否可用。"""

    def __init__(self, backend: str, *, url: str = "", get_file: GetFileData | None = None):
        self.profile = backend_profile(backend)
        self.url = url
        self.file_data = get_file
        self.calls: list[tuple[str, dict]] = []
        self.get_file_calls = 0

    def _record(self, action: str, **params: Any) -> str:
        self.calls.append((action, params))
        return self.url

    async def get_chat_file_url(
        self, ctx: Any, file_id: str, *, group_id: int = 0, user_id: int = 0, file_hash: str = ""
    ) -> str:
        self.calls.append(
            (
                "get_chat_file_url",
                {
                    "file_id": file_id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "file_hash": file_hash,
                },
            )
        )
        return self.url

    async def get_file(self, ctx: Any, file: str) -> GetFileData:
        self.get_file_calls += 1
        return self.file_data or GetFileData(file="", url="")


class _AdapterStub:
    def __init__(self, transport: Any) -> None:
        self.transport = transport


def _file_segment(meta: dict[str, Any] | None = None) -> MessageSegment:
    return MessageSegment(
        type=MessageSegmentType.FILE,
        platform_file_id="file-uuid",
        name="report.pdf",
        platform_meta=dict(meta or {}),
    )


class ChatFileDownloadCase(unittest.IsolatedAsyncioTestCase):
    async def test_snowluma_uses_group_file_url_with_hash(self) -> None:
        transport = _FileTransport("snowluma", url="https://cdn.example/report.pdf")
        segment = _file_segment({"group_id": "12345", "file_hash": "HASH"})
        path, url = await resolve_chat_file(_AdapterStub(transport), None, segment, "file-uuid", 1024)
        self.assertEqual(path, "")
        self.assertEqual(url, "https://cdn.example/report.pdf")
        self.assertEqual(transport.get_file_calls, 0)
        action, params = transport.calls[0]
        self.assertEqual(action, "get_chat_file_url")
        self.assertEqual(params["group_id"], 12345)
        self.assertEqual(params["file_hash"], "HASH")
        self.assertEqual(params["file_id"], "file-uuid")

    async def test_snowluma_private_file_uses_user_id(self) -> None:
        transport = _FileTransport("snowluma", url="https://cdn.example/private.pdf")
        segment = _file_segment({"user_id": "777", "file_hash": "H"})
        _, url = await resolve_chat_file(_AdapterStub(transport), None, segment, "fid", 1024)
        self.assertTrue(url.endswith("private.pdf"))
        _, params = transport.calls[0]
        self.assertEqual(params["user_id"], 777)
        self.assertEqual(params["file_hash"], "H")

    async def test_snowluma_never_falls_back_to_get_file(self) -> None:
        transport = _FileTransport("snowluma", url="")
        segment = _file_segment({"group_id": "1", "file_hash": "H"})
        with self.assertRaises(AtriXError) as ctx:
            await resolve_chat_file(_AdapterStub(transport), None, segment, "fid", 1024)
        self.assertIn("cannot download chat files with get_file", str(ctx.exception))
        self.assertEqual(transport.get_file_calls, 0)

    async def test_napcat_falls_back_to_get_file(self) -> None:
        transport = _FileTransport(
            "napcat", url="", get_file=GetFileData(file="C:/tmp/x", url="")
        )
        path, url = await resolve_chat_file(_AdapterStub(transport), None, _file_segment(), "fid", 1024)
        self.assertEqual(path, "C:/tmp/x")
        self.assertEqual(url, "")
        self.assertEqual(transport.get_file_calls, 1)

    async def test_get_file_size_limit_still_applies(self) -> None:
        transport = _FileTransport(
            "napcat", url="", get_file=GetFileData(file="C:/tmp/big", file_size="2048")
        )
        with self.assertRaises(Exception) as ctx:
            await resolve_chat_file(_AdapterStub(transport), None, _file_segment(), "fid", 1024)
        self.assertIn("import limit", str(ctx.exception))

    async def test_resolve_media_passes_through_direct_url(self) -> None:
        transport = _FileTransport("snowluma", url="")
        segment = _file_segment({"group_id": "1"})
        segment.url = "https://cdn.example/direct.pdf"
        source = await resolve_media(_AdapterStub(transport), None, segment, 1024)
        self.assertEqual(source.url, "https://cdn.example/direct.pdf")
        self.assertEqual(transport.calls, [])


class PlatformMetaCase(unittest.TestCase):
    def test_file_segment_keeps_file_hash(self) -> None:
        segment = file_segment(
            "文件",
            {
                "file_id": "uuid",
                "file": "uuid",
                "name": "报告.pdf",
                "file_size": 128,
                "file_hash": "ABC",
            },
        )
        self.assertEqual(segment.platform_file_id, "uuid")
        self.assertEqual(segment.platform_meta.get("file_hash"), "ABC")

    def test_file_segment_without_hash_has_empty_meta(self) -> None:
        segment = file_segment("文件", {"file_id": "uuid"})
        self.assertEqual(segment.platform_meta, {})

    def test_segment_json_round_trip_keeps_platform_meta(self) -> None:
        segment = _file_segment({"file_hash": "ABC", "group_id": "9"})
        restored = MessageSegment.from_json(segment.to_json())
        self.assertEqual(restored.platform_meta, {"file_hash": "ABC", "group_id": "9"})
        self.assertEqual(restored.to_json(), segment.to_json())

    def test_empty_platform_meta_is_not_serialized(self) -> None:
        payload = MessageSegment(type=MessageSegmentType.TEXT, text="hi").to_json()
        self.assertNotIn("platform_meta", payload)


class AttachMediaScopeCase(unittest.TestCase):
    def _event(self, **overrides: Any) -> Event:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 1,
            "user_id": 42,
            "group_id": 12345,
            "self_id": 100,
            "message": [],
        }
        raw.update(overrides)
        return Event.from_json(raw)

    def test_group_id_is_added_to_file_segments_only(self) -> None:
        file = _file_segment({"file_hash": "H"})
        text = MessageSegment(type=MessageSegmentType.TEXT, text="hi")
        attach_media_scope([file, text], self._event())
        self.assertEqual(file.platform_meta.get("group_id"), "12345")
        self.assertEqual(file.platform_meta.get("user_id"), "42")
        self.assertEqual(file.platform_meta.get("file_hash"), "H")
        self.assertEqual(text.platform_meta, {})

    def test_private_message_has_no_group_id(self) -> None:
        file = _file_segment()
        attach_media_scope([file], self._event(message_type="private", group_id=0))
        self.assertNotIn("group_id", file.platform_meta)
        self.assertEqual(file.platform_meta.get("user_id"), "42")


class ActionWrapperCase(unittest.IsolatedAsyncioTestCase):
    """动作薄封装的参数口径（SnowLuma / NapCat 都认的那一套）。"""

    async def _capture(self, transport: Transport) -> list[tuple[str, dict]]:
        calls: list[tuple[str, dict]] = []

        async def fake_call(ctx, action, params):
            calls.append((action, dict(params or {})))
            return _ok({"file_id": "fid-1", "url": "https://cdn.example/x"})

        transport.call = fake_call  # type: ignore[method-assign]
        return calls

    async def test_upload_group_file_returns_file_id(self) -> None:
        transport = Transport(url="ws://x/", backend="snowluma")
        calls = await self._capture(transport)
        file_id = await transport.upload_group_file(None, 123, "file:///tmp/a.pdf", "a.pdf")
        self.assertEqual(file_id, "fid-1")
        self.assertEqual(calls[0][0], "upload_group_file")
        self.assertEqual(
            calls[0][1], {"group_id": 123, "file": "file:///tmp/a.pdf", "name": "a.pdf"}
        )

    async def test_upload_private_file_returns_file_id(self) -> None:
        transport = Transport(url="ws://x/")
        calls = await self._capture(transport)
        file_id = await transport.upload_private_file(None, 777, "file:///tmp/a.pdf", "a.pdf")
        self.assertEqual(file_id, "fid-1")
        self.assertEqual(calls[0][0], "upload_private_file")
        self.assertEqual(calls[0][1]["user_id"], 777)

    async def test_group_file_url_includes_hash_when_present(self) -> None:
        transport = Transport(url="ws://x/")
        calls = await self._capture(transport)
        url = await transport.get_group_file_url(None, 42, "fid", "HASH")
        self.assertEqual(url, "https://cdn.example/x")
        self.assertEqual(
            calls[0][1], {"group_id": 42, "file_id": "fid", "file_hash": "HASH"}
        )

    async def test_private_file_url_omits_empty_user_and_hash(self) -> None:
        transport = Transport(url="ws://x/")
        calls = await self._capture(transport)
        await transport.get_private_file_url(None, 0, "fid")
        self.assertEqual(calls[0][1], {"file_id": "fid"})

    async def test_get_chat_file_url_dispatches_by_scope(self) -> None:
        transport = Transport(url="ws://x/", backend="snowluma")
        calls = await self._capture(transport)
        await transport.get_chat_file_url(None, "fid", group_id=9, file_hash="H")
        await transport.get_chat_file_url(None, "fid", user_id=7)
        self.assertEqual(
            [action for action, _params in calls],
            ["get_group_file_url", "get_private_file_url"],
        )
        self.assertEqual(await transport.get_chat_file_url(None, "fid"), "")

    async def test_transport_profile_follows_backend(self) -> None:
        self.assertEqual(
            Transport(url="ws://x/", backend="snowluma").profile.forward_id_param, "id"
        )
        self.assertEqual(
            Transport(url="ws://x/", backend="napcat").profile.forward_id_param,
            "message_id",
        )
        self.assertEqual(Transport(url="ws://x/").backend, "napcat")

    async def test_login_and_version_wrappers_parse_payload(self) -> None:
        transport = Transport(url="ws://x/")
        replies = {
            "get_login_info": {"user_id": 10001, "nickname": "bot"},
            "get_version_info": {
                "app_name": "SnowLuma",
                "app_version": "2.2.0",
                "protocol_version": "v11",
            },
            "can_send_image": {"yes": True},
            "can_send_record": {"yes": False},
        }

        async def fake_call(ctx, action, params):
            return _ok(replies[action])

        transport.call = fake_call  # type: ignore[method-assign]
        login = await transport.get_login_info()
        self.assertEqual(login.user_id, 10001)
        self.assertTrue(login.logged_in())
        version = await transport.get_version_info()
        self.assertEqual(version.app_name, "SnowLuma")
        self.assertTrue(await transport.can_send_image())
        self.assertFalse(await transport.can_send_record())


if __name__ == "__main__":
    unittest.main()
