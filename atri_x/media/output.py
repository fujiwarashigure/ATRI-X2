"""元数据读取与发送用临时导出。

对应 Go：``internal/media/output.go``。

* :func:`metadata` —— 只读元数据，不碰对象体；永远返回**清洗后的副本**。
* :func:`resolve_for_output` —— 为既有平台发送器导出**临时文件**。与
  :meth:`atri_x.media.resolver.resolve_for_llm` 的关键差别：这条路径**永远不**产生
  预签名 URL，而是把文件落到本地临时目录并把路径交给发送器；发送期间必须用返回的
  ``cleanup`` 保住引用，发完再调它。

临时目录根
----------
Go 用 ``os.MkdirTemp("", "atri-x-output-*")``（系统临时目录）。本仓默认同样走
:func:`tempfile.mkdtemp`，但允许用 ``Manager.output_root`` 覆盖：受限环境（例如本
仓的测试沙箱）不允许在新建目录里继续写文件，这时把根指到一个可写目录即可。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from typing import Any, Callable

from atri_x.delivery.delivery import Source
from atri_x.media.sanitize import sanitize_media_metadata
from atri_x.media.types import MediaError, raise_if_cancelled, valid_id
from atri_x.storage.models import Media

__all__ = ["metadata", "resolve_for_output"]

#: Go ``os.MkdirTemp`` 的模式串 ``atri-x-output-*``。
OUTPUT_DIR_PREFIX = "atri-x-output-"


async def metadata(manager: Any, ctx: Any, id: str) -> Media:
    """取媒体元数据（不读对象体）。对应 Go ``Metadata``。"""
    if not valid_id(id):
        raise MediaError("invalid media ID")
    item = manager.store.media().get(id)
    return sanitize_media_metadata(item)


async def resolve_for_output(
    manager: Any, ctx: Any, id: str
) -> tuple[Any, Callable[[], None]]:
    """导出临时文件供平台发送。对应 Go ``ResolveForOutput``。

    返回 ``(delivery.Source, cleanup)``，``cleanup`` 幂等。
    """
    raise_if_cancelled(ctx)
    release = manager.hold(id)
    try:
        directory = _make_output_dir(manager)
    except BaseException:
        release()
        raise
    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        shutil.rmtree(directory, ignore_errors=True)
        release()

    path = os.path.join(directory, "media")
    try:
        item = await manager.export(ctx, id, path)
    except BaseException:
        cleanup()
        raise
    from atri_x.delivery.delivery import Source

    return Source(path=path, mime_type=item.mime_type), cleanup


def _make_output_dir(manager: Any) -> str:
    root = getattr(manager, "output_root", "") or ""
    try:
        if root:
            os.makedirs(root, exist_ok=True)
            directory = os.path.join(root, OUTPUT_DIR_PREFIX + uuid.uuid4().hex)
            os.makedirs(directory, exist_ok=True)
            return directory
        return tempfile.mkdtemp(prefix=OUTPUT_DIR_PREFIX)
    except OSError as exc:
        raise MediaError(f"create output media directory: {exc}") from exc
