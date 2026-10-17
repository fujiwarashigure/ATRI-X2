"""纯 Python 重采样：最近邻 / 双线性 / Lanczos3（默认）。

核缩放（scale-aware）：降采样时把核支撑按缩放比放大（``support * scale``），
再对权重归一化，因此不会出现「只抽点、不做低通」造成的混叠。上采样不放大
（``resize_image`` 逐轴钳到源尺寸，与对外契约一致）。

Pillow 可用时缩放走 ``PIL.Image.resize``（对应 NEAREST/BILINEAR/LANCZOS），
两条路径的差异只体现在重采样核的实现细节上（Pillow 的 Lanczos 支撑为 3，
与本实现一致），像素级结果可有 ±2 的差别。
"""

from __future__ import annotations

import math

from .image import Image, ImageCodecError, backend as _backend

#: 支持的滤波器名（别名归一化后）。
_FILTERS = {
    "nearest": "nearest",
    "neighbor": "nearest",
    "bilinear": "bilinear",
    "linear": "bilinear",
    "triangle": "bilinear",
    "lanczos": "lanczos",
    "lanczos3": "lanczos",
}
_PIL_FILTERS = {"nearest": "NEAREST", "bilinear": "BILINEAR", "lanczos": "LANCZOS"}


def _sinc(value: float) -> float:
    if value == 0.0:
        return 1.0
    angle = value * math.pi
    return math.sin(angle) / angle


def _lanczos3(distance: float) -> float:
    if distance >= 3.0:
        return 0.0
    return _sinc(distance) * _sinc(distance / 3.0)


def _triangle(distance: float) -> float:
    if distance >= 1.0:
        return 0.0
    return 1.0 - distance


def _kernel(name: str):
    return _lanczos3 if name == "lanczos" else _triangle


def _axis_weights(
    source: int, target: int, name: str
) -> list[tuple[int, float]]:
    """预计算每个目标坐标的 ``(源下标, 权重)`` 列表（权重已归一化）。"""
    scale = target / source
    if name == "nearest":
        return [
            [(min(int((i + 0.5) / scale), source - 1), 1.0)]
            for i in range(target)
        ]
    support = 3.0 if name == "lanczos" else 1.0
    if scale < 1.0:
        support /= scale
    kernel = _kernel(name)
    weights = []
    for i in range(target):
        center = (i + 0.5) / scale - 0.5
        start = int(math.floor(center - support + 0.5))
        stop = int(math.ceil(center + support - 0.5))
        taps = []
        total = 0.0
        for j in range(start, stop + 1):
            if j < 0 or j >= source:
                continue
            distance = abs(j - center)
            weight = kernel(distance if scale >= 1.0 else distance * scale)
            if weight == 0.0:
                continue
            taps.append((j, weight))
            total += weight
        if not taps:
            taps = [(min(max(int(center + 0.5), 0), source - 1), 1.0)]
            total = 1.0
        weights.append([(j, weight / total) for j, weight in taps])
    return weights


def resize_image(img: Image, width: int, height: int, *, filter: str = "lanczos") -> Image:
    """缩放到 ``width`` x ``height``；任一轴大于源尺寸时该轴钳到源尺寸（不放大）。"""
    name = _FILTERS.get(filter.strip().lower())
    if name is None:
        raise ImageCodecError(f"未知重采样滤波器 {filter!r}（nearest/bilinear/lanczos）")
    if width <= 0 or height <= 0:
        raise ImageCodecError(f"目标尺寸无效：{width}x{height}")
    target_w = min(int(width), img.width)
    target_h = min(int(height), img.height)
    if target_w == img.width and target_h == img.height:
        return img.copy()
    if img.mode == "P":
        # 调色板索引不能插值，两种后端统一先展开成真彩
        img = img.convert("RGBA" if img.transparency is not None else "RGB")
    if _backend() == "pillow":
        result = _pil_resize(img, target_w, target_h, name)
        if result is not None:
            return result
    return _pure_resize(img, target_w, target_h, name)


def _pil_resize(img: Image, width: int, height: int, name: str) -> Image | None:
    from .image import _pil_from_image, _pil_image, _pil_to_image

    pil = _pil_image()
    if pil is None:  # pragma: no cover - 由 backend() 保证
        return None
    resampling = getattr(pil, "Resampling", pil)
    handle = _pil_from_image(img).resize(
        (width, height), getattr(resampling, _PIL_FILTERS[name])
    )
    return _pil_to_image(handle)


def _pure_resize(img: Image, width: int, height: int, name: str) -> Image:
    channels = img.channels
    data = img.data
    source_w, source_h = img.width, img.height
    weights_x = _axis_weights(source_w, width, name)
    weights_y = _axis_weights(source_h, height, name)
    # 水平 pass：source_h x width
    horizontal = bytearray(source_h * width * channels)
    for y in range(source_h):
        row_base = y * source_w * channels
        out_base = y * width * channels
        for x in range(width):
            taps = weights_x[x]
            if len(taps) == 1:
                source_base = row_base + taps[0][0] * channels
                horizontal[out_base + x * channels : out_base + (x + 1) * channels] = data[
                    source_base : source_base + channels
                ]
                continue
            accumulated = [0.0] * channels
            for index, weight in taps:
                base = row_base + index * channels
                for c in range(channels):
                    accumulated[c] += data[base + c] * weight
            target = out_base + x * channels
            for c in range(channels):
                value = int(accumulated[c] + 0.5)
                horizontal[target + c] = 0 if value < 0 else (255 if value > 255 else value)
    # 垂直 pass：height x width
    out = bytearray(width * height * channels)
    for y in range(height):
        taps = weights_y[y]
        out_base = y * width * channels
        if len(taps) == 1:
            source_base = taps[0][0] * width * channels
            out[out_base : out_base + width * channels] = horizontal[
                source_base : source_base + width * channels
            ]
            continue
        for x in range(width):
            target = out_base + x * channels
            for c in range(channels):
                value = 0.0
                for index, weight in taps:
                    value += horizontal[(index * width + x) * channels + c] * weight
                rounded = int(value + 0.5)
                out[target + c] = 0 if rounded < 0 else (255 if rounded > 255 else rounded)
    return Image(
        width,
        height,
        img.mode,
        out,
        palette=img.palette,
        transparency=img.transparency,
    )


__all__ = ["resize_image"]
