"""按需初始化的远端（S3）后端包装。

对应 Go：``internal/media/remote.go``。

``LazyBackend`` 把每一次调用转发给 ``Manager.remote_backend(ctx)``，因此：

* S3 后端**只在第一次真正需要时**初始化（配置不可用只让这次操作失败）；
* 初始化失败**不会被缓存**成永久状态 —— 与 Go 一致，下一次调用还会再试一次
  （Go 的 ``remoteBackend`` 在 factory 失败时不写 ``m.Remote``）。

Go 的 ``backendName`` 把 ``*lazyBackend`` 归为 ``"s3"``：媒体记录在 ``s3`` 模式下
一落库就带 ``backend="s3"``，即使对象此刻还在本地。
"""

from __future__ import annotations

from typing import Any, BinaryIO

from atri_x.media.types import Backend

__all__ = ["LazyBackend"]


class LazyBackend:
    """延迟到首次使用时才解析真实后端的转发层。对应 Go ``lazyBackend``。"""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def _backend(self, ctx: Any) -> Backend:
        return await self.manager.remote_backend(ctx)

    async def put(
        self, ctx: Any, id: str, data: bytes, size: int, content_type: str
    ) -> str:
        backend = await self._backend(ctx)
        return await backend.put(ctx, id, data, size, content_type)

    async def open(self, ctx: Any, media: Any) -> BinaryIO:
        backend = await self._backend(ctx)
        return await backend.open(ctx, media)

    async def remove(self, ctx: Any, media: Any) -> None:
        backend = await self._backend(ctx)
        await backend.remove(ctx, media)

    async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
        backend = await self._backend(ctx)
        return await backend.presign_get(ctx, media, expiry)
