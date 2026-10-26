"""装配根：配置、日志、主存储、聊天历史、维护服务与 cron 管理器。

对应 Go：``internal/app/foundation.go``。

``defaultFoundationFactory.Build`` 是**同步**的（Go 侧也同步）：配置加载、
SQLite 打开、日志管理器创建都不需要事件循环。Python 侧把它写成 ``async def``，
以便 :func:`atri_x.app.runner.maybe_await` 统一调用 —— 内部没有 ``await``，
纯粹为了接口一致。

与 Go 的差异：

* Go 用 ``defer`` 在半成品状态下回滚（``foundationLifecycle.Close``）；
  Python 用 ``try/except`` 显式调用同一个回滚路径。
* Go 的 ``config.Load`` 返回 ``(*Config, error)``；Python 的
  :func:`atri_x.config.load` 直接抛异常。
* ``validateWorkModel`` 会往 stderr 写一行中文/英文混排提示，这里经
  :func:`_eprint` 兜底编码（控制台是 GBK）。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from atri_x.app.cron import enabled_cron_platforms, start_cron_async
from atri_x.app.dependencies import FoundationComponents, FoundationRequest
from atri_x.app.environment import startup_profile_enabled
from atri_x.config import load as _load_config
from atri_x.cron import Manager as CronManager
from atri_x.errors import InvalidError
from atri_x.historygate import new_policy, wrap_chat, wrap_outbound
from atri_x.logging import Manager as LogManager
from atri_x.maintenance import new_service_with_config
from atri_x.storage.sqlite import new as _new_store
from atri_x.storage.sqlite import new_chat_history as _new_chat_history
from atri_x.time_service import TimeService
from atri_x.timesync import Verifier, VerifierOptions

__all__ = [
    "DefaultFoundationFactory",
    "defaultFoundationFactory",
    "FoundationLifecycle",
    "foundationLifecycle",
    "logStartupConfiguration",
    "log_startup_configuration",
    "validateWorkModel",
    "validate_work_model",
    "with_config_env_dir",
]


def _safe(text: str) -> str:
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "backslashreplace").decode("ascii")
    return text


def _eprint(text: str) -> None:
    try:
        print(_safe(text), file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def with_config_env_dir(ctx: Any, config_dir: str) -> Any:
    """``builtin.WithConfigEnvDir``：把配置目录挂到 ctx 上。

    延迟导入 ``atri_x.tool.builtin``（它很大，且 app 的其它路径不需要它）。
    """
    from atri_x.tool.builtin.common import with_config_env_dir as _impl

    return _impl(ctx, config_dir)


class FoundationLifecycle:
    """基础组件的关停顺序与 cron 启动等待。对应 Go ``foundationLifecycle``。"""

    def __init__(self, cfg: Any, logs: Any) -> None:
        self.cfg = cfg
        self.logs = logs
        self.store: Any = None
        self.chat_history_store: Any = None
        self.cron_manager: Any = None
        #: Python 版新增：网络时间校验器（``[time.verification]``）。关机时先停它。
        self.time_verifier: Any = None
        self._cron_startup_done: asyncio.Event | None = None
        self._cron_scheduled = False
        self._cron_task: asyncio.Task | None = None

    @property
    def cron_scheduled(self) -> bool:
        return self._cron_scheduled

    async def start_cron(self, ctx: Any, service: Any) -> None:
        """对应 Go ``foundationLifecycle.startCron``：幂等，只启动一次。"""
        if self.cron_manager is None or service is None or self._cron_scheduled:
            return
        self._cron_scheduled = True
        self._cron_startup_done = asyncio.Event()
        logger = _runtime_logger(self.logs)
        self._cron_task = start_cron_async(
            ctx,
            self.cron_manager,
            service,
            self.cfg,
            logger,
            self._cron_startup_done,
        )

    async def close(self, ctx: Any = None) -> None:
        """对应 Go ``foundationLifecycle.Close``：逐个关停并聚合异常。"""
        errors: list[BaseException] = []
        # 网络时间校验最先停：它是最外围的定时 I/O，不能在建库关停后还跑。
        if self.time_verifier is not None:
            try:
                await asyncio.wait_for(self.time_verifier.stop(), _timeout(ctx))
            except asyncio.TimeoutError:
                errors.append(TimeoutError("stop time verifier: timed out"))
            except Exception as exc:  # noqa: BLE001 - 继续关停其余组件
                errors.append(exc)
        if self._cron_scheduled and self._cron_startup_done is not None:
            try:
                await asyncio.wait_for(self._cron_startup_done.wait(), _timeout(ctx))
            except asyncio.TimeoutError:
                errors.append(TimeoutError("wait cron startup: timed out"))
        if self.cron_manager is not None:
            try:
                await asyncio.wait_for(_maybe_await(self.cron_manager.stop()), _timeout(ctx))
            except asyncio.TimeoutError:
                errors.append(TimeoutError("stop cron manager: timed out"))
            except Exception as exc:  # noqa: BLE001 - 继续关停其余组件
                errors.append(exc)
        if self.chat_history_store is not None:
            try:
                _maybe_close(self.chat_history_store)
            except Exception as exc:  # noqa: BLE001
                errors.append(InvalidError(f"close chat history store: {exc}"))
        if self.store is not None:
            try:
                _maybe_close(self.store)
            except Exception as exc:  # noqa: BLE001
                errors.append(InvalidError(f"close main store: {exc}"))
        if self.logs is not None:
            try:
                self.logs.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(InvalidError(f"close logs: {exc}"))
        if errors:
            if all(isinstance(item, Exception) for item in errors):
                raise ExceptionGroup("foundation close errors", errors)  # type: ignore[arg-type]
            raise errors[0]

    # Go 命名别名。
    startCron = start_cron
    Close = close


def _timeout(ctx: Any) -> float:
    """关停步骤的等待上限；``ctx`` 若带 ``timeout`` 就用它。"""
    value = getattr(ctx, "timeout", None)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return 30.0


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _maybe_close(obj: Any) -> Any:
    close = getattr(obj, "close", None)
    if callable(close):
        return close()
    return None


def _runtime_logger(logs: Any) -> Any:
    if logs is None:
        return None
    method = getattr(logs, "runtime", None)
    return method() if callable(method) else None


def _audit_func(logs: Any) -> Any:
    """基础组件的审计入口（与 :func:`atri_x.app.runtime_support.audit_func` 同语义）。

    刻意在本模块写一份 12 行的实现：``runtime_support`` 依赖 app 层的请求结构体，
    从这里 import 它会把 foundation 拉进更深的导入链。审计是"事件名 + 字段"的
    简单契约，重复实现的代价远小于循环导入。
    """
    if logs is None:
        return None

    def audit(event: str, **attrs: Any) -> None:
        sink = logs.audit()
        emit = getattr(sink, "info", None)
        if not callable(emit):
            return
        fields: dict[str, Any] = {"event": event, "module": "app"}
        fields.update(attrs)
        emit("audit event", **fields)

    return audit


class DefaultFoundationFactory:
    """对应 Go ``defaultFoundationFactory``。"""

    async def build(self, req: FoundationRequest) -> FoundationComponents:
        """对应 Go ``Build``。"""
        options = req.options
        profiler = req.profiler
        cfg = _load_config(getattr(options, "config_path", "") or "")
        profiler.set_enabled(
            startup_profile_enabled(getattr(getattr(cfg, "runtime", None), "log_level", ""))
        )
        profiler.mark("config.Load")
        # Python 版新增（1.0.10）：统一时间服务。时区非法 / tzdata 缺失在这里
        # **直接失败**（不回落），并带上修复指引。
        time_service = TimeService.from_config(getattr(cfg, "time", None))
        profiler.mark("time service")
        # Go 在这里做 ``ctx = builtin.WithConfigEnvDir(ctx, dir)`` 并把它传给
        # sqlite.New。Python 侧的构造器不接收 ctx，内置工具改由
        # RegisterOptions.config_dir 获得配置目录，因此这一步没有消费者；
        # 保留函数 with_config_env_dir 供需要 ctx 的调用方复用。

        runtime_cfg = getattr(cfg, "runtime", None)
        storage_cfg = getattr(cfg, "storage", None)
        logs = LogManager(
            getattr(runtime_cfg, "log_level", "") or "",
            getattr(storage_cfg, "sessions_sqlite_path", "") or "",
            int(getattr(runtime_cfg, "log_retention_days", 0) or 0),
        )
        lifecycle = FoundationLifecycle(cfg, logs)
        try:
            profiler.mark("logging.NewManager")
            logger = logs.runtime()
            log_startup_configuration(logger, options, cfg)
            validate_work_model(cfg)

            store = _new_store(getattr(storage_cfg, "sessions_sqlite_path", "") or "")
            lifecycle.store = store
            profiler.mark("sqlite.New")

            chat_history_store = _new_chat_history(
                getattr(storage_cfg, "chat_history_sqlite_path", "") or ""
            )
            lifecycle.chat_history_store = chat_history_store
            profiler.mark("chat history sqlite.New")

            history_policy = new_policy()
            chat_history = wrap_chat(_repository(chat_history_store), history_policy)
            outbound_messages = wrap_outbound(_outbound(chat_history_store), history_policy)

            maint = new_service_with_config(
                logs, store, chat_history, outbound_messages, cfg, logger
            )
            # Python 版新增（1.0.10）：网络时间校验器。构造是纯内存操作；真正的
            # 后台循环在 ``runner.after_start`` 才 ``start()``（需要事件循环）。
            time_cfg = getattr(cfg, "time", None)
            time_verifier = Verifier(
                VerifierOptions(
                    config=getattr(time_cfg, "verification", None),
                    time_service=time_service,
                    sources=list(getattr(time_cfg, "sources", None) or []),
                    logger=logger,
                    audit=_audit_func(logs),
                    max_sample_age_seconds=int(
                        getattr(time_cfg, "max_sample_age_seconds", 0) or 0
                    ),
                )
            )
            lifecycle.time_verifier = time_verifier
            profiler.mark("time verifier prepared")
            # cron 的时区与"现在"必须与业务时间同源（否则 DST / 跨时区部署下
            # 预约与提醒的口径会分裂）。
            cron_manager = CronManager(
                _cron_jobs(store),
                logger,
                location=time_service.timezone(),
                now=time_service.now_utc,
            )
            lifecycle.cron_manager = cron_manager
            await maint.register_cron_handlers(cron_manager)
            profiler.mark("cron async prepared")
        except BaseException as exc:
            try:
                await lifecycle.close(None)
            except BaseException as close_exc:  # noqa: BLE001
                raise ExceptionGroup(
                    "foundation build failed and cleanup failed", [exc, close_exc]
                ) from exc
            raise

        return FoundationComponents(
            maintenance=maint,
            history_policy=history_policy,
            config=cfg,
            logs=logs,
            logger=logger,
            store=store,
            chat_history_store=chat_history_store,
            chat_history=chat_history,
            outbound_messages=outbound_messages,
            cron_manager=cron_manager,
            start_cron=lifecycle.start_cron,
            lifecycle=lifecycle,
            time_service=time_service,
            time_verifier=time_verifier,
        )

    # Go 命名别名。
    Build = build


def _repository(chat_history_store: Any) -> Any:
    method = getattr(chat_history_store, "repository", None)
    return method() if callable(method) else chat_history_store


def _outbound(chat_history_store: Any) -> Any:
    method = getattr(chat_history_store, "outbound", None)
    return method() if callable(method) else None


def _cron_jobs(store: Any) -> Any:
    method = getattr(store, "cron_jobs", None)
    return method() if callable(method) else None


def log_startup_configuration(logger: Any, opts: Any, cfg: Any) -> None:
    """对应 Go ``logStartupConfiguration``（键名逐字照抄）。"""
    emit = getattr(logger, "info", None)
    if not callable(emit):
        return
    mode_models = getattr(cfg, "mode_models", None) or {}
    storage = getattr(cfg, "storage", None)
    soul = getattr(cfg, "soul", None)
    emit(
        "atri-x started",
        version=getattr(opts, "version", ""),
        config_path=cfg.config_path,
        services_config_path=cfg.services_config_path,
        providers_config_path=cfg.providers_config_path,
        state_config_path=cfg.state_config_path,
        atrinis_config_path=cfg.atrinis_config_path,
        work_provider=_selection_field(mode_models, "work", "provider"),
        work_model=_selection_field(mode_models, "work", "model"),
        chat_provider=_selection_field(mode_models, "chat", "provider"),
        chat_model=_selection_field(mode_models, "chat", "model"),
        soul_path=getattr(soul, "path", ""),
        sessions_sqlite_path=getattr(storage, "sessions_sqlite_path", ""),
        chat_history_sqlite_path=getattr(storage, "chat_history_sqlite_path", ""),
        timezone=str(getattr(getattr(cfg, "time", None), "timezone", "") or ""),
    )


def _selection_field(mode_models: Any, mode: str, field: str) -> str:
    selection = mode_models.get(mode) if hasattr(mode_models, "get") else None
    return str(getattr(selection, field, "") or "")


def validate_work_model(cfg: Any) -> None:
    """对应 Go ``validateWorkModel``。

    work 模型缺失/未配 provider 时抛 :class:`atri_x.errors.InvalidError`；
    前一条还会在 stderr 打印带路径的可操作提示。
    """
    mode_models = getattr(cfg, "mode_models", None) or {}
    work = mode_models.get("work") if hasattr(mode_models, "get") else None
    provider = str(getattr(work, "provider", "") or "")
    model = str(getattr(work, "model", "") or "")
    if not provider or not model:
        provider_config_path = cfg.services_config_path or cfg.providers_config_path
        _eprint(
            "atri-x: no work model configured. Set [mode_models.work] provider/model "
            f"in {provider_config_path} or {cfg.state_config_path}"
        )
        raise InvalidError("no work model configured")
    providers = getattr(cfg, "providers", None) or {}
    if provider not in providers:
        raise InvalidError(f'provider "{provider}" not found in config')


# Go 命名别名。
defaultFoundationFactory = DefaultFoundationFactory
foundationLifecycle = FoundationLifecycle
logStartupConfiguration = log_startup_configuration
validateWorkModel = validate_work_model
