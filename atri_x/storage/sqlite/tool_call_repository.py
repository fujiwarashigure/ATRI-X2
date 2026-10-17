"""工具调用审计仓储。对应 Go：``internal/storage/sqlite/tool_call_repository.go``。

``successful_ids`` 是"这轮里哪些 tool_call_id 曾经成功过"的批量查询：Go 侧先做
trim + 去重，再按 500 一批拼 ``IN (...)`` 占位符（不是一次性拼几千个参数），本仓照做。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from atri_x.storage.ids import new_id
from atri_x.storage.models import ToolCallRecord, ToolUsageSummary
from atri_x.storage.sqlite.store import bool_int, null_string
from atri_x.storage.times import format_time, is_zero_time, now

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["ToolCallRepository"]

_SUCCESSFUL_BATCH_SIZE = 500


class ToolCallRepository:
    """工具调用审计仓储。对应 Go ``sqlite.ToolCallRepository``。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    def create(self, record: ToolCallRecord) -> None:
        """写入一条工具调用记录（零值时间由实现填充，并回填到传入的对象）。"""
        if not record.id:
            record.id = new_id()
        moment = now()
        if is_zero_time(record.started_at):
            record.started_at = moment
        if is_zero_time(record.finished_at):
            record.finished_at = moment
        if is_zero_time(record.created_at):
            record.created_at = record.finished_at

        self._db.execute(
            """
INSERT INTO tool_call_records (
    id, session_id, tool_call_id, tool_name, actor_id, risk_level, success, error,
    result_preview, started_at, finished_at, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id,
                record.session_id,
                record.tool_call_id,
                record.tool_name,
                record.actor_id,
                record.risk_level,
                bool_int(record.success),
                null_string(record.error),
                null_string(record.result_preview),
                format_time(record.started_at),
                format_time(record.finished_at),
                format_time(record.created_at),
            ),
        )

    def successful_ids(self, tool_call_ids: Sequence[str]) -> dict[str, bool]:
        """返回给定工具调用 ID 中"成功过"的子集（对应 Go 的 ``map[string]bool``）。"""
        unique: list[str] = []
        seen: set[str] = set()
        for raw in tool_call_ids:
            value = (raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)

        successful: dict[str, bool] = {}
        for start in range(0, len(unique), _SUCCESSFUL_BATCH_SIZE):
            batch = unique[start : start + _SUCCESSFUL_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = self._db.query(
                f"""
SELECT DISTINCT tool_call_id
FROM tool_call_records
WHERE success = 1 AND tool_call_id IN ({placeholders})""",
                batch,
            )
            for row in rows:
                successful[row["tool_call_id"]] = True
        return successful

    def usage_by_session(self, session_id: str) -> list[ToolUsageSummary]:
        """按工具名统计会话内的调用次数（按工具名升序）。"""
        rows = self._db.query(
            """
SELECT tool_name, COUNT(*) AS count
FROM tool_call_records
WHERE session_id = ?
GROUP BY tool_name
ORDER BY tool_name ASC""",
            (session_id,),
        )
        return [ToolUsageSummary(tool_name=row["tool_name"], count=int(row["count"])) for row in rows]
