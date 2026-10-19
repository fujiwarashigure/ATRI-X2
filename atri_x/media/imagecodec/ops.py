"""图像后处理：白底合成、等比缩放、阈值压缩。

对应 Go ``internal/media/image.go`` 的 ``compressImage`` / ``resizeImage``
（「解码 → 缩放 → 白底合成 → JPEG 重编码」链路）。

与 Go 的对应关系与差异：

* 边长判定与 Go 一致：``长边 >= max_edge`` 就算超限；压缩目标长边取
  ``max_edge - 1``，保证再次判定时不再触发（Go 用 ``maxLength - 1``）。
* 字节预算判定与 Go 一致：编码结果必须 ``< max_bytes``（严格小于）。
* 质量阶梯从调用方给的 ``quality`` 起，每轮 -5，最低 35（Go 固定从 92 起）。
* 合成取整用四舍五入（``+127) // 255``），Go 用截断，最多差 1 个色阶。
"""

from __future__ import annotations

from .image import (
    Image,
    ImageCodecError,
    ImageDecodeError,
    ImageEncodeError,
    UnsupportedImageError,
    _MODE_CHANNELS,
    _luma,
    decode,
    encode_jpeg,
    resize,
    sniff,
)

#: 压缩时最多缩小这么多轮，避免病态输入长时间占住 CPU。
_MAX_SHRINK_ROUNDS = 40


def convert(img: Image, mode: str) -> Image:
    """模式转换：``P`` 展开成真彩，``RGB``/``RGBA``/``L``/``LA`` 互转。

    转 ``RGB``/``L`` 时 alpha 直接丢弃（白底合成用 :func:`to_rgb`）；转 ``P``
    需要量化，抛 :class:`UnsupportedImageError`。``Image.convert`` 委托到这里。
    """
    if mode == img.mode:
        return img.copy()
    if mode not in _MODE_CHANNELS:
        raise UnsupportedImageError(f"不支持的像素模式 {mode!r}")
    if mode == "P":
        raise UnsupportedImageError("不支持量化到调色板模式 P")
    source = img.mode
    pixels = img.width * img.height
    data = img.data
    alpha_map: bytes | None = None
    if source == "P":
        # 调色板补齐到 256 项，避免越界索引（PNG 允许调色板短于 2^位深）
        palette = ((img.palette or b"") + bytes(768))[:768]
        expanded = bytearray(pixels * 3)
        for i in range(pixels):
            base = data[i] * 3
            expanded[i * 3 : i * 3 + 3] = bytes(
                (palette[base], palette[base + 1], palette[base + 2])
            )
        data, source, alpha_map = expanded, "RGB", img.transparency
    out = bytearray(pixels * _MODE_CHANNELS[mode])
    for i in range(pixels):
        if source == "L":
            value = data[i]
            red = green = blue = value
            alpha = 255
        elif source == "LA":
            value = data[i * 2]
            red = green = blue = value
            alpha = data[i * 2 + 1]
        elif source == "RGB":
            base = i * 3
            red, green, blue = data[base], data[base + 1], data[base + 2]
            alpha = alpha_map[i] if alpha_map is not None and i < len(alpha_map) else 255
        else:  # RGBA
            base = i * 4
            red, green, blue = data[base], data[base + 1], data[base + 2]
            alpha = data[base + 3]
        if mode == "RGB":
            out[i * 3 : i * 3 + 3] = bytes((red, green, blue))
        elif mode == "RGBA":
            out[i * 4 : i * 4 + 4] = bytes((red, green, blue, alpha))
        elif mode == "L":
            out[i] = _luma(red, green, blue)
        else:  # LA
            out[i * 2 : i * 2 + 2] = bytes((_luma(red, green, blue), alpha))
    return Image(img.width, img.height, mode, out)


def to_rgb(img: Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image:
    """白底（或指定底色）合成去掉 alpha，返回 ``RGB`` 图像。"""
    if not isinstance(background, (tuple, list)) or len(background) != 3:
        raise ImageCodecError(f"底色需要 3 个分量，收到 {background!r}")
    try:
        bg_r, bg_g, bg_b = (int(value) for value in background)
    except (TypeError, ValueError) as exc:
        raise ImageCodecError(f"底色分量非法：{background!r}") from exc
    for value in (bg_r, bg_g, bg_b):
        if not 0 <= value <= 255:
            raise ImageCodecError(f"底色分量越界：{background!r}")
    if img.mode == "P":
        img = img.convert("RGBA" if img.transparency is not None else "RGB")
    if img.mode == "RGB":
        return img.copy()
    if img.mode == "L":
        return img.convert("RGB")
    pixels = img.width * img.height
    out = bytearray(pixels * 3)
    data = img.data
    if img.mode == "LA":
        background_luma = (19595 * bg_r + 38470 * bg_g + 7471 * bg_b + (1 << 15)) >> 16
        for i in range(pixels):
            alpha = data[i * 2 + 1]
            value = data[i * 2]
            inverse = 255 - alpha
            blended = (value * alpha + background_luma * inverse + 127) // 255
            out[i * 3] = blended
            out[i * 3 + 1] = blended
            out[i * 3 + 2] = blended
        return Image(img.width, img.height, "RGB", out)
    if img.mode != "RGBA":
        raise UnsupportedImageError(f"无法把模式 {img.mode!r} 合成为 RGB")
    for i in range(pixels):
        base = i * 4
        alpha = data[base + 3]
        inverse = 255 - alpha
        target = i * 3
        out[target] = (data[base] * alpha + bg_r * inverse + 127) // 255
        out[target + 1] = (data[base + 1] * alpha + bg_g * inverse + 127) // 255
        out[target + 2] = (data[base + 2] * alpha + bg_b * inverse + 127) // 255
    return Image(img.width, img.height, "RGB", out)


def _fit_edge(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """按长边收敛到 ``max_edge`` 以内（与 Go 的 ``int(float64(w) * scale)`` 同取整）。"""
    if max_edge <= 0:
        return width, height
    longest = max(width, height)
    if longest <= max_edge:
        return width, height
    scale = max_edge / longest
    return max(1, int(width * scale)), max(1, int(height * scale))


def resize_to_fit(img: Image, max_width: int, max_height: int) -> Image:
    """按宽高比缩放到不超过给定框（不放大），默认 Lanczos3。"""
    if max_width <= 0 or max_height <= 0:
        raise ImageCodecError(f"目标框非法：{max_width}x{max_height}")
    if img.width <= max_width and img.height <= max_height:
        return img.copy()
    scale = min(max_width / img.width, max_height / img.height)
    width = max(1, int(img.width * scale))
    height = max(1, int(img.height * scale))
    return resize(img, width, height)


def compress(
    data: bytes,
    *,
    max_bytes: int = 0,
    max_edge: int = 0,
    quality: int = 85,
) -> tuple[bytes, bool]:
    """超过字节阈值/边长就转成白底 JPEG。

    返回 ``(字节, 是否压缩过)``：未超限时原样返回入参 ``bytes`` 与 ``False``；
    超限时返回重编码的 JPEG（alpha 已合成到白底、动图只取首帧）。
    无法识别或无法解码的格式在超限时抛 :class:`UnsupportedImageError`。
    """
    raw = data if isinstance(data, bytes) else bytes(data)
    if not raw:
        raise ImageDecodeError("图像数据为空")
    if not 1 <= quality <= 100:
        raise ImageCodecError(f"JPEG 质量 {quality} 越界（1..100）")
    info = sniff(raw)
    over_bytes = max_bytes > 0 and len(raw) > max_bytes
    if info.format == "unknown":
        # 与 Go 的 PrepareVisionImage 一致：无法度量的载荷只看字节预算
        if over_bytes:
            raise UnsupportedImageError(
                f"格式无法识别且体积 {len(raw)} 字节超过 {max_bytes} 字节预算，无法本地压缩"
            )
        return raw, False
    over_edge = max_edge > 0 and max(info.width, info.height) >= max_edge
    if not over_bytes and not over_edge:
        return raw, False

    image = decode(raw)
    target_edge = max(1, max_edge - 1) if over_edge else 0
    width, height = _fit_edge(image.width, image.height, target_edge)
    for _round in range(_MAX_SHRINK_ROUNDS):
        candidate = to_rgb(resize(image, width, height))
        for level in range(quality, 34, -5):
            encoded = encode_jpeg(candidate, level)
            if max_bytes <= 0 or len(encoded) < max_bytes:
                return encoded, True
        if width == 1 and height == 1:
            break
        width = max(1, int(width * 0.85))
        height = max(1, int(height * 0.85))
        if target_edge:
            width, height = _fit_edge(width, height, target_edge)
    raise ImageEncodeError(
        f"图像缩到 {width}x{height} 仍无法压进 {max_bytes} 字节预算"
    )


__all__ = ["compress", "resize_to_fit", "to_rgb"]
