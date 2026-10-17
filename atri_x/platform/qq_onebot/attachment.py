"""入站媒体解析（OneBot 的 ``get_image`` / ``get_file`` 兜底）。

对应 Go：``internal/platform/qq-onebot/attachment.go``。

上游规则：段自带的体积必须已经在上限内；直链 URL 直接透传（不下载、不落盘），
否则用 ``get_image`` / ``get_file`` 问平台要本地路径或可下载 URL；``get_file``
返回的 ``file_size`` 再检查一次上限。

形式差异：``(Source, error)`` → 抛异常；超过导入上限用
:class:`atri_x.errors.MediaTooLargeError`（与 Telegram 适配器同一口径），其余不可用
情形用 :class:`atri_x.errors.AtriXError`，文案与 Go 逐字一致。
"""

from __future__ import annotations

from typing import Any

from atri_x.delivery.delivery import Source, is_http_media_source
from atri_x.errors import AtriXError, MediaTooLargeError
from atri_x.platform.platform import MessageSegment, MessageSegmentType
from atri_x.platform.qq_onebot.forward import first_non_empty
from atri_x.platform.qq_onebot.types import parse_int64


async def resolve_media(
    adapter: Any, ctx: Any, segment: MessageSegment, max_bytes: int
) -> Source:
    """把入站媒体段解析成可发送的媒体来源。对应 Go ``(*Adapter).ResolveMedia``。"""
    if segment.size > max_bytes:
        raise MediaTooLargeError(
            f"onebot media exceeds import limit of {max_bytes} bytes"
        )
    if is_http_media_source(segment.url):
        return Source(url=segment.url, mime_type=segment.mime_type)
    file = first_non_empty(segment.platform_file_id, segment.name)
    if file == "" or adapter.transport is None:
        raise AtriXError("onebot media source unavailable")
    if segment.type == MessageSegmentType.IMAGE:
        data = await adapter.transport.get_image(ctx, file)
        path, url = data.file, data.url
    elif segment.type == MessageSegmentType.FILE:
        data = await adapter.transport.get_file(ctx, file)
        size = parse_int64((data.file_size or "").strip())
        if size is not None and size > max_bytes:
            raise MediaTooLargeError(
                f"onebot media exceeds import limit of {max_bytes} bytes"
            )
        path, url = data.file, data.url
    else:
        raise AtriXError(f"unsupported onebot media kind {segment.type!r}")
    if is_http_media_source(url):
        return Source(url=(url or "").strip(), mime_type=segment.mime_type)
    if (path or "").strip() == "":
        raise AtriXError("onebot media source unavailable")
    return Source(path=(path or "").strip(), mime_type=segment.mime_type)


__all__ = [name for name in dir() if not name.startswith("_")]
