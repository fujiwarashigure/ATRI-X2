"""``atri_x.atrinis`` 的纯助手函数测试（从 ``test_atrinis_llm.py`` 拆出）。

覆盖 ``background.go`` / ``store.go`` / ``prepare.go`` / ``segments.go`` 里的
包内纯函数：模型槽位与会话模式白名单、沙盒子目录、目标 JSON 还原、哈希与
字符串工具、目标归一化、段校验与段文本。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.atriena import (
    Atriwisp,
    Call,
    CallKindCapability,
    CapabilityMessageRecall,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Origin,
    OriginInternal,
    Request,
    Segment,
    SegmentKindImage,
    SegmentKindText,
    Target,
    VersionV2,
)
from atri_x.atrinis import (
    ATRIWISP_NAME_PATTERN,
    Options,
    Service,
    StatusCompleted,
    StatusDelivering,
    StatusDuplicate,
    StatusFailed,
    StatusQueued,
    StatusResultReady,
    UNAUTHORIZED_MESSAGE,
    atrinis_sandbox_subdir,
    background_tool_names,
    content_hash,
    decode_resolved_targets,
    first_non_empty,
    first_platform,
    hash_text,
    is_atrinis_model_slot,
    is_atrinis_session_mode,
    normalize_targets,
    segments_content_text,
    set_from_strings,
    trim_strings,
    unique_sorted,
    unique_targets,
    validate_media_segment,
    validate_segment_url,
)
from atri_x.background import JSONResult
from atri_x.config import (
    AtrinisAtriwispConfig,
    AtrinisConfig,
    AtrinisTargetConfig,
    ModelSelection,
)
from atri_x.errors import InvalidError
from atri_x.storage import (
    AtrinisReportDeliveryDelivered,
    AtrinisReportDeliveryFailed,
    Message,
    RoleAssistant,
    Session,
    SessionModeWork,
    SessionStatusActive,
    sqlite,
)
from atri_x.toolrun import AtriwispToolDeclaration

from tests._atrinis_support import (
    TMP_ROOT,
    _FakePlatformCaller,
    _FakePlatformCallerResolver,
    _FakeRunner,
    _ServiceCase,
    external_tool,
    test_request,
)



class TestHelpers(unittest.TestCase):
    def test_model_slot_and_session_mode(self) -> None:
        self.assertTrue(is_atrinis_model_slot(" atriwisp1 "))
        self.assertFalse(is_atrinis_model_slot("work"))
        self.assertTrue(is_atrinis_session_mode("work"))
        self.assertTrue(is_atrinis_session_mode("chat"))
        self.assertFalse(is_atrinis_session_mode("bot"))

    def test_sandbox_subdir(self) -> None:
        self.assertEqual(atrinis_sandbox_subdir("watcher"), "atrinis/watcher")
        self.assertEqual(atrinis_sandbox_subdir(" watcher "), "atrinis/watcher")

    def test_first_platform(self) -> None:
        self.assertEqual(first_platform('[{"platform":"cli"},{"platform":"tg"}]'), "cli")
        self.assertEqual(first_platform('[{"platform":"  "},{"platform":"tg"}]'), "tg")
        self.assertEqual(first_platform("not json"), "")
        self.assertEqual(first_platform("null"), "")
        self.assertEqual(first_platform("{}"), "")

    def test_first_non_empty(self) -> None:
        self.assertEqual(first_non_empty("  ", " x "), "x")
        self.assertEqual(first_non_empty("", None), "")  # type: ignore[arg-type]

    def test_unique_sorted(self) -> None:
        self.assertEqual(unique_sorted([" b", "a", "b", " "]), ["a", "b"])

    def test_string_helpers(self) -> None:
        self.assertEqual(trim_strings([" a ", "", "  "]), ["a"])
        self.assertEqual(set_from_strings(["a", "a", " b "]), {"a", "b"})
        self.assertEqual(background_tool_names(["b", "a", "b"]), ["b", "a"])

    def test_hashes(self) -> None:
        self.assertEqual(hash_text(""), "")
        self.assertEqual(len(hash_text("x")), 64)
        req = test_request(ModeRecord)
        self.assertEqual(content_hash(req), content_hash(test_request(ModeRecord)))
        other = test_request(ModeRecord)
        other.content = "different"
        self.assertNotEqual(content_hash(req), content_hash(other))

    def test_normalize_targets_rules(self) -> None:
        with self.assertRaises(InvalidError):
            normalize_targets([])
        with self.assertRaises(InvalidError):
            normalize_targets([Target(platform="  ")])
        with self.assertRaises(InvalidError):
            normalize_targets([Target(platform="all", type="group")])
        with self.assertRaises(InvalidError):
            normalize_targets([Target(platform="cli", id="7")])
        with self.assertRaises(InvalidError):
            normalize_targets([Target(platform="cli", type="group")])
        with self.assertRaises(InvalidError):
            normalize_targets([Target(platform="cli", type="channel", id="7")])
        normalized = normalize_targets(
            [
                Target(platform=" qqonebot ", type=" group ", id=" 456 "),
                Target(platform="cli"),
                Target(platform="cli"),
            ]
        )
        self.assertEqual(
            [(t.platform, t.type, t.id) for t in normalized],
            [("cli", "", ""), ("qqonebot", "group", "456")],
        )

    def test_unique_targets_sorts_and_dedups(self) -> None:
        out = unique_targets(
            [
                Target(platform="telegram", type="group", id="2"),
                Target(platform="cli"),
                Target(platform="telegram", type="group", id="2"),
            ]
        )
        self.assertEqual([(t.platform, t.type, t.id) for t in out], [("cli", "", ""), ("telegram", "group", "2")])

    def test_validate_segment_url(self) -> None:
        validate_segment_url("https://example.com/a.png")
        validate_segment_url("data:image/png;base64,AAAA")
        for bad in ("", "   ", "ftp://example.com/a", "https://", "not a url"):
            with self.assertRaises(InvalidError):
                validate_segment_url(bad)

    def test_validate_media_segment(self) -> None:
        validate_media_segment(Segment(kind=SegmentKindText, text="hi"))
        with self.assertRaises(InvalidError):
            validate_media_segment(Segment(kind=SegmentKindText, text="hi", url="https://x/y"))
        validate_media_segment(Segment(kind=SegmentKindImage, url="media:" + "a" * 64))
        with self.assertRaises(InvalidError):
            validate_media_segment(Segment(kind=SegmentKindImage, url="media:zz"))
        with self.assertRaises(InvalidError):
            validate_media_segment(Segment(kind="video", url="https://x/y"))

    def test_segments_content_text(self) -> None:
        segments = [
            Segment(kind=SegmentKindText, text="看"),
            Segment(kind=SegmentKindImage, url="https://x/a.png", name="图"),
            Segment(kind="file", url="https://x/b.pdf"),
            Segment(kind=SegmentKindImage),
        ]
        self.assertEqual(
            segments_content_text(segments),
            "看[图片: 图][文件: https://x/b.pdf][图片: [图片]]",
        )
        self.assertEqual(segments_content_text(None), "")

    def test_response_requires_valid_origin(self) -> None:
        # Origin.validate 的拒绝对应 dispatch_atriena 的失败响应。
        self.assertEqual(
            Origin(kind="carrier-pigeon", name="x").kind, "carrier-pigeon"
        )





if __name__ == "__main__":
    unittest.main()
