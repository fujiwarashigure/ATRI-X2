"""Chat History 原始有序 segments 的编解码与敏感来源清洗。

对应 Go：``internal/platform/media.go``。

``marshal_chat_segments`` 只存"平台事实"：不落本地/已物化的媒体来源，并把带
凭据风险的 URL、路径式平台文件 ID、内联数据名一律清掉，避免把 bot token 之类
的东西写进历史库。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlparse, urlunparse

from atri_x.platform.platform import MessageSegment, MessageSegmentType


def marshal_chat_segments(segments: Sequence[MessageSegment] | None) -> str:
    """存下有序的平台事实，丢掉已物化或本地的来源。对应 Go ``MarshalChatSegments``。"""
    stored: list[dict[str, Any]] = []
    for segment in segments or []:
        if segment.type not in (
            MessageSegmentType.TEXT,
            MessageSegmentType.IMAGE,
            MessageSegmentType.FILE,
        ):
            continue
        segment = dataclasses.replace(segment)
        segment.media_id = ""
        segment.url = _chat_media_url(segment.url)
        if segment.type != MessageSegmentType.TEXT:
            file_id = segment.platform_file_id
            if file_id and ("/" in file_id or "\\" in file_id or ":" in file_id):
                segment.platform_file_id = ""
            name = segment.name
            if "://" in name or name.startswith("data:") or name.startswith("base64:"):
                segment.name = ""
            else:
                name = _path_base(name.replace("\\", "/"))
                segment.name = "" if name == "." else name
        stored.append(segment.to_json())
    if not stored:
        return ""
    return json.dumps(stored, ensure_ascii=False, separators=(",", ":"))


def _path_base(value: str) -> str:
    """对应 Go ``path.Base``（与 ``posixpath.basename`` 在结尾斜杠上不同）。"""
    if value == "":
        return "."
    value = value.rstrip("/")
    if "/" in value:
        value = value[value.rindex("/") + 1:]
    return value or "/"


def unmarshal_chat_segments(value: str) -> list[MessageSegment]:
    """对应 Go ``UnmarshalChatSegments``；解析失败返回空列表（Go 返回 nil）。"""
    text = (value or "").strip()
    if not text:
        return []
    try:
        raw = json.loads(text)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [MessageSegment.from_json(item) for item in raw]


def _chat_media_url(value: str) -> str:
    """对应 Go ``chatMediaURL``：只保留无凭据风险的绝对 HTTP(S) URL。"""
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        # Go 的 url.Parse 对含空白的 URL 直接报错。
        return ""
    try:
        u = urlparse(value)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname:
        return ""
    if u.username is not None or u.password is not None:
        return ""
    # Telegram 的下载路径里带 bot token（即使没有 query）。
    if "/bot" in u.path.lower():
        return ""
    for key, _ in parse_qsl(u.query, keep_blank_values=True):
        key = key.lower()
        if (
            key.startswith("x-amz-")
            or key.startswith("x-goog-")
            or "token" in key
            or "signature" in key
            or key in ("sig", "rkey", "auth_key")
        ):
            return ""
    return urlunparse(u)


__all__ = [name for name in dir() if not name.startswith("_")]
