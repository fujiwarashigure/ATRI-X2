"""JPEG 纯 Python 解码器 vs Pillow/libjpeg 的精度固定样本用例。

``atri_x/media/imagecodec/jpeg_decode.py`` 头部的精度口径（4:4:4 / 4:2:2 / 4:2:0
的平均差与最大差上限）由本用例钉住：样本是**确定性生成**的（不用随机数、不写临时
文件），参考解码固定为 Pillow/libjpeg，本仓解码器逐像素比对。

覆盖要求（对应评审 §八）：

* 三种采样：Pillow 编码器 ``subsampling=0/1/2``（4:4:4 / 4:2:2 / 4:2:0）；
* 奇数宽高与非 MCU 对齐：33x17、41x39、37x23、63x65；
* 单行 / 单列：64x1、1x64；极端长宽比：200x3、1x33、3x5；
* 高对比色度边缘：饱和色竖条（chroma_edges）；
* 两条入口：:func:`atri_x.media.imagecodec.jpeg_decode.decode_jpeg` 与
  ``atri_x.media.imagecodec.image.decode``（强制 ``pure`` 后端）；
* 本仓纯编码器（4:2:0）的产物也要能被 Pillow 参考解码对上。

误差上限取"实测值 × 约 2~4"的余量；改算法前先改这里（上限收紧或放宽都要有实测
依据）。Pillow 缺失时整个用例 ``skipTest``——没有 reference decoder 就没法判精度。

与 Go ``image/jpeg`` **未做差分验证**：本用例的基准是 libjpeg（Pillow），Go 标准库
解码器的上采样/取整细节与 libjpeg 不同，"对齐 Pillow"不等于"对齐 Go"。
"""

from __future__ import annotations

import io
import unittest
from typing import Callable, NamedTuple

from atri_x.media.imagecodec import Image, jpeg_decode, jpeg_encode
from atri_x.media.imagecodec import image as image_module
from atri_x.media.imagecodec.backend_probe import pillow_module

PIL_IMAGE = pillow_module()
#: 参考解码器（libjpeg）不可用时没有精度基准，整个用例跳过。
SKIP_REASON = "精度基准需要 Pillow/libjpeg 参考解码；本机没有可用的 Pillow"

#: 质量参数：与运行时默认（``image.encode_jpeg`` 的 85）附近取 90，量化表更接近标准表。
QUALITY = 90

#: 各采样的误差上限 ``(平均差, 最大差)``，括号里是 Pillow 12.3.0 实测值。
LIMITS_444 = (0.5, 6)  # 实测 <=0.12 / 2
LIMITS_422 = (1.5, 10)  # 实测 <=0.63 / 4
LIMITS_420 = (2.5, 12)  # 实测 <=1.08 / 4
LIMITS_GRAY = (0.5, 4)  # 实测 0 / 0（灰度无上采样）
LIMITS_BY_SUBSAMPLING = {0: LIMITS_444, 1: LIMITS_422, 2: LIMITS_420}
#: 极小尺寸 + 平滑梯度：色度上采样边界与 libjpeg 的 fancy upsampling 差异明显。
LIMITS_TINY_444 = (0.5, 6)
LIMITS_TINY_422 = (15.0, 50)  # 实测 10.04 / 34
LIMITS_TINY_420 = (20.0, 80)  # 实测 11.80 / 53


class Case(NamedTuple):
    """一个固定样本：怎么造图、怎么编码、允许多大误差。"""

    name: str
    maker: Callable[[int, int], Image]
    width: int
    height: int
    mode: str
    subsampling: int
    max_mean: float
    max_peak: int

    @property
    def label(self) -> str:
        sub = "gray" if self.mode == "L" else {0: "444", 1: "422", 2: "420"}[self.subsampling]
        return "%s[%s]" % (self.name, sub)


# ---------------------------------------------------------------------------
# 确定性样本（无随机数、无外部文件）
# ---------------------------------------------------------------------------


def _lcg(seed: int) -> Callable[[], int]:
    """固定种子的 LCG：跨进程/跨平台可复现（不用 ``random`` 以免实现变动）。"""
    state = seed & 0xFFFFFFFF

    def nxt() -> int:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state

    return nxt


def gradient(width: int, height: int) -> Image:
    """三通道渐变（含对角分量）：平滑图，主要暴露 IDCT / 上采样取整差异。"""
    data = bytearray()
    span_x = max(width - 1, 1)
    span_y = max(height - 1, 1)
    span_d = max(width + height - 2, 1)
    for y in range(height):
        for x in range(width):
            data += bytes(
                (
                    (x * 255) // span_x,
                    (y * 255) // span_y,
                    ((x + y) * 255) // span_d,
                )
            )
    return Image(width, height, "RGB", data)


def chroma_edges(width: int, height: int) -> Image:
    """饱和色竖条（3 像素一根）：高对比色度边缘，暴露色度上采样与 YCbCr 取整。"""
    colors = ((255, 0, 0), (0, 255, 255), (0, 0, 255), (255, 255, 0), (0, 255, 0), (255, 0, 255))
    data = bytearray()
    for _y in range(height):
        for x in range(width):
            data += bytes(colors[(x // 3) % len(colors)])
    return Image(width, height, "RGB", data)


def checker(width: int, height: int) -> Image:
    """4x4 黑白棋盘：亮度高频，量化误差会被放大。"""
    data = bytearray()
    for y in range(height):
        for x in range(width):
            on = ((x // 4) + (y // 4)) % 2 == 0
            data += bytes((235, 235, 235)) if on else bytes((16, 16, 16))
    return Image(width, height, "RGB", data)


def texture(width: int, height: int) -> Image:
    """伪随机纹理（LCG）：接近最坏情况的 DCT 系数分布。"""
    nxt = _lcg(12345)
    data = bytearray()
    for _ in range(width * height):
        data += bytes((nxt() % 256, nxt() % 256, nxt() % 256))
    return Image(width, height, "RGB", data)


def pills(width: int, height: int) -> Image:
    """圆角色块：既有平坦区又有硬边缘（典型的"大块近似"误差源）。"""
    data = bytearray()
    radius_sq = (min(width, height) // 3) ** 2
    for y in range(height):
        for x in range(width):
            inside = ((x - width // 2) ** 2 + (y - height // 2) ** 2) < radius_sq
            data += bytes((250, 240, 200)) if inside else bytes((20, 60, 120))
    return Image(width, height, "RGB", data)


def gray_gradient(width: int, height: int) -> Image:
    """``L`` 模式渐变：走单分量路径（无颜色转换、无上采样）。"""
    data = bytearray()
    span = max(width - 1, 1)
    for _y in range(height):
        for x in range(width):
            data.append((x * 255) // span)
    return Image(width, height, "L", data)


#: 形状表：(名字, 造图函数, 宽, 高, 模式)。宽度/高度刻意包含奇数与非 MCU 对齐值。
SHAPES: tuple[tuple[str, Callable[[int, int], Image], int, int, str], ...] = (
    ("gradient_64x64", gradient, 64, 64, "RGB"),
    ("gradient_33x17_odd", gradient, 33, 17, "RGB"),
    ("chroma_edges_41x39", chroma_edges, 41, 39, "RGB"),
    ("chroma_edges_8x8", chroma_edges, 8, 8, "RGB"),
    ("checker_40x40", checker, 40, 40, "RGB"),
    ("texture_63x65", texture, 63, 65, "RGB"),
    ("pills_37x23", pills, 37, 23, "RGB"),
    ("row_64x1", gradient, 64, 1, "RGB"),
    ("col_1x64", gradient, 1, 64, "RGB"),
    ("long_200x3", gradient, 200, 3, "RGB"),
    ("tall_1x33", gradient, 1, 33, "RGB"),
    ("tiny_3x5", gradient, 3, 5, "RGB"),
    ("gray_33x17", gray_gradient, 33, 17, "L"),
    ("gray_row_64x1", gray_gradient, 64, 1, "L"),
)

#: 极小尺寸（宽 <= 4 且含平滑梯度）：上采样边界差异最大，单独放宽上限。
TINY_NAME = "tiny_3x5"
TINY_LIMITS = {0: LIMITS_TINY_444, 1: LIMITS_TINY_422, 2: LIMITS_TINY_420}


def _limits(name: str, mode: str, subsampling: int) -> tuple[float, int]:
    if mode == "L":
        return LIMITS_GRAY
    if name == TINY_NAME:
        return TINY_LIMITS[subsampling]
    return LIMITS_BY_SUBSAMPLING[subsampling]


def cases() -> list[Case]:
    """Pillow 编码样本：每种采样一份（``L`` 只有单分量，不分子采样）。"""
    out: list[Case] = []
    for name, maker, width, height, mode in SHAPES:
        for subsampling in ((0,) if mode == "L" else (0, 1, 2)):
            mean, peak = _limits(name, mode, subsampling)
            out.append(Case(name, maker, width, height, mode, subsampling, mean, peak))
    return out


#: 本仓纯编码器样本（彩色固定 4:2:0、灰度单分量），用来验证自家编码产物同样对得上。
ENCODER_LIMITS = (2.5, 12)


def encoder_cases() -> list[Case]:
    out: list[Case] = []
    for name, maker, width, height, mode in SHAPES:
        mean, peak = (LIMITS_GRAY if mode == "L" else
                      (LIMITS_TINY_420 if name == TINY_NAME else ENCODER_LIMITS))
        out.append(Case(name, maker, width, height, mode, 2, mean, peak))
    return out


def pil_encode(case: Case) -> bytes:
    """用 Pillow 编码样本（确定性：同版本 Pillow → 同字节）。"""
    image = case.maker(case.width, case.height)
    handle = PIL_IMAGE.frombytes(image.mode, (image.width, image.height), image.to_bytes())
    buffer = io.BytesIO()
    handle.save(buffer, format="JPEG", quality=QUALITY, subsampling=case.subsampling)
    return buffer.getvalue()


def pil_decode(data: bytes, *, gray: bool) -> bytes:
    """参考解码：libjpeg 解出的像素（模式归一化成 ``L`` / ``RGB``）。"""
    with PIL_IMAGE.open(io.BytesIO(data)) as handle:
        handle.load()
        return handle.convert("L" if gray else "RGB").tobytes()


def errors(actual: bytes, expected: bytes) -> tuple[float, int]:
    """``(平均绝对误差, 最大绝对误差)``，逐通道逐像素。"""
    if len(actual) != len(expected):
        raise AssertionError("像素缓冲长度不一致：%d != %d" % (len(actual), len(expected)))
    total = 0
    peak = 0
    for left, right in zip(actual, expected):
        diff = left - right
        if diff < 0:
            diff = -diff
        total += diff
        if diff > peak:
            peak = diff
    return total / len(actual), peak


def measure_all() -> list[tuple[str, float, int, float, int, int]]:
    """跑一遍全部样本，返回 ``(标签, 平均差, 最大差, 平均上限, 最大上限, 字节数)``。

    直接可被外部脚本调用，用来复核"上限 vs 实测"（改上限时先看这张表）。
    """
    rows: list[tuple[str, float, int, float, int, int]] = []
    for case in cases():
        data = pil_encode(case)
        decoded = jpeg_decode.decode_jpeg(data)
        mean, peak = errors(decoded.to_bytes(), pil_decode(data, gray=case.mode == "L"))
        rows.append((case.label, mean, peak, case.max_mean, case.max_peak, len(data)))
    for case in encoder_cases():
        data = jpeg_encode.encode_jpeg(case.maker(case.width, case.height), QUALITY)
        decoded = jpeg_decode.decode_jpeg(data)
        mean, peak = errors(decoded.to_bytes(), pil_decode(data, gray=case.mode == "L"))
        rows.append((case.label + "+pure_encoder", mean, peak, case.max_mean, case.max_peak, len(data)))
    return rows


@unittest.skipUnless(PIL_IMAGE is not None, SKIP_REASON)
class JpegPrecisionTest(unittest.TestCase):
    """固定样本 + 误差上限：纯解码器必须与 libjpeg 在同一量级内。"""

    def tearDown(self) -> None:
        image_module.set_backend("auto")

    def test_samples_cover_the_required_shapes(self) -> None:
        """形状/采样覆盖是硬要求：少了任何一类，精度口径就没有依据。"""
        named = {case.name for case in cases()}
        for required in (
            "gradient_33x17_odd",
            "chroma_edges_41x39",
            "row_64x1",
            "col_1x64",
            "long_200x3",
            "tall_1x33",
            TINY_NAME,
        ):
            self.assertIn(required, named)
        self.assertEqual({case.subsampling for case in cases() if case.mode == "RGB"}, {0, 1, 2})
        self.assertEqual({case.mode for case in cases()}, {"RGB", "L"})

    def test_reference_decoder_sees_the_declared_geometry(self) -> None:
        for case in cases():
            data = pil_encode(case)
            with PIL_IMAGE.open(io.BytesIO(data)) as handle:
                self.assertEqual(handle.size, (case.width, case.height), case.label)
            decoded = jpeg_decode.decode_jpeg(data)
            self.assertEqual((decoded.width, decoded.height), (case.width, case.height), case.label)
            self.assertEqual(decoded.mode, "L" if case.mode == "L" else "RGB", case.label)

    def test_pure_decoder_is_within_tolerance_of_libjpeg(self) -> None:
        for case in cases():
            with self.subTest(case=case.label):
                data = pil_encode(case)
                decoded = jpeg_decode.decode_jpeg(data)
                mean, peak = errors(decoded.to_bytes(), pil_decode(data, gray=case.mode == "L"))
                self.assertLessEqual(
                    mean,
                    case.max_mean,
                    "%s 平均差 %.3f 超过上限 %.3f" % (case.label, mean, case.max_mean),
                )
                self.assertLessEqual(
                    peak,
                    case.max_peak,
                    "%s 最大差 %d 超过上限 %d" % (case.label, peak, case.max_peak),
                )

    def test_image_decode_pure_path_matches_libjpeg(self) -> None:
        """``image.decode`` 强制 pure 后端时与 ``decode_jpeg`` 等价且精度达标。"""
        from atri_x.media.imagecodec import decode

        image_module.set_backend("pure")
        for case in cases():
            with self.subTest(case=case.label):
                data = pil_encode(case)
                decoded = decode(data)
                self.assertEqual(decoded, jpeg_decode.decode_jpeg(data), case.label)
                mean, peak = errors(decoded.to_bytes(), pil_decode(data, gray=case.mode == "L"))
                self.assertLessEqual(mean, case.max_mean, case.label)
                self.assertLessEqual(peak, case.max_peak, case.label)

    def test_repo_encoder_output_still_matches_libjpeg(self) -> None:
        """本仓纯编码器（彩色 4:2:0）的产物，Pillow 能解、纯解码器也对得上。"""
        for case in encoder_cases():
            with self.subTest(case=case.label):
                data = jpeg_encode.encode_jpeg(case.maker(case.width, case.height), QUALITY)
                decoded = jpeg_decode.decode_jpeg(data)
                mean, peak = errors(decoded.to_bytes(), pil_decode(data, gray=case.mode == "L"))
                self.assertLessEqual(mean, case.max_mean, case.label)
                self.assertLessEqual(peak, case.max_peak, case.label)


if __name__ == "__main__":
    unittest.main()
