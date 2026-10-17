"""``character_*`` / ``artist_extract`` / ``artist_library_*`` 的单元测试。

对应 Go 测试：``internal/tool/builtin/character_test.go``、
``internal/tool/builtin/artist_extract_test.go``、
``internal/tool/builtin/artist_library_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.artist import Service as ArtistService
from atri_x.artist import Store as ArtistStore
from atri_x.artist import Viewer as ArtistViewer
from atri_x.character import NewStore as new_character_store
from atri_x.character import Viewer as CharacterViewer
from atri_x.character import WriteRequest as CharacterWriteRequest
from atri_x.tool.builtin.artist_extract import ArtistExtractTool, artist_extract_text
from atri_x.tool.builtin.artist_library import new_artist_library_tools
from atri_x.tool.builtin.artist_tools import ArtistTools, artist_viewer
from atri_x.tool.builtin.character import (
    CharacterDeleteTool,
    CharacterListTool,
    CharacterManageTool,
    CharacterReadTool,
    CharacterSearchTool,
    character_not_found_text,
    character_viewer,
    new_character_tools,
)
from atri_x.tool.builtin.common import go_json
from atri_x.tool.tool import CallRequest, RoleSuperadmin, WithActor
from atri_x.tool.tool import Actor

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_character_artist_test")

#: 一个没有平台信息、但已是超级管理员的上下文：所有可见性检查都会通过。
ctx = WithActor(None, Actor(id="admin", platform="test", role=RoleSuperadmin))


def _run(coro):
    return asyncio.run(coro)


def _req(payload: dict) -> CallRequest:
    return CallRequest(arguments=json.dumps(payload, ensure_ascii=False))


class CharacterToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.store = new_character_store(os.path.join(TMP_ROOT, "characters"))
        self.store.write(
            ctx,
            CharacterWriteRequest(
                id="atri",
                name="亚托莉",
                aliases=["Atri"],
                description="爱吃西瓜的机器人",
                tags=["main"],
                docs={"profile": "你是亚托莉。", "greeting": "你好。"},
            ),
            CharacterViewer(superadmin=True),
        )
        self.deps = __import__(
            "atri_x.tool.builtin.character", fromlist=["CharacterTools"]
        ).CharacterTools(store=self.store, media=None)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_new_character_tools_names(self) -> None:
        names = [tool.name() for tool in new_character_tools(self.store, None)]
        self.assertEqual(
            names,
            [
                "character_list",
                "character_read",
                "character_search",
                "character_manage",
                "character_delete",
            ],
        )

    def test_viewer_defaults_to_anonymous(self) -> None:
        viewer = character_viewer(None)
        self.assertEqual(viewer.actor_id, "")
        self.assertFalse(viewer.superadmin)

    def test_list_renders_summary(self) -> None:
        result = _run(CharacterListTool(self.deps).call(ctx, CallRequest()))
        self.assertIn("共 1 个可见", result.content)
        self.assertIn("atri | 亚托莉", result.content)
        self.assertIn("aliases=Atri", result.content)
        self.assertIn("@char:<id>", result.content)

    def test_list_query_filter_and_unknown_tag(self) -> None:
        tool = CharacterListTool(self.deps)
        hit = _run(tool.call(ctx, _req({"query": "西瓜"})))
        self.assertIn("共 1 个可见", hit.content)
        miss = _run(tool.call(ctx, _req({"tag": "nope"})))
        self.assertIn("共 0 个可见", miss.content)

    def test_list_rejects_project_keys(self) -> None:
        # 传了未知键不会炸（decode_args 忽略未知键），但也不会改变结果。
        result = _run(CharacterListTool(self.deps).call(ctx, _req({"nope": 1})))
        self.assertIn("共 1 个可见", result.content)

    def test_read_defaults_to_all_sections(self) -> None:
        result = _run(CharacterReadTool(self.deps).call(ctx, _req({"id": "atri"})))
        text = "".join(segment.text for segment in result.segments)
        self.assertIn("## profile", text)
        self.assertIn("## greeting", text)
        self.assertIn("角色 亚托莉（atri）", text)

    def test_read_single_section_and_unknown_section(self) -> None:
        tool = CharacterReadTool(self.deps)
        result = _run(tool.call(ctx, _req({"id": "atri", "section": "profile"})))
        text = "".join(segment.text for segment in result.segments)
        self.assertIn("## profile", text)
        self.assertNotIn("## greeting", text)
        with self.assertRaises(Exception):
            _run(tool.call(ctx, _req({"id": "atri", "section": "nope"})))

    def test_read_missing_character_returns_text(self) -> None:
        result = _run(CharacterReadTool(self.deps).call(ctx, _req({"id": "nope"})))
        self.assertEqual(result.content, character_not_found_text("nope"))

    def test_read_include_images_without_images(self) -> None:
        result = _run(
            CharacterReadTool(self.deps).call(ctx, _req({"id": "atri", "include_images": True}))
        )
        texts = [segment.text for segment in result.segments if segment.text]
        self.assertIn("（该角色没有图片）", texts)

    def test_search_hit_and_miss(self) -> None:
        tool = CharacterSearchTool(self.deps)
        hit = _run(tool.call(ctx, _req({"query": "亚托莉"})))
        self.assertIn("命中 1 个角色", hit.content)
        miss = _run(tool.call(ctx, _req({"query": "zzz-none"})))
        self.assertEqual(miss.content, "没有找到匹配的角色。")

    def test_manage_create_then_update_docs(self) -> None:
        tool = CharacterManageTool(self.deps)
        created = _run(
            tool.call(
                ctx,
                _req(
                    {
                        "operation": "create",
                        "id": "yuki",
                        "name": "小雪",
                        "visibility": "public",
                        "docs": {"profile": "你是小雪。"},
                    }
                ),
            )
        )
        self.assertIn("角色已保存：小雪", created.content)
        updated = _run(
            tool.call(
                ctx,
                _req(
                    {
                        "operation": "update",
                        "id": "yuki",
                        "name": "小雪",
                        "docs": {"world": "冬天。"},
                    }
                ),
            )
        )
        self.assertIn("文档 2 个", updated.content)

    def test_manage_requires_known_operation(self) -> None:
        result = _run(
            CharacterManageTool(self.deps).call(ctx, _req({"operation": "nope", "id": "x"}))
        )
        self.assertIn("operation 必须是", result.content)

    def test_manage_add_image_requires_media_center(self) -> None:
        result = _run(
            CharacterManageTool(self.deps).call(
                ctx,
                _req(
                    {
                        "operation": "add_image",
                        "id": "atri",
                        "image_source": "media:" + "a" * 64,
                    }
                ),
            )
        )
        self.assertIn("Media Center 未配置", result.content)

    def test_manage_add_image_needs_source(self) -> None:
        from atri_x.tool.builtin.character import CharacterTools

        # Go 先查 Media Center，再查 image_source；这里给一个假的 media 让检查走到第二步。
        deps = CharacterTools(store=self.store, media=object())
        result = _run(
            CharacterManageTool(deps).call(
                ctx, _req({"operation": "add_image", "id": "atri"})
            )
        )
        self.assertIn("add_image 需要 image_source", result.content)

    def test_manage_remove_image_needs_name(self) -> None:
        result = _run(
            CharacterManageTool(self.deps).call(
                ctx, _req({"operation": "remove_image", "id": "atri"})
            )
        )
        self.assertIn("remove_image 需要 image_name", result.content)

    def test_delete_removes_character(self) -> None:
        result = _run(
            CharacterDeleteTool(self.deps).call(ctx, _req({"id": "atri"}))
        )
        self.assertEqual(result.content, "已删除角色 atri。")
        missing = _run(
            CharacterReadTool(self.deps).call(ctx, _req({"id": "atri"}))
        )
        self.assertEqual(missing.content, character_not_found_text("atri"))

    def test_disabled_store_short_circuits(self) -> None:
        from atri_x.tool.builtin.character import CharacterTools

        # store 为 None 时 enabled() 为 False（Go 里是 nil *Store）。
        deps = CharacterTools(store=None, media=None)
        for tool in new_character_tools(None, None):
            result = _run(tool.call(ctx, CallRequest()))
            self.assertEqual(result.content, "角色素材库未配置。")
        self.assertFalse(deps.enabled())

    def test_artist_viewer_accepts_superadmin(self) -> None:
        viewer = artist_viewer(ctx)
        self.assertTrue(viewer.superadmin)
        self.assertEqual(viewer.actor_id, "admin")


class ArtistLibraryToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        library_path = os.path.join(TMP_ROOT, "artist_library.json")
        self.service = ArtistService(ArtistStore(library_path))
        self.deps = ArtistTools(service=self.service, media=None, vision=None)
        self.tools = {tool.name(): tool for tool in new_artist_library_tools(self.deps)}
        self.viewer = ArtistViewer(
            actor_id="admin", platform="test", superadmin=True, scope_id="room-1"
        )

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_disabled_library_returns_shared_message(self) -> None:
        deps = ArtistTools(service=ArtistService(ArtistStore("")))
        tool = {item.name(): item for item in new_artist_library_tools(deps)}[
            "artist_library_search"
        ]
        result = _run(tool.call(ctx, CallRequest()))
        self.assertIn("画师串库未启用", result.content)

    def test_search_lists_all_when_query_empty(self) -> None:
        self.service.save(
            self.viewer,
            _preset("soft", artists=[("artist_a", 1.0)]),
            0,
        )
        result = _run(
            self.tools["artist_library_search"].call(ctx, CallRequest())
        )
        payload = json.loads(result.data)
        self.assertEqual(len(payload["matches"]), 1)
        self.assertEqual(payload["matches"][0]["kind"], "preset")
        self.assertIn("找到 1 条", result.content)

    def test_search_kind_filter_and_limit(self) -> None:
        self.service.save(self.viewer, _preset("soft", artists=[("artist_a", 1.0)]), 0)
        result = _run(
            self.tools["artist_library_search"].call(
                ctx, _req({"kind": "artist", "limit": 1})
            )
        )
        payload = json.loads(result.data)
        self.assertEqual(payload["matches"], [])
        # Go 按 query 而不是按过滤后的结果挑文案：空 query + kind=artist 会得到
        # 「库里还没有可见的条目」。
        self.assertIn("画师串库里还没有可见的条目", result.content)

    def test_search_filter_with_query_reports_no_match(self) -> None:
        self.service.save(self.viewer, _preset("soft", artists=[("artist_a", 1.0)]), 0)
        result = _run(
            self.tools["artist_library_search"].call(
                ctx, _req({"kind": "artist", "query": "soft"})
            )
        )
        self.assertEqual(json.loads(result.data)["matches"], [])
        self.assertIn("没有匹配的画师或组合", result.content)

    def test_read_renders_dialect_and_revision(self) -> None:
        _register_artists(self.service, "artist_a")
        saved = self.service.save(
            self.viewer, _preset("soft", artists=[("artist_a", 1.2)]), 0
        )
        result = _run(
            self.tools["artist_library_read"].call(
                ctx, _req({"id": saved.id, "dialect": "sd_weighted"})
            )
        )
        self.assertIn(f"revision：{saved.revision}", result.content)
        self.assertIn("渲染正向", result.content)
        payload = json.loads(result.data)
        self.assertEqual(payload["id"], saved.id)
        self.assertEqual(payload["artists"][0]["artist_id"], saved.artists[0].artist_id)
        self.assertEqual(payload["artists"][0]["tag"], "artist_a")

    def test_read_requires_id(self) -> None:
        result = _run(self.tools["artist_library_read"].call(ctx, CallRequest()))
        self.assertEqual(result.content, "id 不能为空。")

    def test_read_rejects_over_long_id(self) -> None:
        result = _run(
            self.tools["artist_library_read"].call(ctx, _req({"id": "x" * 200}))
        )
        self.assertEqual(result.content, "id 过长。")

    def test_read_conflict_message(self) -> None:
        saved = self.service.save(
            self.viewer, _preset("soft", artists=[("artist_a", 1.0)]), 0
        )
        self.service.save(
            self.viewer, _preset("soft", artists=[("artist_a", 1.0)], id=saved.id), saved.revision
        )
        replayed = _preset("soft", artists=[("artist_a", 1.0)], id=saved.id)
        result = _run(
            self.tools["artist_library_save"].call(
                ctx,
                _req(
                    {
                        "name": replayed.name,
                        "id": saved.id,
                        "expected_revision": saved.revision,
                        "artists": [{"artist_id": "artist_a", "weight": 1.0}],
                    }
                ),
            )
        )
        self.assertIn("已被修改", result.content)

    def test_save_requires_name(self) -> None:
        result = _run(self.tools["artist_library_save"].call(ctx, CallRequest()))
        self.assertEqual(result.content, "name 不能为空。")

    def test_save_creates_artist_from_tag(self) -> None:
        result = _run(
            self.tools["artist_library_save"].call(
                ctx,
                _req(
                    {
                        "name": "粉彩",
                        "artists": [{"tag": "sakura", "weight": 1.4}],
                        "positive_tags": ["soft light"],
                    }
                ),
            )
        )
        self.assertIn("已保存画师串组合：粉彩", result.content)
        payload = json.loads(result.data)
        self.assertEqual(payload["artists"][0]["tag"], "sakura")
        self.assertEqual(payload["artists"][0]["weight"], 1.4)

    def test_save_requires_artist_id_or_tag(self) -> None:
        result = _run(
            self.tools["artist_library_save"].call(
                ctx, _req({"name": "x", "artists": [{"weight": 1.0}]})
            )
        )
        self.assertIn("artists 里每一项都需要 artist_id 或 tag", result.content)

    def test_save_pending_warning(self) -> None:
        result = _run(
            self.tools["artist_library_save"].call(
                ctx,
                _req(
                    {
                        "name": "候选",
                        "status": "pending",
                        "raw_string": "(artist_a:1.1)",
                    }
                ),
            )
        )
        self.assertIn("状态是 pending", result.content)

    def test_delete_requires_confirmation(self) -> None:
        saved = self.service.save(
            self.viewer, _preset("soft", artists=[("artist_a", 1.0)]), 0
        )
        tool = self.tools["artist_library_delete"]
        first = _run(tool.call(ctx, _req({"id": saved.id})))
        self.assertIn("将要删除画师串组合", first.content)
        self.assertIn("confirm=true", first.content)
        second = _run(tool.call(ctx, _req({"id": saved.id, "confirm": True})))
        self.assertIn("已删除画师串组合", second.content)
        payload = json.loads(second.data)
        self.assertEqual(payload["deleted"], saved.id)

    def test_delete_requires_id(self) -> None:
        result = _run(self.tools["artist_library_delete"].call(ctx, CallRequest()))
        self.assertEqual(result.content, "id 不能为空。")


class ArtistExtractToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.service = ArtistService(ArtistStore(os.path.join(TMP_ROOT, "lib.json")))
        # Go 的 classify 只把「带 artist: 前缀」或「目录里认得出」的片段当画师，
        # 所以这里先把两个标签写进目录。
        _register_artists(self.service, "artist_a", "artist_b")
        self.deps = ArtistTools(service=self.service, media=None, vision=None)
        self.tool = ArtistExtractTool(self.deps)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_requires_text_or_image(self) -> None:
        result = _run(self.tool.call(ctx, CallRequest()))
        self.assertIn("需要 text", result.content)

    def test_rejects_both_inputs(self) -> None:
        result = _run(
            self.tool.call(ctx, _req({"text": "1girl", "image": "media:" + "a" * 64}))
        )
        self.assertIn("只能二选一", result.content)

    def test_extracts_artist_from_text(self) -> None:
        result = _run(
            self.tool.call(
                ctx, _req({"text": "1girl, artist:artist_a, (artist_b:1.2), soft lighting"})
            )
        )
        payload = json.loads(result.data)
        self.assertEqual(payload["source"], "text")
        self.assertEqual(payload["source_label"], "文本解析")
        self.assertIsNotNone(payload["original_artist_string"])
        names = [item["name"] for item in payload["artists"]]
        self.assertEqual(names, ["artist_a", "artist_b"])
        self.assertIn("原始画师串：", result.content)

    def test_text_output_uses_dialect(self) -> None:
        # 原文里的 artist: 前缀在 sd_weighted 下会被渲染掉，因此会多出一行
        # 「按方言渲染：」（Go 只在渲染结果与原文不同时才打这一行）。
        result = _run(
            self.tool.call(
                ctx,
                _req({"text": "1girl, artist:artist_a", "dialect": "sd_weighted"}),
            )
        )
        payload = json.loads(result.data)
        self.assertEqual(payload["dialect"], "sd_weighted")
        self.assertEqual(payload["original_artist_string"], "artist:artist_a")
        # sd_weighted 会转义冒号（\\:），因为裸冒号在加权标签语法里有别的含义。
        self.assertEqual(payload["artist_string"], "artist\\:artist_a")
        self.assertIn("按方言渲染：", result.content)

    def test_json_format_returns_structured_content(self) -> None:
        result = _run(
            self.tool.call(ctx, _req({"text": "1girl, artist:artist_a", "format": "json"}))
        )
        self.assertEqual(result.content, result.data)
        self.assertEqual(json.loads(result.content)["source"], "text")

    def test_image_requires_valid_media_id(self) -> None:
        result = _run(self.tool.call(ctx, _req({"image": "nope"})))
        self.assertIn("media:<sha256>", result.content)

    def test_image_requires_media_center(self) -> None:
        result = _run(self.tool.call(ctx, _req({"image": "media:" + "a" * 64})))
        self.assertEqual(result.content, "Media Center 未配置，无法读取图片。")

    def test_text_renderer_reports_missing_evidence(self) -> None:
        result = _run(self.tool.call(ctx, _req({"text": "1girl, soft light"})))
        payload = json.loads(result.data)
        self.assertIsNone(payload["original_artist_string"])
        text = artist_extract_text(_output_from(payload))
        self.assertIn("无法取得", text)

    def test_go_json_matches_omitempty_semantics(self) -> None:
        result = _run(self.tool.call(ctx, _req({"text": "1girl, artist:artist_a"})))
        payload = json.loads(result.data)
        # Go 的 omitempty：没有内容的可选字段不出现；artist_string / artists /
        # recommended_presets / warnings 没有 omitempty，永远出现。
        self.assertNotIn("style_description", payload)
        self.assertNotIn("generation_parameters", payload)
        self.assertNotIn("metadata_source", payload)
        self.assertIn("artist_string", payload)
        self.assertIn("artists", payload)
        self.assertIn("recommended_presets", payload)
        self.assertIn("warnings", payload)
        text = go_json({"b": 1, "a": "<x>"})
        self.assertEqual(text, '{"a":"\\u003cx\\u003e","b":1}')


def _preset(name: str, artists, id: str = "") -> object:
    """构造一个测试用 preset。"""
    from atri_x.artist import (
        Dialect,
        Preset,
        PresetArtist,
        PresetSource,
        Scope,
        SourceKind,
    )

    return Preset(
        id=id,
        name=name,
        scope=Scope.GLOBAL,
        dialect=Dialect.SD_WEIGHTED,
        raw_string="",
        artists=[PresetArtist(artist_id=item[0], weight=item[1]) for item in artists],
        source=PresetSource(type=SourceKind.TEXT, operator="admin"),
    )


def _register_artists(service: ArtistService, *tags: str) -> None:
    """把画师标签写进目录，这样 ``resolve`` 能认出没有 ``artist:`` 前缀的写法。"""
    from atri_x.artist import Artist, new_artist_id

    store = service.store()
    for tag in tags:
        store.put_artist(Artist(id=new_artist_id(tag), canonical_tag=tag))


def _output_from(payload: dict):
    """把 JSON 形状还原成 :class:`ArtistExtractOutput`（供文本渲染测试用）。"""
    from atri_x.tool.builtin.artist_extract import ArtistExtractOutput, ArtistRefJSON

    return ArtistExtractOutput(
        source=payload["source"],
        source_label=payload["source_label"],
        dialect=payload["dialect"],
        original_artist_string=payload.get("original_artist_string"),
        artist_string=payload["artist_string"],
        artists=[
            ArtistRefJSON(
                id=item.get("id", ""),
                name=item["name"],
                raw=item["raw"],
                weight=item["weight"],
            )
            for item in payload["artists"]
        ],
        warnings=payload["warnings"],
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
