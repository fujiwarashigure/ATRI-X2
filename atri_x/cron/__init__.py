"""中央 Cron Runtime：持久化 job 的调度、注册、upsert、禁用、删除、运行状态与执行日志。

对应 Go：``internal/cron/``（``service.go`` / ``model.go`` / ``execution.go`` /
``delivery.go`` / ``recovery.go`` / ``manager.go``，1831 行）。

Go 侧六个文件同属 ``package cron``；本包按同一文件边界拆成六个模块，并在此再导出，
使 ``from atri_x.cron import Service, Manager`` 或
``from atri_x.cron.delivery import deliver_prepared`` 两种引用都能用：

* :mod:`atri_x.cron.model` —— Metadata、投递状态类型、校验与规范化。
* :mod:`atri_x.cron.manager` —— handler 注册 + 调度循环 + 运行状态（``Registry`` 实现）。
* :mod:`atri_x.cron.service` —— 装配、CRUD 与公开入口。
* :mod:`atri_x.cron.execution` —— Direct/LLM 执行、报告生成、JSON 格式重试。
* :mod:`atri_x.cron.delivery` —— 逐目标逐输出发送、状态持久化、降级与 receipt 映射。
* :mod:`atri_x.cron.recovery` —— 平台连接跟踪、过期 once 扫描与补发入口。
* :mod:`atri_x.cron.errors` —— ``errors.Join`` 的等价物（Python 侧新增件）。
* :mod:`atri_x.cron.jsonview` —— 任务视图/任务行的 Go ``json.Marshal`` 形态（Python 侧新增件）。

**维护类任务**（``internal/maintenance``）不属本包：它通过 :class:`~atri_x.cron.manager.Registry`
注册集中维护任务，本包只提供注册入口。

**与 Agent 的边界**：LLM cron 经 :class:`atri_x.background.Runner` 注入的后台 runner
执行（每次实际触发新建 Session），本包不 import ``atri_x.agent``；cron 的 Agent 侧入口
请求/结果类型是 :class:`~atri_x.cron.service.RunCronMessageRequest` /
:class:`~atri_x.cron.service.RunCronMessageResult`。
"""

from __future__ import annotations

from atri_x.cron.errors import join_errors
from atri_x.cron.delivery import (
    DeliveryMixin,
    ResolvedCronTarget,
    cron_sandbox,
    cron_target_label,
    delivery_output_ids,
    delivery_status_done,
    ensure_delivery_output_state,
    ensure_delivery_target_state,
    find_delivery_output_status,
    find_delivery_target_state,
    missed_once_report_text,
    private_scope_id,
    report_segment_fallback_text,
)
from atri_x.cron.execution import (
    ExecutionMixin,
    cron_format_retry_prompt,
    cron_parse_failed_message,
    cron_prompt,
    maybe_await,
    parse_llm_result,
)
from atri_x.cron.jsonview import (
    GO_ZERO_TIME_TEXT,
    cron_job_json,
    job_view_json,
)
from atri_x.cron.manager import (
    Handler,
    Manager,
    Registry,
    UpsertJobRequest,
    compute_next_run_at,
    user_once_run_at,
    user_once_run_at_expired,
)
from atri_x.cron.model import (
    METADATA_KIND,
    RUN_AT_LAYOUT,
    USER_HANDLER_NAME,
    CronActor,
    CronDeliveryOutputState,
    CronDeliveryState,
    CronDeliveryTargetState,
    CronLLMMetadata,
    CronLLMResult,
    CronSchedule,
    CronTarget,
    CronTrigger,
    DeliveryDelivered,
    DeliveryFallbackDelivered,
    DeliveryFallbackPending,
    DeliveryPending,
    DeliveryStatus,
    JobView,
    LegacyCronDeliveryMetadata,
    Metadata,
    PatchRequest,
    ScheduleCron,
    ScheduleMode,
    ScheduleOnce,
    TriggerDirect,
    TriggerLLM,
    TriggerMode,
    UpsertRequest,
    actor_metadata,
    contains_string,
    cron_sandbox_subdir,
    cron_scope_id,
    cron_session_metadata,
    decode_delivery_state,
    decode_legacy_delivery_metadata,
    decode_metadata,
    encode_delivery_state,
    first_non_empty,
    format_run_at,
    has_legacy_delivery_metadata,
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
from atri_x.cron.service import (
    AuditFunc,
    LLMRunner,
    Options,
    PlatformTarget,
    RunCronMessageRequest,
    RunCronMessageResult,
    Service,
    TargetSenderFunc,
    normalize_platform_targets,
    starts_new_delivery_cycle,
    target_owner_id,
)

__all__ = [
    "GO_ZERO_TIME_TEXT",
    "METADATA_KIND",
    "RUN_AT_LAYOUT",
    "USER_HANDLER_NAME",
    "AuditFunc",
    "CronActor",
    "CronDeliveryOutputState",
    "CronDeliveryState",
    "CronDeliveryTargetState",
    "CronLLMMetadata",
    "CronLLMResult",
    "CronSchedule",
    "CronTarget",
    "CronTrigger",
    "DeliveryDelivered",
    "DeliveryFallbackDelivered",
    "DeliveryFallbackPending",
    "DeliveryMixin",
    "DeliveryPending",
    "DeliveryStatus",
    "ExecutionMixin",
    "Handler",
    "JobView",
    "LLMRunner",
    "LegacyCronDeliveryMetadata",
    "Manager",
    "Metadata",
    "Options",
    "PatchRequest",
    "PlatformTarget",
    "RecoveryMixin",
    "Registry",
    "ResolvedCronTarget",
    "RunCronMessageRequest",
    "RunCronMessageResult",
    "ScheduleCron",
    "ScheduleMode",
    "ScheduleOnce",
    "Service",
    "TargetSenderFunc",
    "TriggerDirect",
    "TriggerLLM",
    "TriggerMode",
    "UpsertJobRequest",
    "UpsertRequest",
    "actor_metadata",
    "compute_next_run_at",
    "contains_string",
    "cron_format_retry_prompt",
    "cron_job_json",
    "cron_parse_failed_message",
    "cron_prompt",
    "cron_sandbox",
    "cron_sandbox_subdir",
    "cron_scope_id",
    "cron_session_metadata",
    "cron_target_label",
    "decode_delivery_state",
    "decode_legacy_delivery_metadata",
    "decode_metadata",
    "delivery_output_ids",
    "delivery_status_done",
    "encode_delivery_state",
    "ensure_delivery_output_state",
    "ensure_delivery_target_state",
    "find_delivery_output_status",
    "find_delivery_target_state",
    "first_non_empty",
    "format_run_at",
    "has_legacy_delivery_metadata",
    "join_errors",
    "job_view_json",
    "maybe_await",
    "missed_once_report_text",
    "normalize_job_name",
    "normalize_llm_session_mode",
    "normalize_platform_targets",
    "normalize_tool_list_names",
    "parse_llm_result",
    "parse_run_at",
    "private_scope_id",
    "report_segment_fallback_text",
    "require_superadmin",
    "schedule_expr",
    "starts_new_delivery_cycle",
    "system_location",
    "target_owner_id",
    "trim_strings",
    "unique_strings",
    "user_once_run_at",
    "user_once_run_at_expired",
    "validate_llm_session_mode",
    "validate_metadata",
]
