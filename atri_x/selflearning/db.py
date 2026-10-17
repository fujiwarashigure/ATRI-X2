"""自学习库的建表、迁移与行映射。

对应 Go：``internal/selflearning/store.go`` 的 ``Open``（建表/补列部分）与
``scanCandidates``，以及 ``service.go`` 里 store 侧使用的默认上限
（``defaultMaxObservationRunes`` / ``defaultMaxObservationsPerScope`` /
``defaultMaxMineChars``）。

拆出本模块只为满足 ``PORTING.md`` §2 的单文件行数约束，并让
:mod:`atri_x.selflearning.store` 与 :mod:`atri_x.selflearning.mining` 共用同一份
事务 helper 与行映射，避免两处 SQL 漂移。

SQLite 表结构（与 Go 的 ``Open`` 逐列对应）::

    observations(id, platform, scope_id, user_id, text, created_at)
    candidates(id, kind, platform, scope_id, pattern, meaning, count,
               user_count, status, created_at, updated_at, reviewed_at, reviewed_by,
               UNIQUE(kind, platform, scope_id, pattern))
    candidate_reviews(id, candidate_id, platform, scope_id, from_status,
                      to_status, meaning, reviewer, created_at)

``user_count`` / ``reviewed_at`` / ``reviewed_by`` 是后加的列，所以 ``migrate``
建表后逐列 ``ALTER TABLE ADD COLUMN``，并忽略 "duplicate column" 错误（与 Go 一致）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from atri_x.selflearning.models import Candidate, ReviewRecord

__all__ = [
    "DEFAULT_MAX_MINE_CHARS",
    "DEFAULT_MAX_OBSERVATIONS_PER_SCOPE",
    "DEFAULT_MAX_OBSERVATION_RUNES",
    "SELECT_CANDIDATE_COLUMNS",
    "migrate",
    "row_to_candidate",
    "row_to_review",
    "transaction",
]

#: 单条观察的 rune 上限。对应 Go ``defaultMaxObservationRunes``。
DEFAULT_MAX_OBSERVATION_RUNES = 1000
#: 单 scope 观察行数上限。对应 Go ``defaultMaxObservationsPerScope``。
DEFAULT_MAX_OBSERVATIONS_PER_SCOPE = 5000
#: 一次挖掘扫描的总字符预算。对应 Go ``defaultMaxMineChars``。
DEFAULT_MAX_MINE_CHARS = 200000

SELECT_CANDIDATE_COLUMNS = (
    "SELECT id, kind, platform, scope_id, pattern, meaning, count, user_count, status,"
    " created_at, updated_at, reviewed_at, reviewed_by FROM candidates"
)

_CREATE_STATEMENTS = """
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selflearning_observations_scope_created
ON observations(platform, scope_id, created_at);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    platform TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    pattern TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 0,
    user_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    UNIQUE(kind, platform, scope_id, pattern)
);
CREATE INDEX IF NOT EXISTS idx_selflearning_candidates_scope_status
ON candidates(kind, platform, scope_id, status, count);

CREATE TABLE IF NOT EXISTS candidate_reviews (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '',
    reviewer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selflearning_reviews_candidate
ON candidate_reviews(candidate_id, created_at);
"""

#: 后续追加的候选列（Go ``Open`` 里的三条 ALTER）。
_ADDED_COLUMNS: tuple[str, ...] = (
    "ALTER TABLE candidates ADD COLUMN user_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN reviewed_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT ''",
)


def migrate(connection: sqlite3.Connection) -> None:
    """建表 + 补列。对应 Go ``Open`` 里的建表与 ALTER 序列。"""
    connection.executescript(_CREATE_STATEMENTS)
    for statement in _ADDED_COLUMNS:
        try:
            connection.execute(statement)
        except sqlite3.OperationalError as exc:
            # 旧库已经补过这一列时按幂等处理（Go 判的是 "duplicate column"）。
            if "duplicate column" not in str(exc).lower():
                raise


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：异常一律回滚。对应 Go 的 ``BeginTx`` + ``defer Rollback``。

    调用方负责用 store 级的可重入锁把整个「读-判断-写」序列串行化：SQLite 只保证
    单条语句原子，Go 侧靠 ``SetMaxOpenConns(1)`` 加显式事务达到同样的效果。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - 回滚失败时保留原始异常
            pass
        raise
    connection.execute("COMMIT")


def _parse_optional(raw: Any):
    from atri_x.storage.times import parse_time

    if raw is None:
        return None
    text = str(raw)
    if text.strip() == "":
        return None
    try:
        return parse_time(text)
    except ValueError:
        return None


def row_to_candidate(row: Any) -> Candidate:
    """把一行 ``SELECT_CANDIDATE_COLUMNS`` 映射成 :class:`Candidate`。"""
    candidate = Candidate(
        id=row[0],
        kind=row[1],
        platform=row[2],
        scope_id=row[3],
        pattern=row[4],
        meaning=row[5] or "",
        count=int(row[6] or 0),
        user_count=int(row[7] or 0),
        status=row[8] or "",
        reviewed_by=row[12] or "",
    )
    candidate.created_at = _parse_optional(row[9])
    candidate.updated_at = _parse_optional(row[10])
    candidate.reviewed_at = _parse_optional(row[11])
    return candidate


def row_to_review(row: Any) -> ReviewRecord:
    """把一行 ``candidate_reviews`` 映射成 :class:`ReviewRecord`。"""
    record = ReviewRecord(
        id=row[0],
        candidate_id=row[1],
        platform=row[2],
        scope_id=row[3],
        from_status=row[4] or "",
        to_status=row[5] or "",
        meaning=row[6] or "",
        reviewer=row[7] or "",
    )
    record.created_at = _parse_optional(row[8])
    return record
