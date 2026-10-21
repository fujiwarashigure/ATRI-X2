"""``atri_x.delivery`` 测试。

对应 Go：``internal/delivery/{delivery_test.go,tool_preview_test.go}``。
覆盖输出归一/降级、kind 集合与媒体源校验、投递时机拆分，
以及 ``ShouldDropGroupToolPreview`` 的三条件分支。
"""

from __future__ import annotations

import asyncio
import logging
import unittest

from atri_x.delivery import (
    DELIVERY_AFTER_ASSISTANT,
    DELIVERY_IMMEDIATE,
    META_DELIVERY_TIMING,
    META_HOOK_NAME,
    TOOL_PREVIEW_PREFIX,
    DeliveryError,
    Kind,
    Manager,
    Notice,
    Output,
    Receipt,
    SentMessage,
    Source,
    Target,
    at,
    delivery_timing,
    emoticon,
    fallback_output,
    fallback_text,
    file_path,
    image_path,
    is_http_media_source,
    is_tool_preview_notice,
    record_path,
    reply,
    should_drop_group_tool_preview,
    split_by_delivery_timing,
    text,
    use_temporary_connection,
    validate_delivery_timing,
    validate_outputs,
    validate_outputs_target,
    with_delivery_timing,
    with_temporary_connection,
)


class TestKindSet(unittest.TestCase):
    def test_kind_set_matches_go(self) -> None:
        self.assertEqual(
            Kind.ALL,
            ("text", "emoticon", "image", "file", "record", "at", "reply"),
        )

    def test_tool_preview_prefix_is_exact(self) -> None:
        self.assertEqual(TOOL_PREVIEW_PREFIX, "[tool] ")

    def test_is_http_media_source(self) -> None:
        self.assertTrue(is_http_media_source(" HTTPS://example.com/a.png "))
        self.assertTrue(is_http_media_source("http://example.com"))
        self.assertFalse(is_http_media_source("file:///a.png"))
        self.assertFalse(is_http_media_source(""))


class TestToolPreviewDetection(unittest.TestCase):
    """对应 Go ``tool_preview_test.go``。"""

    def test_recognizes_agent_preview_prefix(self) -> None:
        self.assertTrue(text("[tool] 正在调用 shell").is_tool_preview())
        self.assertTrue(Output(kind=Kind.TEXT, text="  [tool] 正在调用").is_tool_preview())

    def test_rejects_other_output(self) -> None:
        cases = {
            "plain reply": text("正在调用 shell"),
            "bracketed word": text("[tools] 正在调用"),
            "empty": text(""),
            "non-text kind": Output(kind=Kind.IMAGE, text="[tool] 图片"),
            "prefix only": text("[tool]"),
            "mention not tool": text("[toolbox] 内容"),
            "answer about tool": text("我用了 [tool] 前缀"),
        }
        for name, out in cases.items():
            with self.subTest(name=name):
                self.assertFalse(out.is_tool_preview())

    def test_notice_requires_exactly_one_text_output(self) -> None:
        preview = text("[tool] 正在调用 shell")
        self.assertTrue(is_tool_preview_notice([preview]))
        self.assertFalse(is_tool_preview_notice(None))
        self.assertFalse(is_tool_preview_notice([]))
        self.assertFalse(is_tool_preview_notice([preview, text("附加说明")]))
        self.assertFalse(is_tool_preview_notice([text("普通回答")]))

    def test_should_drop_requires_all_three_conditions(self) -> None:
        preview = text("[tool] 正在调用 shell")
        normal = text("普通回答")
        explicit = Target(private_user_id="10001")
        cases = [
            ("group current-conversation preview", Target(), [preview], True, True),
            ("private current-conversation preview", Target(), [preview], False, False),
            ("group current-conversation reply", Target(), [normal], True, False),
            ("explicit target keeps preview", explicit, [preview], True, False),
            ("no outputs", Target(), None, True, False),
            ("multi output notice", Target(), [preview, normal], True, False),
            ("superadmin target is explicit", Target(superadmins=True), [preview], True, False),
        ]
        for name, target, outputs, group_context, want in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    should_drop_group_tool_preview(target, outputs, group_context), want
                )

    def test_plain_group_notices_are_kept(self) -> None:
        self.assertFalse(should_drop_group_tool_preview(Target(), [text("普通回答")], True))

    def test_group_preview_switch_keeps_preview(self) -> None:
        """Python 侧开关：打开后群聊也投递工具进度预览。"""
        preview = text("[tool] 正在调用 shell")
        self.assertTrue(should_drop_group_tool_preview(Target(), [preview], True))
        self.assertFalse(
            should_drop_group_tool_preview(
                Target(), [preview], True, show_in_group=True
            )
        )
        # 开关只影响"群聊丢弃"这一条：私聊/显式目标本来就保留。
        self.assertFalse(
            should_drop_group_tool_preview(
                Target(), [preview], False, show_in_group=True
            )
        )


class TestConstructorsAndFallback(unittest.TestCase):
    def test_constructors_match_go(self) -> None:
        self.assertEqual(text("hi"), Output(kind=Kind.TEXT, text="hi"))
        self.assertEqual(image_path("a.png").source.path, "a.png")
        self.assertEqual(file_path("a.bin").kind, Kind.FILE)
        self.assertEqual(record_path("a.mp3").kind, Kind.RECORD)
        emoticon_out = emoticon(" 1 ", " 笑脸 ", " hi ")
        self.assertEqual(emoticon_out.emoticon_id, "1")
        self.assertEqual(emoticon_out.name, "笑脸")
        self.assertEqual(emoticon_out.text, "hi")
        self.assertEqual(emoticon_out.alt_text, "[表情: 笑脸]")
        self.assertEqual(emoticon("1", "", " 文本 ").alt_text, "文本")
        self.assertEqual(at(" u1 ").name, "u1")
        self.assertEqual(at("u1").alt_text, "@u1")
        self.assertEqual(at("").alt_text, "")
        self.assertEqual(reply(" m1 ", "hi").reply_to_platform_message_id, "m1")

    def test_fallback_text_per_kind(self) -> None:
        cases = [
            (text("hi"), "hi"),
            (Output(kind=Kind.TEXT, text="hi", alt_text="alt"), "alt"),
            (emoticon("1", "笑脸", ""), "[表情: 笑脸]"),
            # 构造器已经把 Text 写进 AltText，因此降级文本就是它本身。
            (emoticon("1", "", "文本"), "文本"),
            (emoticon("1", "", ""), ""),
            (Output(kind=Kind.EMOTICON, text="文本"), "[表情: 文本]"),
            (at("u1"), "@u1"),
            (at(""), ""),
            (reply("m1", "hi"), "[引用消息 m1]\nhi"),
            (reply("", "hi"), "hi"),
            (image_path("a.png"), "[图片: a.png]"),
            (image_path(""), "[图片]"),
            (Output(kind=Kind.IMAGE, source=Source(url="https://x/a.png")), "[图片: https://x/a.png]"),
            (file_path(""), "[文件]"),
            (record_path(""), "[语音]"),
            (Output(kind=Kind.IMAGE, name="图名", source=Source(path="a.png")), "[图片: 图名]"),
            (Output(kind="unknown", name="n"), "n"),
        ]
        for out, want in cases:
            with self.subTest(kind=out.kind, want=want):
                self.assertEqual(fallback_text(out), want)

    def test_fallback_output_joins_texts_and_drops_empty(self) -> None:
        out = fallback_output([text(" a "), text("  "), image_path(""), text("b")])
        self.assertEqual(out.kind, Kind.TEXT)
        self.assertEqual(out.text, "a\n[图片]\nb")
        self.assertEqual(fallback_output([]).text, "")
        self.assertEqual(fallback_output(None).text, "")


class TestValidation(unittest.TestCase):
    def test_text_output_cannot_have_media_source(self) -> None:
        with self.assertRaises(Exception) as ctx:
            validate_outputs([Output(kind=Kind.TEXT, text="x", source=Source(path="a.png"))])
        self.assertIn("outputs[0]", str(ctx.exception))

    def test_image_requires_exactly_one_source(self) -> None:
        validate_outputs([image_path("a.png")])
        with self.assertRaises(Exception):
            validate_outputs([Output(kind=Kind.IMAGE)])
        with self.assertRaises(Exception):
            validate_outputs(
                [Output(kind=Kind.IMAGE, source=Source(path="a.png", url="https://x/a.png"))]
            )

    def test_media_path_must_not_be_uri(self) -> None:
        with self.assertRaises(Exception) as ctx:
            validate_outputs([image_path("https://x/a.png")])
        self.assertIn("not a URI", str(ctx.exception))

    def test_media_url_must_be_absolute_http(self) -> None:
        validate_outputs([Output(kind=Kind.IMAGE, source=Source(url="https://x/a.png"))])
        for bad in ("file:///a.png", "https://", "/a.png", "ftp://x/a"):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    validate_outputs([Output(kind=Kind.IMAGE, source=Source(url=bad))])

    def test_data_source_counts_toward_source_count(self) -> None:
        validate_outputs([Output(kind=Kind.FILE, source=Source(data=b"x"))])

    def test_emoticon_needs_id_and_no_source(self) -> None:
        validate_outputs([emoticon("1", "笑脸", "")])
        with self.assertRaises(Exception):
            validate_outputs([Output(kind=Kind.EMOTICON, emoticon_id="", name="n")])
        with self.assertRaises(Exception):
            validate_outputs([Output(kind=Kind.EMOTICON, emoticon_id="1", source=Source(path="a"))])

    def test_at_and_reply_require_target_ids(self) -> None:
        validate_outputs([at("u1"), reply("m1", "hi")])
        with self.assertRaises(Exception):
            validate_outputs([Output(kind=Kind.AT)])
        with self.assertRaises(Exception):
            validate_outputs([Output(kind=Kind.REPLY)])

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            validate_outputs([Output(kind="video")])
        self.assertIn("unsupported output kind", str(ctx.exception))

    def test_batch_target_must_be_unique(self) -> None:
        target = Target(private_user_id="1")
        self.assertEqual(validate_outputs_target([text("a"), text("b")]), Target())
        self.assertEqual(
            validate_outputs_target([Output(kind=Kind.TEXT, target=target)]), target
        )
        with self.assertRaises(Exception):
            validate_outputs_target(
                [
                    Output(kind=Kind.TEXT, target=target),
                    Output(kind=Kind.TEXT, target=Target(private_user_id="2")),
                ]
            )

    def test_delivery_timing_roundtrip(self) -> None:
        out = text("hi")
        self.assertEqual(delivery_timing(out), DELIVERY_IMMEDIATE)
        deferred = with_delivery_timing(out, DELIVERY_AFTER_ASSISTANT)
        self.assertEqual(delivery_timing(deferred), DELIVERY_AFTER_ASSISTANT)
        self.assertEqual(deferred.meta, {META_DELIVERY_TIMING: DELIVERY_AFTER_ASSISTANT})
        # 原对象不被就地修改（Go 的值语义）
        self.assertIsNone(out.meta)
        self.assertEqual(with_delivery_timing(out, ""), out)
        self.assertEqual(with_delivery_timing(out, DELIVERY_IMMEDIATE), out)
        validate_delivery_timing("")
        validate_delivery_timing(DELIVERY_AFTER_ASSISTANT)
        with self.assertRaises(Exception):
            validate_delivery_timing("later")

    def test_split_by_delivery_timing(self) -> None:
        out = text("hi")
        deferred = with_delivery_timing(text("later"), DELIVERY_AFTER_ASSISTANT)
        immediate, after = split_by_delivery_timing([out, deferred])
        self.assertEqual(immediate, [out])
        self.assertEqual(after, [deferred])
        self.assertEqual(split_by_delivery_timing([]), ([], []))

    def test_invalid_timing_in_batch_is_reported(self) -> None:
        bad = Output(kind=Kind.TEXT, text="x", meta={META_DELIVERY_TIMING: "later"})
        with self.assertRaises(Exception) as ctx:
            validate_outputs([bad])
        self.assertIn("outputs[0]", str(ctx.exception))


class TestReceipt(unittest.TestCase):
    def test_mark_partial_failure_keeps_ids_and_is_copy(self) -> None:
        receipt = Receipt(platform_message_ids=["m1"])
        marked = receipt.mark_partial_failure(RuntimeError("boom"))
        self.assertTrue(marked.failed)
        self.assertEqual(marked.failure, "boom")
        self.assertEqual(marked.platform_message_ids, ["m1"])
        self.assertFalse(receipt.failed)
        self.assertEqual(receipt.failure, "")

    def test_mark_partial_failure_keeps_first_reason(self) -> None:
        receipt = Receipt(failed=True, failure="first")
        self.assertEqual(receipt.mark_partial_failure(RuntimeError("second")).failure, "first")
        self.assertEqual(receipt.mark_partial_failure(None).failure, "first")

    def test_merge_appends_and_preserves_failure(self) -> None:
        left = Receipt(platform_message_ids=["a"], sent_messages=[SentMessage(platform_message_id="a")])
        right = Receipt(
            platform_message_ids=["b"],
            sent_messages=[SentMessage(platform_message_id="b")],
            failed=True,
            failure="inner",
        )
        merged = left.merge(right)
        self.assertEqual(merged.platform_message_ids, ["a", "b"])
        self.assertEqual(len(merged.sent_messages), 2)
        self.assertTrue(merged.failed)
        self.assertEqual(merged.failure, "inner")
        self.assertEqual(left.platform_message_ids, ["a"])
        self.assertFalse(left.failed)


class FakeSender:
    """记录发送调用的假适配器。"""

    def __init__(self, chat_error=None, notice_error=None, receipt=None):
        self.chat_calls = []
        self.notice_calls = []
        self.chat_error = chat_error
        self.notice_error = notice_error
        self.receipt = receipt or Receipt(platform_message_ids=["m1"])

    async def send_chat(self, ctx, outputs):
        self.chat_calls.append(list(outputs))
        if self.chat_error is not None:
            raise self.chat_error
        return self.receipt

    async def send_notice(self, ctx, notice):
        self.notice_calls.append(notice)
        if self.notice_error is not None:
            raise self.notice_error
        return self.receipt


class TestManager(unittest.IsolatedAsyncioTestCase):
    async def test_send_chat_validates_before_sending(self) -> None:
        sender = FakeSender()
        manager = Manager(sender=sender)
        with self.assertRaises(Exception):
            await manager.send_chat(None, [Output(kind=Kind.IMAGE)])
        self.assertEqual(sender.chat_calls, [])

    async def test_send_chat_requires_sender(self) -> None:
        with self.assertRaises(Exception) as ctx:
            await Manager().send_chat(None, [text("hi")])
        self.assertIn("sender is not configured", str(ctx.exception))

    async def test_send_chat_returns_receipt(self) -> None:
        sender = FakeSender()
        receipt = await Manager(sender=sender).send_chat(None, [text("hi")])
        self.assertEqual(receipt.platform_message_ids, ["m1"])
        self.assertEqual(len(sender.chat_calls), 1)

    async def test_send_chat_failure_carries_partial_receipt(self) -> None:
        partial = Receipt(platform_message_ids=["sent"])
        # 适配器用 DeliveryError 汇报"已投递部分 + 失败"（对应 Go 的 (receipt, err)）。
        sender = FakeSender(chat_error=DeliveryError("boom", partial))
        with self.assertRaises(DeliveryError) as ctx:
            await Manager(sender=sender).send_chat(None, [text("hi")])
        self.assertEqual(ctx.exception.receipt.platform_message_ids, ["sent"])
        self.assertTrue(ctx.exception.receipt.failed)
        self.assertEqual(ctx.exception.receipt.failure, "boom")

    async def test_send_chat_plain_error_marks_empty_receipt(self) -> None:
        sender = FakeSender(chat_error=RuntimeError("boom"))
        with self.assertRaises(DeliveryError) as ctx:
            await Manager(sender=sender).send_chat(None, [text("hi")])
        self.assertEqual(ctx.exception.receipt.platform_message_ids, [])
        self.assertTrue(ctx.exception.receipt.failed)
        self.assertEqual(ctx.exception.receipt.failure, "boom")

    async def test_send_notice_empty_outputs_is_noop(self) -> None:
        sender = FakeSender()
        receipt = await Manager(sender=sender).send_notice(None, Notice())
        self.assertEqual(receipt.platform_message_ids, [])
        self.assertEqual(sender.notice_calls, [])

    async def test_send_notice_fills_target_from_outputs(self) -> None:
        sender = FakeSender()
        target = Target(platform="qq", group_id="1")
        notice = Notice(outputs=[Output(kind=Kind.TEXT, text="hi", target=target)])
        await Manager(sender=sender).send_notice(None, notice)
        self.assertEqual(sender.notice_calls[0].target, target)

    async def test_send_notice_keeps_explicit_target(self) -> None:
        sender = FakeSender()
        explicit = Target(platform="cli", scope_id="s")
        notice = Notice(
            target=explicit,
            outputs=[Output(kind=Kind.TEXT, text="hi", target=Target(private_user_id="1"))],
        )
        await Manager(sender=sender).send_notice(None, notice)
        self.assertEqual(sender.notice_calls[0].target, explicit)

    async def test_send_notice_wraps_hook_source_error(self) -> None:
        sender = FakeSender(notice_error=RuntimeError("boom"))
        logger = logging.getLogger("test.delivery")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        manager = Manager(sender=sender, logger=logger)
        out = Output(kind=Kind.TEXT, text="hi", meta={META_HOOK_NAME: "myhook"})
        with self.assertRaises(DeliveryError) as ctx:
            await manager.send_notice(None, Notice(outputs=[out]))
        self.assertIn("hook myhook output", str(ctx.exception))
        self.assertTrue(ctx.exception.receipt.failed)

    async def test_send_notices_with_receipt_delegates(self) -> None:
        sender = FakeSender()
        receipt = await Manager(sender=sender).send_notices_with_receipt(None, [text("hi")])
        self.assertEqual(receipt.platform_message_ids, ["m1"])
        await Manager(sender=sender).send_notices(None, [text("hi")])
        self.assertEqual(len(sender.notice_calls), 2)


class TestTemporaryConnection(unittest.TestCase):
    def test_flag_roundtrip_on_dict_and_token(self) -> None:
        ctx = with_temporary_connection(None)
        self.assertTrue(use_temporary_connection(ctx))
        self.assertFalse(use_temporary_connection(None))
        self.assertFalse(use_temporary_connection({}))

        class Token:
            def __init__(self, parent=None):
                self._values = {}
                self._parent = parent

            def with_value(self, key, value):
                child = Token(self)
                child._values = {key: value}
                return child

            def value(self, key, default=None):
                node = self
                while node is not None:
                    if key in node._values:
                        return node._values[key]
                    node = node._parent
                return default

        self.assertTrue(use_temporary_connection(with_temporary_connection(Token())))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
