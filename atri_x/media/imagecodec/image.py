"""图像编解码公共契约：Image 容器、格式嗅探、后端分发与统一上限。

对应 Go：``image`` / ``image/jpeg`` / ``image/png``，被 ``internal/media/image.go``
的「解码 → 缩放 → 白底合成 → JPEG 重编码」链路消费。

设计要点：

* :class:`Image` 是紧凑像素缓冲（``bytearray``），模式限定为 ``RGB`` /
  ``RGBA`` / ``L`` / ``LA`` / ``P``；16 位源解码时降到 8 位（取高字节），
  CMYK 解码时转成 RGB，因此 ``decode().mode`` 只会是上面五种。
* :func:`sniff` 永远走纯 Python 头部解析：两种后端判定完全一致，并且是
  :func:`decode` 的统一前置闸门（格式白名单、边长/总像素上限）。
* Pillow 只是可选加速后端：``import PIL`` 失败即回落纯 Python（探测唯一出处是
  :mod:`atri_x.media.imagecodec.backend_probe`，doctor / ``/diagnostics`` 与打包
  脚本共用它的结论）；也可用环境变量
  ``ATRI_X_IMAGECODEC_BACKEND=pure|pillow|auto`` 或 :func:`set_backend` 强制。
* 纯后端只实现 PNG 与 baseline JPEG。GIF/WebP/BMP 能被 :func:`sniff` 正确
  识别，但没有 Pillow 时 :func:`decode` 抛 :class:`UnsupportedImageError`。
"""

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass
from typing import Any, Sequence

from ...errors import AtriXError

#: 单边最大像素数（PNG/JPEG 规范上限）。
MAX_EDGE = 65535
#: 解码后总像素上限，默认 24 MPix（解压炸弹防线，可被 decode 参数覆盖）。
#: 与视觉预处理上限（``media.image.VISION_IMAGE_MAX_INPUT_PIXELS``）对齐：纯 Python
#: 解码器的成本与峰值内存都按"像素数 × 字节/像素"增长，100 MPix 的旧上限允许
#: 合法文件就把进程打到 GiB 级（见研究报告 §3.3）。
DEFAULT_MAX_PIXELS = 24_000_000
#: 单次解码的内存预算（估算值：输出像素缓冲 + 解码器中间平面）。
#: 超过预算时抛 :class:`ImageTooLargeError`，而不是先分配再校验。
DEFAULT_MAX_DECODE_BYTES = 192 * 1024 * 1024

_MODE_CHANNELS: dict[str, int] = {"RGB": 3, "RGBA": 4, "L": 1, "LA": 2, "P": 1}
#: PNG 颜色类型 → 合法位深（sniff 与 png.py 共用）。
PNG_DEPTHS: dict[int, tuple[int, ...]] = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_PNG_MODE_BY_COLOR: dict[int, str] = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}
_FORMAT_MIME: dict[str, str] = {
    "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "unknown": "application/octet-stream",
}
_SOF_MARKERS = frozenset([0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF])


class ImageCodecError(AtriXError):
    """图像编解码错误基类。"""


class UnsupportedImageError(ImageCodecError):
    """格式已知但不受支持（渐进 JPEG、纯后端下的 GIF 等）。"""


class ImageDecodeError(ImageCodecError):
    """数据损坏、截断或不是图像。"""


class ImageEncodeError(ImageCodecError):
    """编码阶段失败（含压缩后仍超字节预算）。"""


class ImageTooLargeError(ImageDecodeError):
    """声明尺寸超过上限（解压炸弹）。"""


def pixel_limit(max_pixels: int | None) -> int:
    """归一化像素上限：``None`` 取默认值，非正数按调用方错误拒绝。

    ``max_pixels=0`` 过去等价于"关闭像素闸门"（合法文件都能过），属于静默
    fail-open；这里改为受控的 :class:`ValueError`，调用方必须显式给正整数。
    """
    if max_pixels is None:
        return DEFAULT_MAX_PIXELS
    if max_pixels <= 0:
        raise ValueError(f"max_pixels 必须是正整数，收到 {max_pixels!r}")
    return max_pixels


@dataclass
class ImageInfo:
    """头部嗅探结果（不含像素）。"""

    format: str
    width: int
    height: int
    mode: str
    mime_type: str
    has_alpha: bool
    animated: bool


_BACKEND_REQUEST = os.environ.get("ATRI_X_IMAGECODEC_BACKEND", "auto").strip().lower()
if _BACKEND_REQUEST not in ("auto", "pure", "pillow"):
    _BACKEND_REQUEST = "auto"


def _pil_image() -> Any:
    """``PIL.Image`` 模块；不可用时返回 ``None``。

    ``import PIL`` 的唯一实现在 :mod:`.backend_probe`（doctor / ``/diagnostics``
    与打包脚本看的是同一份探测结果）。这里**按需导入**：包 ``__init__`` 不预导入
    ``backend_probe``，``python -m ...backend_probe`` 才不会被 runpy 判成已导入。
    """
    from .backend_probe import pillow_module

    return pillow_module()


def backend() -> str:
    """当前生效的后端：``"pillow"`` 或 ``"pure"``。"""
    if _BACKEND_REQUEST == "pure":
        return "pure"
    if _BACKEND_REQUEST == "pillow":
        return "pillow" if _pil_image() is not None else "pure"
    return "pillow" if _pil_image() is not None else "pure"


def set_backend(name: str) -> str:
    """强制后端（``"auto"`` / ``"pure"`` / ``"pillow"``），返回实际生效值。

    请求 ``pillow`` 但 Pillow 不可用时静默回落 ``pure``（硬约束：无第三方依赖
    也必须能跑）。
    """
    global _BACKEND_REQUEST
    if name not in ("auto", "pure", "pillow"):
        raise ValueError(f"未知后端 {name!r}，只接受 auto/pure/pillow")
    _BACKEND_REQUEST = name
    return backend()


class Image:
    """紧凑像素缓冲。

    ``data`` 为 ``bytearray``，按行优先、通道连续排布：

    * ``RGB`` → 3 字节/像素，``RGBA`` → 4，``L`` → 1，``LA`` → 2，
    * ``P`` → 1 字节调色板索引，配色板 :attr:`palette`（``bytes``，RGB 三元组）
      与可选 :attr:`transparency`（``bytes``，每索引一个 alpha）。
    """

    __slots__ = ("width", "height", "mode", "data", "palette", "transparency", "info")

    def __init__(
        self,
        width: int,
        height: int,
        mode: str = "RGB",
        data: bytes | bytearray | None = None,
        *,
        palette: bytes | bytearray | None = None,
        transparency: bytes | bytearray | None = None,
        info: dict[str, Any] | None = None,
    ) -> None:
        if mode not in _MODE_CHANNELS:
            raise ImageCodecError(f"不支持的像素模式 {mode!r}")
        if width <= 0 or height <= 0:
            raise ImageCodecError(f"图像尺寸无效：{width}x{height}")
        size = width * height * _MODE_CHANNELS[mode]
        if data is None:
            buffer = bytearray(size)
        elif isinstance(data, bytearray):
            if len(data) != size:
                raise ImageCodecError(f"缓冲区长度 {len(data)} != {width}x{height} 所需 {size}")
            buffer = data
        else:
            raw = bytes(data)
            if len(raw) != size:
                raise ImageCodecError(f"缓冲区长度 {len(raw)} != {width}x{height} 所需 {size}")
            buffer = bytearray(raw)
        self.width = width
        self.height = height
        self.mode = mode
        self.data = buffer
        self.palette = bytes(palette) if palette is not None else None
        self.transparency = bytes(transparency) if transparency is not None else None
        self.info = dict(info) if info else {}

    # -- 基本属性 ---------------------------------------------------------

    @property
    def channels(self) -> int:
        return _MODE_CHANNELS[self.mode]

    @property
    def has_alpha(self) -> bool:
        if self.mode in ("RGBA", "LA"):
            return True
        return self.mode == "P" and self.transparency is not None

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    # -- 像素访问（测试友好） ---------------------------------------------

    def get_pixel(self, x: int, y: int) -> tuple[int, ...]:
        """返回该像素的通道元组；``P`` 返回 ``(索引,)``，越界抛 ImageCodecError。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ImageCodecError(f"像素坐标越界：({x}, {y}) 尺寸 {self.width}x{self.height}")
        step = _MODE_CHANNELS[self.mode]
        start = (y * self.width + x) * step
        return tuple(self.data[start : start + step])

    def put_pixel(self, x: int, y: int, value: int | Sequence[int]) -> None:
        """写入单个像素；``value`` 为整数或通道序列。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ImageCodecError(f"像素坐标越界：({x}, {y}) 尺寸 {self.width}x{self.height}")
        step = _MODE_CHANNELS[self.mode]
        if isinstance(value, int):
            values = [value] * step
        else:
            values = [int(v) for v in value]
        if len(values) != step:
            raise ImageCodecError(f"{self.mode} 需要 {step} 个通道，收到 {len(values)}")
        start = (y * self.width + x) * step
        for offset, item in enumerate(values):
            if not 0 <= item <= 255:
                raise ImageCodecError(f"通道值越界：{item}")
            self.data[start + offset] = item

    # -- 序列化 -----------------------------------------------------------

    def to_bytes(self) -> bytes:
        """返回像素缓冲的 ``bytes`` 副本。"""
        return bytes(self.data)

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray,
        width: int,
        height: int,
        mode: str = "RGB",
        *,
        palette: bytes | bytearray | None = None,
        transparency: bytes | bytearray | None = None,
    ) -> "Image":
        """从紧凑缓冲构造（校验长度）。"""
        return cls(width, height, mode, data, palette=palette, transparency=transparency)

    def copy(self) -> "Image":
        return Image(
            self.width,
            self.height,
            self.mode,
            bytearray(self.data),
            palette=self.palette,
            transparency=self.transparency,
            info=self.info,
        )

    # -- 模式转换 ---------------------------------------------------------

    def convert(self, mode: str) -> "Image":
        """在 ``RGB``/``RGBA``/``L``/``LA``/``P`` 之间转换（实现在 :mod:`ops`）。

        ``P`` 只能作为源（索引→真彩，含 tRNS alpha）；转成 ``P`` 需要量化，
        抛 :class:`UnsupportedImageError`。转 ``RGB``/``L`` 时 alpha 直接丢弃，
        白底合成请用 :func:`atri_x.media.imagecodec.to_rgb`。
        """
        from . import ops
        return ops.convert(self, mode)

    # -- 杂项 -------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Image):
            return NotImplemented
        return (
            self.width == other.width
            and self.height == other.height
            and self.mode == other.mode
            and self.data == other.data
            and self.palette == other.palette
            and self.transparency == other.transparency
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Image({self.width}x{self.height}, {self.mode})"


def _luma(r: int, g: int, b: int) -> int:
    """ITU-R BT.601 亮度（8 位入参；系数与 Go ``color.GrayModel`` 同一组）。"""
    return (19595 * r + 38470 * g + 7471 * b + (1 << 15)) >> 16


def sniff(data: bytes) -> ImageInfo:
    """只读头部判定格式/尺寸/模式，不解码任何像素。

    无法识别时返回 ``format="unknown"``；是图像但头部损坏时抛
    :class:`ImageDecodeError`。
    """
    raw = _as_bytes(data)
    if not raw:
        raise ImageDecodeError("图像数据为空")
    try:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return _sniff_png(raw)
        if raw.startswith(b"\xff\xd8"):
            return _sniff_jpeg(raw)
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return _sniff_gif(raw)
        if len(raw) >= 16 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return _sniff_webp(raw)
        if raw.startswith(b"BM"):
            return _sniff_bmp(raw)
    except struct.error as exc:  # 任何漏检的短读都收敛成受控异常
        raise ImageDecodeError(f"图像头部损坏：{exc}") from exc
    return ImageInfo("unknown", 0, 0, "unknown", _FORMAT_MIME["unknown"], False, False)


def _sniff_png(raw: bytes) -> ImageInfo:
    if len(raw) < 33 or raw[12:16] != b"IHDR":
        raise ImageDecodeError("PNG 缺少 IHDR 块")
    width, height, depth, color = struct.unpack_from(">IIBB", raw, 16)
    mode = _PNG_MODE_BY_COLOR.get(color)
    if mode is None:
        raise ImageDecodeError(f"PNG 颜色类型 {color} 无效")
    if depth not in PNG_DEPTHS[color]:
        raise ImageDecodeError(f"PNG 颜色类型 {color} 不允许位深 {depth}")
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"PNG 声明尺寸无效：{width}x{height}")
    has_alpha = color in (4, 6)
    animated = False
    # 只走块头，最多看 64 个块就停（tRNS/acTL 一定在 IDAT 之前）。
    pos, limit, seen = 8, len(raw), 0
    while pos + 8 <= limit and seen < 64:
        seen += 1
        (length,) = struct.unpack_from(">I", raw, pos)
        kind = raw[pos + 4 : pos + 8]
        if kind == b"tRNS":
            has_alpha = True
        elif kind == b"acTL":
            animated = True
        if kind in (b"IDAT", b"IEND"):
            break
        if pos + 12 + length > limit:
            break
        pos += 12 + length
    return ImageInfo("png", width, height, mode, "image/png", has_alpha, animated)


def _sniff_jpeg(raw: bytes) -> ImageInfo:
    end = len(raw)
    pos = 2
    info: ImageInfo | None = None
    while pos + 4 <= end:
        if raw[pos] != 0xFF:
            raise ImageDecodeError("JPEG 段标记损坏")
        while pos < end and raw[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = raw[pos]
        pos += 1
        if marker == 0x00 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        (length,) = struct.unpack_from(">H", raw, pos)
        if length < 2 or pos + length > end:
            raise ImageDecodeError("JPEG 段长度越界")
        if marker in _SOF_MARKERS:
            if info is not None:
                # 尺寸闸门与解码器必须看到同一个 SOF；多个帧头说明尺寸有歧义，
                # 一律拒绝（旧实现 sniff 取第一个、解码器取最后一个 → 可绕闸门）。
                raise ImageDecodeError("JPEG 出现多个 SOF 帧头（尺寸有歧义，拒绝）")
            if length < 8:
                raise ImageDecodeError("JPEG SOF 段过短")
            precision, height, width, components = struct.unpack_from(">BHHB", raw, pos + 2)
            if precision not in (8, 12, 16) or width <= 0 or height <= 0:
                raise ImageDecodeError(f"JPEG 声明无效：{width}x{height} 精度 {precision}")
            mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(components, "unknown")
            info = ImageInfo("jpeg", width, height, mode, "image/jpeg", False, False)
        elif marker == 0xDA:
            if info is None:
                raise ImageDecodeError("JPEG 在 SOS 之前没有 SOF 段")
            return info
        pos += length
    if info is not None:
        # SOF 已就绪但文件在 SOS 前截断：仍能给出尺寸（解码阶段会报截断）。
        return info
    raise ImageDecodeError("JPEG 未找到 SOF 段")


def _gif_skip_subblocks(raw: bytes, pos: int, end: int) -> int:
    while pos < end:
        size = raw[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    return pos


def _sniff_gif(raw: bytes) -> ImageInfo:
    if len(raw) < 13:
        raise ImageDecodeError("GIF 头部截断")
    width, height = struct.unpack_from("<HH", raw, 6)
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"GIF 声明尺寸无效：{width}x{height}")
    end = min(len(raw), 1 << 16)
    packed = raw[10]
    pos = 13
    if packed & 0x80:
        pos += 3 * (1 << ((packed & 0x07) + 1))
    has_alpha = False
    frames = 0
    while pos < end:
        block = raw[pos]
        if block == 0x3B:
            break
        if block == 0x21:
            if pos + 2 > end:
                break
            if raw[pos + 1] == 0xF9 and pos + 4 <= end and raw[pos + 2] >= 4:
                if raw[pos + 3] & 0x01:
                    has_alpha = True
            pos = _gif_skip_subblocks(raw, pos + 2, end)
        elif block == 0x2C:
            frames += 1
            if pos + 10 > end:
                break
            local = raw[pos + 9]
            pos += 10
            if local & 0x80:
                pos += 3 * (1 << ((local & 0x07) + 1))
            pos = _gif_skip_subblocks(raw, pos + 1, end)
        else:
            break
    return ImageInfo("gif", width, height, "P", "image/gif", has_alpha, frames > 1)


def _sniff_webp(raw: bytes) -> ImageInfo:
    if len(raw) < 30:
        raise ImageDecodeError("WebP 头部截断")
    fourcc = raw[12:16]
    has_alpha = False
    animated = False
    if fourcc == b"VP8X":
        flags = raw[20]
        width = 1 + int.from_bytes(raw[24:27], "little")
        height = 1 + int.from_bytes(raw[27:30], "little")
        has_alpha = bool(flags & 0x10)
        animated = bool(flags & 0x02)
    elif fourcc == b"VP8L":
        if raw[20] != 0x2F:
            raise ImageDecodeError("WebP VP8L 签名无效")
        bits = int.from_bytes(raw[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        has_alpha = bool((bits >> 28) & 1)
    elif fourcc == b"VP8 ":
        if raw[23:26] != b"\x9d\x01\x2a":
            raise ImageDecodeError("WebP VP8 帧头无效")
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
    else:
        raise ImageDecodeError(f"WebP 变体 {fourcc!r} 无法识别")
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"WebP 声明尺寸无效：{width}x{height}")
    mode = "RGBA" if has_alpha else "RGB"
    return ImageInfo("webp", width, height, mode, "image/webp", has_alpha, animated)


def _sniff_bmp(raw: bytes) -> ImageInfo:
    if len(raw) < 26:
        raise ImageDecodeError("BMP 头部截断")
    header = int.from_bytes(raw[14:18], "little")
    if header == 12:
        if len(raw) < 26:
            raise ImageDecodeError("BMP CORE 头截断")
        width = int.from_bytes(raw[18:20], "little")
        height = int.from_bytes(raw[20:22], "little")
        bits = int.from_bytes(raw[24:26], "little")
    elif header >= 40:
        if len(raw) < 30:
            raise ImageDecodeError("BMP INFO 头截断")
        width = int.from_bytes(raw[18:22], "little", signed=True)
        height = int.from_bytes(raw[22:26], "little", signed=True)
        bits = int.from_bytes(raw[28:30], "little")
    else:
        raise ImageDecodeError(f"BMP DIB 头 {header} 无法识别")
    if width <= 0 or height == 0:
        raise ImageDecodeError(f"BMP 声明尺寸无效：{width}x{height}")
    mode = {1: "P", 2: "P", 4: "P", 8: "P", 16: "RGB", 24: "RGB", 32: "RGBA"}.get(bits)
    if mode is None:
        raise ImageDecodeError(f"BMP 位深 {bits} 不支持")
    return ImageInfo("bmp", width, abs(height), mode, "image/bmp", bits == 32, False)


def _as_bytes(data: bytes | bytearray | memoryview) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"需要 bytes/bytearray/memoryview，收到 {type(data).__name__}")


def _check_limits(info: ImageInfo, max_pixels: int | None) -> None:
    if info.width > MAX_EDGE or info.height > MAX_EDGE:
        raise ImageTooLargeError(
            f"图像边长 {info.width}x{info.height} 超过 {MAX_EDGE} 上限"
        )
    limit = pixel_limit(max_pixels)
    pixels = info.width * info.height
    if pixels > limit:
        raise ImageTooLargeError(
            f"图像 {info.width}x{info.height}（{pixels} 像素）超过 {limit} 像素上限"
        )


def decode(
    data: bytes, *, max_pixels: int | None = None, strict_crc: bool = False
) -> Image:
    """解码为像素缓冲；Pillow 可用时走加速路径，否则纯 Python。

    受支持的格式：PNG（全位深/隔行/调色板，纯 Python）与 baseline JPEG
    （纯 Python）。GIF/WebP/BMP 只有 Pillow 后端能解码。

    ``strict_crc=True`` 时 PNG 校验全部块的 CRC（默认只强制关键块
    IHDR/PLTE/IDAT/IEND；辅助块损坏记录在 ``Image.info["crc_mismatches"]``）。
    为保证 CRC 真的被校验，该参数会绕过 Pillow 直接走纯解码器。
    """
    raw = _as_bytes(data)
    if not raw:
        raise ImageDecodeError("图像数据为空")
    info = sniff(raw)
    if info.format == "unknown":
        raise UnsupportedImageError("无法识别的图像格式（无 Pillow 时仅支持 PNG/JPEG）")
    _check_limits(info, max_pixels)
    if backend() == "pillow" and not (strict_crc and info.format == "png"):
        image = _pil_decode(raw)
        if image is not None:
            return image
    return _pure_decode(raw, info, max_pixels, strict_crc)


def _pure_decode(
    raw: bytes, info: ImageInfo, max_pixels: int | None, strict_crc: bool = False
) -> Image:
    from . import jpeg_decode, png

    if info.format == "png":
        return png.decode_png(raw, max_pixels=max_pixels, strict_crc=strict_crc)
    if info.format == "jpeg":
        return jpeg_decode.decode_jpeg(raw, max_pixels=max_pixels)
    raise UnsupportedImageError(
        f"纯 Python 后端不支持 {info.format} 解码（安装 Pillow 后可解码）"
    )


def _pil_decode(raw: bytes) -> Image | None:
    """Pillow 解码；对 16 位源返回 ``None`` 交由纯解码器（保证取值约定一致）。"""
    pil = _pil_image()
    if pil is None:
        return None
    try:
        with pil.open(io.BytesIO(raw)) as handle:
            handle.seek(0)
            if handle.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N", "F"):
                return None
            return _pil_to_image(handle)
    except (ImageCodecError, MemoryError):
        raise
    except Exception as exc:  # Pillow 的异常族全部收敛
        raise ImageDecodeError(f"Pillow 解码失败：{exc}") from exc


def _pil_to_image(handle: Any) -> Image:
    """PIL 图像 → :class:`Image`（模式归一化到本包的词汇表）。"""
    source_mode = handle.mode
    width, height = handle.size
    if source_mode == "P":
        palette = handle.getpalette() or []
        transparency = handle.info.get("transparency")
        if isinstance(transparency, int):
            transparency = bytes([transparency])
        elif transparency is not None:
            transparency = bytes(transparency)
        return Image(
            width,
            height,
            "P",
            bytearray(handle.tobytes()),
            palette=bytes(palette) if palette else None,
            transparency=transparency,
        )
    target = _PIL_MODE_MAP.get(source_mode)
    if target is None:
        handle = handle.convert("RGBA")
        target = "RGBA"
    elif target != source_mode:
        handle = handle.convert(target)
    return Image.from_bytes(handle.tobytes(), width, height, target)


_PIL_MODE_MAP: dict[str, str] = {
    "1": "L",
    "L": "L",
    "LA": "LA",
    "P": "P",
    "RGB": "RGB",
    "RGBX": "RGB",
    "RGBa": "RGBA",
    "RGBA": "RGBA",
    "PA": "RGBA",
    "CMYK": "RGB",
    "YCbCr": "RGB",
    "LAB": "RGB",
    "HSV": "RGB",
}


def _pil_from_image(img: Image) -> Any:
    pil = _pil_image()
    if img.mode == "P":
        handle = pil.frombytes("P", (img.width, img.height), img.to_bytes())
        palette = img.palette or bytes(768)
        if len(palette) < 768:
            palette = palette + bytes(768 - len(palette))
        handle.putpalette(palette[:768])
        if img.transparency is not None:
            handle.info["transparency"] = img.transparency
        return handle
    return pil.frombytes(img.mode, (img.width, img.height), img.to_bytes())


def encode_jpeg(img: Image, quality: int = 85) -> bytes:
    """编码为 baseline JPEG（4:2:0；``L`` 模式编成灰度）。

    带 alpha 的图先做白底合成（JPEG 没有 alpha 通道），调色板模式先展开成
    RGB；两条后端共用这一步，保证行为一致。``quality`` 取 1..100，越界会抛
    :class:`ImageCodecError`。
    """
    if not 1 <= quality <= 100:
        raise ImageCodecError(f"JPEG 质量 {quality} 越界（1..100）")
    if img.mode not in ("L", "RGB"):
        img = to_rgb(img)
    if backend() == "pillow":
        pil = _pil_image()
        try:
            handle = _pil_from_image(img)
            buffer = io.BytesIO()
            if img.mode == "L":
                handle.save(buffer, format="JPEG", quality=quality)
            else:
                handle.convert("RGB").save(
                    buffer, format="JPEG", quality=quality, subsampling=2
                )
            return buffer.getvalue()
        except Exception as exc:  # pragma: no cover - Pillow 极少失败
            raise ImageEncodeError(f"Pillow JPEG 编码失败：{exc}") from exc
    from . import jpeg_encode

    return jpeg_encode.encode_jpeg(img, quality)


def encode_png(img: Image) -> bytes:
    """编码为 PNG（位深 8；``P`` 保留调色板与 tRNS）。"""
    if backend() == "pillow":
        pil = _pil_image()
        try:
            handle = _pil_from_image(img)
            buffer = io.BytesIO()
            handle.save(buffer, format="PNG", optimize=False)
            return buffer.getvalue()
        except Exception as exc:  # pragma: no cover
            raise ImageEncodeError(f"Pillow PNG 编码失败：{exc}") from exc
    from . import png

    return png.encode_png(img)


def resize(img: Image, width: int, height: int, *, filter: str = "lanczos") -> Image:
    """缩放到 ``width`` x ``height``，**不放大**（逐轴钳到源尺寸）。"""
    from . import resample
    return resample.resize_image(img, width, height, filter=filter)


def resize_to_fit(img: Image, max_width: int, max_height: int) -> Image:
    """按宽高比缩放到不超过 ``max_width`` x ``max_height``（不放大）。"""
    from . import ops
    return ops.resize_to_fit(img, max_width, max_height)


def to_rgb(img: Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image:
    """白底合成去掉 alpha，返回 ``RGB`` 图像。"""
    from . import ops
    return ops.to_rgb(img, background)


def compress(
    data: bytes, *, max_bytes: int = 0, max_edge: int = 0, quality: int = 85
) -> tuple[bytes, bool]:
    """超阈值/边长就转白底 JPEG；返回 ``(新字节, 是否压缩过)``。

    未超限时原样返回入参 ``bytes`` 与 ``False``。实现见 :mod:`ops`。
    """
    from . import ops
    return ops.compress(data, max_bytes=max_bytes, max_edge=max_edge, quality=quality)


__all__ = [
    "DEFAULT_MAX_DECODE_BYTES",
    "DEFAULT_MAX_PIXELS",
    "MAX_EDGE",
    "PNG_DEPTHS",
    "Image",
    "ImageCodecError",
    "ImageDecodeError",
    "ImageEncodeError",
    "ImageInfo",
    "ImageTooLargeError",
    "UnsupportedImageError",
    "backend",
    "compress",
    "decode",
    "encode_jpeg",
    "encode_png",
    "pixel_limit",
    "resize",
    "resize_to_fit",
    "set_backend",
    "sniff",
    "to_rgb",
]
