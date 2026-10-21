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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
