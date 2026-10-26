"""运行期装配的共享助手：审计入口、凭据解析、cron 服务、Hook 生命周期。

对应 Go：``internal/app/runtime.go`` 里的无状态助手与 ``hookRuntimeLifecycle``。

从 :mod:`atri_x.app.runtime` 拆出（PORTING §2 单文件行数上限），被
:mod:`atri_x.app.runtime_hooks` / :mod:`atri_x.app.runtime_agent` 共用；
**本模块不得反向 import 那三个模块**，否则会成环。

模块内实现保持下划线命名（对应 Go 的私有助手），文件末尾给出公开别名。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from atri_x.app.cron import enabled_cron_platforms
from atri_x.app.support import call_bool, maybe_await
from atri_x.command import primary_prefix
from atri_x.config import ModelSelection, config_env
from atri_x.cron import Service as CronService
from atri_x.cron import USER_HANDLER_NAME
from atri_x.cron import UpsertJobRequest
from atri_x.delivery import Target, text
from atri_x.errors import InvalidError
from atri_x.groupanalysis import Request as GroupAnalysisRequest

__all__ = [
    "max_int64",
    "maxInt64",
    "config_dir_of",
    "register_turn_alias",
    "audit_func",
    "resolve_file_delivery_credentials",
    "build_cron_service",
    "HookRuntimeLifecycle",
    "hookRuntimeLifecycle",
    "warn",
    "log_error",
    "schedule",
]


# ---------------------------------------------------------------------------
# 无状态助手（与 Go 逐字对应）
# ---------------------------------------------------------------------------


def max_int64(value: int, fallback: int) -> int:
    """对应 Go ``maxInt64``。"""
    return value if value > fallback else fallback


def config_dir_of(cfg: Any) -> str:
    """配置目录（Go 里到处写 ``filepath.Dir(cfg.ConfigPath)``）。"""
    return os.path.dirname(str(getattr(cfg, "config_path", "") or ""))


def register_turn_alias(aliases: dict[str, str], alias: str, name: str) -> None:
    """对应 Go ``registerTurnAlias``：别名小写去空白后指向规范名。"""
    key = (alias or "").strip().lower()
    if not key:
        return
    aliases[key] = name


def audit_func(logs: Any) -> Any:
    """app 层所有审计来源的统一入口。对应 Go ``auditFunc``。

    与 Agent 侧一样补 ``module=app``：来源标识是日志契约的一部分，
    ``/audit --module`` 类的筛选依赖它。
    """

    def audit(event: str, **attrs: Any) -> None:
        from atri_x.logging import ModuleApp

        head: dict[str, Any] = {"event": event, "module": ModuleApp}
        head.update(attrs)
        emit = getattr(logs.audit(), "info", None)
        if callable(emit):
            emit("audit event", **head)

    return audit


def resolve_file_delivery_credentials(cfg: Any, config_dir: str) -> Any:
    """解析 S3 后端需要的静态凭据。对应 Go ``resolveFileDeliveryCredentials``。

    非 s3/hybrid 后端返回 ``None``；缺失环境变量抛错。
    """
    backend = str(getattr(cfg, "backend", "") or "").strip()
    if not backend:
        from atri_x.config import default as default_config

        backend = str(default_config().file_delivery.backend or "")
    if backend not in ("s3", "hybrid"):
        return None

    def resolve(name: str, label: str) -> str:
        name = (name or "").strip()
        if not name:
            raise InvalidError(f"{label} environment variable name is empty")
        value = config_env(name, config_dir)
        if value is None or not value.strip():
            raise InvalidError(
                f"{label} environment variable {name!r} is not configured"
            )
        return value.strip()

    access_key = resolve(getattr(cfg, "s3_access_key_env", ""), "s3 access key")
    secret_key = resolve(getattr(cfg, "s3_secret_key_env", ""), "s3 secret key")
    return {"access_key": access_key, "secret_key": secret_key, "session_token": ""}


async def build_cron_service(
    ctx: Any, foundation: Any, send: Any
) -> CronService:
    """对应 Go ``buildCronService``。"""
    cfg = foundation.config
    service = CronService(
        _cron_options(cfg, foundation, send)
    )
    await maybe_await(service.migrate_legacy_delivery_state(ctx))
    await maybe_await(
        foundation.cron_manager.register_handler(USER_HANDLER_NAME, service.handler)
    )
    return service


def _cron_options(cfg: Any, foundation: Any, send: Any) -> Any:
    from atri_x.cron import Options as CronOptions

    time_service = getattr(foundation, "time_service", None)
    return CronOptions(
        manager=foundation.cron_manager,
        store=foundation.store,
        logger=foundation.logger,
        enabled_platforms=enabled_cron_platforms(cfg),
        sandbox_root=str(getattr(getattr(cfg, "sandbox", None), "root", "") or ""),
        command_prefix=primary_prefix(getattr(getattr(cfg, "commands", None), "prefixes", None)),
        audit=audit_func(foundation.logs),
        send_target=send,
        # 与 Manager 同源：时区与"现在"都来自统一时间服务。
        location=getattr(time_service, "timezone", lambda: None)(),
        now=getattr(time_service, "now_utc", None),
    )


def _warn(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "warn", None)
    if callable(emit):
        emit(message, **fields)


def _log_error(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "error", None)
    if callable(emit):
        emit(message, **fields)


def _schedule(awaitable: Any) -> None:
    """在运行中的事件循环里安排一个协程；没有循环时丢弃（启动期不应发生）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        return
    asyncio.ensure_future(awaitable)



def _group_analysis_summarizer(cfg: Any, models: Any) -> Any:
    selection = (
        cfg.default_model_selection()
        if callable(getattr(cfg, "default_model_selection", None))
        else ModelSelection()
    )
    if not selection.provider or not selection.model:
        return None
    client = (models.by_provider or {}).get(selection.provider)
    if client is None:
        return None
    from atri_x.groupanalysis import LLMSummarizer

    return LLMSummarizer(client, selection.model)


async def _register_group_analysis(
    foundation: Any, req: RuntimeRequest, tool_runtime: Any, send_notice: Any
) -> None:
    """对应 Go 里注册 ``group_analysis.report`` handler + UpsertJob 的那一段。"""
    cfg = foundation.config
    section = getattr(cfg, "group_analysis", None)
    if not call_bool(section, "is_report_enabled") or tool_runtime.group_analysis is None:
        return
    manager = foundation.cron_manager

    async def handler(cctx: Any, job: Any) -> None:
        days = int(getattr(section, "report_days", 0) or 0)
        if days <= 0:
            days = 1
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        report = tool_runtime.group_analysis.analyze(
            GroupAnalysisRequest(
                platform=str(getattr(section, "report_platform", "") or ""),
                scope_id=str(getattr(section, "report_scope_id", "") or ""),
                since=now - timedelta(days=days),
                until=now,
            )
        )
        body = report.format_text()
        summary = await maybe_await(tool_runtime.group_analysis.summarize(cctx, report))
        if summary and str(summary).strip():
            body = "摘要：" + str(summary).strip() + "\n\n" + body
        target = Target(
            platform=str(getattr(section, "report_platform", "") or ""),
            scope_id=str(getattr(section, "report_scope_id", "") or ""),
        )
        if not target.platform and not target.scope_id:
            target.superadmins = True
        await maybe_await(send_notice(cctx, target, [text(body)]))

    await maybe_await(manager.register_handler("group_analysis.report", handler))
    await maybe_await(
        manager.upsert_job(
            UpsertJobRequest(
                name="system.group_analysis.report",
                handler="group_analysis.report",
                schedule=str(getattr(section, "report_schedule", "") or ""),
                enabled=True,
            )
        )
    )


def _register_retention_cleanups(foundation: Any, tool_runtime: Any) -> None:
    """对应 Go 里注册 angel_memory / self_learning 保留期清理的那一段。"""
    maintenance = foundation.maintenance
    if maintenance is None:
        return
    register = getattr(maintenance, "register_retention_cleanup", None)
    if not callable(register):
        return
    if tool_runtime.angel_memory is not None:

        async def cleanup_angel(cctx: Any, cutoff: Any) -> None:
            await maybe_await(tool_runtime.angel_memory.delete_before(cctx, cutoff))

        register("angel_memory", cleanup_angel)
    if tool_runtime.self_learning is not None:

        async def cleanup_learning(cctx: Any, cutoff: Any) -> None:
            await maybe_await(tool_runtime.self_learning.delete_before(cctx, cutoff))

        register("self_learning", cleanup_learning)



# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


class HookRuntimeLifecycle:
    """运行期清理：先停平台事件与通知队列，最后关 Hook 运行时。

    对应 Go ``hookRuntimeLifecycle``。顺序不可换：平台连接的补做本身就跑在 hook
    运行时上，反过来的顺序会打断在途工作。
    """

    def __init__(self, runtime: Any = None, agent: Any = None) -> None:
        self.runtime = runtime
        self.agent = agent

    async def close(self, ctx: Any = None) -> None:
        errors: list[BaseException] = []
        if self.agent is not None:
            for name in ("close_platform_events", "close_notice_queues"):
                method = getattr(self.agent, name, None)
                if not callable(method):
                    continue
                try:
                    await maybe_await(method(ctx))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
        # Python 版新增：DSH 深度路由可能拉起过 node 子进程，必须显式回收。
        if self.agent is not None:
            try:
                from atri_x.agent.dsh.route_call import close_route_service

                await maybe_await(close_route_service(self.agent))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if self.runtime is not None:
            try:
                await maybe_await(self.runtime.close(ctx))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            if all(isinstance(item, Exception) for item in errors):
                raise ExceptionGroup("runtime close errors", errors)  # type: ignore[arg-type]
            raise errors[0]

    # Go 命名别名。
    Close = close



# 公开别名：模块内实现保留下划线命名（与 Go 的私有助手对应）。
warn = _warn
log_error = _log_error
schedule = _schedule
maxInt64 = max_int64
hookRuntimeLifecycle = HookRuntimeLifecycle
