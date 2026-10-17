"""Atrinis：ATRI-X 的对内/对外事件入口（Atrwisp 后台任务服务）。

对应 Go：``internal/atrinis``（13 个生产文件）。本模块把各主题子模块的公开名
再导出，使 ``from atri_x.atrinis import Service, Request, StatusCompleted`` 可用，
等价于 Go 的 ``package atrinis`` 表面。

子模块与 Go 文件的对应关系：

======================  ==========================================================
本仓模块                对应 Go 文件
======================  ==========================================================
``types.py``            ``types.go``（对 ``internal/atriena`` 的再导出）
``background.py``       ``background.go``（通用小工具 + ``maybe_await``）
``store.py``            ``store.go``（终态写入、审计/日志、哈希）
``segments.py``         ``segments.go``（段校验与段换算）
``prepare.py``          ``prepare.go``（字段归一化、校验、派生列）
``targets.py``          ``targets.go``（``all`` 展开与禁用规则）
``auth.py``             ``auth.go``（令牌、atriwisp 策略、工具白名单）
``media.py``            ``media.go``（段物化、报告段导入、workspace 读写）
``direct.py``           ``direct.go``（``direct`` 模式与消息映射回填）
``llm.py``              ``llm.go``（``llm`` 模式、JSON 重试、提示词）
``outbox.py``           ``outbox.go``（汇报落库、认领、投递、恢复）
``service.py``          ``service.go``（装配与三种模式分派）
``http.py``             ``http.go``（HTTP 入口与后台运行时）
======================  ==========================================================

Go 里同一个 ``Service`` 结构体的方法分散在上述文件；Python 没有等价写法，
因此每个文件对应一个**混入类**，在 ``service.py`` 里组合成 ``Service``。
"""

from __future__ import annotations

from atri_x.atrinis.auth import AuthMixin
from atri_x.atrinis.background import (
    atrinis_actor,
    atrinis_sandbox_subdir,
    first_non_empty,
    first_non_empty_str,
    first_platform,
    is_atrinis_model_slot,
    is_atrinis_session_mode,
    maybe_await,
    unique_sorted,
)
from atri_x.atrinis.direct import DirectMixin
from atri_x.atrinis.http import (
    EVENT_PATHS,
    HEALTH_PATH,
    REPORT_RECOVERY_INTERVAL_SECONDS,
    Runtime,
    bearer_token,
    configured_timeout,
    decode_single_json,
    new_runtime,
    response_status,
)
from atri_x.atrinis.llm import LLMMixin
from atri_x.atrinis.media import MediaMixin, media_filename
from atri_x.atrinis.outbox import (
    OutboxMixin,
    output_from_json,
    output_to_json,
    receipt_from_json,
    receipt_to_json,
)
from atri_x.atrinis.prepare import (
    ATRIWISP_NAME_PATTERN,
    PrepareMixin,
    normalize_targets,
    normalized_tool_declarations,
    unique_targets,
)
from atri_x.atrinis.segments import (
    segments_content_text,
    segments_llm,
    validate_media_segment,
    validate_segment_url,
)
from atri_x.atrinis.service import (
    UNAUTHORIZED_MESSAGE,
    AuditFunc,
    EnqueueLLMFunc,
    ModelResolverFunc,
    Options,
    QueuedLLMEvent,
    SenderFunc,
    Service,
    new_service,
)
from atri_x.atrinis.store import (
    StoreMixin,
    background_tool_names,
    content_hash,
    hash_bytes,
    hash_text,
    set_from_strings,
    trim_strings,
)
from atri_x.atrinis.targets import (
    TargetsMixin,
    config_targets_to_atrinis,
    decode_resolved_targets,
    disabled_target_matches,
)
from atri_x.atrinis.types import (
    Atriwisp,
    Call,
    Event,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Request,
    Response,
    Segment,
    SegmentKind,
    SegmentKindFile,
    SegmentKindImage,
    SegmentKindText,
    StatusAccepted,
    StatusCompleted,
    StatusDelivering,
    StatusDuplicate,
    StatusFailed,
    StatusQueued,
    StatusResultReady,
    StatusRunning,
    StatusUnsupported,
    Target,
)

__all__ = [
    "ATRIWISP_NAME_PATTERN",
    "AuditFunc",
    "AuthMixin",
    "Atriwisp",
    "Call",
    "DirectMixin",
    "EVENT_PATHS",
    "EnqueueLLMFunc",
    "Event",
    "HEALTH_PATH",
    "LLMMixin",
    "MediaMixin",
    "ModeDirect",
    "ModeLLM",
    "ModeRecord",
    "ModelResolverFunc",
    "Options",
    "OutboxMixin",
    "PrepareMixin",
    "QueuedLLMEvent",
    "REPORT_RECOVERY_INTERVAL_SECONDS",
    "Request",
    "Response",
    "Runtime",
    "Segment",
    "SegmentKind",
    "SegmentKindFile",
    "SegmentKindImage",
    "SegmentKindText",
    "SenderFunc",
    "Service",
    "StatusAccepted",
    "StatusCompleted",
    "StatusDelivering",
    "StatusDuplicate",
    "StatusFailed",
    "StatusQueued",
    "StatusResultReady",
    "StatusRunning",
    "StatusUnsupported",
    "StoreMixin",
    "Target",
    "TargetsMixin",
    "UNAUTHORIZED_MESSAGE",
    "atrinis_actor",
    "atrinis_sandbox_subdir",
    "background_tool_names",
    "bearer_token",
    "config_targets_to_atrinis",
    "configured_timeout",
    "content_hash",
    "decode_resolved_targets",
    "decode_single_json",
    "disabled_target_matches",
    "first_non_empty",
    "first_non_empty_str",
    "first_platform",
    "hash_bytes",
    "hash_text",
    "is_atrinis_model_slot",
    "is_atrinis_session_mode",
    "maybe_await",
    "media_filename",
    "new_runtime",
    "new_service",
    "normalize_targets",
    "normalized_tool_declarations",
    "output_from_json",
    "output_to_json",
    "receipt_from_json",
    "receipt_to_json",
    "response_status",
    "segments_content_text",
    "segments_llm",
    "set_from_strings",
    "trim_strings",
    "unique_sorted",
    "unique_targets",
    "validate_media_segment",
    "validate_segment_url",
]
