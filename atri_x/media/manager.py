"""媒体中心 Manager：导入、读取、导出、签名与后端选择。

对应 Go：``internal/media/manager.go``（``Manager`` struct 本体在
``types.go``，本仓一并放在这里；``NewConfigured`` 在 ``config.go``）。

媒体本体按 **SHA-256 去重**：内容 ID 是 ``media:<sha256 hex>``，因此同一份字节
只会落一个对象；命中已有记录时只刷新 ``last_accessed_at``（``touch``），并把这次
导入的生成参数补交给 sink（首次导入可能早于 sink 配置）。

模块职责划分（Go 单个 ``package media`` → 本仓多个模块）
------------------------------------------------------
``Manager`` 是唯一的对外门面；具体实现按 Go 的文件切分放在平级模块里，
``manager.py`` 只保留"真正属于 manager.go"的部分并对其他模块做一行转发：

===============  ==========================================
Go 文件          本仓模块
===============  ==========================================
``manager.go``   ``manager.py``（本模块）
``config.go``    ``config.py`` + 本模块的 ``new_configured``
``resolver.go``  ``resolver.py``
``lifecycle.go`` ``lifecycle.py``
``history.go``   ``history.py``
``output.go``    ``output.py``
``platform.go``  ``platform.py``
``reference.go`` ``reference.py``
``image.go``     ``image.py``
``worker*.go``   ``worker.py``
===============  ==========================================

调用约定（与 Go 的差异，逐条列在这里供集成期复核）
------------------------------------------------
* ``(T, error)`` → 抛异常；错误类型见 :class:`atri_x.media.types.MediaError`
  与 :class:`atri_x.errors.MediaTooLargeError`。
* ``ctx`` 保留为**第一个位置参数**并做鸭子类型处理（见
  :mod:`atri_x.media.types`）。
* 纯引用记账（``add_reference`` / ``remove_reference`` / ``hold`` /
  ``retain_session_tool_arguments``）是**同步**方法：底层是同步 SQLite 调用，
  且 ``hold`` 的释放函数必须能在同步的清理路径里调用（``PORTING.md`` §1.6）。
* ``Go 的 Put(io.Reader)`` 收成 ``bytes``（见 :class:`atri_x.media.types.Backend`）。
* ``Go 的 DownloadTimeout`` 收成秒（``float``）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import stat
from typing import Any, BinaryIO, Callable, Sequence
from urllib.parse import urlsplit

from atri_x.errors import MediaTooLargeError, NotFoundError
from atri_x.media import history as history_module
from atri_x.media import lifecycle as lifecycle_module
from atri_x.media import output as output_module
from atri_x.media import platform as platform_module
from atri_x.media import reference as reference_module
from atri_x.media import resolver as resolver_module
from atri_x.media.config import Config, FileDeliveryConfig, default_config, media_root
from atri_x.media.generation import (
    GenerationCapture,
    GenerationSink,
    capture_generation,
)
from atri_x.media.image import (
    compress_image_isolated,
    compressed_name,
    imported_image,
    should_compress_image,
)
from atri_x.media.local import LocalBackend
from atri_x.media.locks import ObjectLocks
from atri_x.media.mimetype import detect_content_type, extension_of, mime_type_by_extension
from atri_x.media.remote import LazyBackend
from atri_x.media.s3 import S3Backend, new_s3_backend
from atri_x.media.sanitize import (
    path_base,
    sanitize_input,
    sanitize_media_metadata,
)
from atri_x.media.types import (
    IDPrefix,
    Backend,
    ImportLimits,
    Input,
    MediaError,
    raise_if_cancelled,
    valid_id,
)
from atri_x.net.httpclient import HTTPClient, HTTPError
from atri_x.storage.models import Media, MediaReference
from atri_x.storage.times import now as storage_now

__all__ = ["Manager", "backend_name", "new_configured", "new_manager", "read_all", "valid_id"]

#: ``download_timeout <= 0`` 时的兜底：Go 的 ``http.Client{Timeout: 0}`` 表示不超时，
#: 而 Python 的 ``socket.settimeout(0)`` 会切成非阻塞模式，所以这里换成一个
#: 实际上"永不触发"的有限值。
_UNBOUNDED_TIMEOUT = 86400.0


def read_all(reader: BinaryIO, limit: int = 0) -> bytes:
    """把可读流读空（``limit > 0`` 时最多读 ``limit`` 字节）。"""
    if limit <= 0:
        return reader.read()
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = reader.read(min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class Manager:
    """媒体中心。对应 Go ``media.Manager``。"""

    def __init__(
        self,
        store: Any,
        root: str,
        backend: Backend | None = None,
        *,
        config: Config | None = None,
        now: Callable[[], Any] | None = None,
        logger: Any = None,
    ) -> None:
        cfg = config or default_config()
        root = os.path.normpath(root) if root else root
        local: Backend = LocalBackend(root)
        if backend is not None and backend_name(backend) == "local":
            local = backend
        self.objects = ObjectLocks()
        self.cleanup_lock = asyncio.Lock()
        self.local = local
        self.store = store
        #: 可选的聊天历史仓储（跨库对账用；``None`` 时跳过对账）。
        self.history: Any = None
        #: Go 的 ``Backend`` 为 nil 时会在 Put 上 panic；这里回落到本地后端。
        self.backend: Backend = backend if backend is not None else local
        self.remote: Backend | None = None
        self.remote_factory: Callable[[Any], Backend] | None = None
        self.remote_lock = asyncio.Lock()
        self.max_import_bytes = cfg.max_import_bytes
        self.download_timeout = cfg.download_timeout
        self.root = root
        self.guard: Any = None
        self.generation: GenerationSink | None = None
        self.file_delivery: FileDeliveryConfig = cfg.file_delivery
        self.media = cfg.media
        self.platform_files = cfg.platform_files
        self.logger = logger
        self.now: Callable[[], Any] = now or storage_now
        #: Python 侧追加：``ResolveForOutput`` 的临时目录根。
        #: 空串 = 系统临时目录（与 Go 的 ``os.MkdirTemp("")`` 一致）。
        #: 某些受限环境（含本仓的测试沙箱）不允许在新目录里继续写文件，
        #: 这时把它指向一个可写根目录即可。
        self.output_root = ""

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    async def import_bytes(self, ctx: Any, data: bytes, spec: Input) -> Media:
        """导入内存字节。对应 Go ``ImportBytes``。"""
        return await self.import_reader(ctx, data, len(data), spec)

    async def import_url(self, ctx: Any, raw_url: str, spec: Input) -> Media:
        """下载并导入。对应 Go ``ImportURL``。"""
        return await self.import_url_with_limits(
            ctx,
            raw_url,
            spec,
            ImportLimits(
                max_import_bytes=self.max_import_bytes,
                download_timeout=self.download_timeout,
            ),
        )

    async def import_url_with_limits(
        self, ctx: Any, raw_url: str, spec: Input, limits: ImportLimits
    ) -> Media:
        """对应 Go ``Manager.importURL``（带单次调用上限）。"""
        raise_if_cancelled(ctx)
        text = (raw_url or "").strip()
        parsed = _parse_http_url(text)
        timeout = limits.download_timeout if limits.download_timeout > 0 else self.download_timeout
        if timeout <= 0:
            timeout = _UNBOUNDED_TIMEOUT
        client = HTTPClient(timeout=timeout)
        try:
            response = await client.get(text, cancel=ctx)
        except HTTPError as exc:
            raise MediaError(f"download media: {exc}") from exc
        if response.status < 200 or response.status >= 300:
            raise MediaError(f"download media: unexpected HTTP status {response.status}")
        if not spec.source.url:
            spec.source.url = text
        if not spec.mime_type:
            spec.mime_type = response.header("Content-Type")
        if not spec.name:
            spec.name = path_base(parsed.path)
        body = response.body
        return await self.import_reader_with_limit(
            ctx, body, len(body), spec, limits.max_import_bytes
        )

    async def import_file(self, ctx: Any, path: str, spec: Input) -> Media:
        """导入本地文件。对应 Go ``ImportFile``。"""
        return await self.import_file_with_limit(ctx, path, spec, self.max_import_bytes)

    async def import_file_with_limit(
        self, ctx: Any, path: str, spec: Input, max_bytes: int
    ) -> Media:
        """对应 Go ``Manager.importFile``。"""
        raise_if_cancelled(ctx)
        try:
            handle = open(path, "rb")
        except OSError as exc:
            raise MediaError(f"open media source: {exc}") from exc
        try:
            try:
                info = os.fstat(handle.fileno())
            except OSError as exc:
                raise MediaError(f"stat media source: {exc}") from exc
            if not stat.S_ISREG(info.st_mode):
                raise MediaError("media source is not a regular file")
            if not spec.name:
                spec.name = os.path.basename(path)
            return await self.import_reader_with_limit(ctx, handle, info.st_size, spec, max_bytes)
        finally:
            handle.close()

    async def import_reader(self, ctx: Any, input: Any, size: int, spec: Input) -> Media:
        """导入任意可读流/字节。对应 Go ``ImportReader``。"""
        return await self.import_reader_with_limit(
            ctx, input, size, spec, self.max_import_bytes
        )

    async def import_reader_with_limit(
        self, ctx: Any, input: Any, size: int, spec: Input, max_bytes: int
    ) -> Media:
        """对应 Go ``Manager.importReader``（``ImportReader`` 的核心）。

        顺序与 Go 逐字一致，这里只强调两个**不能动**的接缝：

        1. 生成参数采集在**压缩之前**（压缩会销毁 PNG 文本块与 EXIF/XMP）；
        2. 采集记录在**哈希之后**落盘（媒体 ID 那时才存在）。
        """
        raise_if_cancelled(ctx)
        if size > max_bytes:
            raise MediaError(f"media exceeds import limit of {max_bytes} bytes")
        data = _read_input(input, max_bytes + 1)
        size = len(data)
        if size > max_bytes:
            raise MediaError(f"media exceeds import limit of {max_bytes} bytes")
        spec = sanitize_input(spec)
        if not spec.mime_type:
            spec.mime_type = mime_type_by_extension(extension_of(spec.name))
        if not spec.mime_type:
            spec.mime_type = detect_content_type(data)

        if self.guard is not None:
            level, critical = await self.guard.check(ctx)
            if self.guard.last_error is not None:
                self._warn(
                    "disk guard check failed", level=level, error=self.guard.last_error
                )
            if critical:
                guard_error = self.guard.error(level)
                raise guard_error if guard_error is not None else MediaError(
                    "disk space is critically low; refusing non-essential media writes"
                )

        is_image = imported_image(spec.mime_type, data)
        capture_image = self.generation is not None and is_image
        original: bytes | None = data
        source_name = spec.name
        source_mime = spec.mime_type
        source_size = size
        transcoded = False
        if is_image and should_compress_image(data, self.media):
            data = await compress_image_isolated(
                ctx,
                data,
                self.media.llm_image_compression_threshold_bytes,
                self.media.llm_image_max_length,
            )
            size = len(data)
            spec.name = compressed_name(spec.name)
            spec.mime_type = "image/jpeg"
            transcoded = True
        if not capture_image:
            # 只有采集需要转码前的字节；丢掉引用才能保证非图片导入不会把它们交给 sink。
            original = None
        id = IDPrefix + hashlib.sha256(data).hexdigest()
        capture = GenerationCapture(
            media_id=id,
            source_name=source_name,
            source_mime=source_mime,
            stored_mime=spec.mime_type,
            source_size=source_size,
            stored_size=size,
            transcoded=transcoded,
            original=original,
        )
        if original is not None:
            capture.source_hash = hashlib.sha256(original).hexdigest()

        unlock = await self.objects.acquire(ctx, id)
        try:
            existing = self._get_media(id)
            if existing is not None:
                if existing.deleting:
                    raise MediaError("media is being deleted")
                self.store.media().touch(id, self.now())
                # 再次导入同一份字节，正是补交首次导入没能落下的记录的机会：
                # 那时 sink 可能还没配置，或者它的记录已经被淘汰。
                await capture_generation(self, ctx, capture)
                return sanitize_media_metadata(existing)
            media = Media(
                id=id,
                name=spec.name,
                mime_type=spec.mime_type,
                size=size,
                backend=backend_name(self.backend),
                source_platform=spec.source.platform,
                source_url=spec.source.url,
                source_file_id=spec.source.file_id,
            )
            location = await self.backend.put(ctx, id, data, size, media.mime_type)
            if media.backend == "s3":
                media.object_key = location
            else:
                media.local_path = location
            try:
                self.store.media().upsert(media)
            except Exception:
                await self._remove_quietly(ctx, media)
                raise
            await capture_generation(self, ctx, capture)
            return media
        finally:
            unlock()

    # ------------------------------------------------------------------
    # 读取与导出
    # ------------------------------------------------------------------
    async def open(self, ctx: Any, id: str) -> tuple[Any, Media]:
        """打开媒体，返回 ``(reader, metadata)``。对应 Go ``Open``。

        ``reader`` 必须 ``close()``：它同时释放读取期间持有的临时引用。
        """
        if not valid_id(id):
            raise MediaError(f"invalid media ID {id!r}")
        media = self.store.media().get(id)
        if media.deleting:
            raise MediaError("media is being deleted")
        backend = await self.backend_for_stored_media(ctx, media)
        self.store.media().touch(id, self.now())
        release = self.hold(id)
        try:
            stream = await backend.open(ctx, media)
        except BaseException:
            release()
            raise
        return reference_module.ReferencedReader(stream, release), sanitize_media_metadata(media)

    async def read(self, ctx: Any, id: str) -> tuple[bytes, Media]:
        """读出全部字节。对应 Go ``Read``。"""
        reader, media = await self.open(ctx, id)
        try:
            data = read_all(reader)
        except Exception as exc:  # noqa: BLE001
            _close_quietly(reader)
            raise MediaError(f"read media {id!r}: {exc}") from exc
        _close_quietly(reader)
        return data, media

    async def read_limited(self, ctx: Any, id: str, max_bytes: int) -> tuple[bytes, Media]:
        """带读取预算的 :meth:`read`。对应 Go ``ReadLimited``。

        超预算抛 :class:`atri_x.errors.MediaTooLargeError`（Go 的
        ``media.ErrMediaTooLarge``），让调用方能在**加载进内存之前**拒绝超大对象。
        元数据说够但流更长时，靠 ``max_bytes + 1`` 的流上兜底同样会拒绝。
        """
        if max_bytes <= 0:
            return await self.read(ctx, id)
        reader, media = await self.open(ctx, id)
        try:
            if media is not None and media.size > max_bytes:
                raise MediaTooLargeError(
                    f"media {id!r} is {media.size} bytes, over the {max_bytes} byte limit"
                )
            data = read_all(reader, max_bytes + 1)
        except MediaTooLargeError:
            _close_quietly(reader)
            raise
        except Exception as exc:  # noqa: BLE001
            _close_quietly(reader)
            raise MediaError(f"read media {id!r}: {exc}") from exc
        _close_quietly(reader)
        if len(data) > max_bytes:
            raise MediaTooLargeError(
                f"media {id!r} exceeds the {max_bytes} byte limit"
            )
        return data, media

    async def export(self, ctx: Any, id: str, destination: str) -> Media:
        """导出到目标路径。对应 Go ``Export``。"""
        reader, media = await self.open(ctx, id)
        try:
            with open(destination, "wb") as target:
                while True:
                    chunk = reader.read(1 << 20)
                    if not chunk:
                        break
                    target.write(chunk)
        except MediaError:
            raise
        except OSError as exc:
            raise MediaError(f"export media: {exc}") from exc
        finally:
            _close_quietly(reader)
        return media

    # ------------------------------------------------------------------
    # 引用记账（同步：纯 SQLite 调用）
    # ------------------------------------------------------------------
    def add_reference(self, reference: MediaReference) -> None:
        """新增引用。对应 Go ``AddReference``（省略 ctx，见模块 docstring）。"""
        if reference is None or not valid_id(reference.media_id):
            raise MediaError("invalid media reference")
        if self._get_media(reference.media_id) is None:
            raise NotFoundError(f"media not found: {reference.media_id!r}")
        self.store.media_references().add(reference)

    def remove_reference(self, reference: MediaReference) -> None:
        """删除引用。对应 Go ``RemoveReference``。"""
        if not valid_id(reference.media_id):
            raise MediaError("invalid media reference")
        self.store.media_references().remove(reference)

    def hold(self, id: str) -> Callable[[], None]:
        """持有一个临时引用。对应 Go ``Hold``。

        返回幂等的 ``release``：调用方在读取/发送期间用它把引用留到操作结束，
        而不是依赖持久化 owner。
        """
        return reference_module.hold(self, id)

    def retain_session_tool_arguments(self, session_id: str, arguments: str) -> None:
        """把工具参数里的媒体 ID 关联到 Session。对应 Go ``RetainSessionToolArguments``。"""
        return reference_module.retain_session_tool_arguments(self, session_id, arguments)

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------
    async def presign_get(self, ctx: Any, id: str, expiry: float = 3600.0) -> str:
        """生成限时下载 URL。对应 Go ``PresignGet``。

        存的是本地对象而当前配置要求远端投递时，这里会把对象**提升**到 S3 并
        把 ``object_key`` 写回记录（``backend`` 保持 ``local``，与 Go 一致）。
        """
        if not valid_id(id):
            raise MediaError(f"invalid media ID {id!r}")
        if expiry <= 0:
            expiry = 3600.0
        unlock = await self.objects.acquire(ctx, id)
        try:
            release = self.hold(id)
            try:
                metadata = self.store.media().get(id)
                if metadata.deleting:
                    raise MediaError("media is being deleted")
                backend = await self.remote_backend(ctx)
                if not metadata.object_key and metadata.backend != "s3":
                    reader, _media = await self.open(ctx, id)
                    try:
                        metadata.object_key = await backend.put(
                            ctx, id, read_all(reader), metadata.size, metadata.mime_type
                        )
                    finally:
                        _close_quietly(reader)
                    self.store.media().upsert(metadata)
                return await backend.presign_get(ctx, metadata, expiry)
            finally:
                release()
        finally:
            unlock()

    # ------------------------------------------------------------------
    # 后端选择
    # ------------------------------------------------------------------
    async def backend_for_stored_media(self, ctx: Any, media: Media) -> Backend:
        """按记录里的实际存储位置选后端。对应 Go ``backendForStoredMedia``。"""
        kind = getattr(media, "backend", "")
        if kind == "local":
            if self.local is not None:
                return self.local
            return LocalBackend(self.root)
        if kind == "s3":
            return await self.remote_backend(ctx)
        raise MediaError(f"unsupported stored media backend {kind!r}")

    async def remote_backend(self, ctx: Any) -> Backend:
        """按需初始化远端后端。对应 Go ``remoteBackend``。

        初始化失败**不缓存**：下一次调用还会再试。失败只让这次远端操作失败。
        """
        async with self.remote_lock:
            if self.remote is not None:
                return self.remote
            if self.remote_factory is None:
                raise MediaError("remote media backend is unavailable")
            try:
                backend = self.remote_factory(ctx)
                if asyncio.iscoroutine(backend):
                    backend = await backend
            except Exception as exc:  # noqa: BLE001
                self._warn("initialize S3 media backend failed", error=exc)
                raise
            self.remote = backend
            return backend

    # ------------------------------------------------------------------
    # 其他模块的转发（一行一个，签名与 Go 一一对应）
    # ------------------------------------------------------------------
    async def metadata(self, ctx: Any, id: str) -> Media:
        """取媒体元数据（不读对象体）。对应 Go ``Metadata``。"""
        return await output_module.metadata(self, ctx, id)

    async def resolve_for_output(self, ctx: Any, id: str) -> tuple[Any, Callable[[], None]]:
        """导出临时文件供平台发送。对应 Go ``ResolveForOutput``。"""
        return await output_module.resolve_for_output(self, ctx, id)

    async def materialize(self, ctx: Any, segments: Sequence[Any]) -> list[Any]:
        """把新的入站媒体来源换成稳定引用。对应 Go ``Materialize``。"""
        return await resolver_module.materialize(self, ctx, segments)

    async def materialize_with_limits(
        self, ctx: Any, segments: Sequence[Any], limits: ImportLimits
    ) -> list[Any]:
        """带单次导入上限的 :meth:`materialize`。对应 Go ``MaterializeWithLimits``。"""
        return await resolver_module.materialize_with_limits(self, ctx, segments, limits)

    async def resolve_for_llm(self, ctx: Any, messages: Sequence[Any]) -> tuple[list[Any], Callable[[], None]]:
        """物化 LLM 请求副本。对应 Go ``ResolveForLLM``。"""
        return await resolver_module.resolve_for_llm(self, ctx, messages)

    async def cleanup(self, ctx: Any = None) -> None:
        """一轮媒体清理。对应 Go ``Cleanup``。"""
        return await lifecycle_module.cleanup(self, ctx)

    async def reconcile_history(self, ctx: Any = None) -> None:
        """跨库历史 owner 对账。对应 Go ``ReconcileHistory``。"""
        return await history_module.reconcile_history(self, ctx)

    async def import_platform(
        self, ctx: Any, platform_name: str, resolver: Any, segment: Any
    ) -> Media:
        """导入平台媒体段。对应 Go ``ImportPlatform``。"""
        return await platform_module.import_platform(self, ctx, platform_name, resolver, segment)

    async def history_ids(self, ctx: Any, row: Any) -> dict[int, str]:
        """历史消息位序 → 本地媒体 ID。对应 Go ``HistoryIDs``。"""
        return await platform_module.history_ids(self, ctx, row)

    async def associate_history(
        self, ctx: Any, row: Any, index: int, kind: str, id: str
    ) -> None:
        """记录一条历史媒体位置。对应 Go ``AssociateHistory``。"""
        return await platform_module.associate_history(self, ctx, row, index, kind, id)

    async def get_history_media(
        self, ctx: Any, row: Any, index: int, resolver: Any
    ) -> Media:
        """按历史位序取（必要时下载）媒体。对应 Go ``GetHistoryMedia``。"""
        return await platform_module.get_history_media(self, ctx, row, index, resolver)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _get_media(self, id: str) -> Media | None:
        """取媒体行；不存在返回 ``None``（其他错误继续抛）。"""
        try:
            return self.store.media().get(id)
        except NotFoundError:
            return None

    async def _remove_quietly(self, ctx: Any, media: Media) -> None:
        try:
            await self.backend.remove(ctx, media)
        except Exception as exc:  # noqa: BLE001 - 回滚失败不掩盖原始错误
            self._warn("rollback media object failed", media_id=media.id, error=exc)

    def _warn(self, message: str, **fields: Any) -> None:
        logger = self.logger
        if logger is None:
            return
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        try:
            logger.warning("%s %s", message, suffix)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------


def backend_name(backend: Any) -> str:
    """后端名字（``local`` / ``s3``）。对应 Go ``backendName``。"""
    if isinstance(backend, (S3Backend, LazyBackend)):
        return "s3"
    return "local"


def new_manager(
    store: Any,
    root: str,
    backend: Backend | None = None,
    *,
    config: Config | None = None,
    now: Callable[[], Any] | None = None,
    logger: Any = None,
) -> Manager:
    """对应 Go ``NewManager``。

    ``Manager.__init__`` 已经承担这个职责，本函数保留 Go 的名字便于交叉检索。
    """
    return Manager(store, root, backend, config=config, now=now, logger=logger)


def new_configured(
    store: Any,
    root: str,
    file_delivery: FileDeliveryConfig | None = None,
    credentials: Any = None,
    *,
    config: Config | None = None,
    now: Callable[[], Any] | None = None,
    logger: Any = None,
) -> Manager:
    """共享一个本地内容库与一个可选的懒加载 S3 后端。对应 Go ``NewConfigured``。

    * ``base64``：只用本地库。
    * ``s3``：新对象直接写远端（``backend`` 记为 ``s3``），远端后端**仍按需初始化**。
    * ``hybrid``：新对象写本地，只有 LLM 请求总量超阈值时才提升到远端。

    Go 的 ``ctx`` 参数实际未被使用（``_ context.Context``），这里直接省略。
    凭据不可用**不阻止构造**：只有真正需要远端时 :meth:`Manager.remote_backend`
    才报错。
    """
    cfg = copy.deepcopy(config) if config is not None else default_config()
    if file_delivery is not None:
        cfg.file_delivery = _apply_file_delivery_defaults(copy.deepcopy(file_delivery), cfg.file_delivery)
    base_root = cfg.root or media_root(cfg) or root
    manager = Manager(
        store,
        base_root or root,
        LocalBackend(base_root or root),
        config=cfg,
        now=now,
        logger=logger,
    )
    mode = cfg.file_delivery.backend or "base64"
    if mode == "base64":
        return manager
    if mode in ("s3", "hybrid"):
        delivery = cfg.file_delivery

        def factory(_ctx: Any, delivery: FileDeliveryConfig = delivery) -> Backend:
            return new_s3_backend(delivery, credentials)

        manager.remote_factory = factory
        if mode == "s3":
            manager.backend = LazyBackend(manager)
        return manager
    raise MediaError(f"unsupported media backend {mode!r}")


def _apply_file_delivery_defaults(
    cfg: FileDeliveryConfig, defaults: FileDeliveryConfig
) -> FileDeliveryConfig:
    """对应 Go ``NewConfigured`` 里的三个 ``if ... == ""``/``<= 0`` 兜底。"""
    if not cfg.backend:
        cfg.backend = defaults.backend or "base64"
    if cfg.max_direct_base64_bytes <= 0:
        cfg.max_direct_base64_bytes = defaults.max_direct_base64_bytes
    if not cfg.s3_region:
        cfg.s3_region = defaults.s3_region or "auto"
    return cfg


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _parse_http_url(value: str) -> Any:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise MediaError("invalid media URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise MediaError("invalid media URL")
    return parsed


def _read_input(input: Any, limit: int) -> bytes:
    """按 Go ``io.ReadAll(io.LimitReader(...))`` 的语义读入导入源。"""
    if isinstance(input, (bytes, bytearray, memoryview)):
        return bytes(input[:limit])
    reader = getattr(input, "read", None)
    if not callable(reader):
        raise MediaError("media import source is not readable")
    return read_all(input, limit)


def _close_quietly(reader: Any) -> None:
    try:
        reader.close()
    except Exception:  # noqa: BLE001 - Go 的 defer Close() 同样丢弃错误
        pass
