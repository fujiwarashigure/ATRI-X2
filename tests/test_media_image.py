"""``atri_x.media`` 的图片压缩与视觉预处理测试。

对应上游：``internal/media/image_test.go``（11 个测试函数 / 214 行），
外加 ``image.go`` 里没有独立 Go 测试但被 import 路径依赖的判定函数
（``imageDimensions`` / ``importedImage`` / ``shouldCompressImage`` /
``compressedName`` / ``visionImageMIMEType`` / ``verifyVisionImage``）
与 :mod:`atri_x.media.worker` 的工作进程协议。

与 Go 测试的差异：Go 用 ``image/jpeg`` 解码结果断言像素；本仓用
:mod:`atri_x.media.imagecodec` 的纯 Python 解码（可选 Pillow）。重采样核不同
（见 :mod:`atri_x.media.image` 的模块 docstring），因此**尺寸**逐字断言、
**像素**只断言"透明区域合成为白底"这种量级判定。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import struct
import unittest
import zlib

from atri_x.media import (
    VISION_IMAGE_DEFAULT_BYTE_BUDGET,
    VISION_IMAGE_HARD_LIMIT,
    VISION_IMAGE_MAX_INPUT_EDGE,
    VISION_IMAGE_MAX_INPUT_PIXELS,
    VisionImageInputLimit,
    MediaError,
    compress_image,
    compress_image_isolated,
    compressed_name,
    image_dimensions,
    imported_image,
    prepare_vision_image,
    prepare_vision_image_context,
    should_compress_image,
    verify_vision_image,
    vision_image_mime_type,
)
from atri_x.media.imagecodec import Image, ImageDecodeError, decode, encode_png, sniff, to_rgb
from atri_x.media.worker import (
    IMAGE_WORKER_ARG,
    IMAGE_WORKER_TIMEOUT,
    ImageWorkerRequest,
    ImageWorkerResponse,
    decode_worker_request,
    decode_worker_response,
    encode_worker_request,
    encode_worker_response,
    execute_worker_request,
    main,
    run_image_worker,
    run_isolated,
    truncate_worker_error,
)
from atri_x.safecontext import CancelToken
from tests.test_media_manager import AsyncMediaTestCase, make_image, make_png, png_with_text


def transparent_png(width: int, height: int) -> bytes:
    """左半不透明红、右半完全透明（对应 Go 测试里 x<40 的 NRGBA 构造）。"""
    data = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            base = (y * width + x) * 4
            if x < width // 2:
                data[base] = 255
                data[base + 3] = 255
    return encode_png(Image(width, height, "RGBA", data))


def crafted_png_header(width: int, height: int) -> bytes:
    """改写 IHDR 的宽高并修好 CRC：能读出声明尺寸，但不是可解码的图片。

    这正是 :func:`image_dimensions` 这种"只读头部的廉价预检"要挡的东西。
    """
    data = bytearray(make_png(8, 8))
    if bytes(data[12:16]) != b"IHDR":
        raise AssertionError("unexpected png layout")
    struct.pack_into(">I", data, 16, width)
    struct.pack_into(">I", data, 20, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


class TestCompressImage(unittest.TestCase):
    """``image_test.go`` 的 TestCompressImageProducesBoundedJPEGWithWhiteBackground。"""

    def test_bounded_jpeg_with_white_background(self) -> None:
        source = transparent_png(80, 40)
        output = compress_image(source, 16 * 1024, 32)
        self.assertLess(len(output), 16 * 1024)
        decoded = to_rgb(decode(output))
        self.assertEqual((decoded.width, decoded.height), (31, 15), "maxLength-1 的取整")
        white = decoded.get_pixel(decoded.width - 2, decoded.height // 2)
        self.assertGreaterEqual(white[0], 240)
        self.assertGreaterEqual(white[1], 240)
        self.assertGreaterEqual(white[2], 240)

    def test_rejects_impossible_limits(self) -> None:
        with self.assertRaises(MediaError):
            compress_image(make_png(8, 8), 1, 32)
        with self.assertRaises(MediaError):
            compress_image(make_png(8, 8), 1024, 1)

    def test_rejects_undecodable_input(self) -> None:
        with self.assertRaises(MediaError):
            compress_image(b"not an image", 1024, 32)

    def test_survives_tight_budget_by_shrinking(self) -> None:
        output = compress_image(make_png(200, 200, noise=True), 4096, 64)
        self.assertLess(len(output), 4096)
        width, height = image_dimensions(output)
        self.assertLessEqual(max(width, height), 64)


class TestPrepareVisionImage(unittest.TestCase):
    """``image_test.go`` 的 TestPrepareVisionImage* 系列。"""

    def test_keeps_small_image_untouched(self) -> None:
        data = make_png(8, 8)
        output, mime_type = prepare_vision_image(data, "image/png", 64, 1024 * 1024)
        self.assertEqual(output, data)
        self.assertEqual(mime_type, "image/png")

    def test_downscales_long_edge(self) -> None:
        data = make_png(200, 100)
        output, mime_type = prepare_vision_image(data, "image/png", 64, 0)
        self.assertEqual(mime_type, "image/jpeg")
        width, height = image_dimensions(output)
        self.assertLessEqual(max(width, height), 64)

    def test_enforces_byte_budget(self) -> None:
        # 输入体积必须与编码后端无关：同一张"噪声"图，Pillow 与纯标准库后端压出来
        # 的 PNG 差好几倍（CI 没装 Pillow，256×256 压完只有 ~2 KB，比预算还小，
        # 于是 `assertGreater` 先炸）。改用 PNG tEXt 文本块把体积撑到预算之上——
        # 这同时是真实场景：A1111 / NovelAI 会把生成参数写进这种文本块。
        data = png_with_text(
            make_png(256, 256, noise=True), keyword=b"parameters", text=b"steps: 20, " * 1024
        )
        budget = 4 * 1024
        self.assertGreater(len(data), budget)
        output, mime_type = prepare_vision_image(data, "image/png", 0, budget)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertLess(len(output), budget)

    def test_passes_undecodable_through(self) -> None:
        data = b"this is not an image"
        output, mime_type = prepare_vision_image(data, "application/octet-stream", 64, 4096)
        self.assertEqual(output, data)
        self.assertEqual(mime_type, "application/octet-stream")

    def test_rejects_undecodable_over_budget(self) -> None:
        with self.assertRaises(MediaError):
            prepare_vision_image(b"this is not an image", "application/octet-stream", 64, 8)

    def test_rejects_unsafe_decode_sizes(self) -> None:
        for label, (width, height) in {
            "oversized edge": (VISION_IMAGE_MAX_INPUT_EDGE + 1, 8),
            "oversized pixels": (12000, 4000),
        }.items():
            with self.subTest(label):
                data = crafted_png_header(width, height)
                with self.assertRaises(MediaError):
                    prepare_vision_image(data, "image/png", 64, 0)

    def test_honours_both_limits(self) -> None:
        data = make_png(400, 200, noise=True)
        budget = 5120
        output, mime_type = prepare_vision_image(data, "image/png", 64, budget)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertLessEqual(len(output), budget)
        width, height = image_dimensions(output)
        self.assertLessEqual(max(width, height), 64)
        verify_vision_image(output, 64, budget)

    def test_rejects_empty(self) -> None:
        with self.assertRaises(MediaError):
            prepare_vision_image(b"", "image/png", 64, 1024)

    def test_corrects_mismatched_mime_type(self) -> None:
        data = make_png(8, 8)
        output, mime_type = prepare_vision_image(data, "image/jpeg", 64, 1024 * 1024)
        self.assertEqual(output, data)
        self.assertEqual(mime_type, "image/png", "以内容嗅探为准")

    def test_rejects_oversized_payload(self) -> None:
        with self.assertRaises(MediaError):
            prepare_vision_image(b"\x00" * (VISION_IMAGE_HARD_LIMIT + 1), "application/octet-stream", 0, 0)

    def test_constants_match_go(self) -> None:
        self.assertEqual(VISION_IMAGE_HARD_LIMIT, 64 * 1024 * 1024)
        self.assertEqual(VisionImageInputLimit, VISION_IMAGE_HARD_LIMIT)
        self.assertEqual(VISION_IMAGE_DEFAULT_BYTE_BUDGET, 12 * 1024 * 1024)
        self.assertEqual(VISION_IMAGE_MAX_INPUT_EDGE, 20000)
        self.assertEqual(VISION_IMAGE_MAX_INPUT_PIXELS, 24000000)


class TestPrepareVisionImageCancellation(unittest.TestCase):
    """``image_test.go`` 的 TestPrepareVisionImageContextCancellation。"""

    def test_cancelled_context(self) -> None:
        token = CancelToken.with_cancel()
        token.cancel("test")
        with self.assertRaises(Exception):
            prepare_vision_image_context(token, make_png(400, 200, noise=True), "image/png", 64, 0)

    def test_uncancelled_context_passes(self) -> None:
        token = CancelToken.with_cancel()
        output, mime_type = prepare_vision_image_context(
            token, make_png(400, 200), "image/png", 64, 0
        )
        self.assertEqual(mime_type, "image/jpeg")
        token.cancel("done")


class TestImageHelpers(unittest.TestCase):
    def test_image_dimensions_only_reads_header(self) -> None:
        self.assertEqual(image_dimensions(make_png(37, 11)), (37, 11))
        with self.assertRaises(MediaError):
            image_dimensions(b"definitely not an image")
        # 声明尺寸可以读出来，即使像素数据是坏的。
        self.assertEqual(image_dimensions(crafted_png_header(999, 5)), (999, 5))

    def test_imported_image(self) -> None:
        self.assertTrue(imported_image("image/png", b"x"))
        self.assertTrue(imported_image("IMAGE/PNG", b"x"))
        self.assertFalse(imported_image("application/octet-stream", b"ordinary file"))
        self.assertTrue(imported_image("application/octet-stream", make_png(4, 4)))
        self.assertFalse(imported_image("text/plain", b"ordinary file"))

    def test_should_compress_image_boundaries(self) -> None:
        from atri_x.media import MediaConfig

        data = make_png(80, 40)
        cfg = MediaConfig(llm_image_compression_threshold_bytes=len(data) + 1, llm_image_max_length=81)
        self.assertFalse(should_compress_image(data, cfg), "严格大于阈值才算超限")

        cfg = MediaConfig(llm_image_compression_threshold_bytes=len(data), llm_image_max_length=81)
        self.assertFalse(should_compress_image(data, cfg), "等于阈值不算超限")

        cfg = MediaConfig(llm_image_compression_threshold_bytes=len(data) - 1, llm_image_max_length=81)
        self.assertTrue(should_compress_image(data, cfg), "超过一个字节就压缩")

        cfg = MediaConfig(llm_image_compression_threshold_bytes=4096, llm_image_max_length=80)
        self.assertTrue(should_compress_image(data, cfg), "边长达到限制即压缩（>=）")

        cfg = MediaConfig(llm_image_compression_threshold_bytes=4096, llm_image_max_length=81)
        self.assertFalse(should_compress_image(data, cfg))

        cfg = MediaConfig(llm_image_compression_threshold_bytes=4096, llm_image_max_length=32)
        self.assertFalse(should_compress_image(b"not an image", cfg), "量不出尺寸就不压缩")

    def test_compressed_name(self) -> None:
        self.assertEqual(compressed_name("photo.png"), "photo.jpg")
        self.assertEqual(compressed_name("noext"), "noext.jpg")
        self.assertEqual(compressed_name("archive.tar.gz"), "archive.tar.jpg")
        self.assertEqual(
            compressed_name("https://example.com/a/b/pic.webp?x=1"), "pic.jpg"
        )
        self.assertEqual(compressed_name(""), "file.jpg")

    def test_vision_image_mime_type(self) -> None:
        png = make_png(4, 4)
        self.assertEqual(vision_image_mime_type(png, "image/jpeg"), "image/png")
        self.assertEqual(vision_image_mime_type(png, "image/png"), "image/png")
        self.assertEqual(
            vision_image_mime_type(b"plain text", "application/octet-stream"),
            "application/octet-stream",
        )
        self.assertEqual(vision_image_mime_type(png, ""), "image/png")
        self.assertEqual(vision_image_mime_type(png, "image/png; charset=binary"), "image/png")

    def test_verify_vision_image(self) -> None:
        output = compress_image(make_png(120, 60), 64 * 1024, 32)
        verify_vision_image(output, 64, 64 * 1024)
        with self.assertRaises(MediaError):
            verify_vision_image(output, 8, 64 * 1024)
        with self.assertRaises(MediaError):
            verify_vision_image(output, 64, 1)
        with self.assertRaises(MediaError):
            verify_vision_image(b"not an image", 64, 64 * 1024)


class TestWorkerProtocol(unittest.TestCase):
    """``worker.go`` 的 JSON 信封与"请求 → 响应"转换。"""

    def test_request_round_trip(self) -> None:
        payload = encode_worker_request(b"bytes\x00here", 1024, 32)
        request = decode_worker_request(payload)
        self.assertEqual(request.data, base64.b64encode(b"bytes\x00here").decode("ascii"))
        self.assertEqual(request.max_bytes, 1024)
        self.assertEqual(request.max_length, 32)
        self.assertEqual(ImageWorkerRequest.from_json(request.to_json()), request)
        with self.assertRaises(MediaError):
            decode_worker_request(b"{not json")

    def test_executes_compression(self) -> None:
        request = ImageWorkerRequest(
            data=base64.b64encode(make_png(200, 100)).decode("ascii"),
            max_bytes=64 * 1024,
            max_length=32,
        )
        response = execute_worker_request(request)
        self.assertEqual(response.error, "")
        output = base64.b64decode(response.data)
        width, height = image_dimensions(output)
        self.assertLess(width, 32)
        self.assertLess(height, 32)

    def test_reports_error_in_band(self) -> None:
        request = ImageWorkerRequest(data="bm90IGFuIGltYWdl", max_bytes=1, max_length=1)
        response = execute_worker_request(request)
        self.assertTrue(response.error, "失败写进响应体而不是崩掉进程")
        self.assertEqual(response.data, "")
        self.assertIn("too small", response.error)

    def test_rejects_bad_base64(self) -> None:
        with self.assertRaises(MediaError):
            execute_worker_request(ImageWorkerRequest(data="%%%", max_bytes=1024, max_length=32))

    def test_response_round_trip_and_omits_empty(self) -> None:
        self.assertEqual(encode_worker_response(ImageWorkerResponse()), b"{}")
        payload = encode_worker_response(ImageWorkerResponse(data="AA=="))
        self.assertEqual(decode_worker_response(payload).data, "AA==")
        with self.assertRaises(MediaError):
            decode_worker_response(b"{oops")

    def test_truncate_worker_error(self) -> None:
        self.assertEqual(truncate_worker_error("short"), "short")
        truncated = truncate_worker_error("x" * 600)
        self.assertEqual(len(truncated), 515)
        self.assertTrue(truncated.endswith("..."))

    def test_run_image_worker_streams(self) -> None:
        stdin = io.BytesIO(
            encode_worker_request(make_png(80, 40), 32 * 1024, 32)
        )
        stdout = io.BytesIO()
        run_image_worker(stdin, stdout)
        response = decode_worker_response(stdout.getvalue())
        self.assertEqual(response.error, "")
        self.assertEqual(image_dimensions(base64.b64decode(response.data))[0], 31)

    def test_main_entrypoint(self) -> None:
        self.assertEqual(IMAGE_WORKER_ARG, "__atrix_media_worker")
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            self.assertEqual(main(["something-else"]), 2)
        self.assertIn("unknown media worker argument", captured.getvalue())


class TestIsolatedCompression(AsyncMediaTestCase):
    """``worker.py`` 的 Python 侧等价职责（线程 + 闸门 + 超时）。"""

    async def test_compresses_in_worker_thread(self) -> None:
        output = await self.guard(compress_image_isolated(None, make_png(200, 100), 64 * 1024, 32))
        width, height = image_dimensions(output)
        self.assertEqual((width, height), (31, 15))
        self.assertEqual(IMAGE_WORKER_TIMEOUT, 60.0)

    async def test_timeout_is_reported(self) -> None:
        import threading

        release = threading.Event()
        self.addCleanup(release.set)

        def slow() -> bytes:
            release.wait(timeout=10.0)
            return b"late"

        with self.assertRaises(MediaError) as ctx:
            await run_isolated(None, slow, timeout=0.05)
        self.assertIn("timed out", str(ctx.exception))

    async def test_cancelled_context_short_circuits(self) -> None:
        token = CancelToken.with_cancel()
        token.cancel("test")
        with self.assertRaises(Exception):
            await compress_image_isolated(token, make_png(8, 8), 1024, 32)

    async def test_concurrent_isolated_calls_are_gated(self) -> None:
        payloads = [make_png(120, 60) for _ in range(4)]
        results = await asyncio.wait_for(
            asyncio.gather(
                *(compress_image_isolated(None, data, 64 * 1024, 32) for data in payloads)
            ),
            30.0,
        )
        for output in results:
            self.assertEqual(image_dimensions(output), (31, 15))

    async def test_sniff_and_decode_errors_are_stable(self) -> None:
        self.assertEqual(sniff(make_png(2, 2)).format, "png")
        with self.assertRaises(ImageDecodeError):
            sniff(b"")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
