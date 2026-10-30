"""``comic_plan`` / ``comic_render`` 工具层测试。

工具层只做三件事：参数解码、调用服务、把结果包成 Result（预览文本 / 图片段 /
可投递 outputs）。真正的业务在 ``tests/test_comic_service.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.comic import ComicOptions, ComicService, ComicStore, PlanRequest
from atri_x.imagegen import Config as ImageConfig
from atri_x.tool.builtin.comic import (
    COMIC_PLAN_NAME,
    COMIC_RENDER_NAME,
    ComicPlanTool,
    ComicRenderTool,
    new_comic_tools,
    render_plan_preview,
)
from atri_x.tool.builtin.register import RegisterOptions, register_all
from atri_x.tool.tool import CallRequest, Registry
from tests._comic_fakes import (
    FakeAvatarResolver,
    FakeCharacterStore,
    FakeHistory,
    FakeImageClient,
    FakeMediaManager,
    build_context,
    make_rows,
)

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_comic_test")


class FakeProfile:
    def __init__(self, name: str, client, config) -> None:
        self.name = name
        self.client = client
        self.config = config


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
    )
    base.update(overrides)
    return ImageConfig(**base)


def _service(client=None, **kwargs) -> ComicService:
    return ComicService(
        kwargs.pop("options", ComicOptions(send_by_default=True)),
        store=ComicStore(os.path.join(TMP_ROOT, "comic")),
        history=FakeHistory(make_rows(["今天去吃火锅吧", "好啊，我请客"])),
        characters=FakeCharacterStore(),
        media=FakeMediaManager(),
        profiles={"main": FakeProfile("main", client or FakeImageClient(), _config())},
        default_profile="main",
        avatar_resolver=FakeAvatarResolver(
            {("qqonebot", "1001"): "https://a/1.png", ("qqonebot", "1002"): "https://a/2.png"}
        ),
        **kwargs,
    )


def _req(payload: dict) -> CallRequest:
    return CallRequest(arguments=json.dumps(payload, ensure_ascii=False))


def _run(coro):
    return asyncio.run(coro)


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_tools_register_only_with_comic_service(self) -> None:
        registry = Registry()
        register_all(registry, RegisterOptions())
        names = {info.name for info in registry.list()}
        self.assertNotIn(COMIC_PLAN_NAME, names)
        self.assertNotIn(COMIC_RENDER_NAME, names)

        registry = Registry()
        register_all(registry, RegisterOptions(comic=_service()))
        names = {info.name for info in registry.list()}
        self.assertIn(COMIC_PLAN_NAME, names)
        self.assertIn(COMIC_RENDER_NAME, names)

    def test_new_comic_tools_returns_both(self) -> None:
        tools = new_comic_tools(_service())
        self.assertEqual([tool.name() for tool in tools], [COMIC_PLAN_NAME, COMIC_RENDER_NAME])

    def test_render_info_follows_default_profile_superadmin_only(self) -> None:
        service = _service()
        service.profiles["main"] = FakeProfile(
            "main", FakeImageClient(), _config(superadmin_only=True)
        )
        self.assertTrue(ComicRenderTool(service).info().superadmin_only)
        self.assertFalse(ComicRenderTool(_service()).info().superadmin_only)


class PlanToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_plan_returns_preview_and_plan_id(self) -> None:
        client = FakeImageClient()
        service = _service(client=client)
        tool = ComicPlanTool(service)
        result = _run(
            tool.call(
                build_context(),
                _req({"panel_count": 2, "character_mode": "avatars", "title": "火锅之夜"}),
            )
        )
        self.assertIn("漫画分镜已规划", result.content)
        self.assertIn("火锅之夜", result.content)
        self.assertIn("comic_render(plan_id=", result.content)
        self.assertIn("今天去吃火锅吧", result.content)
        self.assertEqual(result.outputs, [])
        self.assertEqual(client.requests, [])
        self.assertTrue(result.metadata.get("plan_id"))
        self.assertEqual(json.loads(result.data)["panel_count"], 2)

    def test_plan_error_is_reported_as_content(self) -> None:
        service = _service()
        tool = ComicPlanTool(service)
        result = _run(
            tool.call(build_context(), _req({"panel_count": 2, "anchor": "msg-404"}))
        )
        self.assertIn("锚点", result.content)

    def test_plan_bad_character_mode_is_reported(self) -> None:
        service = _service()
        result = _run(
            ComicPlanTool(service).call(
                build_context(), _req({"panel_count": 2, "character_mode": "nope"})
            )
        )
        self.assertIn("auto", result.content)

    def test_plan_preview_lists_reference_numbers(self) -> None:
        service = _service()
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=2, character_mode="avatars"),
                viewer=None,
            )
        )
        text = render_plan_preview(outcome.plan)
        self.assertIn("参考图#1", text)
        self.assertIn("参考图#2", text)
        self.assertIn("取材：", text)


class RenderToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _plan_id(self, service: ComicService, panel_count: int = 2) -> str:
        outcome = _run(
            service.plan(
                build_context(),
                PlanRequest(panel_count=panel_count, character_mode="avatars"),
                viewer=None,
            )
        )
        return outcome.plan.plan_id

    def test_render_returns_image_segment_and_output(self) -> None:
        client = FakeImageClient()
        service = _service(client=client)
        plan_id = self._plan_id(service)
        tool = ComicRenderTool(service)
        result = _run(tool.call(build_context(), _req({"plan_id": plan_id, "send": True})))
        self.assertIn("已生成整页漫画", result.content)
        self.assertIn("media:", result.content)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[1].type, "image")
        self.assertEqual(len(result.outputs), 1)
        self.assertTrue(result.metadata.get("task_id"))

    def test_render_without_send_has_no_outputs(self) -> None:
        service = _service()
        plan_id = self._plan_id(service)
        result = _run(
            ComicRenderTool(service).call(build_context(), _req({"plan_id": plan_id, "send": False}))
        )
        self.assertEqual(result.outputs, [])

    def test_render_requires_plan_id(self) -> None:
        result = _run(
            ComicRenderTool(_service()).call(build_context(), _req({}))
        )
        self.assertIn("plan_id", result.content)

    def test_render_unknown_plan_is_reported(self) -> None:
        result = _run(
            ComicRenderTool(_service()).call(build_context(), _req({"plan_id": "nope"}))
        )
        self.assertIn("不存在", result.content)

    def test_render_dry_run(self) -> None:
        client = FakeImageClient()
        service = _service(client=client)
        plan_id = self._plan_id(service)
        result = _run(
            ComicRenderTool(service).call(
                build_context(), _req({"plan_id": plan_id, "dry_run": True})
            )
        )
        self.assertIn("dry_run", result.content)
        self.assertIn("最终提示词", result.content)
        self.assertEqual(client.requests, [])
        payload = json.loads(result.data)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(len(payload["references"]), 2)

    def test_render_resend_uses_task_id(self) -> None:
        client = FakeImageClient()
        service = _service(client=client)
        plan_id = self._plan_id(service)
        tool = ComicRenderTool(service)
        first = _run(tool.call(build_context(), _req({"plan_id": plan_id, "send": False})))
        task_id = first.metadata["task_id"]
        second = _run(
            tool.call(build_context(), _req({"plan_id": plan_id, "task_id": task_id}))
        )
        self.assertIn("重发", second.content)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(second.metadata["media_id"], first.metadata["media_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
