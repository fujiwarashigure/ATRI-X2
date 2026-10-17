"""维护任务：日志/会话/沙盒/聊天历史/隐私数据清理，以及 cron 装配。

对应 Go：``internal/maintenance/maintenance.go``。

:class:`Service` 把五类清理任务收在一处，并对外提供两个装配入口：

* :meth:`Service.register_cron_handlers` —— 注册 7 个 handler 名（cron 侧只管调度，
  业务逻辑在这里）；
* :func:`setup_cron` —— 按配置把任务 upsert 或停用，最后启动调度器。

**与 Go 的 async/sync 取舍**：Go 每个方法都收 ``ctx``。本仓的下游一半是同步的
（``atri_x.storage`` / ``atri_x.session`` / ``atri_x.logging`` 都用
``threading.Lock`` + 同步 SQLite），一半是异步的（``atri_x.media`` 的
``cleanup`` / ``reconcile_history``，以及 ``atri_x.cron`` 的 Manager）。为了不给
调用方留"有的要 await 有的不要"的陷阱，本模块的 ``run_*`` / ``register_cron_handlers``
/ ``setup_cron`` **一律是 async**：同步下游直接调用，异步下游 ``await``。
``ctx`` 只在传给异步下游时使用（同步下游不需要取消信号，其单次操作是原子的）。

**与 Go 的日志回调**：Go 用 ``*slog.Logger``（``s.logger.Info(msg, args...)``）。
本仓按 :mod:`atri_x.logging` 的 ``Logger`` 口径调用
``logger.info(msg, *attrs)``（扁平键值对），并用鸭子类型接受任何提供
``info``/``warn`` 的对象；未配置时静默。
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.config import (
    AppConfig,
    ChatHistoryCleanupConfig,
    DailyReportConfig,
    MaintenanceCleanupConfig,
)
from atri_x.errors import NotFoundError
from atri_x.maintenance.report import RunDailyReportMixin
from atri_x.session import new_service as new_session_service
from atri_x.storage import ErrNotFound

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "RetentionCleanup",
    "Service",
    "cleanup_sandbox",
    "new_service",
    "new_service_with_config",
    "remove_empty_dirs",
    "setup_cron",
    "upsert_or_disable",
]

#: 没有专门保留期配置时的默认保留天数。对应 Go 里的字面量 ``180``。
DEFAULT_RETENTION_DAYS = 180


@dataclass
class RetentionCleanup:
    """一个由业务模块自带的清理回调。对应 Go ``retentionCleanup``。"""

    name: str = ""
    fn: Callable[..., Any] | None = None


class Service(RunDailyReportMixin):
    """维护任务服务。对应 Go ``Service``。

    构造与 Go 的两个构造器对应：:meth:`Service.__init__` 是
    ``NewService``（只带会话清理配置），:meth:`Service.with_config` 是
    ``NewServiceWithConfig``（从整个 ``Config`` 取全部维护配置）。
    日报相关方法来自 :class:`atri_x.maintenance.report.RunDailyReportMixin`
    （对应 Go ``report.go`` 里挂在同一个 ``Service`` 上的方法）。
    """

    def __init__(
        self,
        logs: Any = None,
        store: Any = None,
        session_cleanup: MaintenanceCleanupConfig | None = None,
        logger: Any = None,
    ) -> None:
        self.media: Any = None
        self.logs = logs
        self.store = store
        self.chat_history: Any = None
        self.outbound_messages: Any = None
        self.session_cleanup = session_cleanup or MaintenanceCleanupConfig()
        self.chat_history_cleanup = ChatHistoryCleanupConfig()
        self.privacy_cleanup = MaintenanceCleanupConfig()
        self.sandbox_root = ""
        self.sandbox_cleanup = MaintenanceCleanupConfig()
        # Go 的 ``report`` 是值类型 ``config.DailyReportConfig``，零值也能用；
        # Python 侧同样给一个零值对象（而不是 ``None``），否则 NewService 路径下
        # 的 RunDailyReport 会解引用失败。
        self.report = DailyReportConfig()
        self.logger = logger
        self.angel_memory_retention_days = 0
        self.self_learning_retention_days = 0
        self.retention_cleanups: list[RetentionCleanup] = []
        #: 日报投递回调（由 app 装配到超级管理员）。``report(ctx, text)``，
        #: 同步或异步都可以。
        self.report_sender: Callable[..., Any] | None = None

    @classmethod
    def with_config(
        cls,
        logs: Any,
        store: Any,
        chat_history: Any,
        outbound_messages: Any,
        cfg: AppConfig | None,
        logger: Any = None,
    ) -> "Service":
        """按整份配置构造。对应 Go ``NewServiceWithConfig``。

        ``cfg`` 为 ``None`` 时退回 :class:`Service` 的默认构造（同 Go）。
        """
        if cfg is None:
            return cls(logs, store, MaintenanceCleanupConfig(), logger)
        service = cls(logs, store, cfg.maintenance.session_cleanup, logger)
        service.chat_history = chat_history
        service.outbound_messages = outbound_messages
        service.chat_history_cleanup = cfg.maintenance.chat_history_cleanup
        service.privacy_cleanup = cfg.maintenance.privacy_cleanup
        service.sandbox_root = cfg.sandbox.root
        service.sandbox_cleanup = cfg.maintenance.sandbox_cleanup
        service.report = cfg.maintenance.daily_report
        service.angel_memory_retention_days = cfg.angel_memory.retention_days
        service.self_learning_retention_days = cfg.self_learning.retention_days
        return service

    # -- 装配 ---------------------------------------------------------------

    def register_retention_cleanup(
        self, name: str, fn: Callable[..., Any] | None
    ) -> None:
        """注册业务模块自带的清理回调。对应 Go ``RegisterRetentionCleanup``。

        回调在 ``privacy_cleanup`` 里按名字选 cutoff 后调用：名字含 ``angel`` 用
        ``[angel_memory].retention_days``，含 ``learning`` 用
        ``[self_learning].retention_days``，其余用 :data:`DEFAULT_RETENTION_DAYS`。
        """
        if fn is None:
            return
        self.retention_cleanups.append(
            RetentionCleanup(name=(name or "").strip(), fn=fn)
        )

    async def register_cron_handlers(self, manager: Any) -> None:
        """把 7 个维护 handler 注册到 cron 管理器。对应 Go ``RegisterCronHandlers``。

        ``manager`` 为 ``None`` 时直接返回（同 Go）。``manager.register_handler``
        是协程（``atri_x.cron.Manager`` 的约定），因此本方法也是协程。
        """
        if manager is None:
            return None
        await manager.register_handler(
            "maintenance.log_cleanup", lambda ctx, job: self.run_log_cleanup(ctx)
        )
        await manager.register_handler(
            "maintenance.session_cleanup", lambda ctx, job: self.run_session_cleanup(ctx)
        )
        await manager.register_handler(
            "maintenance.sandbox_cleanup", lambda ctx, job: self.run_sandbox_cleanup(ctx)
        )
        await manager.register_handler(
            "maintenance.media_cleanup", lambda ctx, job: self._run_media_cleanup(ctx)
        )
        await manager.register_handler(
            "maintenance.chat_history_cleanup",
            lambda ctx, job: self.run_chat_history_cleanup(ctx),
        )
        await manager.register_handler(
            "maintenance.privacy_cleanup", lambda ctx, job: self.run_privacy_cleanup(ctx)
        )
        await manager.register_handler(
            "maintenance.daily_report", lambda ctx, job: self.run_daily_report(ctx)
        )
        return None

    async def _run_media_cleanup(self, ctx: Any) -> None:
        """媒体清理（未配置媒体中心时为 no-op）。对应 Go 的 media handler。"""
        if self.media is None:
            return None
        await _maybe_await(self.media.cleanup(ctx))
        return None

    # -- 各项清理 -----------------------------------------------------------

    async def run_log_cleanup(self, ctx: Any) -> None:
        """清理旧日志。对应 Go ``RunLogCleanup``。"""
        try:
            self.logs.cleanup_old_logs()
        except Exception as exc:  # noqa: BLE001 - 记录后向上抛（Go 的 return err）
            self.warn("maintenance log cleanup failed", "error", exc)
            raise
        self.info(
            "maintenance log cleanup completed",
            "retention_days",
            self.logs.retention_days(),
        )
        return None

    async def run_session_cleanup(self, ctx: Any) -> None:
        """清理过期会话。对应 Go ``RunSessionCleanup``。

        与 Go 一致：``session_cleanup.enabled`` 为假时直接返回（不报错）。
        """
        if not self.session_cleanup.enabled:
            return None
        cutoff = _now() - timedelta(days=self.session_cleanup.retention_days)
        try:
            deleted = new_session_service(self.store).cleanup_expired(cutoff)
        except Exception as exc:  # noqa: BLE001
            self.warn(
                "maintenance session cleanup failed",
                "error",
                exc,
                "retention_days",
                self.session_cleanup.retention_days,
            )
            raise
        self.info(
            "maintenance session cleanup completed",
            "deleted",
            deleted,
            "retention_days",
            self.session_cleanup.retention_days,
        )
        return None

    async def run_chat_history_cleanup(self, ctx: Any) -> None:
        """清理聊天历史与出站消息，并对账历史媒体。对应 Go ``RunChatHistoryCleanup``。"""
        cfg = self.chat_history_cleanup
        if not cfg.enabled or cfg.retention_days <= 0 or self.chat_history is None:
            return None
        cutoff = _now() - timedelta(days=cfg.retention_days)
        try:
            deleted = self.chat_history.delete_before(cutoff)
        except Exception as exc:  # noqa: BLE001
            self.warn(
                "maintenance chat history cleanup failed",
                "error",
                exc,
                "retention_days",
                cfg.retention_days,
            )
            raise
        outbound_deleted = 0
        if self.outbound_messages is not None:
            try:
                outbound_deleted = self.outbound_messages.delete_before(cutoff)
            except Exception as exc:  # noqa: BLE001
                self.warn(
                    "maintenance outbound message cleanup failed",
                    "error",
                    exc,
                    "retention_days",
                    cfg.retention_days,
                )
                raise
        if self.media is not None:
            await _maybe_await(self.media.reconcile_history(ctx))
        self.info(
            "maintenance chat history cleanup completed",
            "deleted",
            deleted,
            "outbound_deleted",
            outbound_deleted,
            "retention_days",
            cfg.retention_days,
        )
        return None

    async def run_privacy_cleanup(self, ctx: Any) -> None:
        """按保留期清理各业务模块的自有数据。对应 Go ``RunPrivacyCleanup``。

        单个回调抛错时只告警并 ``continue``（Go 的 ``continue``），不影响其余
        模块；名字命中 ``angel`` / ``learning`` 但对应保留期 <= 0 时**跳过**该回调。
        """
        now = _now()
        succeeded: set[str] = set()
        for cleanup in self.retention_cleanups:
            if "angel" in cleanup.name:
                if self.angel_memory_retention_days <= 0:
                    continue
                cutoff = now - timedelta(days=self.angel_memory_retention_days)
            elif "learning" in cleanup.name:
                if self.self_learning_retention_days <= 0:
                    continue
                cutoff = now - timedelta(days=self.self_learning_retention_days)
            else:
                cutoff = now - timedelta(days=DEFAULT_RETENTION_DAYS)
            try:
                await _maybe_await(cleanup.fn(ctx, cutoff))
            except Exception as exc:  # noqa: BLE001 - 单模块失败不中断整轮
                self.warn(
                    "maintenance privacy cleanup failed", "feature", cleanup.name, "error", exc
                )
                continue
            succeeded.add(cleanup.name)
        self.info(
            "maintenance privacy cleanup completed",
            "features",
            len(succeeded),
            "angel_memory_retention_days",
            self.angel_memory_retention_days,
            "self_learning_retention_days",
            self.self_learning_retention_days,
        )
        return None

    async def run_sandbox_cleanup(self, ctx: Any) -> None:
        """清理沙盒里过期的产物文件。对应 Go ``RunSandboxCleanup``。"""
        if not self.sandbox_root or self.sandbox_cleanup.retention_days <= 0:
            return None
        cutoff = _now() - timedelta(days=self.sandbox_cleanup.retention_days)
        try:
            deleted = await _maybe_await(cleanup_sandbox(ctx, self.sandbox_root, cutoff))
        except Exception as exc:  # noqa: BLE001
            self.warn(
                "maintenance sandbox cleanup failed",
                "error",
                exc,
                "retention_days",
                self.sandbox_cleanup.retention_days,
            )
            raise
        self.info(
            "maintenance sandbox cleanup completed",
            "deleted",
            deleted,
            "retention_days",
            self.sandbox_cleanup.retention_days,
        )
        return None

    # -- 日志 ---------------------------------------------------------------

    def info(self, msg: str, *attrs: Any) -> None:
        """info 日志（未配置 logger 时静默）。对应 Go ``s.info``。"""
        logger = self.logger
        if logger is None:
            return
        emit = getattr(logger, "info", None)
        if callable(emit):
            emit(msg, *attrs)

    def warn(self, msg: str, *attrs: Any) -> None:
        """warn 日志（未配置 logger 时静默）。对应 Go ``s.warn``。"""
        logger = self.logger
        if logger is None:
            return
        emit = getattr(logger, "warn", None) or getattr(logger, "warning", None)
        if callable(emit):
            emit(msg, *attrs)


# ---------------------------------------------------------------------------
# 沙盒清理
# ---------------------------------------------------------------------------


async def cleanup_sandbox(ctx: Any, dir: str, cutoff: datetime) -> int:
    """删除沙盒里 ``mtime`` 早于 cutoff 的**文件**，再尝试删空目录。

    对应 Go ``cleanupSandbox``。返回删除的文件数；目录不存在时返回 0（不报错）。
    与 Go 一致：只删文件，目录的 mtime 不参与判定，最后自底向上尝试 ``rmdir``
    并忽略失败（非空目录删不掉是正常的）。
    """
    root = (dir or "").strip()
    if not root or not os.path.exists(root):
        return 0
    if not os.path.isdir(root):
        raise NotADirectoryError(f"sandbox root is not a directory: {root}")
    deleted = 0
    for current, _dirnames, filenames in os.walk(root):
        _raise_if_cancelled(ctx)
        for name in filenames:
            path = os.path.join(current, name)
            _raise_if_cancelled(ctx)
            try:
                modified = os.stat(path).st_mtime
            except OSError:
                continue
            if modified < cutoff.timestamp():
                try:
                    os.remove(path)
                except OSError:
                    raise
                deleted += 1
    remove_empty_dirs(root)
    return deleted


def remove_empty_dirs(root: str) -> None:
    """自底向上删除空目录（失败忽略）。对应 Go ``removeEmptyDirs``。"""
    dirs: list[str] = []
    for current, dirnames, _filenames in os.walk(root):
        for name in dirnames:
            path = os.path.join(current, name)
            if path != root:
                dirs.append(path)
    for path in reversed(dirs):
        try:
            os.rmdir(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# cron 装配
# ---------------------------------------------------------------------------


async def setup_cron(ctx: Any, manager: Any, cfg: AppConfig | None) -> None:
    """按配置装配维护任务并启动调度器。对应 Go ``SetupCron``。

    ``manager`` 或 ``cfg`` 为 ``None`` 时直接返回（同 Go）。七条任务的顺序与
    schedule 来源与 Go 逐条对应；``media_cleanup`` 硬编码启用并沿用 sandbox
    cleanup 的 schedule（Go 里传的字面量就是 ``cfg.Maintenance.SandboxCleanup.Schedule``）。
    """
    if manager is None or cfg is None:
        return None
    maintenance = cfg.maintenance
    jobs = (
        (
            maintenance.log_cleanup.enabled,
            "maintenance.log_cleanup",
            maintenance.log_cleanup.schedule,
        ),
        (
            maintenance.session_cleanup.enabled,
            "maintenance.session_cleanup",
            maintenance.session_cleanup.schedule,
        ),
        (
            maintenance.sandbox_cleanup.enabled,
            "maintenance.sandbox_cleanup",
            maintenance.sandbox_cleanup.schedule,
        ),
        (
            maintenance.chat_history_cleanup.enabled,
            "maintenance.chat_history_cleanup",
            maintenance.chat_history_cleanup.schedule,
        ),
        (
            True,
            "maintenance.media_cleanup",
            maintenance.sandbox_cleanup.schedule,
        ),
        (
            maintenance.privacy_cleanup.enabled,
            "maintenance.privacy_cleanup",
            maintenance.privacy_cleanup.schedule,
        ),
        (
            maintenance.daily_report.enabled,
            "maintenance.daily_report",
            maintenance.daily_report.schedule,
        ),
    )
    for enabled, handler, schedule in jobs:
        await upsert_or_disable(
            ctx, manager, enabled, "system." + handler, handler, schedule
        )
    await _maybe_await(manager.start(ctx))
    return None


async def upsert_or_disable(
    ctx: Any,
    manager: Any,
    enabled: bool,
    name: str,
    handler: str,
    schedule: str,
) -> None:
    """启用则 upsert，否则停用（停用时吞掉"任务不存在"）。对应 Go ``upsertOrDisable``。"""
    if enabled:
        await _maybe_await(
            manager.upsert_job(
                _upsert_request(name=name, handler=handler, schedule=schedule)
            )
        )
        return None
    try:
        await _maybe_await(manager.disable_job(name))
    except Exception as exc:  # noqa: BLE001 - 只吞"没这个任务"
        if exc is ErrNotFound or isinstance(exc, NotFoundError):
            return None
        raise
    return None


def _upsert_request(name: str, handler: str, schedule: str) -> Any:
    """构造 ``atri_x.cron.UpsertJobRequest``（延迟导入）。

    延迟导入的原因：``atri_x.cron`` 的导入链在 ``model.py`` 里会抛
    ``NameError``（既有问题，不属于本模块），模块级 import 会让
    ``atri_x.maintenance`` 一起无法导入。这里在**调用点**才 import，因此
    ``setup_cron`` 之前 maintenance 一直可用；测试通过注入
    ``sys.modules["atri_x.cron"]`` 的替身来验证装配逻辑（见
    ``tests/test_maintenance.py``）。
    """
    from atri_x.cron import UpsertJobRequest

    return UpsertJobRequest(name=name, handler=handler, schedule=schedule, enabled=True)


# ---------------------------------------------------------------------------
# Python 侧新增的小工具
# ---------------------------------------------------------------------------


def new_service(
    logs: Any,
    store: Any,
    session_cleanup: MaintenanceCleanupConfig | None,
    logger: Any = None,
) -> Service:
    """构造维护服务。对应 Go ``NewService``。"""
    return Service(logs, store, session_cleanup, logger)


def new_service_with_config(
    logs: Any,
    store: Any,
    chat_history: Any,
    outbound_messages: Any,
    cfg: AppConfig | None,
    logger: Any = None,
) -> Service:
    """按配置构造维护服务。对应 Go ``NewServiceWithConfig``。"""
    return Service.with_config(logs, store, chat_history, outbound_messages, cfg, logger)


async def _maybe_await(value: Any) -> Any:
    """``await`` 一个可能是同步的返回值（Python 侧新增件，同 ``atri_x`` 的既有惯例）。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _raise_if_cancelled(ctx: Any) -> None:
    """``ctx`` 已取消时抛异常（``None`` 视为未取消）。"""
    checker = getattr(ctx, "raise_if_cancelled", None)
    if callable(checker):
        checker()


def _now() -> datetime:
    """当前时间（UTC aware）。对应 Go ``time.Now()``。"""
    return datetime.now(timezone.utc)
