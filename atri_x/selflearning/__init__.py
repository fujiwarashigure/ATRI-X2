"""ATRI-X 的 clean-room 自学习层（观察 / 候选挖掘 / 先审后应用）。

对应 Go：``internal/selflearning/doc.go`` + ``store.go`` + ``service.go``。

* :mod:`atri_x.selflearning.models` —— 模型与状态常量（``Candidate`` / ``MineStats`` …）。
* :mod:`atri_x.selflearning.patterns` —— 候选词抽取与 Han/ASCII 判定。
* :mod:`atri_x.selflearning.db` —— 建表、迁移、事务与行映射。
* :mod:`atri_x.selflearning.mining` —— ``Mine`` 与候选 upsert。
* :mod:`atri_x.selflearning.store` —— Go ``store.go`` 的 ``Store``。
* :mod:`atri_x.selflearning.service` —— Go ``service.go`` 的 ``Service`` / ``Options``。

**review-before-apply**：挖掘只会创建或刷新 ``pending`` 候选；只有显式
``decide`` / ``undo`` 改变状态，且每次都往 ``candidate_reviews`` 追加一条只增不改的
审计记录。``context`` 只注入 ``approved`` 候选。

**生命周期闸门**：``set_scope_enabled_policy`` 安装的策略决定某个 platform/scope 是否
开启自学习；关闭时 ``observe`` 静默丢弃、``context`` 返回空串，其余生命周期操作抛
:data:`atri_x.errors.ErrLearningDisabled`。
"""

from __future__ import annotations

from atri_x.selflearning.db import (
    DEFAULT_MAX_MINE_CHARS,
    DEFAULT_MAX_OBSERVATIONS_PER_SCOPE,
    DEFAULT_MAX_OBSERVATION_RUNES,
)
from atri_x.selflearning.mining import (
    DEFAULT_MIN_COUNT,
    DEFAULT_MIN_USERS,
    MAX_MINE_ROWS,
    mine,
    upsert_candidate,
)
from atri_x.selflearning.models import (
    Candidate,
    DecideRequest,
    KindExpression,
    KindJargon,
    MineStats,
    ReviewRecord,
    StatusApproved,
    StatusPending,
    StatusRejected,
    StoreLimits,
    TokenStat,
    UpsertCreated,
    UpsertResult,
    UpsertSkipped,
    UpsertUpdated,
)
from atri_x.selflearning.patterns import (
    STOP_CANDIDATES,
    contains_cjk,
    extract_candidates,
    is_ascii_word,
    is_han,
    is_stop_candidate,
    is_word_rune,
)
from atri_x.selflearning.service import (
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_MAX_CONTEXT_RUNES,
    DEFAULT_MAX_LINE_RUNES,
    DEFAULT_MAX_MEANING_RUNES,
    DEFAULT_MINE_TIMEOUT,
    DEFAULT_OBSERVATION_WRITES_PER_MINUTE,
    DefaultOptions,
    NewService,
    Options,
    Service,
    default_options,
    new_service,
)
from atri_x.selflearning.store import Open, Store, open_store

__all__ = [
    "Candidate",
    "DEFAULT_CONTEXT_LIMIT",
    "DEFAULT_MAX_CONTEXT_RUNES",
    "DEFAULT_MAX_LINE_RUNES",
    "DEFAULT_MAX_MEANING_RUNES",
    "DEFAULT_MAX_MINE_CHARS",
    "DEFAULT_MAX_OBSERVATIONS_PER_SCOPE",
    "DEFAULT_MAX_OBSERVATION_RUNES",
    "DEFAULT_MIN_COUNT",
    "DEFAULT_MIN_USERS",
    "DEFAULT_MINE_TIMEOUT",
    "DEFAULT_OBSERVATION_WRITES_PER_MINUTE",
    "DecideRequest",
    "DefaultOptions",
    "KindExpression",
    "KindJargon",
    "MAX_MINE_ROWS",
    "MineStats",
    "NewService",
    "Open",
    "Options",
    "ReviewRecord",
    "STOP_CANDIDATES",
    "Service",
    "StatusApproved",
    "StatusPending",
    "StatusRejected",
    "Store",
    "StoreLimits",
    "TokenStat",
    "UpsertCreated",
    "UpsertResult",
    "UpsertSkipped",
    "UpsertUpdated",
    "contains_cjk",
    "default_options",
    "extract_candidates",
    "is_ascii_word",
    "is_han",
    "is_stop_candidate",
    "is_word_rune",
    "mine",
    "new_service",
    "open_store",
    "upsert_candidate",
]
