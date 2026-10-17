"""Atrinis 的事件状态收尾、审计/日志与哈希工具。

对应 Go：``internal/atrinis/store.go``。

这里集中三件容易写错的事：

* **终态写入必须顶住取消**。事件进入 ``failed`` / ``completed`` 时会释放媒体引用，
  如果此时调用方已被取消就直接放弃，会留下永久占用磁盘的孤儿媒体。Go 用
  ``context.WithoutCancel`` + 5 秒超时做"清理上下文"；Python 侧对应
  :func:`cleanup_token`：拿一个**未取消**的短超时 token 做收尾。
* **审计属性是扁平键值对**（Go 的 ``attrs ...any`` = ``[key, value, ...]``）。
  Python 侧沿用本仓 ``atri_x.agent.logging`` 的同一约定：位置参数按 key/value 成对。
* **去重键与工具哈希**都是 SHA-256 十六进制小写，字段口径见 :func:`content_hash`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from atri_x.atriena import Event, Request
from atri_x.errors import AtriXError
from atri_x.safecontext import with_timeout
from atri_x.storage import (
    AtrinisEvent,
    UpdateAtrinisEventRequest,
)

__all__ = [
    "StoreMixin",
    "background_tool_names",
    "cleanup_token",
    "content_hash",
    "hash_bytes",
    "hash_text",
    "set_from_strings",
    "trim_strings",
]

#: 终态收尾上下文的超时（秒）。对应 Go 的 ``5*time.Second``。
TERMINAL_CLEANUP_TIMEOUT_SECONDS = 5.0


def cleanup_token(ctx: Any) -> Any:
    """终态收尾用的"未被取消"短超时上下文。

    对应 Go ``context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)``。
    Go 的 ``WithoutCancel`` 是 Go 1.21 新增的；Python 侧的 :class:`CancelToken`
    没有"脱钩父级"的构造，因此这里直接造一个**无父**的根 token——语义等价于
    "取消不再传播过来"，并已由 :func:`atri_x.safecontext.with_timeout` 自动登记
    atexit 兜底取消。
    """
    return with_timeout(None, TERMINAL_CLEANUP_TIMEOUT_SECONDS)


def content_hash(req: Request) -> str:
    """请求体哈希（去重时用来发现"同 key 不同内容"）。对应 Go ``contentHash``。

    Go 对 ``json.Marshal(req)`` 求 SHA-256；本仓用
    :meth:`atri_x.atriena.Request.to_json` 得到同一键序与同一 ``omitempty`` 口径，
    因此本实现内部稳定（同请求同摘要）。注意 Go 的 ``json.Marshal`` 会把
    ``<`` ``>`` ``&`` 转成 ``\\u003c`` 等转义、Python 的 ``json.dumps`` 不会，
    含这些字符时两侧摘要不同——本仓不要求与 Go 逐字节一致。
    """
    data = json.dumps(req.to_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hash_bytes(data.encode("utf-8"))


def hash_text(value: str) -> str:
    """文本哈希；空串返回空串（不产生摘要）。对应 Go ``hashText``。"""
    if not value:
        return ""
    return hash_bytes(value.encode("utf-8"))


def hash_bytes(data: bytes) -> str:
    """SHA-256 十六进制小写。对应 Go ``hashBytes``。"""
    return hashlib.sha256(data).hexdigest()


def trim_strings(values: list[str] | None) -> list[str]:
    """去空白并丢掉空项（**总是返回列表**，不返回 ``None``）。对应 Go ``trimStrings``。"""
    out: list[str] = []
    for value in values or []:
        text = (value or "").strip()
        if text:
            out.append(text)
    return out


def set_from_strings(values: list[str] | None) -> set[str]:
    """转成集合（去空白、丢空项）。对应 Go ``setFromStrings``。"""
    return set(trim_strings(values))


def background_tool_names(values: list[str] | None) -> list[str]:
    """去重后的后台工具名列表（保持首次出现顺序）。对应 Go ``backgroundToolNames``。

    注意与 :func:`trim_strings` 的差别：这里**去重但不排序**。
    """
    seen: set[str] = set()
    out: list[str] = []
    for value in trim_strings(values):
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


class StoreMixin:
    """事件落库收尾 + 审计/日志。对应 Go ``store.go`` 里的 ``Service`` 方法。

    混入方需提供 ``store`` / ``audit`` / ``logger`` 三个属性，见
    :class:`atri_x.atrinis.service.Service`。
    """

    def handle_duplicate(self, event: Event, existing: AtrinisEvent | None) -> None:
        """重复事件：内容哈希不一致时告警，否则记 info。对应 Go ``handleDuplicate``。"""
        attrs = self.event_attrs(event)
        if existing is not None and existing.content_hash != event.content_hash:
            self.log_warn(
                "atrinis duplicate event hash mismatch",
                *attrs,
                "existing_event_id",
                existing.id,
            )
        else:
            self.log_info("atrinis duplicate event", *attrs)
        self.audit_event("atrinis.duplicate", *attrs)

    def complete_event(
        self,
        ctx: Any,
        id: str,
        resolved_targets: str,
        status: str,
        result: str,
        event_err: str,
    ) -> None:
        """写终态（不带 session）。对应 Go ``completeEvent``。"""
        self.complete_event_with_session(ctx, id, resolved_targets, status, "", result, event_err)

    def complete_event_with_session(
        self,
        ctx: Any,
        id: str,
        resolved_targets: str,
        status: str,
        session_id: str,
        result: str,
        event_err: str,
    ) -> None:
        """写事件状态（可带 session）。对应 Go ``completeEventWithSession``。

        ``failed`` / ``completed`` 是终态：即使 ``ctx`` 已取消，也要落库并释放引用。
        """
        from atri_x.atrinis.types import StatusCompleted, StatusFailed

        if status in (StatusFailed, StatusCompleted):
            ctx = cleanup_token(ctx)
        self.store.atrinis_events().update(
            UpdateAtrinisEventRequest(
                id=id,
                resolved_targets=resolved_targets,
                status=status,
                session_id=session_id,
                result=result,
                error=event_err,
            )
        )

    def event_attrs(self, event: Event, *attrs: Any) -> tuple[Any, ...]:
        """审计/日志的公共属性。对应 Go ``eventAttrs``（返回扁平键值对）。"""
        return (
            *attrs,
            "origin",
            event.origin.label() if event.origin is not None else "",
            "atriwisp_name",
            event.request.atriwisp.name,
            "source",
            event.request.source,
            "source_id",
            event.request.id,
            "event_key",
            event.event_key,
            "mode",
            event.request.mode,
            "tags",
            event.tags_json,
        )

    def audit_event(self, event: str, *attrs: Any) -> None:
        """写一条审计记录（未配置审计回调时静默）。对应 Go ``auditEvent``。"""
        audit = getattr(self, "audit", None)
        if audit is None:
            return
        try:
            audit(event, *attrs)
        except AtriXError:  # 审计失败不能影响主流程
            pass

    def log_info(self, msg: str, *attrs: Any) -> None:
        """info 日志。对应 Go ``logInfo``（未配置 logger 时静默）。"""
        logger = getattr(self, "logger", None)
        if logger is None:
            return
        info = getattr(logger, "info", None)
        if callable(info):
            info(msg, *attrs)

    def log_warn(self, msg: str, *attrs: Any) -> None:
        """warn 日志。对应 Go ``logWarn``。

        同时兼容 ``warn``（本仓 ``atri_x.logging.Logger``）与 ``warning``（标准库）。
        """
        logger = getattr(self, "logger", None)
        if logger is None:
            return
        warn = getattr(logger, "warn", None) or getattr(logger, "warning", None)
        if callable(warn):
            warn(msg, *attrs)
