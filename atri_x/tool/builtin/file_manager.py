"""出站文件交付的准备与体积校验。

对应 Go：``internal/tool/builtin/file_manager.go``。

``FileManager`` 是 ``send_file`` 与媒体入库工具的共享依赖：把源路径规范化、
校验类型与体积，算出最终文件名与 MIME 类型。
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.config import FileDeliveryConfig, default as default_app_config
from atri_x.errors import InvalidError
from atri_x.media import Manager, new_manager
from atri_x.tool.builtin.common import first_non_empty_string, safe_file_name


@dataclass
class PreparedFile:
    """一个已校验、可发送的文件。对应 Go ``preparedFile``。"""

    path: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0


@dataclass
class FileManager:
    """对应 Go ``FileManager``。"""

    sandbox_root: str = ""
    config: FileDeliveryConfig = field(default_factory=FileDeliveryConfig)
    media: Manager | None = None

    def prepare(self, source_path: str, name: str = "", mime_type: str = "") -> PreparedFile:
        """校验并准备一个待发送文件。对应 Go ``FileManager.Prepare``。"""
        source = os.path.normpath((source_path or "").strip())
        if source == "" or source == ".":
            raise InvalidError("path is required")
        try:
            info = os.stat(source)
        except OSError as exc:
            raise InvalidError(f"stat file {source!r}: {exc}") from exc
        if os.path.isdir(source):
            raise InvalidError(f"file {source!r} is a directory")
        if not os.path.isfile(source):
            raise InvalidError(f"file {source!r} is not a regular file")
        self._check_size(source, info.st_size)
        file_name = safe_file_name(first_non_empty_string([name, os.path.basename(source)]))
        mime_type = (mime_type or "").strip()
        if mime_type == "":
            mime_type = mimetypes.guess_type(file_name)[0] or ""
        return PreparedFile(
            path=source, name=file_name, mime_type=mime_type, size=info.st_size
        )

    def _check_size(self, source: str, size: int) -> None:
        """对应 Go ``FileManager.checkSize``。"""
        limit = self.config.max_direct_base64_bytes
        if limit <= 0 or size <= limit:
            return
        backend = (self.config.backend or "").strip()
        if backend in ("", "base64"):
            raise InvalidError(
                f"file {source!r} is {size} bytes, exceeds "
                f"file_delivery.max_direct_base64_bytes={limit}; configure future "
                "s3/r2 backend for large files"
            )
        raise InvalidError(f"file delivery backend {backend!r} is not implemented yet")


def new_file_manager(sandbox_root: str, cfg: FileDeliveryConfig) -> FileManager:
    """对应 Go ``NewFileManager``：空值回落到内置默认。"""
    defaults = default_app_config()
    if (sandbox_root or "").strip() == "":
        sandbox_root = defaults.sandbox.root
    if cfg.max_direct_base64_bytes <= 0:
        cfg.max_direct_base64_bytes = defaults.file_delivery.max_direct_base64_bytes
    if (cfg.backend or "").strip() == "":
        cfg.backend = defaults.file_delivery.backend
    if (cfg.s3_region or "").strip() == "":
        cfg.s3_region = defaults.file_delivery.s3_region
    return FileManager(sandbox_root=os.path.normpath(sandbox_root), config=cfg)


def new_file_manager_with_media(
    sandbox_root: str, cfg: FileDeliveryConfig, store: Any
) -> FileManager:
    """对应 Go ``NewFileManagerWithMedia``：附带一个落在沙盒内 ``.media`` 的媒体库。"""
    manager = new_file_manager(sandbox_root, cfg)
    if store is not None:
        root = os.path.join(manager.sandbox_root, ".media")
        # 延迟到这里的局部 import 无必要：media 是已落地依赖，直接构造本地后端。
        from atri_x.media import LocalBackend

        manager.media = new_manager(store, root, LocalBackend(root=root))
    return manager


__all__ = [name for name in dir() if not name.startswith("_")]
