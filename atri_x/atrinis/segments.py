"""Atrinis 段校验与段换算（纯函数）。

对应 Go：``internal/atrinis/segments.go``。

两类换算共用同一份段列表：

* :func:`segments_llm` —— 转成 LLM 消息段（文本段直带文本，媒体段带 URL/媒体 ID）；
* :func:`segments_content_text` —— 转成"人能读的一句话摘要"，用于审计与转发给
  不支持富文本的入口（图片/文件降级成 ``[图片: 名字]`` / ``[文件: 名字]``）。

URL 校验只在**媒体段**上做：``data:`` 前缀放行（内联数据由媒体中心负责导入），
否则必须是带 host 的 http/https。
"""

from __future__ import annotations

from urllib.parse import urlparse

from atri_x.atriena import Segment, SegmentKindFile, SegmentKindImage, SegmentKindText
from atri_x.errors import InvalidError
from atri_x.llm import MessageSegment, SegmentType
from atri_x.media import IDPrefix, ValidID

__all__ = [
    "segments_content_text",
    "segments_llm",
    "validate_media_segment",
    "validate_segment_url",
]


def validate_segment_url(raw_url: str) -> None:
    """校验媒体段 URL。对应 Go ``validateSegmentURL``（返回 error）。

    放行 ``data:`` 内联数据；其余必须是 ``http``/``https`` 且有 host。
    """
    text = (raw_url or "").strip()
    if not text:
        raise InvalidError("url is required for image/file segments")
    if text.startswith("data:"):
        return
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise InvalidError(f"url scheme must be http or https, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise InvalidError("url host is empty")


def validate_media_segment(seg: Segment) -> None:
    """校验一条媒体段。对应 Go ``validateMediaSegment``。

    文本段不允许带媒体；图片/文件段的 ``url`` 要么是合法媒体 ID，要么是合法 URL。
    """
    if seg.kind == SegmentKindText:
        if seg.url:
            raise InvalidError("text segment cannot contain media")
        return
    if seg.kind in (SegmentKindImage, SegmentKindFile):
        if (seg.url or "").lower().startswith(IDPrefix):
            if not ValidID(seg.url):
                raise InvalidError("invalid media ID")
            return
        validate_segment_url(seg.url)
        return
    raise InvalidError(f"unsupported segment kind {seg.kind!r}")


def segments_content_text(segments: list[Segment] | None) -> str:
    """段的纯文本摘要。对应 Go ``segmentsContentText``。

    文本段原样拼接（**不**加分隔符，与 Go 的 ``WriteString`` 一致）；
    媒体段写成 ``[图片: X]`` / ``[文件: X]``，``X`` 取名字，其次 URL，
    都没有则退到 ``[图片]`` / ``[文件]``。整体去首尾空白。
    """
    parts: list[str] = []
    for seg in segments or []:
        if seg.kind == SegmentKindText:
            parts.append(seg.text)
        elif seg.kind == SegmentKindImage:
            label = (seg.name or "").strip() or (seg.url or "").strip() or "[图片]"
            parts.append(f"[图片: {label}]")
        elif seg.kind == SegmentKindFile:
            label = (seg.name or "").strip() or (seg.url or "").strip() or "[文件]"
            parts.append(f"[文件: {label}]")
    return "".join(parts).strip()


def segments_llm(segments: list[Segment] | None) -> list[MessageSegment]:
    """转成 LLM 消息段。对应 Go ``segmentsLLM``。

    未知 ``kind`` 被忽略（与 Go 的 ``switch`` 无 default 分支一致）。
    """
    out: list[MessageSegment] = []
    for seg in segments or []:
        if seg.kind == SegmentKindText:
            out.append(MessageSegment(type=SegmentType.TEXT, text=seg.text))
        elif seg.kind == SegmentKindImage:
            out.append(
                MessageSegment(
                    type=SegmentType.IMAGE, url=seg.url, name=seg.name, mime_type=seg.mime_type
                )
            )
        elif seg.kind == SegmentKindFile:
            out.append(
                MessageSegment(
                    type=SegmentType.FILE, url=seg.url, name=seg.name, mime_type=seg.mime_type
                )
            )
    return out
