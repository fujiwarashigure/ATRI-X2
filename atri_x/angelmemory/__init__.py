"""ATRI-X 的 clean-room 长期记忆层（scope-local）。

对应 Go：``internal/angelmemory/doc.go`` + ``store.go`` + ``service.go``。

* :mod:`atri_x.angelmemory.models` —— Go ``store.go`` / ``service.go`` 的模型。
* :mod:`atri_x.angelmemory.schema` —— Go ``store.go`` 的建表、迁移与文本 helper。
* :mod:`atri_x.angelmemory.store` —— Go ``store.go`` 的 ``Store``。
* :mod:`atri_x.angelmemory.service` —— Go ``service.go`` 的 ``Service`` / ``Options``。

**来源**：写入时记录 ``source_kind`` / ``source_actor_id`` / ``source_message_id`` /
``source_session_id``；查询、前缀解析、按来源删除、召回与上下文构造都以
``platform + scope`` 为硬边界。平台撤回事件按「平台 + scope + source_message_id」
删除派生记忆（``delete_by_source(..., SourceFilter(message_id=...))``）。

**旧数据**：结构化来源出现之前的行只有自由文本 ``source``（唯一的生产写入方固定写
``"tool"``）。``backfill_legacy_source_kind`` 只回填这一个可确定的字段，绝不发明成员 /
消息 / Session id，因此这些行继续被"按来源删除"跳过。

**依赖**：只依赖标准库 ``sqlite3`` 与本仓的 ``atri_x.errors`` / ``atri_x.ratelimit`` /
``atri_x.safecontext`` / ``atri_x.textmatch`` / ``atri_x.storage``。独立于主库
（``angel_memory.db`` 是它自己的库文件），因此不使用 ``atri_x.storage.sqlite``。
"""

from __future__ import annotations

from atri_x.errors import (
    ErrAmbiguousID,
    ErrContentTooLong,
    ErrNotFound,
    ErrScopeFull,
)
from atri_x.angelmemory.models import (
    LegacySourceBackfill,
    Memory,
    RecallQuery,
    SourceFilter,
    StoreLimits,
)
from atri_x.angelmemory.schema import (
    LEGACY_TOOL_SOURCE_LABEL,
    like_prefix_pattern,
    migrate,
    normalize_tags,
    source_filter_clause,
    truncate_source_text,
)
from atri_x.angelmemory.service import (
    ANGEL_FORGET_MAX_PER_MINUTE,
    DEFAULT_MAX_CONTEXT_RUNES,
    DEFAULT_MAX_CONTENT_RUNES,
    DEFAULT_MAX_MEMORY_LINE_RUNES,
    DEFAULT_MAX_PER_SCOPE,
    DEFAULT_MAX_WRITES_PER_MINUTE,
    DefaultOptions,
    ErrWriteRateLimited,
    ForgetGuard,
    NewService,
    Options,
    Service,
    Source,
    WriteRateLimitedError,
    default_options,
    new_service,
)
from atri_x.angelmemory.store import Open, Store, open_store

__all__ = [
    "ANGEL_FORGET_MAX_PER_MINUTE",
    "DEFAULT_MAX_CONTEXT_RUNES",
    "DEFAULT_MAX_CONTENT_RUNES",
    "DEFAULT_MAX_MEMORY_LINE_RUNES",
    "DEFAULT_MAX_PER_SCOPE",
    "DEFAULT_MAX_WRITES_PER_MINUTE",
    "DefaultOptions",
    "ErrAmbiguousID",
    "ErrContentTooLong",
    "ErrNotFound",
    "ErrScopeFull",
    "ErrWriteRateLimited",
    "ForgetGuard",
    "LEGACY_TOOL_SOURCE_LABEL",
    "LegacySourceBackfill",
    "Memory",
    "NewService",
    "Open",
    "Options",
    "RecallQuery",
    "Service",
    "Source",
    "SourceFilter",
    "Store",
    "StoreLimits",
    "WriteRateLimitedError",
    "default_options",
    "like_prefix_pattern",
    "migrate",
    "new_service",
    "normalize_tags",
    "open_store",
    "source_filter_clause",
    "truncate_source_text",
]
