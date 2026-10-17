"""``atri_x.platform.qq_onebot`` 的单元测试。

对应 Go：``internal/platform/qq-onebot/{config,event,message,message_forward,output,
inbound,inbound_media,transport}_test.go``。

测试**不连真实 QQ 服务**：WebSocket 用假连接对象，OneBot 动作用假 Transport
（记录调用并返回构造好的 ``Response``）。目录型临时文件统一用 ``os.makedirs``
建在仓库根下并在 ``tearDown`` 里 ``shutil.rmtree``（PORTING §7.2）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import unittest

from atri_x.delivery.delivery import (
    DeliveryError,
    Notice,
    Output,
    Receipt,
    Source,
    Target as DeliveryTarget,
)
from atri_x.errors import AtriXError, InvalidError, MediaTooLargeError
from atri_x.platform.platform import (
    MessageSegment,
    MessageSegmentType,
    PlatformEvent,
    ReplyContext,
    SegmentAt,
    SegmentFile,
    SegmentImage,
    SegmentText,
)
from atri_x.security import GroupRoleMember
from atri_x.platform.qq_onebot.adapter import Adapter, new_adapter
from atri_x.platform.qq_onebot.attachment import resolve_media
from atri_x.platform.qq_onebot.capabilities import (
    fetch_group_history,
    get_group_info,
    get_group_member_list,
    get_user_avatar_url,
    raw_id_string,
)
from atri_x.platform.qq_onebot.config import (
    Config,
    apply_defaults,
    new_from_platform_config,
    validate_send_file_mode,
)
from atri_x.platform.qq_onebot.forward import (
    FORWARD_TRUST_MARKER,
    new_forward_parse_state,
    render_forward_data,
)
from atri_x.platform.qq_onebot.inbound import (
    INBOUND_DEDUP_STATE_COMPLETED,
    INBOUND_DEDUP_STATE_FAILED,
    InboundDeduper,
    OutboundEventJob,
    inbound_key,
    new_inbound_runtime,
    raise_context_error,
    sleep_context,
)
from atri_x.platform.qq_onebot.message import (
    normalize_message,
    normalize_message_with_limits,
    normalize_plain_text,
    normalize_segments,
)
from atri_x.platform.qq_onebot.output import (
    QQ_TEXT_PAGE_RUNES,
    local_path_file_uri,
    one_bot_media_receipt,
    one_bot_source_file,
    output_segments,
    qq_forward_nodes,
    qq_text_pages,
    text_outputs,
)
from atri_x.platform.qq_onebot.preprocess import is_configured_superadmin
from atri_x.platform.qq_onebot.target import (
    Target,
    event_scope_id,
    scope_id,
    target_from_context,
    target_to_qq,
    with_target,
)
from atri_x.platform.qq_onebot.transport import Transport, write_timeout_for
from atri_x.platform.qq_onebot.types import (
    Event,
    ForwardLimits,
    NormalizedMessage,
    Segment,
    parse_int64,
)
from atri_x.storage.times import now as storage_now

TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_qq_onebot_test")


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


class _FakeConn:
    """假 WebSocket 连接：记录发出的帧，按脚本回放收到的帧。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.max_size = 8 * 1024 * 1024
        self._decoder = type("_D", (), {"max_frame_size": 8 * 1024 * 1024})()
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self, timeout=None):
        return await self.incoming.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _FakeTransport:
    """假 Transport：记录调用并返回脚本化的响应。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}
        self.fail: set[str] = set()
        self.fail_users: set[int] = set()
        self.forward: dict[str, object] = {}

    async def call(self, ctx, action, params):
        self.calls.append((action, dict(params or {})))
        if action in self.fail:
            raise AtriXError(f"onebot action {action} failed: status=failed retcode=1")
        payload = self.responses.get(action, {})
        return _response(payload)

    async def get_message(self, ctx, message_id):
        from atri_x.platform.qq_onebot.transport import GetMessageData
        from atri_x.platform.qq_onebot.types import Sender

        if message_id in self.fail:
            raise AtriXError("get_msg failed")
        data = self.responses.get("get_msg")
        if data is None:
            raise AtriXError("get_msg failed")
        payload = dict(data)
        payload["sender"] = Sender.from_json(payload.get("sender"))
        return GetMessageData(**payload)

    async def get_forward_msg(self, ctx, message_id):
        if message_id in self.fail:
            raise AtriXError("get_forward_msg failed")
        if message_id not in self.forward:
            raise AtriXError("get_forward_msg failed")
        return self.forward.get(message_id)

    async def get_image(self, ctx, file):
        from atri_x.platform.qq_onebot.transport import GetImageData

        return GetImageData(**self.responses.get("get_image", {}))

    async def get_file(self, ctx, file):
        from atri_x.platform.qq_onebot.transport import GetFileData

        return GetFileData(**self.responses.get("get_file", {}))

    async def get_group_member_info(self, ctx, group_id, user_id):
        from atri_x.platform.qq_onebot.types import Sender

        if str(user_id) in self.fail:
            raise AtriXError("get_group_member_info failed")
        data = self.responses.get("get_group_member_info", {})
        return Sender(**data)

    async def send_private_message(self, ctx, user_id, text):
        self.calls.append(("send_private_msg", {"user_id": user_id, "message": text}))
        if user_id in self.fail_users:
            raise AtriXError("send_private_msg failed")
        return str(self.responses.get("send_private_msg", "1"))

    async def send_group_message(self, ctx, group_id, text):
        self.calls.append(("send_group_msg", {"group_id": group_id, "message": text}))
        return str(self.responses.get("send_group_msg", "2"))

    async def send_private_segments(self, ctx, user_id, segments):
        self.calls.append(("send_private_segments", {"user_id": user_id, "segments": segments}))
        if user_id in self.fail_users:
            raise AtriXError("send_private_segments failed")
        return str(self.responses.get("send_private_msg", "1"))

    async def send_group_segments(self, ctx, group_id, segments):
        self.calls.append(("send_group_segments", {"group_id": group_id, "segments": segments}))
        if "send_group_segments" in self.fail:
            raise AtriXError("send_group_segments failed")
        return str(self.responses.get("send_group_msg", "4"))

    async def send_private_forward_message(self, ctx, user_id, nodes):
        self.calls.append(("send_private_forward_msg", {"user_id": user_id, "nodes": nodes}))
        return "fwd-1"

    async def send_group_forward_message(self, ctx, group_id, nodes):
        self.calls.append(("send_group_forward_msg", {"group_id": group_id, "nodes": nodes}))
        return "fwd-2"


def _response(data):
    from atri_x.platform.qq_onebot.transport import Response

    return Response(status="ok", retcode=0, data=data, echo="e")


def _adapter(**cfg_kwargs) -> Adapter:
    adapter = new_adapter(Config(**cfg_kwargs), None, None, None)
    adapter.transport = _FakeTransport()
    return adapter


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_apply_defaults_matches_go(self) -> None:
        cfg = Config()
        apply_defaults(cfg)
        self.assertEqual(cfg.url, "ws://127.0.0.1:6700/")
        self.assertEqual(cfg.reconnect_interval_seconds, 3)
        self.assertEqual(cfg.api_timeout_seconds, 15)
        self.assertEqual(cfg.send_file_mode, "base64")
        self.assertEqual(cfg.command_prefixes, ["/"])
        self.assertEqual(cfg.max_receive_file_bytes, 100 * 1024 * 1024)
        self.assertEqual(cfg.download_timeout_secs, 60)
        self.assertEqual(cfg.forward_max_nodes, 50)
        self.assertEqual(cfg.forward_max_runes, 20000)
        self.assertEqual(cfg.forward_fetch_timeout_seconds, 5)
        self.assertTrue(cfg.inbound_dedup_enabled)
        self.assertEqual(cfg.inbound_dedup_ttl_seconds, 1800)
        self.assertEqual(cfg.preprocess_workers, 4)
        self.assertEqual(cfg.high_priority_workers, 1)

    def test_validate_send_file_mode(self) -> None:
        validate_send_file_mode("base64")
        validate_send_file_mode("file_uri")
        with self.assertRaises(InvalidError):
            validate_send_file_mode("http")

    def test_ws_url_alias_decodes(self) -> None:
        adapter = new_from_platform_config(
            {"enabled": True, "ws_url": "ws://example.invalid:6700/"}, None, None, None
        )
        self.assertEqual(adapter.cfg.url, "ws://example.invalid:6700/")
        self.assertTrue(adapter.enabled())
        self.assertEqual(adapter.name(), "qqonebot")

    def test_send_file_mode_validation_at_construction(self) -> None:
        with self.assertRaises(InvalidError):
            new_from_platform_config({"enabled": True, "send_file_mode": "nope"}, None, None, None)

    def test_inbound_defaults_and_disable(self) -> None:
        enabled = new_from_platform_config(
            {"enabled": True, "inbound_dedup_ttl_seconds": 120}, None, None, None
        )
        self.assertIsNotNone(enabled.inbound.dedup)
        self.assertEqual(enabled.inbound.dedup.ttl, 120.0)
        disabled = new_from_platform_config(
            {"enabled": True, "inbound_dedup_enabled": False}, None, None, None
        )
        self.assertIsNone(disabled.inbound.dedup)
        duplicate, _state = disabled.begin_inbound_job("k")
        self.assertFalse(duplicate)

    def test_access_token_from_dotenv(self) -> None:
        os.makedirs(TMP_DIR, exist_ok=True)
        env_path = os.path.join(TMP_DIR, ".env")
        with open(env_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("QQONEBOT_TOKEN=from-dotenv\n")
        previous = os.environ.pop("QQONEBOT_TOKEN", None)
        try:
            adapter = new_from_platform_config(
                {"enabled": True, "access_token_env": "QQONEBOT_TOKEN"},
                None,
                None,
                None,
                config_env_dir=TMP_DIR,
            )
            self.assertEqual(adapter.cfg.access_token, "from-dotenv")
        finally:
            if previous is not None:
                os.environ["QQONEBOT_TOKEN"] = previous
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_explicit_access_token_wins(self) -> None:
        adapter = new_from_platform_config(
            {"enabled": True, "access_token": "legacy", "access_token_env": "QQONEBOT_TOKEN"},
            None,
            None,
            None,
            config_env_dir=TMP_DIR,
        )
        self.assertEqual(adapter.cfg.access_token, "legacy")

    def test_enabled_flag(self) -> None:
        self.assertFalse(_adapter().enabled())
        self.assertTrue(_adapter(enabled=True).enabled())


# ---------------------------------------------------------------------------
# 事件解析
# ---------------------------------------------------------------------------

class TestEvent(unittest.TestCase):
    def test_from_json_and_back(self) -> None:
        raw = {
            "time": 1700000000,
            "self_id": 10000,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 42,
            "user_id": 7,
            "group_id": 9,
            "raw_message": "hi",
            "message": [{"type": "text", "data": {"text": "hi"}}],
            "sender": {"user_id": 7, "nickname": "昵称", "card": "名片", "role": "member"},
        }
        event = Event.from_json(raw)
        self.assertEqual(event.message_type, "group")
        self.assertEqual(event.sender.card, "名片")
        payload = event.to_json()
        self.assertEqual(
            sorted(payload),
            sorted(
                [
                    "time", "self_id", "post_type", "message_type", "sub_type", "message_id",
                    "user_id", "group_id", "notice_type", "request_type", "operator_id",
                    "target_id", "message", "raw_message", "sender",
                ]
            ),
        )
        self.assertEqual(payload["sender"]["card"], "名片")

    def test_string_into_int_field_is_rejected(self) -> None:
        with self.assertRaises(InvalidError):
            Event.from_json({"post_type": "message", "message_id": "42"})

    def test_bool_into_int_field_is_rejected(self) -> None:
        with self.assertRaises(InvalidError):
            Event.from_json({"post_type": "message", "time": True})

    def test_scope_helpers(self) -> None:
        group = Event.from_json({"post_type": "message", "message_type": "group", "group_id": 9})
        private = Event.from_json({"post_type": "message", "message_type": "private", "user_id": 5})
        self.assertEqual(scope_id(group), "group:9")
        self.assertEqual(scope_id(private), "private:5")
        self.assertEqual(event_scope_id(group), "group:9")
        self.assertEqual(event_scope_id(private), "private:5")
        self.assertEqual(event_scope_id(Event()), "")

    def test_parse_int64_rejects_python_only_syntax(self) -> None:
        self.assertEqual(parse_int64(" 12 "), 12)
        self.assertEqual(parse_int64("+12"), 12)
        self.assertIsNone(parse_int64("1_2"))
        self.assertIsNone(parse_int64("１２"))
        self.assertIsNone(parse_int64(str(2**64)))
        self.assertIsNone(parse_int64("abc"))


# ---------------------------------------------------------------------------
# 消息归一化
# ---------------------------------------------------------------------------

class TestNormalize(unittest.TestCase):
    def test_text_and_mention_view(self) -> None:
        raw = [
            {"type": "text", "data": {"text": "你好"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "text", "data": {"text": " 世界"}},
        ]
        normalized = normalize_message(raw, "", 10000)
        self.assertEqual(normalized.text, "你好[at qq:123] 世界")
        self.assertEqual(normalized.match_text, normalized.text)
        self.assertEqual([mention.user_id for mention in normalized.mentions], ["123"])
        self.assertEqual(
            [segment.type for segment in normalized.segments],
            [SegmentText, SegmentAt, SegmentText],
        )

    def test_self_mention_is_not_rendered(self) -> None:
        normalized = normalize_message(
            [{"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": "x"}}],
            "",
            10000,
        )
        self.assertEqual(normalized.text, "x")
        self.assertEqual([segment.type for segment in normalized.segments], [SegmentText])

    def test_reply_face_and_media_segments(self) -> None:
        raw = [
            {"type": "reply", "data": {"id": "88"}},
            {"type": "face", "data": {"id": "1"}},
            {"type": "image", "data": {"file": "a.jpg", "url": "https://x/a.jpg", "file_size": 5}},
            {"type": "record", "data": {"file": "r.silk"}},
            {"type": "video", "data": {"file": "v.mp4"}},
            {"type": "file", "data": {"file_id": "fid", "name": "doc.pdf"}},
            {"type": "unknown_kind", "data": {}},
        ]
        normalized = normalize_message(raw, "", 1)
        self.assertEqual(normalized.reply_id, "88")
        self.assertEqual(normalized.text, "[表情][语音][视频][文件][unknown_kind]")
        kinds = [segment.type for segment in normalized.segments]
        self.assertEqual(
            kinds, [SegmentImage, SegmentFile, SegmentFile, SegmentFile]
        )
        image = normalized.segments[0]
        self.assertEqual(image.url, "https://x/a.jpg")
        self.assertEqual(image.platform_file_id, "a.jpg")
        # Go 的段数据是 map[string]any：JSON 数字变 float64，再经 Sprintf("%f")
        # 变形后 ParseInt 失败 → file_size 归 0（上游既有怪癖，逐字保留）。
        self.assertEqual(image.size, 0)
        self.assertEqual(
            normalize_message(
                [{"type": "image", "data": {"file": "a.jpg", "file_size": "5"}}], "", 1
            ).segments[0].size,
            5,
        )
        record = normalized.segments[1]
        self.assertEqual((record.text, record.name), ("语音", "r.silk"))
        file_segment_value = normalized.segments[3]
        self.assertEqual(file_segment_value.platform_file_id, "fid")
        self.assertEqual(file_segment_value.name, "doc.pdf")

    def test_image_direct_url_fallback(self) -> None:
        normalized = normalize_message(
            [{"type": "image", "data": {"file": "https://x/b.png"}}], "", 1
        )
        self.assertEqual(normalized.segments[0].url, "https://x/b.png")

    def test_json_string_message_is_parsed(self) -> None:
        text = json.dumps([{"type": "text", "data": {"text": "hi"}}])
        normalized = normalize_message(text, "", 1)
        self.assertEqual(normalized.text, "hi")

    def test_plain_text_fallback_keeps_newlines(self) -> None:
        normalized = normalize_message("a\r\nb\r\n", "", 1)
        self.assertEqual(normalized.text, "a\nb")
        self.assertEqual(normalize_plain_text("").segments, [])

    def test_segment_data_string_replicates_go_float_quirk(self) -> None:
        from atri_x.platform.qq_onebot.forward import segment_data_int64, segment_data_string

        # Go 走 Sprintf("%f") 后各去掉一次后缀：整数会留下 123.00000。
        self.assertEqual(segment_data_string({"file_size": 123}, "file_size"), "123.00000")
        self.assertEqual(segment_data_int64({"file_size": 123}, "file_size"), 0)
        self.assertEqual(segment_data_int64({"file_size": "123"}, "file_size"), 123)

    def test_forward_segment_without_body_yields_id(self) -> None:
        normalized = normalize_message(
            [{"type": "forward", "data": {"id": "abc"}}], "", 1
        )
        self.assertEqual(normalized.forward_ids, ["abc"])
        self.assertIn("abc", normalized.text)
        # 转发内容不进匹配视图。
        self.assertEqual(normalized.match_text, "")

    def test_normalize_segments_respects_limits(self) -> None:
        segment = Segment(type="text", data={"text": "x" * 10})
        normalized = normalize_segments([segment], 1)
        self.assertEqual(normalized.text, "x" * 10)
        self.assertEqual(normalize_message_with_limits([], "", 1, ForwardLimits()).text, "")


# ---------------------------------------------------------------------------
# 转发展开
# ---------------------------------------------------------------------------

class TestForwardRendering(unittest.TestCase):
    def test_render_node_with_content(self) -> None:
        state = new_forward_parse_state()
        raw = {
            "type": "node",
            "data": {
                "user_id": 7,
                "nickname": "小明",
                "message_id": "m1",
                "content": [{"type": "text", "data": {"text": "  转发正文"}}],
            },
        }
        text, forward_id, ok = render_forward_data(raw, state)
        self.assertTrue(ok)
        self.assertEqual(forward_id, "")
        self.assertIn("[转发节点] 发送者：小明（7）", text)
        self.assertIn("消息ID：m1", text)
        self.assertIn("  转发正文", text)

    def test_cycle_detection(self) -> None:
        state = new_forward_parse_state()
        node = {"message_id": "loop", "content": [{"type": "text", "data": {"text": "a"}}]}
        node["content"].append({"type": "node", "data": node})
        text, _id, _ok = render_forward_data(node, state)
        self.assertIn("[检测到循环引用，已省略]", text)

    def test_depth_limit_truncates(self) -> None:
        state = new_forward_parse_state(ForwardLimits(max_depth=1))
        inner = {"content": [{"type": "text", "data": {"text": "deep"}}]}
        outer = {"message_id": "o", "content": [inner]}
        text, _id, _ok = render_forward_data(outer, state)
        self.assertIn("转发层级超过上限", text)

    def test_node_budget_truncates(self) -> None:
        state = new_forward_parse_state(ForwardLimits(max_nodes=1))
        first = {"message_id": "1", "content": [{"type": "text", "data": {"text": "a"}}]}
        render_forward_data(first, state)
        second = {"message_id": "2", "content": [{"type": "text", "data": {"text": "b"}}]}
        text, _id, _ok = render_forward_data(second, state)
        self.assertEqual(text, "")
        self.assertTrue(state.truncated)

    def test_non_text_budget(self) -> None:
        state = new_forward_parse_state(ForwardLimits(max_non_text=1))
        node = {
            "message_id": "n",
            "content": [
                {"type": "image", "data": {}},
                {"type": "image", "data": {}},
            ],
        }
        text, _id, _ok = render_forward_data(node, state)
        self.assertIn("[图片]", text)
        self.assertIn("[更多非文本节点已省略]", text)

    def test_trust_marker_in_segment_normalization(self) -> None:
        normalized = normalize_message(
            [
                {
                    "type": "forward",
                    "data": {"content": [{"type": "text", "data": {"text": "转发的字"}}]},
                }
            ],
            "",
            1,
        )
        self.assertTrue(normalized.text.startswith(FORWARD_TRUST_MARKER))
        self.assertIn("转发的字", normalized.text)
        self.assertEqual(normalized.match_text, "")

    def test_raw_id_only(self) -> None:
        state = new_forward_parse_state()
        text, forward_id, ok = render_forward_data({"id": "xyz"}, state)
        self.assertEqual((text, forward_id, ok), ("", "xyz", True))


# ---------------------------------------------------------------------------
# 输出 → 段
# ---------------------------------------------------------------------------

class TestOutput(unittest.TestCase):
    def test_text_outputs(self) -> None:
        text, ok = text_outputs([Output(kind="text", text="a"), Output(kind="text", text="b")])
        self.assertEqual((text, ok), ("ab", True))
        self.assertEqual(text_outputs([Output(kind="image")]), ("", False))

    def test_output_segments_every_kind(self) -> None:
        segments = output_segments(
            "base64",
            [
                Output(kind="text", text="hi"),
                Output(kind="reply", text="q", reply_to_platform_message_id="9"),
                Output(kind="emoticon", emoticon_id="3"),
                Output(kind="image", source=Source(data=b"img")),
                Output(kind="file", name="doc.pdf", source=Source(url="https://x/f")),
                Output(kind="record", source=Source(data=b"rec")),
                Output(kind="at", name="123"),
            ],
        )
        payload = [segment.to_json() for segment in segments]
        self.assertEqual(payload[0], {"type": "text", "data": {"text": "hi"}})
        self.assertEqual(payload[1], {"type": "reply", "data": {"id": "9"}})
        self.assertEqual(payload[3], {"type": "face", "data": {"id": "3"}})
        self.assertEqual(
            payload[4]["data"]["file"], "base64://" + base64.b64encode(b"img").decode()
        )
        self.assertEqual(payload[5]["data"], {"file": "https://x/f", "name": "doc.pdf"})
        self.assertEqual(payload[7], {"type": "at", "data": {"qq": "123"}})

    def test_output_segments_errors(self) -> None:
        with self.assertRaises(InvalidError):
            output_segments("base64", [Output(kind="reply", reply_to_platform_message_id="")])
        with self.assertRaises(InvalidError):
            output_segments("base64", [Output(kind="emoticon")])
        with self.assertRaises(InvalidError):
            output_segments("base64", [Output(kind="at")])
        with self.assertRaises(InvalidError):
            output_segments("base64", [Output(kind="weird")])
        with self.assertRaises(InvalidError):
            output_segments("base64", [Output(kind="image")])

    def test_at_falls_back_to_text(self) -> None:
        segments = output_segments("base64", [Output(kind="at", text=" 77 ")])
        self.assertEqual(segments[0].data, {"qq": "77"})

    def test_text_pages_and_forward_nodes(self) -> None:
        pages = qq_text_pages("界" * (QQ_TEXT_PAGE_RUNES + 1))
        self.assertEqual(len(pages), 2)
        self.assertTrue(all(len(page) <= QQ_TEXT_PAGE_RUNES for page in pages))
        self.assertNotIn("（1/2）", pages[0])
        nodes = qq_forward_nodes(pages)
        self.assertEqual(nodes[0].type, "node")
        self.assertEqual(nodes[0].data["content"][0]["data"]["text"], pages[0])

    def test_media_receipt(self) -> None:
        target = Target(message_type="group", group_id=9)
        receipt = one_bot_media_receipt(
            Receipt(platform_message_ids=["77"]),
            target,
            [Output(kind="text"), Output(kind="image")],
        )
        self.assertEqual(len(receipt.sent_messages), 1)
        self.assertEqual(receipt.sent_messages[0].scope_id, "group:9")
        self.assertEqual(receipt.sent_messages[0].output_indexes, [1])
        self.assertEqual(
            one_bot_media_receipt(Receipt(), target, [Output(kind="image")]).sent_messages, []
        )

    def test_source_file_modes(self) -> None:
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, "a.bin")
        with open(path, "wb") as handle:
            handle.write(b"data")
        try:
            self.assertEqual(
                one_bot_source_file(Source(path=path), "file", "base64"),
                "base64://" + base64.b64encode(b"data").decode(),
            )
            uri = one_bot_source_file(Source(path=path), "file", "file_uri")
            self.assertTrue(uri.startswith("file:///"))
            self.assertTrue(uri.endswith("a.bin"))
            with self.assertRaises(InvalidError):
                one_bot_source_file(Source(path=path), "file", "nope")
            with self.assertRaises(InvalidError):
                one_bot_source_file(Source(), "file", "base64")
        finally:
            shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_local_path_file_uri_unc(self) -> None:
        uri = local_path_file_uri("//server/share/x.bin", "file")
        self.assertEqual(uri, "file://server/share/x.bin")


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------

class TestSend(unittest.IsolatedAsyncioTestCase):
    async def test_send_chat_text_uses_ctx_target(self) -> None:
        adapter = _adapter()
        ctx = with_target(None, Target(message_type="group", group_id=9))
        receipt = await adapter.send_chat(ctx, [Output(kind="text", text="hello")])
        self.assertEqual(receipt.platform_message_ids, ["2"])
        self.assertEqual(adapter.transport.calls[-1][0], "send_group_msg")
        self.assertEqual(adapter.transport.calls[-1][1]["group_id"], 9)

    async def test_send_chat_long_text_becomes_forward(self) -> None:
        adapter = _adapter()
        ctx = with_target(None, Target(message_type="private", user_id=5))
        receipt = await adapter.send_chat(ctx, [Output(kind="text", text="界" * 3001)])
        self.assertEqual(receipt.platform_message_ids, ["fwd-1"])
        self.assertEqual(adapter.transport.calls[-1][0], "send_private_forward_msg")
        self.assertEqual(len(adapter.transport.calls[-1][1]["nodes"]), 2)

    async def test_send_chat_missing_target_raises(self) -> None:
        adapter = _adapter()
        with self.assertRaises(InvalidError):
            await adapter.send_chat(None, [Output(kind="text", text="x")])
        with self.assertRaises(InvalidError):
            await adapter.send_chat(None, [Output(kind="image", source=Source(data=b"1"))])

    async def test_send_chat_empty_text_is_noop(self) -> None:
        adapter = _adapter()
        ctx = with_target(None, Target(message_type="group", group_id=1))
        receipt = await adapter.send_chat(ctx, [Output(kind="text", text="   ")])
        self.assertEqual(receipt.platform_message_ids, [])
        self.assertEqual(adapter.transport.calls, [])

    async def test_send_notice_empty_target_reuses_chat(self) -> None:
        adapter = _adapter()
        ctx = with_target(None, Target(message_type="group", group_id=3))
        receipt = await adapter.send_notice(ctx, Notice(outputs=[Output(kind="text", text="n")]))
        self.assertEqual(receipt.platform_message_ids, ["2"])

    async def test_send_notice_superadmins(self) -> None:
        # Go 的超管分支不剥 ``qqonebot:`` 前缀（只有 isConfiguredSuperadmin 剥），
        # 所以名单必须是裸数字 ID。
        adapter = _adapter(enabled=True)
        adapter.cfg.superadmins = ["11", "22", ""]
        receipt = await adapter.send_notice(
            None,
            Notice(
                target=DeliveryTarget(superadmins=True),
                outputs=[Output(kind="text", text="alert")],
            ),
        )
        self.assertEqual(len(receipt.platform_message_ids), 2)
        sent = [call for call in adapter.transport.calls if call[0] == "send_private_segments"]
        self.assertEqual([call[1]["user_id"] for call in sent], [11, 22])

    async def test_send_notice_superadmins_with_prefix_fails_like_go(self) -> None:
        adapter = _adapter()
        adapter.cfg.superadmins = ["qqonebot:22"]
        with self.assertRaises(DeliveryError):
            await adapter.send_notice(
                None,
                Notice(
                    target=DeliveryTarget(superadmins=True),
                    outputs=[Output(kind="text", text="alert")],
                ),
            )

    async def test_send_notice_superadmins_unconfigured(self) -> None:
        with self.assertRaises(AtriXError):
            await _adapter().send_notice(
                None,
                Notice(
                    target=DeliveryTarget(superadmins=True),
                    outputs=[Output(kind="text", text="x")],
                ),
            )

    async def test_group_tool_preview_is_dropped(self) -> None:
        adapter = _adapter()
        ctx = with_target(None, Target(message_type="group", group_id=3))
        receipt = await adapter.send_notice(
            ctx, Notice(outputs=[Output(kind="text", text="[tool] 调用中")])
        )
        self.assertEqual(receipt.platform_message_ids, [])
        self.assertEqual(adapter.transport.calls, [])

    async def test_private_tool_preview_is_sent(self) -> None:
        adapter = _adapter()
        ctx = with_target(None, Target(message_type="private", user_id=3))
        receipt = await adapter.send_notice(
            ctx, Notice(outputs=[Output(kind="text", text="[tool] 调用中")])
        )
        self.assertEqual(receipt.platform_message_ids, ["1"])

    async def test_send_notice_target_by_scope(self) -> None:
        adapter = _adapter()
        receipt = await adapter.send_notice(
            None,
            Notice(
                target=DeliveryTarget(scope_id="group:42"),
                outputs=[Output(kind="text", text="hi")],
            ),
        )
        self.assertEqual(receipt.platform_message_ids, ["4"])
        self.assertEqual(adapter.transport.calls[-1][1]["group_id"], 42)

    async def test_target_conversion_errors(self) -> None:
        with self.assertRaises(InvalidError):
            target_to_qq(DeliveryTarget())
        with self.assertRaises(InvalidError):
            target_to_qq(DeliveryTarget(private_user_id="abc"))
        self.assertEqual(
            target_to_qq(DeliveryTarget(scope_id="private:5")),
            Target(message_type="private", user_id=5),
        )

    async def test_call_platform_api_returns_json_text(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_version_info"] = {"app_name": "x"}
        raw = await adapter.call_platform_api(None, " get_version_info ", None)
        self.assertEqual(json.loads(raw), {"app_name": "x"})

    async def test_send_temporary_notice_uses_fresh_transport(self) -> None:
        from atri_x.delivery.delivery import with_temporary_connection

        adapter = _adapter()
        created: list[Transport] = []

        class _TempTransport(_FakeTransport):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__()
                created.append(self)

            async def connect(self, ctx):
                return None

            async def close(self, status=1000, reason=""):
                self.closed = True

            async def read_responses(self, ctx):
                return None

        import atri_x.platform.qq_onebot.adapter as adapter_module

        original = adapter_module.Transport
        adapter_module.Transport = _TempTransport
        try:
            ctx = with_temporary_connection(
                with_target(None, Target(message_type="group", group_id=7))
            )
            receipt = await adapter.send_notice(
                ctx,
                Notice(
                    target=DeliveryTarget(scope_id="group:7"),
                    outputs=[Output(kind="text", text="temp")],
                ),
            )
        finally:
            adapter_module.Transport = original
        self.assertEqual(len(created), 1)
        self.assertEqual(receipt.platform_message_ids, ["4"])
        self.assertTrue(created[0].closed)


# ---------------------------------------------------------------------------
# 回执与失败
# ---------------------------------------------------------------------------

class TestReceipts(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_raises_delivery_error(self) -> None:
        adapter = _adapter()
        adapter.transport.fail.add("send_group_segments")
        ctx = with_target(None, Target(message_type="group", group_id=1))
        with self.assertRaises(AtriXError):
            await adapter.send_chat(ctx, [Output(kind="image", source=Source(data=b"1"))])

    async def test_superadmin_partial_failure_keeps_receipt(self) -> None:
        adapter = _adapter()
        adapter.cfg.superadmins = ["11", "22", "33"]
        adapter.transport.fail_users.add(22)
        with self.assertRaises(DeliveryError) as raised:
            await adapter.send_notice(
                None,
                Notice(
                    target=DeliveryTarget(superadmins=True),
                    outputs=[Output(kind="text", text="a")],
                ),
            )
        receipt = raised.exception.receipt
        self.assertTrue(receipt.failed)
        self.assertEqual(receipt.platform_message_ids, ["1"])
        self.assertNotIn("33", [call[1].get("user_id") for call in adapter.transport.calls])


# ---------------------------------------------------------------------------
# 去重与 ctx 工具
# ---------------------------------------------------------------------------

class TestInboundDedup(unittest.TestCase):
    def test_duplicate_and_terminal_states(self) -> None:
        deduper = InboundDeduper(ttl=60, max_entries=10)
        state, duplicate = deduper.begin("k")
        self.assertEqual(state, "processing")
        self.assertFalse(duplicate)
        state, duplicate = deduper.begin("k")
        self.assertEqual(state, "processing")
        self.assertTrue(duplicate)
        deduper.finish("k", INBOUND_DEDUP_STATE_FAILED, "boom")
        state, duplicate = deduper.begin("k")
        self.assertEqual(state, INBOUND_DEDUP_STATE_FAILED)
        self.assertTrue(duplicate, "failed 在 TTL 内是终态")
        deduper.finish("missing", INBOUND_DEDUP_STATE_COMPLETED, "")
        self.assertFalse(deduper.begin("")[1])

    def test_finish_defaults_to_completed(self) -> None:
        deduper = InboundDeduper()
        deduper.begin("k")
        deduper.finish("k", "", "")
        self.assertEqual(deduper.entries["k"].state, INBOUND_DEDUP_STATE_COMPLETED)

    def test_ttl_prune_and_max_entries(self) -> None:
        deduper = InboundDeduper(ttl=0.000001, max_entries=2)
        deduper.begin("a")
        import time as _time

        _time.sleep(0.01)
        self.assertFalse(deduper.begin("a")[1], "过期条目应被清掉")
        for key in ("1", "2", "3"):
            deduper.begin(key)
        self.assertLessEqual(len(deduper.entries), 2)

    def test_inbound_key_shape(self) -> None:
        adapter = _adapter()
        event = Event.from_json(
            {"post_type": "message", "message_type": "group", "group_id": 9,
             "message_id": 5, "self_id": 100}
        )
        self.assertEqual(inbound_key(adapter, event), "qqonebot|100|group:9|5")
        self.assertEqual(inbound_key(adapter, Event()), "")

    def test_runtime_and_stats(self) -> None:
        adapter = _adapter(enabled=True)
        duplicate, state = adapter.begin_inbound_job("k")
        self.assertFalse(duplicate)
        self.assertEqual(state, "processing")
        adapter.finish_inbound_job("k", INBOUND_DEDUP_STATE_COMPLETED, "")
        duplicate, state = adapter.begin_inbound_job("k")
        self.assertTrue(duplicate)
        self.assertEqual(state, INBOUND_DEDUP_STATE_COMPLETED)
        adapter.inbound.record_reject(storage_now())
        stats = adapter.inbound_stats()
        self.assertEqual(stats.dedup_hits, 1)
        self.assertEqual(stats.queue_rejected, 1)
        self.assertIsNotNone(stats.last_queue_reject_at)

    def test_new_inbound_runtime_disabled(self) -> None:
        cfg = Config()
        cfg.inbound_dedup_enabled = False
        self.assertIsNone(new_inbound_runtime(cfg).dedup)

    def test_outbound_event_job_defaults(self) -> None:
        job = OutboundEventJob()
        self.assertEqual(job.message_key, "")
        self.assertEqual(job.event.post_type, "")


class TestCtxHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_context_cancel(self) -> None:
        ctx = _Ctx()
        self.assertTrue(await sleep_context(ctx, 0.01))
        ctx.cancel()
        self.assertFalse(await sleep_context(ctx, 0.05))
        with self.assertRaises(asyncio.CancelledError):
            raise_context_error(ctx)

    async def test_sleep_context_cancelled_during_wait(self) -> None:
        ctx = _Ctx()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.01)
            ctx.cancel()

        task = asyncio.ensure_future(cancel_soon())
        self.assertFalse(await sleep_context(ctx, 5.0))
        await task


# ---------------------------------------------------------------------------
# Transport（假连接，不连真实服务）
# ---------------------------------------------------------------------------

class TestTransport(unittest.IsolatedAsyncioTestCase):
    async def test_call_matches_echo_and_rejects_failed_status(self) -> None:
        transport = Transport(url="ws://x/", timeout=1.0)
        conn = _FakeConn()
        transport._conn = conn

        async def respond() -> None:
            await asyncio.sleep(0.01)
            frame = json.loads(conn.sent[0])
            transport.dispatch_response(
                {"status": "ok", "retcode": 0, "data": {"message_id": 5}, "echo": frame["echo"]}
            )

        task = asyncio.ensure_future(respond())
        resp = await transport.call(None, "send_group_msg", {"group_id": 1})
        await task
        self.assertEqual(resp.data, {"message_id": 5})
        self.assertEqual(json.loads(conn.sent[0])["action"], "send_group_msg")

    async def test_call_times_out_and_disconnect_wakes_waiter(self) -> None:
        transport = Transport(url="ws://x/", timeout=0.02)
        transport._conn = _FakeConn()
        with self.assertRaises(TimeoutError):
            await transport.call(None, "slow", None)

        transport2 = Transport(url="ws://x/", timeout=5.0)
        conn = _FakeConn()
        transport2._conn = conn

        async def close_soon() -> None:
            await asyncio.sleep(0.01)
            await transport2.close()

        task = asyncio.ensure_future(close_soon())
        with self.assertRaises(AtriXError):
            await transport2.call(None, "pending", None)
        await task
        self.assertTrue(conn.closed)

    async def test_call_without_connection(self) -> None:
        with self.assertRaises(AtriXError):
            await Transport(timeout=1.0).call(None, "x", None)

    async def test_read_decodes_events_and_skips_responses(self) -> None:
        transport = Transport(url="ws://x/", timeout=1.0)
        conn = _FakeConn()
        transport._conn = conn
        await conn.incoming.put(json.dumps({"echo": "none", "status": "ok", "retcode": 0}))
        self.assertIsNone(await transport.read(None))
        await conn.incoming.put(
            json.dumps({"post_type": "message", "message_type": "private", "user_id": 1})
        )
        event = await transport.read(None)
        self.assertIsNotNone(event)
        self.assertEqual(event.message_type, "private")
        await conn.incoming.put("not json")
        with self.assertRaises(InvalidError):
            await transport.read(None)

    async def test_read_is_cancellable_by_ctx(self) -> None:
        transport = Transport(url="ws://x/", timeout=5.0)
        conn = _FakeConn()
        transport._conn = conn
        ctx = _Ctx()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            ctx.cancel(TimeoutError("cancelled"))

        task = asyncio.ensure_future(cancel_soon())
        with self.assertRaises(TimeoutError):
            await transport.read(ctx)
        await task
        # ctx 为 None 时仍走直连路径（旧行为不变）。
        await conn.incoming.put(json.dumps({"post_type": "meta_event"}))
        event = await transport.read(None)
        self.assertEqual(event.post_type, "meta_event")

    async def test_dispatch_response_ignores_unknown_echo(self) -> None:
        transport = Transport()
        self.assertFalse(transport.dispatch_response("nope"))
        self.assertFalse(transport.dispatch_response({"status": "ok"}))
        self.assertFalse(transport.dispatch_response({"echo": "e", "retcode": "bad"}))
        self.assertTrue(transport.dispatch_response({"echo": "e", "status": "ok", "retcode": 0}))

    async def test_write_timeout_scales_with_frame(self) -> None:
        transport = Transport(timeout=15.0)
        self.assertEqual(transport.write_timeout(0), 15.0)
        self.assertEqual(transport.write_timeout(512 * 1024), 15.0)
        self.assertEqual(transport.write_timeout(3 * 1024 * 1024), 18.0)
        self.assertEqual(transport.write_timeout(30 * 1024 * 1024), 30.0)
        self.assertEqual(write_timeout_for(15.0, 30 * 1024 * 1024), 30.0)

    async def test_read_limit_is_applied_and_restored(self) -> None:
        transport = Transport(url="ws://x/", timeout=1.0, read_limit_bytes=1024)
        conn = _FakeConn()
        transport._conn = conn
        seen: list[int] = []

        async def respond() -> None:
            await asyncio.sleep(0.01)
            seen.append(conn.max_size)
            frame = json.loads(conn.sent[0])
            transport.dispatch_response({"status": "ok", "retcode": 0, "data": {}, "echo": frame["echo"]})

        task = asyncio.ensure_future(respond())
        await transport.get_forward_msg(None, "7")
        await task
        self.assertEqual(seen[0], 1024 + 64 * 1024)
        self.assertEqual(conn.max_size, 8 * 1024 * 1024)
        with self.assertRaises(InvalidError):
            await transport.get_forward_msg(None, "  ")

    async def test_acquire_write_respects_ctx(self) -> None:
        transport = Transport(timeout=0.5)
        await transport.acquire_write(None, 0.5, "a")
        ctx = _Ctx()
        ctx.cancel(TimeoutError("deadline"))
        with self.assertRaises(TimeoutError):
            await transport.acquire_write(ctx, 0.5, "b")
        transport.release_write()
        await transport.acquire_write(None, 0.5, "c")
        transport.release_write()

    async def test_acquire_write_timeout(self) -> None:
        transport = Transport(timeout=0.02)
        await transport.acquire_write(None, 0.02, "a")
        with self.assertRaises(AtriXError):
            await transport.acquire_write(None, 0.02, "blocked")
        transport.release_write()

    async def test_send_forward_message_requires_id(self) -> None:
        transport = Transport(url="ws://x/", timeout=1.0)
        conn = _FakeConn()
        transport._conn = conn

        async def respond() -> None:
            await asyncio.sleep(0.01)
            frame = json.loads(conn.sent[0])
            transport.dispatch_response(
                {"status": "ok", "retcode": 0, "data": {"message_id": 0}, "echo": frame["echo"]}
            )

        task = asyncio.ensure_future(respond())
        with self.assertRaises(AtriXError):
            await transport.send_group_forward_message(None, 1, [])
        await task

    async def test_connect_requires_url(self) -> None:
        with self.assertRaises(InvalidError):
            await Transport().connect(None)

    async def test_read_responses_returns_on_error(self) -> None:
        transport = Transport(url="ws://x/", timeout=0.05)
        await asyncio.wait_for(transport.read_responses(None), 1.0)

    async def test_get_message_parses_payload(self) -> None:
        transport = Transport(url="ws://x/", timeout=1.0)
        conn = _FakeConn()
        transport._conn = conn

        async def respond() -> None:
            await asyncio.sleep(0.01)
            frame = json.loads(conn.sent[0])
            transport.dispatch_response(
                {
                    "status": "ok",
                    "retcode": 0,
                    "echo": frame["echo"],
                    "data": {
                        "time": 1,
                        "message_type": "group",
                        "message_id": 5,
                        "user_id": 7,
                        "group_id": 9,
                        "raw_message": "hi",
                        "message": [{"type": "text", "data": {"text": "hi"}}],
                        "sender": {"user_id": 7, "nickname": "n", "card": "c", "role": "admin"},
                    },
                }
            )

        task = asyncio.ensure_future(respond())
        data = await transport.get_message(None, "5")
        await task
        self.assertEqual(data.user_id, 7)
        self.assertEqual(data.sender.card, "c")
        with self.assertRaises(InvalidError):
            await transport.get_message(None, "x")


# ---------------------------------------------------------------------------
# 媒体解析
# ---------------------------------------------------------------------------

class TestResolveMedia(unittest.IsolatedAsyncioTestCase):
    async def test_size_limit_short_circuits(self) -> None:
        adapter = _adapter()
        segment = MessageSegment(type=SegmentImage, size=10)
        with self.assertRaises(MediaTooLargeError):
            await resolve_media(adapter, None, segment, 5)

    async def test_http_url_passthrough(self) -> None:
        adapter = _adapter()
        segment = MessageSegment(
            type=SegmentImage, url="https://x/a.png", mime_type="image/png"
        )
        source = await resolve_media(adapter, None, segment, 100)
        self.assertEqual(source.url, "https://x/a.png")
        self.assertEqual(source.mime_type, "image/png")

    async def test_image_path_from_transport(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_image"] = {"file": "C:/tmp/a.png", "url": ""}
        segment = MessageSegment(type=SegmentImage, platform_file_id="fid")
        source = await resolve_media(adapter, None, segment, 100)
        self.assertEqual(source.path, "C:/tmp/a.png")

    async def test_file_size_limit_from_transport(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_file"] = {
            "file": "C:/tmp/a.bin",
            "url": "",
            "file_size": "999",
        }
        segment = MessageSegment(type=SegmentFile, platform_file_id="fid")
        with self.assertRaises(MediaTooLargeError):
            await resolve_media(adapter, None, segment, 100)

    async def test_unavailable_and_unsupported(self) -> None:
        adapter = _adapter()
        with self.assertRaises(AtriXError):
            await resolve_media(adapter, None, MessageSegment(type=SegmentImage), 100)
        segment = MessageSegment(type=SegmentAt, platform_file_id="x")
        with self.assertRaises(AtriXError):
            await resolve_media(adapter, None, segment, 100)

    async def test_file_url_from_transport(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_file"] = {
            "file": "",
            "url": "https://cdn/x.bin",
            "file_size": "10",
        }
        source = await resolve_media(
            adapter, None, MessageSegment(type=SegmentFile, platform_file_id="fid"), 100
        )
        self.assertEqual(source.url, "https://cdn/x.bin")


# ---------------------------------------------------------------------------
# 可选能力
# ---------------------------------------------------------------------------

class TestCapabilities(unittest.IsolatedAsyncioTestCase):
    async def test_group_history(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_group_msg_history"] = {
            "messages": [
                {
                    "time": 1700000000,
                    "message_id": 5,
                    "user_id": 7,
                    "sender": {"user_id": 7, "nickname": "n", "card": "c"},
                    "raw_message": "hi",
                    "message": [{"type": "text", "data": {"text": "hi"}}],
                }
            ]
        }
        rows = await fetch_group_history(adapter, None, "9", None, None, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].platform_message_id, "5")
        self.assertEqual(rows[0].sender_id, "7")
        self.assertEqual(rows[0].sender_name, "c")
        self.assertEqual(rows[0].text, "hi")
        self.assertEqual(rows[0].created_at.tzinfo is not None, True)
        self.assertEqual(adapter.transport.calls[-1][1]["count"], 10)

    async def test_group_history_direct_array_and_filters(self) -> None:
        from datetime import datetime, timezone

        adapter = _adapter()
        adapter.transport.responses["get_group_msg_history"] = [
            {"time": 100, "message_id": 1, "user_id": 1, "raw_message": "old", "message": "old"},
            {"time": 1700000000, "message_id": 2, "user_id": 1, "raw_message": "new", "message": "new"},
        ]
        since = datetime.fromtimestamp(1000, timezone.utc)
        rows = await fetch_group_history(adapter, None, "9", since, None, 999999)
        self.assertEqual([row.platform_message_id for row in rows], ["2"])
        self.assertEqual(adapter.transport.calls[-1][1]["count"], 5000)

    async def test_group_history_requires_transport(self) -> None:
        adapter = _adapter()
        adapter.transport = None
        with self.assertRaises(AtriXError):
            await fetch_group_history(adapter, None, "9", None, None, 10)

    async def test_group_history_bad_id(self) -> None:
        adapter = _adapter()
        with self.assertRaises(InvalidError):
            await fetch_group_history(adapter, None, "x", None, None, 10)

    async def test_group_info_and_members(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_group_info"] = {
            "group_id": 9, "group_name": " 群 ", "member_count": 3
        }
        adapter.transport.responses["get_group_member_list"] = [
            {"user_id": 7, "nickname": "n", "card": "c", "role": "admin"},
            {"user_id": "", "nickname": "skip"},
        ]
        info = await get_group_info(adapter, None, "9")
        self.assertEqual((info.id, info.name, info.member_count), ("9", "群", 3))
        members = await get_group_member_list(adapter, None, "9")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].display_name, "c")
        self.assertEqual(members[0].role, "admin")
        # Go 直接 ParseInt(groupID)：带 scope 前缀的 ID 在 Go 里同样报错。
        with self.assertRaises(InvalidError):
            await get_group_info(adapter, None, "group:9")

    async def test_group_info_errors(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_group_info"] = [1]
        with self.assertRaises(InvalidError):
            await get_group_info(adapter, None, "9")
        adapter2 = _adapter()
        adapter2.transport.responses["get_group_member_list"] = {"a": 1}
        with self.assertRaises(InvalidError):
            await get_group_member_list(adapter2, None, "9")

    async def test_avatar_url(self) -> None:
        url = get_user_avatar_url("123", 0)
        self.assertEqual(url, "https://q1.qlogo.cn/g?b=qq&nk=123&s=100")
        self.assertIn("s=640", get_user_avatar_url("123", 640))
        # size 超上限（>640）与 <=0 一样退到 100，与 Go 的一致。
        self.assertIn("s=100", get_user_avatar_url("123", 99999))
        with self.assertRaises(InvalidError):
            get_user_avatar_url("", 10)

    async def test_raw_id_string(self) -> None:
        self.assertEqual(raw_id_string(7), "7")
        self.assertEqual(raw_id_string(" 7 "), "7")
        self.assertEqual(raw_id_string(None), "")
        self.assertEqual(raw_id_string(True), "")

    async def test_superadmin_matching(self) -> None:
        self.assertTrue(is_configured_superadmin(["qqonebot:7"], "7"))
        self.assertFalse(is_configured_superadmin(["7"], ""))
        self.assertFalse(is_configured_superadmin(None, "7"))


# ---------------------------------------------------------------------------
# 入站处理（prepareInbound / handleEvent）
# ---------------------------------------------------------------------------

class _CaptureHandler:
    def __init__(self, events: bool = False) -> None:
        self.messages: list[tuple[object, str]] = []
        self.events: list[PlatformEvent] = []
        self._events = events

    async def handle_message(self, ctx, text):
        self.messages.append((ctx, text))

    async def handle_platform_event(self, ctx, event):
        self.events.append(event)


class TestInboundFlow(unittest.IsolatedAsyncioTestCase):
    async def test_handle_event_builds_message_context(self) -> None:
        adapter = _adapter(enabled=True)
        adapter.cfg.trigger_keywords = ["atri"]
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "time": 1700000000,
                "self_id": 100,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "message": [{"type": "text", "data": {"text": "hello"}}],
                "sender": {"user_id": 7, "nickname": "昵称", "card": "名片", "role": "member"},
            }
        )
        await adapter.handle_event(None, handler, event)
        self.assertEqual(len(handler.messages), 1)
        ctx, text = handler.messages[0]
        self.assertEqual(text, "hello")
        from atri_x.platform.platform import message_context_from

        message_ctx, found = message_context_from(ctx)
        self.assertTrue(found)
        self.assertEqual(message_ctx.platform, "qqonebot")
        self.assertEqual(message_ctx.scope_id, "group:9")
        self.assertEqual(message_ctx.group_role, GroupRoleMember)
        self.assertEqual(message_ctx.display_name, "名片")
        self.assertEqual(message_ctx.platform_message_id, "5")
        self.assertTrue(message_ctx.buffer_assistant_output)
        self.assertTrue(message_ctx.match_text_set)
        self.assertEqual(message_ctx.trigger_keywords, ["atri"])
        self.assertEqual(message_ctx.bot.user_id, "100")
        self.assertEqual(message_ctx.meta["qq_onebot.group_id"], "9")
        self.assertEqual(json.loads(message_ctx.platform_message), event.message)
        target, ok = target_from_context(ctx)
        self.assertTrue(ok)
        self.assertEqual(target, Target(message_type="group", user_id=7, group_id=9))

    async def test_empty_message_is_skipped(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        event = Event.from_json(
            {"post_type": "message", "message_type": "private", "user_id": 1, "message": ""}
        )
        await adapter.handle_event(None, handler, event)
        self.assertEqual(handler.messages, [])

    async def test_non_message_type_is_recorded_only(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler()
        event = Event.from_json(
            {"post_type": "message", "message_type": "channel", "user_id": 1, "raw_message": "x"}
        )
        _msg_ctx, _ctx, text, ok = await adapter.prepare_inbound(None, None, event)
        self.assertFalse(ok)
        self.assertEqual(text, "")
        self.assertEqual(handler.messages, [])

    async def test_at_resolution_uses_group_member_info(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_group_member_info"] = {
            "user_id": 55, "nickname": "小明", "card": ""
        }
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "self_id": 100,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "message": [
                    {"type": "at", "data": {"qq": "55"}},
                    {"type": "text", "data": {"text": " 在吗"}},
                ],
            }
        )
        await adapter.handle_event(None, handler, event)
        self.assertEqual(handler.messages[0][1], "[at 小明 qq:55] 在吗")

    async def test_at_resolution_failure_keeps_original(self) -> None:
        adapter = _adapter()
        adapter.transport.fail.add("55")
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "self_id": 100,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "message": [{"type": "at", "data": {"qq": "55"}}],
            }
        )
        await adapter.handle_event(None, handler, event)
        self.assertEqual(handler.messages[0][1], "[at qq:55]")

    async def test_forward_expansion_appends_trust_marker(self) -> None:
        adapter = _adapter()
        adapter.transport.forward["f1"] = {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": 7,
                        "nickname": "小明",
                        "content": [{"type": "text", "data": {"text": "转发内容"}}],
                    },
                }
            ]
        }
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "self_id": 100,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "message": [
                    {"type": "text", "data": {"text": "看这个"}},
                    {"type": "forward", "data": {"id": "f1"}},
                ],
            }
        )
        await adapter.handle_event(None, handler, event)
        text = handler.messages[0][1]
        self.assertIn("看这个", text)
        self.assertIn(FORWARD_TRUST_MARKER, text)
        self.assertIn("转发内容", text)

    async def test_forward_expansion_failure_marks_text(self) -> None:
        adapter = _adapter()
        adapter.transport.fail.add("f1")
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "self_id": 100,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "message": [{"type": "forward", "data": {"id": "f1"}}],
            }
        )
        await adapter.handle_event(None, handler, event)
        self.assertIn("拉取失败", handler.messages[0][1])

    async def test_reply_to_bot_uses_self_id(self) -> None:
        adapter = _adapter()
        adapter.transport.responses["get_msg"] = {
            "user_id": 100,
            "message": [{"type": "text", "data": {"text": "bot 的回答"}}],
            "sender": {"user_id": 100, "nickname": "bot"},
        }
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "self_id": 100,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "message": [
                    {"type": "reply", "data": {"id": "88"}},
                    {"type": "text", "data": {"text": "继续"}},
                ],
            }
        )
        await adapter.handle_event(None, handler, event)
        from atri_x.platform.platform import message_context_from

        message_ctx, _found = message_context_from(handler.messages[0][0])
        self.assertEqual(message_ctx.reply_to_sender_id, "100")
        self.assertIn("bot 的回答", message_ctx.context_text)

    async def test_platform_event_forwarding(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler(events=True)
        event = Event.from_json(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": 9,
                "user_id": 7,
                "operator_id": 8,
                "message_id": 5,
            }
        )
        await adapter.handle_platform_event(None, handler, event)
        self.assertEqual(len(handler.events), 1)
        forwarded = handler.events[0]
        self.assertEqual(forwarded.platform, "qqonebot")
        self.assertEqual(forwarded.kind, "notice")
        self.assertEqual(forwarded.type, "group_recall")
        self.assertEqual(forwarded.scope_id, "group:9")
        self.assertEqual(forwarded.user_id, "8")
        self.assertEqual(forwarded.message_id, "5")
        self.assertEqual(json.loads(forwarded.raw)["notice_type"], "group_recall")

    async def test_platform_event_ban_uses_target(self) -> None:
        adapter = _adapter()
        handler = _CaptureHandler(events=True)
        event = Event.from_json(
            {"post_type": "notice", "notice_type": "group_ban", "group_id": 9, "target_id": 6}
        )
        await adapter.handle_platform_event(None, handler, event)
        self.assertEqual(handler.events[0].user_id, "6")

    async def test_platform_event_skipped_for_plain_handler(self) -> None:
        class _Plain:
            async def handle_message(self, ctx, text):
                return None

        adapter = _adapter()
        await adapter.handle_platform_event(None, _Plain(), Event.from_json({"post_type": "notice"}))

    async def test_record_chat_message(self) -> None:
        class _History:
            def __init__(self) -> None:
                self.rows = []

            def append(self, row) -> None:
                self.rows.append(row)

        adapter = _adapter()
        adapter.chat_history = _History()
        event = Event.from_json(
            {
                "time": 1700000000,
                "post_type": "message",
                "message_type": "group",
                "message_id": 5,
                "user_id": 7,
                "group_id": 9,
                "raw_message": "hi",
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "sender": {"user_id": 7, "nickname": "n", "card": "c"},
            }
        )
        normalized = NormalizedMessage(text="hi", segments=[])
        adapter.record_chat_message(None, event, normalized, ReplyContext())
        self.assertEqual(len(adapter.chat_history.rows), 1)
        row = adapter.chat_history.rows[0]
        self.assertEqual(row.platform, "qqonebot")
        self.assertEqual(row.platform_scope_id, "group:9")
        self.assertEqual(row.scope_type, "group")
        self.assertEqual(row.platform_message_id, "5")
        self.assertEqual(row.sender_id, "7")
        self.assertEqual(row.sender_name, "c")
        self.assertEqual(row.created_at.year, 2023)

    async def test_record_chat_message_skips_empty(self) -> None:
        class _History:
            def __init__(self) -> None:
                self.rows = []

            def append(self, row) -> None:
                self.rows.append(row)

        adapter = _adapter()
        adapter.chat_history = _History()
        adapter.record_chat_message(None, Event(), NormalizedMessage(), ReplyContext())
        self.assertEqual(adapter.chat_history.rows, [])


# ---------------------------------------------------------------------------
# 读循环与重连
# ---------------------------------------------------------------------------

class _LoopTransport:
    """假 Transport：按脚本回放事件，失败时抛错。"""

    def __init__(self, events, fail_times: int = 0) -> None:
        self.events = list(events)
        self.fail_times = fail_times
        self.connects = 0
        self.closes = 0

    async def connect(self, ctx):
        self.connects += 1

    async def close(self, status=1000, reason=""):
        self.closes += 1

    async def read(self, ctx):
        # 真实传输在 I/O 上会让出事件循环；假传输要让出一次，worker 才有机会跑。
        await asyncio.sleep(0)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise AtriXError("disconnected")
        if not self.events:
            raise AtriXError("disconnected")
        return self.events.pop(0)


class TestRunLoop(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_run_is_noop(self) -> None:
        adapter = _adapter()
        transport = _LoopTransport([])
        adapter.transport = transport
        await adapter.run(None, _CaptureHandler())
        self.assertEqual(transport.connects, 0)

    async def test_run_reconnects_after_disconnect(self) -> None:
        adapter = _adapter(enabled=True)
        adapter.cfg.reconnect_interval_seconds = 0.01
        transport = _LoopTransport([], fail_times=1)
        adapter.transport = transport
        ctx = _Ctx()
        task = asyncio.ensure_future(adapter.run(ctx, _CaptureHandler()))
        await asyncio.sleep(0.05)
        ctx.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(transport.connects, 1)
        self.assertGreaterEqual(transport.closes, 1)

    async def test_run_raises_connect_failure_when_ctx_cancelled(self) -> None:
        adapter = _adapter(enabled=True)
        adapter.cfg.reconnect_interval_seconds = 0.01
        transport = _LoopTransport([])

        async def boom(ctx):
            raise AtriXError("connect failed")

        transport.connect = boom
        adapter.transport = transport
        ctx = _Ctx()
        task = asyncio.ensure_future(adapter.run(ctx, _CaptureHandler()))
        await asyncio.sleep(0.03)
        ctx.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_read_loop_dispatches_messages_and_events(self) -> None:
        adapter = _adapter(enabled=True)
        handler = _CaptureHandler(events=True)
        message_event = Event.from_json(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 5,
                "user_id": 7,
                "self_id": 100,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        notice_event = Event.from_json(
            {"post_type": "notice", "notice_type": "group_recall", "group_id": 9, "user_id": 7}
        )
        transport = _LoopTransport([message_event, notice_event])
        adapter.transport = transport
        with self.assertRaises(AtriXError):
            await adapter.read_loop(None, handler)
        for _ in range(50):
            if handler.messages and handler.events:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(handler.messages), 1)
        self.assertEqual(len(handler.events), 1)

    async def test_duplicate_message_is_suppressed(self) -> None:
        adapter = _adapter(enabled=True)
        handler = _CaptureHandler()
        event = Event.from_json(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 5,
                "user_id": 7,
                "self_id": 100,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        transport = _LoopTransport([event, event])
        adapter.transport = transport
        with self.assertRaises(AtriXError):
            await adapter.read_loop(None, handler)
        for _ in range(50):
            if handler.messages:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(len(handler.messages), 1)
        self.assertEqual(adapter.inbound_stats().dedup_hits, 1)

    async def test_connect_notifier_is_invoked(self) -> None:
        adapter = _adapter(enabled=True)
        adapter.cfg.reconnect_interval_seconds = 0.01
        seen: list[str] = []

        def notify(ctx, platform):
            seen.append(platform)

        adapter.set_connect_notifier(notify)
        transport = _LoopTransport([], fail_times=1)
        adapter.transport = transport
        ctx = _Ctx()
        task = asyncio.ensure_future(adapter.run(ctx, _CaptureHandler()))
        await asyncio.sleep(0.05)
        ctx.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual(set(seen), {"qqonebot"})


if __name__ == "__main__":
    unittest.main()
