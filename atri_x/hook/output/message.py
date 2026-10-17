"""进程 Hook 替换消息内容的线格式（``message.segments``）。

对应 Go：``internal/hook/output/message.go``。
"""

from __future__ import annotations

import base64 as _base64
import os
from dataclasses import dataclass
from typing import Any, Sequence

from atri_x.hook.output.jsonstrict import (
    decode_json,
    require_string,
    strict_object,
)
from atri_x.hook.output.output import (
    MAX_BASE64_BYTES,
    OutputError,
    _decoded_len,
    _strip_base64_whitespace,
)
from atri_x.llm import llm
from atri_x.media.mimetype import detect_content_type, mime_type_by_extension
from atri_x.media.types import IDPrefix, valid_id

__all__ = [
    "MessageSegment",
    "build_message_segments",
    "build_message_segment",
    "build_message_image",
    "read_limited_image",
    "decode_image_data_url",
    "image_data_url",
]


@dataclass
class MessageSegment:
    """进程 Hook 替换消息内容的 wire 段。对应 Go ``MessageSegment``。"""

    type: str = ""
    text: str = ""
    url: str = ""
    path: str = ""
    base64: str = ""
    name: str = ""
    mime_type: str = ""

    def to_json(self) -> dict[str, Any]:
        """对齐 Go 的 json tag（``type`` 必出，其余 ``omitempty``）。"""
        out: dict[str, Any] = {"type": self.type}
        for key, value in (
            ("text", self.text),
            ("url", self.url),
            ("path", self.path),
            ("base64", self.base64),
            ("name", self.name),
            ("mime_type", self.mime_type),
        ):
            if value:
                out[key] = value
        return out

    @classmethod
    def from_json(cls, raw: Any, path: str = "segment") -> "MessageSegment":
        return decode_json(raw, lambda obj: cls.from_object(obj, path))

    @classmethod
    def from_object(cls, obj: Any, path: str = "segment") -> "MessageSegment":
        values = strict_object(
            obj,
            path,
            {
                "type": require_string,
                "text": require_string,
                "url": require_string,
                "path": require_string,
                "base64": require_string,
                "name": require_string,
                "mime_type": require_string,
            },
        )
        return cls(**values)


def build_message_segments(
    specs: Sequence[MessageSegment], base_dir: str
) -> list[llm.MessageSegment]:
    """把一组 wire 段构造成 llm 消息段。对应 Go ``BuildMessageSegments``。"""
    segments: list[llm.MessageSegment] = []
    for index, spec in enumerate(specs):
        try:
            segments.append(build_message_segment(spec, base_dir))
        except OutputError as exc:
            raise OutputError(f"message.segments[{index}]: {exc}") from exc
    return segments


def build_message_segment(spec: MessageSegment, base_dir: str) -> llm.MessageSegment:
    """构造单个消息段。对应 Go ``buildMessageSegment``。"""
    spec = MessageSegment(
        type=spec.type.strip(),
        text=spec.text,
        url=spec.url.strip(),
        path=spec.path.strip(),
        base64=spec.base64.strip(),
        name=spec.name.strip(),
        mime_type=spec.mime_type.strip(),
    )
    if spec.type == llm.SegmentType.TEXT:
        if spec.url or spec.path or spec.base64 or spec.name or spec.mime_type:
            raise OutputError("text segment only supports text")
        return llm.MessageSegment(type=llm.SegmentType.TEXT, text=spec.text)
    if spec.type == llm.SegmentType.IMAGE:
        return build_message_image(spec, base_dir)
    raise OutputError(f"unsupported segment type {spec.type!r}")


def build_message_image(spec: MessageSegment, base_dir: str) -> llm.MessageSegment:
    """构造图片段。对应 Go ``buildMessageImage``。"""
    provided = [value for value in (spec.url, spec.path, spec.base64) if value]
    if len(provided) != 1:
        raise OutputError("image segment must provide exactly one of url, path or base64")
    for value in (spec.url, spec.path, spec.base64):
        if value.lower().startswith(IDPrefix):
            if not valid_id(value):
                raise OutputError("invalid media ID")
            return llm.MessageSegment(
                type=llm.SegmentType.IMAGE,
                text=spec.text,
                media=value,
                name=spec.name,
                mime_type=spec.mime_type,
            )
    segment = llm.MessageSegment(
        type=llm.SegmentType.IMAGE,
        text=spec.text,
        name=spec.name,
        mime_type=spec.mime_type,
    )
    if spec.url:
        if spec.url.lower().startswith("data:"):
            mime_type, data = decode_image_data_url(spec.url)
            if segment.mime_type and segment.mime_type.lower() != mime_type.lower():
                raise OutputError(
                    f"mime_type {segment.mime_type!r} does not match data URL {mime_type!r}"
                )
            segment.mime_type = mime_type
            segment.url = image_data_url(mime_type, data)
            return segment
        from urllib.parse import urlparse

        parsed = urlparse(spec.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise OutputError("url must be an absolute HTTP(S) URL or image data URL")
        if segment.mime_type and not segment.mime_type.lower().startswith("image/"):
            raise OutputError("mime_type must be image/*")
        segment.url = spec.url
        return segment

    if spec.path:
        path = spec.path
        if "://" in path:
            raise OutputError("path must be a filesystem path, not a URI")
        if not os.path.isabs(path) and base_dir.strip():
            path = os.path.join(base_dir, path)
        data = read_limited_image(path)
        if not segment.name:
            segment.name = os.path.basename(path)
        if not segment.mime_type:
            segment.mime_type = mime_type_by_extension(os.path.splitext(path)[1])
    else:
        encoded = _strip_base64_whitespace(spec.base64)
        if _decoded_len(len(encoded)) > MAX_BASE64_BYTES:
            raise OutputError("base64 image exceeds 10 MiB decoded limit")
        try:
            data = _base64.b64decode(encoded, validate=True)
        except (ValueError, _base64.binascii.Error) as exc:
            raise OutputError(f"decode base64 image: {exc}") from exc
    if len(data) > MAX_BASE64_BYTES:
        raise OutputError("image exceeds 10 MiB decoded limit")
    if not segment.mime_type:
        segment.mime_type = detect_content_type(data)
    if not segment.mime_type.lower().startswith("image/"):
        raise OutputError("mime_type must be image/*")
    segment.url = image_data_url(segment.mime_type, data)
    return segment


def read_limited_image(path: str) -> bytes:
    """读一个图片文件，超过 10 MiB 报错。对应 Go ``readLimitedImage``。"""
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise OutputError(f"open image path: {exc}") from exc
    try:
        with handle:
            data = handle.read(MAX_BASE64_BYTES + 1)
    except OSError as exc:
        raise OutputError(f"read image path: {exc}") from exc
    if len(data) > MAX_BASE64_BYTES:
        raise OutputError("image path exceeds 10 MiB decoded limit")
    return data


def decode_image_data_url(value: str) -> tuple[str, bytes]:
    """解析 ``data:image/...;base64,...``。对应 Go ``decodeImageDataURL``。"""
    header, sep, encoded = value.partition(",")
    lower = header.lower()
    if not sep or not lower.startswith("data:image/") or not lower.endswith(";base64"):
        raise OutputError("image data URL must use base64 encoding")
    mime_type = header[len("data:") : len(header) - len(";base64")]
    cleaned = _strip_base64_whitespace(encoded)
    if _decoded_len(len(cleaned)) > MAX_BASE64_BYTES:
        raise OutputError("image data URL exceeds 10 MiB decoded limit")
    try:
        data = _base64.b64decode(cleaned, validate=True)
    except (ValueError, _base64.binascii.Error) as exc:
        raise OutputError(f"decode image data URL: {exc}") from exc
    if len(data) > MAX_BASE64_BYTES:
        raise OutputError("image data URL exceeds 10 MiB decoded limit")
    return mime_type, data


def image_data_url(mime_type: str, data: bytes) -> str:
    """构造 ``data:...;base64,...``。对应 Go ``imageDataURL``。"""
    return "data:" + mime_type + ";base64," + _base64.b64encode(data).decode("ascii")
