"""导入期图片压缩与视觉预处理。

对应 Go：``internal/media/image.go``。

职责分两块：

1. **导入期压缩**（``compressImage*`` / ``resizeImage`` / ``shouldCompressImage`` /
   ``importedImage`` / ``compressedName``）—— 超字节阈值或边长达到限制的图片被
   重编码成**白底 JPEG**，只保存压缩内容。
2. **视觉预处理**（``PrepareVisionImageContext`` 及其常量）—— 交给视觉模型的图片
   必须同时满足边长与字节预算；本地解码器不认识的格式只在**已经**满足字节预算时
   原样透传。

实现全部落在 :mod:`atri_x.media.imagecodec`（纯 Python PNG/baseline JPEG 编解码与
重采样，可选 Pillow 加速），本模块只负责 Go 的**判定与循环策略**：

* 质量阶梯固定 ``92 → 35``、步长 5；之后画布每轮 ``×0.85``，并再夹到
  ``max_length - 1`` 以内（与 Go 逐字一致，因此测试可以断言具体尺寸）。
* 白底合成与重采样顺序与 Go 略有不同：Go 先对 NRGBA 采样、再逐像素合成白底；
  本仓先 :func:`imagecodec.to_rgb` 再重采样。对不含 alpha 的图（绝大多数）两条
  路径一致；含 alpha 时边界像素最多差 1 个色阶。
* 重采样核用 ``bilinear``。Go 用的是同映射（``(x+0.5)*src/dst-0.5``）的双线性，
  但边缘不做核缩放；``imagecodec`` 的 bilinear 在降采样时会按缩放比放大支撑
  （抗混叠），因此缩小后的图**更平滑**，像素值可能有小幅差异。
"""

from __future__ import annotations

from typing import Any

from atri_x.media.config import MediaConfig
from atri_x.media.imagecodec import (
    Image,
    ImageCodecError,
    ImageDecodeError,
    UnsupportedImageError,
    decode,
    encode_jpeg,
    resize,
    sniff,
    to_rgb,
)
from atri_x.media.mimetype import detect_content_type, extension_of
from atri_x.media.sanitize import sanitize_media_name
from atri_x.media.types import MediaError, raise_if_cancelled
from atri_x.media.worker import run_isolated

__all__ = [
    "MEDIA_IMAGE_MAX_DECODE_PIXELS",
    "VISION_IMAGE_DEFAULT_BYTE_BUDGET",
    "VISION_IMAGE_HARD_LIMIT",
    "VISION_IMAGE_MAX_CONCURRENT_DECODES",
    "VISION_IMAGE_MAX_INPUT_EDGE",
    "VISION_IMAGE_MAX_INPUT_PIXELS",
    "VisionImageInputLimit",
    "compress_image",
    "compress_image_context",
    "compress_image_isolated",
    "compress_vision_image",
    "compressed_name",
    "encode_jpeg",
    "image_dimensions",
    "imported_image",
    "prepare_vision_image",
    "prepare_vision_image_context",
    "resize_image",
    "should_compress_image",
    "verify_vision_image",
    "vision_image_mime_type",
]

#: ``PrepareVisionImage`` 愿意看的最多图像数据。对应 Go ``visionImageHardLimit``。
VISION_IMAGE_HARD_LIMIT = 64 * 1024 * 1024
#: 调用方没给 ``max_image_bytes`` 时重新编码载荷的预算。对应 Go
#: ``visionImageDefaultByteBudget``。
VISION_IMAGE_DEFAULT_BYTE_BUDGET = 12 * 1024 * 1024
#: 愿意解码的原始边长 / 像素上限。对应 Go ``visionImageMaxInputEdge`` /
#: ``visionImageMaxInputPixels``。
VISION_IMAGE_MAX_INPUT_EDGE = 20000
VISION_IMAGE_MAX_INPUT_PIXELS = 24000000
#: 导入期压缩（``_compress_image``）显式传给解码器的像素上限。
#: 与视觉预处理对齐：纯 Python 解码/重编码成本按像素线性增长，不给闸门时
#: 一张合法大图就能吃爆内存并超过 60 秒工作线程超时（研究报告 §3.3 问题 5）。
MEDIA_IMAGE_MAX_DECODE_PIXELS = VISION_IMAGE_MAX_INPUT_PIXELS
#: 同时在解码的位图数量上限。对应 Go ``visionImageMaxConcurrentDecodes``。
VISION_IMAGE_MAX_CONCURRENT_DECODES = 10
#: ``image_to_prompt`` 愿意物化的最大已存对象。对应 Go ``VisionImageInputLimit``。
VisionImageInputLimit = VISION_IMAGE_HARD_LIMIT


# ---------------------------------------------------------------------------
# 尺寸
# ---------------------------------------------------------------------------


def image_dimensions(data: bytes) -> tuple[int, int]:
    """只读头部的图像尺寸。对应 Go ``imageDimensions``（``image.DecodeConfig``）。

    与 Go 一样，**不做像素解码**：因此一个声明了巨大画布的坏图也能在这里被廉价地
    拦下来。无法识别或头部损坏时抛 :class:`atri_x.media.types.MediaError`。
    """
    try:
        info = sniff(data)
    except (ImageDecodeError, ImageCodecError) as exc:
        raise MediaError(f"decode image dimensions: {exc}") from exc
    if info.format == "unknown" or info.width <= 0 or info.height <= 0:
        raise MediaError("decode image dimensions: unrecognized image format")
    return int(info.width), int(info.height)


def _safe_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        return image_dimensions(data)
    except MediaError:
        return None


# ---------------------------------------------------------------------------
# 压缩
# ---------------------------------------------------------------------------


def compress_image(data: bytes, max_bytes: int, max_length: int) -> bytes:
    """把图片压成满足预算的白底 JPEG。对应 Go ``compressImage``。"""
    return _compress_image(None, data, max_bytes, max_length)


def compress_image_context(
    ctx: Any, data: bytes, max_bytes: int, max_length: int
) -> bytes:
    """带协作取消的 :func:`compress_image`。对应 Go ``compressImageContext``。"""
    return _compress_image(ctx, data, max_bytes, max_length)


async def compress_image_isolated(
    ctx: Any, data: bytes, max_bytes: int, max_length: int
) -> bytes:
    """在工作线程里压缩，超时 60 秒。对应 Go ``compressImageIsolated``。

    Go 用"重新 exec 自己 + 魔法参数"起子进程，好处是画布巨大或病态输入不会拖垮
    主进程，取消时能整棵进程树杀掉。本仓的取舍见 :mod:`atri_x.media.worker`。
    """
    return await run_isolated(ctx, _compress_image, ctx, data, max_bytes, max_length)


def _compress_image(ctx: Any, data: bytes, max_bytes: int, max_length: int) -> bytes:
    if max_bytes <= 1 or max_length <= 1:
        raise MediaError("image compression limits are too small")
    try:
        source = decode(data, max_pixels=MEDIA_IMAGE_MAX_DECODE_PIXELS)
    except (ImageDecodeError, UnsupportedImageError, ImageCodecError) as exc:
        raise MediaError(f"decode image: {exc}") from exc

    width, height = source.width, source.height
    max_dimension = max_length - 1
    if width > max_dimension or height > max_dimension:
        width, height = _scale_to_edge(width, height, max_dimension)

    while True:
        raise_if_cancelled(ctx)
        candidate = resize_image(source, width, height)
        for quality in range(92, 34, -5):
            raise_if_cancelled(ctx)
            encoded = encode_jpeg(candidate, quality)
            if len(encoded) < max_bytes:
                return encoded
        if width == 1 and height == 1:
            break
        width = max(1, int(width * 0.85))
        height = max(1, int(height * 0.85))
        if width > max_dimension or height > max_dimension:
            width, height = _scale_to_edge(width, height, max_dimension)
    raise MediaError(f"compressed image still exceeds {max_bytes} bytes")


def _scale_to_edge(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    """按长边夹到 ``max_dimension``（Go 的 ``int(float64(w)*scale)`` 同取整）。"""
    longest = max(width, height)
    if longest <= max_dimension:
        return width, height
    scale = max_dimension / float(longest)
    return max(1, int(width * scale)), max(1, int(height * scale))


def resize_image(source: Image, width: int, height: int) -> Image:
    """缩放并白底合成。对应 Go ``resizeImage``（见模块 docstring 的顺序差异）。"""
    return resize(to_rgb(source, (255, 255, 255)), width, height, filter="bilinear")


async def compress_vision_image(
    ctx: Any, data: bytes, max_bytes: int, max_length: int
) -> bytes:
    """串行化的视觉压缩。对应 Go ``compressVisionImage``。

    Go 用 ``visionImageDecodeSlots``（10 个槽）限制同时在解码的位图数量；本仓
    复用 :mod:`atri_x.media.worker` 的同一道闸门（同一常量），并把 CPU 密集的
    压缩放到工作线程里，避免卡住事件循环。
    """
    return await run_isolated(
        ctx, _compress_image, ctx, data, max_bytes, max_length, timeout=None
    )


# ---------------------------------------------------------------------------
# 导入期判定
# ---------------------------------------------------------------------------


def imported_image(mime_type: str, data: bytes) -> bool:
    """一次导入是否按图片处理。对应 Go ``importedImage``。

    声明的类型优先，探测到的类型用来兜住缺失/错误的扩展名。
    """
    if (mime_type or "").lower().startswith("image/"):
        return True
    return detect_content_type(data).startswith("image/")


def should_compress_image(data: bytes, cfg: MediaConfig) -> bool:
    """是否需要在导入时压缩。对应 Go ``shouldCompressImage``。

    注意与 :func:`prepare_vision_image` 的差别：这里是 ``>=`` 边长判定，
    那边是严格 ``>``，两者都对上游行为逐字对齐。
    """
    if len(data) > cfg.llm_image_compression_threshold_bytes:
        return True
    dimensions = _safe_dimensions(data)
    if dimensions is None:
        return False
    return dimensions[0] >= cfg.llm_image_max_length or dimensions[1] >= cfg.llm_image_max_length


def compressed_name(name: str) -> str:
    """压缩后的文件名（扩展名换成 ``.jpg``）。对应 Go ``compressedName``。"""
    safe = sanitize_media_name(name)
    extension = extension_of(safe)
    if not extension:
        return safe + ".jpg"
    return safe[: -len(extension)] + ".jpg"


# ---------------------------------------------------------------------------
# 视觉预处理
# ---------------------------------------------------------------------------


def prepare_vision_image(
    data: bytes, mime_type: str, max_edge: int, max_bytes: int
) -> tuple[bytes, str]:
    """``prepare_vision_image_context`` 的无 ctx 版本。对应 Go ``PrepareVisionImage``。"""
    return _prepare_vision_image(None, data, mime_type, max_edge, max_bytes)


def prepare_vision_image_context(
    ctx: Any, data: bytes, mime_type: str, max_edge: int, max_bytes: int
) -> tuple[bytes, str]:
    """返回可直接上传给视觉模型的 ``(字节, MIME)``。

    对应 Go ``PrepareVisionImageContext``。保证（本地解码器认识的格式）：

    * ``max_edge > 0`` 时长边 ``<= max_edge``；
    * ``max_bytes > 0`` 时字节数 ``<= max_bytes``（否则用
      :data:`VISION_IMAGE_DEFAULT_BYTE_BUDGET`）。

    本地解码器不认识的载荷无法度量、也无法缩小，因此只在**已经**满足字节预算时
    原样透传；超预算的不透明 blob 在本地拒绝，不上传。

    已经合规的输入原样返回（保留原格式）；否则降采样并重编码为 JPEG，
    透明像素合成到白底，动图只取首帧。
    """
    return _prepare_vision_image(ctx, data, mime_type, max_edge, max_bytes)


def _prepare_vision_image(
    ctx: Any, data: bytes, mime_type: str, max_edge: int, max_bytes: int
) -> tuple[bytes, str]:
    if not data:
        raise MediaError("image is empty")
    if len(data) > VISION_IMAGE_HARD_LIMIT:
        raise MediaError(
            f"image is {len(data)} bytes, exceeds the {VISION_IMAGE_HARD_LIMIT} byte decode limit"
        )
    mime_type = vision_image_mime_type(data, mime_type)
    dimensions = _safe_dimensions(data)
    if dimensions is None:
        if max_bytes > 0 and len(data) > max_bytes:
            raise MediaError(
                "image format is not supported for local processing and the payload is "
                f"{len(data)} bytes, over the {max_bytes} byte limit"
            )
        return data, mime_type
    width, height = dimensions
    if width <= 0 or height <= 0:
        raise MediaError(f"image declares invalid dimensions {width}x{height}")
    if width > VISION_IMAGE_MAX_INPUT_EDGE or height > VISION_IMAGE_MAX_INPUT_EDGE:
        raise MediaError(
            f"image edge {width}x{height} exceeds the {VISION_IMAGE_MAX_INPUT_EDGE} px decode limit"
        )
    pixels = width * height
    if pixels > VISION_IMAGE_MAX_INPUT_PIXELS:
        raise MediaError(
            f"image is {width}x{height} ({pixels} pixels), over the "
            f"{VISION_IMAGE_MAX_INPUT_PIXELS} pixel decode limit"
        )
    long_edge = max(width, height)
    over_edge = max_edge > 0 and long_edge > max_edge
    over_bytes = max_bytes > 0 and len(data) > max_bytes
    if not over_edge and not over_bytes:
        return data, mime_type
    raise_if_cancelled(ctx)
    limit = max_edge if max_edge > 0 else long_edge
    bytes_limit = max_bytes if max_bytes > 0 else VISION_IMAGE_DEFAULT_BYTE_BUDGET
    encoded = _compress_image(ctx, data, bytes_limit, limit)
    verify_vision_image(encoded, max_edge, bytes_limit)
    return encoded, "image/jpeg"


def vision_image_mime_type(data: bytes, declared: str) -> str:
    """优先相信字节本身，其次才是声明的类型。对应 Go ``visionImageMIMEType``。

    这样扩展名或过期元数据就没法把 JPEG 字节和 ``image/png`` 配在一起上传。
    非图片探测结果回落到声明类型。
    """
    declared = _strip_parameters(declared or "")
    detected = _strip_parameters(detect_content_type(data))
    if detected.startswith("image/") and detected.lower() != declared.lower():
        return detected
    return declared or detected


def _strip_parameters(value: str) -> str:
    index = value.find(";")
    return (value[:index] if index >= 0 else value).strip()


def verify_vision_image(data: bytes, max_edge: int, max_bytes: int) -> None:
    """复核输出契约。对应 Go ``verifyVisionImage``。

    让未来某次回归**显式失败**，而不是悄悄发出一张超预算的图。
    """
    try:
        width, height = image_dimensions(data)
    except MediaError as exc:
        raise MediaError(f"verify processed image: {exc}") from exc
    if max_edge > 0:
        longest = max(width, height)
        if longest > max_edge:
            raise MediaError(f"processed image long edge {longest} exceeds max_edge {max_edge}")
    if max_bytes > 0 and len(data) > max_bytes:
        raise MediaError(
            f"processed image is {len(data)} bytes, over max_image_bytes {max_bytes}"
        )
