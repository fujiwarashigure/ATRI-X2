"""纯 Python baseline JPEG 解码（对应 Go 的 ``image/jpeg`` 解码侧）。

支持：SOI/APPn/COM/DQT（8/16 位精度）/SOF0/SOF1/DHT/SOS/EOI 段解析，DC/AC
Huffman（多表选择）、MCU 解码、DC 预测、AC 游程 + ZRL + EOB、反 zigzag、
浮点可分离 IDCT、上采样（4:4:4 / 4:2:2 / 4:2:0 与任意整数采样因子，半像素
中心的三角插值）、YCbCr→RGB（BT.601 定点）、灰度、Adobe CMYK / YCCK、
DRI 重启间隔 + RSTn 校验。

**IDCT 精度说明**：``_COS`` 是预计算的双精度余弦基，行列各做一次可分离变换，
全零行/列稀疏跳过；源码域误差远小于 0.5 LSB，最后四舍五入并钳位到 0..255。
定点 YCbCr→RGB 与 libjpeg 系数一致（含 +0.5 舍入）。

**与 libjpeg 的实测差异口径**：基准是 Pillow/libjpeg 的解码结果，固定样本 + 每个样本
的平均/最大误差上限见 ``tests/test_imagecodec_jpeg_precision.py``（本机 Pillow 12.3.0、
质量 90 实测，上限留了约 2~4 倍余量；该用例在本机就是唯一可复现的口径来源）：

* 4:4:4：平均差 ≤0.5、最大 ≤6（实测 ≤0.12 / 2）；
* 4:2:2：平均差 ≤1.5、最大 ≤10（实测 ≤0.63 / 4）；
* 4:2:0：平均差 ≤2.5、最大 ≤12（实测 ≤1.08 / 4）；
* 灰度（``L``）：实测逐像素相同（0 / 0），上限 ≤0.5 / 4；
* **极小尺寸 + 平滑梯度**（如 3x5）在色度上采样边界上与 libjpeg 的 fancy upsampling
  差异明显：实测平均 ~11.8、最大 ~53，用例上限放宽到平均 ≤20 / 最大 ≤80。

**与 Go ``image/jpeg`` 未做差分验证**：上面只对齐了 libjpeg（Pillow）；Go 标准库解码器
的上采样与取整细节与 libjpeg 不同，"对齐 Pillow"不等于"对齐 Go"。原文档"平均 ≤0.01、
最大 ≤1"只在 4:4:4 平滑图上成立，已按实测改正（研究报告 §3.3 问题 7）。

**不支持**（抛 :class:`UnsupportedImageError`，异常信息写明原因）：渐进式
SOF2、算术编码 SOF9/SOF10、无损 SOF3、12/16 位精度、多扫描文件、非整数
采样比例。``sniff()`` 仍能把这些文件识别为 jpeg。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from .image import (
    DEFAULT_MAX_DECODE_BYTES,
    MAX_EDGE,
    Image,
    ImageDecodeError,
    ImageTooLargeError,
    UnsupportedImageError,
    pixel_limit,
)

#: zigzag 序号 → 自然（行优先）序号。
_ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)
#: 可分离变换的余弦基：``_COS[u * 8 + x] = alpha(u) / 2 * cos((2x+1)u*pi/16)``。
#: 正变换用 ``M[u][x]``，反变换用同一张表的转置访问。
_COS = tuple(
    (1.0 / math.sqrt(2.0) if u == 0 else 1.0) / 2.0 * math.cos((2 * x + 1) * u * math.pi / 16.0)
    for u in range(8)
    for x in range(8)
)
#: 定点 YCbCr→RGB 查表（BT.601，系数与 libjpeg 相同，含 +0.5 舍入）。
_CR_R = tuple((91881 * (i - 128) + 32768) >> 16 for i in range(256))
_CB_B = tuple((116130 * (i - 128) + 32768) >> 16 for i in range(256))
_CB_G = tuple((-22554 * (i - 128) + 32768) >> 16 for i in range(256))
_CR_G = tuple((-46802 * (i - 128) + 32768) >> 16 for i in range(256))
#: 钳位查表：索引 ``v + 512`` 对 v ∈ [-512, 511] 有效。
_CLAMP = bytes([0] * 512 + list(range(256)) + [255] * 256)

_SOF_MARKERS = frozenset([0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF])
#: 常见变体的可读名（其余回落成 ``SOF<n>``），用于不支持时的报错信息。
_SOF_KINDS = {0xC2: "渐进式(SOF2)", 0xC3: "无损(SOF3)", 0xC9: "算术顺序 DCT(SOF9)", 0xCA: "算术渐进(SOF10)"}


class _BitReader:
    """MSB 优先的位读取器，处理 0xFF00 填充与 RSTn/段结束标记。

    ``pos`` 最多领先消费者两个字节（peek 预取），重启时靠「向前搜索 RSTn」
    重新同步，因此预取不会破坏重启语义。
    """

    __slots__ = ("data", "pos", "end", "acc", "cnt", "marker")

    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data = data
        self.pos = pos
        self.end = end
        self.acc = 0
        self.cnt = 0
        self.marker = 0

    def _refill(self) -> bool:
        if self.marker:
            return False
        pos = self.pos
        if pos >= self.end:
            self.marker = -1
            return False
        byte = self.data[pos]
        pos += 1
        if byte == 0xFF:
            if pos >= self.end:
                self.marker = -1
                return False
            nxt = self.data[pos]
            if nxt == 0x00:
                pos += 1
            else:
                # 标记（含 RSTn）不消费：pos 停在 0xFF 上，交给重启/上层处理
                self.marker = 0xFF00 | nxt
                return False
        self.pos = pos
        self.acc = (self.acc << 8) | byte
        self.cnt += 8
        return True

    def peek8(self) -> int | None:
        while self.cnt < 8:
            if not self._refill():
                return None
        return (self.acc >> (self.cnt - 8)) & 0xFF

    def peek16(self) -> int:
        while self.cnt < 16 and self._refill():
            pass
        if self.cnt >= 16:
            return (self.acc >> (self.cnt - 16)) & 0xFFFF
        return (self.acc << (16 - self.cnt)) & 0xFFFF

    def skip(self, count: int) -> None:
        if self.cnt < count:
            raise ImageDecodeError("JPEG 熵编码数据截断")
        self.cnt -= count
        self.acc &= (1 << self.cnt) - 1

    def receive(self, count: int) -> int:
        if count == 0:
            return 0
        while self.cnt < count:
            if not self._refill():
                raise ImageDecodeError("JPEG 熵编码数据截断")
        self.cnt -= count
        value = (self.acc >> self.cnt) & ((1 << count) - 1)
        self.acc &= (1 << self.cnt) - 1
        return value

    def receive_extend(self, count: int) -> int:
        """接收 ``count`` 位并做 JPEG 的符号扩展。"""
        if count == 0:
            return 0
        value = self.receive(count)
        if value < (1 << (count - 1)):
            value -= (1 << count) - 1
        return value

    def restart(self, expected: int) -> None:
        """丢弃字节内剩余比特并跳到 RSTn（自同步搜索，容许预取误差）。"""
        self.acc = 0
        self.cnt = 0
        pos = self.pos
        end = self.end
        while pos + 2 <= end:
            if self.data[pos] == 0xFF:
                nxt = self.data[pos + 1]
                if 0xD0 <= nxt <= 0xD7:
                    found = nxt - 0xD0
                    if found != expected:
                        raise ImageDecodeError(
                            f"JPEG 重启标记顺序错误：期望 RST{expected}，实际 RST{found}"
                        )
                    self.pos = pos + 2
                    self.marker = 0
                    return
            pos += 1
        raise ImageDecodeError(f"JPEG 缺少重启标记 RST{expected}")


class _HuffmanTable:
    """规范 Huffman 表；前 8 位走 LUT，长码走逐长度查表。"""

    __slots__ = ("codes", "fast")

    def __init__(self, counts: list[int], values: bytes) -> None:
        self.codes: list[dict[int, int]] = [{} for _ in range(17)]
        fast = [0] * 256
        code = 0
        index = 0
        for length in range(1, 17):
            for _ in range(counts[length - 1]):
                if index >= len(values):
                    raise ImageDecodeError("JPEG Huffman 表符号数量不足")
                if code >= (1 << length):
                    raise ImageDecodeError("JPEG Huffman 表码字溢出（表损坏）")
                symbol = values[index]
                index += 1
                self.codes[length][code] = symbol
                if length <= 8:
                    entry = (symbol << 4) | length
                    for slot in range(code << (8 - length), (code + 1) << (8 - length)):
                        fast[slot] = entry
                code += 1
            code <<= 1
        self.fast = fast

    def decode(self, reader: _BitReader) -> int:
        bits8 = reader.peek8()
        if bits8 is not None:
            entry = self.fast[bits8]
            if entry:
                reader.skip(entry & 0x0F)
                return entry >> 4
        peek = reader.peek16()
        for length in range(1, 17):
            table = self.codes[length]
            if table:
                symbol = table.get(peek >> (16 - length))
                if symbol is not None:
                    reader.skip(length)
                    return symbol
        raise ImageDecodeError("JPEG Huffman 码字无效（数据损坏）")


@dataclass
class _Component:
    id: int
    h: int
    v: int
    tq: int
    plane: bytearray = field(default_factory=bytearray)
    stride: int = 0
    predictor: int = 0


@dataclass
class _Frame:
    marker: int
    width: int
    height: int
    components: list[_Component]

    @property
    def hmax(self) -> int:
        return max(c.h for c in self.components)

    @property
    def vmax(self) -> int:
        return max(c.v for c in self.components)


def _parse_dqt(segment: bytes, quant: dict[int, list[int]]) -> None:
    pos = 0
    end = len(segment)
    while pos < end:
        spec = segment[pos]
        pos += 1
        precision = spec >> 4
        table_id = spec & 15
        if precision == 0:
            if pos + 64 > end:
                raise ImageDecodeError("JPEG DQT 段截断")
            quant[table_id] = list(segment[pos : pos + 64])
            pos += 64
        elif precision == 1:
            if pos + 128 > end:
                raise ImageDecodeError("JPEG DQT 段截断")
            quant[table_id] = [struct.unpack_from(">H", segment, pos + 2 * i)[0] for i in range(64)]
            pos += 128
        else:
            raise ImageDecodeError(f"JPEG DQT 精度 {precision} 非法")


def _parse_dht(segment: bytes, dc: dict[int, _HuffmanTable], ac: dict[int, _HuffmanTable]) -> None:
    pos = 0
    end = len(segment)
    while pos < end:
        spec = segment[pos]
        pos += 1
        table_class = spec >> 4
        table_id = spec & 15
        if pos + 16 > end:
            raise ImageDecodeError("JPEG DHT 段截断")
        counts = list(segment[pos : pos + 16])
        pos += 16
        total = sum(counts)
        if pos + total > end:
            raise ImageDecodeError("JPEG DHT 段截断（符号表不完整）")
        table = _HuffmanTable(counts, segment[pos : pos + total])
        pos += total
        if table_class == 0:
            dc[table_id] = table
        elif table_class == 1:
            ac[table_id] = table
        else:
            raise ImageDecodeError(f"JPEG DHT 表类别 {table_class} 非法")


def _parse_sof(marker: int, segment: bytes) -> _Frame:
    if marker not in (0xC0, 0xC1):
        kind = _SOF_KINDS.get(marker, f"SOF{marker - 0xC0}")
        raise UnsupportedImageError(f"JPEG 只解码 baseline 顺序 DCT（SOF0/SOF1），该文件是 {kind}")
    if len(segment) < 6:
        raise ImageDecodeError("JPEG SOF 段过短")
    precision = segment[0]
    height, width = struct.unpack_from(">HH", segment, 1)
    count = segment[5]
    if precision != 8:
        raise UnsupportedImageError(f"JPEG 只支持 8 位精度，该文件是 {precision} 位")
    if count not in (1, 3, 4):
        raise UnsupportedImageError(f"JPEG 分量数 {count} 不受支持")
    if len(segment) < 6 + 3 * count:
        raise ImageDecodeError("JPEG SOF 段截断（分量表不完整）")
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"JPEG 声明尺寸无效：{width}x{height}")
    components = []
    for i in range(count):
        sampling = segment[7 + 3 * i]
        h, v = sampling >> 4, sampling & 15
        if not (1 <= h <= 4 and 1 <= v <= 4):
            raise UnsupportedImageError(f"JPEG 采样因子 {h}x{v} 越界")
        components.append(_Component(segment[6 + 3 * i], h, v, segment[8 + 3 * i]))
    if len({component.id for component in components}) != count:
        raise ImageDecodeError("JPEG SOF 分量 ID 重复")
    return _Frame(marker, width, height, components)


def _find_scan_end(data: bytes, start: int) -> int:
    """找到熵编码数据之后的第一个段标记位置（跳过 FF00 与 RSTn 填充）。"""
    pos = start
    end = len(data)
    while pos + 1 < end:
        if data[pos] == 0xFF:
            nxt = data[pos + 1]
            if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                pos += 2
                continue
            return pos
        pos += 1
    return end


def _decode_block(
    reader: _BitReader, dc_table: _HuffmanTable, ac_table: _HuffmanTable, quant: list[int], predictor: int
) -> tuple[list[int], int]:
    """解出一个 8x8 块（自然顺序、已反量化）。量化表按 zigzag 序存放。"""
    coeffs = [0] * 64
    size = dc_table.decode(reader)
    if size:
        if size > 15:
            raise ImageDecodeError(f"JPEG DC 系数位长 {size} 非法")
        predictor += reader.receive_extend(size)
    coeffs[0] = predictor * quant[0]
    k = 1
    zigzag = _ZIGZAG
    while k < 64:
        rs = ac_table.decode(reader)
        run = rs >> 4
        size = rs & 15
        if size == 0:
            if run != 15:
                break  # EOB
            k += 16
            continue
        k += run
        if k > 63:
            raise ImageDecodeError("JPEG AC 系数游程越界")
        coeffs[zigzag[k]] = reader.receive_extend(size) * quant[k]
        k += 1
    return coeffs, predictor


def _idct(coeffs: list[int]) -> bytearray:
    """8x8 反离散余弦变换（可分离双精度浮点，稀疏跳过全零行）。"""
    if not any(coeffs[1:]):
        value = (coeffs[0] + 4) // 8 + 128
        value = 0 if value < 0 else (255 if value > 255 else value)
        return bytearray([value]) * 64
    cos = _COS
    tmp = [0.0] * 64
    rows: list[int] = []
    for v in range(8):
        base = v * 8
        row = coeffs[base : base + 8]
        nonzero = [(u, row[u]) for u in range(8) if row[u]]
        if not nonzero:
            continue
        rows.append(v)
        for x in range(8):
            acc = 0.0
            for u, coefficient in nonzero:
                acc += coefficient * cos[u * 8 + x]
            tmp[base + x] = acc
    if not rows:
        return bytearray([128]) * 64
    out = bytearray(64)
    for y in range(8):
        for x in range(8):
            value = 128.0
            for v in rows:
                value += tmp[v * 8 + x] * cos[v * 8 + y]
            iv = int(value + 0.5)
            if iv < 0:
                iv = 0
            elif iv > 255:
                iv = 255
            out[y * 8 + x] = iv
    return out


def _store_block(component: _Component, block: bytearray, x: int, y: int) -> None:
    plane = component.plane
    stride = component.stride
    base = y * stride + x
    for row in range(8):
        start = row * 8
        plane[base : base + 8] = block[start : start + 8]
        base += stride


def _axis_weights(count_out: int, count_in: int, scale: int) -> list[tuple[int, int, int]] | None:
    """半像素中心的三角插值权重（256 定点）；``scale == 1`` 返回 ``None``。"""
    if scale == 1:
        return None
    weights = []
    last = count_in - 1
    for i in range(count_out):
        position = (i + 0.5) / scale - 0.5
        base = math.floor(position)
        weight = int((position - base) * 256.0 + 0.5)
        other = base + 1
        base = 0 if base < 0 else (last if base > last else base)
        other = 0 if other < 0 else (last if other > last else other)
        weights.append((base, other, weight))
    return weights


def _row_samples(
    component: _Component, y: int, valid_w: int, scale_y: int,
    weights: list[tuple[int, int, int]] | None,
) -> list[int]:
    """某一输出行的分量样本（已做垂直插值），长度为 ``valid_w``。"""
    plane = component.plane
    stride = component.stride
    if scale_y == 1:
        base = y * stride
        return plane[base : base + valid_w]
    assert weights is not None
    top, bottom, weight = weights[y]
    top_base = top * stride
    if weight == 0:
        return plane[top_base : top_base + valid_w]
    bottom_base = bottom * stride
    inverse = 256 - weight
    return [
        (plane[top_base + i] * inverse + plane[bottom_base + i] * weight) >> 8
        for i in range(valid_w)
    ]


def _expand_row(samples: list[int], weights: list[tuple[int, int, int]] | None, width: int) -> list[int]:
    if weights is None:
        return samples
    return [
        (samples[first] * (256 - weight) + samples[second] * weight) >> 8
        for first, second, weight in weights
    ]


def _ycbcr_rows_to_rgb(
    luma: list[int], cb_row: list[int], cr_row: list[int]
) -> tuple[list[int], list[int], list[int]]:
    """整行 YCbCr → RGB（YCCK 用）。"""
    red = [0] * len(luma)
    green = [0] * len(luma)
    blue = [0] * len(luma)
    for x, yy in enumerate(luma):
        cb = cb_row[x]
        cr = cr_row[x]
        red[x] = _CLAMP[yy + _CR_R[cr] + 512]
        green[x] = _CLAMP[yy + _CB_G[cb] + _CR_G[cr] + 512]
        blue[x] = _CLAMP[yy + _CB_B[cb] + 512]
    return red, green, blue


def _to_image(frame: _Frame, adobe_transform: int | None) -> Image:
    """分量平面 → RGB/灰度 ``Image``（含上采样与颜色转换）。"""
    width, height = frame.width, frame.height
    components = frame.components
    if len(components) == 1:
        component = components[0]
        stride = component.stride
        plane = component.plane
        data = bytearray(width * height)
        for y in range(height):
            base = y * stride
            data[y * width : (y + 1) * width] = plane[base : base + width]
        return Image(width, height, "L", data)

    hmax, vmax = frame.hmax, frame.vmax
    specs = []
    for component in components:
        if hmax % component.h or vmax % component.v:
            raise UnsupportedImageError(
                f"JPEG 采样因子 {component.h}x{component.v} 与最大 {hmax}x{vmax} 不整除"
            )
        scale_x = hmax // component.h
        scale_y = vmax // component.v
        valid_w = (width + scale_x - 1) // scale_x
        specs.append(
            (
                component,
                scale_y,
                valid_w,
                _axis_weights(width, valid_w, scale_x),
                _axis_weights(height, (height + scale_y - 1) // scale_y, scale_y),
            )
        )

    def row_for(index: int, y: int) -> list[int]:
        component, scale_y, valid_w, weights_x, weights_y = specs[index]
        return _expand_row(
            _row_samples(component, y, valid_w, scale_y, weights_y), weights_x, width
        )

    # Adobe transform：0 = 分量直通 RGB/CMYK，2 = YCCK；无标记按 YCbCr / 普通 CMYK
    plain_rgb = len(components) == 3 and adobe_transform == 0
    ycck = adobe_transform == 2
    adobe = adobe_transform in (0, 2)
    unit = 255
    data = bytearray(width * height * 3)
    for y in range(height):
        rows = [row_for(index, y) for index in range(len(components))]
        pos = y * width * 3
        if plain_rgb:
            first, second, third = rows
            for x in range(width):
                data[pos] = first[x]
                data[pos + 1] = second[x]
                data[pos + 2] = third[x]
                pos += 3
            continue
        if len(components) == 3:
            luma_row, cb_row, cr_row = rows
            for x in range(width):
                yy = luma_row[x]
                cb = cb_row[x]
                cr = cr_row[x]
                data[pos] = _CLAMP[yy + _CR_R[cr] + 512]
                data[pos + 1] = _CLAMP[yy + _CB_G[cb] + _CR_G[cr] + 512]
                data[pos + 2] = _CLAMP[yy + _CB_B[cb] + 512]
                pos += 3
            continue
        if ycck:
            # YCCK：前三分量是「反相 CMY 的 YCbCr」，转 RGB 即得 255 - 实际值
            c_row, m_row, yellow_row = _ycbcr_rows_to_rgb(rows[0], rows[1], rows[2])
        else:
            c_row, m_row, yellow_row = rows[0], rows[1], rows[2]
        k_row = rows[3]
        for x in range(width):
            k = k_row[x]
            if adobe:
                # Adobe 存的是反相 CMYK：R = C * K / 255（C、K 已是 255 - 实际值）
                data[pos] = (c_row[x] * k) // unit
                data[pos + 1] = (m_row[x] * k) // unit
                data[pos + 2] = (yellow_row[x] * k) // unit
            else:
                data[pos] = ((unit - c_row[x]) * (unit - k)) // unit
                data[pos + 1] = ((unit - m_row[x]) * (unit - k)) // unit
                data[pos + 2] = ((unit - yellow_row[x]) * (unit - k)) // unit
            pos += 3
    return Image(width, height, "RGB", data)


def decode_jpeg(data: bytes, *, max_pixels: int | None = None) -> Image:
    """解码 baseline JPEG 为 :class:`Image`（灰度→``L``，其余→``RGB``）。"""
    raw = data if isinstance(data, bytes) else bytes(data)
    if len(raw) < 4 or raw[0] != 0xFF or raw[1] != 0xD8:
        raise ImageDecodeError("不是 JPEG（缺少 SOI）")
    quant: dict[int, list[int]] = {}
    dc_tables: dict[int, _HuffmanTable] = {}
    ac_tables: dict[int, _HuffmanTable] = {}
    frame: _Frame | None = None
    restart_interval = 0
    adobe_transform: int | None = None
    end = len(raw)
    pos = 2
    while pos + 2 <= end:
        if raw[pos] != 0xFF:
            raise ImageDecodeError("JPEG 段标记损坏（缺少 0xFF 前缀）")
        while pos < end and raw[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = raw[pos]
        pos += 1
        if marker == 0x00:
            raise ImageDecodeError("JPEG 出现游离的 0x00 填充字节")
        if marker == 0xD9:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > end:
            raise ImageDecodeError("JPEG 段长度截断")
        (length,) = struct.unpack_from(">H", raw, pos)
        if length < 2 or pos + length > end:
            raise ImageDecodeError(f"JPEG 段长度 {length} 越界")
        segment = raw[pos + 2 : pos + length]
        if marker == 0xDB:
            _parse_dqt(segment, quant)
        elif marker == 0xC4:
            _parse_dht(segment, dc_tables, ac_tables)
        elif marker in _SOF_MARKERS:
            if frame is not None:
                # sniff()/image_dimensions() 与解码器必须看同一个 SOF；多帧头
                # 说明尺寸有歧义（旧实现嗅探取第一个、解码取最后一个 → 可绕过闸门）。
                raise ImageDecodeError("JPEG 出现多个 SOF 帧头（尺寸有歧义，拒绝）")
            frame = _parse_sof(marker, segment)
        elif marker == 0xDD:
            if len(segment) < 2:
                raise ImageDecodeError("JPEG DRI 段过短")
            (restart_interval,) = struct.unpack_from(">H", segment, 0)
        elif marker == 0xEE:
            if len(segment) >= 12 and segment[:5] == b"Adobe":
                adobe_transform = segment[11]
        elif marker == 0xDA:
            if frame is None:
                raise ImageDecodeError("JPEG 的 SOS 出现在 SOF 之前")
            return _decode_scan(
                raw, segment, pos + length, frame, quant, dc_tables, ac_tables,
                restart_interval, adobe_transform, max_pixels)
        pos += length
    raise ImageDecodeError("JPEG 未找到可解码的扫描（SOS）")


def _decode_scan(
    raw: bytes,
    segment: bytes,
    scan_start: int,
    frame: _Frame,
    quant: dict[int, list[int]],
    dc_tables: dict[int, _HuffmanTable],
    ac_tables: dict[int, _HuffmanTable],
    restart_interval: int,
    adobe_transform: int | None,
    max_pixels: int | None,
) -> Image:
    width, height = frame.width, frame.height
    if width > MAX_EDGE or height > MAX_EDGE:
        raise ImageTooLargeError(f"JPEG 边长 {width}x{height} 超过 {MAX_EDGE} 上限")
    limit = pixel_limit(max_pixels)
    if width * height > limit:
        raise ImageTooLargeError(
            f"JPEG {width}x{height}（{width * height} 像素）超过 {limit} 像素上限"
        )
    if not segment:
        raise ImageDecodeError("JPEG SOS 段过短")
    count = segment[0]
    if count < 1 or len(segment) < 1 + 2 * count + 3:
        raise ImageDecodeError("JPEG SOS 段截断")
    by_id = {component.id: component for component in frame.components}
    if count != len(frame.components):
        # 非交错（每分量一个扫描）需读多个 SOS；本实现只解第一个扫描，
        # 与其静默给出残缺图像，不如明确拒绝。
        raise UnsupportedImageError(
            f"JPEG 非交错/多扫描不受支持：扫描 {count} 分量 / 图像 {len(frame.components)} 分量"
        )
    scan: list[tuple[_Component, _HuffmanTable, _HuffmanTable]] = []
    for i in range(count):
        cid = segment[1 + 2 * i]
        spec = segment[2 + 2 * i]
        component = by_id.get(cid)
        if component is None:
            raise ImageDecodeError(f"JPEG SOS 引用了不存在的分量 {cid}")
        dc_table = dc_tables.get(spec >> 4)
        ac_table = ac_tables.get(spec & 15)
        if dc_table is None or ac_table is None:
            raise ImageDecodeError(f"JPEG 分量 {cid} 引用了未定义的 Huffman 表")
        if component.tq not in quant:
            raise ImageDecodeError(f"JPEG 分量 {cid} 引用了未定义的量化表")
        scan.append((component, dc_table, ac_table))
    if [component.id for component, _dc, _ac in scan] != [
        component.id for component in frame.components
    ]:
        # 扫描顺序与帧头不一致时，MCU 内各分量的数据单元分配会错位，解出的
        # 是"看起来正常但颜色全错"的图像（Pillow 直接拒绝，这里对齐它的口径）。
        raise ImageDecodeError("JPEG SOS 分量顺序与 SOF 不一致（拒绝解出错误图像）")
    ss, se, ahal = segment[1 + 2 * count], segment[2 + 2 * count], segment[3 + 2 * count]
    if ss != 0 or se != 63 or ahal != 0:
        raise UnsupportedImageError(
            "JPEG 只解码顺序 DCT 扫描（Ss=0,Se=63,Ah=Al=0），"
            f"该扫描是 Ss={ss},Se={se},Ah={ahal >> 4},Al={ahal & 15}"
        )

    # 非交错扫描（本实现只接受"扫描分量数 == 帧分量数"，因此单分量帧就是这种情况）：
    # T.81 A.2.2 规定每个 MCU 是**一个**数据单元，采样因子不参与网格计算。旧的
    # "按 hmax/vmax 划 MCU" 写法会让灰度图里 h/v != 1 的文件（例如 Pillow 用
    # ``subsampling=1/2`` 存的 L 图）多读 h*v 个块，合法文件被误报"熵数据截断"。
    single_component = len(scan) == 1
    if single_component:
        mcu_cols = (width + 7) // 8
        mcu_rows = (height + 7) // 8
    else:
        hmax, vmax = frame.hmax, frame.vmax
        mcu_cols = (width + 8 * hmax - 1) // (8 * hmax)
        mcu_rows = (height + 8 * vmax - 1) // (8 * vmax)
    scan_end = _find_scan_end(raw, scan_start)
    # 分配前先做"熵数据下界"校验：每个 8x8 数据单元至少需要 1 bit DC 符号
    # + 1 bit AC 符号，所以合法扫描的熵数据不可能少于 2 bit/块。旧实现在这里
    # 先按 SOF 声明尺寸分配全部分量平面、再读熵数据，于是"几百字节输入声明
    # 巨大尺寸"就能换来几百 MiB 峰值内存（研究报告 §3.3 的 P0）。
    blocks_per_mcu = 1 if single_component else sum(
        component.h * component.v for component in frame.components
    )
    blocks = mcu_cols * mcu_rows * blocks_per_mcu
    min_entropy_bytes = (2 * blocks + 7) // 8
    entropy_bytes = scan_end - scan_start
    if entropy_bytes < min_entropy_bytes:
        raise ImageDecodeError(
            f"JPEG 熵编码数据截断：声明 {width}x{height} 至少需要 "
            f"{min_entropy_bytes} 字节熵数据，实际只有 {max(0, entropy_bytes)} 字节"
        )
    if single_component:
        allocation = sum(mcu_cols * 8 * mcu_rows * 8 for _ in frame.components)
    else:
        allocation = sum(
            (mcu_cols * 8 * component.h) * (mcu_rows * 8 * component.v)
            for component in frame.components
        )
    allocation += width * height * 3
    if allocation > DEFAULT_MAX_DECODE_BYTES:
        raise ImageTooLargeError(
            f"JPEG {width}x{height} 解码约需 {allocation} 字节内存，"
            f"超过 {DEFAULT_MAX_DECODE_BYTES} 字节预算"
        )
    for component in frame.components:
        if single_component:
            component.stride = mcu_cols * 8
            component.plane = bytearray(component.stride * mcu_rows * 8)
        else:
            component.stride = mcu_cols * 8 * component.h
            component.plane = bytearray(component.stride * mcu_rows * 8 * component.v)
        component.predictor = 0

    reader = _BitReader(raw, scan_start, scan_end)
    expected_rst = 0
    mcu_index = 0
    for row in range(mcu_rows):
        for col in range(mcu_cols):
            if restart_interval and mcu_index and mcu_index % restart_interval == 0:
                reader.restart(expected_rst)
                expected_rst = (expected_rst + 1) & 7
                for component, _dc, _ac in scan:
                    component.predictor = 0
            for component, dc_table, ac_table in scan:
                table = quant[component.tq]
                block_h, block_v = (1, 1) if single_component else (component.h, component.v)
                for v in range(block_v):
                    block_y = (row * block_v + v) * 8
                    for h in range(block_h):
                        coeffs, component.predictor = _decode_block(
                            reader, dc_table, ac_table, table, component.predictor
                        )
                        _store_block(component, _idct(coeffs), (col * block_h + h) * 8, block_y)
            mcu_index += 1
    return _to_image(frame, adobe_transform)


__all__ = ["decode_jpeg"]
