"""``atri_x.media.platform`` 的平台导入回归用例。

这里覆盖一次真实的移植事故：``import_platform`` 曾把 ``delivery.Source``
（带 ``mime_type`` / ``path`` / ``data`` / ``media_id``）误当成
``atri_x.media.types.Source``（只带 ``platform`` / ``url`` / ``file_id``），
于是

* ``source.mime_type`` 直接 ``AttributeError`` → 所有入站图片/文件导入都降级成
  ``[媒体不可用；…]``；
* 即使绕过这一行，``source.path`` / ``data`` / ``media_id`` 分支也取不到值。

回归要点：段自身没有 ``mime_type``（OneBot 图片段的常态）时，平台导入必须能走通，
并用解析器返回的 ``mime_type`` 补上；解析器给的 path / data / media_id 分支也要
各自被正确分派。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from atri_x.delivery import Source as DeliverySource
from atri_x.media.platform import import_platform
from atri_x.media.types import MediaError
from atri_x.platform.platform import MessageSegment, MessageSegmentType


def asyncio_run(awaitable):  # noqa: ANN001
    return asyncio.run(awaitable)


class _RecordingManager:
    """记录分派结果的假 Manager（只实现 ImportPlatform 用到的入口）。"""

    def __init__(self, max_import_bytes: int = 1024) -> None:
        self.max_import_bytes = max_import_bytes
        self.calls: list[tuple[str, object]] = []

    def _media(self, tag: str) -> SimpleNamespace:
        return SimpleNamespace(
            id="media:" + tag * 64,
            name="stored.png",
            mime_type="image/png",
            size=1,
        )

    async def import_url(self, ctx, url, spec):  # noqa: ANN001
        self.calls.append(("url", (url, spec)))
        return self._media("a")

    async def import_file(self, ctx, path, spec):  # noqa: ANN001
        self.calls.append(("file", (path, spec)))
        return self._media("b")

    async def import_bytes(self, ctx, data, spec):  # noqa: ANN001
        self.calls.append(("bytes", (data, spec)))
        return self._media("c")

    async def metadata(self, ctx, id):  # noqa: ANN001
        self.calls.append(("metadata", id))
        return self._media("d")


class _StaticResolver:
    def __init__(self, source: DeliverySource) -> None:
        self.source = source
        self.calls: list[tuple[MessageSegment, int]] = []

    async def resolve_media(self, ctx, segment, max_bytes):  # noqa: ANN001
        self.calls.append((segment, max_bytes))
        return self.source


def _image_segment(**overrides: object) -> MessageSegment:
    values = {
        "type": MessageSegmentType.IMAGE,
        "url": "https://example.com/a.png",
        "name": "{2324B3CB}.png",
        "mime_type": "",
        "size": 0,
    }
    values.update(overrides)
    return MessageSegment(**values)


class ImportPlatformTest(unittest.TestCase):
    def test_url_segment_without_mime_type_does_not_crash(self) -> None:
        manager = _RecordingManager()
        spec_media = asyncio_run(
            import_platform(manager, None, "qqonebot", None, _image_segment())
        )
        self.assertEqual(spec_media.id, "media:" + "a" * 64)
        kind, (url, spec) = manager.calls[0]
        self.assertEqual(kind, "url")
        self.assertEqual(url, "https://example.com/a.png")
        self.assertEqual(spec.name, "{2324B3CB}.png")
        # 段里没有 mime_type、也没有解析器：留给下载时的 Content-Type 补全。
        self.assertEqual(spec.mime_type, "")

    def test_resolved_mime_type_is_applied(self) -> None:
        manager = _RecordingManager()
        resolver = _StaticResolver(
            DeliverySource(url="https://example.com/b.png", mime_type="image/png")
        )
        asyncio_run(
            import_platform(
                manager, None, "qqonebot", resolver, _image_segment(url="")
            )
        )
        kind, (url, spec) = manager.calls[0]
        self.assertEqual(kind, "url")
        self.assertEqual(url, "https://example.com/b.png")
        self.assertEqual(spec.mime_type, "image/png")

    def test_resolver_path_uses_import_file(self) -> None:
        manager = _RecordingManager()
        resolver = _StaticResolver(DeliverySource(path=r"C:\tmp\a.png"))
        asyncio_run(
            import_platform(manager, None, "qqonebot", resolver, _image_segment(url=""))
        )
        self.assertEqual(manager.calls[0][0], "file")
        self.assertEqual(manager.calls[0][1][0], r"C:\tmp\a.png")

    def test_resolver_data_uses_import_bytes(self) -> None:
        manager = _RecordingManager()
        resolver = _StaticResolver(DeliverySource(data=b"png"))
        asyncio_run(
            import_platform(manager, None, "qqonebot", resolver, _image_segment(url=""))
        )
        self.assertEqual(manager.calls[0][0], "bytes")
        self.assertEqual(manager.calls[0][1][0], b"png")

    def test_resolver_media_id_uses_metadata(self) -> None:
        manager = _RecordingManager()
        resolver = _StaticResolver(DeliverySource(media_id="media:" + "e" * 64))
        asyncio_run(
            import_platform(manager, None, "qqonebot", resolver, _image_segment(url=""))
        )
        self.assertEqual(manager.calls[0], ("metadata", "media:" + "e" * 64))

    def test_segment_media_id_short_circuits_metadata(self) -> None:
        manager = _RecordingManager()
        asyncio_run(
            import_platform(
                manager,
                None,
                "qqonebot",
                None,
                _image_segment(media_id="media:" + "f" * 64),
            )
        )
        self.assertEqual(manager.calls[0], ("metadata", "media:" + "f" * 64))

    def test_oversize_segment_is_rejected_before_download(self) -> None:
        manager = _RecordingManager(max_import_bytes=10)
        with self.assertRaises(MediaError):
            asyncio_run(
                import_platform(manager, None, "qqonebot", None, _image_segment(size=11))
            )
        self.assertEqual(manager.calls, [])


def asyncio_run(awaitable):  # noqa: ANN001
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
