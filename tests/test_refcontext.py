"""``atri_x.platform.refcontext`` 的单元测试。

对应上游 ``internal/platform/refcontext/refcontext.go``。

用轻量 fake 实现 ``storage`` 的 repository 形状（refcontext 全部按鸭子类型消费）。
重点覆盖三条上游承诺的语义：

1. 自己 Session 的**最后一条 assistant → Resume**（不重复注入引用内容）；
2. **较早的 assistant → Fork**（并保留引用媒体）；
3. 超管回复**后台 Session**（``cron:`` / ``atrinis:`` 前缀）→ Resume。
"""

from __future__ import annotations

import unittest

from atri_x.platform import refcontext as rc
from atri_x.platform.platform import MessageSegment, MessageSegmentType, ReplyContext
from atri_x.storage.models import ChatMessage, HistoryMedia, Message, Media, MediaOutput, Session


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeMessages:
    def __init__(self, by_platform: dict[tuple[str, str, str], Message],
                 by_session: dict[str, list[Message]] | None = None) -> None:
        self._by_platform = by_platform
        self._by_session = by_session or {}

    def find_by_platform_message(self, platform: str, scope_id: str, message_id: str) -> Message:
        key = (platform.strip(), scope_id.strip(), message_id)
        if key not in self._by_platform:
            raise LookupError("not found")
        return self._by_platform[key]

    def list_by_session(self, session_id: str) -> list[Message]:
        if session_id not in self._by_session:
            raise LookupError("not found")
        return self._by_session[session_id]


class FakeSessions:
    def __init__(self, sessions: dict[str, Session]) -> None:
        self._sessions = sessions

    def get(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise LookupError("not found")
        return self._sessions[session_id]


class FakeMedia:
    def __init__(self, outputs=None, history=None, items=None) -> None:
        self._outputs = outputs or {}
        self._history = history or {}
        self._items = items or {}

    def find_outputs(self, platform: str, scope_id: str, message_id: str, now):
        return self._outputs.get((platform.strip(), scope_id.strip(), message_id), [])

    def find_history(self, platform: str, scope_id: str, message_id: str):
        return self._history.get((platform.strip(), scope_id.strip(), message_id), [])

    def get(self, media_id: str):
        if media_id not in self._items:
            raise LookupError("not found")
        return self._items[media_id]


class FakeStore:
    def __init__(self, messages, sessions=None, media=None) -> None:
        self._messages = messages
        self._sessions = sessions
        self._media = media

    def messages(self):
        return self._messages

    def sessions(self):
        return self._sessions

    def media(self):
        return self._media


class FakeChatHistory:
    def __init__(self, rows: dict[tuple[str, str, str], ChatMessage]) -> None:
        self._rows = rows

    def get_by_platform_message(self, platform: str, scope_id: str, message_id: str):
        row = self._rows.get((platform.strip(), scope_id.strip(), message_id))
        if row is None:
            raise LookupError("not found")
        return row


def make_message(**kw) -> Message:
    kw.setdefault("id", "m1")
    kw.setdefault("session_id", "s1")
    kw.setdefault("role", "assistant")
    kw.setdefault("content", "bot 说过的话")
    return Message(**kw)  # type: ignore[arg-type]


def make_session(**kw) -> Session:
    kw.setdefault("id", "s1")
    kw.setdefault("owner_id", "cli:local")
    kw.setdefault("platform", "cli")
    kw.setdefault("platform_scope_id", "private:local")
    return Session(**kw)  # type: ignore[arg-type]


def base_opts(**kw) -> rc.Options:
    kw.setdefault("platform", "cli")
    kw.setdefault("scope_id", "private:local")
    kw.setdefault("actor_id", "cli:local")
    kw.setdefault("reply_id", "r1")
    kw.setdefault("text", "继续")
    return rc.Options(**kw)  # type: ignore[arg-type]


class TestNoReply(unittest.TestCase):
    def test_no_reply_id_returns_text_unchanged(self) -> None:
        result = rc.apply(base_opts(reply_id="   "))
        self.assertEqual("继续", result.text)
        self.assertEqual("", result.reply.message_id)
        self.assertEqual("", result.fork_from_message_id)
        self.assertEqual("", result.resume_session_id)

    def test_reply_id_recorded(self) -> None:
        result = rc.apply(base_opts(reply_id=" r1 "))
        self.assertEqual("r1", result.reply.message_id)


class TestCommandPath(unittest.TestCase):
    def test_fork_command_rewritten_to_stored_id(self) -> None:
        stored = make_message(id="m42", role="assistant")
        store = FakeStore(FakeMessages({("cli", "private:local", "r1"): stored}))
        result = rc.apply(
            base_opts(store=store, text="/fork", command_prefixes=["/"])
        )
        self.assertEqual("/fork m42", result.text)
        self.assertEqual("", result.fork_from_message_id)
        self.assertEqual("", result.resume_session_id)

    def test_other_command_untouched(self) -> None:
        stored = make_message(id="m42", role="assistant")
        store = FakeStore(FakeMessages({("cli", "private:local", "r1"): stored}))
        result = rc.apply(base_opts(store=store, text="/new", command_prefixes=["/"]))
        self.assertEqual("/new", result.text)

    def test_fork_on_non_assistant_untouched(self) -> None:
        stored = make_message(id="m42", role="user")
        store = FakeStore(FakeMessages({("cli", "private:local", "r1"): stored}))
        result = rc.apply(base_opts(store=store, text="/fork", command_prefixes=["/"]))
        self.assertEqual("/fork", result.text)

    def test_command_prefix_uses_first_prefix(self) -> None:
        stored = make_message(id="m7", role="assistant")
        store = FakeStore(FakeMessages({("cli", "private:local", "r1"): stored}))
        result = rc.apply(
            base_opts(store=store, text="!fork", command_prefixes=["!", "/"])
        )
        self.assertEqual("!fork m7", result.text)


class TestResumeAndFork(unittest.TestCase):
    def _store(self, *, latest: bool, session: Session | None = None,
               messages: list[Message] | None = None) -> FakeStore:
        stored = make_message(id="m2", role="assistant", content="第二句")
        session = session or make_session()
        rows = messages if messages is not None else [
            make_message(id="m1", role="assistant", content="第一句"),
            stored,
        ]
        return FakeStore(
            FakeMessages({("cli", "private:local", "r1"): stored}, {"s1": rows}),
            FakeSessions({"s1": session}),
        )

    def test_latest_assistant_resumes(self) -> None:
        result = rc.apply(base_opts(store=self._store(latest=True)))
        self.assertEqual("s1", result.resume_session_id)
        self.assertEqual("", result.fork_from_message_id)
        # Resume 路径不注入引用段，只带 reply id
        self.assertEqual([], result.reference_segments)
        self.assertEqual("r1", result.reply.message_id)
        self.assertEqual("继续", result.text)

    def test_earlier_assistant_forks(self) -> None:
        stored = make_message(id="m1", role="assistant", content="第一句")
        rows = [stored, make_message(id="m2", role="assistant", content="第二句")]
        store = FakeStore(
            FakeMessages({("cli", "private:local", "r1"): stored}, {"s1": rows}),
            FakeSessions({"s1": make_session()}),
        )
        result = rc.apply(base_opts(store=store))
        self.assertEqual("m1", result.fork_from_message_id)
        self.assertEqual("", result.resume_session_id)
        # 关键：Fork 路径**不改写用户文本**（Go 里 `_, segments, reply = fallback...`
        # 把文本丢掉了），引用内容走 reply/reference_segments 传递。
        self.assertEqual("继续", result.text)
        self.assertEqual("第一句", result.reply.text)
        self.assertEqual([MessageSegmentType.TEXT], [s.type for s in result.reply.segments])

    def test_superadmin_background_session_resumes(self) -> None:
        stored = make_message(id="m1", role="assistant", content="cron 报告")
        rows = [stored, make_message(id="m2", role="assistant", content="后续")]
        store = FakeStore(
            FakeMessages({("cli", "private:local", "r1"): stored}, {"s1": rows}),
            FakeSessions({"s1": make_session(platform_scope_id="cron:daily")}),
        )
        result = rc.apply(base_opts(store=store, is_superadmin=True))
        self.assertEqual("s1", result.resume_session_id)
        self.assertEqual("", result.fork_from_message_id)

    def test_background_session_not_resumed_for_normal_user(self) -> None:
        stored = make_message(id="m1", role="assistant", content="cron 报告")
        rows = [stored, make_message(id="m2", role="assistant", content="后续")]
        store = FakeStore(
            FakeMessages({("cli", "private:local", "r1"): stored}, {"s1": rows}),
            FakeSessions({"s1": make_session(platform_scope_id="atrinis:x")}),
        )
        result = rc.apply(base_opts(store=store, is_superadmin=False))
        # 既不是自己的 session（后台），也不是超管回复后台 -> 走兜底
        self.assertEqual("", result.resume_session_id)
        self.assertEqual("", result.fork_from_message_id)

    def test_other_actors_session_falls_back(self) -> None:
        result = rc.apply(base_opts(store=self._store(latest=True), actor_id="cli:someone"))
        self.assertEqual("", result.resume_session_id)
        self.assertEqual("", result.fork_from_message_id)

    def test_other_platform_session_falls_back(self) -> None:
        result = rc.apply(base_opts(store=self._store(latest=True), platform="telegram"))
        self.assertEqual("", result.resume_session_id)

    def test_user_message_falls_back_not_fork(self) -> None:
        stored = make_message(id="m1", role="user", content="用户原话")
        store = FakeStore(FakeMessages({("cli", "private:local", "r1"): stored}))
        result = rc.apply(base_opts(store=store))
        self.assertEqual("", result.fork_from_message_id)
        self.assertEqual("", result.resume_session_id)
        self.assertIn("用户原话", result.text)


class TestFallbackReferenceText(unittest.TestCase):
    def test_stored_content_injected(self) -> None:
        stored = make_message(id="m1", role="assistant", content="历史内容")
        store = FakeStore(FakeMessages({("cli", "private:local", "r1"): stored}))
        result = rc.apply(base_opts(store=store, text="现在"))
        # 标签 "引用：bot" 会被折叠成发送者名 "bot"，不会逐字出现在文本里
        self.assertIn("[引用#r1", result.text)
        self.assertIn("bot", result.text)
        self.assertIn("历史内容", result.text)
        self.assertTrue(result.text.endswith("现在"))

    def test_outputs_win_over_chat_history(self) -> None:
        media = FakeMedia(
            outputs={
                ("cli", "private:local", "r1"): [
                    MediaOutput(media_id="img1", kind="image"),
                    MediaOutput(media_id="file1", kind="file"),
                ]
            }
        )
        store = FakeStore(FakeMessages({}), media=media)
        result = rc.apply(base_opts(store=store))
        self.assertIn("引用#r1：bot", result.text)
        self.assertEqual(
            [MessageSegmentType.IMAGE, MessageSegmentType.FILE],
            [s.type for s in result.reference_segments],
        )

    def test_chat_history_used_when_no_outputs(self) -> None:
        row = ChatMessage(
            id="h1",
            platform="cli",
            platform_scope_id="private:local",
            platform_message_id="r1",
            sender_id="u1",
            sender_name="小明",
            text="历史文本",
        )
        history = FakeChatHistory({("cli", "private:local", "r1"): row})
        result = rc.apply(base_opts(chat_history=history))
        # 有 sender_id 时头部是 "名字(平台ID类:id)"
        self.assertIn("小明(cli:u1)", result.text)
        self.assertIn("历史文本", result.text)
        self.assertEqual("u1", result.reply.sender_id)
        self.assertEqual("小明", result.reply.sender_name)

    def test_fetch_is_last_resort(self) -> None:
        def fetch(ctx, reply_id):
            return rc.ReferencedMessage(
                sender_id="9", sender_name="平台", label="引用：平台", text="平台文本"
            ), True

        result = rc.apply(base_opts(fetch=fetch))
        self.assertIn("平台文本", result.text)
        self.assertEqual("9", result.reply.sender_id)

    def test_media_only_reference_gets_placeholder(self) -> None:
        def fetch(ctx, reply_id):
            return rc.ReferencedMessage(
                label="引用",
                segments=[MessageSegment(type=MessageSegmentType.IMAGE, media_id="img1")],
            ), True

        result = rc.apply(base_opts(fetch=fetch))
        self.assertIn(rc.MEDIA_ONLY_TEXT, result.text)

    def test_nothing_found_returns_original_text(self) -> None:
        result = rc.apply(base_opts(text="原样"))
        self.assertEqual("原样", result.text)
        self.assertEqual("r1", result.reply.message_id)

    def test_sender_name_derived_from_label(self) -> None:
        def fetch(ctx, reply_id):
            return rc.ReferencedMessage(label="引用：小红", text="x"), True

        result = rc.apply(base_opts(fetch=fetch))
        self.assertEqual("小红", result.reply.sender_name)


class TestRestoreHistoryMedia(unittest.TestCase):
    def _opts(self, *, kind: str = "image", deleting: bool = False) -> rc.Options:
        row = ChatMessage(
            id="h1",
            platform="cli",
            platform_scope_id="private:local",
            platform_message_id="r1",
            segments='[{"type":"image","media":"old"},{"type":"file","media":"f-old"}]',
        )

        other = HistoryMedia(
            history_id="h-other", media_index=1, kind="image", media_id="wrong"
        )
        first = HistoryMedia(history_id="h1", media_index=1, kind="image", media_id="img-new")
        second = HistoryMedia(history_id="h1", media_index=2, kind="file", media_id="file-new")
        media = FakeMedia(
            history={("cli", "private:local", "r1"): [other, first, second]},
            items={
                "img-new": Media(id="img-new", deleting=deleting),
                "file-new": Media(id="file-new"),
            },
        )
        store = FakeStore(FakeMessages({}), media=media)
        return rc.Options(
            store=store,
            chat_history=FakeChatHistory({("cli", "private:local", "r1"): row}),
            platform="cli",
            scope_id="private:local",
            reply_id="r1",
        )

    def test_media_ids_restored_by_index(self) -> None:
        result = rc.apply(self._opts())
        ids = [s.media_id for s in result.reference_segments]
        self.assertEqual(["img-new", "file-new"], ids)

    def test_deleting_media_not_restored(self) -> None:
        result = rc.apply(self._opts(deleting=True))
        # 第一段保持原样（未替换），第二段正常恢复
        self.assertEqual(["old", "file-new"], [s.media_id for s in result.reference_segments])

    def test_other_history_id_is_ignored(self) -> None:
        result = rc.apply(self._opts())
        self.assertNotIn("wrong", [s.media_id for s in result.reference_segments])


class TestFormatReferenceText(unittest.TestCase):
    def test_basic(self) -> None:
        reply = ReplyContext(message_id="m1", sender_name="小明", text="你好")
        self.assertEqual(
            "[引用#m1：小明:你好]\n\n现在",
            rc.format_reference_text("cli", reply, "现在"),
        )

    def test_platform_id_kinds(self) -> None:
        for platform, kind in (
            ("qqonebot", "qq"),
            ("telegram", "tg"),
            ("qqofficial", "openid"),
            ("slack", "slack"),
            ("", "id"),
        ):
            with self.subTest(platform=platform):
                reply = ReplyContext(message_id="m", sender_id="123")
                out = rc.format_reference_text(platform, reply, "")
                self.assertIn(f"{kind}:123", out)

    def test_name_with_id(self) -> None:
        reply = ReplyContext(message_id="m", sender_id="123", sender_name="小明")
        self.assertIn("小明(tg:123)", rc.format_reference_text("telegram", reply, ""))

    def test_empty_current_text_has_no_trailing_block(self) -> None:
        reply = ReplyContext(message_id="m", text="内容")
        self.assertEqual("[引用#m:内容]", rc.format_reference_text("cli", reply, "   "))


class TestChatMetadataRoundTrip(unittest.TestCase):
    def test_round_trip(self) -> None:
        reply = ReplyContext(message_id="m1", sender_id="u1", sender_name="小明", text="文本")
        raw = rc.marshal_chat_metadata(reply)
        self.assertIn("\"reply\"", raw)
        # 空字段必须被省略（对齐 Go 的 omitempty）
        self.assertNotIn("sender_id\"", rc.marshal_chat_metadata(ReplyContext(message_id="m")))
        row = ChatMessage(id="h1", reply_to_platform_message_id="m1", metadata=raw)
        back = rc.chat_message_reply(row)
        self.assertEqual("m1", back.message_id)
        self.assertEqual("u1", back.sender_id)
        self.assertEqual("小明", back.sender_name)
        self.assertEqual("文本", back.text)

    def test_empty_message_id_gives_empty_string(self) -> None:
        self.assertEqual("", rc.marshal_chat_metadata(ReplyContext()))

    def test_bad_json_degrades(self) -> None:
        row = ChatMessage(id="h", reply_to_platform_message_id="m", metadata="{oops")
        back = rc.chat_message_reply(row)
        self.assertEqual("m", back.message_id)
        self.assertEqual("", back.sender_id)

    def test_missing_reply_key(self) -> None:
        row = ChatMessage(id="h", reply_to_platform_message_id="m", metadata='{"x":1}')
        self.assertEqual("", rc.chat_message_reply(row).sender_id)


class TestHelpers(unittest.TestCase):
    def test_reference_sender_name(self) -> None:
        self.assertEqual("", rc._reference_sender_name("引用"))
        self.assertEqual("小明", rc._reference_sender_name("引用：小明"))
        self.assertEqual("其他", rc._reference_sender_name("其他"))

    def test_go_aliases_exist(self) -> None:
        self.assertIs(rc.Apply, rc.apply)
        self.assertIs(rc.FormatReferenceText, rc.format_reference_text)
        self.assertIs(rc.MarshalChatMetadata, rc.marshal_chat_metadata)
        self.assertIs(rc.ChatMessageReply, rc.chat_message_reply)


if __name__ == "__main__":
    unittest.main()
