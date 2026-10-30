"""漫画服务编排测试（P0-6/P0-8 + P1 的失败恢复）。

覆盖的验收点：

* 规划不消耗生图额度，渲染才真正调用 client（``count`` 语义是"一张整页"）；
* 生成/入库/投递三个状态分别落库，记录里能解释"这张图怎么来的"；
* 计划版本绑定：计划更新后旧 revision 被拒绝；
* 重发只发已有 MediaID，不重新调用生图接口；
* 超时这类"可能已在服务端生成"的失败标 unknown 且不自动重试；
* 不同 profile/人物/目标的两个并发任务不串状态；
* 生图 profile 的 ``superadmin_only`` 与 ``supports_reference`` 不被绕过。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from dataclasses import dataclass

from atri_x.app import comic as app_comic_module
from atri_x.app import tool_runtime_comic as tool_runtime_comic_module
from atri_x.character import Viewer
from atri_x.comic import (
    ComicOptions,
    ComicRenderError,
    ComicService,
    ComicStore,
    PlanRequest,
    RenderRequest,
)
from atri_x.comic.references import load_references, validate_reference_capacity
from atri_x.imagegen import Config as ImageConfig
from tests._comic_fakes import (
    FakeAvatarResolver,
    FakeCharacterStore,
    FakeHistory,
    FakeImageClient,
    FakeMediaManager,
    build_context,
    make_rows,
)

TMP_ROOT = os.path.join(os.getcwd(), "tmp_comic_service_test")

VIEWER = Viewer(platform="qqonebot", actor_id="admin", superadmin=True)
MEMBER_VIEWER = Viewer(platform="qqonebot", actor_id="user", superadmin=False)


@dataclass
class FakeProfile:
    name: str
    client: object
    config: object


def _config(**overrides) -> ImageConfig:
    base = dict(
        enabled=True,
        model="fake-image",
        size="1024x1024",
        quality="high",
        supports_reference=True,
        preset_prompt="水彩画风",
        negative_prompt="lowres",
        negative_prompt_mode="field",
        max_prompt_runes=6000,
    )
    base.update(overrides)
    return ImageConfig(**base)


def _make_service(
    *,
    root: str = "",
    rows=None,
    characters=None,
    media=None,
    profiles=None,
    planner=None,
    avatar_resolver=None,
    options=None,
    default_profile="main",
) -> ComicService:
    root = root or TMP_ROOT
    return ComicService(
        options or ComicOptions(send_by_default=True),
        store=ComicStore(os.path.join(root, "comic")),
        history=FakeHistory(rows if rows is not None else make_rows(["今天去吃火锅吧", "好啊，我请客"])),
        characters=characters if characters is not None else FakeCharacterStore(),
        media=media if media is not None else FakeMediaManager(),
        profiles=profiles
        or {"main": FakeProfile("main", FakeImageClient(), _config())},
        default_profile=default_profile,
        planner=planner,
        avatar_resolver=avatar_resolver
        or FakeAvatarResolver(
            {("qqonebot", "1001"): "https://a/1.png", ("qqonebot", "1002"): "https://a/2.png"}
        ),
    )


def _run(coro):
    return asyncio.run(coro)


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_plan_uses_rules_by_default_and_persists(self) -> None:
        service = _make_service()
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        )
        self.assertEqual(outcome.plan.planner, "rules")
        self.assertEqual(outcome.plan.panel_count, 2)
        self.assertTrue(os.path.exists(outcome.plan_path))
        loaded = service.store.load_plan(outcome.plan.plan_id)
        self.assertEqual(loaded.fingerprint(), outcome.plan.fingerprint())

    def test_plan_does_not_call_image_client(self) -> None:
        client = FakeImageClient()
        service = _make_service(profiles={"main": FakeProfile("main", client, _config())})
        _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        )
        self.assertEqual(client.requests, [])

    def test_plan_uses_avatar_references(self) -> None:
        service = _make_service()
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        )
        self.assertEqual([item.source for item in outcome.plan.characters], ["avatar", "avatar"])
        self.assertEqual(
            [item.reference.source for item in outcome.plan.reference_order()],
            ["https://a/1.png", "https://a/2.png"],
        )

    def test_plan_rejects_too_many_panels(self) -> None:
        service = _make_service(options=ComicOptions(max_panels=12))
        with self.assertRaises(Exception):
            _run(
                service.plan(
                    build_context(),
                    PlanRequest(panel_count=15, character_mode="avatars"),
                    viewer=VIEWER,
                )
            )

    def test_llm_planner_result_is_used(self) -> None:
        class Planner:
            def __init__(self) -> None:
                self.requests = []

            async def plan(self, ctx, request):
                self.requests.append(request)
                import json

                return json.dumps(
                    {
                        "title": "模型标题",
                        "language": "zh",
                        "layout": "grid",
                        "panels": [
                            {
                                "index": 1,
                                "scene": "第一格画面",
                                "character_ids": [request.characters[0].id],
                                "source_message_ids": ["msg-1"],
                                "dialogue": [
                                    {
                                        "speaker_id": request.characters[0].id,
                                        "text": "今天去吃火锅吧",
                                        "kind": "original",
                                        "source_message_ids": ["msg-1"],
                                    }
                                ],
                            },
                            {
                                "index": 2,
                                "scene": "第二格画面",
                                "character_ids": [request.characters[0].id],
                                "source_message_ids": ["msg-2"],
                                "dialogue": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                )

        planner = Planner()
        service = _make_service(planner=planner)
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        )
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(outcome.plan.planner, "llm")
        self.assertEqual(outcome.plan.title, "模型标题")

    def test_broken_llm_planner_falls_back_to_rules_in_auto(self) -> None:
        class BrokenPlanner:
            async def plan(self, ctx, request):
                raise RuntimeError("model boom")

        service = _make_service(planner=BrokenPlanner())
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        )
        self.assertEqual(outcome.plan.planner, "rules")
        self.assertTrue(any("模型规划失败" in item for item in outcome.warnings))


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _plan(self, service, panel_count=2):
        return _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=panel_count, character_mode="avatars"),
                viewer=VIEWER,
            )
        )

    def test_render_generates_one_image_and_records_everything(self) -> None:
        client = FakeImageClient()
        media = FakeMediaManager()
        service = _make_service(
            media=media, profiles={"main": FakeProfile("main", client, _config())}
        )
        plan = self._plan(service)
        outcome = _run(
            service.render(build_context(), RenderRequest(plan_id=plan.plan.plan_id), viewer=VIEWER)
        )
        # 一张整页漫画：只有一次生成请求。
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len(media.imported), 1)
        request = client.requests[0]
        self.assertEqual(len(request.references), 2)
        self.assertIn("今天去吃火锅吧", request.prompt)
        self.assertIn("参考图#1", request.prompt)
        record = service.store.load_record(outcome.record.task_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, "succeeded")
        self.assertEqual(record.generation, "ok")
        self.assertEqual(record.storage, "ok")
        self.assertEqual(record.delivery, "requested")
        self.assertEqual(record.media_id, outcome.media_id)
        self.assertEqual(record.plan_revision, plan.plan.revision)
        self.assertIn("今天去吃火锅吧", record.prompt)
        self.assertEqual(len(record.reference_descriptors), 2)
        self.assertEqual(len(media.references), 1)

    def test_dry_run_does_not_touch_client_or_media(self) -> None:
        client = FakeImageClient()
        media = FakeMediaManager()
        service = _make_service(
            media=media, profiles={"main": FakeProfile("main", client, _config())}
        )
        plan = self._plan(service)
        outcome = _run(
            service.render(
                build_context(),
                RenderRequest(plan_id=plan.plan.plan_id, dry_run=True),
                viewer=VIEWER,
            )
        )
        self.assertTrue(outcome.dry_run)
        self.assertEqual(client.requests, [])
        self.assertEqual(media.imported, [])
        self.assertIn("第1格", outcome.prompt)

    def test_render_requires_reference_support(self) -> None:
        client = FakeImageClient()
        service = _make_service(
            profiles={
                "main": FakeProfile("main", client, _config(supports_reference=False))
            }
        )
        plan = self._plan(service)
        with self.assertRaises(ComicRenderError) as caught:
            _run(
                service.render(
                    build_context(), RenderRequest(plan_id=plan.plan.plan_id), viewer=VIEWER
                )
            )
        self.assertIn("supports_reference", str(caught.exception))
        self.assertEqual(client.requests, [])

    def test_extra_profile_is_superadmin_only(self) -> None:
        client = FakeImageClient()
        service = _make_service(
            profiles={
                "main": FakeProfile("main", FakeImageClient(), _config()),
                "vip": FakeProfile("vip", client, _config(superadmin_only=True)),
            }
        )
        plan = self._plan(service)
        with self.assertRaises(ComicRenderError) as caught:
            _run(
                service.render(
                    build_context(),
                    RenderRequest(plan_id=plan.plan.plan_id, profile="vip"),
                    viewer=MEMBER_VIEWER,
                )
            )
        self.assertIn("超级管理员", str(caught.exception))
        self.assertEqual(client.requests, [])

    def test_unknown_profile_is_rejected(self) -> None:
        service = _make_service()
        plan = self._plan(service)
        with self.assertRaises(ComicRenderError):
            _run(
                service.render(
                    build_context(),
                    RenderRequest(plan_id=plan.plan.plan_id, profile="nope"),
                    viewer=VIEWER,
                )
            )

    def test_plan_revision_binding(self) -> None:
        service = _make_service()
        plan = self._plan(service)
        # 模拟"用户改了分镜，计划升到第 2 版"。
        plan.plan.revision = 2
        service.store.save_plan(plan.plan)
        with self.assertRaises(ComicRenderError) as caught:
            _run(
                service.render(
                    build_context(),
                    RenderRequest(plan_id=plan.plan.plan_id, revision=1),
                    viewer=VIEWER,
                )
            )
        self.assertIn("第 2 版", str(caught.exception))

    def test_explicit_media_reference_failure_is_an_error(self) -> None:
        media = FakeMediaManager()  # 里面没有任何媒体
        service = _make_service(media=media)
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(
                    panel_count=1,
                    character_mode="avatars",
                    reference_images=["1001=media:" + "d" * 64],
                ),
                viewer=VIEWER,
            )
        )
        with self.assertRaises(ComicRenderError) as caught:
            _run(
                service.render(
                    build_context(),
                    RenderRequest(plan_id=outcome.plan.plan_id),
                    viewer=VIEWER,
                )
            )
        self.assertIn("读取参考图失败", str(caught.exception))

    def test_resend_reuses_media_without_generating(self) -> None:
        client = FakeImageClient()
        media = FakeMediaManager()
        service = _make_service(
            media=media, profiles={"main": FakeProfile("main", client, _config())}
        )
        plan = self._plan(service)
        first = _run(
            service.render(build_context(), RenderRequest(plan_id=plan.plan.plan_id), viewer=VIEWER)
        )
        second = _run(
            service.render(
                build_context(),
                RenderRequest(plan_id=plan.plan.plan_id, task_id=first.record.task_id),
                viewer=VIEWER,
            )
        )
        self.assertTrue(second.reused)
        self.assertEqual(second.media_id, first.media_id)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len(media.imported), 1)
        self.assertTrue(any("重发" in item for item in second.warnings))

    def test_timeout_marks_unknown_and_does_not_retry(self) -> None:
        client = FakeImageClient(error=TimeoutError("slow upstream"))
        service = _make_service(profiles={"main": FakeProfile("main", client, _config())})
        plan = self._plan(service)
        with self.assertRaises(ComicRenderError) as caught:
            _run(
                service.render(
                    build_context(), RenderRequest(plan_id=plan.plan.plan_id), viewer=VIEWER
                )
            )
        self.assertIn("结果未知", str(caught.exception))
        self.assertEqual(len(client.requests), 1)
        # 记录落在 records 目录里，状态是 unknown（不是 failed，也不是 succeeded）。
        records_dir = service.store.records_dir
        names = os.listdir(records_dir)
        self.assertEqual(len(names), 1)
        record = service.store.load_record(names[0][: -len(".json")])
        self.assertEqual(record.state, "unknown")
        self.assertEqual(record.generation, "unknown")

    def test_hard_failure_marks_failed(self) -> None:
        client = FakeImageClient(error=RuntimeError("bad prompt"))
        service = _make_service(profiles={"main": FakeProfile("main", client, _config())})
        plan = self._plan(service)
        with self.assertRaises(ComicRenderError):
            _run(
                service.render(
                    build_context(), RenderRequest(plan_id=plan.plan.plan_id), viewer=VIEWER
                )
            )
        names = os.listdir(service.store.records_dir)
        record = service.store.load_record(names[0][: -len(".json")])
        self.assertEqual(record.state, "failed")

    def test_concurrent_renders_do_not_cross_state(self) -> None:
        client_a = FakeImageClient(model="model-a")
        client_b = FakeImageClient(model="model-b")
        media = FakeMediaManager()
        service = _make_service(
            media=media,
            profiles={
                "a": FakeProfile("a", client_a, _config(model="model-a")),
                "b": FakeProfile("b", client_b, _config(model="model-b", size="1536x1024")),
            },
        )
        plan_a = self._plan(service)
        plan_b = self._plan(service)

        async def run_both():
            return await asyncio.gather(
                service.render(
                    build_context(),
                    RenderRequest(plan_id=plan_a.plan.plan_id, profile="a"),
                    viewer=VIEWER,
                ),
                service.render(
                    build_context(),
                    RenderRequest(plan_id=plan_b.plan.plan_id, profile="b"),
                    viewer=VIEWER,
                ),
            )

        first, second = _run(run_both())
        self.assertEqual(len(client_a.requests), 1)
        self.assertEqual(len(client_b.requests), 1)
        self.assertEqual(first.record.profile, "a")
        self.assertEqual(second.record.profile, "b")
        self.assertEqual(first.record.size, "1024x1024")
        self.assertEqual(second.record.size, "1536x1024")
        self.assertNotEqual(first.record.task_id, second.record.task_id)
        self.assertNotEqual(first.record.plan_id, second.record.plan_id)


class ReferenceHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_capacity_rejects_profile_without_reference_support(self) -> None:
        service = _make_service(
            profiles={
                "main": FakeProfile("main", FakeImageClient(), _config(supports_reference=False))
            }
        )
        plan = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        ).plan
        with self.assertRaises(ComicRenderError):
            validate_reference_capacity(plan, _config(supports_reference=False))

    def test_download_false_lists_characters_without_media(self) -> None:
        media = FakeMediaManager()
        service = _make_service(media=media)
        plan = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=VIEWER,
            )
        ).plan
        warnings: list[str] = []
        characters, references = _run(
            load_references(
                build_context(),
                plan,
                _config(),
                VIEWER,
                warnings,
                media=media,
                characters=None,
                download=False,
            )
        )
        self.assertEqual(len(characters), 2)
        self.assertEqual(references, [])
        self.assertEqual(media.urls, [])


class AppComicTest(unittest.TestCase):
    def test_platform_avatar_resolver_matches_runtime_by_name(self) -> None:
        from atri_x.app.comic import PlatformAvatarResolver

        class Adapter:
            def name(self) -> str:
                return "qqonebot"

            async def get_user_avatar_url(self, ctx, user_id, size):
                return f"https://a/{user_id}-{size}.png"

        resolver = PlatformAvatarResolver([Adapter()])
        self.assertEqual(
            asyncio.run(resolver.avatar_url(None, "qqonebot", "1001")),
            "https://a/1001-320.png",
        )
        self.assertEqual(asyncio.run(resolver.avatar_url(None, "cli", "1001")), "")

    def test_build_comic_planner_uses_work_slot(self) -> None:
        from types import SimpleNamespace

        from atri_x.app.comic import LLMComicPlanner, build_comic_planner
        from atri_x.app.dependencies import ModelClients
        from atri_x.config import ModelSelection
        from atri_x.storage import SessionModeWork

        client = object()
        cfg = SimpleNamespace(
            mode_models={
                SessionModeWork: ModelSelection(provider="prov", model="planner-model")
            },
            naming_model=ModelSelection(),
            compact_model=ModelSelection(),
            comic=SimpleNamespace(plan_model="work"),
        )
        planner = build_comic_planner(cfg, ModelClients(by_provider={"prov": client}))
        self.assertIsInstance(planner, LLMComicPlanner)
        self.assertEqual(planner.model, "planner-model")
        self.assertIs(planner.client, client)
        # provider 不存在时回落到规则规划（返回 None）。
        self.assertIsNone(build_comic_planner(cfg, ModelClients()))

    def test_llm_planner_streams_json(self) -> None:
        from types import SimpleNamespace

        from atri_x.app.comic import LLMComicPlanner
        from atri_x.comic import ComicContext
        from atri_x.comic.planner import ComicPlanRequest

        class Client:
            def __init__(self) -> None:
                self.requests = []

            def chat_stream(self, ctx, request):
                self.requests.append(request)

                async def generate():
                    yield SimpleNamespace(
                        delta_content='{"title":"模型标题","panels":[]}',
                        error=None,
                        usage=None,
                    )

                return generate()

        client = Client()
        planner = LLMComicPlanner(client, "planner-model")
        request = ComicPlanRequest(
            context=ComicContext(platform="qqonebot", scope_id="group-1"),
            characters=[],
            panel_count=1,
        )
        text = _run(planner.plan(None, request))
        self.assertIn("模型标题", text)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0].model, "planner-model")


class AppWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_app_modules_import_cleanly(self) -> None:
        self.assertEqual(app_comic_module.__name__, "atri_x.app.comic")
        self.assertEqual(tool_runtime_comic_module.__name__, "atri_x.app.tool_runtime_comic")

    def test_transport_modules_are_not_duplicated(self) -> None:
        # 装配层只做装配：漫画业务域不 import app（否则会出现第二份提示词/规划实现）。
        self.assertFalse(hasattr(app_comic_module.LLMComicPlanner, "call"))

    def test_build_comic_service_returns_none_without_image_profiles(self) -> None:
        from types import SimpleNamespace

        warnings: list[str] = []
        service = tool_runtime_comic_module.build_comic_service(
            SimpleNamespace(comic=None, config_dir=TMP_ROOT, logger=None),
            {},  # 没有生图 profile：漫画只能失败，因此不建服务（工具整组不注册）
            "",
            None,
            None,
            warnings,
        )
        self.assertIsNone(service)
        self.assertEqual(warnings, [])

    def test_build_comic_service_returns_none_when_disabled(self) -> None:
        from types import SimpleNamespace

        opts = SimpleNamespace(
            comic=SimpleNamespace(enabled=False), config_dir=TMP_ROOT, logger=None
        )
        self.assertIsNone(
            tool_runtime_comic_module.build_comic_service(
                opts, {"main": object()}, "main", None, None, []
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()