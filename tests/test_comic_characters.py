"""人物与参考图绑定测试（P0-3）。

必须钉住的边界：

* **同名不同人不串**：身份键用 ``member:<platform>:<sender_id>``，不是显示名；
* 用户说"就用头像"（``mode=avatars``）时不能被换成角色卡；
* 头像是可选平台能力：没有能力时 ``auto`` 明确标 ``source=none`` 并告警，
  ``avatars`` 直接报错——都不许假装"已按头像绘制"；
* 本次显式参考图优先级最高；超过 4 个人物直接拒绝，不静默截掉。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.character import Image, ImageSettings, Viewer
from atri_x.comic import (
    CharacterResolveRequest,
    ComicCharacterError,
    ComicContext,
    ComicContextMessage,
    parse_reference_overrides,
    resolve_comic_characters,
)
from tests._comic_fakes import FakeAvatarResolver, FakeCharacterStore, make_character

VIEWER = Viewer(platform="qqonebot", actor_id="admin", superadmin=True)


def _context(members) -> ComicContext:
    messages = []
    for index, (sender_id, sender_name) in enumerate(members):
        messages.append(
            ComicContextMessage(
                message_id=f"msg-{index + 1}",
                seq=index + 1,
                sender_id=sender_id,
                sender_name=sender_name,
                text=f"第{index + 1}句",
            )
        )
    return ComicContext(
        platform="qqonebot",
        scope_id="group-1",
        messages=messages,
    )


def _resolve(context, store=None, resolver=None, **kwargs):
    request = CharacterResolveRequest(**kwargs)
    return asyncio.run(
        resolve_comic_characters(
            None,
            context=context,
            request=request,
            store=store,
            viewer=VIEWER,
            avatar_resolver=resolver,
        )
    )


class IdentityTest(unittest.TestCase):
    def test_same_display_name_but_different_ids_do_not_merge(self) -> None:
        context = _context([("1001", "小明"), ("2002", "小明")])
        resolver = FakeAvatarResolver(
            {("qqonebot", "1001"): "https://a/1.png", ("qqonebot", "2002"): "https://a/2.png"}
        )
        resolved = _resolve(context, resolver=resolver, mode="avatars")
        self.assertEqual(len(resolved.characters), 2)
        self.assertEqual(
            [item.id for item in resolved.characters],
            ["member:qqonebot:1001", "member:qqonebot:2002"],
        )
        self.assertEqual(len({item.id for item in resolved.characters}), 2)


class ModeTest(unittest.TestCase):
    def test_auto_prefers_character_card_over_avatar(self) -> None:
        store = FakeCharacterStore(
            [
                make_character(
                    "atri",
                    "小明",
                    images=[Image(name="avatar.png", mime_type="image/png")],
                )
            ]
        )
        context = _context([("1001", "小明")])
        resolved = _resolve(
            context,
            store=store,
            resolver=FakeAvatarResolver({("qqonebot", "1001"): "https://a.png"}),
            mode="auto",
        )
        self.assertEqual(len(resolved.characters), 1)
        character = resolved.characters[0]
        self.assertEqual(character.source, "character")
        self.assertEqual(character.character_id, "atri")
        self.assertEqual(character.reference.kind, "character_image")
        self.assertEqual(character.reference.source, "avatar.png")

    def test_avatars_mode_does_not_fall_back_to_card(self) -> None:
        store = FakeCharacterStore(
            [make_character("atri", "小明", images=[Image(name="card.png")])]
        )
        context = _context([("1001", "小明")])
        resolver = FakeAvatarResolver({("qqonebot", "1001"): "https://a/1.png"})
        resolved = _resolve(context, store=store, resolver=resolver, mode="avatars")
        character = resolved.characters[0]
        self.assertEqual(character.source, "avatar")
        self.assertEqual(character.reference.kind, "avatar")
        self.assertEqual(character.character_id, "")

    def test_avatars_mode_without_resolver_is_an_error(self) -> None:
        context = _context([("1001", "小明")])
        with self.assertRaises(ComicCharacterError) as caught:
            _resolve(context, mode="avatars")
        self.assertIn("头像", str(caught.exception))

    def test_avatars_mode_missing_url_is_an_error(self) -> None:
        context = _context([("1001", "小明"), ("1002", "小红")])
        resolver = FakeAvatarResolver({("qqonebot", "1001"): "https://a/1.png"})
        with self.assertRaises(ComicCharacterError) as caught:
            _resolve(context, resolver=resolver, mode="avatars")
        self.assertIn("小红", str(caught.exception))

    def test_auto_without_card_or_avatar_marks_none(self) -> None:
        context = _context([("1001", "路人")])
        resolved = _resolve(context, store=FakeCharacterStore(), mode="auto")
        character = resolved.characters[0]
        self.assertEqual(character.source, "none")
        self.assertEqual(character.reference.kind, "none")
        self.assertTrue(any("没有参考图" in item for item in resolved.warnings))

    def test_characters_mode_requires_matching_cards(self) -> None:
        context = _context([("1001", "小明")])
        with self.assertRaises(ComicCharacterError):
            _resolve(context, store=FakeCharacterStore(), mode="characters")

    def test_characters_mode_skips_unmatched_with_warning(self) -> None:
        store = FakeCharacterStore([make_character("atri", "小明")])
        context = _context([("1001", "小明"), ("1002", "小红")])
        resolved = _resolve(context, store=store, mode="characters")
        self.assertEqual([item.display_name for item in resolved.characters], ["小明"])
        self.assertTrue(any("小红" in item for item in resolved.warnings))

    def test_alias_matches_card(self) -> None:
        store = FakeCharacterStore([make_character("atri", "亚托莉", aliases=["Atri"])])
        context = _context([("1001", "atri")])
        resolved = _resolve(context, store=store, mode="auto")
        self.assertEqual(resolved.characters[0].character_id, "atri")

    def test_card_without_images_warns(self) -> None:
        store = FakeCharacterStore([make_character("atri", "小明")])
        context = _context([("1001", "小明")])
        resolved = _resolve(context, store=store, mode="auto")
        self.assertEqual(resolved.characters[0].reference.kind, "none")
        self.assertTrue(any("没有图片" in item for item in resolved.warnings))


class ExplicitTest(unittest.TestCase):
    def test_character_ids_are_respected(self) -> None:
        store = FakeCharacterStore(
            [
                make_character("atri", "亚托莉", images=[Image(name="a.png")]),
                make_character("other", "别人", images=[Image(name="b.png")]),
            ]
        )
        context = _context([("1001", "小明")])
        resolved = _resolve(context, store=store, character_ids=["other"], mode="characters")
        self.assertEqual([item.character_id for item in resolved.characters], ["other"])

    def test_missing_character_id_is_an_error(self) -> None:
        store = FakeCharacterStore([make_character("atri", "亚托莉")])
        context = _context([("1001", "小明")])
        with self.assertRaises(ComicCharacterError):
            _resolve(context, store=store, character_ids=["nope"], mode="characters")

    def test_character_ids_conflict_with_avatars_mode(self) -> None:
        context = _context([("1001", "小明")])
        with self.assertRaises(ComicCharacterError):
            _resolve(
                context,
                store=FakeCharacterStore(),
                character_ids=["atri"],
                mode="avatars",
            )

    def test_max_characters_is_enforced_for_avatars(self) -> None:
        context = _context([(f"10{index}", f"人{index}") for index in range(5)])
        resolver = FakeAvatarResolver(
            {("qqonebot", f"10{index}"): f"https://a/{index}.png" for index in range(5)}
        )
        with self.assertRaises(ComicCharacterError) as caught:
            _resolve(context, resolver=resolver, mode="avatars", max_characters=4)
        self.assertIn("最多 4 个", str(caught.exception))

    def test_private_character_forbidden_surfaces(self) -> None:
        from atri_x.errors import ForbiddenError

        class PrivateStore(FakeCharacterStore):
            def get_visible(self, ctx, id_value, viewer=None):
                raise ForbiddenError("private character")

        context = _context([("1001", "小明")])
        with self.assertRaises(ComicCharacterError) as caught:
            _resolve(
                context,
                store=PrivateStore(),
                character_ids=["someone-private"],
                mode="characters",
            )
        self.assertIn("可访问", str(caught.exception))

    def test_explicit_reference_overrides_card(self) -> None:
        store = FakeCharacterStore(
            [make_character("atri", "小明", images=[Image(name="card.png")])]
        )
        context = _context([("1001", "小明")])
        media_id = "media:" + "b" * 64
        resolved = _resolve(
            context,
            store=store,
            mode="auto",
            reference_images=[f"atri={media_id}"],
        )
        self.assertEqual(resolved.characters[0].reference.kind, "media")
        self.assertEqual(resolved.characters[0].reference.source, media_id)


class ReferenceParseTest(unittest.TestCase):
    def test_scoped_and_bare_references(self) -> None:
        media_id = "media:" + "c" * 64
        scoped, bare = parse_reference_overrides(
            [f"atri={media_id}", "other=extra.png", "avatar2.png"]
        )
        self.assertEqual(scoped["atri"].kind, "media")
        self.assertEqual(scoped["other"].kind, "character_image")
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0].kind, "character_image")

    def test_invalid_media_id_is_rejected(self) -> None:
        with self.assertRaises(ComicCharacterError):
            parse_reference_overrides(["media:not-a-hash"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
