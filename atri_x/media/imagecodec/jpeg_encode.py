"""纯 Python baseline JPEG 编码（对应 Go 的 ``image/jpeg.Encode``）。

编码参数（**写死并说明**）：

* baseline 顺序 DCT（SOF0）、8 位精度、Huffman 编码；
* 彩色图 **4:2:0**（Y 2x2、Cb/Cr 1x1，色度用 2x2 盒式平均），与 Go
  ``jpeg.Encode`` 的默认子采样一致；``L`` 模式编成单分量灰度 JPEG；
* 量化表用 JPEG Annex K 标准表，按 libjpeg 公式随质量缩放
  （``quality < 50 → 5000 / quality``，否则 ``200 - 2 * quality``）；
* Huffman 表用 Annex K 标准表（DC/AC 各明度/色度一份）；
* 段顺序：SOI / APP0(JFIF) / DQT / SOF0 / DHT / SOS / 熵数据 / EOI，
  熵数据里 ``0xFF`` 后插 ``0x00``。

正/反变换共用 :mod:`jpeg_decode` 里的同一张余弦基（``_COS``）与 zigzag 表，
保证自解码往返一致。
"""

from __future__ import annotations

import struct

from .image import Image, ImageCodecError, ImageTooLargeError, MAX_EDGE, UnsupportedImageError
from .jpeg_decode import _COS, _ZIGZAG

#: Annex K 标准量化表（自然顺序，行优先）。
_STD_LUMA_NATURAL = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)
_STD_CHROMA_NATURAL = (
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)
#: Annex K 标准 Huffman 表：(每码长数量的 16 项, 符号表)。
_DC_LUMA_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_DC_LUMA_VALS = bytes(range(12))
_AC_LUMA_BITS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_AC_LUMA_VALS = bytes(
    (
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
    )
)
_DC_CHROMA_BITS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_DC_CHROMA_VALS = bytes(range(12))
_AC_CHROMA_BITS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_AC_CHROMA_VALS = bytes(
    (
        0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
        0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
        0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0, 0x15, 0x62, 0x72, 0xD1,
        0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
        0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44,
        0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
        0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74,
        0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
        0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A,
        0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
        0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
        0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
        0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
    )
)


def _to_zigzag(natural: tuple[int, ...]) -> list[int]:
    return [natural[_ZIGZAG[i]] for i in range(64)]


#: 自然（行优先）序号 → zigzag 序号。
_ZIGZAG_INVERSE = [0] * 64
for _i, _n in enumerate(_ZIGZAG):
    _ZIGZAG_INVERSE[_n] = _i
del _i, _n


def _build_codes(bits: tuple[int, ...], values: bytes) -> dict[int, tuple[int, int]]:
    """规范 Huffman 表 → ``{符号: (码字, 位长)}``。

    注意：JPEG Annex K 的 DC 表**故意留有空码位**（Kraft 和 < 1），因此这里
    只校验「没有过订阅」和「符号数量吻合」，不要求码字空间被占满。
    """
    codes: dict[int, tuple[int, int]] = {}
    code = 0
    index = 0
    for length in range(1, 17):
        count = bits[length - 1]
        if code + count > (1 << length):
            raise ImageCodecError("内置 Huffman 表过订阅（码字空间不足）")
        for _ in range(count):
            codes[values[index]] = (code, length)
            code += 1
            index += 1
        code <<= 1
    if index != len(values) or len(codes) != len(values):
        raise ImageCodecError("内置 Huffman 表符号数量不吻合")
    return codes


_DC_LUMA_CODES = _build_codes(_DC_LUMA_BITS, _DC_LUMA_VALS)
_AC_LUMA_CODES = _build_codes(_AC_LUMA_BITS, _AC_LUMA_VALS)
_DC_CHROMA_CODES = _build_codes(_DC_CHROMA_BITS, _DC_CHROMA_VALS)
_AC_CHROMA_CODES = _build_codes(_AC_CHROMA_BITS, _AC_CHROMA_VALS)


class _BitWriter:
    """MSB 优先的位写入器，自动做 ``0xFF`` 字节填充。"""

    __slots__ = ("buffer", "acc", "cnt")

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.acc = 0
        self.cnt = 0

    def write(self, value: int, length: int) -> None:
        if length <= 0:
            return
        self.acc = (self.acc << length) | (value & ((1 << length) - 1))
        self.cnt += length
        while self.cnt >= 8:
            self.cnt -= 8
            byte = (self.acc >> self.cnt) & 0xFF
            self.buffer.append(byte)
            if byte == 0xFF:
                self.buffer.append(0x00)
        self.acc &= (1 << self.cnt) - 1

    def flush(self) -> bytes:
        """用 1 比特补齐最后一个字节（JPEG 规定）。"""
        if self.cnt:
            self.write((1 << (8 - self.cnt)) - 1, 8 - self.cnt)
        return bytes(self.buffer)


def _quality_tables(quality: int) -> tuple[list[int], list[int], bytes, bytes]:
    """按质量缩放标准量化表。

    返回 ``(明度自然序, 色度自然序, 明度 zigzag 序, 色度 zigzag 序)``：
    量化用自然序，写 DQT 用 zigzag 序。
    """
    if quality < 1:
        quality = 1
    elif quality > 100:
        quality = 100
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    luma = [min(255, max(1, (value * scale + 50) // 100)) for value in _STD_LUMA_NATURAL]
    chroma = [
        min(255, max(1, (value * scale + 50) // 100)) for value in _STD_CHROMA_NATURAL
    ]
    return luma, chroma, bytes(_to_zigzag(tuple(luma))), bytes(_to_zigzag(tuple(chroma)))


def _fdct(samples: list[int], cos: tuple[float, ...]) -> list[float]:
    """8x8 前向 DCT（可分离双精度浮点，与解码共用余弦基）。"""
    tmp = [0.0] * 64
    for y in range(8):
        base = y * 8
        for u in range(8):
            acc = 0.0
            offset = u * 8
            for x in range(8):
                acc += samples[base + x] * cos[offset + x]
            tmp[base + u] = acc
    out = [0.0] * 64
    for v in range(8):
        offset = v * 8
        for u in range(8):
            acc = 0.0
            for y in range(8):
                acc += tmp[y * 8 + u] * cos[offset + y]
            out[offset + u] = acc
    return out


def _quantize(coefficients: list[float], table: list[int]) -> list[int]:
    """量化并输出 zigzag 顺序，使用「四舍五入、远离零」的整数化。"""
    out = [0] * 64
    inverse = _ZIGZAG_INVERSE
    for v in range(8):
        base = v * 8
        for u in range(8):
            value = coefficients[base + u]
            divisor = table[base + u]
            out[inverse[base + u]] = (
                int(value / divisor + 0.5) if value >= 0 else -int(-value / divisor + 0.5)
            )
    return out


def _encode_block(
    writer: _BitWriter,
    block: list[int],
    previous_dc: int,
    dc_codes: dict[int, tuple[int, int]],
    ac_codes: dict[int, tuple[int, int]],
) -> int:
    """写一个块的熵编码，返回新的 DC 预测值。"""
    diff = block[0] - previous_dc
    size = diff.bit_length() if diff >= 0 else (-diff).bit_length()
    code, length = dc_codes[size]
    writer.write(code, length)
    if size:
        writer.write(diff if diff > 0 else diff + (1 << size) - 1, size)
    run = 0
    for k in range(1, 64):
        value = block[k]
        if value == 0:
            run += 1
            continue
        while run > 15:
            code, length = ac_codes[0xF0]
            writer.write(code, length)
            run -= 16
        size = value.bit_length() if value > 0 else (-value).bit_length()
        code, length = ac_codes[(run << 4) | size]
        writer.write(code, length)
        writer.write(value if value > 0 else value + (1 << size) - 1, size)
        run = 0
    if run:
        code, length = ac_codes[0x00]
        writer.write(code, length)
    return block[0]


def _pad_plane(plane: bytearray, width: int, height: int, stride: int, pad_h: int) -> bytearray:
    """把 ``height x width`` 的平面补齐到 ``pad_h x stride``（右侧/底部重复边缘）。"""
    out = bytearray(stride * pad_h)
    for y in range(pad_h):
        source_y = y if y < height else height - 1
        base = source_y * width
        row = plane[base : base + width]
        if width < stride:
            row = row + bytes([row[-1]]) * (stride - width)
        out[y * stride : (y + 1) * stride] = row
    return out


def _blocks_from_plane(
    plane: bytearray, stride: int, bx: int, by: int
) -> list[int]:
    """取 8x8 块并做电平位移（-128）。"""
    block = [0] * 64
    base = by * stride + bx
    for row in range(8):
        start = base + row * stride
        seg = plane[start : start + 8]
        offset = row * 8
        for i in range(8):
            block[offset + i] = seg[i] - 128
    return block


def _clamp(value: int) -> int:
    if value < 0:
        return 0
    if value > 255:
        return 255
    return value


def _gray_plane(img: Image, pad_w: int, pad_h: int) -> bytearray:
    """``L``/``LA`` 图像 → 按 MCU 网格补齐的灰度平面。"""
    if img.mode == "LA":
        source = img.data[0::2]
    else:
        source = img.data
    width, height = img.width, img.height
    out = bytearray(pad_w * pad_h)
    for y in range(pad_h):
        source_y = y if y < height else height - 1
        row = source[source_y * width : source_y * width + width]
        out[y * pad_w : y * pad_w + width] = row
        if pad_w > width:
            out[y * pad_w + width : (y + 1) * pad_w] = bytes([row[-1]]) * (pad_w - width)
    return out


def _build_planes(
    img: Image, pad_w: int, pad_h: int, chroma_w: int, chroma_h: int
) -> tuple[bytearray, bytearray, bytearray]:
    """RGB → (Y, Cb, Cr)，色度取 2x2 盒式平均，三个平面都按 MCU 网格补齐。"""
    if img.mode == "RGB":
        data = img.data
    else:
        data = img.convert("RGB").data
    width, height = img.width, img.height
    luma = bytearray(pad_w * pad_h)
    for y in range(pad_h):
        source_y = y if y < height else height - 1
        row_base = source_y * width * 3
        out_base = y * pad_w
        last = 0
        for x in range(width):
            base = row_base + x * 3
            red = data[base]
            green = data[base + 1]
            blue = data[base + 2]
            # BT.601 亮度，与 Image.convert("L") 同一取整
            last = (19595 * red + 38470 * green + 7471 * blue + (1 << 15)) >> 16
            luma[out_base + x] = last
        if pad_w > width:
            luma[out_base + width : out_base + pad_w] = bytes([last]) * (pad_w - width)
    cb = bytearray(chroma_w * chroma_h)
    cr = bytearray(chroma_w * chroma_h)
    for cy in range(chroma_h):
        top = min(cy * 2, height - 1)
        bottom = min(cy * 2 + 1, height - 1)
        row_top = top * width * 3
        row_bottom = bottom * width * 3
        out_base = cy * chroma_w
        for cx in range(chroma_w):
            left = min(cx * 2, width - 1)
            right = min(cx * 2 + 1, width - 1)
            red = green = blue = 0
            for base in (
                row_top + left * 3,
                row_top + right * 3,
                row_bottom + left * 3,
                row_bottom + right * 3,
            ):
                red += data[base]
                green += data[base + 1]
                blue += data[base + 2]
            red >>= 2
            green >>= 2
            blue >>= 2
            cb[out_base + cx] = _clamp(
                int(128.0 - 0.168736 * red - 0.331264 * green + 0.5 * blue + 0.5)
            )
            cr[out_base + cx] = _clamp(
                int(128.0 + 0.5 * red - 0.418688 * green - 0.081312 * blue + 0.5)
            )
    return luma, cb, cr


# ---------------------------------------------------------------------------
# 段编码
# ---------------------------------------------------------------------------


def _segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def _app0() -> bytes:
    return _segment(
        0xE0, b"JFIF\x00" + bytes([1, 1, 0]) + struct.pack(">HH", 1, 1) + bytes([0, 0])
    )


def _dqt(luma_zz: bytes, chroma_zz: bytes, gray: bool) -> bytes:
    payload = bytearray([0x00])
    payload += luma_zz
    if not gray:
        payload += bytes([0x01]) + chroma_zz
    return _segment(0xDB, bytes(payload))


def _sof0(width: int, height: int, gray: bool) -> bytes:
    if gray:
        components = bytes([1, 0x11, 0])
    else:
        components = bytes([1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    payload = struct.pack(">BHHB", 8, height, width, 1 if gray else 3) + components
    return _segment(0xC0, payload)


def _dht(gray: bool) -> bytes:
    payload = bytearray([0x00])
    payload += bytes(_DC_LUMA_BITS) + _DC_LUMA_VALS
    payload += bytes([0x10]) + bytes(_AC_LUMA_BITS) + _AC_LUMA_VALS
    if not gray:
        payload += bytes([0x01]) + bytes(_DC_CHROMA_BITS) + _DC_CHROMA_VALS
        payload += bytes([0x11]) + bytes(_AC_CHROMA_BITS) + _AC_CHROMA_VALS
    return _segment(0xC4, bytes(payload))


def _sos(gray: bool) -> bytes:
    if gray:
        payload = bytes([1, 1, 0x00, 0, 63, 0])
    else:
        payload = bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0])
    return _segment(0xDA, payload)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def encode_jpeg(img: Image, quality: int = 85) -> bytes:
    """把 :class:`Image` 编码成 baseline JPEG（彩色 4:2:0，``L`` 走灰度）。"""
    if not 1 <= quality <= 100:
        raise ImageCodecError(f"JPEG 质量 {quality} 越界（1..100）")
    if img.width <= 0 or img.height <= 0:
        raise ImageCodecError(f"图像尺寸无效：{img.width}x{img.height}")
    if img.width > MAX_EDGE or img.height > MAX_EDGE:
        raise ImageTooLargeError(f"JPEG 编码边长 {img.width}x{img.height} 超上限")
    if img.mode not in ("L", "LA", "RGB", "RGBA", "P"):
        raise UnsupportedImageError(f"JPEG 编码不支持模式 {img.mode!r}")
    if img.mode not in ("L", "RGB"):
        # 直接调用本模块时也要保证 alpha 合成到白底、调色板展开
        from .ops import to_rgb

        img = to_rgb(img)

    luma_table, chroma_table, luma_zz, chroma_zz = _quality_tables(quality)
    gray = img.mode == "L"
    cos = _COS
    writer = _BitWriter()

    if gray:
        pad_w = ((img.width + 15) // 16) * 16
        pad_h = ((img.height + 15) // 16) * 16
        plane = _gray_plane(img, pad_w, pad_h)
        predictor = 0
        for my in range(pad_h // 8):
            for mx in range(pad_w // 8):
                block = _blocks_from_plane(plane, pad_w, mx * 8, my * 8)
                coefficients = _quantize(_fdct(block, cos), luma_table)
                predictor = _encode_block(
                    writer, coefficients, predictor, _DC_LUMA_CODES, _AC_LUMA_CODES
                )
    else:
        pad_w = ((img.width + 15) // 16) * 16
        pad_h = ((img.height + 15) // 16) * 16
        chroma_w = pad_w // 2
        chroma_h = pad_h // 2
        luma, cb, cr = _build_planes(img, pad_w, pad_h, chroma_w, chroma_h)
        y_predictor = cb_predictor = cr_predictor = 0
        for my in range(pad_h // 16):
            for mx in range(pad_w // 16):
                for by in (0, 1):
                    block_y = my * 16 + by * 8
                    for bx in (0, 1):
                        block = _blocks_from_plane(luma, pad_w, mx * 16 + bx * 8, block_y)
                        coefficients = _quantize(_fdct(block, cos), luma_table)
                        y_predictor = _encode_block(
                            writer,
                            coefficients,
                            y_predictor,
                            _DC_LUMA_CODES,
                            _AC_LUMA_CODES,
                        )
                block = _blocks_from_plane(cb, chroma_w, mx * 8, my * 8)
                cb_predictor = _encode_block(
                    writer,
                    _quantize(_fdct(block, cos), chroma_table),
                    cb_predictor,
                    _DC_CHROMA_CODES,
                    _AC_CHROMA_CODES,
                )
                block = _blocks_from_plane(cr, chroma_w, mx * 8, my * 8)
                cr_predictor = _encode_block(
                    writer,
                    _quantize(_fdct(block, cos), chroma_table),
                    cr_predictor,
                    _DC_CHROMA_CODES,
                    _AC_CHROMA_CODES,
                )

    out = bytearray(b"\xff\xd8")
    out += _app0()
    out += _dqt(luma_zz, chroma_zz, gray)
    out += _sof0(img.width, img.height, gray)
    out += _dht(gray)
    out += _sos(gray)
    out += writer.flush()
    out += b"\xff\xd9"
    return bytes(out)


__all__ = ["encode_jpeg"]
