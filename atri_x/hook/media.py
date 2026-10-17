"""宿主 Media 能力（Hook 进程内的 Go 插件用）。

对应 Go：``internal/hook/media.go``。

这是**宿主专属**能力：它永远不序列化到 hook.v2 协议里，进程 Hook 只能通过
``media.*`` RPC 间接使用它（见 :mod:`atri_x.hook.runtime.media`）。Go Hook
共享媒体中心，而不是直接访问它的存储实现。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from atri_x.media.types import Input
from atri_x.storage.models import Media, MediaReference

__all__ = ["MediaAPI"]


@runtime_checkable
class MediaAPI(Protocol):
    """对应 Go ``hook.MediaAPI``。

    Go 侧每个方法都带 ``context.Context`` 并返回 ``error``；Python 侧 I/O 方法
    是 ``async`` 且失败抛异常。``add_reference`` / ``remove_reference`` 在
    :class:`atri_x.media.manager.Manager` 里是同步方法，这里保持同步。
    """

    async def import_url(self, ctx: Any, url: str, spec: Input) -> Media: ...

    async def import_file(self, ctx: Any, path: str, spec: Input) -> Media: ...

    async def import_reader(
        self, ctx: Any, reader: Any, size: int, spec: Input
    ) -> Media: ...

    async def import_bytes(self, ctx: Any, data: bytes, spec: Input) -> Media: ...

    async def open(self, ctx: Any, media_id: str) -> tuple[Any, Media]: ...

    async def read(self, ctx: Any, media_id: str) -> tuple[bytes, Media]: ...

    async def export(self, ctx: Any, media_id: str, destination: str) -> Media: ...

    async def metadata(self, ctx: Any, media_id: str) -> Media: ...

    async def resolve_for_llm(
        self, ctx: Any, messages: Sequence[Any]
    ) -> tuple[list[Any], Callable[[], None]]: ...

    async def resolve_for_output(
        self, ctx: Any, media_id: str
    ) -> tuple[Any, Callable[[], None]]: ...

    def add_reference(self, reference: MediaReference) -> None: ...

    def remove_reference(self, reference: MediaReference) -> None: ...
