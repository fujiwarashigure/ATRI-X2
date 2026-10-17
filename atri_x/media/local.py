"""本地内容寻址媒体后端。

对应 Go：``internal/media/local.go``。

布局与 Go 完全一致：``<root>/<hash 前 2 位>/<hash 其余 62 位>``，其中 ``hash``
是媒体 ID 去掉 ``media:`` 前缀后的 64 位 SHA-256 十六进制。

与 Go 的两处差异（都不改变可观察行为）：

* Go 用 ``os.CreateTemp`` + ``os.Rename`` 原子发布；本仓用
  ``<目标>.tmp-<uuid>`` + ``os.replace``。``os.replace`` 在两个平台上都能覆盖已存在
  的目标，因此 Go 里"rename 失败但目标已存在则视为成功"的分支不需要了。
* 文件读写是阻塞调用，直接放在 async 方法里执行。Go 在 goroutine 里做同样的
  阻塞 IO；本仓没有线程池（``PORTING.md`` §1.6 允许同步 SQLite/文件路径），
  大文件拷贝期间会占住事件循环 —— 见 ``worker.py`` 的取舍说明。
"""

from __future__ import annotations

import os
import stat
import uuid
from typing import Any, BinaryIO

from atri_x.media.types import IDPrefix, MediaError

__all__ = ["LocalBackend"]


class LocalBackend:
    """根目录下的内容寻址存储。对应 Go ``LocalBackend``。"""

    def __init__(self, root: str) -> None:
        self.root = root or ""

    # -- helper -------------------------------------------------------------
    def path(self, id: str) -> str:
        """对应 Go ``LocalBackend.path``。"""
        digest = id[len(IDPrefix):] if id.startswith(IDPrefix) else id
        return os.path.join(self.root, digest[:2], digest[2:])

    # -- Backend ------------------------------------------------------------
    async def put(
        self, ctx: Any, id: str, data: bytes, size: int, content_type: str
    ) -> str:
        """写入对象，返回落盘路径。对应 Go ``LocalBackend.Put``。"""
        target = self.path(id)
        directory = os.path.dirname(target)
        try:
            os.makedirs(directory, mode=0o755, exist_ok=True)
        except OSError as exc:
            raise MediaError(f"create media directory: {exc}") from exc
        temporary = f"{target}.tmp-{uuid.uuid4().hex}"
        try:
            with open(temporary, "wb") as handle:
                handle.write(bytes(data))
            os.replace(temporary, target)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise MediaError(f"store media: {exc}") from exc
        return target

    async def open(self, ctx: Any, media: Any) -> BinaryIO:
        """打开对象。对应 Go ``LocalBackend.Open``。"""
        path = getattr(media, "local_path", "") or self.path(media.id)
        try:
            return open(path, "rb")
        except OSError as exc:
            raise MediaError(f"open media: {exc}") from exc

    async def remove(self, ctx: Any, media: Any) -> None:
        """删除对象；不存在视为成功。对应 Go ``LocalBackend.Remove``。"""
        path = getattr(media, "local_path", "") or self.path(media.id)
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            if not os.path.exists(path):
                return
            raise MediaError(f"remove media: {exc}") from exc

    async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
        """本地后端不支持预签名 URL。对应 Go ``LocalBackend.PresignGet``。"""
        raise MediaError("local media backend does not support presigned URLs")

    # -- 测试/诊断辅助 -------------------------------------------------------
    def exists(self, id: str) -> bool:
        """对象是否存在于本地根目录（调试与测试用，Go 无对应物）。"""
        try:
            return stat.S_ISREG(os.stat(self.path(id)).st_mode)
        except OSError:
            return False
