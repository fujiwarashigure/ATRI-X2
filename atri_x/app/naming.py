"""会话命名事件的日志/审计记录器。

对应 Go：``internal/app/naming.go``。

* 成功/排期走运行日志（受日志级别过滤）；
* **失败进审计**：审计文件不受运行日志级别过滤，命名失败是要排查的事故。
* 审计记录带 ``result=failed``，与 :mod:`atri_x.logging` 的结果词表一致。

参数面（**有意偏离**）：Go 的三个入口都是 ``(ctx, event)``；本仓 session 侧的
:class:`atri_x.session.types.NamingNotifier` 协议只传 ``event``（session 服务本身
不持 ctx），因此这里同步去掉 ``ctx``。保留 Go 的 ``ctx`` 会让第 3 条消息触发命名
时抛 ``TypeError: missing 1 required positional argument: 'event'``，整个回合以
「请求失败」结束（见 ``tests/test_session_lifecycle.py`` 的回归用例）。
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.logging import ModuleApp, ResultFailed

__all__ = ["NamingLogger", "namingLogger"]


class NamingLogger:
    """对应 Go ``namingLogger``。"""

    def __init__(
        self,
        logger: Any = None,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self.logger = logger
        self.audit = audit

    def notify_naming_scheduled(self, event: Any) -> None:
        """对应 Go ``NotifyNamingScheduled``。"""
        if self.logger is None:
            return
        emit = getattr(self.logger, "info", None)
        if not callable(emit):
            return
        emit(
            "session naming scheduled",
            session_id=getattr(event, "session_id", ""),
            message_count=getattr(event, "message_count", 0),
            trigger_step=getattr(event, "trigger_step", 0),
        )

    def notify_naming_completed(self, event: Any) -> None:
        """对应 Go ``NotifyNamingCompleted``。"""
        if self.logger is None:
            return
        emit = getattr(self.logger, "info", None)
        if not callable(emit):
            return
        emit(
            "session naming completed",
            session_id=getattr(event, "session_id", ""),
            title=getattr(event, "title", ""),
            message_count=getattr(event, "message_count", 0),
        )

    def notify_naming_failed(self, event: Any) -> None:
        """对应 Go ``NotifyNamingFailed``。"""
        if self.audit is not None:
            self.audit(
                "session_naming_failed",
                session_id=getattr(event, "session_id", ""),
                stage=getattr(event, "stage", ""),
                llm_call=getattr(event, "llm_call", ""),
                reason=getattr(event, "reason", ""),
                failure_count=getattr(event, "failure_count", 0),
                fallback_applied=getattr(event, "fallback_applied", False),
                error=str(getattr(event, "err", "") or ""),
                result=ResultFailed,
            )
        if self.logger is None:
            return
        emit = getattr(self.logger, "warn", None)
        if not callable(emit):
            return
        emit(
            "session naming failed",
            session_id=getattr(event, "session_id", ""),
            stage=getattr(event, "stage", ""),
            llm_call=getattr(event, "llm_call", ""),
            reason=getattr(event, "reason", ""),
            invalid_reason=getattr(event, "invalid_reason", ""),
            title=getattr(event, "title", ""),
            generated_title_raw=getattr(event, "generated_title_raw", ""),
            generated_title_normalized=getattr(event, "generated_title_normalized", ""),
            message_count=getattr(event, "message_count", 0),
            failure_count=getattr(event, "failure_count", 0),
            max_failures=getattr(event, "max_failures", 0),
            fallback_applied=getattr(event, "fallback_applied", False),
            fallback_title=getattr(event, "fallback_title", ""),
            error=str(getattr(event, "err", "") or ""),
        )

    # Go 命名别名。
    NotifyNamingScheduled = notify_naming_scheduled
    NotifyNamingCompleted = notify_naming_completed
    NotifyNamingFailed = notify_naming_failed


# 交叉检索：审计来源标识属日志契约的一部分。
AUDIT_MODULE = ModuleApp

namingLogger = NamingLogger
