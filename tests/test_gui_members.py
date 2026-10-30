"""群友角色数据用例：档案读写、从聊天历史发现群友、头像缓存与转换。

数据层（:mod:`atri_x.gui.memberdata` / :mod:`atri_x.gui.avatars`）不需要 Tk；聊天历史库
用真实的 SQLite 建表（``new_chat_history``）再手工插几行消息，验证聚合口径。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
from pathlib import Path

from atri_x.gui import avatars, memberdata
from atri_x.media.imagecodec import Image, encode_jpeg, encode_png
from atri_x.storage.sqlite.chat_history_repository import new_chat_history

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_members"


class _FakePaths:
    """只用得到 ``chat_history_db_path`` 的路径对象。"""

    def __init__(self, db_path: str) -> None:
        self.chat_history_db_path = db_path


def _make_chat_db(path: Path, rows: list[tuple]) -> None:
    """建一张真的 ``chat_messages`` 表并插入 ``(platform, scope, type, msg_id, sender, name, text)``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    store = new_chat_history(str(path))
    store.close()
    connection = sqlite3.connect(str(path))
    try:
        for index, (platform, scope, scope_type, message_id, sender, name, text) in enumerate(rows):
            connection.execute(
                "INSERT INTO chat_messages (id, platform, platform_scope_id, scope_type,"
                " platform_message_id, sender_id, sender_name, text, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"msg-{index}",
                    platform,
                    scope,
                    scope_type,
                    message_id,
                    sender,
                    name,
                    text,
                    f"2026-01-0{index + 1}T12:00:00Z",
                ),
            )
        connection.commit()
    finally:
        connection.close()


class MemberCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.config_dir = TEMP_ROOT / "config"
        self.config_dir.mkdir(parents=True)
        self.db_path = TEMP_ROOT / "data" / "atrix_chat_history.db"

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


class ProfileStoreCase(MemberCase):
    """``gui/members.json`` 的增删改查。"""

    def test_empty_store(self) -> None:
        self.assertEqual(memberdata.load_profiles(str(self.config_dir)), {})
        self.assertFalse(memberdata.delete_profile(str(self.config_dir), "nope"))

    def test_upsert_roundtrip_and_timestamp(self) -> None:
        saved = memberdata.upsert_profile(
            str(self.config_dir),
            memberdata.MemberProfile(
                platform="qqonebot",
                scope_id="g1",
                user_id="1001",
                display_name="小明",
                alias="小明明",
                tags=["群友", "爱画图", "群友"],
                profile="  性格活泼  ",
            ),
        )
        self.assertTrue(saved.updated_at)
        profiles = memberdata.load_profiles(str(self.config_dir))
        self.assertIn(saved.key, profiles)
        stored = profiles[saved.key]
        self.assertEqual(stored.alias, "小明明")
        self.assertEqual(stored.tags, ["群友", "爱画图"])  # 去重
        self.assertEqual(stored.profile, "性格活泼")  # 去首尾空白
        self.assertEqual(stored.title, "小明明")

    def test_key_is_platform_scope_user(self) -> None:
        self.assertEqual(
            memberdata.member_key("qqonebot", "g1", "1001"), "qqonebot/g1/1001"
        )
        profile = memberdata.MemberProfile(platform="p", scope_id="s", user_id="u")
        self.assertEqual(profile.key, "p/s/u")

    def test_profile_is_truncated(self) -> None:
        saved = memberdata.upsert_profile(
            str(self.config_dir),
            memberdata.MemberProfile(
                platform="p", scope_id="s", user_id="u", profile="字" * 5000
            ),
        )
        self.assertEqual(len(saved.profile), memberdata.MAX_PROFILE_RUNES)

    def test_tags_are_limited(self) -> None:
        saved = memberdata.upsert_profile(
            str(self.config_dir),
            memberdata.MemberProfile(
                platform="p", scope_id="s", user_id="u", tags=[f"t{index}" for index in range(30)]
            ),
        )
        self.assertEqual(len(saved.tags), memberdata.MAX_TAGS)

    def test_missing_ids_rejected(self) -> None:
        with self.assertRaises(memberdata.MemberDataError):
            memberdata.upsert_profile(
                str(self.config_dir), memberdata.MemberProfile(platform="", user_id="u")
            )

    def test_empty_table_is_written(self) -> None:
        path = Path(memberdata.members_path(str(self.config_dir)))
        self.assertFalse(path.exists())
        memberdata.save_profiles(str(self.config_dir), {})
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["members"], {})

    def test_corrupt_file_degrades(self) -> None:
        path = Path(memberdata.members_path(str(self.config_dir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(memberdata.load_profiles(str(self.config_dir)), {})

    def test_delete_removes_profile_and_avatar(self) -> None:
        profile = memberdata.MemberProfile(
            platform="qqonebot", scope_id="g1", user_id="1001", display_name="小明"
        )
        memberdata.set_avatar(str(self.config_dir), profile, b"\x89PNG\r\n")
        saved = memberdata.upsert_profile(str(self.config_dir), profile)
        avatar_path = Path(avatars.avatar_file(str(self.config_dir), saved.avatar))
        self.assertTrue(avatar_path.is_file())
        self.assertTrue(memberdata.delete_profile(str(self.config_dir), saved.key))
        self.assertFalse(avatar_path.exists())
        self.assertEqual(memberdata.load_profiles(str(self.config_dir)), {})


class DiscoveryCase(MemberCase):
    """从聊天历史库聚合群/群友。"""

    def setUp(self) -> None:
        super().setUp()
        _make_chat_db(
            self.db_path,
            [
                ("qqonebot", "g1", "group", "m1", "1001", "小明旧", "在吗"),
                ("qqonebot", "g1", "group", "m2", "1001", "小明", "在的"),
                ("qqonebot", "g1", "group", "m3", "1002", "小红", "我也在"),
                ("qqonebot", "g2", "group", "m4", "1003", "阿强", "另一个群"),
                ("telegram", "chat-9", "private", "m5", "2001", "TG", "私聊"),
            ],
        )
        self.paths = _FakePaths(str(self.db_path))

    def test_list_scopes(self) -> None:
        scopes = memberdata.list_scopes(self.paths)
        self.assertEqual(len(scopes), 3)
        self.assertEqual(scopes[0].scope_id, "g1")
        self.assertEqual(scopes[0].messages, 3)
        self.assertEqual(scopes[0].members, 2)
        self.assertIn("群", scopes[0].label)
        self.assertIn("私聊", memberdata.list_scopes(self.paths)[-1].label)

    def test_discover_members_names_and_counts(self) -> None:
        members = memberdata.discover_members(self.paths, "qqonebot", "g1")
        self.assertEqual([item.user_id for item in members], ["1001", "1002"])
        self.assertEqual(members[0].message_count, 2)
        # 昵称取最近一条消息里的名字
        self.assertEqual(members[0].display_name, "小明")
        self.assertEqual(members[0].last_at, "2026-01-02T12:00:00Z")
        self.assertEqual(members[1].display_name, "小红")

    def test_discover_is_scope_limited(self) -> None:
        self.assertEqual(memberdata.discover_members(self.paths, "qqonebot", "g2")[0].user_id, "1003")
        self.assertEqual(memberdata.discover_members(self.paths, "nope", "nowhere"), [])

    def test_missing_db_is_explained(self) -> None:
        with self.assertRaises(memberdata.MemberDataError) as caught:
            memberdata.list_scopes(_FakePaths(str(TEMP_ROOT / "nope.db")))
        self.assertIn("找不到聊天历史库", str(caught.exception))


class AvatarCase(MemberCase):
    """头像：URL 规则、JDEG→PNG 转换、缓存落盘。"""

    def _png(self, size: int = 8) -> bytes:
        pixels = bytearray([180, 40, 60] * (size * size))
        return encode_png(Image(size, size, "RGB", pixels))

    def test_url_rules(self) -> None:
        url = avatars.avatar_url("qqonebot", "10001")
        self.assertIn("10001", url)
        self.assertTrue(url.startswith("https://"))
        self.assertEqual(avatars.avatar_url("telegram", "10001"), "")
        self.assertEqual(avatars.avatar_url("qqonebot", "not-a-number"), "")

    def test_url_size_is_accepted_by_qq_endpoint(self) -> None:
        """回归：``s=96`` 会被 qlogo 回 HTTP 400（X-ErrNo -105），请求边长必须吸附档位。"""
        sizes = [1, 39, 40, 41, 96, 100, 101, 639, 640, 641, 99999]
        for size in sizes:
            url = avatars.avatar_url("qqonebot", "10001", size=size)
            self.assertIn(f"s={avatars._request_size(size)}", url)
            self.assertIn(avatars._request_size(size), avatars.QQ_AVATAR_SIZES)
        # 默认（本地缓存边长 96）请求 100：服务端接受的最小一档，缩到 96 只降不升。
        self.assertIn("s=100", avatars.avatar_url("qqonebot", "10001"))
        self.assertNotIn("s=96", avatars.avatar_url("qqonebot", "10001"))
        self.assertEqual(avatars.QQ_AVATAR_SIZES, (40, 100, 140, 640))

    def test_to_png_accepts_png_and_jpeg(self) -> None:
        png = self._png(10)
        jpeg = encode_jpeg(Image(10, 10, "RGB", bytearray([10, 200, 30] * 100)))
        for data in (png, jpeg):
            result = avatars.to_png(data)
            self.assertTrue(result.startswith(b"\x89PNG"))

    def test_to_png_resizes_to_avatar_size(self) -> None:
        from atri_x.media.imagecodec import decode

        image = decode(avatars.to_png(self._png(40)))
        self.assertLessEqual(image.width, avatars.AVATAR_SIZE)
        self.assertLessEqual(image.height, avatars.AVATAR_SIZE)

    def test_to_png_rejects_garbage(self) -> None:
        with self.assertRaises(avatars.AvatarError):
            avatars.to_png(b"not an image")

    def test_cache_and_clear(self) -> None:
        relative = avatars.cache_avatar(str(self.config_dir), "qqonebot/g1/1001", self._png())
        self.assertTrue(relative.startswith(avatars.AVATAR_DIR + "/"))
        path = Path(avatars.avatar_file(str(self.config_dir), relative))
        self.assertTrue(path.is_file())
        self.assertTrue(avatars.clear_avatar(str(self.config_dir), relative))
        self.assertFalse(path.exists())
        self.assertFalse(avatars.clear_avatar(str(self.config_dir), ""))

    def test_cache_avatar_file_from_local_image(self) -> None:
        source = TEMP_ROOT / "local.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(encode_jpeg(Image(6, 6, "RGB", bytearray([1, 2, 3] * 36))))
        relative = avatars.cache_avatar_file(str(self.config_dir), "k", str(source))
        self.assertTrue(Path(avatars.avatar_file(str(self.config_dir), relative)).is_file())

    def test_empty_url_rejected(self) -> None:
        with self.assertRaises(avatars.AvatarError):
            avatars.fetch_avatar_png("")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
