"""「内容安全」页的数据层续篇：复核队列与审计日志的读取/处置。

从 :mod:`atri_x.gui.safetyconfig` 拆出来只为守住「单文件 ≤600 行」的硬约束：
策略/词表/试扫在那边，复核与审计在这里。两者共用同一个 ``ConfigPaths``：

* 复核库 ``<配置目录>/safety/review.db``：**只读页不建库**——库不存在时返回空，
  避免"打开界面就多一个文件"；写操作（通过/驳回/过期）才碰它；
* 审计 ``<配置目录>/safety-audit.jsonl``：读尾部最多 :data:`AUDIT_LIMIT` 条，
  坏行跳过；记录里只有 sha256 指纹与脱敏预览，没有原文。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from atri_x.gui.safetyconfig import (
    ConfigPaths,
    SafetySettings,
    audit_path,
    review_db_path,
)
from atri_x.safety.audit import AuditLog
from atri_x.safety.review_queue import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ReviewQueue,
)

__all__ = [
    "AUDIT_LIMIT",
    "REVIEW_LIMIT",
    "ReviewItem",
    "expire_review_items",
    "list_review_items",
    "read_audit",
    "resolve_review_item",
    "review_stats",
]

#: 审计页一次最多展示多少条（从文件尾往前取）。
AUDIT_LIMIT = 500

#: 复核队列一次最多展示多少条。
REVIEW_LIMIT = 300


@dataclass(frozen=True)
class ReviewItem:
    """复核队列里的一条（字段与 ``review_items`` 表一致）。"""

    id: str
    created_at: str
    stage: str
    tier: str
    status: str
    action_taken: str
    provider: str
    request_id: str
    platform: str
    scope_id: str
    actor: str
    labels: tuple[str, ...]
    score: float | None
    reason: str
    preview: str
    reviewer: str
    reviewed_at: str
    note: str


def _queue(
    paths: ConfigPaths, settings: SafetySettings, *, create: bool
) -> ReviewQueue | None:
    """取复核队列；库不存在且 ``create=False`` 时返回 ``None``（只读页不建库）。"""
    path = review_db_path(paths, settings)
    if not create and not os.path.isfile(path):
        return None
    if not path:
        return None
    queue = ReviewQueue(path)
    return queue if queue.ready else None


def review_stats(paths: ConfigPaths, settings: SafetySettings) -> dict[str, int]:
    """``{状态: 条数, "total": N}``；库不存在时返回全零。"""
    queue = _queue(paths, settings, create=False)
    if queue is None:
        return {"total": 0, STATUS_PENDING: 0}
    return queue.stats()


def list_review_items(
    paths: ConfigPaths,
    settings: SafetySettings,
    *,
    status: str = STATUS_PENDING,
    limit: int = REVIEW_LIMIT,
) -> list[ReviewItem]:
    """列复核条目（``status=""`` 表示全部状态），按时间倒序。"""
    queue = _queue(paths, settings, create=False)
    if queue is None:
        return []
    rows = queue.list(status=status, limit=limit)
    return [_review_item(row) for row in rows]


def _review_item(row: Mapping[str, Any]) -> ReviewItem:
    labels = row.get("labels")
    if not isinstance(labels, (list, tuple)):
        labels = ()
    score = row.get("score")
    return ReviewItem(
        id=str(row.get("id") or ""),
        created_at=str(row.get("created_at") or ""),
        stage=str(row.get("stage") or ""),
        tier=str(row.get("tier") or ""),
        status=str(row.get("status") or ""),
        action_taken=str(row.get("action_taken") or ""),
        provider=str(row.get("provider") or ""),
        request_id=str(row.get("request_id") or ""),
        platform=str(row.get("platform") or ""),
        scope_id=str(row.get("scope_id") or ""),
        actor=str(row.get("actor") or ""),
        labels=tuple(str(item) for item in labels),
        score=float(score) if isinstance(score, (int, float)) else None,
        reason=str(row.get("reason") or ""),
        preview=str(row.get("preview") or ""),
        reviewer=str(row.get("reviewer") or ""),
        reviewed_at=str(row.get("reviewed_at") or ""),
        note=str(row.get("note") or ""),
    )


def resolve_review_item(
    paths: ConfigPaths,
    settings: SafetySettings,
    item_id: str,
    *,
    approved: bool,
    reviewer: str = "gui",
    note: str = "",
) -> bool:
    """通过 / 驳回一条待复核；库或条目不存在时返回 ``False``。"""
    queue = _queue(paths, settings, create=False)
    if queue is None:
        return False
    status = STATUS_APPROVED if approved else STATUS_REJECTED
    return queue.resolve(item_id, status=status, reviewer=reviewer, note=note)


def expire_review_items(
    paths: ConfigPaths, settings: SafetySettings, *, older_than_seconds: int = 3600
) -> int:
    """把超时未处理的 pending 条目标成 expired，返回条数。"""
    queue = _queue(paths, settings, create=False)
    if queue is None:
        return 0
    return queue.expire(older_than_seconds=older_than_seconds)


def read_audit(
    paths: ConfigPaths, settings: SafetySettings, *, limit: int = AUDIT_LIMIT
) -> list[dict[str, Any]]:
    """读审计日志尾部（按时间正序返回最后 ``limit`` 条；坏行跳过）。"""
    path = audit_path(paths, settings)
    if not path or not os.path.isfile(path):
        return []
    records = AuditLog(path).read_all()
    if limit > 0:
        records = records[-limit:]
    return records
