"""``atri_x.agent.segments.materialize_media`` 的异步语义。

对应 Go ``Agent.materializeMedia``：Go 侧 ``media.Materialize`` 是同步的，Python 版
要下远端媒体所以是异步；调用方必须 ``await``，否则拿到的是 coroutine，后续
``segments_content_text(...)`` 迭代时报 ``'coroutine' object is not iterable``。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from atri_x.agent.segments import materialize_media
from atri_x.llm import MessageSegment, SegmentType


def text_segment(text: str) -> MessageSegment:
    return MessageSegment(type=SegmentType.TEXT, text=text)


class AsyncMedia:
    """只有异步 ``materialize``（``atri_x.media.Manager`` 的形状）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def materialize(self, _ctx, segments):
        self.calls += 1
        return [text_segment(segment.text + "!") for segment in segments]


class SyncMedia:
    """同步 ``materialize``（seam / 测试替身的形状）。"""

    async def _never(self) -> None:  # pragma: no cover - 只是占位
        return None

    def materialize(self, _ctx, segments):
        return list(segments)


class TestMaterializeMedia(unittest.IsolatedAsyncioTestCase):
    async def test_awaits_async_media(self) -> None:
        media = AsyncMedia()
        agent = SimpleNamespace(media=media)
        out = await materialize_media(agent, None, [text_segment("hi")])
        self.assertEqual([segment.text for segment in out], ["hi!"])
        self.assertEqual(media.calls, 1)

    async def test_accepts_sync_media(self) -> None:
        agent = SimpleNamespace(media=SyncMedia())
        out = await materialize_media(agent, None, [text_segment("hi")])
        self.assertEqual([segment.text for segment in out], ["hi"])

    async def test_without_media_returns_copies(self) -> None:
        original = [text_segment("hi")]
        out = await materialize_media(SimpleNamespace(media=None), None, original)
        self.assertEqual(len(out), 1)
        self.assertIsNot(out[0], original[0])
        self.assertEqual(out[0].text, "hi")
