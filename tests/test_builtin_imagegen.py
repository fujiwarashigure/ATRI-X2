"""``image_generate`` / ``image_generate_context`` / ``image_to_prompt`` 的单元测试。

对应 Go 测试：``internal/tool/builtin/image_generate_test.go``、
``internal/tool/builtin/image_generate_context_test.go``、
``internal/tool/builtin/image_to_prompt_test.go`` 与 ``register_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from datetime import datetime, timezone

from atri_x.artist import Service as ArtistService
from atri_x.artist import Store as ArtistStore
from atri_x.character import NewStore as new_character_store
from atri_x.character import Viewer as CharacterViewer
from atri_x.character import WriteRequest as CharacterWriteRequest
from atri_x.imagegen import Config as ImageConfig
from atri_x.imagegen import Result as ImageResult
from atri_x.tool.builtin.artist_tools import ArtistTools
from atri_x.tool.builtin.character import character_viewer
from atri_x.tool.builtin.image_generate import (
    ImageGenerateTool,
    ImageProfile,
    generate_images,
    image_concurrency_message,
    image_generate_error_message,
    image_result_batch,
)
from atri_x.tool.builtin.image_generate_compose import (
    ImageArtistBlock,
    ImageComposition,
    compose_image_request,
    ext_for_mime,
    image_artifact_name,
    image_dry_run_result,
    image_prompt_dialect,
    multi_character_constraint,
    negative_parts,
    preview_text,
    reference_sources,
    resolve_artist_block,
)
from atri_x.tool.builtin.image_generate_context import (
    ImageToolMessage,
    format_image_context,
    image_context_query,
    looks_like_image_context,
    match_characters_by_name,
    resolve_image_characters,
    should_rewrite_image_prompt,
    split_image_context_terms,
)
from atri_x.tool.builtin.image_to_prompt import (
    ImageMetadataRecord,
    ImageToPromptOutput,
    ImageToPromptTool,
    image_metadata_section,
)
from atri_x.tool.builtin.register import RegisterOptions, register_all
from atri_x.tool.tool import Actor, CallRequest, Registry, RoleSuperadmin, WithActor

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_imagegen_test")
ctx = WithActor(None, Actor(id="admin", platform="test", role=RoleSuperadmin))


def _run(coro):
    return asyncio.run(coro)


def _req(payload: dict) -> CallRequest:
    return CallRequest(arguments=json.dumps(payload, ensure_ascii=False))


class FakeClient:
    """一个永远成功的生图客户端。"""

    def __init__(self, mime_type: str = "image/png", model: str = "gpt-image-1") -> None:
        self.mime_type = mime_type
        self.model = model
        self.requests: list[object] = []

    def enabled(self) -> bool:
        return True

    async def generate(self, ctx, request):
        self.requests.append(request)
        return ImageResult(
            data=b"png-bytes",
            mime_type=self.mime_type,
            model=self.model,
            revised_prompt="a nicer prompt",
        )


class FakeMedia:
    """最小可用的 Media Center：只管 import_reader / read。"""

    class Item:
        def __init__(self, id_value: str, mime_type: str = "image/png", name: str = "x.png"):
            self.id = id_value
            self.mime_type = mime_type
            self.name = name

    def __init__(self) -> None:
        self.imported = 0

    async def import_reader(self, ctx, data, size, spec):
        self.imported += 1
        return self.Item(f"media:{self.imported:064d}", name=getattr(spec, "name", ""))

    async def read(self, ctx, id_value):
        return b"read-bytes", self.Item(id_value)

    async def read_limited(self, ctx, id_value, max_bytes):
        return b"read-bytes", self.Item(id_value)


class FakeHistory:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.requests: list[object] = []

    def search(self, req):
        self.requests.append(req)
        return self.rows


def _config(**overrides) -> ImageConfig:
    base = dict(
        enabled=True,
        model="gpt-image-1",
        base_url="https://example.invalid",
        api_key="k",
        size="1024x1024",
        quality="high",
        max_prompt_runes=4000,
        negative_prompt="bad hands",
        negative_prompt_field="negative_prompt",
        negative_prompt_mode="field",
        prompt_dialect="natural",
        model_family="gpt-image",
        optimize="off",
    )
    base.update(overrides)
    return ImageConfig(**base)


def _tool(**kwargs) -> ImageGenerateTool:
    cfg = kwargs.pop("cfg", None) or _config()
    client = kwargs.pop("client", None) or FakeClient()
    tool = ImageGenerateTool(
        profiles={"main": ImageProfile(name="main", client=client, config=cfg)},
        default_profile="main",
        characters=kwargs.pop("characters", None),
        center=kwargs.pop("center", None),
        history=kwargs.pop("history", None),
        rewriter=kwargs.pop("rewriter", None),
        limiter=kwargs.pop("limiter", None),
        artists=kwargs.pop("artists", None),
    )
    return tool


class ComposeTest(unittest.TestCase):
    def test_negative_prompt_travels_in_its_own_channel(self) -> None:
        tool = _tool()
        composition = compose_image_request(tool, [], "a cat", "", "", "extra bad", False)
        self.assertEqual(composition.negative, "extra bad, bad hands")
        self.assertIn("a cat", composition.positive)
        self.assertNotIn("avoid:", composition.positive)

    def test_inline_mode_folds_into_positive(self) -> None:
        tool = _tool(cfg=_config(negative_prompt_mode="inline"))
        composition = compose_image_request(tool, [], "a cat", "", "", "extra", False)
        self.assertEqual(composition.negative, "")
        self.assertIn("avoid: extra, bad hands", composition.positive)

    def test_off_mode_drops_and_warns(self) -> None:
        tool = _tool(cfg=_config(negative_prompt_mode="off"))
        composition = compose_image_request(tool, [], "a cat", "", "", "extra", False)
        self.assertEqual(composition.negative, "")
        self.assertTrue(any("negative_prompt_mode=off" in item for item in composition.warnings))

    def test_artist_block_is_protected_from_dropping(self) -> None:
        tool = _tool(cfg=_config(max_prompt_runes=40, preset_prompt="style " * 20))
        composition = compose_image_request(tool, [], "a cat", "(artist_a:1.2)", "", "", False)
        # 受保护片段永远在结果里，被丢的是风格片段。
        self.assertIn("(artist_a:1.2)", composition.positive)
        self.assertTrue(composition.dropped)
        self.assertTrue(any("已整体丢弃" in item for item in composition.warnings))

    def test_over_limit_raises_message(self) -> None:
        tool = _tool(cfg=_config(max_prompt_runes=10))
        raised = None
        try:
            compose_image_request(tool, [], "x" * 50, "(artist:1.2)", "", "", False)
        except ImageToolMessage as exc:
            raised = exc
        self.assertIsNotNone(raised)
        self.assertIn("max_prompt_runes=10", str(raised))

    def test_empty_prompt_raises(self) -> None:
        tool = _tool()
        with self.assertRaises(ImageToolMessage):
            compose_image_request(tool, [], "   ", "", "", "", False)

    def test_prompt_dialect_falls_back_to_model_family(self) -> None:
        self.assertEqual(
            image_prompt_dialect(_tool(cfg=_config(prompt_dialect="", model_family="sdxl"))),
            __import__("atri_x.artist", fromlist=["Dialect"]).Dialect.SD_WEIGHTED,
        )
        self.assertEqual(
            str(image_prompt_dialect(_tool(cfg=_config(prompt_dialect="", model_family="nai")))),
            "nai",
        )
        self.assertEqual(
            str(image_prompt_dialect(_tool(cfg=_config(prompt_dialect="", model_family="other")))),
            "natural",
        )

    def test_negative_parts_deduplicates(self) -> None:
        tool = _tool(cfg=_config(negative_prompt="dup"))
        parts = negative_parts(tool, [], "dup", "dup")
        self.assertEqual(parts, ["dup"])

    def test_multi_character_constraint_names_all(self) -> None:
        from atri_x.character import Character

        text = multi_character_constraint(
            [Character(id="a", name="A"), Character(id="b", name="B")]
        )
        self.assertIn("A(a)", text)
        self.assertIn("B(b)", text)

    def test_ext_and_artifact_name(self) -> None:
        self.assertEqual(ext_for_mime("image/jpeg"), ".jpg")
        self.assertEqual(ext_for_mime("image/webp"), ".webp")
        self.assertEqual(ext_for_mime("image/png"), ".png")
        self.assertEqual(
            image_artifact_name("atri", "20240101-000000", "-01", "image/jpeg"),
            "atri-20240101-000000-01.jpg",
        )

    def test_preview_text_truncates(self) -> None:
        self.assertEqual(preview_text("a  b\n c"), "a b c")
        self.assertEqual(len(preview_text("x" * 300)), 203)

    def test_reference_sources_prefers_explicit(self) -> None:
        args = _req({"reference_image": "m1", "reference_images": ["m2", "m1"]})
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs
        from atri_x.tool.builtin.common import decode_args

        decoded = decode_args(args.arguments, ImageGenerateArgs, "image_generate")
        sources = reference_sources(decoded, [])
        self.assertEqual([item.source for item in sources], ["m1", "m2"])


class ContextTest(unittest.TestCase):
    def test_split_terms_uses_chinese_bigrams(self) -> None:
        terms = split_image_context_terms("帮我画一张刚才群里的图片")
        # 停用词（帮我 / 画一 / 刚才 / 群里 / 图片）不会出现。
        for stop in ("帮我", "画一", "刚才", "群里", "图片"):
            self.assertNotIn(stop, terms)
        self.assertIn("一张", terms)

    def test_split_terms_keeps_latin_words(self) -> None:
        self.assertEqual(split_image_context_terms("apple, banana"), ["apple", "banana"])

    def test_looks_like_image_context(self) -> None:
        self.assertTrue(looks_like_image_context("画一下刚才那张图"))
        self.assertTrue(looks_like_image_context("the previous image"))
        self.assertFalse(looks_like_image_context("a red apple"))

    def test_context_query_modes(self) -> None:
        tool = _tool(cfg=_config(auto_context=True, context_default_limit=6))
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs

        args = ImageGenerateArgs(prompt="刚才那张图")
        self.assertEqual(image_context_query(tool, args, "刚才那张图", True), ("刚才那张图", True))
        off = ImageGenerateArgs(context_query="off")
        self.assertEqual(image_context_query(tool, off, "刚才那张图", True), ("", False))
        manual = ImageGenerateArgs(context_query="西瓜")
        self.assertEqual(image_context_query(tool, manual, "刚才那张图", True), ("西瓜", True))
        # auto=False 且没传 context_query 时不拉上下文。
        self.assertEqual(image_context_query(tool, ImageGenerateArgs(), "刚才那张图", False), ("", False))

    def test_should_rewrite_modes(self) -> None:
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs

        always = _tool(cfg=_config(optimize_rewrite="always"))
        self.assertTrue(should_rewrite_image_prompt(always, ImageGenerateArgs(), "x", ""))
        off = _tool(cfg=_config(optimize_rewrite="off"))
        self.assertFalse(should_rewrite_image_prompt(off, ImageGenerateArgs(), "x", ""))
        auto = _tool(cfg=_config(optimize_rewrite="auto", optimize_rewrite_min_runes=50))
        self.assertTrue(should_rewrite_image_prompt(auto, ImageGenerateArgs(), "short", ""))
        self.assertTrue(
            should_rewrite_image_prompt(
                auto, ImageGenerateArgs(), "x" * 100, "context here"
            )
        )
        self.assertFalse(should_rewrite_image_prompt(auto, ImageGenerateArgs(), "x" * 100, ""))
        self.assertTrue(
            should_rewrite_image_prompt(off, ImageGenerateArgs(rewrite=True), "x", "")
        )
        self.assertFalse(
            should_rewrite_image_prompt(always, ImageGenerateArgs(rewrite=False), "x", "")
        )

    def test_format_image_context(self) -> None:
        from atri_x.storage import ChatMessage

        rows = [
            ChatMessage(
                text="  你好   世界 ",
                sender_name="小明",
                created_at=datetime(2024, 5, 1, 9, 30, tzinfo=timezone.utc),
            ),
            ChatMessage(text="", sender_name="空"),
        ]
        text = format_image_context(rows)
        self.assertIn("[参考对话]", text)
        self.assertIn("- 05-01 09:30 小明: 你好 世界", text)
        self.assertEqual(format_image_context([]), "")
        self.assertEqual(format_image_context([ChatMessage(text="  ")]), "")

    def test_match_characters_by_name_prefers_longer_names(self) -> None:
        from atri_x.character import Character

        store = _character_store()
        tool = _tool(characters=store)
        viewer = CharacterViewer(superadmin=True)
        items = match_characters_by_name(tool, ctx, "画一张亚托莉", viewer, 4)
        self.assertEqual([item.id for item in items], ["atri"])
        self.assertEqual(match_characters_by_name(tool, ctx, "无关内容", viewer, 4), [])
        self.assertIsInstance(items[0], Character)

    def test_resolve_characters_explicit_ids(self) -> None:
        store = _character_store()
        tool = _tool(characters=store)
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs

        viewer = CharacterViewer(superadmin=True)
        args = ImageGenerateArgs(character_ids=["atri"])
        items = resolve_image_characters(tool, ctx, args, "x", viewer, False)
        self.assertEqual([item.id for item in items], ["atri"])

    def test_resolve_characters_missing_id_becomes_message(self) -> None:
        store = _character_store()
        tool = _tool(characters=store)
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs

        viewer = CharacterViewer(superadmin=True)
        args = ImageGenerateArgs(character_ids=["nope"])
        with self.assertRaises(ImageToolMessage):
            resolve_image_characters(tool, ctx, args, "x", viewer, False)

    def test_resolve_characters_uses_search_for_query(self) -> None:
        store = _character_store()
        tool = _tool(characters=store)
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs

        viewer = CharacterViewer(superadmin=True)
        args = ImageGenerateArgs(character_query="亚托莉", character_id="auto")
        items = resolve_image_characters(tool, ctx, args, "无关", viewer, True)
        self.assertEqual([item.id for item in items], ["atri"])

    def test_resolve_characters_disabled_store(self) -> None:
        tool = _tool(characters=None)
        from atri_x.tool.builtin.image_generate import ImageGenerateArgs

        self.assertEqual(
            resolve_image_characters(
                tool, ctx, ImageGenerateArgs(), "x", CharacterViewer(), False
            ),
            [],
        )


class ImageGenerateToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_requires_prompt(self) -> None:
        result = _run(_tool().call(ctx, CallRequest()))
        self.assertEqual(result.content, "prompt 不能为空。")

    def test_unknown_profile(self) -> None:
        result = _run(_tool().call(ctx, _req({"prompt": "cat", "profile": "nope"})))
        self.assertEqual(result.content, "没有配置生图 profile：nope")

    def test_disabled_client(self) -> None:
        # 没有 profile 时 client 保持 None（Go 里是未初始化的 *imagegen.Client）。
        tool = ImageGenerateTool(profiles={}, default_profile="")
        result = _run(tool.call(ctx, _req({"prompt": "cat"})))
        self.assertIn("生图服务未启用", result.content)

    def test_dry_run_reports_compiled_prompt(self) -> None:
        result = _run(_tool().call(ctx, _req({"prompt": "a cat", "dry_run": True})))
        self.assertIn("dry_run：只编译", result.content)
        self.assertIn("画师串: 本次未使用", result.content)
        payload = json.loads(result.data)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertEqual(payload["negative"], "bad hands")
        self.assertEqual(payload["count"], 1)

    def test_dry_run_does_not_call_client(self) -> None:
        client = FakeClient()
        _run(_tool(client=client).call(ctx, _req({"prompt": "a cat", "dry_run": True})))
        self.assertEqual(client.requests, [])

    def test_generate_success_returns_segments_and_outputs(self) -> None:
        client = FakeClient()
        media = FakeMedia()
        tool = _tool(client=client, center=media)
        result = _run(
            tool.call(ctx, _req({"prompt": "a cat", "send": True}))
        )
        self.assertIn("已生成图片。", result.content)
        self.assertIn("media: media:", result.content)
        self.assertIn("revised_prompt: a nicer prompt", result.content)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[1].type, "image")
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(media.imported, 1)

    def test_generate_without_send_has_no_outputs(self) -> None:
        result = _run(_tool(center=FakeMedia()).call(ctx, _req({"prompt": "a cat"})))
        self.assertEqual(result.outputs, [])

    def test_missing_media_center_fails_the_batch(self) -> None:
        result = _run(_tool(center=None).call(ctx, _req({"prompt": "a cat"})))
        self.assertIn("生图失败。", result.content)
        self.assertIn("Media Center 未配置", result.content)

    def test_batch_count_is_clamped(self) -> None:
        client = FakeClient()
        media = FakeMedia()
        tool = _tool(client=client, center=media)
        result = _run(
            tool.call(ctx, _req({"prompt": "a cat", "count": 9}))
        )
        self.assertEqual(len(client.requests), 4)
        self.assertIn("单次最多生成 4 张", result.content)
        self.assertIn("已生成 4/9 张图片。", result.content)

    def test_artist_mode_auto_warns_when_no_preset_matches(self) -> None:
        service = ArtistService(ArtistStore(os.path.join(TMP_ROOT, "lib.json")))
        tool = _tool(
            center=FakeMedia(),
            artists=ArtistTools(service=service),
        )
        result = _run(
            tool.call(ctx, _req({"prompt": "a cat", "artist_mode": "auto"}))
        )
        self.assertTrue(
            any("没有匹配的已确认组合" in item for item in result.warnings),
            result.warnings,
        )

    def test_artist_mode_off_without_library_keeps_prompt_clean(self) -> None:
        # 默认 artist_mode=off，也没传 artist_string：完全不碰串库，不产生画师串告警。
        tool = _tool(center=FakeMedia())
        result = _run(tool.call(ctx, _req({"prompt": "a cat"})))
        self.assertIn("已生成图片。", result.content)
        self.assertFalse(any("画师串库" in item for item in result.warnings))

    def test_artist_string_without_library_warns(self) -> None:
        tool = _tool(center=FakeMedia(), artists=ArtistTools(service=None))
        result = _run(
            tool.call(
                ctx,
                _req(
                    {
                        "prompt": "a cat",
                        "artist_string": "(a:1.2)",
                        "artist_mode": "explicit",
                    }
                ),
            )
        )
        self.assertTrue(
            any("画师串库未启用" in item for item in result.warnings), result.warnings
        )

    def test_reference_without_support_is_refused(self) -> None:
        tool = _tool(center=FakeMedia())
        result = _run(
            tool.call(
                ctx,
                _req({"prompt": "a cat", "reference_image": "media:" + "a" * 64}),
            )
        )
        self.assertIn("未开启参考图支持", result.content)

    def test_reference_invalid_media_id(self) -> None:
        tool = _tool(center=FakeMedia(), cfg=_config(supports_reference=True))
        result = _run(
            tool.call(
                ctx, _req({"prompt": "a cat", "reference_image": "media:not-a-hash"})
            )
        )
        self.assertIn("参考图 media id 不合法", result.content)

    def test_reference_without_character_needs_character_id(self) -> None:
        # 不是 media: 前缀也不是 http(s) 的，按角色图片名处理；没有角色就明确报错。
        tool = _tool(center=FakeMedia(), cfg=_config(supports_reference=True))
        result = _run(
            tool.call(ctx, _req({"prompt": "a cat", "reference_image": "avatar.png"}))
        )
        self.assertIn("使用角色参考图需要指定 character_id", result.content)

    def test_reference_media_id_is_read(self) -> None:
        client = FakeClient()
        tool = _tool(
            client=client, center=FakeMedia(), cfg=_config(supports_reference=True)
        )
        result = _run(
            tool.call(
                ctx,
                _req({"prompt": "a cat", "reference_image": "media:" + "a" * 64}),
            )
        )
        self.assertIn("参考图：1 张", result.content)
        self.assertEqual(len(client.requests[0].references), 1)
        self.assertEqual(client.requests[0].references[0].data, b"read-bytes")

    def test_rewriter_runs_and_keeps_artist_block(self) -> None:
        class Rewriter:
            def __init__(self) -> None:
                self.requests = []

            async def rewrite_image_prompt(self, ctx, request):
                self.requests.append(request)
                return "rewritten scene"

        rewriter = Rewriter()
        library = ArtistStore(os.path.join(TMP_ROOT, "lib.json"))
        service = ArtistService(library)
        tool = _tool(
            center=FakeMedia(),
            rewriter=rewriter,
            cfg=_config(
                optimize_rewrite="always", optimize_rewrite_model="cheap-model"
            ),
            artists=ArtistTools(service=service),
        )
        result = _run(
            tool.call(
                ctx,
                _req({"prompt": "a cat", "artist_string": "artist:artist_a"}),
            )
        )
        self.assertEqual(len(rewriter.requests), 1)
        request = rewriter.requests[0]
        # 改写器看不到画师串本身，只被告知它存在。
        self.assertNotIn("artist_a", request.scene)
        self.assertTrue(request.protected_blocks)
        self.assertIn("rewritten scene", result.content)
        self.assertIn("语义改写：", result.content)

    def test_generate_error_is_reported_per_image(self) -> None:
        class BrokenClient:
            def enabled(self) -> bool:
                return True

            async def generate(self, ctx, request):
                raise RuntimeError("boom")

        result = _run(
            _tool(client=BrokenClient(), center=FakeMedia()).call(
                ctx, _req({"prompt": "a cat"})
            )
        )
        self.assertIn("生图失败。", result.content)
        self.assertIn("boom", result.content)

    def test_concurrency_messages(self) -> None:
        from atri_x.ops.concurrency import ErrFull, ErrTimeout

        self.assertEqual(image_concurrency_message(ErrFull), "生图服务繁忙，请稍后重试。")
        self.assertEqual(image_concurrency_message(ErrTimeout), "生图服务排队超时，请稍后重试。")
        self.assertEqual(
            image_concurrency_message(asyncio.CancelledError()), "生图已取消。"
        )
        self.assertEqual(
            image_concurrency_message(TimeoutError()), "生图排队超时，请稍后重试。"
        )
        self.assertEqual(
            image_concurrency_message(RuntimeError("x")), "生图服务暂时不可用，请稍后重试。"
        )
        self.assertEqual(image_generate_error_message(None), "")
        self.assertEqual(image_generate_error_message(ErrFull), "生图服务繁忙，请稍后重试。")
        self.assertEqual(image_generate_error_message(RuntimeError("boom")), "boom")

    def test_generate_images_batch_isolates_failures(self) -> None:
        class SometimesBroken:
            def __init__(self) -> None:
                self.calls = 0

            def enabled(self) -> bool:
                return True

            async def generate(self, ctx, request):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("boom")
                return ImageResult(data=b"x", mime_type="image/png", model="m")

        tool = _tool(client=SometimesBroken())
        results, errors = _run(
            generate_images(tool, ctx, _request("cat"), 3)
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(sum(1 for item in errors if item is not None), 1)
        self.assertEqual(len([item for item in results if item is not None]), 2)

    def test_image_result_batch_skips_short_lists(self) -> None:
        class Item:
            id = "media:1"

        result = image_result_batch(
            _tool(cfg=_config(send_by_default=True)),
            [ImageResult(mime_type="image/png")],
            [Item()],
            ["a.png"],
            "summary",
            None,
        )
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.outputs[0].source.media_id, "media:1")


class ImageToPromptTest(unittest.TestCase):
    def test_metadata_section_omits_empty_evidence(self) -> None:
        self.assertEqual(image_metadata_section(ImageMetadataRecord(), False), "")
        self.assertEqual(
            image_metadata_section(ImageMetadataRecord(), True),
            "[图片生成参数]\n没有找到生成参数。",
        )
        self.assertEqual(
            image_metadata_section(ImageMetadataRecord(hint="没有元数据"), True),
            "[图片生成参数]\n没有元数据",
        )

    def test_metadata_section_renders_parameters_sorted(self) -> None:
        record = ImageMetadataRecord(
            has_artist=True,
            artist_original="artist:artist_a",
            positive="a cat",
            negative="blurry",
            parameters={"steps": "20", "cfg": "7"},
            source="a1111",
        )
        text = image_metadata_section(record, False)
        self.assertIn("画师串（图片元数据记录的原文）：artist:artist_a", text)
        self.assertIn("生成参数：cfg=7, steps=20", text)
        self.assertIn("元数据格式：a1111", text)

    def test_output_json_omits_empty_optionals(self) -> None:
        payload = ImageToPromptOutput().to_json()
        self.assertNotIn("style_description", payload)
        self.assertIn("artist_extraction", payload)
        self.assertEqual(payload["warnings"], [])
        self.assertIsNone(payload["artist_extraction"]["original_artist_string"])

    def test_requires_image(self) -> None:
        tool = ImageToPromptTool()
        result = _run(tool.call(ctx, CallRequest()))
        self.assertEqual(result.content, "image 不能为空，请传入 media:<sha256> 媒体 ID。")

    def test_invalid_media_id(self) -> None:
        result = _run(ImageToPromptTool().call(ctx, _req({"image": "nope"})))
        self.assertIn("media:<sha256>", result.content)

    def test_requires_service(self) -> None:
        result = _run(
            ImageToPromptTool().call(ctx, _req({"image": "media:" + "a" * 64}))
        )
        self.assertIn("未配置视觉模型", result.content)

    def test_requires_media_center(self) -> None:
        class Service:
            async def describe(self, ctx, req):  # pragma: no cover - 不该被调用
                raise AssertionError("unused")

        result = _run(
            ImageToPromptTool(None, Service()).call(
                ctx, _req({"image": "media:" + "a" * 64})
            )
        )
        self.assertEqual(result.content, "Media Center 未配置，无法读取图片。")

    def test_rejects_unknown_include_artist(self) -> None:
        class Service:
            async def describe(self, ctx, req):  # pragma: no cover
                raise AssertionError("unused")

        tool = ImageToPromptTool(FakeMedia(), Service())
        result = _run(
            tool.call(
                ctx,
                _req({"image": "media:" + "a" * 64, "include_artist": "nope"}),
            )
        )
        self.assertIn("只能是 none、metadata 或 recommend", result.content)

    def test_text_format_returns_prompt_only(self) -> None:
        from atri_x.vision import Prompt  # noqa: F401  （确认导出存在）

        class Service:
            def __init__(self) -> None:
                self.prompts = []

            async def describe(self, ctx, req):
                self.prompts.append(req.prompt)
                return type("R", (), {"text": "a cat, sitting"})()

        service = Service()
        result = _run(
            ImageToPromptTool(FakeMedia(), service).call(
                ctx,
                _req({"image": "media:" + "a" * 64, "include_artist": "none"}),
            )
        )
        self.assertEqual(result.content, "a cat, sitting")
        self.assertEqual(len(service.prompts), 1)

    def test_json_format_returns_structured_output(self) -> None:
        class Service:
            async def describe(self, ctx, req):
                return type(
                    "R",
                    (),
                    {
                        "text": json.dumps(
                            {
                                "positive_prompt": "a cat",
                                "negative_prompt": "blurry",
                                "style_description": "水彩",
                                "style_tags": ["watercolor"],
                            }
                        )
                    },
                )()

        result = _run(
            ImageToPromptTool(FakeMedia(), Service()).call(
                ctx,
                _req({"image": "media:" + "a" * 64, "format": "json"}),
            )
        )
        payload = json.loads(result.content)
        self.assertEqual(payload["positive_prompt"], "a cat")
        self.assertEqual(payload["negative_prompt"], "blurry")
        self.assertEqual(payload["style_tags"], ["watercolor"])

    def test_unparsable_analysis_suggests_text_mode(self) -> None:
        class Service:
            async def describe(self, ctx, req):
                return type("R", (), {"text": "not json at all"})()

        result = _run(
            ImageToPromptTool(FakeMedia(), Service()).call(
                ctx,
                _req({"image": "media:" + "a" * 64, "format": "json"}),
            )
        )
        self.assertIn("改用 format=text 重试", result.content)

    def test_cancellation_propagates(self) -> None:
        class Service:
            async def describe(self, ctx, req):
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            _run(
                ImageToPromptTool(FakeMedia(), Service()).call(
                    ctx, _req({"image": "media:" + "a" * 64})
                )
            )


class RegisterWiringWithImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.store = _character_store()

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _options(self, **overrides) -> RegisterOptions:
        options = dict(
            character_store=self.store,
            image_profiles={
                "main": ImageProfile(
                    name="main", client=FakeClient(), config=_config()
                )
            },
            default_image_profile="main",
            image_limiter=None,
            chat_history=FakeHistory(),
            image_prompt_service=type(
                "S", (), {"describe": lambda self, ctx, req: None}
            )(),
        )
        options.update(overrides)
        return RegisterOptions(**options)

    def test_all_image_tools_register(self) -> None:
        registry = Registry()
        register_all(registry, self._options())
        names = {info.name for info in registry.list()}
        for name in (
            "character_list",
            "character_read",
            "character_search",
            "character_manage",
            "character_delete",
            "image_generate",
            "artist_extract",
            "image_to_prompt",
        ):
            self.assertIn(name, names, name)
        # 没有串库时四个 library 工具不注册。
        for name in (
            "artist_library_search",
            "artist_library_read",
            "artist_library_save",
            "artist_library_delete",
        ):
            self.assertNotIn(name, names, name)

    def test_artist_library_tools_register_when_service_exists(self) -> None:
        service = ArtistService(ArtistStore(os.path.join(TMP_ROOT, "lib.json")))
        registry = Registry()
        register_all(
            registry, self._options(artists=ArtistTools(service=service))
        )
        names = {info.name for info in registry.list()}
        for name in (
            "artist_library_search",
            "artist_library_read",
            "artist_library_save",
            "artist_library_delete",
        ):
            self.assertIn(name, names, name)

    def test_no_image_tools_without_dependencies(self) -> None:
        registry = Registry()
        register_all(registry, RegisterOptions())
        names = {info.name for info in registry.list()}
        self.assertIn("artist_extract", names)
        for name in ("character_list", "image_generate", "image_to_prompt"):
            self.assertNotIn(name, names, name)

    def test_image_tool_gets_artists_and_store(self) -> None:
        registry = Registry()
        register_all(registry, self._options())
        tool = registry.get("image_generate")[0]
        self.assertIs(tool.characters, self.store)
        self.assertIsInstance(tool.artists, ArtistTools)


def _request(prompt: str):
    from atri_x.imagegen import Request

    return Request(prompt=prompt)


def _character_store():
    root = os.path.join(TMP_ROOT, "characters")
    store = new_character_store(root)
    store.write(
        ctx,
        CharacterWriteRequest(
            id="atri",
            name="亚托莉",
            aliases=["Atri"],
            docs={"profile": "你是亚托莉。"},
        ),
        CharacterViewer(superadmin=True),
    )
    return store


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
