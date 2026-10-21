"""共享平台导入、历史媒体位置关联与本地 ID 查询。

对应 Go：``internal/media/platform.go``。

* :func:`import_platform` —— 解析"只存在于传输层"的来源，且**不持久化任何凭据或
  本地路径**（落库前统一走 ``sanitize_input``）。
* :func:`history_segments` —— 只返回历史消息里的**媒体位置**；文本不占媒体序号。
* :func:`history_ids` —— 只查本地关联（``media_history`` / ``media_outputs``），
  **绝不下载**来源。
* :func:`get_history_media` —— 只有显式获取才下载，成功后写回关联，重复调用复用。

序号约定（与 Go 一致）：历史媒体序号从 **1** 开始，按消息里非文本段的位置顺序；
输出关联记的 ``segment_index`` 是 0 基，所以读回来时 ``+1`` 对齐到同一套序号。
"""

from __future__ import annotations

from typing import Any

from atri_x.delivery import Source as DeliverySource
from atri_x.errors import NotFoundError
from atri_x.media.types import Input, MediaError, Source
from atri_x.platform.media import unmarshal_chat_segments
from atri_x.platform.platform import MessageSegment, MessageSegmentType
from atri_x.storage.models import HistoryMedia, Media

__all__ = [
    "associate_history",
    "get_history_media",
    "history_ids",
    "history_segments",
    "import_platform",
]

_MEDIA_TYPES = (MessageSegmentType.IMAGE, MessageSegmentType.FILE)


async def import_platform(
    manager: Any, ctx: Any, platform_name: str, resolver: Any, segment: MessageSegment
) -> Media:
    """导入一个平台媒体段。对应 Go ``ImportPlatform``。

    已有 ``media_id`` 时直接回元数据（不重新下载）；否则按 resolver 给出的
    来源形态分派到 ``import_url`` / ``import_file`` / ``import_bytes``。
    """
    if segment.media_id:
        return await manager.metadata(ctx, segment.media_id)
    if segment.size > manager.max_import_bytes:
        raise MediaError(
            f"media exceeds import limit of {manager.max_import_bytes} bytes"
        )
    source = DeliverySource(url=(segment.url or "").strip())
    if not source.url and resolver is not None:
        source = await resolver.resolve_media(ctx, segment, manager.max_import_bytes)
    spec = Input(
        name=segment.name,
        mime_type=segment.mime_type,
        source=Source(
            platform=platform_name,
            url=segment.url,
            file_id=segment.platform_file_id,
        ),
    )
    if not spec.mime_type:
        spec.mime_type = source.mime_type
    if source.media_id:
        return await manager.metadata(ctx, source.media_id)
    if source.url:
        return await manager.import_url(ctx, source.url, spec)
    if source.path:
        return await manager.import_file(ctx, source.path, spec)
    if source.data:
        return await manager.import_bytes(ctx, source.data, spec)
    raise MediaError("platform media source unavailable")


def history_segments(row: Any) -> list[MessageSegment]:
    """返回历史消息里的媒体位置。对应 Go ``HistorySegments``。"""
    return [
        segment
        for segment in unmarshal_chat_segments(row.segments)
        if segment.type in _MEDIA_TYPES
    ]


async def history_ids(manager: Any, ctx: Any, row: Any) -> dict[int, str]:
    """历史位序 → 本地媒体 ID。对应 Go ``HistoryIDs``。

    只查本地关联，从不下载来源。已标记 ``deleting`` 的媒体不算数。
    """
    ids: dict[int, str] = {}
    associations = manager.store.media().find_history(
        row.platform, row.platform_scope_id, row.platform_message_id
    )
    for association in associations:
        if association.history_id != row.id:
            continue
        try:
            item = await manager.metadata(ctx, association.media_id)
        except NotFoundError:
            continue
        if not item.deleting:
            ids[association.media_index] = association.media_id
    outputs = manager.store.media().find_outputs(
        row.platform,
        row.platform_scope_id,
        row.platform_message_id,
        manager.now(),
    )
    for output in outputs:
        try:
            item = await manager.metadata(ctx, output.media_id)
        except NotFoundError:
            continue
        if not item.deleting:
            ids[output.segment_index + 1] = output.media_id
    return ids


async def associate_history(
    manager: Any, ctx: Any, row: Any, index: int, kind: str, id: str
) -> None:
    """写一条历史媒体位置（引用由 SQLite 触发器同事务维护）。对应 Go ``AssociateHistory``。"""
    manager.store.media().save_history(
        HistoryMedia(
            history_id=row.id,
            platform=row.platform,
            scope_id=row.platform_scope_id,
            message_id=row.platform_message_id,
            media_index=index,
            kind=kind,
            media_id=id,
        )
    )


async def get_history_media(
    manager: Any, ctx: Any, row: Any, index: int, resolver: Any
) -> Media:
    """按位序取历史媒体（必要时下载）。对应 Go ``GetHistoryMedia``。"""
    segments = history_segments(row)
    if index < 1 or index > len(segments):
        raise MediaError(
            f"media index {index} out of range (message has {len(segments)} media)"
        )
    ids = await history_ids(manager, ctx, row)
    segment = segments[index - 1]
    segment = MessageSegment(
        type=segment.type,
        text=segment.text,
        user_id=segment.user_id,
        url=segment.url,
        media_id=ids.get(index, ""),
        platform_file_id=segment.platform_file_id,
        mime_type=segment.mime_type,
        name=segment.name,
        size=segment.size,
    )
    item = await import_platform(manager, ctx, row.platform, resolver, segment)
    await associate_history(manager, ctx, row, index, segment.type, item.id)
    return item
