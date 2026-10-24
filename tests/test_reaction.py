"""表情反应层（atri_x.reaction）的单元测试。

覆盖三块：

* 标记解析：正常/重复/中文冒号/大小写/非标记方括号/跨行标签；
* 图库：正常解析、缺文件、JSON 损坏、路径越界、重复 ID、权重与启用状态；
* 决策：启用范围、场景抑制、两类冷却、去重、概率、加权随机与提交流程。
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from atri_x.reaction import (  # noqa: E402
    Catalog,
    ReactionService,
    ReactionSettings,
    extract_marker,
    has_marker,
    load_catalog,
    marker_tag,
    parse_catalog,
)
from atri_x.reaction.catalog import CatalogItem, resolve_gallery_path  # noqa: E402

TMP_DIR = os.path.join(REPO_ROOT, "tmp_reaction_test")


class _TmpCase(unittest.TestCase):
    """每个用例一个独立的图库目录。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        self.gallery = os.path.join(TMP_DIR, self._testMethodName)
        os.makedirs(os.path.join(self.gallery, "assets"), exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def write_image(self, name: str) -> str:
        path = os.path.join(self.gallery, "assets", name)
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        return "assets/" + name

    def write_catalog(self, items: list[dict]) -> None:
        path = os.path.join(self.gallery, "catalog.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"version": 1, "items": items}, handle, ensure_ascii=False)


class MarkerTest(unittest.TestCase):
    def test_extracts_and_removes_marker(self) -> None:
        parsed = extract_marker("这个需求已经重写整个项目了吧！[[emo:无语]]")
        self.assertEqual("这个需求已经重写整个项目了吧！", parsed.text)
        self.assertEqual(("无语",), parsed.emotions)
        self.assertEqual("无语", parsed.primary)
        self.assertEqual(1, parsed.found)
        self.assertTrue(parsed.changed)

    def test_supports_fullwidth_colon_and_variants(self) -> None:
        for raw in (
            "好耶[[emo：开心]]",
            "好耶[[EMO:开心]]",
            "好耶[[表情:开心]]",
            "好耶[[ emo :  开心 ]]",
        ):
            parsed = extract_marker(raw)
            self.assertEqual("好耶", parsed.text, raw)
            self.assertEqual(("开心",), parsed.emotions, raw)

    def test_multiple_markers_keep_order_and_dedupe(self) -> None:
        parsed = extract_marker("先[[emo:得意]]后[[emo:无语]][[emo:得意]]")
        self.assertEqual("先后", parsed.text)
        self.assertEqual(("得意", "无语"), parsed.emotions)
        self.assertEqual(3, parsed.found)

    def test_plain_brackets_are_untouched(self) -> None:
        for raw in ("[[1]] 注脚", "代码 [[x]] 不是标记", "[[emo:]] 空标签"):
            self.assertFalse(has_marker(raw) and "[[" not in extract_marker(raw).text)
        self.assertFalse(has_marker("[[1]]"))
        self.assertFalse(has_marker("[[emo]]"))
        too_long = "[[emo:" + "很长的标签" * 8 + "]]"
        self.assertFalse(has_marker(too_long))

    def test_marker_across_newline_is_not_a_marker(self) -> None:
        self.assertFalse(has_marker("[[emo:\n无语]]"))

    def test_blank_lines_collapse_after_removal(self) -> None:
        parsed = extract_marker("正文第一段\n\n[[emo:思考]]\n\n第二段")
        self.assertEqual("正文第一段\n\n第二段", parsed.text)

    def test_no_marker_returns_same_text(self) -> None:
        parsed = extract_marker("普通回复")
        self.assertEqual("普通回复", parsed.text)
        self.assertFalse(parsed.changed)
        self.assertEqual("", parsed.primary)

    def test_marker_tag_round_trip(self) -> None:
        self.assertEqual("[[emo:无语]]", marker_tag("无语"))
        self.assertEqual("", marker_tag("  "))
        self.assertEqual(("无语",), extract_marker(marker_tag("无语")).emotions)


class CatalogTest(_TmpCase):
    def test_loads_valid_catalog(self) -> None:
        path = self.write_image("a.png")
        self.write_catalog(
            [
                {"id": "a", "emotion": "无语", "path": path, "weight": 3},
                {"id": "b", "emotion": "开心", "path": path, "enabled": False},
            ]
        )
        catalog = load_catalog(self.gallery)
        self.assertEqual(2, len(catalog.items))
        self.assertTrue(catalog.items[0].enabled)
        self.assertFalse(catalog.items[1].enabled)
        self.assertEqual(3.0, catalog.items[0].weight)
        self.assertEqual(["无语", "开心"], catalog.emotions())
        self.assertEqual([], catalog.warnings)

    def test_missing_catalog_is_soft(self) -> None:
        catalog = load_catalog(self.gallery)
        self.assertFalse(catalog.items)
        self.assertTrue(any("not found" in item for item in catalog.warnings))

    def test_broken_json_is_soft(self) -> None:
        path = os.path.join(self.gallery, "catalog.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        catalog = load_catalog(self.gallery)
        self.assertFalse(catalog.items)
        self.assertTrue(catalog.warnings)

    def test_missing_image_file_skipped(self) -> None:
        self.write_catalog([{"id": "a", "emotion": "无语", "path": "assets/none.png"}])
        catalog = load_catalog(self.gallery)
        self.assertFalse(catalog.items)
        self.assertTrue(any("not found" in item for item in catalog.warnings))

    def test_path_escape_rejected(self) -> None:
        outside = os.path.join(TMP_DIR, "outside.png")
        with open(outside, "wb") as handle:
            handle.write(b"x")
        for relative in ("../outside.png", os.path.abspath(outside)):
            self.assertIsNone(resolve_gallery_path(self.gallery, relative))
        self.write_catalog(
            [{"id": "a", "emotion": "无语", "path": "../outside.png"}]
        )
        self.assertFalse(load_catalog(self.gallery).items)

    def test_duplicate_id_skipped(self) -> None:
        path = self.write_image("a.png")
        self.write_catalog(
            [
                {"id": "x", "emotion": "无语", "path": path},
                {"id": "x", "emotion": "开心", "path": path},
            ]
        )
        catalog = load_catalog(self.gallery)
        self.assertEqual(1, len(catalog.items))
        self.assertTrue(any("duplicate id" in item for item in catalog.warnings))

    def test_bad_entries_do_not_break_others(self) -> None:
        path = self.write_image("a.png")
        self.write_catalog(
            [
                "不合法条目",
                {"emotion": "无语", "path": path},
                {"id": "ok", "emotion": "无语", "path": path},
            ]
        )
        catalog = load_catalog(self.gallery)
        self.assertEqual(["ok"], catalog.ids())
        self.assertGreaterEqual(len(catalog.warnings), 2)

    def test_weight_and_enabled_normalization(self) -> None:
        path = self.write_image("a.png")
        catalog = parse_catalog(
            {
                "items": [
                    {"id": "a", "emotion": "无语", "path": path, "weight": 0},
                    {
                        "id": "b",
                        "emotion": "无语",
                        "path": path,
                        "weight": "999999",
                    },
                    {"id": "c", "emotion": "无语", "path": path, "enabled": "off"},
                ]
            },
            self.gallery,
        )
        by_id = {item.id: item for item in catalog.items}
        self.assertFalse(by_id["a"].enabled)  # weight <= 0 视作停用
        self.assertLessEqual(by_id["b"].weight, 1000.0)
        self.assertFalse(by_id["c"].enabled)

    def test_scene_filter(self) -> None:
        item = CatalogItem(
            id="a",
            emotion="无语",
            path="/x",
            contexts=("轻松吐槽",),
            avoid=("严肃安慰",),
        )
        self.assertTrue(item.matches_scene(frozenset()))
        self.assertTrue(item.matches_scene(frozenset({"轻松吐槽"})))
        self.assertFalse(item.matches_scene(frozenset({"严肃安慰"})))
        self.assertFalse(item.matches_scene(frozenset({"专业说明"})))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _catalog(*entries: tuple[str, str, float]) -> Catalog:
    items = [
        CatalogItem(id=item_id, emotion=emotion, path="/tmp/" + item_id, weight=weight)
        for item_id, emotion, weight in entries
    ]
    return Catalog(items=items)


def _service(
    settings: ReactionSettings, catalog: Catalog, clock: _FakeClock, seed: int = 1
) -> ReactionService:
    return ReactionService(
        settings,
        "/tmp/gallery",
        clock=clock,
        rng=random.Random(seed),
        catalog_loader=lambda _dir: catalog,
    )


def _enabled(**kwargs) -> ReactionSettings:
    base = dict(enabled=True, cooldown_text_replies=0, session_cooldown_seconds=0,
                group_cooldown_seconds=0, dedupe_recent=0, probability=1.0,
                max_text_chars=0, suppress_keywords=())
    base.update(kwargs)
    return ReactionSettings(**base)


class PolicyTest(unittest.TestCase):
    def test_defaults_are_safe(self) -> None:
        settings = ReactionSettings.from_config(None)
        self.assertFalse(settings.enabled)
        self.assertEqual(2, settings.cooldown_text_replies)
        self.assertTrue(settings.suppress_keywords)

    def test_coercion_and_clamping(self) -> None:
        settings = ReactionSettings.from_config(
            {
                "enabled": "true",
                "cooldown_text_replies": "-3",
                "session_cooldown_seconds": 999999,
                "dedupe_recent": "abc",
                "probability": "1.7",
                "max_text_chars": 200,
                "suppress_keywords": ["错误", " 错误 ", ""],
            }
        )
        self.assertTrue(settings.enabled)
        self.assertEqual(0, settings.cooldown_text_replies)
        self.assertEqual(86400, settings.session_cooldown_seconds)
        self.assertEqual(5, settings.dedupe_recent)
        self.assertEqual(1.0, settings.probability)
        self.assertEqual(("错误",), settings.suppress_keywords)

    def test_empty_keyword_list_means_no_suppression(self) -> None:
        settings = ReactionSettings.from_config({"suppress_keywords": []})
        self.assertEqual((), settings.suppress_keywords)
        self.assertEqual("", settings.suppression_reason("程序报错了"))

    def test_suppression_reasons(self) -> None:
        settings = ReactionSettings(max_text_chars=4, suppress_keywords=("抱歉",))
        self.assertEqual("long_text", settings.suppression_reason("五个字符啊"))
        self.assertEqual("suppress_keyword:抱歉", settings.suppression_reason("抱歉"))
        self.assertEqual("", settings.suppression_reason("好耶"))

    def test_to_config_round_trip(self) -> None:
        settings = ReactionSettings(enabled=True, suppress_keywords=("错误",))
        data = settings.to_config()
        self.assertIs(True, data["enabled"])
        self.assertEqual(["错误"], data["suppress_keywords"])
        self.assertEqual(settings, ReactionSettings.from_config(data))


class ServiceTest(_TmpCase):
    def _service_with(self, **kwargs) -> tuple[ReactionService, _FakeClock]:
        clock = _FakeClock()
        path = self.write_image("a.png")
        self.write_catalog(
            [
                {"id": "a", "emotion": "无语", "path": path},
                {"id": "b", "emotion": "无语", "path": path},
                {"id": "c", "emotion": "开心", "path": path},
            ]
        )
        catalog = load_catalog(self.gallery)
        settings = _enabled(**kwargs)
        service = ReactionService(
            settings,
            self.gallery,
            clock=clock,
            rng=random.Random(7),
            catalog_loader=lambda _dir: catalog,
        )
        return service, clock

    def test_disabled_returns_reason(self) -> None:
        service = ReactionService(
            ReactionSettings(enabled=False), self.gallery, clock=_FakeClock()
        )
        decision = service.decide(
            scope_key="qqonebot:group:1", is_group=True, text="好耶", emotions=["无语"]
        )
        self.assertFalse(decision)
        self.assertEqual("disabled", decision.reason)

    def test_scope_switches(self) -> None:
        for is_group, kwargs, reason in (
            (True, dict(show_in_group=False), "group_disabled"),
            (False, dict(show_in_private=False), "private_disabled"),
        ):
            service, _clock = self._service_with(**kwargs)
            decision = service.decide(
                scope_key="qqonebot:group:1",
                group_key="qqonebot:group:1",
                is_group=is_group,
                text="好耶",
                emotions=["无语"],
            )
            self.assertFalse(decision)
            self.assertEqual(reason, decision.reason)

    def test_picks_matching_emotion(self) -> None:
        service, _clock = self._service_with()
        decision = service.decide(
            scope_key="qqonebot:group:1", is_group=True, text="好耶", emotions=["开心"]
        )
        self.assertTrue(decision)
        self.assertEqual("开心", decision.pick.emotion)
        self.assertEqual("c", decision.pick.item_id)

    def test_text_reply_cooldown_blocks_early_sends(self) -> None:
        service, _clock = self._service_with(cooldown_text_replies=2)
        # 新会话没有历史：第一张图可以直接发。
        first = service.decide(scope_key="s", is_group=True, text="好耶", emotions=["无语"])
        self.assertTrue(first)
        service.commit_image(first.pick, scope_key="s")
        # 发图后必须经过两条不带图的回复才允许再发。
        for _ in range(2):
            decision = service.decide(
                scope_key="s", is_group=True, text="好耶", emotions=["无语"]
            )
            self.assertEqual("text_reply_cooldown", decision.reason)
            service.note_text_reply(scope_key="s")
        decision = service.decide(scope_key="s", is_group=True, text="好耶", emotions=["无语"])
        self.assertTrue(decision)

    def test_session_and_group_cooldown(self) -> None:
        service, clock = self._service_with(
            cooldown_text_replies=0, session_cooldown_seconds=60, group_cooldown_seconds=300
        )
        decision = service.decide(
            scope_key="s1", group_key="g", is_group=True, text="好耶", emotions=["无语"]
        )
        self.assertTrue(decision)
        service.commit_image(decision.pick, scope_key="s1", group_key="g")
        clock.advance(10)
        # 同会话冷却先生效。
        same = service.decide(
            scope_key="s1", group_key="g", is_group=True, text="好耶", emotions=["无语"]
        )
        self.assertEqual("session_cooldown", same.reason)
        # 另一个会话仍被群冷却拦住。
        other = service.decide(
            scope_key="s2", group_key="g", is_group=True, text="好耶", emotions=["无语"]
        )
        self.assertEqual("group_cooldown", other.reason)
        clock.advance(400)
        self.assertTrue(
            service.decide(
                scope_key="s2", group_key="g", is_group=True, text="好耶", emotions=["无语"]
            )
        )

    def test_dedupe_avoids_recent_images(self) -> None:
        service, _clock = self._service_with(dedupe_recent=1)
        seen: list[str] = []
        for _ in range(4):
            decision = service.decide(
                scope_key="s", is_group=True, text="好耶", emotions=["无语"]
            )
            self.assertTrue(decision)
            seen.append(decision.pick.item_id)
            service.commit_image(decision.pick, scope_key="s")
        # 只有两张无语图，去重窗口 1 意味着相邻两次不会重复。
        for first, second in zip(seen, seen[1:]):
            self.assertNotEqual(first, second)

    def test_dedupe_exhausted_falls_back(self) -> None:
        service, _clock = self._service_with(dedupe_recent=5)
        # 先把两张图都发一遍，再发一次：候选全被排除仍应退回 LRU 而不是永远不发。
        for _ in range(2):
            decision = service.decide(
                scope_key="s", is_group=True, text="好耶", emotions=["无语"]
            )
            service.commit_image(decision.pick, scope_key="s")
        decision = service.decide(scope_key="s", is_group=True, text="好耶", emotions=["无语"])
        self.assertTrue(decision)
        self.assertIn(decision.pick.item_id, ("a", "b"))

    def test_probability_zero_never_sends(self) -> None:
        service, _clock = self._service_with(probability=0.0)
        decision = service.decide(
            scope_key="s", is_group=True, text="好耶", emotions=["无语"]
        )
        self.assertFalse(decision)
        self.assertEqual("probability", decision.reason)

    def test_unknown_emotion_no_candidate(self) -> None:
        service, _clock = self._service_with()
        decision = service.decide(
            scope_key="s", is_group=True, text="好耶", emotions=["不存在的情绪"]
        )
        self.assertFalse(decision)
        self.assertEqual("no_candidate", decision.reason)

    def test_fallback_to_earlier_emotion(self) -> None:
        service, _clock = self._service_with()
        decision = service.decide(
            scope_key="s", is_group=True, text="好耶", emotions=["开心", "没这个情绪"]
        )
        self.assertTrue(decision)
        self.assertEqual("开心", decision.pick.emotion)

    def test_suppression_blocks(self) -> None:
        service, _clock = self._service_with(
            suppress_keywords=("报错",), max_text_chars=5
        )
        self.assertEqual(
            "suppress_keyword:报错",
            service.decide(
                scope_key="s", is_group=True, text="程序报错", emotions=["无语"]
            ).reason,
        )
        self.assertEqual(
            "long_text",
            service.decide(
                scope_key="s", is_group=True, text="很长很长很长的正文", emotions=["无语"]
            ).reason,
        )

    def test_snapshot_and_reset(self) -> None:
        service, clock = self._service_with(dedupe_recent=2)
        decision = service.decide(
            scope_key="s", is_group=True, text="好耶", emotions=["无语"]
        )
        service.commit_image(decision.pick, scope_key="s")
        snapshot = service.snapshot("s")
        self.assertEqual([decision.pick.item_id], snapshot["recent_ids"])
        self.assertEqual(clock.now, snapshot["last_image_at"])
        service.reset()
        self.assertEqual([], service.snapshot("s")["recent_ids"])


if __name__ == "__main__":
    unittest.main()
