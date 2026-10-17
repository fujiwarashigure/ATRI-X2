"""``atri_x.media`` 的 Resolver 测试（``resolver_test.go`` 的逐条移植）。

对应上游：``internal/media/resolver_test.go``（11 个测试函数 / 627 行）。

测试基础设施（假 store / 假 HTTP 服务端 / 假 S3 服务端）从
:mod:`tests.test_media_manager` import —— 见那里的模块 docstring 说明。

逐条对照表（Go → 本文件）
------------------------
=========================================  ==========================================
``TestMaterializeWithLimitsIsolation``      :class:`TestMaterializeWithLimitsIsolation`
``TestMaterializeWithLimitsDownloadTimeout`` :class:`TestMaterializeDownloadTimeout`
``TestResolverRequestTotalAndIsolation``    :class:`TestResolverRequestTotalIsolation`
``TestImportCompressionAndResolverReuse``   :class:`TestImportCompressionAndReuse`
``TestImportImageLimits``                   :class:`TestImportImageLimits`
``TestResolverDoesNotCompressExistingMedia`` :class:`TestResolverDoesNotRecompress`
``TestImportURLAndFileCompressIdentically`` :class:`TestImportURLAndFileIdentical`
``TestImportURLUnknownSizeAndLimits``       :class:`TestImportURLUnknownSizeAndLimits`
``TestLocalPresignDoesNotRecordRemoteObject`` :class:`TestLocalPresignKeepsLocalMetadata`
``TestResolverUploadsLocalMediaAfterBackendChange`` :class:`TestUploadLocalAfterBackendChange`
``TestMaterializeAndResolveSanitizeMediaNames`` :class:`TestSanitizeMediaNames`
=========================================  ==========================================
"""

from __future__ import annotations

import base64
import hashlib
import os
import unittest
from typing import Any

from atri_x.llm.llm import (
    LLMMessage,
    MessageSegment,
    RoleTool,
    RoleUser,
    SegmentFile,
    SegmentImage,
    SegmentText,
    segments_content_text,
)
from atri_x.media import (
    IDPrefix,
    FileDeliveryConfig,
    ImportLimits,
    Input,
    LocalBackend,
    Materialize,
    MediaConfig,
    MediaError,
    new_configured,
)
from tests.test_media_manager import (
    AsyncMediaTestCase,
    FakeS3Server,
    MemoryStore,
    OriginServer,
    make_png,
)


class ResolverRemote(LocalBackend):
    """对应 Go ``resolverRemote``：统计上传次数并返回假的预签名 URL。"""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.puts = 0

    async def put(self, ctx: Any, id: str, data: bytes, size: int, content_type: str) -> str:
        self.puts += 1
        return await super().put(ctx, id, data, size, content_type)

    async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
        return "https://s3.test/" + media.id + "?X-Amz-Signature=temporary"


class TestMaterializeWithLimitsIsolation(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestMaterializeWithLimitsIsolation（4 源 × 6 上限）。"""

    async def test_isolation(self) -> None:
        server = OriginServer(body=b"1234", content_type="text/plain")
        self.addCleanup(server.close)
        source_dir = self.make_dir("source")
        path = os.path.join(source_dir, "source.txt")
        with open(path, "wb") as handle:
            handle.write(b"1234")

        manager, _store, _root = self.make_manager()
        manager.max_import_bytes = 4
        manager.download_timeout = 1.0
        sources = {
            "http": server.url,
            "chunked": server.url + "/chunked",
            "data": "data:text/plain;base64,MTIzNA==",
            "file": path,
        }
        for label, url in sources.items():
            for limit in (-1, 0, 1, 3, 4, 8):
                with self.subTest(f"{label}/{limit}"):
                    out = await self.guard(
                        manager.materialize_with_limits(
                            None,
                            [MessageSegment(type=SegmentFile, url=url)],
                            ImportLimits(max_import_bytes=limit),
                        )
                    )
                    want_ok = limit <= 0 or limit >= 4
                    self.assertEqual(len(out), 1)
                    self.assertEqual(bool(out[0].media), want_ok, out)

                    # 受限调用绝不能影响同一 Manager 上的默认导入。
                    default_out = await self.guard(
                        manager.materialize(None, [MessageSegment(type=SegmentFile, url=url)])
                    )
                    self.assertEqual(len(default_out), 1)
                    self.assertTrue(default_out[0].media, default_out)

                    existing = await self.guard(
                        manager.materialize_with_limits(
                            None, default_out, ImportLimits(max_import_bytes=1)
                        )
                    )
                    self.assertEqual(len(existing), 1)
                    self.assertEqual(existing[0].media, default_out[0].media)

                    self.assertEqual(manager.max_import_bytes, 4)
                    self.assertEqual(manager.download_timeout, 1.0)

    async def test_cannot_increase_global_limit(self) -> None:
        manager, _store, _root = self.make_manager()
        manager.max_import_bytes = 4
        out = await self.guard(
            manager.materialize_with_limits(
                None,
                [MessageSegment(type=SegmentFile, url="data:text/plain;base64,MTIzNDU=")],
                ImportLimits(max_import_bytes=8),
            )
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].media, "", "全局上限不能被单次调用放大")
        self.assertIn("媒体不可用", out[0].text)


class TestMaterializeDownloadTimeout(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestMaterializeWithLimitsDownloadTimeout。"""

    async def test_download_timeout(self) -> None:
        server = OriginServer(body=b"ok", content_type="text/plain", stall=True)
        self.addCleanup(server.close)
        manager, _store, _root = self.make_manager()
        manager.download_timeout = 1.0
        out = await self.guard(
            manager.materialize_with_limits(
                None,
                [MessageSegment(type=SegmentFile, url=server.url + "/stall")],
                ImportLimits(download_timeout=0.05),
            )
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].media, "", "挂住的下载必须被超时打断")
        self.assertEqual(manager.download_timeout, 1.0, "共享超时不能被改写")

        out = await self.guard(
            manager.materialize(None, [MessageSegment(type=SegmentFile, url=server.url)])
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].media, out)


class TestResolverRequestTotalIsolation(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestResolverRequestTotalAndIsolation。"""

    async def test_total_and_isolation(self) -> None:
        manager, store, _root = self.make_manager()
        first = await self.guard(
            manager.import_bytes(None, b"1234", Input(name="first.png", mime_type="image/png"))
        )
        second = await self.guard(
            manager.import_bytes(None, b"5678", Input(name="second.png", mime_type="image/png"))
        )
        messages = [
            LLMMessage(
                role=RoleUser, segments=[MessageSegment(type=SegmentImage, media=first.id)]
            ),
            LLMMessage(
                role=RoleTool, segments=[MessageSegment(type=SegmentImage, media=second.id)]
            ),
        ]
        remote = ResolverRemote(self.make_dir("remote"))
        manager.remote = remote

        table = (
            ("base64", 8, False, False),
            ("base64", 7, False, True),
            ("hybrid", 8, False, False),
            ("hybrid", 7, True, False),
            ("s3", 100, True, False),
        )
        for backend, limit, want_remote, want_error in table:
            with self.subTest(f"{backend}/{limit}"):
                manager.file_delivery.backend = backend
                manager.file_delivery.max_direct_base64_bytes = limit
                if want_error:
                    with self.assertRaises(MediaError):
                        await self.guard(manager.resolve_for_llm(None, messages))
                    continue
                out, cleanup = await self.guard(manager.resolve_for_llm(None, messages))
                cleanup()
                for message in out:
                    for segment in message.segments:
                        self.assertEqual(
                            segment.url.startswith("https://"), want_remote, segment.url
                        )
                        if not want_remote:
                            self.assertTrue(
                                segment.url.startswith("data:image/png;base64,"), segment.url
                            )

        self.assertEqual(remote.puts, 2, "两个媒体对象各提升一次，之后复用对象键")
        for message in messages:
            self.assertEqual(message.segments[0].url, "", "规范化输入不能被改写")
        stored = store.media().get(first.id)
        self.assertNotIn("X-Amz", stored.source_url + stored.object_key + stored.local_path)

        manager.file_delivery.backend = "base64"
        manager.file_delivery.max_direct_base64_bytes = 100
        missing = LLMMessage(
            role=RoleUser,
            segments=[MessageSegment(type=SegmentImage, media=IDPrefix + "0" * 64)],
        )
        out, cleanup = await self.guard(manager.resolve_for_llm(None, [missing]))
        cleanup()
        self.assertIn("媒体不可用", segments_content_text(out[0].segments))


class TestImportCompressionAndReuse(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestImportCompressionAndResolverReuse。"""

    async def test_compression_then_reuse(self) -> None:
        manager, _store, _root = self.make_manager()
        manager.media = MediaConfig(
            llm_image_compression_threshold_bytes=1024 * 1024, llm_image_max_length=32
        )
        data = make_png(80, 40)
        materialized = await self.guard(
            manager.materialize(
                None,
                [
                    MessageSegment(
                        type=SegmentImage,
                        name="large.png",
                        mime_type="image/png",
                        url="data:image/png;base64," + base64.b64encode(data).decode("ascii"),
                    )
                ],
            )
        )
        segment = materialized[0]
        self.assertTrue(segment.media)
        self.assertEqual(segment.name, "large.jpg")
        self.assertEqual(segment.mime_type, "image/jpeg")
        self.assertEqual(segment.url, "")

        stored_data, item = await self.guard(manager.read(None, segment.media))
        from atri_x.media import image_dimensions

        width, height = image_dimensions(stored_data)
        self.assertLess(width, 32)
        self.assertLess(height, 32)
        self.assertEqual(item.size, len(stored_data))
        self.assertEqual(item.id, IDPrefix + hashlib.sha256(stored_data).hexdigest())

        original_id = IDPrefix + hashlib.sha256(data).hexdigest()
        from atri_x.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            await self.guard(manager.metadata(None, original_id))

        remote = ResolverRemote(self.make_dir("remote"))
        manager.remote = remote
        messages = [
            LLMMessage(role=RoleUser, segments=[segment.clone(), segment.clone()])
        ]
        # 收紧导入限制不能重新压缩已经存下来的媒体。
        manager.media = MediaConfig(llm_image_compression_threshold_bytes=1, llm_image_max_length=1)
        for mode in ("base64", "hybrid", "s3"):
            manager.file_delivery.backend = mode
            manager.file_delivery.max_direct_base64_bytes = item.size * 2
            for _attempt in range(2):
                with self.subTest(f"{mode}"):
                    out, cleanup = await self.guard(manager.resolve_for_llm(None, messages))
                    cleanup()
                    cleanup()
                    got = out[0].segments[0]
                    want = "data:image/jpeg;base64," + base64.b64encode(stored_data).decode(
                        "ascii"
                    )
                    if mode == "s3":
                        want = "https://s3.test/" + item.id + "?X-Amz-Signature=temporary"
                    self.assertEqual(got.url, want)
                    self.assertEqual(got.media, item.id)
                    self.assertEqual(out[0].segments[1].url, want)

        manager.file_delivery.backend = "hybrid"
        manager.file_delivery.max_direct_base64_bytes = item.size * 2 - 1
        out, cleanup = await self.guard(manager.resolve_for_llm(None, messages))
        cleanup()
        self.assertTrue(
            out[0].segments[0].url.startswith("https://s3.test/" + item.id),
            out[0].segments[0].url,
        )
        self.assertEqual(remote.puts, 1, "同一对象只提升一次")
        self.assertEqual(messages[0].segments[0].url, "", "规范化输入不能被改写")


class TestImportImageLimits(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestImportImageLimits（9 个用例）。

    表列：``(用例名, 数据形态, MIME, 字节阈值, 边长上限, 导入上限增量, 是否压缩, 是否失败)``。
    """

    async def test_import_image_limits(self) -> None:
        from atri_x.media import image_dimensions

        base = make_png(80, 40)
        cases = (
            ("unchanged", base, "", len(base) + 1, 81, 0, False, False),
            ("byte boundary", base, "", len(base), 81, 0, False, False),
            ("bytes", base + bytes(4096), "", 2048, 81, 0, True, False),
            ("dimension boundary", base, "", 4096, 80, 0, True, False),
            (
                "generic MIME image",
                base,
                "application/octet-stream",
                4096,
                32,
                0,
                True,
                False,
            ),
            ("non image", b"ordinary file", "text/plain", 2, 2, 0, False, False),
            ("invalid image", b"invalid image", "image/png", 2, 32, 0, False, True),
            ("impossible compression", base, "", 1, 32, 0, False, True),
            ("hard import limit", base, "", 4096, 32, -1, False, True),
        )
        for label, data, mime, byte_limit, max_length, limit_delta, compressed, want_error in cases:
            with self.subTest(label):
                backend = ResolverRemote(self.make_dir("remote"))
                manager, _store, _root = self.make_manager()
                manager.backend = backend
                manager.local = backend
                manager.media = MediaConfig(
                    llm_image_compression_threshold_bytes=byte_limit,
                    llm_image_max_length=max_length,
                )
                if limit_delta < 0:
                    manager.max_import_bytes = len(base) - 1

                try:
                    item = await self.guard(
                        manager.import_reader(None, data, -1, Input(name="input", mime_type=mime))
                    )
                except MediaError:
                    if not want_error:
                        raise
                    self.assertEqual(backend.puts, 0, "失败的导入不能留下对象")
                    continue
                if want_error:
                    self.fail("expected import failure")

                got, _media = await self.guard(manager.read(None, item.id))
                if compressed:
                    width, height = image_dimensions(got)
                    self.assertLess(width, max_length)
                    self.assertLess(height, max_length)
                    self.assertLess(len(got), byte_limit)
                    self.assertEqual(item.mime_type, "image/jpeg")
                    self.assertEqual(item.name, "input.jpg")
                else:
                    self.assertEqual(got, data, "未压缩的内容必须原样保存")

                again = await self.guard(
                    manager.import_bytes(None, data, Input(name="input", mime_type=mime))
                )
                self.assertEqual(again.id, item.id)
                self.assertEqual(backend.puts, 1)


class TestResolverDoesNotRecompress(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestResolverDoesNotCompressExistingMedia。"""

    async def test_existing_media_untouched(self) -> None:
        manager, _store, _root = self.make_manager()
        data = make_png(80, 40)
        item = await self.guard(manager.import_bytes(None, data, Input(name="old.png")))
        manager.media = MediaConfig(llm_image_compression_threshold_bytes=1, llm_image_max_length=1)
        messages = [
            LLMMessage(role=RoleUser, segments=[MessageSegment(type=SegmentImage, media=item.id)])
        ]
        out, cleanup = await self.guard(manager.resolve_for_llm(None, messages))
        cleanup()
        got = out[0].segments[0]
        self.assertEqual(got.media, item.id)
        self.assertEqual(got.url, "data:image/png;base64," + base64.b64encode(data).decode("ascii"))


class TestImportURLAndFileIdentical(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestImportURLAndFileCompressIdentically。"""

    async def test_url_and_file_compress_identically(self) -> None:
        manager, _store, _root = self.make_manager()
        manager.media.llm_image_max_length = 32
        data = make_png(80, 40)
        path = os.path.join(self.make_dir("source"), "photo.png")
        with open(path, "wb") as handle:
            handle.write(data)
        server = OriginServer(body=data, content_type="image/png")
        self.addCleanup(server.close)

        from_url = await self.guard(manager.import_url(None, server.url + "/photo.png", Input()))
        from_file = await self.guard(manager.import_file(None, path, Input()))
        self.assertEqual(from_url.id, from_file.id)
        self.assertEqual(from_url.name, "photo.jpg")
        self.assertEqual(from_url.mime_type, "image/jpeg")


class TestImportURLUnknownSizeAndLimits(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestImportURLUnknownSizeAndLimits（resolver 侧的一半）。

    另一半（签名参数原样用于下载、清洗后的 ``SourceURL`` 为空、导入上限）在
    :class:`tests.test_media_manager.TestImportURLSanitizesSourceButDownloadsOriginal`。
    """

    async def test_invalid_data_url_degrades(self) -> None:
        manager, _store, _root = self.make_manager()
        invalid = await self.guard(
            manager.materialize(
                None,
                [MessageSegment(type=SegmentImage, url="data:image/png;base64,%%%")],
            )
        )
        self.assertIn("媒体不可用", invalid[0].text)
        self.assertEqual(invalid[0].type, SegmentText)

    async def test_empty_source_segment_is_left_untouched(self) -> None:
        """对应 Go ``default: continue``：没有来源的媒体段原样保留。"""
        manager, _store, _root = self.make_manager()
        original = MessageSegment(type=SegmentImage, name="placeholder.png")
        out = await self.guard(manager.materialize(None, [original]))
        self.assertEqual(out[0], original)
        self.assertEqual(out[0].type, SegmentImage)
        self.assertNotIn("媒体不可用", out[0].text)

        text = MessageSegment(type=SegmentText, text="hello")
        out = await self.guard(manager.materialize(None, [text]))
        self.assertEqual(out[0], text)


class TestLocalPresignKeepsLocalMetadata(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestLocalPresignDoesNotRecordRemoteObject。"""

    async def test_local_presign_fails_without_touching_metadata(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"local only", Input(name="local.png")))
        with self.assertRaises(MediaError):
            await self.guard(manager.presign_get(None, item.id, 3600))
        stored = store.media().get(item.id)
        self.assertEqual(stored.object_key, "")
        self.assertEqual(stored.local_path, item.local_path)
        self.assertEqual(stored.backend, "local")


class TestUploadLocalAfterBackendChange(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestResolverUploadsLocalMediaAfterBackendChange。"""

    async def test_upload_after_backend_change(self) -> None:
        from atri_x.net.httpclient import HTTPClient

        for mode in ("s3", "hybrid"):
            with self.subTest(mode):
                server = FakeS3Server()
                self.addCleanup(server.close)
                store = MemoryStore()
                root = self.make_dir("media")
                local = new_configured(
                    store, root, FileDeliveryConfig(backend="base64"), None
                )
                data = b"local image before backend change"
                item = await self.guard(
                    local.import_bytes(None, data, Input(name="old.png", mime_type="image/png"))
                )

                manager = new_configured(
                    store,
                    root,
                    FileDeliveryConfig(
                        backend=mode,
                        max_direct_base64_bytes=1,
                        s3_endpoint=server.url,
                        s3_region="auto",
                        s3_bucket="bucket",
                    ),
                    {"access_key": "access", "secret_key": "secret"},
                )
                self.assertIsNotNone(await self.guard(manager.remote_backend(None)))
                messages = [
                    LLMMessage(
                        role=RoleUser, segments=[MessageSegment(type=SegmentImage, media=item.id)]
                    )
                ]
                client = HTTPClient(timeout=5.0)
                for _attempt in range(2):
                    out, cleanup = await self.guard(manager.resolve_for_llm(None, messages))
                    cleanup()
                    url = out[0].segments[0].url
                    self.assertTrue(url.startswith(server.url + "/bucket/media/"), url)
                    response = await self.guard(client.get(url))
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.body, data)

                puts = [call for call in server.calls if call.startswith("PUT ")]
                self.assertEqual(len(puts), 1, f"应只上传一次并复用对象键：{server.calls}")
                stored = store.media().get(item.id)
                self.assertEqual(stored.backend, "local")
                self.assertTrue(stored.local_path)
                self.assertTrue(stored.object_key)


class TestSanitizeMediaNames(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestMaterializeAndResolveSanitizeMediaNames。"""

    async def test_names_sanitized_but_input_not_mutated(self) -> None:
        manager, _store, _root = self.make_manager()
        source_name = "https://example.com/private/photo.png?rkey=name-secret"
        materialized = await self.guard(
            manager.materialize(
                None,
                [
                    MessageSegment(
                        type=SegmentImage,
                        url="data:image/png;base64,aW1hZ2U=",
                        name=source_name,
                    )
                ],
            )
        )
        self.assertEqual(len(materialized), 1)
        self.assertTrue(materialized[0].media)
        self.assertEqual(materialized[0].url, "")
        self.assertEqual(materialized[0].name, "photo.png")

        messages = [
            LLMMessage(
                role=RoleUser,
                segments=[
                    MessageSegment(
                        type=SegmentImage, media=materialized[0].media, name=source_name
                    )
                ],
            )
        ]
        manager.file_delivery.backend = "base64"
        manager.file_delivery.max_direct_base64_bytes = 1024
        resolved, cleanup = await self.guard(manager.resolve_for_llm(None, messages))
        cleanup()
        self.assertEqual(resolved[0].segments[0].name, "photo.png")
        self.assertNotIn("name-secret", segments_content_text(resolved[0].segments))
        self.assertEqual(messages[0].segments[0].name, source_name, "规范化输入不能被改写")

        missing = [
            LLMMessage(
                role=RoleUser,
                segments=[
                    MessageSegment(
                        type=SegmentImage,
                        media=IDPrefix + "f" * 64,
                        name="https://example.com/missing.png?rkey=missing-secret",
                    )
                ],
            )
        ]
        resolved, cleanup = await self.guard(manager.resolve_for_llm(None, missing))
        cleanup()
        text = segments_content_text(resolved[0].segments)
        self.assertNotIn("missing-secret", text)
        self.assertIn("missing.png", text)


class TestResolverChooseTransportTable(unittest.TestCase):
    """传输选择的边界表（``base64`` 硬失败 vs ``hybrid`` 自动远端）。"""

    def test_boundary_matrix(self) -> None:
        cases = (
            ("base64", 8, 8, False, False),
            ("base64", 8, 7, False, True),
            ("hybrid", 8, 8, False, False),
            ("hybrid", 8, 7, True, False),
            ("s3", 8, 0, True, False),
        )
        for backend, total, limit, want_remote, want_error in cases:
            with self.subTest(f"{backend}/{total}/{limit}"):
                remote = backend == "s3" or (backend == "hybrid" and total > limit)
                error = (not remote) and total > limit
                self.assertEqual(remote, want_remote)
                self.assertEqual(error, want_error)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
