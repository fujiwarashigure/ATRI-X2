"""漫画计划/记录模型的结构校验与序列化测试。

重点不是"字段能读"，而是**坏计划会被拒绝**：格数不符、人物串台、原文台词没有
来源、参考图类型非法……这些如果不拒绝，最终提示词就会带着错的事实去画图。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest

from atri_x.comic import characters as comic_characters
from atri_x.comic import context as comic_context
from atri_x.comic import errors as comic_errors
from atri_x.comic import models as comic_models
from atri_x.comic import options as comic_options
from atri_x.comic import planner as comic_planner
from atri_x.comic import prompt as comic_prompt
from atri_x.comic import records as comic_records
from atri_x.comic import references as comic_references
from atri_x.comic import service as comic_service
from atri_x.comic import store as comic_store
from atri_x.comic import (
    ComicCharacter,
    ComicCharacterReference,
    ComicDataError,
    ComicDialogue,
    ComicPanel,
    ComicPlan,
    ComicRecord,
    ComicSource,
    is_maybe_unknown_error,
    new_plan_id,
    validate_plan,
)
from tests._comic_fakes import make_character_model, make_plan

#: 业务域子模块必须能独立导入（包 ``__init__`` 不该成为唯一入口）。
COMIC_SUBMODULES = (
    comic_characters,
    comic_context,
    comic_errors,
    comic_models,
    comic_options,
    comic_planner,
    comic_prompt,
    comic_records,
    comic_references,
    comic_service,
    comic_store,
)


class ImportsTest(unittest.TestCase):
    def test_all_submodules_import_cleanly(self) -> None:
        for module in COMIC_SUBMODULES:
            self.assertTrue(module.__name__.startswith("atri_x.comic."))


class PlanValidationTest(unittest.TestCase):
    def test_valid_plan_passes_and_serializes(self) -> None:
        plan = make_plan()
        plan.validate()
        payload = plan.to_dict()
        self.assertEqual(payload["panel_count"], 2)
        self.assertEqual(payload["fingerprint"], plan.fingerprint())
        restored = ComicPlan.from_dict(json.loads(json.dumps(payload)))
        restored.validate()
        self.assertEqual(restored.fingerprint(), plan.fingerprint())
        self.assertEqual(restored.title, plan.title)

    def test_new_plan_id_is_lowercase_uuid(self) -> None:
        plan_id = new_plan_id()
        self.assertEqual(plan_id, plan_id.lower())
        self.assertEqual(len(plan_id.split("-")), 5)

    def test_validate_plan_alias(self) -> None:
        plan = make_plan()
        validate_plan(plan)
        plan.panel_count = 3
        with self.assertRaises(ComicDataError):
            validate_plan(plan)

    def test_panel_count_mismatch_rejected(self) -> None:
        plan = make_plan()
        plan.panel_count = 3
        with self.assertRaises(ComicDataError) as caught:
            plan.validate()
        self.assertIn("实际有 2 格", str(caught.exception))

    def test_empty_title_rejected(self) -> None:
        plan = make_plan()
        plan.title = "   "
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_duplicate_character_id_rejected(self) -> None:
        character = make_character_model()
        plan = make_plan(characters=[character, character])
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_too_many_characters_rejected(self) -> None:
        cast = [
            make_character_model(character_id=f"member:qqonebot:{index}", name=f"人{index}")
            for index in range(5)
        ]
        plan = make_plan(panel_count=1, characters=cast)
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_panel_character_must_exist(self) -> None:
        plan = make_plan()
        plan.panels[0].character_ids = ["member:qqonebot:9999"]
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_panel_indexes_must_be_contiguous(self) -> None:
        plan = make_plan(panel_count=2)
        plan.panels[1].index = 5
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_original_dialogue_needs_source(self) -> None:
        plan = make_plan(panel_count=1)
        plan.panels[0].dialogue[0].source_message_ids = []
        with self.assertRaises(ComicDataError) as caught:
            plan.validate()
        self.assertIn("source_message_ids", str(caught.exception))

    def test_dialogue_source_must_be_in_plan_source(self) -> None:
        plan = make_plan(panel_count=1)
        plan.panels[0].dialogue[0].source_message_ids = ["msg-unknown"]
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_dialogue_speaker_must_exist(self) -> None:
        plan = make_plan(panel_count=1)
        plan.panels[0].dialogue[0].speaker_id = "member:qqonebot:404"
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_invented_dialogue_does_not_need_source(self) -> None:
        plan = make_plan(panel_count=1)
        line = plan.panels[0].dialogue[0]
        line.kind = "invented"
        line.source_message_ids = []
        plan.validate()
        self.assertEqual(plan.panels[0].dialogue[0].kind, "invented")

    def test_empty_scene_rejected(self) -> None:
        plan = make_plan(panel_count=1)
        plan.panels[0].scene = " "
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_source_requires_platform_and_scope(self) -> None:
        plan = make_plan(panel_count=1)
        plan.source.scope_id = ""
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_source_requires_messages(self) -> None:
        plan = make_plan(panel_count=1)
        plan.source.message_ids = []
        with self.assertRaises(ComicDataError):
            plan.validate()

    def test_fingerprint_changes_when_dialogue_changes(self) -> None:
        plan = make_plan(panel_count=1)
        before = plan.fingerprint()
        plan.panels[0].dialogue[0].text = "改过的台词"
        self.assertNotEqual(before, plan.fingerprint())

    def test_reference_order_keeps_plan_order(self) -> None:
        first = make_character_model(
            character_id="member:qqonebot:1", name="甲", reference_kind="avatar"
        )
        second = make_character_model(
            character_id="character:atri",
            name="乙",
            reference_kind="character_image",
            reference_source="avatar.png",
            character_id_value="atri",
        )
        plan = make_plan(panel_count=1, characters=[first, second])
        self.assertEqual([item.display_name for item in plan.reference_order()], ["甲", "乙"])


class CharacterReferenceTest(unittest.TestCase):
    def test_none_reference_clears_source(self) -> None:
        reference = ComicCharacterReference(kind="none", source="stale")
        reference.validate()
        self.assertEqual(reference.source, "")

    def test_media_reference_requires_source(self) -> None:
        reference = ComicCharacterReference(kind="media", source="")
        with self.assertRaises(ComicDataError):
            reference.validate()

    def test_unknown_reference_kind_rejected(self) -> None:
        reference = ComicCharacterReference(kind="whatever", source="x")
        with self.assertRaises(ComicDataError):
            reference.validate()

    def test_describe_is_human_readable(self) -> None:
        self.assertIn("平台头像", ComicCharacterReference(kind="avatar", source="u").describe())
        self.assertIn("角色卡图片", ComicCharacterReference(kind="character_image", source="a.png").describe())
        self.assertEqual(
            ComicCharacterReference(kind="none").describe(), "无参考图（仅文字描述）"
        )

    def test_character_requires_display_name(self) -> None:
        character = ComicCharacter(id="member:qqonebot:1", display_name="")
        with self.assertRaises(ComicDataError):
            character.validate()

    def test_character_source_character_requires_id(self) -> None:
        character = ComicCharacter(
            id="character:atri", display_name="亚托莉", source="character"
        )
        with self.assertRaises(ComicDataError):
            character.validate()


class RecordTest(unittest.TestCase):
    def _record(self, **overrides):
        defaults = dict(
            task_id="task-0001",
            plan_id="plan-0001",
            plan_revision=1,
            plan_fingerprint="f" * 64,
        )
        defaults.update(overrides)
        return ComicRecord(**defaults)

    def test_record_round_trip(self) -> None:
        record = self._record()
        record.validate()
        payload = record.to_dict()
        restored = ComicRecord.from_dict(payload)
        restored.validate()
        self.assertEqual(restored.task_id, record.task_id)
        self.assertEqual(restored.state, "rendering")

    def test_record_needs_plan_binding(self) -> None:
        with self.assertRaises(ComicDataError):
            self._record(plan_fingerprint="").validate()
        with self.assertRaises(ComicDataError):
            self._record(plan_revision=0).validate()

    def test_record_state_machine_values(self) -> None:
        with self.assertRaises(ComicDataError):
            self._record(state="weird").validate()
        record = self._record(state="unknown", generation="unknown", storage="failed")
        record.validate()
        self.assertEqual(record.state, "unknown")

    def test_maybe_unknown_error_classification(self) -> None:
        self.assertTrue(is_maybe_unknown_error(TimeoutError("slow")))
        self.assertTrue(is_maybe_unknown_error(ConnectionError("reset")))
        self.assertFalse(is_maybe_unknown_error(RuntimeError("bad request")))


class StoreTest(unittest.TestCase):
    """计划/记录的持久化：版本累积、损坏拒写、重启恢复。"""

    ROOT = os.path.join(os.getcwd(), "tmp_comic_store_test")

    def setUp(self) -> None:
        shutil.rmtree(self.ROOT, ignore_errors=True)
        os.makedirs(self.ROOT, exist_ok=True)
        from atri_x.comic.store import ComicStore

        self.store = ComicStore(os.path.join(self.ROOT, "comic"))

    def tearDown(self) -> None:
        shutil.rmtree(self.ROOT, ignore_errors=True)

    def test_plan_versions_accumulate(self) -> None:
        plan = make_plan(revision=1)
        self.store.save_plan(plan)
        plan.revision = 2
        plan.title = "第二版标题"
        self.store.save_plan(plan)
        self.assertEqual(self.store.latest_revision(plan.plan_id), 2)
        self.assertEqual(self.store.load_plan(plan.plan_id).title, "第二版标题")
        self.assertEqual(self.store.load_plan(plan.plan_id, 1).title, "群聊漫画：测试")
        with self.assertRaises(Exception):
            self.store.load_plan(plan.plan_id, 9)

    def test_record_round_trip_and_recover_interrupted(self) -> None:
        record = ComicRecord(
            task_id="task-1",
            plan_id="plan-1",
            plan_revision=1,
            plan_fingerprint="f" * 64,
            state="rendering",
        )
        self.store.save_record(record)
        loaded = self.store.load_record("task-1")
        recovered = self.store.recover_interrupted(loaded)
        self.assertEqual(recovered.state, "unknown")
        self.assertEqual(recovered.generation, "unknown")
        self.assertIn("不会自动重试", recovered.error)

    def test_corrupt_plan_file_is_not_overwritten(self) -> None:
        plan = make_plan()
        self.store.save_plan(plan)
        path = self.store.plan_path(plan.plan_id)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("{ broken json")
        with self.assertRaises(Exception):
            self.store.load_plan(plan.plan_id)
        # 损坏文件仍在原地（没有被"修复"覆盖）。
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{ broken json")

    def test_path_traversal_ids_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.store.plan_path("../../etc/passwd")
        with self.assertRaises(Exception):
            self.store.record_path("..\\..\\x")

    def test_comic_root_layout(self) -> None:
        from atri_x.comic.store import comic_root

        self.assertTrue(comic_root(self.ROOT).endswith("comic"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
