"""``atri_x.character`` 的单元测试。

对应 Go：``internal/character/store_test.go``（另加 write/search/manifest 的
逐字段断言）。

临时目录建在仓库根下（本机沙箱的 0o700 ACL 会让系统临时目录的清理抛
``PermissionError``），``tearDown`` 用 ``shutil.rmtree(ignore_errors=True)``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
import unittest
import uuid
from typing import Any

from atri_x.character import (
    FILE_NAME,
    MAX_IMAGES_PER_CHARACTER,
    SCAN_COOLDOWN,
    CharacterError,
    ImageSettings,
    NewStore,
    Store,
    Viewer,
    VisibilityPrivate,
    VisibilityPublic,
    WithActive,
    ActiveIDs,
    WriteRequest,
    doc_file_name,
    next_version,
    normalize_visibility,
    safe_image_name,
    safe_join,
    valid_id,
)
from atri_x.character import write as character_write_module
from atri_x.errors import ErrForbidden, ErrInvalidID, ErrNotFound

character_write = character_write_module

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

OWNER = Viewer(platform="qqonebot", actor_id="qqonebot:1001")
OTHER = Viewer(platform="qqonebot", actor_id="qqonebot:1002")
ADMIN = Viewer(platform="cli", actor_id="cli:local", superadmin=True)


def make_temp_dir(case: unittest.TestCase, prefix: str = ".character-test-") -> str:
    """工作区内的临时目录（见 tests/test_ops.py 的说明）。"""
    while True:
        directory = os.path.join(_REPO_ROOT, prefix + uuid.uuid4().hex[:12])
        if not os.path.exists(directory):
            break
    os.makedirs(directory, mode=0o777)
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return directory


def read_toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        return tomllib.loads(handle.read().decode("utf-8"))


class TestStoreWriteVisibilitySearchAndDelete(unittest.TestCase):
    """对应 Go ``TestStoreWriteVisibilitySearchAndDelete``。"""

    def setUp(self) -> None:
        self.root = make_temp_dir(self)
        self.store = NewStore(self.root)

    def test_full_flow(self) -> None:
        description = "冷面剑客"
        profile = "你是一位沉默寡言的剑客，只在该说话时说话。"
        world = "架空武侠世界。"
        note = "口头禅：剑不问出处。"
        item = self.store.write(
            None,
            WriteRequest(
                id="SwordMan",
                name="剑客",
                aliases=["剑士"],
                tags=["wuxia"],
                description=description,
                docs={"profile": profile, "world": world, "notes/招式": note},
            ),
            OWNER,
        )
        self.assertEqual("swordman", item.id)
        self.assertEqual(VisibilityPrivate, item.visibility)
        self.assertEqual(OWNER.platform, item.owner_platform)
        self.assertEqual(OWNER.actor_id, item.owner_id)
        for name in ("character.toml", "profile.md", "world.md", os.path.join("notes", "招式.md")):
            self.assertTrue(
                os.path.exists(os.path.join(self.root, "swordman", name)), name
            )
        self.assertEqual(profile, item.docs["profile"].strip())
        self.assertEqual(note, item.docs["notes/招式"].strip())

        with self.assertRaises(Exception) as ctx:
            self.store.get_visible(None, "swordman", OTHER)
        self.assertIs(ErrForbidden, ctx.exception)
        self.assertEqual("swordman", self.store.get_visible(None, "swordman", ADMIN).id)

        # 只有属主能改。
        with self.assertRaises(Exception) as ctx:
            self.store.write(None, WriteRequest(id="swordman", name="hacked"), OTHER)
        self.assertIs(ErrForbidden, ctx.exception)

        # 超管建公开角色，人人可读。
        public = self.store.write(
            None, WriteRequest(id="assistant", name="公共助手", visibility="public"), ADMIN
        )
        self.assertEqual(VisibilityPublic, public.visibility)
        self.assertEqual("assistant", self.store.get_visible(None, "assistant", OTHER).id)

        # 检索覆盖 name / alias / tag / 正文，且不泄露 private 角色。
        results = self.store.search(None, "剑客", "", "", 5, OTHER)
        self.assertNotIn("swordman", [result.id for result in results])
        results = self.store.search(None, "口头禅", "", "", 5, OWNER)
        self.assertTrue(results)
        self.assertEqual("swordman", results[0].id)
        self.assertEqual([], self.store.search(None, "wuxia", "", "", 5, OTHER))

        # 图片：原始字节 + Media Center 引用。
        media_id = "media:" + "a" * 64
        image = self.store.add_image(
            None, "swordman", "avatar.png", "image/png", media_id, b"png-bytes", OWNER
        )
        self.assertEqual("avatar.png", image.name)
        self.assertEqual(media_id, image.media_id)
        self.assertTrue(os.path.exists(os.path.join(self.root, "swordman", "images", "avatar.png")))
        loaded = self.store.get(None, "swordman")
        self.assertEqual(1, len(loaded.images))
        self.assertEqual(media_id, loaded.images[0].media_id)

        data, mime = self.store.read_image(None, "swordman", "avatar.png", OWNER)
        self.assertEqual(b"png-bytes", data)
        self.assertEqual("image/png", mime)
        with self.assertRaises(Exception) as ctx:
            self.store.read_image(None, "swordman", "avatar.png", OTHER)
        self.assertIs(ErrForbidden, ctx.exception)

        self.store.remove_image(None, "swordman", "avatar.png", OWNER)
        self.assertEqual([], self.store.get(None, "swordman").images)
        self.assertFalse(os.path.exists(os.path.join(self.root, "swordman", "images", "avatar.png")))

        prompt = self.store.get(None, "swordman").prompt(0)
        self.assertIn(profile, prompt)
        self.assertIn(world, prompt)

        with self.assertRaises(Exception) as ctx:
            self.store.delete(None, "swordman", OTHER)
        self.assertIs(ErrForbidden, ctx.exception)
        self.store.delete(None, "swordman", OWNER)
        with self.assertRaises(Exception) as ctx:
            self.store.get(None, "swordman")
        self.assertIs(ErrNotFound, ctx.exception)


class TestStoreRejectsInvalidID(unittest.TestCase):
    """对应 Go ``TestStoreRejectsInvalidID``。"""

    def setUp(self) -> None:
        self.store = NewStore(make_temp_dir(self))

    def test_invalid_id(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.store.write(None, WriteRequest(id="../escape"), ADMIN)
        self.assertIs(ErrInvalidID, ctx.exception)
        with self.assertRaises(Exception) as ctx:
            self.store.get(None, "../escape")
        self.assertIs(ErrInvalidID, ctx.exception)
        with self.assertRaises(Exception) as ctx:
            self.store.get(None, "")
        self.assertIs(ErrInvalidID, ctx.exception)

    def test_id_normalization_and_pattern(self) -> None:
        self.assertTrue(valid_id("swordman"))
        self.assertTrue(valid_id("a_b.c-d"))
        self.assertFalse(valid_id("Swordman"))
        self.assertFalse(valid_id("-lead"))
        self.assertFalse(valid_id("x" * 65))
        item = self.store.write(None, WriteRequest(id="  Mixed.ID  ", name="x"), ADMIN)
        self.assertEqual("mixed.id", item.id)


class TestStoreVersionsSourcesAndManifest(unittest.TestCase):
    """对应 Go ``TestStoreVersionsSourcesAndManifest``。"""

    def setUp(self) -> None:
        self.root = make_temp_dir(self)
        self.store = NewStore(self.root)

    def test_versions_sources_and_manifest(self) -> None:
        profile = "角色资料"
        item = self.store.write(
            None,
            WriteRequest(
                id="asset",
                name="素材角色",
                version="7",
                source="import",
                docs={"profile": profile},
            ),
            ADMIN,
        )
        self.assertEqual("7", item.version)
        self.assertEqual("import", item.source)
        self.store.add_image(
            None, "asset", "avatar.png", "image/png", "media:asset", b"png", ADMIN
        )
        manifest = self.store.manifest(None, ADMIN)
        self.assertEqual(1, len(manifest.characters))
        self.assertEqual("7", manifest.characters[0].version)
        self.assertEqual("import", manifest.characters[0].source)
        self.assertEqual(2, len(manifest.characters[0].assets))
        for asset in manifest.characters[0].assets:
            self.assertTrue(asset.sha256)
            self.assertTrue(asset.size)

        manifest_path = os.path.join(make_temp_dir(self), "manifest.json")
        self.store.write_manifest(None, manifest_path, ADMIN)
        self.assertTrue(os.path.exists(manifest_path))

    def test_manifest_fields_sha256_and_format(self) -> None:
        profile = "角色资料"
        self.store.write(
            None,
            WriteRequest(
                id="asset",
                name="素材角色",
                version="7",
                source="import",
                docs={"profile": profile, "notes/招式": "口头禅"},
            ),
            ADMIN,
        )
        self.store.add_image_with_meta(
            None, "asset", "avatar.png", "image/png", "media:asset", "3", "gen", b"png-bytes", ADMIN
        )
        manifest = self.store.manifest(None, ADMIN)
        self.assertTrue(_RFC3339.match(manifest.generated_at), manifest.generated_at)

        entry = manifest.characters[0]
        self.assertEqual("asset", entry.id)
        self.assertEqual("素材角色", entry.name)
        self.assertEqual("7", entry.version)
        self.assertEqual("import", entry.source)
        self.assertTrue(_RFC3339.match(entry.updated_at), entry.updated_at)

        by_key = {(asset.kind, asset.name): asset for asset in entry.assets}
        self.assertEqual({("doc", "profile"), ("doc", "notes/招式"), ("image", "avatar.png")}, set(by_key))
        # 逐字段断言 sha256 与来源。
        for asset in entry.assets:
            path = os.path.join(self.root, "asset", *asset.path.split("/"))
            with open(path, "rb") as handle:
                data = handle.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(), asset.sha256)
            self.assertEqual(len(data), asset.size)
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", asset.sha256))
        self.assertEqual("7", by_key[("doc", "profile")].version)
        self.assertEqual("import", by_key[("doc", "profile")].source)
        self.assertEqual("profile.md", by_key[("doc", "profile")].path)
        self.assertEqual("notes/招式.md", by_key[("doc", "notes/招式")].path)
        self.assertEqual("3", by_key[("image", "avatar.png")].version)
        self.assertEqual("gen", by_key[("image", "avatar.png")].source)
        self.assertEqual("images/avatar.png", by_key[("image", "avatar.png")].path)

        # JSON 逐字段：键名 / 顺序 / omitempty / assets 为空时是 null。
        manifest_path = os.path.join(make_temp_dir(self), "manifest.json")
        self.store.write_manifest(None, manifest_path, ADMIN)
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\n"))
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith("{\n  \"generated_at\": "))
        parsed = json.loads(text)
        self.assertEqual(["generated_at", "characters"], list(parsed))
        self.assertEqual(
            ["id", "name", "version", "source", "updated_at", "assets"],
            list(parsed["characters"][0]),
        )
        self.assertEqual(
            ["kind", "name", "path", "size", "version", "source", "sha256"],
            list(parsed["characters"][0]["assets"][0]),
        )
        # Go 的 json.MarshalIndent 缩进是两格。
        self.assertIn('\n          "sha256": "', text)
        self.assertIn('\n          "path": "', text)

    def test_manifest_omits_empty_version_source_and_nulls_empty_assets(self) -> None:
        self.store.write(None, WriteRequest(id="empty", name="空角色"), ADMIN)
        manifest = self.store.manifest(None, ADMIN)
        self.assertEqual([], manifest.characters[0].assets)
        manifest_path = os.path.join(make_temp_dir(self), "manifest.json")
        self.store.write_manifest(None, manifest_path, ADMIN)
        with open(manifest_path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        entry = parsed["characters"][0]
        self.assertEqual(["id", "name", "version", "source", "updated_at", "assets"], list(entry))
        self.assertIn("assets", entry)
        self.assertIsNone(entry["assets"])
        self.assertEqual("1", entry["version"])
        self.assertEqual("cli", entry["source"])
        self.assertTrue(_RFC3339.match(entry["updated_at"]), entry["updated_at"])

    def test_manifest_only_lists_visible_characters(self) -> None:
        self.store.write(None, WriteRequest(id="mine", name="私有的"), OWNER)
        self.store.write(None, WriteRequest(id="public", name="公开的", visibility="public"), ADMIN)
        self.assertEqual(["public"], [item.id for item in self.store.manifest(None, OTHER).characters])
        self.assertEqual(
            ["mine", "public"], [item.id for item in self.store.manifest(None, OWNER).characters]
        )

    def test_manifest_skips_missing_files(self) -> None:
        self.store.write(None, WriteRequest(id="asset", name="x", docs={"profile": "p"}), ADMIN)
        os.remove(os.path.join(self.root, "asset", "profile.md"))
        # 索引里还有 profile，但文件没了：清单跳过它而不是报错（与 Go 一致）。
        self.store.refresh(None)
        self.assertEqual(["asset"], [item.id for item in self.store.manifest(None, ADMIN).characters])
        self.assertEqual([], self.store.manifest(None, ADMIN).characters[0].assets)


class TestWriteSemantics(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir(self)
        self.store = NewStore(self.root)

    def test_atomic_write_leaves_no_temp_files_and_valid_toml(self) -> None:
        self.store.write(
            None, WriteRequest(id="asset", name="x", docs={"profile": "p"}), ADMIN
        )
        character_dir = os.path.join(self.root, "asset")
        self.store.write(None, WriteRequest(id="asset", name="改过名字"), ADMIN)
        leftovers = [
            name for name in os.listdir(character_dir) if name.startswith(".") or name.endswith(".tmp")
        ]
        self.assertEqual([], leftovers)
        self.assertEqual(
            ["character.toml", "images", "notes", "profile.md"], sorted(os.listdir(character_dir))
        )
        parsed = read_toml(os.path.join(character_dir, FILE_NAME))
        self.assertEqual("改过名字", parsed["character"]["name"])
        self.assertEqual("2", parsed["character"]["version"])
        # 写失败不能留下半截文件（``_bad`` 不是合法文档名，见 doc_file_name）。
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", docs={"_bad": "x"}), ADMIN)
        parsed = read_toml(os.path.join(character_dir, FILE_NAME))
        self.assertEqual("改过名字", parsed["character"]["name"])
        self.assertEqual(
            ["character.toml", "images", "notes", "profile.md"], sorted(os.listdir(character_dir))
        )

    def test_toml_field_order_and_omitempty(self) -> None:
        self.store.write(
            None,
            WriteRequest(
                id="asset",
                name="名字",
                aliases=["a"],
                tags=["t"],
                description="d",
                owner_platform="cli",
                owner_id="cli:local",
                docs={"profile": "p"},
            ),
            ADMIN,
        )
        with open(os.path.join(self.root, "asset", FILE_NAME), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("images = []\n[character]\n"))
        order = [
            "id = ",
            "name = ",
            "aliases = ",
            "description = ",
            "tags = ",
            "owner_platform = ",
            "owner_id = ",
            "visibility = ",
            "version = ",
            "source = ",
            "created_at = ",
            "updated_at = ",
        ]
        positions = [text.index(token) for token in order]
        self.assertEqual(positions, sorted(positions))
        # 空的 [image] 段整体省略（Go 的 omitempty）。
        self.assertNotIn("[image]", text)

        self.store.write(
            None,
            WriteRequest(
                id="asset",
                image=ImageSettings(preset_prompt="P", references=["a.png"], quality="high"),
            ),
            ADMIN,
        )
        with open(os.path.join(self.root, "asset", FILE_NAME), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("[image]", text)
        parsed = read_toml(os.path.join(self.root, "asset", FILE_NAME))
        self.assertEqual("P", parsed["image"]["preset_prompt"])
        self.assertEqual(["a.png"], parsed["image"]["references"])
        self.assertEqual("high", parsed["image"]["quality"])
        self.assertNotIn("size", parsed["image"])

    def test_version_and_source_records(self) -> None:
        first = self.store.write(None, WriteRequest(id="asset", name="x"), ADMIN)
        self.assertEqual("1", first.version)
        self.assertEqual("cli", first.source)
        second = self.store.write(None, WriteRequest(id="asset", name="x"), ADMIN)
        self.assertEqual("2", second.version)
        self.assertEqual("cli", second.source)
        third = self.store.write(None, WriteRequest(id="asset", name="x", version="9"), ADMIN)
        self.assertEqual("9", third.version)
        fourth = self.store.write(None, WriteRequest(id="asset", name="x"), ADMIN)
        self.assertEqual("10", fourth.version)
        fifth = self.store.write(None, WriteRequest(id="asset", name="x", source="import"), ADMIN)
        self.assertEqual("import", fifth.source)
        # 非数字版本号在递增时回到 "1"。
        self.store.write(None, WriteRequest(id="asset", name="x", version="beta"), ADMIN)
        self.assertEqual("1", self.store.write(None, WriteRequest(id="asset", name="x"), ADMIN).version)

    def test_regular_user_owns_and_forces_private(self) -> None:
        item = self.store.write(
            None,
            WriteRequest(
                id="mine",
                name="x",
                visibility="public",
                owner_platform="other",
                owner_id="other:1",
            ),
            OWNER,
        )
        self.assertEqual(VisibilityPrivate, item.visibility)
        self.assertEqual(OWNER.platform, item.owner_platform)
        self.assertEqual(OWNER.actor_id, item.owner_id)
        with self.assertRaises(Exception) as ctx:
            self.store.write(None, WriteRequest(id="mine", name="y"), OTHER)
        self.assertIs(ErrForbidden, ctx.exception)

    def test_superadmin_can_set_owner_on_create_only(self) -> None:
        item = self.store.write(
            None,
            WriteRequest(id="owned", name="x", owner_platform="qqonebot", owner_id="qqonebot:1001"),
            ADMIN,
        )
        self.assertEqual("qqonebot", item.owner_platform)
        self.assertEqual("qqonebot:1001", item.owner_id)
        self.assertTrue(item.manageable_by(OWNER))
        # 已存在时超管不再改写 owner（与 Go 的 `existing == nil` 分支一致）。
        again = self.store.write(
            None,
            WriteRequest(id="owned", name="y", owner_platform="cli", owner_id="cli:local"),
            ADMIN,
        )
        self.assertEqual("qqonebot", again.owner_platform)

    def test_docs_add_and_remove(self) -> None:
        self.store.write(
            None,
            WriteRequest(id="asset", name="x", docs={"profile": "p", "notes/a": "n"}), ADMIN
        )
        # None = 不变；空串 = 删除。
        item = self.store.write(
            None, WriteRequest(id="asset", docs={"profile": None, "notes/a": "", "world": "w"}), ADMIN
        )
        self.assertEqual("p", item.docs["profile"].strip())
        self.assertNotIn("notes/a", item.docs)
        self.assertEqual("w", item.docs["world"].strip())
        self.assertFalse(os.path.exists(os.path.join(self.root, "asset", "notes", "a.md")))
        item = self.store.write(None, WriteRequest(id="asset", remove_docs=["world"]), ADMIN)
        self.assertNotIn("world", item.docs)
        # 非法文档名：``_`` 开头不满足 namePattern（Go 的 docFileName 同样拒绝）。
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", docs={"_bad": "x"}), ADMIN)
        # 未带 "notes/" 前缀的名字会被当成 notes 文档（Go 的 TrimPrefix 不要求前缀）。
        item = self.store.write(None, WriteRequest(id="asset", docs={"nope": "x"}), ADMIN)
        self.assertEqual("x", item.docs["notes/nope"].strip())
        self.assertTrue(os.path.exists(os.path.join(self.root, "asset", "notes", "nope.md")))

    def test_image_index_update(self) -> None:
        self.store.write(None, WriteRequest(id="asset", name="x"), ADMIN)
        self.store.add_image(None, "asset", "b.png", "image/png", "media:b", b"bb", ADMIN)
        self.store.add_image(None, "asset", "a.png", "image/png", "media:a", b"a", ADMIN)
        item = self.store.get(None, "asset")
        self.assertEqual(["a.png", "b.png"], [image.name for image in item.images])
        for image in item.images:
            self.assertEqual("1", image.version)
            self.assertEqual("cli", image.source)
            self.assertTrue(_RFC3339.match(image.created_at))
            self.assertEqual("images/" + image.name, image.path)
        replaced = self.store.add_image(None, "asset", "a.png", "image/jpeg", "media:a2", b"aaa", ADMIN)
        self.assertEqual("media:a2", replaced.media_id)
        self.assertEqual(3, replaced.size)
        item = self.store.get(None, "asset")
        self.assertEqual(2, len(item.images))
        self.assertEqual("media:a2", item.images[0].media_id)
        with open(os.path.join(self.root, "asset", "images", "a.png"), "rb") as handle:
            self.assertEqual(b"aaa", handle.read())
        # 索引项写进了 character.toml。
        parsed = read_toml(os.path.join(self.root, "asset", FILE_NAME))
        self.assertEqual(["a.png", "b.png"], [entry["name"] for entry in parsed["images"]])
        self.assertEqual("images/a.png", parsed["images"][0]["path"])

    def test_image_limits_and_bad_names(self) -> None:
        self.store.write(None, WriteRequest(id="asset", name="x"), ADMIN)
        original = character_write.MAX_IMAGES_PER_CHARACTER
        character_write.MAX_IMAGES_PER_CHARACTER = 2
        try:
            self.store.add_image(None, "asset", "a.png", "image/png", "m", b"a", ADMIN)
            self.store.add_image(None, "asset", "b.png", "image/png", "m", b"b", ADMIN)
            with self.assertRaises(CharacterError):
                self.store.add_image(None, "asset", "c.png", "image/png", "m", b"c", ADMIN)
            # 已有名字可以覆盖（不算新增）。
            self.store.add_image(None, "asset", "a.png", "image/png", "m2", b"aa", ADMIN)
        finally:
            character_write.MAX_IMAGES_PER_CHARACTER = original
        self.assertEqual(MAX_IMAGES_PER_CHARACTER, original)
        for bad in ("", "  ", ".", ".hidden", "/"):
            with self.assertRaises(CharacterError):
                self.store.add_image(None, "asset", bad, "image/png", "m", b"x", ADMIN)
        # 带目录的名字会被取基名（Go 的 filepath.Base 语义），不是报错。
        renamed = self.store.add_image(None, "asset", "dir/nested.png", "image/png", "m", b"x", ADMIN)
        self.assertEqual("nested.png", renamed.name)
        self.store.add_image(None, "asset", "  ok.png  ", "image/png", "m", b"x", ADMIN)
        self.assertTrue(os.path.exists(os.path.join(self.root, "asset", "images", "ok.png")))
        with self.assertRaises(CharacterError):
            self.store.remove_image(None, "asset", "", ADMIN)

    def test_validation_limits(self) -> None:
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", name="x" * 129), ADMIN)
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", description="x" * 501), ADMIN)
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", aliases=[f"a{i}" for i in range(21)]), ADMIN)
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", tags=["x" * 65]), ADMIN)
        with self.assertRaises(CharacterError):
            self.store.write(
                None, WriteRequest(id="asset", docs={"profile": "x" * (128 * 1024 + 1)}), ADMIN
            )
        over = {f"notes/n{index}": "x" for index in range(65)}
        with self.assertRaises(CharacterError):
            self.store.write(None, WriteRequest(id="asset", docs=over), ADMIN)

    def test_delete_removes_whole_directory(self) -> None:
        self.store.write(None, WriteRequest(id="asset", name="x", docs={"profile": "p"}), ADMIN)
        self.store.add_image(None, "asset", "a.png", "image/png", "m", b"a", ADMIN)
        self.store.delete(None, "asset", ADMIN)
        self.assertFalse(os.path.exists(os.path.join(self.root, "asset")))
        with self.assertRaises(Exception) as ctx:
            self.store.delete(None, "asset", ADMIN)
        self.assertIs(ErrNotFound, ctx.exception)

    def test_scan_cache_and_refresh(self) -> None:
        self.store.write(None, WriteRequest(id="first", name="x"), ADMIN)
        external = os.path.join(self.root, "second")
        os.makedirs(external, mode=0o777, exist_ok=True)
        with open(os.path.join(external, FILE_NAME), "w", encoding="utf-8") as handle:
            handle.write('[character]\nid = "second"\nname = "外部建的"\n')
        # 3 秒冷却内不重扫（对应 Go ``scanCooldown``）。
        self.assertGreaterEqual(SCAN_COOLDOWN, 3.0)
        self.assertEqual(["first"], [item.id for item in self.store.list(None, ADMIN)])
        self.store.refresh(None)
        self.assertEqual(["first", "second"], [item.id for item in self.store.list(None, ADMIN)])

    def test_broken_character_directory_is_skipped(self) -> None:
        self.store.write(None, WriteRequest(id="good", name="x"), ADMIN)
        broken = os.path.join(self.root, "broken")
        os.makedirs(broken, mode=0o777, exist_ok=True)
        with open(os.path.join(broken, FILE_NAME), "w", encoding="utf-8") as handle:
            handle.write("not toml at all =")
        bad_id = os.path.join(self.root, "badid")
        os.makedirs(bad_id, mode=0o777, exist_ok=True)
        with open(os.path.join(bad_id, FILE_NAME), "w", encoding="utf-8") as handle:
            handle.write('[character]\nid = "BAD ID"\n')
        self.store.refresh(None)
        self.assertEqual(["good"], [item.id for item in self.store.list(None, ADMIN)])

    def test_disabled_store(self) -> None:
        # Go 的 NewStore 会把空根目录补成 "characters"，所以默认就是启用的。
        self.assertEqual("characters", Store("").root)
        self.assertTrue(Store("").enabled())
        store = Store(".")
        store.root = ""
        self.assertFalse(store.enabled())
        with self.assertRaises(CharacterError) as ctx:
            store.write(None, WriteRequest(id="asset"), ADMIN)
        self.assertIn("not configured", str(ctx.exception))
        # 未启用时 list/manifest/refresh 都是空操作，不抛异常。
        self.assertEqual([], store.list(None, ADMIN))
        self.assertEqual([], store.manifest(None, ADMIN).characters)
        store.refresh(None)


class TestSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir(self)
        self.store = NewStore(self.root)
        self.store.write(
            None,
            WriteRequest(
                id="swordman",
                name="剑客",
                aliases=["剑士"],
                tags=["wuxia", "冷面"],
                description="冷面剑客",
                docs={"profile": "你是一位沉默寡言的剑客。", "notes/招式": "口头禅：剑不问出处。"},
            ),
            ADMIN,
        )
        self.store.write(
            None,
            WriteRequest(id="mage", name="法师", tags=["magic"], description="火系法师"), ADMIN
        )

    def test_or_and_modes(self) -> None:
        or_hits = self.store.search(None, "剑客 法师", "or", "", 10, ADMIN)
        self.assertEqual({"mage", "swordman"}, {hit.id for hit in or_hits})
        and_hits = self.store.search(None, "剑客 法师", "and", "", 10, ADMIN)
        self.assertEqual([], and_hits)
        and_hits = self.store.search(None, "剑客 口头禅", "and", "", 10, ADMIN)
        self.assertEqual(["swordman"], [hit.id for hit in and_hits])

    def test_tag_filter_and_limits(self) -> None:
        self.assertEqual(
            ["swordman"], [hit.id for hit in self.store.search(None, "剑客", "", "WUXIA", 5, ADMIN)]
        )
        self.assertEqual([], self.store.search(None, "剑客", "", "magic", 5, ADMIN))
        self.assertEqual([], self.store.search(None, "   ", "", "", 5, ADMIN))
        # limit 越界被夹到 [1, 20]，<=0 用默认值 5。
        self.assertEqual(2, len(self.store.search(None, "剑客 法师", "or", "", 100, ADMIN)))
        self.assertEqual(2, len(self.store.search(None, "剑客 法师", "or", "", 0, ADMIN)))

    def test_snippet_prefers_docs_and_carries_kind(self) -> None:
        results = self.store.search(None, "口头禅", "", "", 5, ADMIN)
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].snippet.startswith("notes/招式: "))
        self.assertIn("口头禅", results[0].snippet)
        self.assertEqual(["wuxia", "冷面"], results[0].tags)
        # 正文没命中时片段回落到 description。
        fallback = self.store.search(None, "火系", "", "", 5, ADMIN)
        self.assertEqual("火系法师", fallback[0].snippet)

    def test_scores_and_order(self) -> None:
        results = self.store.search(None, "剑客 法师", "or", "", 5, ADMIN)
        scores = [hit.score for hit in results]
        self.assertEqual(sorted(scores, reverse=True), scores)
        self.assertGreaterEqual(scores[0], scores[-1])
        # 打平则按 id 升序。
        tied = self.store.search(None, "剑客 法师", "or", "", 5, ADMIN)
        for first, second in zip(tied, tied[1:]):
            if first.score == second.score:
                self.assertLess(first.id, second.id)

    def test_snippet_truncation(self) -> None:
        from atri_x.character import SNIPPET_RUNES
        from atri_x.character.search import make_snippet

        long_text = "x" * 100 + "needle" + "y" * 300
        snippet = make_snippet(long_text, ["needle"])
        self.assertIn("needle", snippet)
        self.assertLessEqual(len(snippet), SNIPPET_RUNES + 6)
        self.assertTrue(snippet.startswith("..."))
        self.assertTrue(snippet.endswith("..."))


class TestHelpers(unittest.TestCase):
    def test_doc_file_name(self) -> None:
        for kind in ("profile", "world", "greeting", "examples", "image_prompt"):
            self.assertEqual((kind + ".md", True), doc_file_name(kind))
        self.assertEqual(("notes/招式.md", True), doc_file_name("notes/招式"))
        self.assertEqual(("notes/ok-1.md", True), doc_file_name("notes/ok-1"))
        self.assertEqual(("notes/ok.md", True), doc_file_name(" notes/ok "))
        self.assertEqual(("", False), doc_file_name("notes/"))
        self.assertEqual(("", False), doc_file_name("notes/ bad"))
        self.assertEqual(("", False), doc_file_name("_bad"))
        # Go 的 TrimPrefix 不要求前缀存在：没带 notes/ 的合法名字也会落到 notes/。
        self.assertEqual(("notes/unknown.md", True), doc_file_name("unknown"))

    def test_safe_image_name(self) -> None:
        self.assertEqual("a.png", safe_image_name("a.png"))
        self.assertEqual("a.png", safe_image_name("  a.png  "))
        self.assertEqual("a.png", safe_image_name("dir/a.png"))
        self.assertEqual("a.png", safe_image_name("dir\\a.png"))
        self.assertEqual("a.png", safe_image_name("dir/a.png/"))
        self.assertEqual("", safe_image_name(""))
        self.assertEqual("", safe_image_name("."))
        self.assertEqual("", safe_image_name(".hidden"))
        self.assertEqual("", safe_image_name("/"))

    def test_safe_join(self) -> None:
        root = make_temp_dir(self)
        self.assertEqual(os.path.join(os.path.abspath(root), "a.md"), safe_join(root, "a.md"))
        self.assertEqual(
            os.path.join(os.path.abspath(root), "notes", "a.md"), safe_join(root, "notes/a.md")
        )
        for bad in ("../escape", "notes/../../escape", "..", "sub/../../x"):
            with self.assertRaises(CharacterError):
                safe_join(root, bad)

    def test_next_version_and_visibility(self) -> None:
        self.assertEqual("1", next_version(""))
        self.assertEqual("1", next_version("abc"))
        self.assertEqual("1", next_version("0"))
        self.assertEqual("1", next_version("-3"))
        self.assertEqual("8", next_version("7"))
        self.assertEqual("11", next_version("+10"))
        self.assertEqual(VisibilityPrivate, normalize_visibility("PRIVATE"))
        self.assertEqual(VisibilityPublic, normalize_visibility("junk"))
        self.assertEqual(VisibilityPublic, normalize_visibility(""))

    def test_prompt_truncation_and_image_prompt(self) -> None:
        root = make_temp_dir(self)
        store = NewStore(root)
        item = store.write(
            None,
            WriteRequest(
                id="asset",
                name="名字",
                aliases=["别名"],
                tags=["tag"],
                description="简介",
                docs={"profile": "资料", "image_prompt": "IMG"},
                image=ImageSettings(preset_prompt="PRESET"),
            ),
            ADMIN,
        )
        prompt = item.prompt(0)
        self.assertTrue(prompt.startswith("[角色设定]\nid: asset\nname: 名字"))
        self.assertIn("aliases: 别名", prompt)
        self.assertIn("tags: tag", prompt)
        self.assertIn("## profile", prompt)
        # docOrder 里确实有 image_prompt（Go 的 docOrder 是五项），所以 persona
        # 块也会带上它；image_prompt 另外还参与 image_prompt() 的拼接。
        self.assertIn("## image_prompt", prompt)
        truncated = item.prompt(20)
        self.assertTrue(truncated.endswith("...[角色设定过长，已截断]"))
        self.assertEqual(len(truncated) - len("\n...[角色设定过长，已截断]"), 20)
        self.assertEqual("PRESET\n\nIMG", item.image_prompt())

    def test_active_ids_context(self) -> None:
        class Ctx:
            """鸭子类型 ctx：带 with_value / value（对应 CancelToken）。"""

            def __init__(self, values: dict[Any, Any] | None = None) -> None:
                self.values = dict(values or {})

            def with_value(self, key: Any, value: Any) -> "Ctx":
                return Ctx({**self.values, key: value})

            def value(self, key: Any, default: Any = None) -> Any:
                return self.values.get(key, default)

        self.assertEqual([], ActiveIDs(None))
        # ctx 为 None 时 WithActive 原样返回。
        self.assertIsNone(WithActive(None, "a"))
        ctx = WithActive(Ctx(), "SwordMan", "swordman", "", "Mage")
        self.assertEqual(["swordman", "mage"], ActiveIDs(ctx))
        ctx = WithActive(ctx, "Third")
        self.assertEqual(["swordman", "mage", "third"], ActiveIDs(ctx))
        # Mapping 形态的 ctx 也支持。
        self.assertEqual(["swordman", "mage", "third"], ActiveIDs(dict(ctx.values)))
        self.assertEqual([], ActiveIDs(object()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
