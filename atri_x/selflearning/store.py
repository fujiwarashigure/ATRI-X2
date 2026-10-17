"""观察写入、候选挖掘、review-before-apply 与上下文构造的 SQLite store。

对应 Go：``internal/selflearning/store.go``（建表/迁移见
:mod:`atri_x.selflearning.db`，挖掘见 :mod:`atri_x.selflearning.mining`，上层限额与
策略闸门见 :mod:`atri_x.selflearning.service`）。

关键语义：

* **观察**：写入前 trim；空文本静默丢弃（它在入站热路径上）；单条超长抛
  ``ErrObservationTooLong``；scope 满抛 ``ErrObservationLimit``；
  「数条数 → 插入」在同一个事务里。
* **候选**：``(kind, platform, scope_id, pattern)`` 唯一；挖掘只创建/刷新 ``pending``。
* **审慎裁决**：:meth:`Store.decide` / :meth:`Store.undo` 只作用于
  ``id + platform + scope_id`` 三元组命中的那一条，并往 ``candidate_reviews`` 追加
  一条只增不改的审计记录；找不到抛 :data:`atri_x.errors.ErrCandidateNotFound`。
* **上下文**：只取 ``approved`` 候选；查询词命中优先排序，全不命中时回落为
  「计数 + 更新时间」序。
"""

from __future__ import annotations

import os
import sqlite3
import threading

from atri_x.errors import (
    ErrCandidateNotFound,
    InvalidError,
    ObservationLimitError,
    ObservationTooLongError,
)
from atri_x.selflearning.db import (
    DEFAULT_MAX_MINE_CHARS,
    DEFAULT_MAX_OBSERVATIONS_PER_SCOPE,
    DEFAULT_MAX_OBSERVATION_RUNES,
    SELECT_CANDIDATE_COLUMNS,
    migrate,
    row_to_candidate,
    row_to_review,
    transaction,
)
from atri_x.selflearning.mining import mine as mine_observations
from atri_x.selflearning.models import (
    Candidate,
    DecideRequest,
    MineStats,
    ReviewRecord,
    StatusApproved,
    StatusPending,
    StatusRejected,
    StoreLimits,
)
from atri_x.storage.ids import new_id
from atri_x.storage.times import format_time, now as storage_now
from atri_x.textmatch import keywords, like_pattern

__all__ = ["Open", "Store", "open_store"]

#: 允许的裁决状态（``decide`` 只接受这三个）。
_REVIEW_STATUSES = (StatusApproved, StatusRejected, StatusPending)


class Store:
    """自学习语料与候选的 SQLite store。对应 Go ``Store``。

    与 angelmemory 一样，所有公开方法持有同一把可重入锁：单连接只串行化单条语句，
    「读-判断-写」序列要靠锁来保证原子。
    """

    def __init__(self, connection: sqlite3.Connection, limits: StoreLimits | None = None) -> None:
        self._db = connection
        self._lock = threading.RLock()
        self.limits = limits or StoreLimits(
            max_observation_runes=DEFAULT_MAX_OBSERVATION_RUNES,
            max_observations_per_scope=DEFAULT_MAX_OBSERVATIONS_PER_SCOPE,
            max_mine_chars=DEFAULT_MAX_MINE_CHARS,
        )

    # -- 生命周期 -----------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接。对应 Go 的 ``Store.db``（测试夹具与诊断用）。"""
        return self._db

    def set_limits(self, limits: StoreLimits) -> None:
        """应用写入/扫描上限。对应 Go ``Store.SetLimits``（零值字段回落默认值）。"""
        with self._lock:
            if limits.max_observation_runes <= 0:
                limits.max_observation_runes = DEFAULT_MAX_OBSERVATION_RUNES
            if limits.max_observations_per_scope <= 0:
                limits.max_observations_per_scope = DEFAULT_MAX_OBSERVATIONS_PER_SCOPE
            if limits.max_mine_chars <= 0:
                limits.max_mine_chars = DEFAULT_MAX_MINE_CHARS
            self.limits = limits

    def close(self) -> None:
        """关闭连接（幂等）。对应 Go ``Store.Close``。"""
        with self._lock:
            if self._db is None:
                return
            connection, self._db = self._db, None
        try:
            connection.close()
        except sqlite3.Error:  # pragma: no cover - 关闭失败不应阻塞调用方
            pass

    # -- 观察 ---------------------------------------------------------------

    def observe(self, platform: str, scope_id: str, user_id: str, text: str) -> None:
        """写入一条观察。对应 Go ``Store.Observe``。

        空文本（trim 后）直接返回 ``None``：这是入站热路径，丢数据比抛异常合适。
        """
        with self._lock:
            self._require_open()
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            user_id = (user_id or "").strip()
            text = (text or "").strip()
            if text == "":
                return
            if platform == "" or scope_id == "":
                raise InvalidError("self learning observation platform and scope are required")
            max_runes = self.limits.max_observation_runes
            if max_runes > 0 and len(text) > max_runes:
                raise ObservationTooLongError(
                    f"self learning observation is too long: {len(text)} runes (max {max_runes})"
                )
            with transaction(self._db):
                max_rows = self.limits.max_observations_per_scope
                if max_rows > 0:
                    count = self._db.execute(
                        "SELECT COUNT(*) FROM observations WHERE platform = ? AND scope_id = ?",
                        (platform, scope_id),
                    ).fetchone()[0]
                    if count >= max_rows:
                        raise ObservationLimitError(
                            "self learning observation scope limit reached:"
                            f" {count} rows (max {max_rows})"
                        )
                self._db.execute(
                    """
INSERT INTO observations (id, platform, scope_id, user_id, text, created_at)
VALUES (?, ?, ?, ?, ?, ?)""",
                    (new_id(), platform, scope_id, user_id, text, format_time(storage_now())),
                )

    # -- 挖掘 ---------------------------------------------------------------

    def mine(
        self,
        platform: str,
        scope_id: str,
        min_count: int,
        min_users: int,
        limit: int,
        deadline: float | None = None,
    ) -> MineStats:
        """扫描近期观察并创建可复核候选。对应 Go ``Store.Mine``。"""
        with self._lock:
            self._require_open()
            return mine_observations(
                self, platform, scope_id, min_count, min_users, limit, deadline
            )

    # -- 候选读取 -----------------------------------------------------------

    def list_candidates(
        self, platform: str, scope_id: str, status: str, limit: int
    ) -> list[Candidate]:
        """按状态列出 scope 内候选。对应 Go ``Store.ListCandidates``。"""
        with self._lock:
            self._require_open()
            if limit <= 0 or limit > 500:
                limit = 100
            conditions = ["platform = ?", "scope_id = ?"]
            params: list[object] = [(platform or "").strip(), (scope_id or "").strip()]
            status = (status or "").strip()
            if status != "":
                conditions.append("status = ?")
                params.append(status)
            params.append(limit)
            rows = self._db.execute(
                SELECT_CANDIDATE_COLUMNS
                + " WHERE "
                + " AND ".join(conditions)
                + " ORDER BY count DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [row_to_candidate(row) for row in rows]

    def approved_context(
        self, platform: str, scope_id: str, query: str, limit: int
    ) -> list[Candidate]:
        """取已批准候选（查询词命中优先）。对应 Go ``Store.ApprovedContext``。"""
        with self._lock:
            self._require_open()
            if limit <= 0 or limit > 50:
                limit = 10
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            terms = keywords(query)
            if not terms:
                return self._load_candidates(
                    SELECT_CANDIDATE_COLUMNS
                    + " WHERE platform = ? AND scope_id = ? AND status = 'approved'"
                    " ORDER BY count DESC, updated_at DESC LIMIT ?",
                    (platform, scope_id, limit),
                )
            where_parts: list[str] = []
            score_parts: list[str] = []
            where_params: list[object] = []
            score_params: list[object] = []
            for term in terms:
                pattern = like_pattern(term)
                where_parts.append(
                    "(pattern LIKE ? ESCAPE '\\' OR meaning LIKE ? ESCAPE '\\')"
                )
                where_params.extend((pattern, pattern))
                score_parts.append(
                    "(CASE WHEN pattern LIKE ? ESCAPE '\\' THEN 2 ELSE 0 END"
                    " + CASE WHEN meaning LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
                )
                score_params.extend((pattern, pattern))
            params: list[object] = [platform, scope_id]
            params.extend(where_params)
            params.extend(score_params)
            params.append(limit)
            statement = (
                SELECT_CANDIDATE_COLUMNS
                + " WHERE platform = ? AND scope_id = ? AND status = 'approved' AND ("
                + " OR ".join(where_parts)
                + ") ORDER BY ("
                + " + ".join(score_parts)
                + ") DESC, count DESC, updated_at DESC LIMIT ?"
            )
            candidates = self._load_candidates(statement, params)
            if candidates:
                return candidates
            return self._load_candidates(
                SELECT_CANDIDATE_COLUMNS
                + " WHERE platform = ? AND scope_id = ? AND status = 'approved'"
                " ORDER BY count DESC, updated_at DESC LIMIT ?",
                (platform, scope_id, limit),
            )

    def _load_candidates(self, statement: str, params: tuple | list) -> list[Candidate]:
        rows = self._db.execute(statement, params).fetchall()
        return [row_to_candidate(row) for row in rows]

    def counts(self, platform: str, scope_id: str) -> tuple[int, int]:
        """返回 ``(pending, approved)`` 计数。对应 Go ``Store.Counts``。"""
        with self._lock:
            if self._db is None:
                return 0, 0
            row = self._db.execute(
                """SELECT
  COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0)
FROM candidates WHERE platform = ? AND scope_id = ?""",
                ((platform or "").strip(), (scope_id or "").strip()),
            ).fetchone()
            return int(row[0] or 0), int(row[1] or 0)

    # -- 裁决（review-before-apply） ----------------------------------------

    def decide(self, req: DecideRequest) -> None:
        """裁决一条候选（只在该 scope 内），并追加审计记录。对应 Go ``Store.Decide``。"""
        with self._lock:
            self._require_open()
            status = (req.status or "").strip().lower()
            if status not in _REVIEW_STATUSES:
                raise InvalidError(f"unsupported review status {status!r}")
            id = (req.id or "").strip()
            platform = (req.platform or "").strip()
            scope_id = (req.scope_id or "").strip()
            if id == "" or platform == "" or scope_id == "":
                raise InvalidError("self learning decide requires id, platform and scope")
            meaning = (req.meaning or "").strip()
            reviewer = (req.reviewer or "").strip()
            with transaction(self._db):
                row = self._db.execute(
                    "SELECT status FROM candidates WHERE id = ? AND platform = ? AND scope_id = ?",
                    (id, platform, scope_id),
                ).fetchone()
                if row is None:
                    raise ErrCandidateNotFound
                from_status = row[0] or ""
                stamp = format_time(storage_now())
                self._db.execute(
                    """
UPDATE candidates
SET status = ?, meaning = ?, reviewed_at = ?, reviewed_by = ?, updated_at = ?
WHERE id = ? AND platform = ? AND scope_id = ?""",
                    (status, meaning, stamp, reviewer, stamp, id, platform, scope_id),
                )
                self._insert_review(
                    ReviewRecord(
                        candidate_id=id,
                        platform=platform,
                        scope_id=scope_id,
                        from_status=from_status,
                        to_status=status,
                        meaning=meaning,
                        reviewer=reviewer,
                    )
                )

    def undo(self, req: DecideRequest) -> None:
        """把候选退回 ``pending``，保留它当前的 meaning。对应 Go ``Store.Undo``。

        这是 review-before-apply 的反向操作：一次误批可以一步撤销，而已经批准的
        释义不会被抹掉。
        """
        with self._lock:
            self._require_open()
            id = (req.id or "").strip()
            platform = (req.platform or "").strip()
            scope_id = (req.scope_id or "").strip()
            if id == "" or platform == "" or scope_id == "":
                raise InvalidError("self learning undo requires id, platform and scope")
            reviewer = (req.reviewer or "").strip()
            with transaction(self._db):
                row = self._db.execute(
                    "SELECT status FROM candidates WHERE id = ? AND platform = ? AND scope_id = ?",
                    (id, platform, scope_id),
                ).fetchone()
                if row is None:
                    raise ErrCandidateNotFound
                from_status = row[0] or ""
                stamp = format_time(storage_now())
                self._db.execute(
                    """
UPDATE candidates
SET status = 'pending', reviewed_at = ?, reviewed_by = ?, updated_at = ?
WHERE id = ? AND platform = ? AND scope_id = ?""",
                    (stamp, reviewer, stamp, id, platform, scope_id),
                )
                self._insert_review(
                    ReviewRecord(
                        candidate_id=id,
                        platform=platform,
                        scope_id=scope_id,
                        from_status=from_status,
                        to_status=StatusPending,
                        reviewer=reviewer,
                    )
                )

    def _insert_review(self, record: ReviewRecord) -> None:
        stamp = record.created_at or storage_now()
        self._db.execute(
            """
INSERT INTO candidate_reviews (id, candidate_id, platform, scope_id, from_status, to_status, meaning, reviewer, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id(),
                record.candidate_id,
                record.platform,
                record.scope_id,
                record.from_status,
                record.to_status,
                record.meaning,
                record.reviewer,
                format_time(stamp),
            ),
        )

    def history(
        self, platform: str, scope_id: str, candidate_id: str, limit: int
    ) -> list[ReviewRecord]:
        """取一条候选的审计链（最新在前）。对应 Go ``Store.History``。"""
        with self._lock:
            self._require_open()
            if limit <= 0 or limit > 200:
                limit = 50
            rows = self._db.execute(
                """
SELECT id, candidate_id, platform, scope_id, from_status, to_status, meaning, reviewer, created_at
FROM candidate_reviews
WHERE platform = ? AND scope_id = ? AND candidate_id = ?
ORDER BY created_at DESC, id DESC
LIMIT ?""",
                (
                    (platform or "").strip(),
                    (scope_id or "").strip(),
                    (candidate_id or "").strip(),
                    limit,
                ),
            ).fetchall()
            return [row_to_review(row) for row in rows]

    # -- 保留期清理 ---------------------------------------------------------

    def delete_before(self, cutoff) -> tuple[int, int]:
        """删除过期的候选/观察/审计记录，返回 ``(候选行数, 观察行数)``。

        对应 Go ``Store.DeleteBefore``（Go 返回三个值，第三个是 error；Python 抛异常）。
        审计记录也一并清理，但不计入返回值（与 Go 一致）。
        """
        with self._lock:
            if self._db is None:
                return 0, 0
            stamp = format_time(cutoff)
            candidates = self._db.execute(
                "DELETE FROM candidates WHERE updated_at < ?", (stamp,)
            )
            observations = self._db.execute(
                "DELETE FROM observations WHERE created_at < ?", (stamp,)
            )
            self._db.execute(
                "DELETE FROM candidate_reviews WHERE created_at < ?", (stamp,)
            )
            return int(candidates.rowcount or 0), int(observations.rowcount or 0)

    def _require_open(self) -> None:
        if self._db is None:
            raise InvalidError("self learning store is not open")


def open_store(path: str) -> Store:
    """打开（并按需建库）自学习 SQLite。对应 Go ``selflearning.Open``。"""
    path = (path or "").strip()
    if path == "":
        raise InvalidError("self learning sqlite path is empty")
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, mode=0o755, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        migrate(connection)
    except BaseException:
        connection.close()
        raise
    return Store(
        connection,
        StoreLimits(
            max_observation_runes=DEFAULT_MAX_OBSERVATION_RUNES,
            max_observations_per_scope=DEFAULT_MAX_OBSERVATIONS_PER_SCOPE,
            max_mine_chars=DEFAULT_MAX_MINE_CHARS,
        ),
    )


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
Open = open_store
