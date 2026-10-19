"""atri_x.media.imagecodec 的单元测试（标准库 unittest）。

覆盖：手工构造的最小 PNG（Adam7 隔行、位深 1/2/4/16、PLTE+tRNS、gAMA）与
baseline JPEG（4:2:0 + DRI 重启），自编解码往返（含 PSNR 断言），sniff 判定，
compress 两条分支，解压炸弹/截断/渐进 JPEG 的受控异常，以及 Pillow 可用时的
跨后端一致性对比。

模块级把后端钉成 ``pure``（纯 Python 路径是本包的必测主体）；需要对比
Pillow 的用例用 :func:`use_backend` 临时切换。
"""

from __future__ import annotations

import contextlib
import io
import math
import struct
import tracemalloc
import unittest
import zlib

from atri_x.media.imagecodec import (
    Image,
    ImageCodecError,
    ImageDecodeError,
    ImageInfo,
    ImageTooLargeError,
    UnsupportedImageError,
    backend,
    compress,
    decode,
    encode_jpeg,
    encode_png,
    resize,
    resize_to_fit,
    set_backend,
    sniff,
    to_rgb,
)
from atri_x.media.imagecodec import jpeg_decode, png as png_module

try:  # 可选后端：缺失时相关用例 skip
    from PIL import Image as PILImage

    HAS_PIL = True
except Exception:  # pragma: no cover - 取决于环境
    PILImage = None  # type: ignore[assignment]
    HAS_PIL = False


def setUpModule() -> None:
    set_backend("pure")


def tearDownModule() -> None:
    set_backend("auto")


@contextlib.contextmanager
def use_backend(name: str):
    previous = backend()
    set_backend(name)
    try:
        yield backend()
    finally:
        set_backend(previous)


PNG_SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def psnr(a: bytes, b: bytes) -> float:
    total = sum((a[i] - b[i]) ** 2 for i in range(len(a)))
    mse = total / len(a)
    return float("inf") if mse == 0 else 10 * math.log10(255 * 255 / mse)


def mean_abs(a: bytes, b: bytes) -> float:
    count = min(len(a), len(b))
    return sum(abs(a[i] - b[i]) for i in range(count)) / count


def rgb_image(width: int, height: int, noise: int = 0) -> Image:
    img = Image(width, height, "RGB")
    for y in range(height):
        for x in range(width):
            img.put_pixel(
                x,
                y,
                (
                    (x * 3 + y + noise * ((x * 7 + y * 13) % 5)) % 256,
                    (y * 3 + x // 2 + noise * ((x * 11 + y) % 7)) % 256,
                    (x + y) % 256,
                ),
            )
    return img


def noisy_image(width: int, height: int, seed: int = 20260113) -> Image:
    """确定性伪随机图（PNG 压不动，用于字节预算用例）。"""
    img = Image(width, height, "RGB")
    state = seed
    data = img.data
    for i in range(len(data)):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        data[i] = (state >> 16) & 0xFF
    return img


def png_chunk(kind: bytes, payload: bytes, crc: int | None = None) -> bytes:
    if crc is None:
        crc = zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def pack_samples(samples: list[int], depth: int) -> bytes:
    if depth == 8:
        return bytes(samples)
    if depth == 16:
        return b"".join(struct.pack(">H", value) for value in samples)
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    out = bytearray()
    for start in range(0, len(samples), per_byte):
        byte = 0
        for offset in range(per_byte):
            index = start + offset
            byte = (byte << depth) | (samples[index] & mask if index < len(samples) else 0)
        out.append(byte)
    return bytes(out)


def build_png(
    width: int,
    height: int,
    color_type: int,
    depth: int,
    rows: list[list[int]],
    *,
    palette: bytes | None = None,
    trns: bytes | None = None,
    interlace: int = 0,
    gamma: int | None = None,
) -> bytes:
    """独立于被测实现的最小 PNG 构造器（rows 为样本值，通道已拼接）。"""
    channels = CHANNELS[color_type]
    raw = bytearray()
    if interlace == 0:
        for row in rows:
            raw.append(0)
            raw += pack_samples(row, depth)
    else:
        for x0, y0, dx, dy in ADAM7:
            pw = (width - x0 + dx - 1) // dx if width > x0 else 0
            ph = (height - y0 + dy - 1) // dy if height > y0 else 0
            if pw <= 0 or ph <= 0:
                continue
            for py in range(ph):
                source = rows[y0 + py * dy]
                samples: list[int] = []
                for px in range(pw):
                    start = (x0 + px * dx) * channels
                    samples += source[start : start + channels]
                raw.append(0)
                raw += pack_samples(samples, depth)
    out = bytearray(PNG_SIG)
    out += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, interlace))
    if gamma is not None:
        out += png_chunk(b"gAMA", struct.pack(">I", gamma))
    if palette is not None:
        out += png_chunk(b"PLTE", palette)
    if trns is not None:
        out += png_chunk(b"tRNS", trns)
    out += png_chunk(b"IDAT", zlib.compress(bytes(raw)))
    out += png_chunk(b"IEND", b"")
    return bytes(out)


class BitWriter:
    """最小 JPEG 熵编码位写入器（带 0xFF 填充）。"""

    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.count = 0

    def put(self, value: int, length: int) -> None:
        if length <= 0:
            return
        self.acc = (self.acc << length) | (value & ((1 << length) - 1))
        self.count += length
        while self.count >= 8:
            self.count -= 8
            byte = (self.acc >> self.count) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0x00)
        self.acc &= (1 << self.count) - 1

    def align(self) -> None:
        if self.count:
            self.put((1 << (8 - self.count)) - 1, 8 - self.count)

    def data(self) -> bytes:
        return bytes(self.out)


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg_flat_block(writer: BitWriter, dc: int, previous: int) -> int:
    """写一个「DC 变化 + 立即 EOB」的块。

    夹具表：DC 类别 c → 4 位码 c（0..14），AC 符号 0x00(EOB) → 码 "0"。
    """
    diff = dc - previous
    size = abs(diff).bit_length()
    writer.put(size, 4)
    if size:
        writer.put(diff if diff > 0 else diff + (1 << size) - 1, size)
    writer.put(0, 1)
    return dc


def build_restart_jpeg() -> bytes:
    """手工构造 32x16、4:2:0、DRI=1（MCU 间有 RST0）的 baseline JPEG。

    量化表全为 16，因此样本 = DC * 2 + 128：Y=136、Cb=200、Cr=100 恒定。
    """
    writer = BitWriter()
    y_pred = cb_pred = cr_pred = 0
    for _ in range(4):  # MCU 0
        y_pred = jpeg_flat_block(writer, 4, y_pred)
    cb_pred = jpeg_flat_block(writer, 36, cb_pred)
    cr_pred = jpeg_flat_block(writer, -14, cr_pred)
    writer.align()
    writer.out += b"\xff\xd0"  # RST0
    # 重启后 DC 预测器归零：MCU 1 的每个分量都按「预测值 = 0」重新编码
    y_pred = cb_pred = cr_pred = 0
    for _ in range(4):
        y_pred = jpeg_flat_block(writer, 4, y_pred)
    jpeg_flat_block(writer, 36, cb_pred)
    jpeg_flat_block(writer, -14, cr_pred)
    writer.align()

    # DC 表：15 个长度 4 的码（0..14），刻意避开「全 1 码」——libjpeg 禁止使用
    # 全 1 的 Huffman 码，夹具必须对第三方解码器同样合法。
    dc_bits = bytes([0, 0, 0, 15] + [0] * 12)
    ac_bits = bytes([1] + [0] * 15)
    out = bytearray(b"\xff\xd8")
    out += jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    out += jpeg_segment(0xDB, bytes([0x00]) + bytes([16] * 64 + [0x01]) + bytes([16] * 64))
    out += jpeg_segment(0xC0, struct.pack(">BHHB", 8, 16, 32, 3) + bytes([1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1]))
    dht = bytearray()
    for tid in (0x00, 0x01):
        dht += bytes([tid]) + dc_bits + bytes(range(15))
    for tid in (0x10, 0x11):
        dht += bytes([tid]) + ac_bits + bytes([0x00])
    out += jpeg_segment(0xC4, bytes(dht))
    out += jpeg_segment(0xDD, struct.pack(">H", 1))
    out += jpeg_segment(0xDA, bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0]))
    out += writer.data()
    out += b"\xff\xd9"
    return bytes(out)


# 期望颜色：YCbCr(136, 200, 100) → 舍入后 (97, 131, 255)，与 libjpeg 一致
EXPECTED_RGB = (97, 131, 255)


class TestSniff(unittest.TestCase):
    def test_png(self) -> None:
        data = build_png(3, 2, 2, 8, [[1, 2, 3] * 3, [4, 5, 6] * 3])
        self.assertEqual(sniff(data), ImageInfo("png", 3, 2, "RGB", "image/png", False, False))

    def test_png_palette_transparency_and_apng(self) -> None:
        data = build_png(2, 2, 3, 4, [[0, 1], [2, 3]], palette=bytes([1, 2, 3] * 4), trns=bytes([0, 128]))
        data = data[:33] + png_chunk(b"acTL", bytes(8)) + data[33:]
        info = sniff(data)
        self.assertEqual((info.format, info.mode, info.has_alpha, info.animated), ("png", "P", True, True))

    def test_jpeg(self) -> None:
        info = sniff(build_restart_jpeg())
        self.assertEqual((info.format, info.width, info.height, info.mode), ("jpeg", 32, 16, "RGB"))
        self.assertEqual(info.mime_type, "image/jpeg")

    def test_gif_webp_bmp_unknown(self) -> None:
        gif = b"GIF89a" + struct.pack("<HH", 7, 9) + bytes([0x00, 0, 0]) + b"\x3b"
        self.assertEqual(sniff(gif), ImageInfo("gif", 7, 9, "P", "image/gif", False, False))
        vp8x = b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
        vp8x += bytes([0x12, 0, 0, 0]) + (5).to_bytes(3, "little") + (7).to_bytes(3, "little")
        info = sniff(vp8x)
        self.assertEqual((info.format, info.width, info.height, info.has_alpha, info.animated), ("webp", 6, 8, True, True))
        bmp = b"BM" + struct.pack("<IHHI", 54, 0, 0, 54) + struct.pack("<IiiHH", 40, 11, -13, 1, 24) + bytes(24)
        self.assertEqual(sniff(bmp), ImageInfo("bmp", 11, 13, "RGB", "image/bmp", False, False))
        unknown = sniff(b"II*\x00" + bytes(12))
        self.assertEqual((unknown.format, unknown.width, unknown.has_alpha), ("unknown", 0, False))

    def test_truncated_headers_raise(self) -> None:
        truncated_webp = b"RIFF" + struct.pack("<I", 100) + b"WEBP" + bytes(4)
        for data in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a", truncated_webp):
            with self.subTest(data=data):
                with self.assertRaises(ImageDecodeError):
                    sniff(data)
        self.assertEqual(sniff(b"RIFF" + bytes(20)).format, "unknown")


class TestImageContainer(unittest.TestCase):
    def test_pixel_access_and_bytes(self) -> None:
        img = Image(2, 2, "RGBA")
        img.put_pixel(1, 1, (10, 20, 30, 40))
        self.assertEqual(img.get_pixel(1, 1), (10, 20, 30, 40))
        self.assertTrue(img.has_alpha)
        self.assertEqual(img.to_bytes(), bytes(12) + bytes([10, 20, 30, 40]))
        self.assertEqual(Image.from_bytes(img.to_bytes(), 2, 2, "RGBA"), img)
        with self.assertRaises(ImageCodecError):
            img.get_pixel(2, 0)
        with self.assertRaises(ImageCodecError):
            img.put_pixel(0, 0, (1, 2))
        with self.assertRaises(ImageCodecError):
            Image.from_bytes(b"\x00", 2, 2, "RGB")

    def test_convert(self) -> None:
        source = Image(2, 1, "RGB")
        source.put_pixel(0, 0, (255, 0, 0))
        source.put_pixel(1, 0, (0, 0, 0))
        self.assertEqual(source.convert("L").get_pixel(0, 0), (76,))
        self.assertEqual(source.convert("RGBA").get_pixel(1, 0), (0, 0, 0, 255))
        self.assertEqual(source.convert("RGBA").convert("RGB"), source)
        self.assertEqual(source.convert("LA").data, bytearray([76, 255, 0, 255]))
        palette = Image(2, 1, "P", bytearray([0, 1]), palette=bytes([9, 8, 7, 6, 5, 4]), transparency=bytes([255, 0]))
        self.assertEqual(palette.convert("RGB").get_pixel(1, 0), (6, 5, 4))
        self.assertEqual(palette.convert("RGBA").get_pixel(1, 0), (6, 5, 4, 0))
        self.assertTrue(palette.has_alpha)
        with self.assertRaises(UnsupportedImageError):
            source.convert("P")
        with self.assertRaises(UnsupportedImageError):
            source.convert("CMYK")


class TestPngDecode(unittest.TestCase):
    def test_truecolor_rows(self) -> None:
        rows = [[(x * 8 + y) % 256 for x in range(6) for _ in range(3)] for y in range(4)]
        img = decode(build_png(6, 4, 2, 8, rows))
        self.assertEqual((img.mode, img.size), ("RGB", (6, 4)))
        for y in range(4):
            for x in range(6):
                self.assertEqual(img.get_pixel(x, y), ((x * 8 + y) % 256,) * 3)

    def test_gray_bit_depths(self) -> None:
        self.assertEqual(decode(build_png(4, 2, 0, 1, [[0, 1, 1, 0], [1, 1, 0, 0]])).data, bytearray([0, 255, 255, 0, 255, 255, 0, 0]))
        self.assertEqual(decode(build_png(2, 2, 0, 2, [[0, 3], [1, 2]])).data, bytearray([0, 255, 85, 170]))
        self.assertEqual(decode(build_png(2, 2, 0, 4, [[7, 15], [0, 1]])).data, bytearray([119, 255, 0, 17]))

    def test_sixteen_bit_takes_high_byte(self) -> None:
        img = decode(build_png(2, 2, 0, 16, [[0x1234, 0xFFFF], [0x0080, 0x00FF]]))
        self.assertEqual(img.mode, "L")
        self.assertEqual(img.data, bytearray([0x12, 0xFF, 0x00, 0x00]))
        self.assertEqual(decode(build_png(1, 1, 2, 16, [[0x1122, 0x3344, 0x5566]])).get_pixel(0, 0), (0x11, 0x33, 0x55))

    def test_palette_with_transparency(self) -> None:
        palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        img = decode(build_png(3, 1, 3, 8, [[0, 1, 2]], palette=palette, trns=bytes([0, 128])))
        self.assertEqual((img.mode, img.data, img.has_alpha), ("P", bytearray([0, 1, 2]), True))
        rgba = img.convert("RGBA")
        self.assertEqual(rgba.get_pixel(0, 0), (255, 0, 0, 0))
        self.assertEqual(rgba.get_pixel(1, 0), (0, 255, 0, 128))
        self.assertEqual(rgba.get_pixel(2, 0), (0, 0, 255, 255))

    def test_adam7_interlaced(self) -> None:
        width, height = 13, 9
        rows = [[(x * 16 + y * 3) % 256 for x in range(width)] for y in range(height)]
        img = decode(build_png(width, height, 0, 8, rows, interlace=1))
        self.assertEqual(img.data, bytearray(value for row in rows for value in row))
        rows_rgb = [[(x * 11 + y * 5) % 256 for x in range(5) for _ in range(4)] for y in range(4)]
        self.assertEqual(decode(build_png(5, 4, 6, 8, rows_rgb, interlace=1)).data, bytearray(v for row in rows_rgb for v in row))

    def test_gamma_chunk_and_strict_crc(self) -> None:
        data = build_png(2, 2, 0, 8, [[1, 2], [3, 4]], gamma=45455)
        self.assertEqual(decode(data).info["gamma"], 0.45455)
        broken = bytearray(data)
        broken[41] ^= 0xFF  # 落在 gAMA 载荷里
        png_module.decode_png(bytes(broken))
        with self.assertRaises(ImageDecodeError):
            png_module.decode_png(bytes(broken), strict_crc=True)

    def test_truncated_and_bad_signature(self) -> None:
        data = build_png(4, 4, 0, 8, [[1] * 4 for _ in range(4)])
        with self.assertRaises(ImageDecodeError):
            decode(data[: len(data) // 2])
        with self.assertRaises(ImageDecodeError):
            decode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        with self.assertRaises(UnsupportedImageError):
            decode(b"not an image at all")

    def test_decompression_bomb_and_size_limits(self) -> None:
        bomb = bytearray(build_png(1, 1, 0, 8, [[0]]))
        struct.pack_into(">II", bomb, 16, 20000, 20000)  # 头部声明 20000x20000
        with self.assertRaises(ImageTooLargeError):
            decode(bytes(bomb))
        lying = bytearray(PNG_SIG)
        lying += png_chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 0, 0, 0, 0))
        lying += png_chunk(b"IDAT", zlib.compress(bytes(4096)))  # 解压后远超声明
        lying += png_chunk(b"IEND", b"")
        with self.assertRaises(ImageDecodeError):
            decode(bytes(lying))
        small = build_png(20, 20, 0, 8, [[0] * 20 for _ in range(20)])
        with self.assertRaises(ImageTooLargeError):
            decode(small, max_pixels=100)


class TestPngEncode(unittest.TestCase):
    def test_roundtrip_all_modes(self) -> None:
        cases = {
            "RGB": rgb_image(17, 9),
            "RGBA": rgb_image(17, 9).convert("RGBA"),
            "L": rgb_image(17, 9).convert("L"),
            "LA": rgb_image(17, 9).convert("LA"),
        }
        for name, img in cases.items():
            with self.subTest(mode=name):
                data = encode_png(img)
                info = sniff(data)
                self.assertEqual((info.format, info.width, info.height), ("png", 17, 9))
                self.assertEqual(decode(data), img)

    def test_palette_roundtrip(self) -> None:
        img = Image(4, 2, "P", bytearray([0, 1, 2, 3, 3, 2, 1, 0]))
        img.palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9])
        img.transparency = bytes([0, 64, 128, 255])
        back = decode(encode_png(img))
        self.assertEqual((back.mode, back.data), ("P", img.data))
        self.assertEqual(back.palette[:12], img.palette)
        self.assertEqual(back.transparency, img.transparency)

    def test_encoder_output_has_valid_crc(self) -> None:
        data = encode_png(rgb_image(8, 8))
        pos = 8
        while pos < len(data):
            (length,) = struct.unpack_from(">I", data, pos)
            kind = data[pos + 4 : pos + 8]
            payload = data[pos + 8 : pos + 8 + length]
            (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
            self.assertEqual(crc, zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF)
            pos += 12 + length
        self.assertEqual(data[-8:-4], b"IEND")


class TestJpegFixtures(unittest.TestCase):
    def test_handmade_420_with_restart(self) -> None:
        data = build_restart_jpeg()
        info = sniff(data)
        self.assertEqual((info.format, info.width, info.height), ("jpeg", 32, 16))
        img = decode(data)
        self.assertEqual((img.mode, img.size), ("RGB", (32, 16)))
        for x, y in ((0, 0), (15, 15), (16, 0), (31, 15)):
            pixel = img.get_pixel(x, y)
            for channel in range(3):
                self.assertLess(abs(pixel[channel] - EXPECTED_RGB[channel]), 4, pixel)
        # 重启后 DC 预测器必须归零：左右两个 MCU 渲染结果逐行一致
        for y in range(16):
            base = y * 32 * 3
            self.assertEqual(img.data[base : base + 48], img.data[base + 48 : base + 96])

    def test_truncated_jpeg(self) -> None:
        data = build_restart_jpeg()
        with self.assertRaises(ImageDecodeError):
            decode(data[: len(data) // 2])
        with self.assertRaises(ImageDecodeError):
            jpeg_decode.decode_jpeg(data[:20])

    def test_jpeg_size_bomb_is_rejected(self) -> None:
        data = bytearray(build_restart_jpeg())
        index = data.index(b"\xff\xc0")
        struct.pack_into(">HH", data, index + 5, 40000, 40000)  # SOF0 高度/宽度
        with self.assertRaises(ImageTooLargeError):
            decode(bytes(data))
        with self.assertRaises(ImageTooLargeError):
            jpeg_decode.decode_jpeg(bytes(data))
        small = bytearray(build_restart_jpeg())
        struct.pack_into(">HH", small, index + 5, 4, 4)
        with self.assertRaises(ImageTooLargeError):
            jpeg_decode.decode_jpeg(bytes(small), max_pixels=8)

    def test_progressive_is_rejected_but_sniffed(self) -> None:
        data = bytearray(build_restart_jpeg())
        data[data.index(b"\xff\xc0") + 1] = 0xC2  # SOF0 → SOF2
        self.assertEqual(sniff(bytes(data)).format, "jpeg")
        with self.assertRaises(UnsupportedImageError) as caught:
            decode(bytes(data))
        self.assertIn("SOF2", str(caught.exception))

    def test_unsupported_marker_variants(self) -> None:
        for code, name in ((0xC3, "SOF3"), (0xC9, "SOF9"), (0xCA, "SOF10")):
            data = bytearray(build_restart_jpeg())
            data[data.index(b"\xff\xc0") + 1] = code
            with self.subTest(marker=name):
                with self.assertRaises(UnsupportedImageError):
                    decode(bytes(data))

    def test_non_interleaved_scan_is_rejected(self) -> None:
        # 把 SOS 的分量数从 3 改成 1：非交错/多扫描文件必须明确拒绝而不是给出残缺图
        data = bytearray(build_restart_jpeg())
        index = data.index(b"\xff\xda")
        data[index + 4] = 1
        with self.assertRaises(UnsupportedImageError) as caught:
            decode(bytes(data))
        self.assertIn("非交错", str(caught.exception))


class TestDecodeHardening(unittest.TestCase):
    """研究报告 §3.3 的 P0/P1 修复回归。

    覆盖：JPEG 分配前熵下界、多 SOF 歧义拒绝、SOS 分量顺序校验、
    PNG 流式解码的峰值内存、关键/辅助块 CRC 策略、``max_pixels<=0`` 的受控拒绝。
    """

    def test_jpeg_entropy_lower_bound_blocks_allocation(self) -> None:
        data = bytearray(build_restart_jpeg())
        index = data.index(b"\xff\xc0")
        struct.pack_into(">HH", data, index + 5, 4000, 4000)  # 16 MPix，低于默认上限
        tracemalloc.start()
        with self.assertRaises(ImageDecodeError) as caught:
            jpeg_decode.decode_jpeg(bytes(data))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertIn("熵编码数据截断", str(caught.exception))
        # 旧实现会先按 4000x4000 分配三个分量平面（~48 MiB）再报截断。
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_jpeg_multiple_sof_is_rejected(self) -> None:
        data = build_restart_jpeg()
        index = data.index(b"\xff\xc0")
        seg_len = int.from_bytes(data[index + 2 : index + 4], "big")
        payload = bytearray(data[index + 4 : index + 2 + seg_len])
        struct.pack_into(">HH", payload, 1, 9000, 9000)
        fake = b"\xff\xc0" + struct.pack(">H", len(payload) + 2) + bytes(payload)
        crafted = data[: index + 2 + seg_len] + fake + data[index + 2 + seg_len :]
        # 尺寸闸门（sniff）与解码器必须同时拒绝，否则"嗅探 8x8、解码 9000x9000"可绕闸门。
        with self.assertRaises(ImageDecodeError):
            sniff(crafted)
        with self.assertRaises(ImageDecodeError):
            decode(crafted)
        with self.assertRaises(ImageDecodeError):
            jpeg_decode.decode_jpeg(crafted)

    def test_jpeg_sos_component_order_mismatch_is_rejected(self) -> None:
        data = bytearray(build_restart_jpeg())
        index = data.index(b"\xff\xda")
        # 交换 SOS 里两个分量选择子（(1,2,3) → (3,2,1)），载荷结构本身仍然合法。
        data[index + 5], data[index + 9] = data[index + 9], data[index + 5]
        with self.assertRaises(ImageDecodeError) as caught:
            jpeg_decode.decode_jpeg(bytes(data))
        self.assertIn("分量顺序", str(caught.exception))

    def test_png_streaming_peak_is_output_sized(self) -> None:
        width = height = 512
        row = b"\x00" + b"\x12\x34" * (4 * width)  # filter 0 + 16-bit RGBA
        blob = (
            PNG_SIG
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 6, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(row * height, 6))
            + png_chunk(b"IEND", b"")
        )
        tracemalloc.start()
        image = decode(blob)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual((image.mode, image.size), ("RGBA", (width, height)))
        output_bytes = width * height * 4
        # 旧实现：解压原始缓冲(2×) + 还原缓冲(2×) + 输出(1×) ≈ 5 倍输出；流式后 ≈ 1 倍。
        self.assertLess(peak, output_bytes * 2)

    def test_png_critical_crc_is_always_enforced(self) -> None:
        data = bytearray(
            build_png(2, 2, 3, 4, [[0, 1], [2, 3]], palette=bytes([1, 2, 3] * 4))
        )
        index = data.index(b"PLTE")
        data[index + 4] ^= 0xFF  # 破坏 PLTE 载荷（关键块）
        with self.assertRaises(ImageDecodeError):
            decode(bytes(data))
        with self.assertRaises(ImageDecodeError):
            png_module.decode_png(bytes(data))

    def test_png_ancillary_crc_recorded_and_strict_mode_raises(self) -> None:
        data = bytearray(
            build_png(
                2,
                2,
                3,
                4,
                [[0, 1], [2, 3]],
                palette=bytes([1, 2, 3] * 4),
                trns=bytes([0, 128]),
            )
        )
        index = data.index(b"tRNS")
        data[index + 4] ^= 0xFF  # 破坏 tRNS 载荷（辅助块）
        image = decode(bytes(data))
        self.assertEqual(image.info["crc_mismatches"], ["tRNS"])
        with self.assertRaises(ImageDecodeError):
            decode(bytes(data), strict_crc=True)

    def test_zero_max_pixels_is_rejected(self) -> None:
        png_data = build_png(2, 2, 0, 8, [[0, 1], [2, 3]])
        with self.assertRaises(ValueError):
            decode(png_data, max_pixels=0)
        with self.assertRaises(ValueError):
            png_module.decode_png(png_data, max_pixels=0)
        with self.assertRaises(ValueError):
            jpeg_decode.decode_jpeg(build_restart_jpeg(), max_pixels=-1)

    def test_invalid_arguments_raise_controlled_errors(self) -> None:
        image = rgb_image(2, 2)
        with self.assertRaises(ImageCodecError):
            resize(image, 1, 1, filter=None)  # type: ignore[arg-type]
        with self.assertRaises(ImageCodecError):
            to_rgb(image, None)  # type: ignore[arg-type]


class TestJpegRoundtrip(unittest.TestCase):
    def test_roundtrip_psnr(self) -> None:
        source = rgb_image(48, 32, noise=3)
        data = encode_jpeg(source, 90)
        info = sniff(data)
        self.assertEqual((info.format, info.width, info.height), ("jpeg", 48, 32))
        decoded = decode(data)
        self.assertEqual(decoded.size, source.size)
        value = psnr(source.data, decoded.data)
        self.assertGreaterEqual(value, 30.0, f"PSNR={value:.2f} dB")

    def test_quality_affects_size(self) -> None:
        source = rgb_image(64, 64, noise=5)
        low = encode_jpeg(source, 40)
        high = encode_jpeg(source, 95)
        self.assertLess(len(low), len(high))
        self.assertGreater(psnr(source.data, decode(high).data), psnr(source.data, decode(low).data))

    def test_grayscale_roundtrip(self) -> None:
        source = rgb_image(32, 24).convert("L")
        data = encode_jpeg(source, 92)
        self.assertEqual(sniff(data).mode, "L")
        decoded = decode(data)
        self.assertEqual(decoded.mode, "L")
        self.assertGreaterEqual(psnr(source.data, decoded.data), 30.0)

    def test_alpha_is_composited_on_white(self) -> None:
        img = Image(8, 8, "RGBA", bytearray(bytes([0, 0, 0, 0]) * 64))
        pixel = decode(encode_jpeg(img, 95)).get_pixel(4, 4)
        self.assertTrue(all(value >= 245 for value in pixel), pixel)
        with self.assertRaises(ImageCodecError):
            encode_jpeg(img, 0)


@unittest.skipUnless(HAS_PIL, "Pillow 不可用，跳过外来 JPEG 样本")
class TestJpegForeign(unittest.TestCase):
    def _pil_jpeg(self, pil_image, **options) -> bytes:
        buffer = io.BytesIO()
        pil_image.save(buffer, format="JPEG", **options)
        return buffer.getvalue()

    def test_handmade_fixture_agrees_with_libjpeg(self) -> None:
        data = build_restart_jpeg()
        theirs = PILImage.open(io.BytesIO(data)).convert("RGB")
        self.assertEqual(theirs.getpixel((0, 0)), EXPECTED_RGB)
        self.assertEqual(theirs.getpixel((20, 8)), EXPECTED_RGB)
        self.assertLessEqual(mean_abs(decode(data).data, theirs.tobytes()), 1.0)

    def test_pillow_encoded_subsampling_variants(self) -> None:
        source = PILImage.new("RGB", (40, 24))
        for x in range(40):
            for y in range(24):
                source.putpixel((x, y), (x * 6 % 256, y * 10 % 256, (x + y) * 4 % 256))
        for subsampling in (0, 1, 2):
            data = self._pil_jpeg(source, quality=90, subsampling=subsampling)
            with self.subTest(subsampling=subsampling):
                mine = decode(data)
                theirs = PILImage.open(io.BytesIO(data)).convert("RGB")
                self.assertEqual(mine.size, (40, 24))
                self.assertLessEqual(mean_abs(mine.data, theirs.tobytes()), 2.0)

    def test_pillow_grayscale_and_cmyk(self) -> None:
        decoded = decode(self._pil_jpeg(PILImage.new("L", (16, 16), 128), quality=90))
        self.assertEqual((decoded.mode, decoded.size), ("L", (16, 16)))
        self.assertLess(abs(decoded.get_pixel(8, 8)[0] - 128), 4)
        cmyk = PILImage.new("CMYK", (24, 16), (0, 0, 0, 0))
        for x in range(24):
            cmyk.putpixel((x, 8), (0, 200, 200, 0))
        data = self._pil_jpeg(cmyk, quality=95)
        self.assertEqual(sniff(data).mode, "CMYK")
        decoded = decode(data)
        theirs = PILImage.open(io.BytesIO(data)).convert("RGB")
        self.assertEqual(decoded.mode, "RGB")
        self.assertLessEqual(mean_abs(decoded.data, theirs.tobytes()), 2.0)

    def test_grayscale_with_sampling_factors_decodes(self) -> None:
        """单分量扫描按 T.81 A.2.2 是"每 MCU 一个数据单元"，采样因子不参与网格。

        旧实现按 hmax/vmax 划 MCU，把 Pillow 用非 1x1 采样写的灰度图多读 h*v 个块，
        合法文件被误报"熵数据截断"（1x1 / 1x33 / 3x5 均复现）。
        """
        for subsampling in (0, 1, 2):
            for size in ((1, 1), (1, 33), (3, 5), (17, 17)):
                source = PILImage.new("L", size, 123)
                data = self._pil_jpeg(source, quality=85, subsampling=subsampling)
                with self.subTest(subsampling=subsampling, size=size):
                    decoded = decode(data)
                    self.assertEqual((decoded.mode, decoded.size), ("L", size))
                    self.assertLess(abs(decoded.get_pixel(0, 0)[0] - 123), 6)

    def test_pillow_png_variants(self) -> None:
        source = PILImage.new("RGB", (21, 13))
        for x in range(21):
            for y in range(13):
                source.putpixel((x, y), (x * 12 % 256, y * 7 % 256, 33))
        for mode in ("RGB", "RGBA", "L", "LA", "P", "1"):
            buffer = io.BytesIO()
            converted = source.convert(mode)
            converted.save(buffer, format="PNG")
            with self.subTest(mode=mode):
                mine = decode(buffer.getvalue())
                self.assertEqual(mine.size, (21, 13))
                if mode == "P":
                    self.assertEqual(mine.convert("RGB").data, converted.convert("RGB").tobytes())
                else:
                    target = "L" if mode == "1" else mode
                    self.assertEqual(mine.convert(target).data, converted.convert(target).tobytes())


class TestResample(unittest.TestCase):
    def test_nearest_picks_source_pixels(self) -> None:
        img = Image(4, 4, "L")
        for y in range(4):
            for x in range(4):
                img.put_pixel(x, y, (x * 10 + y,))
        scaled = resize(img, 2, 2, filter="nearest")
        self.assertEqual(scaled.size, (2, 2))
        self.assertEqual(scaled.get_pixel(0, 0), (img.get_pixel(1, 1)[0],))
        self.assertEqual(scaled.get_pixel(1, 1), (img.get_pixel(3, 3)[0],))

    def test_no_upscale_and_bad_filter(self) -> None:
        img = rgb_image(4, 4)
        self.assertEqual(resize(img, 40, 40).size, (4, 4))
        self.assertEqual(resize(img, 8, 2).size, (4, 2))
        with self.assertRaises(ImageCodecError):
            resize(img, 0, 4, filter="bilinear")
        with self.assertRaises(ImageCodecError):
            resize(img, 4, 4, filter="mitchell")

    def test_downsample_avoids_aliasing(self) -> None:
        img = Image(64, 64, "L")
        for y in range(64):
            for x in range(64):
                img.put_pixel(x, y, (255 if x % 2 else 0,))
        nearest = resize(img, 8, 8, filter="nearest")
        bilinear = resize(img, 8, 8, filter="bilinear")
        lanczos = resize(img, 8, 8, filter="lanczos")
        self.assertIn(nearest.data[0], (0, 255))  # 最近邻保留极端值（混叠）
        for value in bilinear.data:
            self.assertTrue(80 < value < 175, value)
        for value in lanczos.data:
            self.assertTrue(100 < value < 155, value)  # 核缩放后应收到均值附近

    def test_resize_to_fit_keeps_aspect(self) -> None:
        img = rgb_image(200, 100)
        self.assertEqual(resize_to_fit(img, 64, 64).size, (64, 32))
        self.assertEqual(resize_to_fit(img, 64, 10).size, (20, 10))
        self.assertEqual(resize_to_fit(img, 400, 400).size, (200, 100))
        with self.assertRaises(ImageCodecError):
            resize_to_fit(img, 0, 10)

    def test_alpha_and_palette_modes(self) -> None:
        img = rgb_image(10, 10).convert("RGBA")
        scaled = resize(img, 5, 5)
        self.assertEqual(scaled.mode, "RGBA")
        self.assertEqual(scaled.data[3::4], bytes([255]) * 25)
        palette = Image(4, 4, "P", bytearray([0, 1, 2, 3] * 4), palette=bytes([1, 2, 3] * 3))
        self.assertEqual(resize(palette, 2, 2).mode, "RGB")  # 索引不可插值，先展开


class TestOps(unittest.TestCase):
    def test_to_rgb_composites_alpha(self) -> None:
        img = Image(3, 1, "RGBA")
        img.put_pixel(0, 0, (10, 20, 30, 255))
        img.put_pixel(1, 0, (10, 20, 30, 0))
        img.put_pixel(2, 0, (200, 100, 0, 128))
        out = to_rgb(img)
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.get_pixel(0, 0), (10, 20, 30))
        self.assertEqual(out.get_pixel(1, 0), (255, 255, 255))
        self.assertEqual(out.get_pixel(2, 0), (227, 177, 127))
        self.assertEqual(to_rgb(img, (0, 0, 0)).get_pixel(1, 0), (0, 0, 0))
        self.assertEqual(to_rgb(Image(1, 1, "LA", bytearray([100, 0]))).get_pixel(0, 0), (255, 255, 255))
        with self.assertRaises(ImageCodecError):
            to_rgb(img, (1, 2))

    def test_compress_passthrough(self) -> None:
        data = encode_png(rgb_image(16, 16))
        out, changed = compress(data, max_bytes=1 << 20, max_edge=256)
        self.assertFalse(changed)
        self.assertIs(out, data)
        self.assertFalse(compress(data)[1])

    def test_compress_by_bytes(self) -> None:
        source = noisy_image(160, 120)
        data = encode_png(source)
        budget = len(data) // 4
        self.assertGreater(budget, 512)
        out, changed = compress(data, max_bytes=budget)
        self.assertTrue(changed)
        self.assertLess(len(out), budget)
        info = sniff(out)
        self.assertEqual((info.format, info.width, info.height), ("jpeg", 160, 120))

    def test_compress_by_edge_and_white_background(self) -> None:
        img = Image(80, 40, "RGBA", bytearray(bytes([255, 0, 0, 0]) * 3200))
        img.data[0::4] = bytes([255]) * 3200  # 透明红 → 白底后应为纯白
        out, changed = compress(encode_png(img), max_bytes=1 << 20, max_edge=32)
        self.assertTrue(changed)
        info = sniff(out)
        self.assertEqual((info.format, info.width, info.height), ("jpeg", 31, 15))
        self.assertLess(max(info.width, info.height), 32)
        pixel = decode(out).get_pixel(28, 7)
        self.assertTrue(all(value >= 240 for value in pixel), pixel)

    def test_compress_rejects_undecodable_over_limit(self) -> None:
        with self.assertRaises(UnsupportedImageError):
            compress(b"this is not an image", max_bytes=4)
        out, changed = compress(b"this is not an image", max_bytes=1 << 20)
        self.assertFalse(changed)
        self.assertEqual(out, b"this is not an image")
        with self.assertRaises(ImageDecodeError):
            compress(b"")
        with self.assertRaises(ImageCodecError):
            compress(encode_png(rgb_image(4, 4)), quality=0)

    def test_compress_gives_up_on_impossible_budget(self) -> None:
        with self.assertRaises(ImageCodecError):
            compress(encode_png(noisy_image(64, 64)), max_bytes=1)


@unittest.skipUnless(HAS_PIL, "Pillow 不可用，跳过跨后端对比")
class TestBackendParity(unittest.TestCase):
    def test_sniff_and_decode_agree(self) -> None:
        source = rgb_image(23, 17, noise=2)
        png_data = encode_png(source)
        jpeg_data = encode_jpeg(source, 92)
        self.assertEqual(backend(), "pure")
        for data in (png_data, jpeg_data):
            with self.subTest(kind=sniff(data).format):
                pure_info = sniff(data)
                pure_image = decode(data)
                with use_backend("pillow") as name:
                    self.assertEqual(name, "pillow")
                    self.assertEqual(sniff(data), pure_info)
                    pillow_image = decode(data)
                self.assertEqual(pillow_image.size, pure_image.size)
                self.assertEqual(pillow_image.mode, pure_image.mode)
                if pure_info.format == "png":
                    self.assertEqual(pillow_image.data, pure_image.data)
                else:
                    self.assertLessEqual(mean_abs(pillow_image.data, pure_image.data), 3.0)

    def test_encoders_agree(self) -> None:
        source = rgb_image(32, 24, noise=2)
        with use_backend("pure"):
            pure_png, pure_jpeg = encode_png(source), encode_jpeg(source, 90)
        with use_backend("pillow"):
            pillow_png, pillow_jpeg = encode_png(source), encode_jpeg(source, 90)
        self.assertEqual(decode(pure_png).data, source.data)
        self.assertEqual(decode(pillow_png).data, source.data)
        with use_backend("pure"):
            mine, theirs = decode(pure_jpeg), decode(pillow_jpeg)
        self.assertEqual(mine.size, theirs.size)
        self.assertLessEqual(mean_abs(mine.data, theirs.data), 8.0)
        self.assertGreaterEqual(psnr(source.data, mine.data), 30.0)
        self.assertGreaterEqual(psnr(source.data, theirs.data), 30.0)

    def test_resize_and_compress_agree(self) -> None:
        source = rgb_image(40, 30, noise=1)
        with use_backend("pure"):
            pure = resize(source, 20, 15)
        with use_backend("pillow"):
            pillow = resize(source, 20, 15)
        self.assertEqual(pure.size, pillow.size)
        self.assertLessEqual(mean_abs(pure.data, pillow.data), 6.0)
        data = encode_png(noisy_image(120, 60))
        with use_backend("pure"):
            a, changed_a = compress(data, max_bytes=4096, max_edge=64)
        with use_backend("pillow"):
            b, changed_b = compress(data, max_bytes=4096, max_edge=64)
        self.assertEqual((changed_a, changed_b), (True, True))
        with use_backend("pure"):
            info_a, info_b = sniff(a), sniff(b)
            image_a, image_b = decode(a), decode(b)
        self.assertEqual((info_a.format, info_b.format), ("jpeg", "jpeg"))
        self.assertEqual((info_a.width, info_a.height), (info_b.width, info_b.height))
        self.assertLessEqual(mean_abs(image_a.data, image_b.data), 8.0)

    def test_sixteen_bit_falls_back_to_pure(self) -> None:
        data = build_png(2, 2, 0, 16, [[0x1234, 0xFFFF], [0x0080, 0x00FF]])
        with use_backend("pillow"):
            self.assertEqual(decode(data).data, bytearray([0x12, 0xFF, 0x00, 0x00]))


class TestUnsupportedFormats(unittest.TestCase):
    def test_pure_backend_cannot_decode_gif_webp_bmp(self) -> None:
        gif = b"GIF89a" + struct.pack("<HH", 2, 2) + bytes([0x00, 0, 0]) + b"\x3b"
        with self.assertRaises(UnsupportedImageError) as caught:
            decode(gif)
        self.assertIn("gif", str(caught.exception))
        self.assertEqual(sniff(gif).format, "gif")
        bmp = b"BM" + struct.pack("<IHHI", 54, 0, 0, 54) + struct.pack("<IiiHH", 40, 2, 2, 1, 24) + bytes(24)
        with self.assertRaises(UnsupportedImageError):
            decode(bmp)

    def test_decode_type_and_empty_errors(self) -> None:
        with self.assertRaises(TypeError):
            decode("not bytes")  # type: ignore[arg-type]
        with self.assertRaises(ImageDecodeError):
            decode(b"")

    def test_set_backend_validation(self) -> None:
        with self.assertRaises(ValueError):
            set_backend("cairo")
        self.assertEqual(backend(), "pure")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
