"""纯 Python PNG 编解码（对应 Go 的 ``image/png``）。

解码支持：签名校验、IHDR/PLTE/tRNS/gAMA/IDAT（多块拼接）/IEND、
CRC32 可选严格校验、Adam7 隔行、位深 1/2/4/8/16、颜色类型 0/2/3/4/6、
滤波器 0-4 逐行还原。16 位源取高字节降到 8 位；调色板 + tRNS 保留为
``P`` 模式（含 alpha 语义），其余颜色类型映射到 ``L``/``LA``/``RGB``/``RGBA``。

编码：位深 8，颜色类型 0/2/3/4/6（跟随源模式），逐行在 filter 0（None）与
filter 1（Sub）之间按「最小绝对差之和」启发式取优，CRC32 正确，输出可被
Pillow/浏览器/libpng 直接读取。

不支持的：APNG 动画（只解 IDAT，``acTL`` 被忽略）、位深 16 的编码、
未知关键块（抛 :class:`UnsupportedImageError`）。
"""

from __future__ import annotations

import struct
import zlib

from .image import (
    DEFAULT_MAX_DECODE_BYTES,
    MAX_EDGE,
    PNG_DEPTHS,
    Image,
    ImageDecodeError,
    ImageTooLargeError,
    UnsupportedImageError,
    pixel_limit,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CHANNELS: dict[int, int] = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_MODE_BY_COLOR: dict[int, str] = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}
_COLOR_BY_MODE: dict[str, int] = {"L": 0, "RGB": 2, "P": 3, "LA": 4, "RGBA": 6}
#: Adam7 的七个通道：(x 起点, y 起点, x 步长, y 步长)。
_ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


# ---------------------------------------------------------------------------
# 滤波还原
# ---------------------------------------------------------------------------


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = p - a if p >= a else a - p
    pb = p - b if p >= b else b - p
    pc = p - c if p >= c else c - p
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _bytewise_add(left: int, right: int, mask: int) -> int:
    """按字节取模 256 相加（SWAR），用于加速 Up 滤波器。"""
    return ((left & mask) + (right & mask)) ^ ((left ^ right) & ~mask)


def _row_mask(row_bytes: int) -> int:
    """``_bytewise_add`` 用的字节掩码；``row_bytes < 8`` 时返回 0（走逐字节路径）。"""
    if row_bytes < 8:
        return 0
    return int.from_bytes(b"\x7f" * row_bytes, "big")


def _unfilter_row(
    line: bytearray, prev: bytearray, bpp: int, ftype: int, mask: int
) -> None:
    """就地还原一行滤波器 0-4；``prev`` 是上一行还原后的原始字节。"""
    row_bytes = len(line)
    if ftype == 0:
        return
    if ftype == 1:
        for i in range(bpp, row_bytes):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:
        if mask:
            value = _bytewise_add(
                int.from_bytes(line, "big"), int.from_bytes(prev, "big"), mask
            )
            line[:] = value.to_bytes(row_bytes, "big")
        else:
            for i in range(row_bytes):
                line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:
        for i in range(bpp):
            line[i] = (line[i] + (prev[i] >> 1)) & 0xFF
        for i in range(bpp, row_bytes):
            line[i] = (line[i] + ((line[i - bpp] + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(bpp):
            line[i] = (line[i] + prev[i]) & 0xFF
        for i in range(bpp, row_bytes):
            line[i] = (line[i] + _paeth(line[i - bpp], prev[i], prev[i - bpp])) & 0xFF
    else:
        raise ImageDecodeError(f"PNG 滤波器类型 {ftype} 无效")


class _RowFeeder:
    """按行增量解压 IDAT，避免把整幅解压后的原始缓冲同时留在内存里。

    旧实现一次性 ``decompress(expected + 1)`` 得到 ``height * row_bytes`` 的原始缓冲，
    再分配同样大的还原缓冲和输出缓冲，峰值约 2×原始 + 1×输出（16 位 RGBA 达
    20 字节/像素）。这里改成"要一行解一行"：同时存活的只有若干行 + 输出缓冲。
    """

    __slots__ = ("decomp", "idat", "pos", "inbuf", "pending", "produced", "expected", "done")

    def __init__(self, idat: bytes, expected: int) -> None:
        self.decomp = zlib.decompressobj()
        self.idat = idat
        self.pos = 0
        self.inbuf = b""
        self.pending = bytearray()
        self.produced = 0
        self.expected = expected
        self.done = False

    def take(self, count: int) -> bytearray | None:
        """取恰好 ``count`` 字节；数据不足返回 ``None``。"""
        pending = self.pending
        while len(pending) < count:
            if self.done:
                return None
            if self.inbuf:
                data = self.inbuf
            elif self.pos < len(self.idat):
                data = self.idat[self.pos : self.pos + 65536]
                self.pos += len(data)
            else:
                data = b""
            try:
                if data:
                    out = self.decomp.decompress(data, count - len(pending))
                    self.inbuf = self.decomp.unconsumed_tail
                else:
                    out = self.decomp.flush()
                    self.inbuf = b""
                    self.done = True
            except zlib.error as exc:
                raise ImageDecodeError(f"PNG IDAT 解压失败：{exc}") from exc
            if out:
                self.produced += len(out)
                if self.produced > self.expected:
                    raise ImageDecodeError("PNG 像素数据超出 IHDR 声明尺寸（疑似解压炸弹）")
                pending += out
            elif not data:
                return None
        row = bytearray(pending[:count])
        del pending[:count]
        return row


def _extract_row(row: bytes | bytearray, count: int, depth: int, color: int) -> bytearray:
    """把一行样本转成输出通道布局（8 位/样本）。"""
    if depth == 8:
        return bytearray(row)
    if depth == 16:
        return bytearray(row[0::2])  # 取高字节即 v >> 8
    # depth < 8 只可能出现在颜色类型 0/3
    out = bytearray(count)
    per_byte = 8 // depth
    max_value = (1 << depth) - 1
    scale = 255 // max_value if color == 0 else 1
    for i in range(count):
        shift = 8 - depth * ((i % per_byte) + 1)
        out[i] = ((row[i // per_byte] >> shift) & max_value) * scale
    return out


def _pass_geometry(width: int, height: int) -> list[tuple[int, int, int, int, int, int]]:
    """Adam7 七个通道中非空的那些：(x0, y0, dx, dy, pw, ph)。"""
    passes = []
    for x0, y0, dx, dy in _ADAM7:
        pw = (width - x0 + dx - 1) // dx if width > x0 else 0
        ph = (height - y0 + dy - 1) // dy if height > y0 else 0
        if pw > 0 and ph > 0:
            passes.append((x0, y0, dx, dy, pw, ph))
    return passes


def _expected_raw_size(width: int, height: int, channels: int, depth: int, interlace: int) -> int:
    """按 IHDR 推出的解压后字节数（用于解压炸弹上限）。"""
    bits = channels * depth

    def row_size(count: int) -> int:
        return (count * bits + 7) // 8

    if interlace == 0:
        return (row_size(width) + 1) * height
    total = 0
    for _x0, _y0, _dx, _dy, pw, ph in _pass_geometry(width, height):
        total += (row_size(pw) + 1) * ph
    return total


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------


def decode_png(
    data: bytes, *, max_pixels: int | None = None, strict_crc: bool = False
) -> Image:
    """解码 PNG 为 :class:`Image`；任何损坏/超限都抛受控异常。"""
    raw = data if isinstance(data, bytes) else bytes(data)
    if not raw.startswith(PNG_SIGNATURE):
        raise ImageDecodeError("不是 PNG（签名不符）")

    header: tuple[int, ...] | None = None
    palette: bytes | None = None
    transparency: bytes | None = None
    idat = bytearray()
    gamma: float | None = None
    crc_mismatches: list[bytes] = []
    pos = 8
    end = len(raw)
    while pos + 8 <= end:
        (length,) = struct.unpack_from(">I", raw, pos)
        kind = raw[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + length
        if data_end + 4 > end:
            raise ImageDecodeError(f"PNG 块 {kind!r} 长度 {length} 越界（文件截断）")
        chunk = raw[data_start:data_end]
        # 关键块（首字母大写）的 CRC 始终强制校验；辅助块默认只记录，strict_crc 才拒绝。
        (want,) = struct.unpack_from(">I", raw, data_end)
        got = zlib.crc32(chunk, zlib.crc32(kind)) & 0xFFFFFFFF
        if got != want:
            if strict_crc or (kind[0] & 0x20) == 0:
                raise ImageDecodeError(f"PNG 块 {kind!r} CRC 校验失败")
            crc_mismatches.append(kind)
        if kind == b"IHDR":
            if length != 13:
                raise ImageDecodeError(f"PNG IHDR 长度 {length} 应为 13")
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"tRNS":
            transparency = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"gAMA":
            if length == 4:
                gamma = struct.unpack(">I", chunk)[0] / 100000.0
        elif kind == b"IEND":
            break
        elif not kind[0] & 0x20:
            raise UnsupportedImageError(f"PNG 关键块 {kind!r} 不受支持")
        pos = data_end + 4

    if header is None:
        raise ImageDecodeError("PNG 缺少 IHDR 块")
    if not idat:
        raise ImageDecodeError("PNG 缺少 IDAT 块")
    width, height, depth, color, compression, filter_method, interlace = header
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"PNG 声明尺寸无效：{width}x{height}")
    if width > MAX_EDGE or height > MAX_EDGE:
        raise ImageTooLargeError(f"PNG 边长 {width}x{height} 超过 {MAX_EDGE} 上限")
    limit = pixel_limit(max_pixels)
    if width * height > limit:
        raise ImageTooLargeError(
            f"PNG {width}x{height}（{width * height} 像素）超过 {limit} 像素上限"
        )
    if color not in PNG_DEPTHS or depth not in PNG_DEPTHS[color]:
        raise ImageDecodeError(f"PNG 颜色类型 {color} / 位深 {depth} 组合非法")
    if compression != 0 or filter_method != 0:
        raise UnsupportedImageError("PNG 压缩/滤波方法只支持 0")
    if interlace not in (0, 1):
        raise UnsupportedImageError(f"PNG 隔行方式 {interlace} 不受支持")
    if color == 3 and (palette is None or len(palette) % 3 or len(palette) > 768):
        raise ImageDecodeError("PNG 调色板缺失或长度非法")

    channels = _CHANNELS[color]
    bpp = max(1, (channels * depth) // 8)
    out_row = width * channels
    output_bytes = height * out_row
    if output_bytes > DEFAULT_MAX_DECODE_BYTES:
        raise ImageTooLargeError(
            f"PNG {width}x{height} 解码输出约 {output_bytes} 字节，"
            f"超过 {DEFAULT_MAX_DECODE_BYTES} 字节内存预算"
        )
    expected = _expected_raw_size(width, height, channels, depth, interlace)
    feeder = _RowFeeder(bytes(idat), expected)

    if interlace == 0:
        row_bytes = (width * channels * depth + 7) // 8
        data = bytearray(output_bytes)
        mask = _row_mask(row_bytes)
        prev = bytearray(row_bytes)
        for y in range(height):
            row = feeder.take(row_bytes + 1)
            if row is None:
                raise ImageDecodeError(
                    f"PNG 像素数据截断：需要 {expected} 字节，只解出 {feeder.produced} 字节"
                )
            ftype = row[0]
            del row[0]
            _unfilter_row(row, prev, bpp, ftype, mask)
            if depth == 8:
                data[y * out_row : (y + 1) * out_row] = row
            else:
                data[y * out_row : (y + 1) * out_row] = _extract_row(
                    row, width, depth, color
                )
            prev = row
    else:
        data = bytearray(output_bytes)
        for x0, y0, dx, dy, pw, ph in _pass_geometry(width, height):
            row_bytes = (pw * channels * depth + 7) // 8
            pass_row = pw * channels
            mask = _row_mask(row_bytes)
            prev = bytearray(row_bytes)
            for py in range(ph):
                row = feeder.take(row_bytes + 1)
                if row is None:
                    raise ImageDecodeError(
                        f"PNG 像素数据截断：需要 {expected} 字节，只解出 {feeder.produced} 字节"
                    )
                ftype = row[0]
                del row[0]
                _unfilter_row(row, prev, bpp, ftype, mask)
                samples = row if depth == 8 else _extract_row(row, pw, depth, color)
                base = ((y0 + py * dy) * width + x0) * channels
                if dx == 1:
                    data[base : base + pass_row] = samples
                else:
                    step = channels * dx
                    for px in range(pw):
                        target = base + px * step
                        data[target : target + channels] = samples[
                            px * channels : (px + 1) * channels
                        ]
                prev = row
    # 尾随数据：解压出超过 IHDR 声明尺寸的字节即按解压炸弹拒绝（take 内部检查）。
    feeder.take(1)

    info: dict[str, object] = {}
    if gamma is not None:
        info["gamma"] = gamma
    if palette is not None:
        info["palette_entries"] = len(palette) // 3
    if crc_mismatches:
        info["crc_mismatches"] = [kind.decode("latin-1") for kind in crc_mismatches]
    mode = _MODE_BY_COLOR[color]
    return Image(
        width,
        height,
        mode,
        data,
        palette=palette if color == 3 else None,
        transparency=transparency if color == 3 else None,
        info=info,
    )


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF)
    )


def _filter_row(row: bytes | bytearray, bpp: int) -> tuple[int, bytearray]:
    """在 filter 0/1 中选绝对差之和更小者。"""
    plain = bytearray(row)
    cost0 = 0
    for value in plain:
        cost0 += value if value < 128 else 256 - value
    sub = bytearray(len(row))
    cost1 = 0
    for i in range(len(row)):
        value = (row[i] - (row[i - bpp] if i >= bpp else 0)) & 0xFF
        sub[i] = value
        cost1 += value if value < 128 else 256 - value
    if cost1 < cost0:
        return 1, sub
    return 0, plain


def encode_png(img: Image) -> bytes:
    """把 :class:`Image` 编码成 PNG（位深 8）。"""
    color = _COLOR_BY_MODE.get(img.mode)
    if color is None:
        raise UnsupportedImageError(f"PNG 编码不支持模式 {img.mode!r}")
    if img.width > MAX_EDGE or img.height > MAX_EDGE:
        raise ImageTooLargeError(f"PNG 编码边长 {img.width}x{img.height} 超上限")
    channels = _CHANNELS[color]
    row_bytes = img.width * channels
    raw = bytearray()
    data = img.data
    for y in range(img.height):
        start = y * row_bytes
        ftype, filtered = _filter_row(data[start : start + row_bytes], channels)
        raw.append(ftype)
        raw += filtered
    parts = [_chunk(b"IHDR", struct.pack(">IIBBBBB", img.width, img.height, 8, color, 0, 0, 0))]
    if color == 3:
        palette = img.palette or bytes(3)
        if len(palette) % 3 or len(palette) > 768 or not palette:
            raise ImageDecodeError("调色板长度非法，无法编码为 PNG")
        parts.append(_chunk(b"PLTE", bytes(palette)))
        if img.transparency is not None:
            alpha = bytes(img.transparency)
            if len(alpha) > len(palette) // 3:
                alpha = alpha[: len(palette) // 3]
            parts.append(_chunk(b"tRNS", alpha))
    parts.append(_chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
    parts.append(_chunk(b"IEND", b""))
    return PNG_SIGNATURE + b"".join(parts)


__all__ = ["PNG_SIGNATURE", "decode_png", "encode_png"]
