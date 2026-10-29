"""人设卡导入 / 导出 / SOUL 激活的单元测试（1.1.3 修订新增）。

覆盖：

* ``atri_x.character.archive``：文本 / JSON / ZIP / Markdown / 目录导入，可见性与
  owner 规则，ZIP 路径穿越拒绝；
* ``atri_x.character.activation``：激活写 SOUL.md、备份、回滚、非超管拒绝。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
import zipfile

from atri_x.character import CharacterError, NewStore, Viewer
from atri_x.character import WriteRequest
from atri_x.character.activation import (
    activate_character,
    render_soul_from_character,
    restore_soul,
    soul_status,
)
from atri_x.character.archive import (
    export_json,
    export_markdown,
    export_zip,
    import_file,
    import_text,
)
from atri_x.errors import ForbiddenError

TMP_ROOT = os.path.join(os.getcwd(), "tmp_character_archive_test")

ADMIN = Viewer(platform="test", actor_id="admin", superadmin=True)
USER = Viewer(platform="qqonebot", actor_id="qqonebot:1001", superadmin=False)
OTHER = Viewer(platform="qqonebot", actor_id="qqonebot:2002", superadmin=False)


def _store() -> NewStore:
    return NewStore(os.path.join(TMP_ROOT, "characters"))


def _text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _seed_card(store, character_id: str = "atri", viewer=ADMIN) -> None:
    store.write(
        None,
        WriteRequest(
            id=character_id,
            name="亚托莉",
            aliases=["Atri"],
            description="测试角色",
            tags=["test"],
            visibility="public",
            source="test",
            docs={
                "profile": "# 亚托莉\n\n说话简短。",
                "world": "海底。",
            },
        ),
        viewer,
    )


class CharacterArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.store = _store()

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    # -- 文本导入 -----------------------------------------------------------
    def test_import_text_for_normal_user_is_private(self) -> None:
        result = import_text(
            self.store, None, "# 大青鱼\n\n蓝色的鱼。", USER, character_id="bigfish"
        )
        item = result.character
        self.assertTrue(result.created)
        self.assertEqual(item.visibility, "private")
        self.assertEqual(item.owner_platform, "qqonebot")
        self.assertEqual(item.owner_id, "qqonebot:1001")
        self.assertEqual(item.source, "import")
        self.assertIn("蓝色的鱼", item.docs["profile"])
        self.assertEqual(len(self.store.list(None, USER)), 1)
        self.assertEqual(self.store.list(None, OTHER), [])

    def test_import_text_superadmin_public_and_name_from_heading(self) -> None:
        result = import_text(self.store, None, "# 亚尔缇娜\n\n我是亚尔缇娜。", ADMIN)
        item = result.character
        self.assertEqual(item.name, "亚尔缇娜")
        self.assertEqual(item.visibility, "public")
        self.assertTrue(item.id)

    def test_import_text_without_id_generates_ascii_id(self) -> None:
        result = import_text(self.store, None, "# BigFish\n\n蓝色。", ADMIN)
        self.assertEqual(result.character.id, "bigfish")

    def test_import_text_rejects_empty(self) -> None:
        with self.assertRaises(CharacterError):
            import_text(self.store, None, "   ", ADMIN)

    # -- 文件导入 -----------------------------------------------------------
    def test_import_markdown_file(self) -> None:
        path = os.path.join(TMP_ROOT, "persona.md")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# 小蓝\n\n来自文件的人设。\n")
        result = import_file(self.store, None, path, USER)
        self.assertEqual(result.character.id, "persona")
        self.assertIn("来自文件的人设", result.character.docs["profile"])

    def test_import_directory(self) -> None:
        directory = os.path.join(TMP_ROOT, "imported-card")
        os.makedirs(os.path.join(directory, "notes"), exist_ok=True)
        os.makedirs(os.path.join(directory, "images"), exist_ok=True)
        with open(os.path.join(directory, "character.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[character]\nid = "imported"\nname = "导入角色"\nvisibility = "public"\n'
            )
        with open(os.path.join(directory, "profile.md"), "w", encoding="utf-8") as handle:
            handle.write("目录导入的人设。\n")
        with open(os.path.join(directory, "notes", "extra.md"), "w", encoding="utf-8") as handle:
            handle.write("备注内容。\n")
        with open(os.path.join(directory, "images", "a.png"), "wb") as handle:
            handle.write(b"\x89PNG-fake")
        result = import_file(self.store, None, directory, ADMIN)
        item = result.character
        self.assertEqual(item.id, "imported")
        self.assertEqual(item.name, "导入角色")
        self.assertIn("目录导入的人设", item.docs["profile"])
        self.assertIn("notes/extra", item.docs)
        self.assertEqual(len(item.images), 1)

    # -- 导出 / 往返 --------------------------------------------------------
    def test_zip_roundtrip_keeps_docs_and_images(self) -> None:
        _seed_card(self.store)
        self.store.add_image_with_meta(
            None, "atri", "a.png", "image/png", "", "", "test", b"\x89PNG-data", ADMIN
        )
        archive_path = os.path.join(TMP_ROOT, "atri.zip")
        export_zip(self.store, None, "atri", archive_path, ADMIN)
        self.assertTrue(os.path.exists(archive_path))

        other = NewStore(os.path.join(TMP_ROOT, "characters2"))
        result = import_file(other, None, archive_path, ADMIN)
        item = result.character
        self.assertEqual(item.id, "atri")
        self.assertIn("亚托莉", item.docs["profile"])
        self.assertIn("海底", item.docs["world"])
        self.assertEqual([image.name for image in item.images], ["a.png"])
        data, mime = other.read_image(None, "atri", "a.png", ADMIN)
        self.assertEqual(data, b"\x89PNG-data")
        self.assertEqual(mime, "image/png")

    def test_json_roundtrip_keeps_docs_and_warns_images(self) -> None:
        _seed_card(self.store)
        self.store.add_image_with_meta(
            None, "atri", "a.png", "image/png", "", "", "test", b"\x89PNG", ADMIN
        )
        path = os.path.join(TMP_ROOT, "atri.json")
        export_json(self.store, None, "atri", path, ADMIN)
        payload = json.loads(_text(path))
        self.assertEqual(payload["format"], "atri-x-character")

        other = NewStore(os.path.join(TMP_ROOT, "characters2"))
        result = import_file(other, None, path, ADMIN)
        self.assertIn("说话简短", result.character.docs["profile"])
        self.assertTrue(any("JSON" in warning for warning in result.warnings))

    def test_export_markdown_writes_profile(self) -> None:
        _seed_card(self.store)
        path = os.path.join(TMP_ROOT, "atri.md")
        export_markdown(self.store, None, "atri", path, ADMIN)
        text = _text(path)
        self.assertIn("# 亚托莉", text)
        self.assertNotIn("海底", text)

    def test_import_rejects_path_traversal_in_zip(self) -> None:
        path = os.path.join(TMP_ROOT, "evil.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../evil.txt", "x")
        with self.assertRaises(CharacterError):
            import_file(self.store, None, path, ADMIN)

    def test_import_unknown_extension(self) -> None:
        path = os.path.join(TMP_ROOT, "a.bin")
        with open(path, "wb") as handle:
            handle.write(b"\x00")
        with self.assertRaises(CharacterError):
            import_file(self.store, None, path, ADMIN)


class SoulActivationCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.store = _store()
        _seed_card(self.store)
        self.soul_path = os.path.join(TMP_ROOT, "SOUL.md")
        with open(self.soul_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("旧的全局人设。\n")

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_render_includes_profile_and_world(self) -> None:
        item = self.store.get(None, "atri")
        text = render_soul_from_character(item)
        self.assertIn("亚托莉", text)
        self.assertIn("说话简短", text)
        self.assertIn("## world", text)

    def test_activate_writes_soul_and_backup(self) -> None:
        result = activate_character(self.store, None, "atri", self.soul_path, ADMIN)
        text = _text(self.soul_path)
        self.assertIn("亚托莉", text)
        backup = _text(result.backup_path)
        self.assertEqual(backup, "旧的全局人设。\n")
        self.assertTrue(soul_status(self.soul_path).backup_exists)

    def test_restore_roundtrip(self) -> None:
        activate_character(self.store, None, "atri", self.soul_path, ADMIN)
        self.assertTrue(restore_soul(self.soul_path))
        self.assertEqual(_text(self.soul_path), "旧的全局人设。\n")

    def test_restore_without_backup_returns_false(self) -> None:
        self.assertFalse(restore_soul(self.soul_path))

    def test_non_superadmin_forbidden(self) -> None:
        with self.assertRaises(ForbiddenError):
            activate_character(self.store, None, "atri", self.soul_path, USER)


if __name__ == "__main__":
    unittest.main()
