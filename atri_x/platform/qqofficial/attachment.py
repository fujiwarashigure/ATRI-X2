"""入站附件 → 平台中立消息段，以及附件文件名的清洗。

对应 Go：``internal/platform/qqofficial/attachment.go``。

形式差异：

* Go 的 ``mime.ParseMediaType`` → :mod:`email.message` 的头部解析（标准库；
  RFC 2231 的 ``filename*=`` 会被它直接解成 ``filename``，因此这里先看
  ``filename*`` 再看 ``filename``，行为与 Go 的取值顺序一致）。
* ``http.Header`` → 鸭子类型的 ``Mapping``（``None`` 视为没有响应头）。
* QQ 官方消息**不下载**普通群媒体：附件只做成 ``MessageSegment``，与 Go 一致
  （``ResolveMedia`` 在 qqofficial 侧并不存在）。
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from atri_x.platform.platform import MessageSegment, MessageSegmentType
from atri_x.platform.qqofficial.types import MessageAttachment

#: 图片扩展名（与 Go ``isImageURL`` 的列表逐字一致）。
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

#: Content-Type → 扩展名。对应 Go ``extensionFromContentType``。
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}

#: 文件名里必须替换掉的字符（Windows + POSIX 合并集）。对应 Go ``sanitizeFilename``。
_UNSAFE_FILENAME_CHARS = ("\\", "/", ":", "*", "?", '"', "<", ">", "|")


def parse_media_type(value: str) -> tuple[str, dict[str, str]]:
    """对应 Go ``mime.ParseMediaType``；无法解析时返回 ``("", {})``。"""
    text = (value or "").strip()
    if text == "":
        return "", {}
    message = EmailMessage()
    try:
        message["content-type"] = text
        params = message.get_params()
    except Exception:  # noqa: BLE001 - 非法头部按解析失败处理
        return "", {}
    if not params:
        return "", {}
    main = (params[0][0] or "").strip().lower()
    if main == "":
        return "", {}
    extra: dict[str, str] = {}
    for key, item in params[1:]:
        if isinstance(key, str) and isinstance(item, str):
            extra[key.strip().lower()] = item
    return main, extra


def first_non_empty(*values: str) -> str:
    """第一个去空白后非空的值（**已去空白**）。对应 Go ``attachment.firstNonEmpty``。"""
    for value in values:
        value = (value or "").strip()
        if value != "":
            return value
    return ""


def _header_get(header: Mapping[str, str] | None, name: str) -> str:
    """对应 Go ``http.Header.Get``（大小写不敏感）。"""
    if not header:
        return ""
    target = name.lower()
    for key, value in header.items():
        if str(key).lower() == target:
            return str(value)
    return ""


def content_disposition_filename(value: str) -> str:
    """从 ``Content-Disposition`` 里取文件名。对应 Go ``contentDispositionFilename``。"""
    _main, params = parse_media_type(value)
    if not params:
        return ""
    return first_non_empty(params.get("filename*", ""), params.get("filename", ""))


def is_image_url(value: str) -> bool:
    """按扩展名判断是不是图片。对应 Go ``isImageURL``。"""
    value = (value or "").strip().lower()
    return value.endswith(IMAGE_SUFFIXES)


def is_image_attachment(attachment: MessageAttachment) -> bool:
    """对应 Go ``isImageAttachment``。"""
    content_type = (attachment.content_type or "").strip().lower()
    if content_type.startswith("image/"):
        return True
    if content_type == "file":
        return False
    if attachment.width > 0 or attachment.height > 0:
        return True
    return is_image_url(attachment.filename) or is_image_url(attachment.url)


def attachment_declared_mime_type(attachment: MessageAttachment) -> str:
    """对应 Go ``attachmentDeclaredMIMEType``。"""
    mime_type = (attachment.content_type or "").strip()
    media_type, _params = parse_media_type(mime_type)
    if media_type != "":
        mime_type = media_type
    return mime_type.lower()


def extension_from_content_type(value: str) -> str:
    """对应 Go ``extensionFromContentType``。"""
    media_type, _params = parse_media_type(value)
    if media_type == "":
        media_type = (value or "").strip().lower()
    return CONTENT_TYPE_EXTENSIONS.get(media_type, "")


def looks_like_qq_download_name(name: str) -> bool:
    """对应 Go ``looksLikeQQDownloadName``。"""
    lower = (name or "").strip().lower()
    return "qqdownload" in lower or lower.startswith("robot1.0_") or len(lower) > 96


def sanitize_filename(name: str) -> str:
    """对应 Go ``sanitizeFilename``：非法字符换 ``_``、截到 160 码点、去首尾 ``" ."``。"""
    name = (name or "").strip()
    for char in _UNSAFE_FILENAME_CHARS:
        name = name.replace(char, "_")
    if len(name) > 160:
        name = name[:160]
    return name.strip(" .")


def inbound_attachment_name(
    attachment: MessageAttachment,
    header: Mapping[str, str] | None = None,
    index: int = 1,
) -> str:
    """推导附件文件名。对应 Go ``inboundAttachmentName``。"""
    name = first_non_empty(
        attachment.filename,
        content_disposition_filename(_header_get(header, "Content-Disposition")),
    )
    if name == "":
        parsed = urlsplit(attachment.url or "")
        path = parsed.path or ""
        if "/" in path:
            name = path[path.rindex("/") + 1 :]
        else:
            name = path
    name = sanitize_filename(name)
    if name == "" or looks_like_qq_download_name(name):
        name = f"attachment-{index}{extension_from_content_type(_header_get(header, 'Content-Type'))}"
    return name


def inbound_attachment_segments(
    attachments: Sequence[MessageAttachment] | None,
) -> list[MessageSegment]:
    """附件列表 → 图片/文件段。对应 Go ``inboundAttachmentSegments``。"""
    segments: list[MessageSegment] = []
    for offset, attachment in enumerate(attachments or []):
        kind = MessageSegmentType.FILE
        if is_image_attachment(attachment):
            kind = MessageSegmentType.IMAGE
        mime_type = attachment_declared_mime_type(attachment)
        if mime_type == "file":
            mime_type = ""
        segments.append(
            MessageSegment(
                type=kind,
                url=(attachment.url or "").strip(),
                name=inbound_attachment_name(attachment, None, offset + 1),
                mime_type=mime_type,
                size=attachment.size,
            )
        )
    return segments


__all__ = [name for name in dir() if not name.startswith("_")]
