"""视觉预处理线程化的回归测试：纯 CPU 工作不能冻住事件循环。

审计背景（本轮静默降级审计 §5 / 研究报告 §5）：``vision/service.py``
与 ``tool/builtin/view_image.py`` 曾在异步函数里**同步**调用
``prepare_vision_image_context``（实测 3.1 MPix 图冻结事件循环 15.7 秒）。修复是
``asyncio.to_thread``；这里用一个 0.02 秒 ticker 观察"准备图片期间事件循环还活着"。

断言方式是**行为级**的：把预处理替换成睡 0.3 秒的函数，期间 ticker 必须至少跑过一次。
同步实现下 ticker 一次都跑不了（事件循环被占满），所以这是能区分新旧实现的判据，
不依赖实现细节。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from atri_x.llm.llm import StreamChunk
from atri_x.tool.builtin import view_image as view_image_module
from atri_x.tool.builtin.view_image import new_view_image_tool
from atri_x.vision import Options, Prompt, Request, Service
from tests._vision_fakes import FakeLLM

#: ticker 间隔：远小于预处理的 0.3 秒，同步实现下会是 0 次。
_TICK_INTERVAL = 0.02
#: 假预处理的耗时：足够 ticker 跑很多次，又不至于拖慢用例。
_PREPARE_SECONDS = 0.3


def _slow_prepare(seconds: float = _PREPARE_SECONDS):
    """替代 ``prepare_vision_image_context``：纯 CPU 阻塞（``time.sleep``）。"""

    def _prepare(ctx, data, mime_type, max_edge, max_bytes):
        del ctx, max_edge, max_bytes
        time.sleep(seconds)
        return data, mime_type or "image/png"

    return _prepare


async def _run_with_ticker(operation) -> tuple[object, list[float]]:
    """跑 ``operation()``，同时按固定间隔记录事件循环还活着。"""
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            await asyncio.sleep(_TICK_INTERVAL)
            ticks.append(time.monotonic())

    task = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0)  # 让 ticker 先进入第一次等待
        result = await operation()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return result, ticks


def _vision_request() -> Request:
    return Request(
        media_id="media:" + "b" * 64,
        data=b"fake-image-bytes",
        mime_type="image/png",
        prompt=Prompt(version="image-to-prompt.v1", system="sys", user="usr"),
    )


class _FakeCenter:
    """``view_image`` 只需要 ``read_limited`` 返回 (bytes, meta)。"""

    async def read_limited(self, ctx, id, limit):
        del ctx, id, limit
        return b"\x89PNG-fake", SimpleNamespace(mime_type="image/png")


class TestServicePreprocessIsThreaded(unittest.IsolatedAsyncioTestCase):
    """``vision.Service._run`` 的预处理不能占着事件循环。"""

    async def test_describe_keeps_the_loop_alive_during_preprocess(self):
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = Service(
            Options(
                client=client,
                provider="openai",
                model="vision-model",
                max_tokens=16,
                temperature=0.0,
                max_edge=0,
                max_image_bytes=0,
                timeout=5.0,
                shared_timeout=5.0,
            )
        )
        with mock.patch(
            "atri_x.vision.service.prepare_vision_image_context",
            side_effect=_slow_prepare(),
        ) as prepare:
            result, ticks = await _run_with_ticker(
                lambda: service.describe(None, _vision_request())
            )
        self.assertEqual(result.text, "ok")
        self.assertEqual(prepare.call_count, 1)
        self.assertGreaterEqual(
            len(ticks),
            1,
            "预处理期间事件循环被冻住：0.3 秒里 ticker 一次都没跑（仍在同步调用？）",
        )


class TestViewImageValidationIsThreaded(unittest.IsolatedAsyncioTestCase):
    """``view_image._validate_image`` 的解码校验同样必须线程化。"""

    async def test_validate_image_keeps_the_loop_alive(self):
        tool = new_view_image_tool(None, _FakeCenter())
        item = SimpleNamespace(id="media:" + "a" * 64, mime_type="image/png")
        with mock.patch.object(
            view_image_module,
            "prepare_vision_image_context",
            side_effect=_slow_prepare(),
        ) as prepare:
            _result, ticks = await _run_with_ticker(
                lambda: tool._validate_image(None, item)
            )
        self.assertEqual(prepare.call_count, 1)
        self.assertGreaterEqual(
            len(ticks),
            1,
            "校验期间事件循环被冻住：0.3 秒里 ticker 一次都没跑（仍在同步调用？）",
        )


if __name__ == "__main__":
    unittest.main()
