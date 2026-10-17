"""``atri_x.platform`` 测试。

对应 Go：``internal/platform/{media_test.go,config_test.go}`` 与 ``platform.go``
的信号/可选能力语义。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.platform import (
    Backoff,
    ConnectedEvent,
    ConversationGroup,
    ConversationKind,
    EventKind,
    GroupAssetProvider,
    GroupDirectoryProvider,
    GroupHistoryProvider,
    GroupInfo,
    GroupMember,
    HistoryMessage,
    MediaResolver,
    MessageContext,
    MessageSegment,
    MessageSegmentType,
    PlatformConnected,
    PlatformEvent,
    PlatformEventHandler,
    PlatformHandler,
    Runtime,
    SegmentFile,
    SegmentImage,
    SegmentText,
    StreamingMessageSender,
    UserAvatarProvider,
    command_name,
    decode_config,
    has_command_prefix,
    marshal_chat_segments,
    message_context_from,
    new_backoff,
    primary_command_prefix,
    strip_trigger_keyword,
    unmarshal_chat_segments,
    with_message_context,
)


class TestEnums(unittest.TestCase):
    def test_event_kinds_and_segment_types(self) -> None:
        self.assertEqual(EventKind.ALL, ("notice", "request", "meta_event"))
        self.assertEqual(MessageSegmentType.ALL, ("text", "image", "file", "at"))
        self.assertEqual(SegmentText, "text")
        self.assertEqual(SegmentImage, "image")
        self.assertEqual(
            ConversationKind.ALL, ("unknown", "private", "group", "channel")
        )
        self.assertEqual(ConversationGroup, "group")


class TestMessageSegmentJSON(unittest.TestCase):
    def test_to_json_omits_empty_fields_with_go_keys(self) -> None:
        segment = MessageSegment(
            type=SegmentImage,
            url="https://x/a.png",
            media_id="m1",
            mime_type="image/png",
            name="a.png",
            size=12,
            platform_file_id="pf1",
            user_id="u1",
            text="t",
        )
        self.assertEqual(
            segment.to_json(),
            {
                "type": "image",
                "text": "t",
                "user_id": "u1",
                "url": "https://x/a.png",
                "media": "m1",
                "platform_file_id": "pf1",
                "mime_type": "image/png",
                "name": "a.png",
                "size": 12,
            },
        )
        self.assertEqual(MessageSegment(type=SegmentText).to_json(), {"type": "text"})

    def test_from_json_accepts_media_key_and_ignores_unknown(self) -> None:
        segment = MessageSegment.from_json(
            {"type": "image", "media": "m1", "size": 3, "unknown": "x"}
        )
        self.assertEqual(segment.media_id, "m1")
        self.assertEqual(segment.size, 3)
        self.assertEqual(MessageSegment.from_json(None), MessageSegment())
        self.assertEqual(MessageSegment.from_json("x"), MessageSegment())


class TestMarshalChatSegments(unittest.TestCase):
    def test_drops_unsupported_types_and_local_sources(self) -> None:
        segments = [
            MessageSegment(type=SegmentText, text="hi", url="https://x/a.png", media_id="m"),
            MessageSegment(type="at", user_id="u1"),
        ]
        self.assertEqual(marshal_chat_segments(segments), '[{"type":"text","text":"hi","url":"https://x/a.png"}]')

    def test_keeps_plain_https_url_and_clears_media_id(self) -> None:
        stored = marshal_chat_segments(
            [MessageSegment(type=SegmentImage, url="https://x/a.png", media_id="m1")]
        )
        self.assertIn('"url":"https://x/a.png"', stored)
        self.assertNotIn("media", stored)

    def test_drops_urls_with_credentials_or_tokens(self) -> None:
        cases = [
            "https://x/a.png?token=1",
            "https://x/a.png?X-Amz-Signature=1",
            "https://x/a.png?x-goog-signature=1",
            "https://x/a.png?rkey=1",
            "https://x/a.png?sig=1",
            "https://x/a.png?auth_key=1",
            "https://x/a.png?signature=1",
            "https://user:pw@x/a.png",
            "https://api.telegram.org/file/bot123/x.png",
            "file:///a.png",
            "ftp://x/a.png",
            "https:///a.png",
            "https://x/a b.png",
            "",
        ]
        for url in cases:
            with self.subTest(url=url):
                stored = marshal_chat_segments([MessageSegment(type=SegmentImage, url=url)])
                self.assertNotIn('"url"', stored)

    def test_platform_file_id_and_name_are_sanitized(self) -> None:
        stored = marshal_chat_segments(
            [
                MessageSegment(
                    type=SegmentFile,
                    platform_file_id="a/b",
                    name="dir/file.txt",
                )
            ]
        )
        self.assertIn('"name":"file.txt"', stored)
        self.assertNotIn("platform_file_id", stored)

        url_name = marshal_chat_segments(
            [MessageSegment(type=SegmentFile, name="https://x/a.txt")]
        )
        self.assertNotIn('"name"', url_name)

        inline = marshal_chat_segments(
            [MessageSegment(type=SegmentFile, name="data:application/octet-stream;base64,AA")]
        )
        self.assertNotIn('"name"', inline)

        base64_name = marshal_chat_segments([MessageSegment(type=SegmentFile, name="base64://secret")])
        self.assertNotIn('"name"', base64_name)

        windows_name = marshal_chat_segments(
            [MessageSegment(type=SegmentFile, name="C:\\media\\secret.txt")]
        )
        self.assertIn('"name":"secret.txt"', windows_name)
        self.assertNotIn("C:", windows_name)

        colon_file_id = marshal_chat_segments(
            [MessageSegment(type=SegmentFile, platform_file_id="C:/media/secret.txt")]
        )
        self.assertNotIn("platform_file_id", colon_file_id)

    def test_roundtrip_preserves_order_and_duplicates(self) -> None:
        image = MessageSegment(
            type=SegmentImage,
            url="https://example.com/a.png",
            platform_file_id="file-id",
            name="a.png",
            mime_type="image/png",
            size=42,
        )
        segments = [
            MessageSegment(type=SegmentText, text="hello"),
            image,
            MessageSegment(type=SegmentFile, url="data:image/png;base64,c2VjcmV0"),
            MessageSegment(type=SegmentImage, url="https://x/bot123:secret/a.jpg"),
            MessageSegment(type=SegmentImage, url="https://example.com/a?X-Amz-Signature=s"),
            MessageSegment(type=SegmentImage, url="https://example.com/a?token=s"),
            image,
        ]
        raw = marshal_chat_segments(segments)
        for forbidden in ("data:", "base64:", "bot123", "Signature", "token="):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, raw)
        got = unmarshal_chat_segments(raw)
        self.assertEqual(len(got), len(segments))
        self.assertEqual(got[1], image)
        self.assertEqual(got[-1], image)

    def test_empty_input_returns_empty_string(self) -> None:
        self.assertEqual(marshal_chat_segments([]), "")
        self.assertEqual(marshal_chat_segments(None), "")

    def test_unmarshal_roundtrip_and_bad_input(self) -> None:
        stored = marshal_chat_segments([MessageSegment(type=SegmentText, text="hi")])
        self.assertEqual(unmarshal_chat_segments(stored)[0].text, "hi")
        self.assertEqual(unmarshal_chat_segments(""), [])
        self.assertEqual(unmarshal_chat_segments("not json"), [])
        self.assertEqual(unmarshal_chat_segments("{}"), [])


class TestConfigHelpers(unittest.TestCase):
    def test_primary_command_prefix(self) -> None:
        cases = [
            (None, "/"),
            ([], "/"),
            (["", "   "], "/"),
            (["/"], "/"),
            (["/*"], "/*"),
            (["/", "/*"], "/"),
        ]
        for prefixes, want in cases:
            with self.subTest(prefixes=prefixes):
                self.assertEqual(primary_command_prefix(prefixes), want)

    def test_strip_trigger_keyword_removes_only_matched_prefix(self) -> None:
        self.assertEqual(strip_trigger_keyword("  你好 ATRI", ["你好"]), ("ATRI", True))
        self.assertEqual(strip_trigger_keyword("ATRI 你好", ["你好"]), ("ATRI 你好", False))
        self.assertEqual(strip_trigger_keyword("你好", ["你好"]), ("", True))
        self.assertEqual(strip_trigger_keyword("x", ["", "  "]), ("x", False))

    def test_command_name(self) -> None:
        self.assertEqual(command_name("//help me", ["/"]), ("/help", True))
        self.assertEqual(command_name("/help", ["/"]), ("help", True))
        self.assertEqual(command_name("/", ["/"]), ("", True))
        self.assertEqual(command_name("hello", ["/"]), ("", False))
        self.assertTrue(has_command_prefix("/help", ["/"]))
        self.assertFalse(has_command_prefix("help", ["/"]))

    def test_decode_config_assigns_known_fields_case_insensitively(self) -> None:
        class Config:
            def __init__(self) -> None:
                self.access_token = ""
                self.self_id = ""
                self.extra = "keep"

        config = Config()
        decode_config({"access_token": "t", "SELF_ID": "1", "unknown": "x"}, config)
        self.assertEqual(config.access_token, "t")
        self.assertEqual(config.self_id, "1")
        self.assertEqual(config.extra, "keep")

    def test_decode_config_edges(self) -> None:
        decode_config(None, object())
        with self.assertRaises(Exception):
            decode_config({"a": 1}, None)
        with self.assertRaises(Exception):
            decode_config(["not", "a", "table"], object())


class TestBackoff(unittest.TestCase):
    def test_delay_doubles_and_caps(self) -> None:
        backoff = Backoff(0, 0)
        self.assertEqual(
            [backoff.delay() for _ in range(5)], [3.0, 6.0, 10.0, 10.0, 10.0]
        )
        self.assertEqual(new_backoff(1.0, 4.0).delay(), 1.0)

    def test_max_smaller_than_base_falls_back_to_default_max(self) -> None:
        backoff = Backoff(5.0, 1.0)
        self.assertEqual(backoff.delay(), 5.0)
        self.assertEqual(backoff.delay(), 10.0)

    def test_should_warn_only_on_transition(self) -> None:
        backoff = Backoff()
        self.assertTrue(backoff.should_warn())
        self.assertFalse(backoff.should_warn())
        backoff.reset()
        self.assertTrue(backoff.should_warn())
        self.assertEqual(backoff.delay(), 3.0)


class TestMessageContext(unittest.TestCase):
    def test_roundtrip(self) -> None:
        msg = MessageContext(platform="cli", scope_id="s1", match_text="hi")
        ctx = with_message_context(None, msg)
        got, ok = message_context_from(ctx)
        self.assertTrue(ok)
        self.assertEqual(got, msg)
        self.assertEqual(message_context_from(None), (MessageContext(), False))

    def test_message_context_defaults(self) -> None:
        msg = MessageContext()
        self.assertEqual(msg.group_role, "unknown")
        self.assertEqual(msg.conversation_kind, "unknown")
        self.assertFalse(msg.match_text_set)
        self.assertIsNone(msg.sender)
        self.assertIsNone(msg.media_resolver)
        self.assertEqual(msg.bot.user_id, "")
        self.assertEqual(msg.reply.message_id, "")
        self.assertEqual(msg.segments, [])
        self.assertEqual(msg.mentions, [])


class TestOptionalCapabilities(unittest.TestCase):
    """可选能力必须能像 Go 的类型断言那样判定。"""

    def test_type_assertions_work_by_method_presence(self) -> None:
        class Adapter:
            def name(self):
                return "cli"

            async def run(self, ctx, handler):
                return None

            async def send_chat(self, ctx, outputs):
                return None

            async def send_notice(self, ctx, notice):
                return None

            async def fetch_group_history(self, ctx, group_id, since, until, limit):
                return [HistoryMessage(platform_message_id="m1")]

            async def get_group_info(self, ctx, group_id):
                return GroupInfo(id=group_id)

            async def get_group_member_list(self, ctx, group_id):
                return [GroupMember(user_id="u1")]

            async def get_user_avatar_url(self, ctx, user_id, size):
                return "https://x/a.png"

            async def upload_group_file(self, ctx, group_id, file_path, filename):
                return None

            async def upload_group_album(self, ctx, group_id, image_path, album_id, album_name):
                return None

            async def resolve_media(self, ctx, segment, max_bytes):
                return None

        adapter = Adapter()
        for protocol in (
            Runtime,
            GroupHistoryProvider,
            GroupDirectoryProvider,
            UserAvatarProvider,
            GroupAssetProvider,
            MediaResolver,
        ):
            with self.subTest(protocol=protocol.__name__):
                self.assertIsInstance(adapter, protocol)

        class Bare:
            pass

        self.assertNotIsInstance(Bare(), Runtime)
        self.assertNotIsInstance(Bare(), GroupHistoryProvider)

    def test_platform_handler_and_event_handler(self) -> None:
        class Handler:
            async def handle_message(self, ctx, text):
                return None

            async def handle_platform_event(self, ctx, event):
                return None

        self.assertIsInstance(Handler(), PlatformHandler)
        self.assertIsInstance(Handler(), PlatformEventHandler)
        self.assertNotIsInstance(object(), PlatformHandler)


class TestPlatformConnectedSignal(unittest.IsolatedAsyncioTestCase):
    async def test_connect_emit_and_disconnect(self) -> None:
        seen: list[str] = []

        def handler(ctx, event: ConnectedEvent) -> None:
            seen.append(event.platform)

        PlatformConnected.connect(handler)
        try:
            PlatformConnected.emit(None, ConnectedEvent(platform="cli"))
            self.assertEqual(seen, ["cli"])
            self.assertIn(handler, PlatformConnected.handlers())
        finally:
            PlatformConnected.disconnect(handler)
        PlatformConnected.emit(None, ConnectedEvent(platform="qq"))
        self.assertEqual(seen, ["cli"])
        PlatformConnected.disconnect(handler)  # 幂等

    async def test_emit_async_awaits_handlers_in_order(self) -> None:
        order: list[str] = []

        async def first(ctx, event):
            order.append("first")

        async def second(ctx, event):
            order.append("second")

        PlatformConnected.connect(first)
        PlatformConnected.connect(second)
        try:
            await PlatformConnected.emit_async(None, ConnectedEvent(platform="cli"))
        finally:
            PlatformConnected.disconnect(first)
            PlatformConnected.disconnect(second)
        self.assertEqual(order, ["first", "second"])

    async def test_sync_emit_rejects_coroutine_handler(self) -> None:
        async def handler(ctx, event):
            return None

        PlatformConnected.connect(handler)
        try:
            with self.assertRaises(TypeError):
                PlatformConnected.emit(None, ConnectedEvent(platform="cli"))
        finally:
            PlatformConnected.disconnect(handler)

    def test_signal_name_matches_go(self) -> None:
        self.assertEqual(PlatformConnected.name, "platform.connected")


class TestPlatformEvent(unittest.TestCase):
    def test_event_defaults(self) -> None:
        event = PlatformEvent(platform="qq", kind=EventKind.NOTICE, type="group_upload")
        self.assertEqual(event.meta, {})
        self.assertEqual(event.raw, "")
        self.assertEqual(event.message_id, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
