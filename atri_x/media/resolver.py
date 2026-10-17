"""LLM 媒体解析与传输选择。

对应 Go：``internal/media/resolver.go``。

两块职责：

* :func:`materialize_with_limits` —— 把新的入站媒体来源（``data:`` URL、HTTP(S)
  URL、本地路径、``file://``）换成稳定引用；失败的位置降级成
  ``[媒体不可用；…]`` 文本段，**不让整条消息失败**。
* :func:`resolve_for_llm` —— **只改请求副本**：按**持久化媒体大小**之和选择
  base64 / S3 / hybrid 传输，并把签名 URL 或 data URL 写进副本的段落。
  规范化输入（canonical messages）绝不被修改。

传输选择规则（与 Go 逐字一致）：:

    remote = backend == "s3" or (backend == "hybrid" and total > max_direct_base64_bytes)
    if not remote and total > max_direct_base64_bytes: 报错

``base64`` 模式下超阈值是**硬错误**（不能悄悄降级成远端），``hybrid`` 模式才
自动切远端。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlparse

from atri_x.errors import NotFoundError
from atri_x.llm.llm import MessageSegment, SegmentFile, SegmentImage, SegmentText, clone_messages
from atri_x.media.sanitize import sanitize_media_name
from atri_x.media.types import (
    ImportLimits,
    Input,
    MediaError,
    is_cancelled,
    raise_if_cancelled,
    valid_id,
)
from atri_x.storage.models import Media

__all__ = [
    "materialize",
    "materialize_with_limits",
    "resolve_for_llm",
    "unavailable",
]

_MEDIA_TYPES = (SegmentImage, SegmentFile)

#: ``_resolve_segment`` 的"这条段落没有来源、应当原样保留"哨兵。
#:
#: 对应 Go ``MaterializeWithLimits`` 的 ``default: continue``：没有 media ID、也没有
#: URL 的图片/文件段**不改写**（不是"媒体不可用"）。用一个独立哨兵而不是 ``None``，
#: 是因为 ``None`` 还要表示"导入失败"。
_UNCHANGED = object()


def unavailable(segment: MessageSegment) -> MessageSegment:
    """把取不到的媒体位置换成文本占位。对应 Go ``unavailable``。

    名称会先清洗，因此这里不会把带 query/token 的原始 URL 泄进模型可见文本。
    """
    label = (segment.name or "").strip()
    if label:
        label = sanitize_media_name(label)
    if segment.media:
        label += "；媒体 ID：" + segment.media
    return MessageSegment(type=SegmentText, text="[媒体不可用；" + label + "]")


async def materialize(manager: Any, ctx: Any, segments: Sequence[MessageSegment]) -> list[MessageSegment]:
    """对应 Go ``Materialize``。"""
    return await materialize_with_limits(manager, ctx, segments, ImportLimits())


async def materialize_with_limits(
    manager: Any,
    ctx: Any,
    segments: Sequence[MessageSegment],
    limits: ImportLimits,
) -> list[MessageSegment]:
    """带单次导入上限的 :func:`materialize`。对应 Go ``MaterializeWithLimits``。

    ``limits`` **只影响这一次调用**，不会改动共享 Manager 上的设置；``max_import_bytes``
    只能收紧、不能放大 Manager 的上限。
    """
    if limits.max_import_bytes <= 0 or limits.max_import_bytes > manager.max_import_bytes:
        limits = ImportLimits(
            max_import_bytes=manager.max_import_bytes, download_timeout=limits.download_timeout
        )
    if limits.download_timeout <= 0:
        limits = ImportLimits(
            max_import_bytes=limits.max_import_bytes, download_timeout=manager.download_timeout
        )
    # Go 复制切片元素（值语义），本仓必须显式克隆，否则会改到调用方的段落对象。
    out = [segment.clone() for segment in segments]
    for index, segment in enumerate(out):
        if segment.type not in _MEDIA_TYPES:
            continue
        if segment.name:
            segment.name = sanitize_media_name(segment.name)
        spec = Input(name=segment.name, mime_type=segment.mime_type)
        metadata: Any = None
        try:
            metadata = await _resolve_segment(manager, ctx, segment, spec, limits)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Go 把这里的**所有**失败降级成"媒体不可用"
            metadata = None
        if is_cancelled(ctx):
            # 与 Go 的唯一差异：取消不降级成文本段，而是继续往上抛，
            # 免得调用方拿着"媒体不可用"的副本继续跑完整轮对话。
            raise_if_cancelled(ctx)
        if metadata is _UNCHANGED:
            continue
        if metadata is None:
            out[index] = unavailable(segment)
            continue
        if not segment.media:
            segment.name = metadata.name
            segment.mime_type = metadata.mime_type
        segment.media = metadata.id
        segment.url = ""
        if not segment.name:
            segment.name = metadata.name
        if not segment.mime_type:
            segment.mime_type = metadata.mime_type
        out[index] = segment
    return out


async def _resolve_segment(
    manager: Any,
    ctx: Any,
    segment: MessageSegment,
    spec: Input,
    limits: ImportLimits,
) -> Any:
    """按来源形态分派一次物化。对应 Go ``MaterializeWithLimits`` 的 ``switch``。

    返回已存媒体、``None``（失败，调用方降级为"媒体不可用"）或
    :data:`_UNCHANGED`（没有来源，原样保留）。
    """
    if segment.media:
        if not valid_id(segment.media):
            raise MediaError("invalid media ID")
        return await manager.metadata(ctx, segment.media)

    url = segment.url
    if url.startswith("data:"):
        return await _import_data_url(manager, ctx, url, spec, limits)

    if url.startswith("http://") or url.startswith("https://"):
        return await manager.import_url_with_limits(ctx, url, spec, limits)

    if url:
        return await _import_local_path(manager, ctx, url, spec, limits)

    return _UNCHANGED


async def _import_data_url(
    manager: Any, ctx: Any, url: str, spec: Input, limits: ImportLimits
) -> Media:
    """处理 ``data:<mime>;base64,<body>``。对应 Go 的 ``data:`` 分支。"""
    header, separator, body = url.partition(",")
    if not separator or not header.endswith(";base64"):
        raise MediaError("invalid media data URL")
    if len(body) > (limits.max_import_bytes + 2) // 3 * 4:
        raise MediaError("media exceeds import limit")
    try:
        data = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaError(f"invalid media data URL: {exc}") from exc
    if not spec.mime_type:
        spec.mime_type = header[len("data:"):-len(";base64")]
    return await manager.import_reader_with_limit(
        ctx, data, len(data), spec, limits.max_import_bytes
    )


async def _import_local_path(
    manager: Any, ctx: Any, url: str, spec: Input, limits: ImportLimits
) -> Media:
    """处理 ``file://`` 与裸本地路径。对应 Go 的最后一个 ``case``。"""
    path = url
    if path.startswith("file://"):
        parsed = urlparse(path)
        path = unquote(parsed.path)
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if parsed.netloc and parsed.netloc != "localhost":
            raise MediaError("unsupported file URL host")
    return await manager.import_file_with_limit(
        ctx, _from_slash(path), spec, limits.max_import_bytes
    )


def _from_slash(path: str) -> str:
    """对应 Go ``filepath.FromSlash``。"""
    return path.replace("/", os.sep) if os.sep != "/" else path


async def resolve_for_llm(
    manager: Any, ctx: Any, messages: Sequence[Any]
) -> tuple[list[Any], Callable[[], None]]:
    """物化请求副本。对应 Go ``ResolveForLLM``。

    返回 ``(副本消息, cleanup)``；``cleanup`` 当前是空操作（Go 同样返回
    ``func() {}``，保留它是为了不改调用方形状）。
    """
    out = clone_messages(messages) or []
    media_by_id: dict[str, Media] = {}
    total = 0
    for index, message in enumerate(out):
        for position, segment in enumerate(message.segments):
            if segment.type in _MEDIA_TYPES and segment.name:
                out[index].segments[position].name = sanitize_media_name(segment.name)
            if not segment.media:
                continue
            metadata = media_by_id.get(segment.media)
            if metadata is None:
                try:
                    metadata = await manager.metadata(ctx, segment.media)
                except NotFoundError:
                    out[index].segments[position] = unavailable(segment)
                    continue
                media_by_id[segment.media] = metadata
            if not out[index].segments[position].name:
                out[index].segments[position].name = metadata.name
            out[index].segments[position].mime_type = metadata.mime_type
            total += metadata.size

    delivery = manager.file_delivery
    limit = delivery.max_direct_base64_bytes
    remote = delivery.backend == "s3" or (delivery.backend == "hybrid" and total > limit)
    if not remote and total > limit:
        raise MediaError(
            f"media request is {total} bytes, exceeds "
            f"file_delivery.max_direct_base64_bytes={limit}"
        )

    urls: dict[str, str] = {}
    for index, message in enumerate(out):
        for position, segment in enumerate(message.segments):
            metadata = media_by_id.get(segment.media)
            if not segment.media or metadata is None:
                continue
            value = urls.get(segment.media)
            if value is None:
                try:
                    value = await _transport_value(manager, ctx, segment.media, metadata, remote)
                except Exception:  # noqa: BLE001 - Go 在这里同样降级成文本占位
                    out[index].segments[position] = unavailable(segment)
                    continue
                urls[segment.media] = value
            out[index].segments[position].url = value
    return out, _noop_cleanup


def _noop_cleanup() -> None:
    """对应 Go ``ResolveForLLM`` 返回的空 cleanup。"""


async def _transport_value(
    manager: Any, ctx: Any, id: str, metadata: Media, remote: bool
) -> str:
    if remote:
        return await manager.presign_get(ctx, id, 3600.0)
    data, _media = await manager.read(ctx, id)
    return "data:" + metadata.mime_type + ";base64," + base64.b64encode(data).decode("ascii")
