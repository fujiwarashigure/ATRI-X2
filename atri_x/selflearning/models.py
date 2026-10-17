"""自学习层的领域模型与状态常量。

对应 Go：``internal/selflearning/store.go`` 的 ``Candidate`` / ``ReviewRecord`` /
``MineStats`` / ``DecideRequest`` / ``StoreLimits`` 与状态、种类常量。

按 ``PORTING.md`` §2 的「单文件 ≤ 600 行」约束，Go 的 ``store.go`` 在这里拆成
:mod:`atri_x.selflearning.models`（模型）、:mod:`atri_x.selflearning.patterns`
（候选词抽取）、:mod:`atri_x.selflearning.mining`（``Mine`` 与候选 upsert）与
:mod:`atri_x.selflearning.store`（``Store`` 其余部分）。

``review-before-apply`` 体现在 :attr:`Candidate.status`：挖掘只创建/刷新 ``pending``
候选，只有显式 :meth:`Store.decide`（或 :meth:`Store.undo`）才改变状态，并且每次改变
都往 ``candidate_reviews`` 追加一条**只增不改**的审计记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "Candidate",
    "DecideRequest",
    "KindExpression",
    "KindJargon",
    "MineStats",
    "ReviewRecord",
    "StatusApproved",
    "StatusPending",
    "StatusRejected",
    "StoreLimits",
    "TokenStat",
    "UpsertCreated",
    "UpsertResult",
    "UpsertSkipped",
    "UpsertUpdated",
]

#: 待审。对应 Go ``StatusPending``。
StatusPending = "pending"
#: 已批准。对应 Go ``StatusApproved``。
StatusApproved = "approved"
#: 已拒绝。对应 Go ``StatusRejected``。
StatusRejected = "rejected"

#: 表达方式类候选。对应 Go ``KindExpression``。
KindExpression = "expression"
#: 黑话/行话类候选（纯 ASCII 词）。对应 Go ``KindJargon``。
KindJargon = "jargon"


@dataclass
class Candidate:
    """一条可复核的表达/黑话候选。对应 Go ``Candidate``。"""

    id: str = ""
    kind: str = ""
    platform: str = ""
    scope_id: str = ""
    pattern: str = ""
    meaning: str = ""
    count: int = 0
    user_count: int = 0
    status: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str = ""


@dataclass
class ReviewRecord:
    """候选审计链里的一条记录（只增不改）。对应 Go ``ReviewRecord``。"""

    id: str = ""
    candidate_id: str = ""
    platform: str = ""
    scope_id: str = ""
    from_status: str = ""
    to_status: str = ""
    meaning: str = ""
    reviewer: str = ""
    created_at: datetime | None = None


@dataclass
class MineStats:
    """一次挖掘的统计。对应 Go ``MineStats``。

    ``created`` / ``updated`` / ``skipped`` 把"真正新增"、"刷新待审行"与"没变化或已
    裁决过"分开，``scanned`` / ``total_chars`` / ``truncated`` 报告语料被扫了多少。
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    scanned: int = 0
    total_chars: int = 0
    truncated: bool = False
    #: Go 在返回 ``context.DeadlineExceeded`` 之前会置位；Python 侧超时直接抛
    #: :class:`atri_x.safecontext.DeadlineExceededError`，本字段仅为 API 对齐保留。
    timed_out: bool = False

    def total(self) -> int:
        """``created + updated + skipped``。对应 Go ``MineStats.Total``。"""
        return self.created + self.updated + self.skipped


@dataclass
class DecideRequest:
    """定位一条候选并给出裁决。对应 Go ``DecideRequest``。"""

    id: str = ""
    platform: str = ""
    scope_id: str = ""
    status: str = ""
    meaning: str = ""
    reviewer: str = ""


@dataclass
class StoreLimits:
    """由 SQLite store 直接执行的写入/扫描上限。对应 Go ``StoreLimits``。"""

    max_observation_runes: int = 0
    max_observations_per_scope: int = 0
    max_mine_chars: int = 0


#: 候选 upsert 结果。对应 Go ``upsertResult``。
UpsertSkipped = 0
UpsertCreated = 1
UpsertUpdated = 2
UpsertResult = int


@dataclass
class TokenStat:
    """挖掘过程中的单个 token 计数（Go ``Mine`` 里的匿名 ``tokenStat``）。"""

    count: int = 0
    users: set[str] = field(default_factory=set)
