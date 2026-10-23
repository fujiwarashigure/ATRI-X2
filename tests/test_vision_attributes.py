"""两段式识图第一段：属性模板、解析/限幅，以及图片段的 ``detail`` 落线。

这里覆盖三件事：

* 新的「画面属性描述」模板存在、版本独立、并明确禁止猜作品/角色/画师；
* :func:`parse_image_attributes` 的宽容解析与限幅（缺字段不算失败，全空才算）；
* ``MessageSegment.detail`` 从落库 JSON 一路带到 chat-completions 与 Responses
  的请求体，空值时**不写**该键（与 Go 落库键兼容）。
"""

from __future__ import annotations

import json
import unittest

from atri_x.errors import ImageAnalysisEmptyError
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText
from atri_x.llm.openai.responses import to_responses_content
from atri_x.llm.openai.wire import openai_image_url, to_openai_content
from atri_x.vision import (
    IDENTITY_CONFIDENCE_HIGH,
    IDENTITY_CONFIDENCE_LOW,
    MAX_ATTRIBUTE_FIELD_RUNES,
    AttributeDescriptionTemplateVersion,
    ChatDescriptionTemplateVersion,
    ImageAnalysisTemplateVersion,
    ImagePromptTemplateVersion,
    analyze_attributes_text,
    attributes_summary,
    image_attributes_prompt,
    normalize_identity_confidence,
    parse_image_attributes,
)


class TestAttributesTemplate(unittest.TestCase):
    def test_version_is_distinct_from_existing_templates(self) -> None:
        versions = {
            AttributeDescriptionTemplateVersion,
            ChatDescriptionTemplateVersion,
            ImageAnalysisTemplateVersion,
            ImagePromptTemplateVersion,
        }
        self.assertEqual(len(versions), 4)
        self.assertTrue(AttributeDescriptionTemplateVersion.startswith("chat-image-"))

    def test_prompt_forbids_guessing_identity(self) -> None:
        prompt = image_attributes_prompt("zh")
        self.assertEqual(prompt.version, AttributeDescriptionTemplateVersion)
        for required in (
            "hair_color",
            "eye_color",
            "hairstyle",
            "outfit_colors",
            "trim",
            "badges",
            "accessories",
            "visible_text",
            "art_style",
            "ai_generated",
            "scene",
            "character_count",
            "identity_confidence",
            '"description"',
        ):
            self.assertIn(required, prompt.system)
        self.assertIn("不要猜测作品名、角色名、画师名", prompt.system)
        self.assertIn("JSON", prompt.system)
        self.assertIn("JSON", prompt.user)

    def test_english_instruction(self) -> None:
        prompt = image_attributes_prompt("en")
        self.assertIn("as JSON", prompt.user)

    def test_language_aliasing(self) -> None:
        # 未识别的语言一律折叠成 zh，与既有模板一致。
        self.assertEqual(image_attributes_prompt("").user, image_attributes_prompt("zh").user)


class TestParseAttributes(unittest.TestCase):
    def test_parses_prose_wrapped_object(self) -> None:
        payload = {
            "hair_color": "黑色",
            "eye_color": "琥珀色",
            "hairstyle": "长直发",
            "outfit_colors": "黑白配色，红色滚边",
            "trim": "蕾丝袖口",
            "badges": "胸前蓝色徽章",
            "accessories": "贝雷帽",
            "visible_text": "ATRI",
            "art_style": "赛璐璐",
            "ai_generated": True,
            "scene": "半身像，室内窗边",
            "character_count": 1,
            "identity_confidence": "low",
            "description": "黑发琥珀瞳的长发少女，黑白配红滚边制服。",
        }
        text = "好的，结果如下：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        out = parse_image_attributes(text)
        self.assertEqual(out.hair_color, "黑色")
        self.assertEqual(out.eye_color, "琥珀色")
        self.assertTrue(out.ai_generated)
        self.assertEqual(out.character_count, 1)
        self.assertEqual(out.identity_confidence, IDENTITY_CONFIDENCE_LOW)
        self.assertIn("黑发琥珀瞳", out.description)

    def test_truncates_long_fields(self) -> None:
        payload = {"hair_color": "长" * (MAX_ATTRIBUTE_FIELD_RUNES + 50), "description": "x"}
        out = parse_image_attributes(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(len(out.hair_color), MAX_ATTRIBUTE_FIELD_RUNES)

    def test_missing_fields_are_not_a_failure(self) -> None:
        out = parse_image_attributes('{"hair_color": "银白"}')
        self.assertEqual(out.hair_color, "银白")
        self.assertEqual(out.eye_color, "")
        self.assertEqual(out.identity_confidence, IDENTITY_CONFIDENCE_LOW)
        self.assertFalse(out.ai_generated)

    def test_lenient_bool_and_count(self) -> None:
        out = parse_image_attributes(
            '{"description": "d", "ai_generated": "true", "character_count": "3"}'
        )
        self.assertTrue(out.ai_generated)
        self.assertEqual(out.character_count, 3)
        out = parse_image_attributes(
            '{"description": "d", "ai_generated": false, "character_count": -2}'
        )
        self.assertFalse(out.ai_generated)
        self.assertEqual(out.character_count, 0)

    def test_type_mismatch_fields_are_ignored(self) -> None:
        out = parse_image_attributes(
            '{"hair_color": {"a": 1}, "description": "d", "character_count": "很多"}'
        )
        self.assertEqual(out.hair_color, "")
        self.assertEqual(out.character_count, 0)

    def test_all_empty_is_empty_result(self) -> None:
        with self.assertRaises(ImageAnalysisEmptyError):
            parse_image_attributes('{"hair_color": "", "description": "  "}')

    def test_missing_object_is_empty_result(self) -> None:
        with self.assertRaises(ImageAnalysisEmptyError):
            parse_image_attributes("画面里是一个女孩")

    def test_broken_json_raises_decode_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_attributes('{"description": "x",}')

    def test_analyze_attributes_text_never_raises(self) -> None:
        attributes, failed = analyze_attributes_text("no json here")
        self.assertIsNone(attributes)
        self.assertTrue(failed)
        attributes, failed = analyze_attributes_text('{"description": "d"}')
        self.assertIsNotNone(attributes)
        self.assertFalse(failed)

    def test_identity_confidence_folding(self) -> None:
        self.assertEqual(normalize_identity_confidence("HIGH"), IDENTITY_CONFIDENCE_HIGH)
        self.assertEqual(normalize_identity_confidence("medium"), "medium")
        self.assertEqual(normalize_identity_confidence("unknown"), IDENTITY_CONFIDENCE_LOW)
        self.assertEqual(normalize_identity_confidence(""), IDENTITY_CONFIDENCE_LOW)


class TestAttributesSummary(unittest.TestCase):
    def test_skips_empty_fields_and_keeps_order(self) -> None:
        out = parse_image_attributes(
            json.dumps(
                {
                    "hair_color": "黑色",
                    "outfit_colors": "黑白",
                    "character_count": 2,
                    "ai_generated": True,
                    "description": "d",
                },
                ensure_ascii=False,
            )
        )
        summary = attributes_summary(out)
        self.assertIn("发色=黑色", summary)
        self.assertIn("服装配色=黑白", summary)
        self.assertIn("人物数量=2", summary)
        self.assertIn("疑似 AI 生成", summary)
        self.assertNotIn("瞳色", summary)

    def test_empty_attributes_summary(self) -> None:
        self.assertEqual(attributes_summary(None), "")


class TestSegmentDetail(unittest.TestCase):
    def test_to_json_omits_empty_detail(self) -> None:
        segment = MessageSegment(type=SegmentImage, url="http://x/1.png")
        self.assertNotIn("detail", segment.to_json())

    def test_to_json_includes_non_empty_detail(self) -> None:
        segment = MessageSegment(type=SegmentImage, url="http://x/1.png", detail="original")
        self.assertEqual(segment.to_json()["detail"], "original")

    def test_from_json_roundtrip(self) -> None:
        raw = {"type": SegmentImage, "url": "http://x/1.png", "detail": "high"}
        segment = MessageSegment.from_json(raw)
        self.assertEqual(segment.detail, "high")
        self.assertEqual(segment.to_json(), raw)

    def test_from_json_defaults_missing_detail_to_empty(self) -> None:
        segment = MessageSegment.from_json({"type": SegmentImage, "url": "u"})
        self.assertEqual(segment.detail, "")

    def test_clone_keeps_detail(self) -> None:
        segment = MessageSegment(type=SegmentImage, url="u", detail="original")
        self.assertEqual(segment.clone().detail, "original")

    def test_openai_image_url_carries_detail(self) -> None:
        segment = MessageSegment(type=SegmentImage, url="u", detail="original")
        self.assertEqual(
            openai_image_url(segment), {"url": "u", "detail": "original"}
        )
        self.assertEqual(openai_image_url(MessageSegment(type=SegmentImage, url="u")), {"url": "u"})

    def test_to_openai_content_parts(self) -> None:
        segments = [
            MessageSegment(type=SegmentText, text="看图"),
            MessageSegment(type=SegmentImage, url="http://x/1.png", detail="original"),
        ]
        parts = to_openai_content(segments)
        image = [part for part in parts if part["type"] == "image_url"][0]
        self.assertEqual(image["image_url"], {"url": "http://x/1.png", "detail": "original"})

    def test_to_openai_content_without_detail(self) -> None:
        segments = [
            MessageSegment(type=SegmentText, text="看图"),
            MessageSegment(type=SegmentImage, url="http://x/1.png"),
        ]
        parts = to_openai_content(segments)
        image = [part for part in parts if part["type"] == "image_url"][0]
        self.assertNotIn("detail", image["image_url"])

    def test_responses_input_image_carries_detail(self) -> None:
        segments = [MessageSegment(type=SegmentImage, url="http://x/1.png", detail="original")]
        parts = to_responses_content(segments, "user")
        self.assertEqual(parts[0]["type"], "input_image")
        self.assertEqual(parts[0]["image_url"], "http://x/1.png")
        self.assertEqual(parts[0]["detail"], "original")

    def test_responses_input_image_without_detail(self) -> None:
        segments = [MessageSegment(type=SegmentImage, url="http://x/1.png")]
        parts = to_responses_content(segments, "user")
        self.assertNotIn("detail", parts[0])


if __name__ == "__main__":
    unittest.main()
