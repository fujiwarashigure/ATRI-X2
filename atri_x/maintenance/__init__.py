"""维护任务：定时清理与定时报告/总结。

对应 Go：``internal/maintenance``（``maintenance.go`` + ``report.go``）。

* :mod:`atri_x.maintenance.maintenance` —— ``Service``、cron 装配、沙盒清理；
* :mod:`atri_x.maintenance.report` —— 日报的聚合、计价与渲染
  （``RunDailyReportMixin`` 混入 ``Service``，对应 Go 里挂在同一个 ``Service``
  上的 ``report.go`` 方法）；
* :mod:`atri_x.maintenance.dsh` —— 内置 DSH 内核 ``DSH_HOME`` 的保留清理
  （**Python 版新增件**：Go 上游没有 DSH 深度路由）；
* :mod:`atri_x.maintenance.summary` —— 每日会话总结（**Python 版新增件**）：
  确定性摘要 + 落盘；CLI 在 :mod:`atri_x.maintenance.summary_cli`
  （``python -m atri_x.maintenance.summary_cli``）。
"""

from __future__ import annotations

from atri_x.maintenance.dsh import (
    DSH_CACHE_DIRS,
    DSH_SESSIONS_DIR,
    DshCleanupResult,
    RunDshCleanupMixin,
    cleanup_dsh,
    resolve_dsh_home,
)
from atri_x.maintenance.maintenance import (
    DEFAULT_RETENTION_DAYS,
    RetentionCleanup,
    Service,
    cleanup_sandbox,
    new_service,
    new_service_with_config,
    remove_empty_dirs,
    setup_cron,
    upsert_or_disable,
)
from atri_x.maintenance.report import (
    IMAGE_QUERY_LIMIT,
    UNKNOWN_MODEL,
    USAGE_QUERY_LIMIT,
    ImageReportStats,
    ModelUsageReport,
    RunDailyReportMixin,
    compute_entry_cost,
    currency_symbol,
    format_daily_report,
    format_money,
    human_int,
    is_off_peak,
    log_query_days,
    offpeak_price,
    parse_log_int,
    price_for,
)
from atri_x.maintenance.summary import (
    DAILY_SUMMARY_DIR,
    DailySummary,
    RunDailySummaryMixin,
    collect_digest,
    data_dir_of,
    list_summaries,
    load_summary,
    save_summary,
    summary_dir,
    window,
)

__all__ = [
    "DAILY_SUMMARY_DIR",
    "DEFAULT_RETENTION_DAYS",
    "DSH_CACHE_DIRS",
    "DSH_SESSIONS_DIR",
    "DailySummary",
    "DshCleanupResult",
    "IMAGE_QUERY_LIMIT",
    "ImageReportStats",
    "ModelUsageReport",
    "RetentionCleanup",
    "RunDailyReportMixin",
    "RunDailySummaryMixin",
    "RunDshCleanupMixin",
    "Service",
    "UNKNOWN_MODEL",
    "USAGE_QUERY_LIMIT",
    "cleanup_dsh",
    "cleanup_sandbox",
    "collect_digest",
    "compute_entry_cost",
    "currency_symbol",
    "data_dir_of",
    "format_daily_report",
    "format_money",
    "human_int",
    "is_off_peak",
    "list_summaries",
    "load_summary",
    "log_query_days",
    "new_service",
    "new_service_with_config",
    "offpeak_price",
    "parse_log_int",
    "price_for",
    "remove_empty_dirs",
    "resolve_dsh_home",
    "save_summary",
    "setup_cron",
    "summary_dir",
    "upsert_or_disable",
    "window",
]
