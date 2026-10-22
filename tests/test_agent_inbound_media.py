"""入站平台媒体 → 历史位序关联的回归测试。

对应 Go：``internal/agent/inbound_media.go``（``associateInboundHistory`` /
``materializePlatformMedia``）。Go 侧没有独立测试文件，这里按行为补。

真机 bug（0.9.12 群聊发图）：``_history_lookup`` 给**同步**仓储
:meth:`atri_x.storage.repositories.ChatHistoryRepository.get_by_platform_message`
多传了一个 ``ctx``，抛出的 ``TypeError`` 又被 ``associate_inbound_history``
吞成一条 WARN，于是入站图片永远关联不到历史消息位序——``get_media`` /
``view_image`` 随后按位序取图时拿不到本地 media ID。

这里的假仓储与协议签名**逐字一致**（同步、不带 ``ctx``），多传参数会立刻失败。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from atri_x.agent.inbound_media import associate_inbound_history
from atri_x.errors import NotFoundError
from atri_x.platform import SegmentFile, SegmentImage

MEDIA_A = "media:" + "a" * 64
MEDIA_B = "media:" + "b" * 64


class FakeHistory:
    """与 ``ChatHistoryRepository`` 同签名的假仓储（同步，不带 ``ctx``）。"""

    def __init__(self, row: object = None, error: BaseException | None = None) -> None:
        self.row = row
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def get_by_platform_message(
        self, platform: str, scope_id: str, platform_message_id: str
    ) -> object:
        self.calls.append((platform, scope_id, platform_message_id))
        if self.error is not None:
            raise self.error
        if self.row is None:
            raise NotFoundError("history row not found")
        return self.row


class FakeMedia:
    def __init__(self, history: object = None) -> None:
        self.history = history
        self.associations: list[tuple] = []

    async def associate_history(self, ctx, row, index, kind, media_id) -> None:  # noqa: ANN001
        self.associations.append((row, index, kind, media_id))


def _agent(history: object = None) -> SimpleNamespace:
    return SimpleNamespace(media=FakeMedia(history))


def _message(platform: str = "qqonebot", scope_id: str = "group:1") -> SimpleNamespace:
    return SimpleNamespace(platform=platform, scope_id=scope_id)


def _run(coro):
    return asyncio.run(coro)


class AssociateInboundHistoryTest(unittest.TestCase):
    def test_links_media_with_real_repository_signature(self) -> None:
        row = SimpleNamespace(id="h1")
        history = FakeHistory(row)
        agent = _agent(history)
        original = [
            SimpleNamespace(type="text"),
            SimpleNamespace(type=SegmentImage),
            SimpleNamespace(type=SegmentFile),
        ]
        resolved = [
            SimpleNamespace(media_id=""),
            SimpleNamespace(media_id=MEDIA_A),
            SimpleNamespace(media_id=MEDIA_B),
        ]
        _run(
            associate_inbound_history(agent, None, _message(), "pm1", original, resolved)
        )
        self.assertEqual(history.calls, [("qqonebot", "group:1", "pm1")])
        self.assertEqual(
            agent.media.associations,
            [(row, 1, SegmentImage, MEDIA_A), (row, 2, SegmentFile, MEDIA_B)],
        )

    def test_missing_history_row_is_ignored(self) -> None:
        agent = _agent(FakeHistory(None))
        _run(
            associate_inbound_history(
                agent,
                None,
                _message(),
                "pm1",
                [SimpleNamespace(type=SegmentImage)],
                [SimpleNamespace(media_id=MEDIA_A)],
            )
        )
        self.assertEqual(agent.media.associations, [])

    def test_unavailable_history_is_ignored(self) -> None:
        # 历史库报错不能挡住消息处理（与 Go 一样只记 WARN）。
        agent = _agent(FakeHistory(error=RuntimeError("history offline")))
        _run(
            associate_inbound_history(
                agent,
                None,
                _message(),
                "pm1",
                [SimpleNamespace(type=SegmentImage)],
                [SimpleNamespace(media_id=MEDIA_A)],
            )
        )
        self.assertEqual(agent.media.associations, [])

    def test_no_history_repository_is_noop(self) -> None:
        agent = _agent(None)
        _run(
            associate_inbound_history(
                agent,
                None,
                _message(),
                "pm1",
                [SimpleNamespace(type=SegmentImage)],
                [SimpleNamespace(media_id=MEDIA_A)],
            )
        )
        self.assertEqual(agent.media.associations, [])


class _WarnLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warn(self, message: str, **fields) -> None:  # noqa: ANN003
        self.warnings.append((message, fields))


class _FailingMedia:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def import_platform(self, ctx, platform, resolver, segment) -> None:  # noqa: ANN001
        raise self.error


class MaterializeSegmentFailureTest(unittest.TestCase):
    """降级成 ``[媒体不可用；…]`` 时必须留一条 WARN，不能再静默。

    真机事故（1.0.0）：磁盘守卫抛 ``AttributeError`` 被这里吞掉，日志里一个字都没有，
    "群聊每张图都读不到"只能靠猜。
    """

    def test_failed_import_degrades_and_warns(self) -> None:
        from atri_x.agent.inbound_media import materialize_platform_segment
        from atri_x.platform.platform import MessageSegment, MessageSegmentType

        logger = _WarnLogger()
        agent = SimpleNamespace(
            media=_FailingMedia(AttributeError("boom")), logger=logger
        )
        segment = MessageSegment(
            type=MessageSegmentType.IMAGE, name="{B1E2D3F4}.jpg", url="https://example.invalid/a.jpg"
        )
        msg = SimpleNamespace(platform="qqonebot", platform_message_id="9000000001")
        out = _run(materialize_platform_segment(agent, {"platform": "qqonebot"}, msg, segment))
        self.assertEqual(out.type, "text")
        self.assertIn("媒体不可用", out.text)
        self.assertIn("{B1E2D3F4}.jpg", out.text)
        self.assertEqual(len(logger.warnings), 1)
        message, fields = logger.warnings[0]
        self.assertIn("inbound platform media", message)
        self.assertIn("boom", fields["error"])
        self.assertEqual(fields["segment_name"], "{B1E2D3F4}.jpg")
        self.assertEqual(fields["segment_type"], "image")
        self.assertEqual(fields["message_id"], "9000000001")
        # 来源 URL 可能带凭据：诊断字段里不能出现。
        self.assertNotIn("example.invalid", repr(fields))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
