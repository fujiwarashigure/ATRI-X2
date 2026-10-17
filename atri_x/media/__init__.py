"""媒体中心（Media Center）。

对应 Go：``internal/media``（18 文件 / 1885 行）。

Go 的 ``package media`` 是单个平铺包；本仓按 ``PORTING.md`` §2 的"单文件 ≤ 700 行"
把实现拆到本包下的平级模块，并在这里统一再导出，使
``from atri_x.media import Manager, IDPrefix, valid_id`` 这样的引用不受拆分影响
（也等价于 Go 的 ``media.Xxx`` 交叉检索面）。

模块 → Go 文件对照
------------------
===================================  ===========================================
本包模块                             Go 文件
===================================  ===========================================
:mod:`~atri_x.media.manager`         ``manager.go`` + ``config.go``
:mod:`~atri_x.media.types`           ``types.go``
:mod:`~atri_x.media.config`          ``config.go``（配置映射）
:mod:`~atri_x.media.sanitize`        ``manager.go``（sanitize 部分）
:mod:`~atri_x.media.locks`           ``locks.go``
:mod:`~atri_x.media.local`           ``local.go``
:mod:`~atri_x.media.remote`          ``remote.go``
:mod:`~atri_x.media.s3`              ``s3.go``
:mod:`~atri_x.media.image`           ``image.go``
:mod:`~atri_x.media.imagecodec`      ``image`` / ``image/jpeg`` / ``image/png``（新增）
:mod:`~atri_x.media.worker`          ``worker.go`` + ``worker_process_*.go``
:mod:`~atri_x.media.generation`      ``generation.go``
:mod:`~atri_x.media.resolver`        ``resolver.go``
:mod:`~atri_x.media.lifecycle`       ``lifecycle.go``
:mod:`~atri_x.media.history`         ``history.go``
:mod:`~atri_x.media.output`          ``output.go``
:mod:`~atri_x.media.platform`        ``platform.go``
:mod:`~atri_x.media.reference`       ``reference.go``
:mod:`~atri_x.media.mimetype`        Go ``net/http`` + ``mime``（新增）
===================================  ===========================================

下游（toolrun / vision / tool.skill / hook.output / atrinis）常用的入口见各模块的
docstring；函数级的 ``PascalCase`` 别名（``ValidID`` / ``SanitizeName`` /
``PrepareVisionImage`` …）同时导出，便于按 Go 名交叉检索。
"""

from __future__ import annotations

from atri_x.errors import MediaTooLargeError
from atri_x.errors import ErrMediaTooLarge as ErrMediaTooLarge
from atri_x.media.config import (
    Config,
    FileDeliveryConfig,
    MediaConfig,
    PlatformFilesConfig,
    SandboxConfig,
    config_from_app,
    default_config,
    media_root,
    section_from,
)
from atri_x.media.generation import (
    GenerationCapture,
    GenerationSink,
    RecordingGenerationSink,
    capture_generation,
    forget_generation,
)
from atri_x.media.history import PAGE_SIZE as HISTORY_PAGE_SIZE
from atri_x.media.history import reconcile_history
from atri_x.media.image import (
    VISION_IMAGE_DEFAULT_BYTE_BUDGET,
    VISION_IMAGE_HARD_LIMIT,
    VISION_IMAGE_MAX_CONCURRENT_DECODES,
    VISION_IMAGE_MAX_INPUT_EDGE,
    VISION_IMAGE_MAX_INPUT_PIXELS,
    VisionImageInputLimit,
    compress_image,
    compress_image_context,
    compress_image_isolated,
    compress_vision_image,
    compressed_name,
    image_dimensions,
    imported_image,
    prepare_vision_image,
    prepare_vision_image_context,
    resize_image,
    should_compress_image,
    verify_vision_image,
    vision_image_mime_type,
)
from atri_x.media.imagecodec import (
    Image,
    ImageCodecError,
    ImageDecodeError,
    ImageInfo,
    UnsupportedImageError,
    backend as image_backend,
    decode as decode_image,
    encode_jpeg,
    encode_png,
    sniff as sniff_image,
    to_rgb,
)
from atri_x.media.lifecycle import (
    CLEANUP_CONCURRENCY,
    ORPHAN_GRACE,
    cleanup,
    delete_object,
)
from atri_x.media.local import LocalBackend
from atri_x.media.locks import ObjectLocks
from atri_x.media.manager import Manager, backend_name, new_configured, new_manager, read_all
from atri_x.media.mimetype import detect_content_type, mime_type_by_extension
from atri_x.media.output import metadata, resolve_for_output
from atri_x.media.platform import (
    associate_history,
    get_history_media,
    history_ids,
    history_segments,
    import_platform,
)
from atri_x.media.reference import (
    ReferencedReader,
    argument_media_ids,
    hold,
    retain_session_tool_arguments,
)
from atri_x.media.remote import LazyBackend
from atri_x.media.resolver import (
    materialize,
    materialize_with_limits,
    resolve_for_llm,
    unavailable,
)
from atri_x.media.s3 import (
    S3Backend,
    new_s3_backend,
    object_key,
    resolve_credentials,
    s3_config,
)
from atri_x.media.sanitize import (
    path_base,
    sanitize_input,
    sanitize_media_metadata,
    sanitize_media_name,
    sanitize_name,
    sanitize_source_file_id,
    sanitize_source_url,
)
from atri_x.media.types import (
    ID_HASH_LENGTH,
    ID_PREFIX,
    IDPrefix,
    Backend,
    ImportLimits,
    Input,
    MediaError,
    Source,
    is_cancelled,
    raise_if_cancelled,
    valid_id,
    wait_cancelled,
)
from atri_x.media.worker import (
    IMAGE_WORKER_ARG,
    IMAGE_WORKER_TIMEOUT,
    ImageWorkerRequest,
    ImageWorkerResponse,
    run_image_worker,
    run_isolated,
)

# 媒体中心对外交割的领域模型来自 :mod:`atri_x.storage.models`（Go 的
# ``storage.Media`` 等）；此处只是**再导出同一个对象**，不是二次定义 ——
# PORTING §3 要求跨模块类型只有一个出处，下游 ``isinstance`` 因此仍然成立。
from atri_x.storage.models import HistoryMedia, Media, MediaOutput, MediaReference

# ---------------------------------------------------------------------------
# Go 导出名的 PascalCase 别名（函数级），便于按上游名字交叉检索与集成期替换
# ---------------------------------------------------------------------------
ValidID = valid_id
SanitizeName = sanitize_name
PrepareVisionImage = prepare_vision_image
PrepareVisionImageContext = prepare_vision_image_context
VisionImageMIMEType = vision_image_mime_type
VerifyVisionImage = verify_vision_image
ImageDimensions = image_dimensions
ImportedImage = imported_image
ShouldCompressImage = should_compress_image
CompressedName = compressed_name
CompressImage = compress_image
CompressImageContext = compress_image_context
CompressImageIsolated = compress_image_isolated
CompressVisionImage = compress_vision_image
BackendName = backend_name
ObjectKey = object_key
NewS3Backend = new_s3_backend
NewConfigured = new_configured
NewManager = new_manager
ReconcileHistory = reconcile_history
Materialize = materialize
MaterializeWithLimits = materialize_with_limits
ResolveForLLM = resolve_for_llm
Cleanup = cleanup
DeleteObject = delete_object
Metadata = metadata
ResolveForOutput = resolve_for_output
ImportPlatform = import_platform
HistorySegments = history_segments
HistoryIDs = history_ids
AssociateHistory = associate_history
GetHistoryMedia = get_history_media
RetainSessionToolArguments = retain_session_tool_arguments
ArgumentMediaIDs = argument_media_ids
Hold = hold
OrphanGrace = ORPHAN_GRACE
CleanupConcurrency = CLEANUP_CONCURRENCY

__all__ = [
    "ArgumentMediaIDs",
    "AssociateHistory",
    "Backend",
    "BackendName",
    "CLEANUP_CONCURRENCY",
    "Cleanup",
    "CleanupConcurrency",
    "CompressImage",
    "CompressImageContext",
    "CompressImageIsolated",
    "CompressVisionImage",
    "CompressedName",
    "Config",
    "DeleteObject",
    "ErrMediaTooLarge",
    "FileDeliveryConfig",
    "GenerationCapture",
    "GenerationSink",
    "GetHistoryMedia",
    "HISTORY_PAGE_SIZE",
    "HistoryIDs",
    "HistoryMedia",
    "HistorySegments",
    "Hold",
    "IDPrefix",
    "ID_HASH_LENGTH",
    "ID_PREFIX",
    "IMAGE_WORKER_ARG",
    "IMAGE_WORKER_TIMEOUT",
    "Image",
    "ImageCodecError",
    "ImageDecodeError",
    "ImageDimensions",
    "ImageInfo",
    "ImageWorkerRequest",
    "ImageWorkerResponse",
    "ImportLimits",
    "ImportPlatform",
    "ImportedImage",
    "Input",
    "LazyBackend",
    "LocalBackend",
    "Manager",
    "Materialize",
    "MaterializeWithLimits",
    "Media",
    "MediaConfig",
    "MediaError",
    "MediaOutput",
    "MediaReference",
    "MediaTooLargeError",
    "Metadata",
    "NewConfigured",
    "NewManager",
    "NewS3Backend",
    "ORPHAN_GRACE",
    "ObjectKey",
    "ObjectLocks",
    "OrphanGrace",
    "PlatformFilesConfig",
    "PrepareVisionImage",
    "PrepareVisionImageContext",
    "RecordingGenerationSink",
    "ReconcileHistory",
    "ReferencedReader",
    "ResolveForLLM",
    "ResolveForOutput",
    "RetainSessionToolArguments",
    "S3Backend",
    "SandboxConfig",
    "SanitizeName",
    "ShouldCompressImage",
    "Source",
    "UnsupportedImageError",
    "VISION_IMAGE_DEFAULT_BYTE_BUDGET",
    "VISION_IMAGE_HARD_LIMIT",
    "VISION_IMAGE_MAX_CONCURRENT_DECODES",
    "VISION_IMAGE_MAX_INPUT_EDGE",
    "VISION_IMAGE_MAX_INPUT_PIXELS",
    "ValidID",
    "VerifyVisionImage",
    "VisionImageInputLimit",
    "VisionImageMIMEType",
    "argument_media_ids",
    "associate_history",
    "backend_name",
    "capture_generation",
    "cleanup",
    "compress_image",
    "compress_image_context",
    "compress_image_isolated",
    "compress_vision_image",
    "compressed_name",
    "config_from_app",
    "decode_image",
    "default_config",
    "delete_object",
    "detect_content_type",
    "encode_jpeg",
    "encode_png",
    "forget_generation",
    "get_history_media",
    "history_ids",
    "history_segments",
    "hold",
    "image_backend",
    "image_dimensions",
    "import_platform",
    "imported_image",
    "is_cancelled",
    "materialize",
    "materialize_with_limits",
    "media_root",
    "metadata",
    "mime_type_by_extension",
    "new_configured",
    "new_manager",
    "new_s3_backend",
    "object_key",
    "path_base",
    "prepare_vision_image",
    "prepare_vision_image_context",
    "raise_if_cancelled",
    "read_all",
    "reconcile_history",
    "resize_image",
    "resolve_credentials",
    "resolve_for_llm",
    "resolve_for_output",
    "retain_session_tool_arguments",
    "run_image_worker",
    "run_isolated",
    "s3_config",
    "sanitize_input",
    "sanitize_media_metadata",
    "sanitize_media_name",
    "sanitize_name",
    "sanitize_source_file_id",
    "sanitize_source_url",
    "section_from",
    "should_compress_image",
    "sniff_image",
    "to_rgb",
    "unavailable",
    "valid_id",
    "verify_vision_image",
    "vision_image_mime_type",
    "wait_cancelled",
]
