"""Agent 的日志与审计通道。

对应 Go：``internal/agent/logging.go``。

与 Go 的差异（仅形式）：Go 的 ``*slog.Logger`` → :mod:`atri_x.logging` 的
:class:`~atri_x.logging.logging.Logger`（``log(level, message, *key_values)``，
级别用 ``"debug"/"info"/"warn"/"error"`` 字符串）。审计记录同样按
``key, value`` 成对追加，因此本模块把 Go 的 ``attrs ...any`` 原样透传。
"""

from __future__ import annotations

import logging as std_logging
from typing import Any, Protocol, runtime_checkable

from atri_x.logging import LogQuery, ModuleAgent, QueryResult, Reader, ResultFailed
from atri_x.redact import error as redact_error

__all__ = [
    "LogManager",
    "audit_attrs_have_key",
    "normalize_audit_attrs",
    "set_log_manager",
    "set_logger",
]

#: Go slog 级别整数 → atri_x.logging 的级别字符串。
_LEVEL_NAMES = {
    std_logging.DEBUG: "debug",
    std_logging.INFO: "info",
    std_logging.WARNING: "warn",
    std_logging.ERROR: "error",
    std_logging.CRITICAL: "error",
}


def level_name(level: int | str) -> str:
    """把 :mod:`logging` 级别整数映射成 atri_x.logging 的级别字符串。"""
    if isinstance(level, str):
        normalized = level.strip().lower()
        return "warn" if normalized == "warning" else normalized
    return _LEVEL_NAMES.get(int(level), "info")


@runtime_checkable
class LogManager(Protocol):
    """日志中心的最小契约。对应 Go ``LogManager``。

    Go 的 ``Runtime()/Audit()/LogDir()`` → 本仓 :class:`atri_x.logging.Manager`
    的 ``runtime()/audit()/log_dir()``。
    """

    def runtime(self) -> Any: ...
    def audit(self) -> Any: ...
    def log_dir(self) -> str: ...


def set_logger(agent: Any, logger: Any) -> None:
    """对应 Go ``Agent.SetLogger``。"""
    agent.logger = logger
    if getattr(agent, "title_gen", None) is not None:
        agent.title_gen.set_logger(logger)


def set_log_manager(agent: Any, logs: LogManager | None) -> None:
    """对应 Go ``Agent.SetLogManager``。"""
    if logs is None:
        agent.logger = None
        agent.audit_logger = None
        agent.log_reader = None
        if getattr(agent, "title_gen", None) is not None:
            agent.title_gen.set_logger(None)
        return
    agent.logger = logs.runtime()
    agent.audit_logger = logs.audit()
    agent.log_reader = Reader(dir=logs.log_dir())
    if getattr(agent, "title_gen", None) is not None:
        agent.title_gen.set_logger(agent.logger)


def query_logs(agent: Any, ctx: Any, query: LogQuery) -> QueryResult:
    """对应 Go ``Agent.QueryLogs``（本仓日志读取是同步 I/O，因此不是协程）。"""
    reader = getattr(agent, "log_reader", None)
    if reader is None:
        return QueryResult()
    return reader.query(ctx, query)


def audit(agent: Any, event: str, *attrs: Any) -> None:
    """写一条 INFO 级审计记录。对应 Go ``Agent.audit``。"""
    audit_log(agent, std_logging.INFO, event, *attrs)


def audit_debug(agent: Any, event: str, *attrs: Any) -> None:
    """对应 Go ``Agent.auditDebug``。"""
    audit_log(agent, std_logging.DEBUG, event, *attrs)


def audit_warn(agent: Any, event: str, *attrs: Any) -> None:
    """对应 Go ``Agent.auditWarn``。"""
    audit_log(agent, std_logging.WARNING, event, *attrs)


def audit_error(agent: Any, event: str, *attrs: Any) -> None:
    """ERROR 级审计记录按约定就是"这次操作失败了"。

    对应 Go ``Agent.auditError``：调用方没给 ``result`` 时补 ``result=failed``。
    """
    if not audit_attrs_have_key(attrs, "result"):
        attrs = ("result", ResultFailed, *attrs)
    audit_log(agent, std_logging.ERROR, event, *attrs)


def audit_attrs_have_key(attrs: tuple[Any, ...] | list[Any], key: str) -> bool:
    """审计属性里是否已经有该键（按 ``key, value`` 成对给出）。"""
    for index in range(0, len(attrs) - 1, 2):
        name = attrs[index]
        if isinstance(name, str) and name == key:
            return True
    return False


def audit_log(agent: Any, level: int, event: str, *attrs: Any) -> None:
    """写一条审计记录。对应 Go ``Agent.auditLog``。"""
    logger = getattr(agent, "audit_logger", None)
    if logger is None:
        return
    # module 属于来源标识契约：/log --hook、/audit --hook 按 module=hook 过滤，
    # 写入端必须给出自己的来源。event 与 module 放最前，调用方仍可用同名属性覆盖。
    head: tuple[Any, ...] = ("event", event, "module", ModuleAgent, *attrs)
    logger.log(level_name(level), "audit event", *normalize_audit_attrs(head))


def normalize_audit_attrs(attrs: tuple[Any, ...]) -> tuple[Any, ...]:
    """把审计属性里的 error 统一转成脱敏后的字符串。对应 Go ``normalizeAuditAttrs``。"""
    out = list(attrs)
    for index in range(0, len(out) - 1, 2):
        value = out[index + 1]
        if isinstance(value, BaseException):
            out[index + 1] = redact_error(value)
    return tuple(out)
