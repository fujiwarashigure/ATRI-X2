"""媒体输出的准备、解析与回执缓存。

对应 Go：``internal/agent/media_output.go``。

职责：``prepareMediaOutputs`` / ``resolveMediaOutputs`` / ``sendPreparedMedia`` /
``cacheMediaReceipt`` / ``isMediaOutputKind``。

与 Go 的差异（形式，不是简化）：

* ``media.Manager`` 的导入/解析在 Python 侧是 async，因此三个入口都是 async
  （``AgentSeams.send_prepared_media`` 已按 async 调用）。
* ``cleanups`` 用一个 ``cleanup()`` 闭包统一释放，与 Go 的 ``func()`` 相同；
  释放顺序按加入顺序（Go 亦然）。
* ``errors.Join`` → 收集后一次性抛出 ``ExceptionGroup``（Python 的等价物）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from atri_x.delivery import Kind, KindFile, KindImage, KindRecord, Output, Receipt, Source, validate_outputs
from atri_x.media import Input as MediaInput
from atri_x.media import sanitize_name
from atri_x.storage import MediaOutput
from atri_x.storage.times import now as storage_now

from atri_x.agent.tools import service_host

__all__ = [
    "MediaOutputMixin",
    "cache_media_receipt",
    "is_media_output_kind",
    "prepare_media_outputs",
    "resolve_media_outputs",
    "send_prepared_media",
]


async def prepare_media_outputs(agent: Any, ctx: Any, outputs: list[Any]) -> list[Any]:
    """把输出里的媒体来源落成本地媒体条目。对应 Go ``prepareMediaOutputs``。"""
    host = service_host(agent)
    validate_outputs(outputs)
    prepared = list(outputs)
    media = getattr(host, "media", None)
    if media is None:
        for out in prepared:
            if getattr(getattr(out, "source", None), "media_id", ""):
                raise ValueError("media center is not configured")
        return prepared
    for index, out in enumerate(prepared):
        if getattr(out, "kind", "") not in (KindImage, KindFile, KindRecord):
            continue
        source = getattr(out, "source", None) or Source()
        spec = MediaInput(name=getattr(out, "name", "") or "", mime_type=source.mime_type)
        item = None
        if source.media_id:
            item = await media.metadata(ctx, source.media_id)
        elif source.url:
            spec.source.url = source.url
            item = await media.import_url(ctx, source.url, spec)
        elif source.path:
            item = await media.import_file(ctx, source.path, spec)
        elif source.data:
            item = await media.import_bytes(ctx, source.data, spec)
        if item is None:
            raise ValueError("prepare output media %d: no media source" % index)
        prepared[index].source = Source(media_id=item.id, mime_type=item.mime_type)
        if not getattr(prepared[index], "name", ""):
            prepared[index].name = item.name
        else:
            prepared[index].name = sanitize_name(prepared[index].name)
    return prepared


async def resolve_media_outputs(
    agent: Any, ctx: Any, outputs: list[Any]
) -> tuple[list[Any], Callable[[], None]]:
    """只解析发送副本；Hook 事件与排队中的输出仍保留媒体 ID。

    对应 Go ``resolveMediaOutputs``。
    """
    host = service_host(agent)
    resolved = list(outputs)
    cleanups: list[Callable[[], None]] = []

    def _cleanup() -> None:
        for release in cleanups:
            release()

    media = getattr(host, "media", None)
    for index, out in enumerate(resolved):
        source = getattr(out, "source", None) or Source()
        if not source.media_id:
            continue
        if media is None:
            _cleanup()
            return [], _noop
        try:
            new_source, release = await media.resolve_for_output(ctx, source.media_id)
        except BaseException:
            _cleanup()
            raise
        cleanups.append(release)
        resolved[index].source = new_source
    return resolved, _cleanup


async def send_prepared_media(
    agent: Any, ctx: Any, outputs: list[Any], send: Callable[[list[Any]], Any]
) -> Any:
    """准备 → 解析 → 发送 → 记回执。对应 Go ``sendPreparedMedia``。"""
    host = service_host(agent)
    prepared = await prepare_media_outputs(host, ctx, outputs)
    resolved, cleanup = await resolve_media_outputs(host, ctx, prepared)
    try:
        result = send(resolved)
        if hasattr(result, "__await__"):
            result = await result
    finally:
        cleanup()
    receipt = result if isinstance(result, Receipt) else Receipt()
    try:
        cache_media_receipt(host, ctx, prepared, receipt)
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：只记错误
        logger = getattr(host, "logger", None)
        if logger is not None:
            error = getattr(logger, "error", None)
            if error is not None:
                error("cache sent media receipt failed", error=str(exc))
    if not isinstance(result, Receipt):
        return result
    return receipt


def cache_media_receipt(agent: Any, ctx: Any, outputs: list[Any], receipt: Any) -> None:
    """把"这条消息的第 N 段是哪个媒体"写进索引。对应 Go ``cacheMediaReceipt``。"""
    host = service_host(agent)
    store = getattr(host, "store", None)
    retention = int(getattr(host, "media_retention_days", 0) or 0)
    if store is None or store.media() is None or retention <= 0:
        return
    expires_at = _now() + timedelta(days=retention)
    errors: list[BaseException] = []
    for message in getattr(receipt, "sent_messages", None) or ():
        platform_name = str(getattr(message, "platform", "") or "").strip()
        scope_id = str(getattr(message, "scope_id", "") or "").strip()
        message_id = str(getattr(message, "platform_message_id", "") or "").strip()
        if not platform_name or not scope_id or not message_id:
            continue
        segment_index = 0
        for output_index in getattr(message, "output_indexes", None) or ():
            if output_index < 0 or output_index >= len(outputs):
                continue
            out = outputs[output_index]
            media_id = str(getattr(getattr(out, "source", None), "media_id", "") or "")
            if not media_id or not is_media_output_kind(getattr(out, "kind", "")):
                continue
            try:
                store.media().save_output(
                    MediaOutput(
                        platform=platform_name,
                        scope_id=scope_id,
                        message_id=message_id,
                        segment_index=segment_index,
                        kind=str(out.kind),
                        media_id=media_id,
                        expires_at=expires_at,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - 汇总后一次抛出
                errors.append(exc)
            segment_index += 1
    if errors:
        raise ExceptionGroup("cache media receipt", errors)


def is_media_output_kind(kind: str) -> bool:
    """是否是带媒体的输出种类。对应 Go ``isMediaOutputKind``。"""
    return kind in (KindImage, KindFile, KindRecord)


class MediaOutputMixin:
    """Go ``*Agent`` 的媒体输出方法组。"""

    async def prepare_media_outputs(self, ctx: Any, outputs: list[Output]) -> list[Output]:
        return await prepare_media_outputs(self, ctx, outputs)

    async def resolve_media_outputs(
        self, ctx: Any, outputs: list[Output]
    ) -> tuple[list[Output], Callable[[], None]]:
        return await resolve_media_outputs(self, ctx, outputs)

    async def send_prepared_media(
        self, ctx: Any, outputs: list[Output], send: Callable[[list[Output]], Any]
    ) -> Any:
        return await send_prepared_media(self, ctx, outputs, send)

    def cache_media_receipt(self, ctx: Any, outputs: list[Output], receipt: Receipt) -> None:
        cache_media_receipt(self, ctx, outputs, receipt)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _noop() -> None:
    return None


def _now() -> datetime:
    moment = storage_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _kind_values() -> tuple[str, ...]:
    return (Kind.Image, Kind.FILE, Kind.RECORD)
