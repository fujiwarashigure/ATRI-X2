"""Cron Service：装配、CRUD 与公开入口。对应 Go：``internal/cron/service.go``。

本模块是 cron 包的**装配与门面**：持有 Manager / Store / 投递回调 / LLM runner、
平台目标表、沙盒根与命令前缀，并实现创建、更新、禁用、删除、查询与"一次触发"的
总编排（:meth:`Service.handler`）。

**拆分说明**（``PORTING.md`` §2 的"单文件 ≤ 600 行"）：Go 的 ``service.go`` 是单文件；
本仓按 Go 的文件边界拆成 ``model`` / ``manager`` / ``execution`` / ``delivery`` /
``recovery`` 五个模块，Service 通过**同名前缀的方法**（``run_direct`` / ``deliver_prepared`` /
``notify_platform_connected`` …）组合它们，调用点与 Go 逐一对齐，便于交叉检索。

与 Go 的差异（Python 侧的等价物）：

* 公开方法都是 ``async``：投递回调（平台发送）与 LLM runner 本质是 I/O，Go 的阻塞调用
  在本仓是 ``await``。注入的回调**同步/异步都支持**（调用点用 :func:`_maybe_await`）。
* 仓库调用（``atri_x.storage``）是同步的，直接出现在 ``async`` 方法体里。
* 审计回调 Go 是 ``func(event string, attrs ...any)``（交替 key/value）；本仓改为
  ``audit(event, **attrs)``，键名与 Go 完全一致。
* ``now`` 可注入（与 Go 的 ``Service.now`` 一致）；``location`` 是"解释 cron 表达式与
  ``run_at`` 墙钟"的时区，默认系统本地时区（等价 Go 隐式的 ``time.Local``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any

from atri_x.atriyph import parse_task
from atri_x.background import Runner as LLMRunner
from atri_x.cron.delivery import DeliveryMixin
from atri_x.cron.execution import ExecutionMixin, maybe_await
from atri_x.cron.manager import Manager, UpsertJobRequest, compute_next_run_at
from atri_x.cron.model import (
    METADATA_KIND,
    USER_HANDLER_NAME,
    CronDeliveryState,
    CronLLMMetadata,
    CronSchedule,
    CronTarget,
    CronTrigger,
    JobView,
    Metadata,
    PatchRequest,
    ScheduleMode,
    TriggerLLM,
    TriggerMode,
    UpsertRequest,
    actor_metadata,
    decode_delivery_state,
    decode_metadata,
    cron_sandbox_subdir,
    first_non_empty,
    format_run_at,
    normalize_job_name,
    normalize_llm_session_mode,
    normalize_tool_list_names,
    parse_run_at,
    require_superadmin,
    schedule_expr,
    system_location,
    trim_strings,
    unique_strings,
    validate_llm_session_mode,
    validate_metadata,
)
from atri_x.cron.recovery import RecoveryMixin
from atri_x.errors import InvalidError
from atri_x.security import Actor
from atri_x.storage.models import CronJob, UpsertCronJobRequest
from atri_x.storage.times import now as default_now

__all__ = [
    "METADATA_KIND",
    "USER_HANDLER_NAME",
    "AuditFunc",
    "LLMRunner",
    "Options",
    "PlatformTarget",
    "RunCronMessageRequest",
    "RunCronMessageResult",
    "Service",
    "TargetSenderFunc",
    "normalize_platform_targets",
    "starts_new_delivery_cycle",
    "target_owner_id",
]

#: 审计回调：``audit(event, **attrs)``。对应 Go ``AuditFunc``。
AuditFunc = Callable[..., None]

#: 往一个具体收件目标发送输出的回调。对应 Go ``TargetSenderFunc``。
#: 可以返回 :class:`~atri_x.delivery.delivery.Receipt`，也可以返回 awaitable。
TargetSenderFunc = Callable[..., Any]


@dataclass
class PlatformTarget:
    """一个可投递平台及其超管 ID。对应 Go ``PlatformTarget``。"""

    name: str = ""
    superadmin_ids: list[str] = field(default_factory=list)


#: LLM runner。对应 Go ``LLMRunner``（即 ``background.Runner``）。
#: 这里直接复用 :class:`atri_x.background.Runner`，不另立同名 Protocol（重复定义会让
#: 跨模块的 ``isinstance`` 失效 —— 见 ``PORTING.md`` §3）。


@dataclass
class RunCronMessageRequest:
    """Agent 后台 runner 的 cron 入口请求。对应 Go ``RunCronMessageRequest``。"""

    job_name: str = ""
    title: str = ""
    platform: str = ""
    actor: Actor = field(default_factory=Actor)
    scope_id: str = ""
    session_id: str = ""
    model_provider: str = ""
    model: str = ""
    prompt: str = ""
    tool_list_names: list[str] = field(default_factory=list)


@dataclass
class RunCronMessageResult:
    """Agent 后台 runner 的 cron 入口结果。对应 Go ``RunCronMessageResult``。"""

    session_id: str = ""
    text: str = ""
    #: ``True`` 表示该 Session 已被前台接管；``text`` 为空，不应作为报告解析。
    taken_over: bool = False


@dataclass
class Options:
    """装配参数。对应 Go ``Options``。"""

    manager: Manager | None = None
    store: Any = None
    logger: logging.Logger | None = None
    audit: AuditFunc | None = None
    send_target: TargetSenderFunc | None = None
    runner: LLMRunner | None = None
    enabled_platforms: list[PlatformTarget] = field(default_factory=list)
    sandbox_root: str = ""
    command_prefix: str = ""
    location: tzinfo | None = None
    now: Callable[[], datetime] | None = None


class Service(ExecutionMixin, DeliveryMixin, RecoveryMixin):
    """cron 服务。对应 Go ``Service``。"""

    def __init__(self, opts: Options) -> None:
        sandbox_root = (opts.sandbox_root or "").strip()
        if not sandbox_root:
            sandbox_root = os.path.join("data", "sandbox")
        command_prefix = (opts.command_prefix or "").strip()
        if not command_prefix:
            command_prefix = "/"
        self.manager = opts.manager
        self.store = opts.store
        self.logger = opts.logger
        self.audit = opts.audit
        self.send_target = opts.send_target
        self.runner = opts.runner
        self.sandbox_root = sandbox_root
        self.command_prefix = command_prefix
        self.location = opts.location
        self.now: Callable[[], datetime] = opts.now or default_now
        self.enabled_platforms = normalize_platform_targets(opts.enabled_platforms)
        self.connected_platforms: dict[str, bool] = {}
        self.delivery_gates: dict[str, asyncio.Lock] = {}
        self._gates_lock = asyncio.Lock()

    def set_runner(self, runner: LLMRunner | None) -> None:
        """替换 LLM runner。对应 Go ``SetRunner``。"""
        self.runner = runner

    # -- 一次触发 -----------------------------------------------------------
    async def handler(self, ctx: Any, job: CronJob) -> None:
        """job 触发入口（注册给 Manager）。对应 Go ``Handler``。"""
        async with self._delivery_gate(job.name):
            latest = self.cron_jobs().get_by_name(job.name)
            if not latest.enabled:
                return
            job = latest
            try:
                meta = decode_metadata(job.metadata)
            except Exception as exc:  # noqa: BLE001 - 记日志后原样抛出
                self.log_warn("cron metadata parse failed", job=job.name, error=str(exc))
                raise
            self.audit_event(
                "cron.trigger_started",
                **self.cron_audit_attrs(
                    job.name,
                    meta,
                    trigger_mode=meta.trigger.mode,
                    schedule_mode=meta.schedule.mode,
                ),
            )
            self.log_info(
                "cron trigger started",
                **self.cron_audit_attrs(
                    job.name,
                    meta,
                    trigger_mode=meta.trigger.mode,
                    schedule_mode=meta.schedule.mode,
                ),
            )
            run_err: BaseException | None = None
            if meta.trigger.mode == TriggerMode.DIRECT:
                run_err = await self.run_direct(ctx, job, meta)
            elif meta.trigger.mode == TriggerLLM:
                run_err = await self.run_llm(ctx, job, meta)
            else:
                run_err = InvalidError(f'unsupported trigger mode "{meta.trigger.mode}"')
            if run_err is not None:
                self.audit_event(
                    "cron.trigger_failed",
                    **self.cron_audit_attrs(job.name, meta, error=str(run_err)),
                )
                self.log_warn(
                    "cron trigger failed",
                    **self.cron_audit_attrs(job.name, meta, error=str(run_err)),
                )
                raise run_err
            self.audit_event("cron.trigger_completed", **self.cron_audit_attrs(job.name, meta))
            self.log_info("cron trigger completed", **self.cron_audit_attrs(job.name, meta))

    # -- CRUD ---------------------------------------------------------------
    async def create(self, ctx: Any, req: UpsertRequest) -> CronJob:
        """创建任务。对应 Go ``Create``。"""
        try:
            require_superadmin(req.actor)
        except Exception as exc:
            self.audit_event(
                "cron.permission_denied",
                operation="create",
                actor_id=req.actor.id,
                reason=str(exc),
            )
            raise
        meta = Metadata(
            kind=METADATA_KIND,
            version=1,
            title=(req.title or "").strip(),
            created_by=actor_metadata(req.actor),
            schedule=CronSchedule(
                mode=req.schedule_mode,
                run_at=(req.run_at or "").strip(),
                cron_expr=(req.cron_expr or "").strip(),
            ),
            trigger=CronTrigger(mode=req.trigger_mode, message=(req.message or "").strip()),
            target=CronTarget(
                all_enabled_platforms=req.all_enabled_platforms,
                source_platform=first_non_empty(req.source_platform, req.actor.platform),
            ),
            llm=CronLLMMetadata(
                tool_list_names=normalize_tool_list_names(req.tool_list_names),
                session_mode=normalize_llm_session_mode(req.session_mode),
            ),
        )
        validate_atriyph_cron_task(meta)
        if req.enabled:
            self.validate_user_schedule(meta)
        job = await self.upsert(ctx, req.name, meta, req.enabled, True)
        self.audit_event(
            "cron.create",
            job=job.name,
            actor_id=req.actor.id,
            trigger_mode=meta.trigger.mode,
            schedule_mode=meta.schedule.mode,
        )
        return job

    async def update(self, ctx: Any, req: PatchRequest) -> CronJob:
        """更新任务。对应 Go ``Update``。"""
        try:
            require_superadmin(req.actor)
        except Exception as exc:
            self.audit_event(
                "cron.permission_denied",
                operation="update",
                actor_id=req.actor.id,
                reason=str(exc),
            )
            raise
        name = normalize_job_name(req.name)
        job = self.cron_jobs().get_by_name(name)
        meta = decode_metadata(job.metadata)
        previous_meta = replace(
            meta,
            created_by=replace(meta.created_by),
            schedule=replace(meta.schedule),
            trigger=replace(meta.trigger),
            target=replace(meta.target),
            llm=replace(meta.llm, tool_list_names=list(meta.llm.tool_list_names)),
        )
        if req.title is not None:
            meta.title = (req.title or "").strip()
        if req.schedule_mode is not None:
            meta.schedule.mode = req.schedule_mode
        if req.run_at is not None:
            meta.schedule.run_at = (req.run_at or "").strip()
        if req.cron_expr is not None:
            meta.schedule.cron_expr = (req.cron_expr or "").strip()
        if req.trigger_mode is not None:
            meta.trigger.mode = req.trigger_mode
        if req.message is not None:
            meta.trigger.message = (req.message or "").strip()
        if req.tool_list_names is not None:
            meta.llm.tool_list_names = normalize_tool_list_names(req.tool_list_names)
        if req.session_mode is not None:
            meta.llm.session_mode = validate_llm_session_mode(req.session_mode)
        if req.all_enabled_platforms is not None:
            meta.target.all_enabled_platforms = req.all_enabled_platforms
        enabled = job.enabled
        if req.enabled is not None:
            enabled = req.enabled
        validate_atriyph_cron_task(meta)
        if enabled:
            self.validate_user_schedule(meta)
        reset_delivery = starts_new_delivery_cycle(previous_meta, meta, req, enabled)
        updated = await self.upsert(ctx, name, meta, enabled, reset_delivery)
        self.audit_event("cron.update", job=updated.name, actor_id=req.actor.id)
        return updated

    async def disable(self, ctx: Any, name: str, actor: Actor) -> None:
        """禁用任务。对应 Go ``Disable``。"""
        try:
            require_superadmin(actor)
        except Exception as exc:
            self.audit_event(
                "cron.permission_denied",
                operation="disable",
                actor_id=actor.id,
                reason=str(exc),
            )
            raise
        name = normalize_job_name(name)
        if self.manager is not None:
            await self.manager.disable_job(name)
        else:
            self.cron_jobs().disable_by_name(name)
        self.audit_event("cron.disable", job=name, actor_id=actor.id)

    async def delete(self, ctx: Any, name: str, actor: Actor) -> None:
        """删除任务。对应 Go ``Delete``。"""
        try:
            require_superadmin(actor)
        except Exception as exc:
            self.audit_event(
                "cron.permission_denied",
                operation="delete",
                actor_id=actor.id,
                reason=str(exc),
            )
            raise
        name = normalize_job_name(name)
        if self.manager is not None:
            await self.manager.delete_job(name)
        else:
            self.cron_jobs().delete_by_name(name)
        self.audit_event("cron.delete", job=name, actor_id=actor.id)

    async def get(self, ctx: Any, name: str, actor: Actor) -> JobView:
        """取单个任务视图。对应 Go ``Get``。"""
        try:
            require_superadmin(actor)
        except Exception as exc:
            self.audit_event(
                "cron.permission_denied",
                operation="get",
                actor_id=actor.id,
                reason=str(exc),
            )
            raise
        job = self.cron_jobs().get_by_name(normalize_job_name(name))
        meta = decode_metadata(job.metadata)
        state = decode_delivery_state(job.delivery_state)
        return JobView(job=job, metadata=meta, delivery=state)

    async def list(
        self, ctx: Any, include_disabled: bool, include_completed: bool, actor: Actor
    ) -> list[JobView]:
        """列任务视图。对应 Go ``List``。"""
        try:
            require_superadmin(actor)
        except Exception as exc:
            self.audit_event(
                "cron.permission_denied",
                operation="list",
                actor_id=actor.id,
                reason=str(exc),
            )
            raise
        jobs = self.cron_jobs().list(include_disabled)
        views: list[JobView] = []
        for job in jobs:
            try:
                meta = decode_metadata(job.metadata)
            except Exception:  # noqa: BLE001 - 脏数据跳过，不拖垮列表
                continue
            if meta.kind != METADATA_KIND:
                continue
            try:
                state = decode_delivery_state(job.delivery_state)
            except Exception:  # noqa: BLE001
                continue
            if not include_completed and self.is_completed_cron(job, meta, state):
                continue
            views.append(JobView(job=job, metadata=meta, delivery=state))
        return views

    # -- 内部 ---------------------------------------------------------------
    async def upsert(
        self, ctx: Any, name: str, meta: Metadata, enabled: bool, reset_delivery: bool
    ) -> CronJob:
        """规范化 + 落库（经 Manager 或直接经仓库）。对应 Go ``upsert``。"""
        _ = ctx
        name = normalize_job_name(name)
        if not name:
            raise InvalidError("cron name is required")
        if not meta.title:
            meta.title = name[len("user.cron.") :]
        if not meta.kind:
            meta.kind = METADATA_KIND
        if meta.version < 2:
            meta.version = 2
        schedule = schedule_expr(meta.schedule, self.location)
        validate_metadata(meta)
        data = meta.encode()
        if self.manager is not None:
            return await self.manager.upsert_job(
                UpsertJobRequest(
                    name=name,
                    handler=USER_HANDLER_NAME,
                    schedule=schedule,
                    enabled=enabled,
                    metadata=data,
                    reset_delivery=reset_delivery,
                )
            )
        next_run = compute_next_run_at(schedule, data, enabled, self.now(), self.location)
        return self.cron_jobs().upsert(
            UpsertCronJobRequest(
                name=name,
                handler=USER_HANDLER_NAME,
                schedule=schedule,
                enabled=enabled,
                metadata=data,
                next_run_at=next_run,
                reset_delivery=reset_delivery,
            )
        )

    def is_completed_cron(self, job: CronJob, meta: Metadata, state: CronDeliveryState) -> bool:
        """一次性任务是否算"已完成"。对应 Go ``isCompletedCron``。"""
        if meta.schedule.mode != ScheduleMode.ONCE:
            return False
        return (not job.enabled) or (state.report_ready and self.delivery_complete(meta, state))

    def validate_user_schedule(self, meta: Metadata) -> None:
        """校验一次性任务的 ``run_at`` 粒度。对应 Go ``validateUserSchedule``。

        一次性 cron 是分钟级调度：过去时间直接拒绝；落在当前这一分钟则顺延到下一分钟
        （否则中央 cron 本轮的 tick 可能已经错过，任务看起来创建成功却不会准时触发）。
        """
        if meta is None or meta.schedule.mode != ScheduleMode.ONCE:
            return
        run_at = parse_run_at(meta.schedule.run_at, self.location)
        location = self.location or system_location()
        now_local = self.now().astimezone(location)
        now_minute = now_local.replace(second=0, microsecond=0)
        run_minute = run_at.astimezone(location).replace(second=0, microsecond=0)
        if run_minute == now_minute:
            meta.schedule.run_at = format_run_at(now_local.replace(second=0, microsecond=0) + timedelta(minutes=1), location)
            return
        if run_minute < now_minute:
            raise InvalidError(
                "run_at 时间不正确：一次性 cron 是分钟级调度，必须晚于当前这一分钟。"
                f"当前时间：{format_run_at(now_local, location)}"
            )

    # -- 基础设施 -----------------------------------------------------------
    def cron_jobs(self) -> Any:
        """取 cron 仓库（``store`` 为空时返回 ``None``）。"""
        if self.store is None:
            return None
        return self.store.cron_jobs()

    def enabled_platform_names(self) -> list[str]:
        """启用中的平台名（已排序）。对应 Go ``enabledPlatformNames``。"""
        return [target.name for target in self.enabled_platforms]

    def target_platform_names(self, meta: Metadata) -> list[str]:
        """本任务的投递平台名。对应 Go ``targetPlatformNames``。"""
        if meta.target.all_enabled_platforms:
            return self.enabled_platform_names()
        return unique_strings([meta.target.source_platform])

    def cron_sandbox_relative(self, job_name: str) -> str:
        """任务的沙盒相对目录（``cron/<name>``）。"""
        return cron_sandbox_subdir(job_name)

    def audit_event(self, event: str, **attrs: Any) -> None:
        """发一条审计。对应 Go ``auditEvent``。"""
        if self.audit is not None:
            self.audit(event, **attrs)

    def cron_audit_attrs(self, job_name: str, meta: Metadata, **attrs: Any) -> dict[str, Any]:
        """cron 审计/日志的公共属性。对应 Go ``cronAuditAttrs``。"""
        base: dict[str, Any] = {
            "job": job_name,
            "source_platform": meta.target.source_platform,
            "target_platforms": ",".join(self.target_platform_names(meta)),
            "target_all_enabled_platforms": meta.target.all_enabled_platforms,
        }
        base.update(attrs)
        return base

    cron_log_attrs = cron_audit_attrs

    def log_info(self, msg: str, **attrs: Any) -> None:
        if self.logger is not None:
            self.logger.info(msg, extra=attrs or None)

    def log_warn(self, msg: str, **attrs: Any) -> None:
        if self.logger is not None:
            self.logger.warning(msg, extra=attrs or None)

    @contextlib.asynccontextmanager
    async def _delivery_gate(self, job_name: str) -> AsyncIterator[None]:
        """同一 job 的投递串行闸门。对应 Go ``lockDeliveryJob``。

        并发触发（正常 tick 与平台连接补发同时发生）必须串行，否则同一轮投递会被
        执行两遍。每个 job 一把 ``asyncio.Lock``（对应 Go 的每 job ``sync.Mutex``）。
        """
        key = normalize_job_name(job_name)
        async with self._gates_lock:
            gate = self.delivery_gates.get(key)
            if gate is None:
                gate = asyncio.Lock()
                self.delivery_gates[key] = gate
        async with gate:
            yield


def normalize_platform_targets(targets: list[PlatformTarget] | None) -> list[PlatformTarget]:
    """规范化平台目标表（始终含 ``cli``，按名字排序）。对应 Go ``normalizePlatformTargets``。"""
    seen: set[str] = set()
    out: list[PlatformTarget] = [PlatformTarget(name="cli", superadmin_ids=["local"])]
    seen.add("cli")
    for target in targets or []:
        name = (target.name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(PlatformTarget(name=name, superadmin_ids=trim_strings(target.superadmin_ids)))
    out.sort(key=lambda item: item.name)
    return out


def target_owner_id(target: PlatformTarget) -> str:
    """平台的超管 owner ID。对应 Go ``targetOwnerID``。"""
    from atri_x.security import actor_id

    ids = trim_strings(target.superadmin_ids)
    if not ids:
        return ""
    return actor_id(target.name, ids[0])


def starts_new_delivery_cycle(
    before: Metadata, after: Metadata, req: PatchRequest, enabled: bool
) -> bool:
    """这次更新是否开启新一轮投递。对应 Go ``startsNewDeliveryCycle``。

    重新启用、或改了调度/触发/目标/LLM 预注入，都要清掉旧的投递状态（新一轮报告才有意义）；
    纯改名不算。
    """
    if not enabled:
        return False
    if req.enabled is not None and req.enabled:
        return True
    if before.schedule != after.schedule or before.trigger != after.trigger or before.target != after.target:
        return True
    if before.llm.session_mode != after.llm.session_mode or "\x00".join(
        before.llm.tool_list_names
    ) != "\x00".join(after.llm.tool_list_names):
        return True
    return False


def validate_atriyph_cron_task(meta: Metadata) -> None:
    """LLM cron 的任务正文必须是合法 Atriyph ``#task``。对应 Go ``validateAtriyphCronTask``。

    执行时会注入规则卡，任务正文保持可 lint、可复用、可审计；task 名只作任务内部标识，
    不要求和 cron job 名一致。
    """
    if meta.trigger.mode != TriggerLLM:
        return
    parse_task(meta.trigger.message, "")
