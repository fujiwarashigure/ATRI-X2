"""人工复核队列：``<config_dir>/safety/review.db``（sqlite）。

为什么要独立库而不是复用 ``selflearning.db``：那是"学到的候选模式"，这里是"待人判定的
内容事件"，生命周期、保留期、查询口径都不同。迁移风格照抄
:mod:`atri_x.selflearning.db`（建表 + ``ALTER TABLE ADD COLUMN`` 容错），这样以后加列
不会让老库打不开。

状态机：``pending → approved / rejected / expired``。``hold`` 档的内容在 approve 后
才真正发出去，所以 :meth:`ReviewQueue.resolve` 是"放行"的唯一入口。

**不做什么**：不存原文（只存 ``sha256`` + 脱敏预览 + 可选 ``payload_ref``）、不发消息
（发送由调用方在 approve 后自己决定）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = ["STATUS_APPROVED", "STATUS_EXPIRED", "STATUS_PENDING", "STATUS_REJECTED", "ReviewQueue"]

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_items(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  stage TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  scope_id TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'review',
  action_taken TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  request_id TEXT NOT NULL DEFAULT '',
  labels TEXT NOT NULL DEFAULT '[]',
  score REAL,
  reason TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL DEFAULT '',
  preview TEXT NOT NULL DEFAULT '',
  payload_ref TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  reviewer TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status, created_at);
CREATE TABLE IF NOT EXISTS review_actions(
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  from_status TEXT NOT NULL DEFAULT '',
  to_status TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""

#: 后加的列（老库升级用）；与 selflearning 的迁移写法一致：失败就当已存在。
_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = ()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReviewQueue:
    """一份 sqlite 队列（``threading.Lock`` 串行，写失败不抛给主链路）。"""

    def __init__(self, path: str, *, logger: Any = None, now: Any = None) -> None:
        self.path = (path or "").strip()
        self.logger = logger
        self._now = now if callable(now) else _utc_now
        self._lock = threading.Lock()
        self.available = bool(self.path)
        self.ready = False
        if self.available:
            self._ensure_schema()

    # -- 建表 ---------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                for name, ddl in _EXTRA_COLUMNS:
                    try:
                        connection.execute(f"ALTER TABLE review_items ADD COLUMN {ddl}")
                    except sqlite3.OperationalError:
                        _ = name
            self.ready = True
        except (OSError, sqlite3.Error) as exc:
            self.available = False
            self._warn("复核队列不可用（内容安全其余功能不受影响）", str(exc))

    # -- 写入 ---------------------------------------------------------------
    def add(
        self,
        *,
        stage: str,
        tier: str = "review",
        action_taken: str = "",
        platform: str = "",
        scope_id: str = "",
        actor: str = "",
        provider: str = "",
        request_id: str = "",
        labels: object = (),
        score: float | None = None,
        reason: str = "",
        sha256: str = "",
        preview: str = "",
        payload_ref: str = "",
    ) -> str:
        """入队一条待复核事件，返回 item id（不可用时返回空串）。"""
        item_id = str(uuid.uuid4())
        if not self.ready:
            return ""
        record = (
            item_id,
            _iso(self._now()),
            stage or "",
            platform or "",
            scope_id or "",
            actor or "",
            tier or "review",
            action_taken or "",
            provider or "",
            request_id or "",
            json.dumps(list(labels or []), ensure_ascii=False),
            score,
            reason or "",
            sha256 or "",
            (preview or "")[:500],
            payload_ref or "",
            STATUS_PENDING,
        )
        sql = (
            "INSERT INTO review_items(id, created_at, stage, platform, scope_id, actor,"
            " tier, action_taken, provider, request_id, labels, score, reason, sha256,"
            " preview, payload_ref, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute(sql, record)
            except sqlite3.Error as exc:
                self._warn("写复核队列失败（不影响主流程）", str(exc))
                return ""
        return item_id

    def resolve(
        self,
        item_id: str,
        *,
        status: str,
        reviewer: str = "",
        note: str = "",
    ) -> bool:
        """把一条 pending 事件改成 approved / rejected。"""
        target = (status or "").strip().lower()
        if target not in (STATUS_APPROVED, STATUS_REJECTED) or not self.ready:
            return False
        moment = _iso(self._now())
        with self._lock:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT status FROM review_items WHERE id=?", (item_id,)
                    ).fetchone()
                    if row is None or row["status"] != STATUS_PENDING:
                        return False
                    connection.execute(
                        "UPDATE review_items SET status=?, reviewer=?, reviewed_at=?, note=?"
                        " WHERE id=? AND status=?",
                        (target, reviewer or "", moment, note or "", item_id, STATUS_PENDING),
                    )
                    connection.execute(
                        "INSERT INTO review_actions(id, item_id, from_status, to_status,"
                        " reviewer, note, created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()),
                            item_id,
                            STATUS_PENDING,
                            target,
                            reviewer or "",
                            note or "",
                            moment,
                        ),
                    )
            except sqlite3.Error as exc:
                self._warn("更新复核队列失败", str(exc))
                return False
        return True

    def expire(self, *, older_than_seconds: int = 3600) -> int:
        """把超时未处理的 pending 事件标成 expired，返回条数。"""
        if not self.ready:
            return 0
        moment = _iso(self._now())
        cutoff = self._now().timestamp() - max(0, int(older_than_seconds))
        cutoff_iso = _iso(datetime.fromtimestamp(cutoff, timezone.utc))
        with self._lock:
            try:
                with self._connect() as connection:
                    cursor = connection.execute(
                        "UPDATE review_items SET status=?, reviewed_at=?, note=note"
                        " WHERE status=? AND created_at < ?",
                        (STATUS_EXPIRED, moment, STATUS_PENDING, cutoff_iso),
                    )
                    return int(cursor.rowcount or 0)
            except sqlite3.Error as exc:
                self._warn("过期复核队列失败", str(exc))
                return 0

    # -- 读取 ---------------------------------------------------------------
    def get(self, item_id: str) -> dict[str, Any] | None:
        rows = self.list(limit=1, item_id=item_id)
        return rows[0] if rows else None

    def list(
        self,
        *,
        status: str = STATUS_PENDING,
        limit: int = 50,
        item_id: str = "",
    ) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if item_id:
            clauses.append("id=?")
            params.append(item_id)
        elif status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM review_items{where} ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            self._warn("读复核队列失败", str(exc))
            return []
        return [_row_to_dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        if not self.ready:
            return {"total": 0, STATUS_PENDING: 0}
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS n FROM review_items GROUP BY status"
                ).fetchall()
        except sqlite3.Error:
            return {"total": 0, STATUS_PENDING: 0}
        out: dict[str, int] = {}
        total = 0
        for row in rows:
            count = int(row["n"] or 0)
            out[str(row["status"])] = count
            total += count
        out["total"] = total
        return out

    # -- 内部 ---------------------------------------------------------------
    def _warn(self, message: str, error: str) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            warn = getattr(logger, "warning", None) or getattr(logger, "warn", None)
            if callable(warn):
                warn(message, error=error)
        except Exception:  # noqa: BLE001 - 日志坏了不能影响主流程
            pass


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {key: row[key] for key in row.keys()}
    try:
        out["labels"] = json.loads(out.get("labels") or "[]")
    except ValueError:
        out["labels"] = []
    return out
