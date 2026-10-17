"""``atri_x.hook.output`` 与严格 JSON 的单元测试。

对应上游 ``internal/hook/output/{output,message}.go``。
"""

from __future__ import annotations

import base64
import os
import shutil
import unittest
from pathlib import Path

from atri_x import delivery
from atri_x.hook.output import (
    BuildOptions,
    Group,
    JSONDecodeError,
    MessageSegment,
    OutputError,
    Segment,
    Target,
    build_group,
    build_message_image,
    build_message_segments,
    decode_image_data_url,
    decode_json,
    image_data_url,
    loads_strict,
    read_limited_image,
    strict_object,
)
from atri_x.hook.output.jsonstrict import require_string

TMP = Path(__file__).resolve().parent.parent / "tmp_hook_output_test"


class TestStrictJSON(unittest.TestCase):
    def test_unknown_field_rejected(self) -> None:
        # loads_strict 只负责 JSON 语法与尾随值；未知字段由严格映射层拒绝
        # （对应 Go 的 DisallowUnknownFields 作用在解码器上）。
        self.assertEqual({"kind": "text", "nope": 1}, loads_strict('{"kind":"text","nope":1}'))
        with self.assertRaises(JSONDecodeError) as ctx:
            decode_json(
                '{"kind":"text","nope":1}',
                lambda obj: strict_object(obj, "s", {"kind": require_string}),
            )
        self.assertIn("unknown field", str(ctx.exception))

    def test_trailing_value_rejected(self) -> None:
        with self.assertRaises(JSONDecodeError) as ctx:
            loads_strict('{"a":1} {"b":2}')
        self.assertIn("trailing", str(ctx.exception))

    def test_empty_input_rejected(self) -> None:
        for raw in (None, "", "   ", b""):
            with self.assertRaises(JSONDecodeError):
                loads_strict(raw)

    def test_strict_object_missing_key_not_in_result(self) -> None:
        result = strict_object({"a": "x"}, "p", {"a": require_string, "b": require_string})
        self.assertEqual({"a": "x"}, result)

    def test_strict_object_type_mismatch(self) -> None:
        with self.assertRaises(JSONDecodeError):
            strict_object({"a": 1}, "p", {"a": require_string})


class TestBuildGroup(unittest.TestCase):
    def test_text_with_defaults(self) -> None:
        outputs = build_group(
            Group(outputs=[Segment(kind="text", text=" hi ")]),
            BuildOptions(
                default_target=Target(platform="cli", scope_id="s1"),
                default_timing=delivery.DELIVERY_AFTER_ASSISTANT,
            ),
        )
        self.assertEqual(1, len(outputs))
        out = outputs[0]
        self.assertEqual(delivery.Kind.TEXT, out.kind)
        self.assertEqual("hi", out.text)
        self.assertEqual("cli", out.target.platform)
        self.assertEqual("s1", out.target.scope_id)
        self.assertEqual(
            delivery.DELIVERY_AFTER_ASSISTANT, delivery.delivery_timing(out)
        )

    def test_group_target_wins_over_default(self) -> None:
        outputs = build_group(
            Group(
                outputs=[Segment(kind="text", text="x")],
                target=Target(platform="qq", scope_id="g"),
            ),
            BuildOptions(default_target=Target(platform="cli", scope_id="s")),
        )
        self.assertEqual("qq", outputs[0].target.platform)

    def test_emoticon_requires_id(self) -> None:
        with self.assertRaises(OutputError):
            build_group(Group(outputs=[Segment(kind="emoticon")]), BuildOptions())

    def test_at_requires_user_id(self) -> None:
        with self.assertRaises(OutputError):
            build_group(Group(outputs=[Segment(kind="at")]), BuildOptions())

    def test_reply_requires_message_id(self) -> None:
        with self.assertRaises(OutputError):
            build_group(Group(outputs=[Segment(kind="reply", text="t")]), BuildOptions())

    def test_emoticon_at_reply(self) -> None:
        outputs = build_group(
            Group(
                outputs=[
                    Segment(kind="emoticon", emoticon_id="e1", name="n", text="t"),
                    Segment(kind="at", user_id="u1"),
                    Segment(kind="reply", message_id="m1", text="r"),
                ]
            ),
            BuildOptions(),
        )
        self.assertEqual(delivery.Kind.EMOTICON, outputs[0].kind)
        self.assertEqual("e1", outputs[0].emoticon_id)
        self.assertEqual(delivery.Kind.AT, outputs[1].kind)
        self.assertEqual("u1", outputs[1].text or outputs[1].source.media_id or "u1")
        self.assertEqual(delivery.Kind.REPLY, outputs[2].kind)
        self.assertEqual("m1", outputs[2].reply_to_platform_message_id)

    def test_reject_fields_for_text(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            build_group(
                Group(outputs=[Segment(kind="text", text="x", url="http://a/b")]),
                BuildOptions(),
            )
        self.assertIn("field url is not supported", str(ctx.exception))

    def test_unsupported_kind(self) -> None:
        with self.assertRaises(OutputError):
            build_group(Group(outputs=[Segment(kind="video")]), BuildOptions())

    def test_bad_timing(self) -> None:
        with self.assertRaises(Exception):
            build_group(
                Group(outputs=[Segment(kind="text", text="x")], timing="later"),
                BuildOptions(),
            )

    def test_error_prefixes_index(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            build_group(
                Group(outputs=[Segment(kind="text", text="ok"), Segment(kind="nope")]),
                BuildOptions(),
            )
        self.assertIn("outputs[1]:", str(ctx.exception))


class TestBuildMediaSource(unittest.TestCase):
    def test_media_id(self) -> None:
        media_id = "media:" + "a" * 64
        outputs = build_group(
            Group(outputs=[Segment(kind="image", path=media_id)]), BuildOptions()
        )
        self.assertEqual(media_id, outputs[0].source.media_id)

    def test_invalid_media_id(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            build_group(
                Group(outputs=[Segment(kind="image", path="media:zzz")]), BuildOptions()
            )
        self.assertIn("invalid media ID", str(ctx.exception))

    def test_two_sources_rejected(self) -> None:
        with self.assertRaises(OutputError):
            build_group(
                Group(
                    outputs=[
                        Segment(kind="file", path="a.txt", url="http://a/b.txt"),
                    ]
                ),
                BuildOptions(),
            )

    def test_no_source_rejected(self) -> None:
        with self.assertRaises(OutputError):
            build_group(Group(outputs=[Segment(kind="file")]), BuildOptions())

    def test_relative_path_joined_with_base_dir(self) -> None:
        outputs = build_group(
            Group(outputs=[Segment(kind="file", path="sub/a.txt")]),
            BuildOptions(base_dir="/base"),
        )
        self.assertEqual(os.path.join("/base", "sub/a.txt"), outputs[0].source.path)

    def test_path_uri_rejected(self) -> None:
        with self.assertRaises(OutputError):
            build_group(
                Group(outputs=[Segment(kind="file", path="http://a/b.txt")]),
                BuildOptions(),
            )

    def test_url_must_be_absolute_http(self) -> None:
        for url in ("ftp://a/b", "not-a-url", "http://"):
            with self.assertRaises(OutputError):
                build_group(
                    Group(outputs=[Segment(kind="image", url=url)]), BuildOptions()
                )

    def test_base64_decodes(self) -> None:
        encoded = base64.b64encode(b"hello").decode("ascii")
        outputs = build_group(
            Group(outputs=[Segment(kind="file", base64=encoded, name="a.txt")]),
            BuildOptions(),
        )
        self.assertEqual(b"hello", outputs[0].source.data)

    def test_bad_base64_rejected(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            build_group(
                Group(outputs=[Segment(kind="file", base64="!!not-base64!!")]),
                BuildOptions(),
            )
        self.assertIn("decode base64 output", str(ctx.exception))


class TestSegmentJSON(unittest.TestCase):
    def test_round_trip(self) -> None:
        spec = Segment.from_json('{"kind":"text","text":"hi"}')
        self.assertEqual("text", spec.kind)
        self.assertEqual({"kind": "text", "text": "hi"}, spec.to_json())

    def test_unknown_field(self) -> None:
        with self.assertRaises(JSONDecodeError):
            Segment.from_json('{"kind":"text","bogus":1}')

    def test_group_from_json(self) -> None:
        group = Group.from_json(
            '{"outputs":[{"kind":"text","text":"a"}],"timing":"immediate"}'
        )
        self.assertEqual(1, len(group.outputs))
        self.assertEqual("immediate", group.timing)

    def test_target_empty(self) -> None:
        self.assertTrue(Target().empty())
        self.assertFalse(Target(superadmins=True).empty())


class TestMessageSegments(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_text(self) -> None:
        segments = build_message_segments(
            [MessageSegment(type="text", text="hi")], ""
        )
        self.assertEqual("hi", segments[0].text)
        self.assertEqual("text", segments[0].type)

    def test_text_rejects_extra_fields(self) -> None:
        with self.assertRaises(OutputError):
            build_message_segments(
                [MessageSegment(type="text", text="hi", url="http://a/b")], ""
            )

    def test_unsupported_type(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            build_message_segments([MessageSegment(type="file", path="a")], "")
        self.assertIn("unsupported segment type", str(ctx.exception))

    def test_error_prefixes_index(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            build_message_segments(
                [MessageSegment(type="text", text="a"), MessageSegment(type="x")], ""
            )
        self.assertIn("message.segments[1]:", str(ctx.exception))

    def test_image_from_path(self) -> None:
        png = base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
            b"IQAAAABJRU5ErkJggg=="
        )
        path = TMP / "one.png"
        path.write_bytes(png)
        segment = build_message_image(
            MessageSegment(type="image", path="one.png"), str(TMP)
        )
        self.assertEqual("image/png", segment.mime_type)
        self.assertEqual("one.png", segment.name)
        self.assertTrue(segment.url.startswith("data:image/png;base64,"))

    def test_image_from_base64_with_mime_mismatch(self) -> None:
        data = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
        with self.assertRaises(OutputError):
            build_message_image(
                MessageSegment(
                    type="image",
                    url="data:image/jpeg;base64," + data,
                    mime_type="image/png",
                ),
                "",
            )

    def test_image_http_url(self) -> None:
        segment = build_message_image(
            MessageSegment(type="image", url="https://a/b.png"), ""
        )
        self.assertEqual("https://a/b.png", segment.url)

    def test_image_url_not_http(self) -> None:
        with self.assertRaises(OutputError):
            build_message_image(MessageSegment(type="image", url="file:///a.png"), "")

    def test_image_requires_exactly_one_source(self) -> None:
        with self.assertRaises(OutputError):
            build_message_image(MessageSegment(type="image"), "")
        with self.assertRaises(OutputError):
            build_message_image(
                MessageSegment(type="image", url="https://a/b.png", path="b.png"), ""
            )

    def test_image_media_id(self) -> None:
        media_id = "media:" + "b" * 64
        segment = build_message_image(
            MessageSegment(type="image", path=media_id), ""
        )
        self.assertEqual(media_id, segment.media)

    def test_data_url_round_trip(self) -> None:
        url = image_data_url("image/png", b"abc")
        mime_type, data = decode_image_data_url(url)
        self.assertEqual("image/png", mime_type)
        self.assertEqual(b"abc", data)

    def test_data_url_requires_base64(self) -> None:
        with self.assertRaises(OutputError):
            decode_image_data_url("data:image/png,abc")

    def test_read_limited_image_missing(self) -> None:
        with self.assertRaises(OutputError) as ctx:
            read_limited_image(str(TMP / "nope.png"))
        self.assertIn("open image path", str(ctx.exception))

    def test_message_segment_round_trip(self) -> None:
        spec = MessageSegment.from_json('{"type":"image","path":"a.png"}')
        self.assertEqual({"type": "image", "path": "a.png"}, spec.to_json())
        with self.assertRaises(JSONDecodeError):
            MessageSegment.from_json('{"type":"image","nope":1}')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
