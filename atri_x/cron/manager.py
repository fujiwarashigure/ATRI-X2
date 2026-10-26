"""中央 Cron Manager：注册 handler、调度启用的 job、维护运行状态。对应 Go：``internal/cron/manager.go``。

职责与 Go 一致：``Registry`` 接口（注册/upsert/禁用/删除）+ 基于仓库的调度循环 +
``run_job`` 的运行状态与执行日志写入。

与 Go 的差异（Python 侧的等价物）：

* Go 用 ``robfig/cron`` 的 ``Cron``（自带 ticker + 全局循环）调度；本仓**每个 job 一个
  ``asyncio.Task``**，循环体是"算 next_run → 单次 ``sleep`` 到点 → 触发 → 重算"，
  不忙轮询；:meth:`Manager.stop` 置停止标志并等待在跑的任务结束（优雅停止）。
* Go 的 ``sync.Mutex`` → 本仓不再加锁：调度状态只在事件循环线程里读写，
  并发安全由"同一 loop、无 await 间隙的临界区"保证（仓库自身是线程安全的 SQLite）。
* 仓库调用是**同步**的（``atri_x.storage`` 契约如此），所以它们直接出现在 ``async``
  方法体里；这正是 Go 里"阻塞调用"的等价物，不使用 ``to_thread``。
* Go 的 ``time.Local`` 是隐式的；本仓把"解释 cron 表达式的时区"做成 ``location``
  参数（默认系统本地时区），以便测试固定时区、不受机器时区影响。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone, tzinfo
from typing import Any, Protocol, runtime_checkable

from atri_x.cron.model import (
    METADATA_KIND,
    ScheduleMode,
    decode_metadata,
    format_run_at_utc,
    parse_run_at,
    parse_run_at_utc,
    resolve_once_run_at,
    system_location,
)
from atri_x.errors import InvalidError
from atri_x.net.cronspec import parse_standard
from atri_x.storage.models import CronJob, CronJobRunState, UpsertCronJobRequest
from atri_x.storage.times import now as default_now

__all__ = [
    "Handler",
    "Manager",
    "Registry",
    "UpsertJobRequest",
    "compute_next_run_at",
    "user_once_run_at",
    "user_once_run_at_expired",
]

#: 一次 job 触发的处理函数（对应 Go ``Handler``）。可返回 awaitable，Manager 会 await。
Handler = Callable[[Any, CronJob], Any]

#: ``UpsertJobRequest`` 与仓库请求同形（对应 Go 的 type alias）。
UpsertJobRequest = UpsertCronJobRequest


@runtime_checkable
class Registry(Protocol):
    """维护类任务的注册入口。对应 Go ``Registry``。

    除了 cron Service 自己，``internal/maintenance`` 也通过这个接口注册集中维护任务
    （维护包不归本模块实现，本模块只提供入口）。Go 的方法是同步的，本仓为 ``async``
    （调度可能落在事件循环里）；实现方需保证幂等。
    """

    async def register_handler(self, name: str, handler: Handler) -> None:
        """注册一个 handler；已启动时会立刻重扫启用中的 job。"""

    async def upsert_job(self, req: UpsertJobRequest) -> CronJob:
        """按名字插入/更新 job 并返回最新行。"""

    async def disable_job(self, name: str) -> None:
        """停用 job（保留行）。"""

    async def delete_job(self, name: str) -> None:
        """删除 job。"""


@dataclass
class _Entry:
    """一个已调度的 job。对应 robfig ``Cron`` 的一个 entry。

    ``next_run`` 是"下一次触发时间"；触发前会被推进到再下一次，好让 ``run_job``
    写库时拿到与 Go ``scheduler.Entry(id).Next`` 相同的值。
    """

    name: str = ""
    next_run: datetime | None = None
    task: asyncio.Task[None] | None = None


#: 调度等待的单段上限（秒）：分段才能及时发现墙钟前跳/回拨。
_WAIT_CHUNK_SECONDS = 30.0
#: 墙钟回拨超过它就不再死等（重新计算下次触发），避免等到"天荒地老"。
_BACKWARD_JUMP_RECOMPUTE_SECONDS = 3600.0


class Manager:
    """cron manager。对应 Go ``Manager``。"""

    def __init__(
        self,
        repo: Any = None,
        logger: logging.Logger | None = None,
        *,
        location: tzinfo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.logger = logger
        self.location = location
        self.now: Callable[[], datetime] = now or default_now
        self.handlers: dict[str, Handler] = {}
        self.entries: dict[str, _Entry] = {}
        self.running: set[str] = set()
        self.started = False
        self._stopping = asyncio.Event()
        self._ctx: Any = None

    # -- 注册 / CRUD --------------------------------------------------------
    async def register_handler(self, name: str, handler: Handler) -> None:
        """注册 handler。对应 Go ``RegisterHandler``。"""
        if not name:
            raise InvalidError("cron handler name is empty")
        if handler is None:
            raise InvalidError(f'cron handler "{name}" is nil')
        self.handlers[name] = handler
        if self.started:
            await self.reload_enabled(self._ctx)

    async def upsert_job(self, req: UpsertJobRequest) -> CronJob:
        """插入或更新 job。对应 Go ``UpsertJob``。"""
        validate_upsert_request(req)
        req = self._stamp_once_run_at(req)
        next_run = compute_next_run_at(
            req.schedule, req.metadata, req.enabled, self.now(), self.location
        )
        job = self.repo.upsert(replace(req, next_run_at=next_run))
        if self.started:
            await self.schedule_job(job)
        return job

    def _stamp_once_run_at(self, req: UpsertJobRequest) -> UpsertJobRequest:
        """给一次性任务补 ``run_at_utc``（确定的 UTC 时刻 + 创建时区名）。

        兼容与安全口径（1.0.10）：

        * 只在 ``schedule.mode == "once"`` 且**尚未**带 ``run_at_utc`` 时补；
        * 已有 ``run_at_utc`` 的任务（新数据）**不改写**——改时区不应改变已定死的时刻；
        * DST 不存在的时刻在这里抛 ``InvalidError``（创建/更新当场失败，而不是静默偏移）；
        * DST 重复时刻取第一次，并把 ``run_at_dst_ambiguous`` 落进 metadata。
        """
        if not req.metadata:
            return req
        try:
            meta = decode_metadata(req.metadata)
        except Exception:  # noqa: BLE001 - 非本仓 metadata：交给下游原样处理
            return req
        if meta.schedule.mode != ScheduleMode.ONCE:
            return req
        if (meta.schedule.run_at_utc or "").strip():
            return req
        run_at_text = (meta.schedule.run_at or "").strip()
        if not run_at_text:
            return req
        moment, ambiguous = resolve_once_run_at(run_at_text, self.location)
        location = self.location or system_location()
        meta.schedule.run_at_utc = format_run_at_utc(moment)
        meta.schedule.run_at_tz = str(getattr(location, "key", "") or location)
        meta.schedule.run_at_dst_ambiguous = bool(ambiguous)
        return replace(req, metadata=meta.encode())

    async def disable_job(self, name: str) -> None:
        """停用 job。对应 Go ``DisableJob``。"""
        self.repo.disable_by_name(name)
        await self.remove_entry(name)

    async def disable_job_if_delivery_token(self, name: str, delivery_token: str) -> bool:
        """仅在投递令牌匹配时停用 job。对应 Go ``DisableJobIfDeliveryToken``。"""
        disabled = self.repo.disable_by_name_if_delivery_token(name, delivery_token)
        if not disabled:
            return False
        await self.remove_entry(name)
        return True

    async def delete_job(self, name: str) -> None:
        """删除 job。对应 Go ``DeleteJob``。"""
        await self.remove_entry(name)
        self.repo.delete_by_name(name)

    # -- 生命周期 -----------------------------------------------------------
    async def start(self, ctx: Any = None) -> None:
        """启动调度。对应 Go ``Start``。"""
        if self.started:
            return
        self._ctx = ctx
        self._stopping = asyncio.Event()
        self.started = True
        try:
            await self.reload_enabled(ctx)
        except Exception:
            raise
        self.log_info("cron manager started")

    async def stop(self, timeout: float = 30.0) -> None:
        """优雅停止：置停止标志，等在跑的 job 结束；超时才强制取消。

        对应 Go ``Stop``（Go 返回一个 ctx 由调用方等待，本仓直接 ``await``）。
        未启动时立即返回（对应 ``TestManagerStopBeforeStartDoesNotBlock``）。
        """
        self.started = False
        self._stopping.set()
        tasks = [entry.task for entry in self.entries.values() if entry.task is not None]
        self.entries = {}
        if not tasks:
            self.log_info("cron manager stopped")
            return
        self.log_info("cron manager stopping")
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        _ = done
        self.log_info("cron manager stopped")

    async def reload_enabled(self, ctx: Any = None) -> None:
        """重扫启用中的 job 并重新调度。对应 Go ``reloadEnabled``。"""
        _ = ctx
        jobs = self.repo.list_enabled()
        for job in jobs:
            try:
                await self.schedule_job(job)
            except Exception as exc:  # noqa: BLE001 - 单个 job 调度失败不该拖垮整轮
                self.log_warn(
                    "cron job schedule failed", job=job.name, handler=job.handler, error=str(exc)
                )

    async def schedule_job(self, job: CronJob) -> None:
        """把 job 挂到调度循环上（或按需摘掉）。对应 Go ``scheduleJob``。"""
        await self.remove_entry(job.name)
        if not self.started:
            return
        if not job.enabled:
            await self._update_next_run_at(job, None)
            return
        if job.handler not in self.handlers:
            self.log_warn(
                "cron job handler not registered", job=job.name, handler=job.handler
            )
            await self._update_next_run_at(job, None)
            return
        try:
            next_run = compute_next_run_at(
                job.schedule, job.metadata, True, self.now(), self.location
            )
        except Exception as exc:
            raise InvalidError(f'schedule cron job "{job.name}": {exc}') from exc
        if next_run is None:
            await self._update_next_run_at(job, None)
            return
        entry = _Entry(name=job.name, next_run=next_run)
        entry.task = asyncio.create_task(self._job_loop(job.name), name=f"cron:{job.name}")
        self.entries[job.name] = entry
        await self._update_next_run_at(job, next_run)
        self.log_info(
            "cron job scheduled", job=job.name, handler=job.handler, schedule=job.schedule
        )

    async def remove_entry(self, name: str) -> None:
        """摘掉一个 job 的调度循环。对应 Go ``removeEntry``。"""
        entry = self.entries.pop(name, None)
        if entry is None or entry.task is None:
            return
        if entry.task is asyncio.current_task():
            # 从自己的循环体里摘自己：不能 await 自己的取消。
            return
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)

    # -- 调度循环 -----------------------------------------------------------
    async def _job_loop(self, name: str) -> None:
        """单个 job 的调度循环：算下次触发 → 睡到点 → 触发 → 重算。"""
        while not self._stopping.is_set():
            entry = self.entries.get(name)
            if entry is None:
                return
            try:
                job = self.repo.get_by_name(name)
            except Exception as exc:  # noqa: BLE001 - 任务被删/读失败即退出该循环
                self.log_warn("cron job load failed", job=name, error=str(exc))
                return
            if not job.enabled:
                return
            try:
                next_run = compute_next_run_at(
                    job.schedule, job.metadata, True, self.now(), self.location
                )
            except Exception as exc:  # noqa: BLE001
                self.log_warn("cron job schedule failed", job=name, error=str(exc))
                return
            if next_run is None:
                entry.next_run = None
                await self._update_next_run_at(job, None)
                return
            delay = (next_run - self.now()).total_seconds()
            if delay > 0 and not await self._wait_until(next_run):
                return
            # 醒来后**复核目标时刻**（1.0.10）：墙钟回拨 / 系统校时都会让
            # "睡了 delay 秒"不再等于"到点了"。没到点就重新算，绝不提前触发。
            if self.now() < next_run:
                continue
            # 触发前把 next_run 推进到再下一次，对齐 robfig 更新 e.Next 的时机。
            try:
                entry.next_run = compute_next_run_at(
                    job.schedule, job.metadata, True, self.now(), self.location
                )
            except Exception:  # noqa: BLE001
                entry.next_run = None
            await self.run_job(name)

    async def _wait_until(self, target: datetime) -> bool:
        """分段等待到 ``target``；返回 ``False`` = 收到停止信号。

        1.0.10 的跳时规则（对应计划 §阶段 6）：

        * 每段最多 30 秒，每段结束**重新用墙钟**算剩余——因此墙钟前进会立刻收敛到
          目标，不会按旧 delay 一直睡下去；
        * 墙钟被**回拨**超过 1 小时：不死等（可能等到天荒地老），返回 ``True`` 让
          外层重新计算下次触发；
        * 墙钟**前跳**越过目标：立即返回，外层复核后触发一次（不补发多次）；
        * 不宣称 exactly-once，也不做秒级保证：分钟级 cron 就是分钟级。
        """
        first = self.now()
        while not self._stopping.is_set():
            current = self.now()
            remaining = (target - current).total_seconds()
            if remaining <= 0:
                return True
            if (first - current).total_seconds() > _BACKWARD_JUMP_RECOMPUTE_SECONDS:
                return True
            chunk = min(_WAIT_CHUNK_SECONDS, remaining)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=chunk)
            except asyncio.TimeoutError:
                continue
            return False
        return False

    async def run_job(self, name: str) -> None:
        """执行一次 job 并写入运行状态。对应 Go ``runJob``。"""
        try:
            job = self.repo.get_by_name(name)
        except Exception as exc:  # noqa: BLE001
            self.log_warn("cron job load failed", job=name, error=str(exc))
            return
        if not job.enabled:
            await self.remove_entry(job.name)
            return
        if job.name in self.running:
            self.log_warn(
                "cron job skipped because previous run is still running",
                job=job.name,
                handler=job.handler,
            )
            return
        handler = self.handlers.get(job.handler)
        if handler is None:
            self.log_warn("cron job handler not registered", job=job.name, handler=job.handler)
            return

        self.running.add(job.name)
        started_at = self.now()
        self.log_info("cron job started", job=job.name, handler=job.handler)
        run_err: BaseException | None = None
        try:
            result = handler(None, job)
            if isinstance(result, Awaitable):
                await result
        except Exception as exc:  # noqa: BLE001 - 与 Go 一致：handler 的错误记进 last_error
            run_err = exc
        duration = self.now() - started_at
        self.running.discard(job.name)

        entry = self.entries.get(job.name)
        state_job = job
        try:
            state_job = self.repo.get_by_name(job.name)
        except Exception as exc:  # noqa: BLE001
            self.log_warn(
                "cron job reload after run failed",
                job=job.name,
                handler=job.handler,
                error=str(exc),
            )

        last_error = ""
        if run_err is not None:
            last_error = str(run_err)
            self.log_warn(
                "cron job failed",
                job=job.name,
                handler=job.handler,
                duration=str(duration),
                error=str(run_err),
            )
        else:
            self.log_info(
                "cron job completed",
                job=job.name,
                handler=job.handler,
                duration=str(duration),
            )

        enabled = state_job.enabled
        next_run: datetime | None = None
        if enabled and entry is not None and entry.next_run is not None:
            next_run = entry.next_run
        if user_once_run_at_expired(state_job.metadata, self.now()):
            next_run = None
            await self.remove_entry(job.name)
        try:
            self.repo.update_run_state(
                state_job.id,
                CronJobRunState(
                    last_run_at=started_at,
                    next_run_at=next_run,
                    run_count=state_job.run_count + 1,
                    last_error=last_error,
                    enabled=enabled,
                    updated_at=self.now(),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.log_warn(
                "cron job state update failed",
                job=job.name,
                handler=job.handler,
                error=str(exc),
            )

    async def _update_next_run_at(self, job: CronJob, next_run_at: datetime | None) -> None:
        """写 ``next_run_at``。对应 Go ``updateNextRunAt``。"""
        try:
            self.repo.update_next_run_at(job.id, next_run_at, self.now())
        except Exception as exc:  # noqa: BLE001
            self.log_warn(
                "cron job next run update failed",
                job=job.name,
                handler=job.handler,
                error=str(exc),
            )

    # -- 日志 ---------------------------------------------------------------
    def log_info(self, msg: str, **attrs: Any) -> None:
        if self.logger is not None:
            self.logger.info(msg, extra=attrs or None)

    def log_warn(self, msg: str, **attrs: Any) -> None:
        if self.logger is not None:
            self.logger.warning(msg, extra=attrs or None)


# ---------------------------------------------------------------------------
# 下次触发时间
# ---------------------------------------------------------------------------
def compute_next_run_at(
    schedule: str,
    metadata: str,
    enabled: bool,
    now: datetime,
    location: tzinfo | None = None,
) -> datetime | None:
    """计算下次触发时间。对应 Go ``computeNextRunAt``。

    * 表达式先解析（非法表达式即使 ``enabled=False`` 也报错）；
    * 未启用 → ``None``；
    * 一次性任务按 metadata 里的 ``run_at`` 定点，已过期 → ``None``；
    * 周期性任务用 :func:`atri_x.net.cronspec.parse_standard`（robfig ``ParseStandard``
      等价物）在 ``location``（默认系统本地时区，等价 Go 的 ``time.Local``）里求下一次。

    返回 UTC aware 时间；无下次触发返回 ``None``。
    """
    parsed = parse_standard(schedule)
    if not enabled:
        return None
    run_at, ok, err = user_once_run_at(metadata, location)
    if ok or err is not None:
        if err is not None:
            raise err
        assert run_at is not None
        if run_at > now:
            return run_at
        return None
    next_run = parsed.next(now.astimezone(location or system_location()))
    if next_run is None:
        return None
    return next_run.astimezone(timezone.utc)


def user_once_run_at(
    metadata: str, location: tzinfo | None = None
) -> tuple[datetime | None, bool, BaseException | None]:
    """取一次性任务的 ``run_at``。对应 Go ``userOnceRunAt``。

    返回 ``(run_at, ok, err)``：``ok`` 表示这确实是一次性任务的 metadata（此时
    ``run_at`` 可能因解析失败而为 ``None``，对应 Go 的零值 + error）。
    """
    if not metadata:
        return None, False, None
    try:
        meta = decode_metadata(metadata)
    except Exception:  # noqa: BLE001 - 解不出来就当它不是一次性任务
        return None, False, None
    if meta.kind != METADATA_KIND or meta.schedule.mode != ScheduleMode.ONCE:
        return None, False, None
    try:
        # 1.0.10：优先用确定时刻（新任务都有）。旧数据没有该字段时仍按本地墙钟解释，
        # **不猜测**历史时区——这就是"legacy 语义"。
        if (meta.schedule.run_at_utc or "").strip():
            return parse_run_at_utc(meta.schedule.run_at_utc), True, None
        return parse_run_at(meta.schedule.run_at, location), True, None
    except Exception as exc:  # noqa: BLE001
        return None, True, exc


def user_once_run_at_expired(metadata: str, now: datetime, location: tzinfo | None = None) -> bool:
    """一次性任务是否已过期。对应 Go ``userOnceRunAtExpired``。"""
    run_at, ok, err = user_once_run_at(metadata, location)
    return err is None and ok and run_at is not None and run_at <= now


def validate_upsert_request(req: UpsertJobRequest) -> None:
    """校验 upsert 请求。对应 Go ``validateUpsertRequest``。"""
    if not req.name:
        raise InvalidError("cron job name is empty")
    if not req.handler:
        raise InvalidError("cron job handler is empty")
    if not req.schedule:
        raise InvalidError("cron job schedule is empty")
