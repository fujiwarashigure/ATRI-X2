"""图像编解码包：纯 Python 实现 PNG / baseline JPEG，Pillow 为可选加速后端。

对应 Go：``image`` + ``image/jpeg`` + ``image/png``（``internal/media/image.go``
的解码-缩放-白底合成-重编码链路）。对外契约在 :mod:`.image` 里，本模块统一
再导出：``ImageInfo`` / ``sniff`` / ``decode`` / ``encode_jpeg`` / ``encode_png``
/ ``to_rgb`` / ``resize`` / ``resize_to_fit`` / ``compress`` / ``backend``。
"""

from __future__ import annotations

from .image import (
    DEFAULT_MAX_PIXELS,
    MAX_EDGE,
    Image,
    ImageCodecError,
    ImageDecodeError,
    ImageEncodeError,
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

__all__ = [
    "DEFAULT_MAX_PIXELS",
    "MAX_EDGE",
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
    "resize",
    "resize_to_fit",
    "set_backend",
    "sniff",
    "to_rgb",
]
