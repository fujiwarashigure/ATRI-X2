"""媒体 MIME 判定：内容嗅探与扩展名映射。

Python 标准库**没有** Go ``net/http.DetectContentType`` 与
``mime.TypeByExtension``，所以这里补两个最小实现，供
:mod:`atri_x.media.manager`（导入时的 MIME 推断）与
:mod:`atri_x.media.image`（视觉预处理的 MIME 纠正）使用。

对应 Go 能力：``net/http.DetectContentType``（WHATWG 嗅探算法里 media 包真正用到
的那部分）与 ``mime.TypeByExtension``（Go 内置表）。

与 Go 的差异：

* 嗅探只覆盖媒体中心会遇到的签名（PNG/JPEG/GIF/WebP/BMP/ICO/PDF/gzip/zip/
  mpeg/ogg/wav/webm/mp4），其余回落到 Go 也用到的 "文本 vs 二进制" 兜底；
  Go 的表更长，但判成 ``image/*`` 的那些签名都覆盖到了。
* 扩展名表取 Go 内置表的常用子集（Go 在 Windows 上还会查注册表，本仓不查，
  以免测试结果依赖宿主环境）。
* Go 的文本兜底对 ``text/plain`` 追加 ``; charset=utf-8``；本仓同样保留这个
  后缀，因为它在 Go 侧会原样落进 ``media.mime_type``。
"""

from __future__ import annotations

import os

__all__ = ["detect_content_type", "mime_type_by_extension"]

#: Go mime 内置表里 media 相关且会被用到的部分（小写扩展名）。
_BY_EXTENSION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jfif": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/vnd.microsoft.icon",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".xml": "text/xml; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tar": "application/x-tar",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

_SNIFFERS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"\x00\x00\x02\x00", "image/x-icon"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b\x08", "application/x-gzip"),
    (b"ID3", "audio/mpeg"),
    (b"OggS", "application/ogg"),
    (b"RIFF", ""),  # 需要看 offset 8 的子类型，下面单独处理
    (b"fLaC", "audio/flac"),
)

#: RIFF 容器的子类型（offset 8..12）。
_RIFF_TYPES = {
    b"WAVE": "audio/wav",
    b"AVI ": "video/x-msvideo",
    b"WEBP": "image/webp",
}


def mime_type_by_extension(extension: str) -> str:
    """对应 Go ``mime.TypeByExtension``；未收录时返回空串。"""
    if not extension:
        return ""
    return _BY_EXTENSION.get(extension.lower(), "")


def detect_content_type(data: bytes) -> str:
    """对应 Go ``http.DetectContentType``（媒体中心用到的子集）。"""
    if not data:
        return "text/plain; charset=utf-8"
    head = bytes(data[:512])
    for signature, mime in _SNIFFERS:
        if signature == b"RIFF":
            continue
        if head.startswith(signature):
            return mime
    if head.startswith(b"RIFF") and len(head) >= 12:
        return _RIFF_TYPES.get(head[8:12], "application/octet-stream")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if _looks_like_text(head):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def _looks_like_text(head: bytes) -> bool:
    """Go 的嗅探算法在无签名命中时的兜底：可打印 ASCII / 合法 UTF-8 才算文本。"""
    printable = set(b"\t\n\v\f\r\x1b")
    for byte in head:
        if byte <= 0x08 or byte == 0x0B or (0x0E <= byte <= 0x1A) or (0x1C <= byte <= 0x1F):
            return False
        if byte == 0x7F:
            return False
        if byte > 0x7F:
            break
        if 0x20 <= byte <= 0x7E or byte in printable:
            continue
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def extension_of(value: str) -> str:
    """``os.path.splitext`` 的薄封装（对应 Go ``filepath.Ext``）。

    与 Go 的差异：``filepath.Ext`` 用平台分隔符，这里统一按 basename 取；
    媒体名在清洗后已不含目录成分。
    """
    return os.path.splitext(value or "")[1]
