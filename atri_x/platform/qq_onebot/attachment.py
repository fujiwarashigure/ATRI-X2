"""入站媒体解析（``get_image`` / ``get_file`` / 群文件直链兜底）。

对应 Go：``internal/platform/qq-onebot/attachment.go``。

上游规则：段自带的体积必须已经在上限内；直链 URL 直接透传（不下载、不落盘），
否则用 ``get_image`` / ``get_file`` 问平台要本地路径或可下载 URL；``get_file``
返回的 ``file_size`` 再检查一次上限。

Python 侧新增（SnowLuma 接入）：**群/私聊文件不能只靠 ``get_file``**。SnowLuma 的
``get_file`` 只覆盖图片/语音缓存，群文件/私聊文件必须走 ``get_group_file_url`` /
``get_private_file_url``，且私聊文件依赖入站 ``file`` 段的 ``file_hash``。所以文件段
的下载顺序是「专有动作 → ``get_file`` 兜底」；后端明确不支持 ``get_file`` 取文件时
直接给可行动的报错，而不是白跑一次再报"source unavailable"。

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
        path, url = await resolve_chat_file(adapter, ctx, segment, file, max_bytes)
    else:
        raise AtriXError(f"unsupported onebot media kind {segment.type!r}")
    if is_http_media_source(url):
        return Source(url=(url or "").strip(), mime_type=segment.mime_type)
    if (path or "").strip() == "":
        raise AtriXError("onebot media source unavailable")
    return Source(path=(path or "").strip(), mime_type=segment.mime_type)


async def resolve_chat_file(
    adapter: Any, ctx: Any, segment: MessageSegment, file: str, max_bytes: int
) -> tuple[str, str]:
    """群/私聊文件下载：返回 ``(本地路径, 直链)``，两者至少一个非空。

    * 先用协议端的专有动作（群 → ``get_group_file_url``、私聊 →
      ``get_private_file_url``）。SnowLuma 只有这条路能用；NapCat 也支持。
    * 后端声明 ``get_file`` 不能取文件（SnowLuma）时，专有动作失败就直接报可行动的
      错误——不要再用 ``get_file`` 白跑一次。
    * 其它后端（含测试桩/旧实现）回落到 ``get_file``，行为与改动前一致。
    """
    transport = adapter.transport
    profile = getattr(transport, "profile", None)
    meta = getattr(segment, "platform_meta", None) or {}
    get_url = getattr(transport, "get_chat_file_url", None)
    if callable(get_url):
        url = await get_url(
            ctx,
            (segment.platform_file_id or "").strip() or file,
            group_id=_as_int(meta.get("group_id")),
            user_id=_as_int(meta.get("user_id")),
            file_hash=str(meta.get("file_hash") or "").strip(),
        )
        if is_http_media_source(url):
            return "", url.strip()
    if not bool(getattr(profile, "get_file_supports_file", True)):
        label = str(getattr(profile, "label", "") or "this backend")
        raise AtriXError(
            f"{label} cannot download chat files with get_file; "
            "the dedicated group/private file url action failed or was unavailable"
        )
    data = await transport.get_file(ctx, file)
    size = parse_int64((data.file_size or "").strip())
    if size is not None and size > max_bytes:
        raise MediaTooLargeError(
            f"onebot media exceeds import limit of {max_bytes} bytes"
        )
    return data.file, data.url


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [name for name in dir() if not name.startswith("_")]
