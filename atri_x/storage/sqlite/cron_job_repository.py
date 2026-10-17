"""定时任务仓储。对应 Go：``internal/storage/sqlite/cron_job_repository.go``。

配置（handler / schedule / enabled / metadata / next_run_at）与投递轮次
（``delivery_state`` / ``delivery_token``）**分列**持久化，各走各的写路径：

* :meth:`upsert` 只碰配置列，且内容完全一致时**不写库**（避免把 ``updated_at`` 推来推去）；
  ``reset_delivery`` 显式要求时才清空投递列。
* :meth:`compare_and_swap_delivery` / :meth:`disable_by_name_if_delivery_token` 是条件更新
  （``WHERE ... AND enabled = 1 AND COALESCE(delivery_token,'') = ?``），只对**仍然启用**且
  令牌仍属于当前轮次的行生效 —— 迟到的投递结果不会覆盖新一轮、也不会把已停用的任务重新点亮。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from atri_x.errors import ErrNotFound, NotFoundError
from atri_x.storage.ids import new_id
from atri_x.storage.models import CronJob, CronJobRunState, UpsertCronJobRequest
from atri_x.storage.sqlite.store import bool_int, null_string, null_time, text_of
from atri_x.storage.times import format_time, now, parse_optional_time, parse_time

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["CronJobRepository"]

CRON_JOB_COLUMNS = """id, name, handler, schedule, enabled, metadata, last_run_at, next_run_at,
       run_count, last_error, created_at, updated_at, delivery_state, delivery_token"""


class CronJobRepository:
    """定时任务仓储。对应 Go ``sqlite.CronJobRepository``。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    # -- 配置 --------------------------------------------------------------

    def upsert(self, req: UpsertCronJobRequest) -> CronJob:
        """按 ``name`` 插入或更新任务并返回最新行。对应 Go ``Upsert``。"""
        moment = now()
        try:
            job = self.get_by_name(req.name)
        except NotFoundError:
            job = CronJob(
                id=new_id(),
                name=req.name,
                handler=req.handler,
                schedule=req.schedule,
                enabled=req.enabled,
                metadata=req.metadata,
                next_run_at=req.next_run_at,
                created_at=moment,
                updated_at=moment,
            )
            self._create(job)
            return job

        if _cron_job_matches_upsert(job, req):
            return job

        job.handler = req.handler
        job.schedule = req.schedule
        job.enabled = req.enabled
        job.metadata = req.metadata
        job.next_run_at = req.next_run_at
        job.updated_at = moment
        self._update(job, req.reset_delivery)
        return self.get_by_name(req.name)

    def get_by_name(self, name: str) -> CronJob:
        """按名字取任务；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(
            f"""
SELECT {CRON_JOB_COLUMNS}
FROM cron_jobs
WHERE name = ?""",
            (name,),
        )
        if row is None:
            raise ErrNotFound
        return _scan_cron_job(row)

    def list(self, include_disabled: bool) -> list[CronJob]:
        """列出任务（``include_disabled`` 为假时只返回启用的），按名字升序。"""
        query = f"""
SELECT {CRON_JOB_COLUMNS}
FROM cron_jobs"""
        if not include_disabled:
            query += """
WHERE enabled = 1"""
        query += """
ORDER BY name"""
        return [_scan_cron_job(row) for row in self._db.query(query)]

    def list_enabled(self) -> list[CronJob]:
        """列出启用中的任务。"""
        return self.list(False)

    def disable_by_name(self, name: str) -> None:
        """按名字停用任务并清空 ``next_run_at``；不存在时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE cron_jobs
SET enabled = 0, next_run_at = NULL, updated_at = ?
WHERE name = ?""",
            (format_time(now()), name),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    def disable_by_name_if_delivery_token(self, name: str, delivery_token: str) -> bool:
        """仅当投递令牌匹配**且任务仍启用**时停用；否则返回 ``False`` 且不写任何东西。"""
        cursor = self._db.execute(
            """
UPDATE cron_jobs
SET enabled = 0, next_run_at = NULL, updated_at = ?
WHERE name = ? AND enabled = 1 AND COALESCE(delivery_token, '') = ?""",
            (format_time(now()), name, delivery_token),
        )
        return cursor.rowcount > 0

    def delete_by_name(self, name: str) -> None:
        """按名字删除任务；不存在时抛 ErrNotFound。"""
        cursor = self._db.execute("DELETE FROM cron_jobs WHERE name = ?", (name,))
        if cursor.rowcount == 0:
            raise ErrNotFound

    # -- 运行态 ------------------------------------------------------------

    def update_next_run_at(self, id: str, next_run_at: datetime | None, updated_at: datetime) -> None:
        """只更新 ``next_run_at`` / ``updated_at``；不存在时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE cron_jobs
SET next_run_at = ?, updated_at = ?
WHERE id = ?""",
            (null_time(next_run_at), format_time(updated_at), id),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    def update_run_state(self, id: str, state: CronJobRunState) -> None:
        """写入一次执行的运行状态；不存在时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE cron_jobs
SET last_run_at = ?, next_run_at = ?, run_count = ?, last_error = ?, enabled = ?, updated_at = ?
WHERE id = ?""",
            (
                format_time(state.last_run_at),
                null_time(state.next_run_at),
                state.run_count,
                null_string(state.last_error),
                bool_int(state.enabled),
                format_time(state.updated_at),
                id,
            ),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    # -- 投递（条件更新） ---------------------------------------------------

    def compare_and_swap_delivery(
        self, id: str, expected_token: str, next_token: str, delivery_state: str
    ) -> bool:
        """投递令牌 CAS：只写仍启用且令牌匹配的行；不匹配返回 ``False``（不写任何东西）。"""
        cursor = self._db.execute(
            """
UPDATE cron_jobs
SET delivery_state = ?, delivery_token = ?, updated_at = ?
WHERE id = ? AND enabled = 1 AND COALESCE(delivery_token, '') = ?""",
            (
                null_string(delivery_state),
                null_string(next_token),
                format_time(now()),
                id,
                expected_token,
            ),
        )
        return cursor.rowcount > 0

    # -- 内部 --------------------------------------------------------------

    def _create(self, job: CronJob) -> None:
        self._db.execute(
            """
INSERT INTO cron_jobs (
    id, name, handler, schedule, enabled, metadata, last_run_at, next_run_at,
    run_count, last_error, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.id,
                job.name,
                job.handler,
                job.schedule,
                bool_int(job.enabled),
                null_string(job.metadata),
                null_time(job.last_run_at),
                null_time(job.next_run_at),
                job.run_count,
                null_string(job.last_error),
                format_time(job.created_at),
                format_time(job.updated_at),
            ),
        )

    def _update(self, job: CronJob, reset_delivery: bool) -> None:
        query = """
UPDATE cron_jobs
SET handler = ?, schedule = ?, enabled = ?, metadata = ?, next_run_at = ?, updated_at = ?"""
        args: list[object] = [
            job.handler,
            job.schedule,
            bool_int(job.enabled),
            null_string(job.metadata),
            null_time(job.next_run_at),
            format_time(job.updated_at),
        ]
        if reset_delivery:
            query += ", delivery_state = NULL, delivery_token = NULL"
        query += "\nWHERE id = ?"
        args.append(job.id)
        cursor = self._db.execute(query, args)
        if cursor.rowcount == 0:
            raise ErrNotFound


def _cron_job_matches_upsert(job: CronJob, req: UpsertCronJobRequest) -> bool:
    """``cronJobMatchesUpsert``：内容没变就不写库。"""
    if job is None:
        return False
    if req.reset_delivery and (job.delivery_state or job.delivery_token):
        return False
    return (
        job.handler == req.handler
        and job.schedule == req.schedule
        and job.enabled == req.enabled
        and job.metadata == req.metadata
        and _optional_time_equal(job.next_run_at, req.next_run_at)
    )


def _optional_time_equal(a: datetime | None, b: datetime | None) -> bool:
    """对应 Go ``optionalTimeEqual``：两边都是 nil 才相等，否则按时刻比较。"""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _scan_cron_job(row) -> CronJob:
    """任务行 → :class:`CronJob`（Go ``scanCronJob``）。"""
    return CronJob(
        id=row["id"],
        name=row["name"],
        handler=row["handler"],
        schedule=row["schedule"],
        enabled=bool(row["enabled"]),
        metadata=text_of(row["metadata"]),
        delivery_state=text_of(row["delivery_state"]),
        delivery_token=text_of(row["delivery_token"]),
        last_run_at=parse_optional_time(text_of(row["last_run_at"])),
        next_run_at=parse_optional_time(text_of(row["next_run_at"])),
        run_count=int(row["run_count"]),
        last_error=text_of(row["last_error"]),
        created_at=parse_time(row["created_at"]),
        updated_at=parse_time(row["updated_at"]),
    )
