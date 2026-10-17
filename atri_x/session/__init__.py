"""会话生命周期服务。对应 Go 包：``internal/session``（``service.go`` / ``types.go`` / ``shared.go`` /
``mode.go`` / ``lifecycle.go`` / ``query.go`` / ``fork.go`` / ``expiration.go`` /
``promotion.go`` / ``naming.go``）。

包内按 Go 文件拆分模块，:class:`atri_x.session.service.Service` 组合各模块的 mixin：

======================  ==============================================
模块                     对应 Go 文件
======================  ==============================================
``service.py``           ``service.go``（当前会话、创建、恢复、访问判定）
``types.py``             ``types.go``（``Scope`` 与请求/结果/事件类型）
``text.py``              ``service.go`` 的文本 helper（为打破循环依赖拆出）
``shared.py``            ``shared.go``（群共享线程）
``mode.py``              ``mode.go``
``lifecycle.py``         ``lifecycle.go``
``query.py``             ``query.go``
``fork.py``              ``fork.go``
``expiration.py``        ``expiration.go``
``promotion.py``         ``promotion.go``
``naming.py``            ``naming.go``
======================  ==============================================

`Scope` 是 ``PORTING.md`` §3 共享词汇表里属于本包的唯一出口，两种 import 路径
（``atri_x.session.Scope`` / ``atri_x.session.types.Scope``）指向同一个类。
"""

from __future__ import annotations

from atri_x.session.expiration import (  # noqa: F401
    ExpirationMixin,
    ExpireIdleRequest,
    ExpireIdleResult,
    IdleExpirationConfig,
    is_group_scope,
    scope_ttl_minutes,
)
from atri_x.session.fork import ForkMixin  # noqa: F401
from atri_x.session.lifecycle import LifecycleMixin, rename_metadata  # noqa: F401
from atri_x.session.mode import ModeMixin, validate_mode  # noqa: F401
from atri_x.session.naming import (  # noqa: F401
    MAX_NAMING_FAILURES,
    NamingMixin,
    NamingState,
    NoopNamingNotifier,
    fallback_title,
    filter_conversation_messages,
    invalid_title_reason,
    is_placeholder_title,
    llm_call_status,
    normalize_title,
    title_renamed_in_metadata,
)
from atri_x.session.promotion import (  # noqa: F401
    BACKGROUND_KIND_KEY,
    FOREGROUND_ORIGIN_KEY,
    ForegroundOrigin,
    background_kind,
    encode_metadata_fields,
    is_background,
    promote_to_foreground,
    session_metadata_fields,
    was_promoted,
)
from atri_x.session.query import QueryMixin  # noqa: F401
from atri_x.session.service import (  # noqa: F401
    Service,
    default_title,
    fork_title,
    new_service,
    new_service_with_config,
    new_service_with_naming,
    preview,
)
from atri_x.session.shared import (  # noqa: F401
    SHARED_THREAD_OWNER_PREFIX,
    is_shared_thread_session,
    session_owner_id,
    shared_thread_metadata_for_scope,
    shared_thread_owner_id,
)
from atri_x.session.types import (  # noqa: F401
    ActivateModeRequest,
    ActivateModeResult,
    Config,
    CreateRequest,
    NamingCompletedEvent,
    NamingConfig,
    NamingFailedEvent,
    NamingNotifier,
    NamingScheduledEvent,
    Scope,
    Status,
    TitleGenerator,
    TitleResult,
)

__all__ = [name for name in dir() if not name.startswith("_")]
