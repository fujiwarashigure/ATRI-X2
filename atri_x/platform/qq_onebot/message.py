"""入站消息归一化：段 → 文本 / @提及 / 媒体段 / 待展开的转发 id。

对应 Go：``internal/platform/qq-onebot/message.go`` 的归一化部分。

要点（上游语义）：

* 文本与匹配视图分开：``text`` 保留换行与缩进（模型输入与 Chat History 用），
  ``match_text`` 是唤醒/命令匹配视图，**不含**转发内容 —— 转发正文不可信，
  不能凭它唤醒模型或触发命令。
* 图片段优先取 ``file``，其次 ``filename``；``url`` 为空且 ``file`` 本身是
  直链（http/https/base64/data/file）时把它当 url 用。
* ``record`` / ``video`` / ``file`` 统一投影成 ``SegmentFile``，``text`` 字段带
  中文种类标签（``语音`` / ``视频`` / ``文件``）。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from atri_x.platform.platform import (
    Mention,
    MessageSegment,
    SegmentAt,
    SegmentFile,
    SegmentImage,
    SegmentText,
)
from atri_x.platform.qq_onebot.forward import (
    clean_text,
    first_non_empty,
    normalize_forward_segment,
    segment_data_int64,
    segment_data_string,
)
from atri_x.platform.qq_onebot.types import (
    ForwardLimits,
    NormalizedMessage,
    Segment,
    Sender,
)


def normalize_message(raw: Any, raw_message: str, self_id: int) -> NormalizedMessage:
    """对应 Go ``normalizeMessage``（使用默认转发展开上限）。"""
    from atri_x.platform.qq_onebot.types import default_forward_limits

    return normalize_message_with_limits(raw, raw_message, self_id, default_forward_limits())


def normalize_message_with_limits(
    raw: Any, raw_message: str, self_id: int, limits: ForwardLimits | None
) -> NormalizedMessage:
    """对应 Go ``normalizeMessageWithLimits``：段数组优先，其次纯文本。"""
    normalized, ok = normalize_message_segments_with_limits(raw, self_id, limits)
    if ok:
        return normalized
    if raw is not None:
        return normalize_plain_text(message_string(raw))
    return normalize_plain_text(raw_message)


def normalize_message_segments(raw: Any, self_id: int) -> tuple[NormalizedMessage, bool]:
    """对应 Go ``normalizeMessageSegments``。"""
    from atri_x.platform.qq_onebot.types import default_forward_limits

    return normalize_message_segments_with_limits(raw, self_id, default_forward_limits())


def normalize_message_segments_with_limits(
    raw: Any, self_id: int, limits: ForwardLimits | None
) -> tuple[NormalizedMessage, bool]:
    """对应 Go ``normalizeMessageSegmentsWithLimits``。

    ``raw`` 是已解析的 JSON 值：既接受真正的段数组，也接受"内容本身是一段 JSON
    数组文本"的字符串形式（部分实现在 ``message`` 里塞数组字符串）。
    """
    segments = _segments_from_json(raw)
    if segments is not None:
        return normalize_segments_with_limits(segments, self_id, limits), True
    text = message_string(raw)
    if text.strip().startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        nested = _segments_from_json(parsed)
        if nested is not None:
            return normalize_segments_with_limits(nested, self_id, limits), True
    return NormalizedMessage(), False


def message_string(raw: Any) -> str:
    """对应 Go ``messageString``（只有 JSON 字符串才算）。"""
    return raw if isinstance(raw, str) else ""


def _segments_from_json(raw: Any) -> list[Segment] | None:
    """对应 Go ``json.Unmarshal(raw, &[]Segment)``：失败返回 ``None``。"""
    if not isinstance(raw, list):
        return None
    segments: list[Segment] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        segments.append(Segment.from_json(item))
    return segments


def normalize_segments(segments: Sequence[Segment], self_id: int) -> NormalizedMessage:
    """对应 Go ``normalizeSegments``。"""
    from atri_x.platform.qq_onebot.types import default_forward_limits

    return normalize_segments_with_limits(segments, self_id, default_forward_limits())


def normalize_segments_with_limits(
    segments: Sequence[Segment], self_id: int, limits: ForwardLimits | None
) -> NormalizedMessage:
    """段数组 → 归一化消息。对应 Go ``normalizeSegmentsWithLimits``。"""
    from atri_x.platform.qq_onebot.forward import at_text

    effective = (limits or ForwardLimits()).normalized()
    out = NormalizedMessage()
    parts: list[str] = []
    match_parts: list[str] = []
    self_text = str(self_id)
    for segment in segments:
        kind = segment.type
        data = segment.data or {}
        if kind == "text":
            text = segment_data_string(data, "text")
            parts.append(text)
            match_parts.append(text)
            if text != "":
                out.segments.append(MessageSegment(type=SegmentText, text=text))
        elif kind == "at":
            qq = segment_data_string(data, "qq").strip()
            if qq == "" or qq == "all":
                continue
            out.mentions.append(Mention(user_id=qq))
            if qq != self_text:
                text = at_text(qq, "")
                parts.append(text)
                match_parts.append(text)
                out.segments.append(MessageSegment(type=SegmentAt, text=text, user_id=qq))
        elif kind == "reply":
            out.reply_id = segment_data_string(data, "id").strip()
        elif kind == "image":
            out.segments.append(image_segment(data))
        elif kind == "record":
            parts.append("[语音]")
            match_parts.append("[语音]")
            out.segments.append(file_segment("语音", data))
        elif kind == "video":
            parts.append("[视频]")
            match_parts.append("[视频]")
            out.segments.append(file_segment("视频", data))
        elif kind == "file":
            parts.append("[文件]")
            match_parts.append("[文件]")
            out.segments.append(file_segment("文件", data))
        elif kind == "face":
            parts.append("[表情]")
            match_parts.append("[表情]")
        elif kind in ("forward", "node"):
            text, forward_id, resolved = normalize_forward_segment(data, effective)
            if forward_id != "" and not resolved:
                out.forward_ids.append(forward_id)
            # 转发正文只做展示/模型上下文，刻意不进入唤醒/命令匹配视图。
            if text != "":
                parts.append(text)
        elif kind != "":
            parts.append("[" + kind + "]")
            match_parts.append("[" + kind + "]")
    out.text = clean_text("".join(parts))
    out.match_text = clean_text("".join(match_parts))
    if not out.segments and out.text != "":
        out.segments.append(MessageSegment(type=SegmentText, text=out.text))
    return out


def normalize_plain_text(text: str) -> NormalizedMessage:
    """对应 Go ``normalizePlainText``。"""
    text = clean_text(text)
    if text == "":
        return NormalizedMessage()
    return NormalizedMessage(
        text=text,
        match_text=text,
        segments=[MessageSegment(type=SegmentText, text=text)],
    )


def image_segment(data: Mapping[str, Any] | None) -> MessageSegment:
    """对应 Go ``imageSegment``。"""
    file = first_non_empty(
        segment_data_string(data, "file"), segment_data_string(data, "filename")
    )
    url = segment_data_string(data, "url").strip()
    if url == "" and is_direct_image_url(file):
        url = file
    return MessageSegment(
        type=SegmentImage,
        url=url,
        name=file,
        platform_file_id=file,
        mime_type=segment_data_string(data, "mime_type"),
        size=segment_data_int64(data, "file_size"),
    )


def is_direct_image_url(value: str) -> bool:
    """对应 Go ``isDirectImageURL``。"""
    value = (value or "").strip().lower()
    return value.startswith(
        ("http://", "https://", "base64://", "data:", "file://")
    )


def file_segment(kind: str, data: Mapping[str, Any] | None) -> MessageSegment:
    """对应 Go ``fileSegment``。"""
    return MessageSegment(
        type=SegmentFile,
        text=kind,
        platform_file_id=first_non_empty(
            segment_data_string(data, "file_id"), segment_data_string(data, "file")
        ),
        mime_type=segment_data_string(data, "mime_type"),
        url=segment_data_string(data, "url").strip(),
        name=first_non_empty(
            segment_data_string(data, "name"),
            segment_data_string(data, "file"),
            segment_data_string(data, "filename"),
            segment_data_string(data, "file_id"),
        ),
        size=segment_data_int64(data, "file_size"),
    )


def sender_name(sender: Sender | None) -> str:
    """优先群名片，其次昵称。对应 Go ``senderName``。"""
    if sender is None:
        return ""
    name = (sender.card or "").strip()
    if name != "":
        return name
    return (sender.nickname or "").strip()


def display_name(sender: Sender | None, user_id: int) -> str:
    """对应 Go ``displayName``（与 ``senderName`` 同义，保留 Go 的调用点分层）。"""
    return sender_name(sender)


__all__ = [name for name in dir() if not name.startswith("_")]
