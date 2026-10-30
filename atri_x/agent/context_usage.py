"""上下文用量：``/context`` 状态文本、最近用量持久化、协议归属记录、压缩触发判定。

对应 Go：``internal/agent/context_usage.go``。

三处语义要点：

* 用量优先取内存（进程内最近一次），内存没有才回落到会话 metadata 里的 ``last_usage``
  （重启后仍能显示），并顺手写回内存，避免每次都解 JSON。
* ``record_llm_origin`` 先与内存里的会话行比对，值没变就完全不做写事务，因此正常情况下
  每个会话只在换协议/换模型时写一次；记录的是适配器**实际**协议，而不是配置里声明的
  ``api_mode``。
* ``should_compact`` 在历史被关闭时直接为假：关掉历史的群不应该因为"窗口快满了"去压缩。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from atri_x.agent.model import host_of
from atri_x.config import ModelSelection
from atri_x.contextmgr import format_tokens
from atri_x.llm.llm import Usage
from atri_x.storage.models import Session

__all__ = [
    "ContextUsageMixin",
    "LLMOriginState",
    "render_context_status",
]

#: 未达阈值但已经接近时的余量。对应 Go ``status`` 里的 ``threshold-0.1``。
NEAR_THRESHOLD_MARGIN = 0.1


def render_context_status(
    usage: Usage | None,
    window: int,
    *,
    compact_enabled: bool,
    compact_trigger_ratio: float,
) -> str:
    """``/context`` 的状态文本。对应 Go ``contextRuntimeState.status`` 的正文。

    文本逐字对齐 Go（含两空格缩进与全角/半角标点）。
    """
    threshold = compact_trigger_ratio or 0.8
    lines = [
        "  " + format_tokens(usage),
        f"  context window: {window}",
    ]
    if usage is None or int(usage.total_tokens or 0) <= 0 or window <= 0:
        lines.append("  context usage: unknown")
        lines.append(f"  compact threshold: {threshold * 100:.0f}%")
        status = "unknown"
        if not compact_enabled:
            status = "disabled"
        lines.append(f"  compact status: {status}")
        return "\n".join(lines) + "\n"

    ratio = float(usage.total_tokens) / float(window)
    lines.append(f"  context usage: {ratio * 100:.1f}%")
    lines.append(f"  compact threshold: {threshold * 100:.0f}%")
    if not compact_enabled:
        status = "disabled"
    elif ratio >= threshold:
        status = "will compact before next request"
    elif ratio >= max(0.0, threshold - NEAR_THRESHOLD_MARGIN):
        status = "near threshold"
    else:
        status = "ok"
    lines.append(f"  compact status: {status}")
    return "\n".join(lines) + "\n"


@dataclass
class LLMOriginState:
    """会话 metadata 里记录的协议归属。对应 Go ``llmOriginState``。

    唯一出处在 agent-core 的 ``session_metadata.py``；本模块在函数内延迟导入复用，
    缺失时用字段等价的本地定义兜底。
    """

    protocol: str = ""
    provider: str = ""
    model: str = ""


class ContextUsageMixin:
    """``internal/agent/context_usage.go`` 里挂在 ``*Agent`` 上的方法。

    宿主成员：``self.context_runtime``（:class:`atri_x.agent.context_runtime.ContextRuntimeState`）、
    ``self.model_for_mode(mode)``、``self.llm_origin_for(provider, model)``、
    ``self.store``、``self.mutate_session_metadata(ctx, session, mutate)``（agent-tools 的
    ``tool_cache.py``）、``self.history_enabled(ctx)``（``group_policy.py``）、``self.logger``。
    """

    def context_status(self, ctx: Any, session: Session | None) -> str:
        """``/context`` 的状态文本。对应 Go ``ContextStatus``（同步返回）。"""
        from atri_x.agent.context_runtime import ensure_context_runtime

        usage = self.usage_for_session(session)
        selection = self.model_for_mode(getattr(session, "mode", "") or "")
        session_id = getattr(session, "id", "") or ""
        return ensure_context_runtime(self).status(ctx, session_id, usage, selection)

    def record_usage(self, session_id: str, usage: Usage | None) -> None:
        """记住并持久化最近用量。对应 Go ``recordUsage``。"""
        if usage is None:
            return
        from atri_x.agent.context_runtime import ensure_context_runtime

        ensure_context_runtime(self).record_usage(session_id, usage)
        self.persist_usage(None, session_id, usage)
        _log_usage_recorded(self, session_id, usage)

    def usage_for_session(self, session: Session | None) -> Usage | None:
        """会话最近一次用量。对应 Go ``usageForSession``。"""
        if session is None:
            return None
        from atri_x.agent.context_runtime import ensure_context_runtime

        runtime = ensure_context_runtime(self)
        usage = runtime.usage(getattr(session, "id", "") or "")
        if usage is not None:
            return usage
        metadata = _decode_metadata(getattr(session, "metadata", "") or "")
        last_usage = getattr(metadata, "last_usage", None)
        if last_usage is None:
            return None
        runtime.record_usage(session.id, last_usage)
        return last_usage

    def persist_usage(self, ctx: Any, session_id: str, usage: Usage | None) -> None:
        """把用量写进会话 metadata。对应 Go ``persistUsage``。

        metadata 变更在 Python 侧是 ``async`` 的（agent-tools 的
        ``mutate_session_metadata`` 走 ``Sessions().Mutate``）；本方法是同步契约，
        因此在有事件循环时把这次写入排进循环（与 Go 的"顺手写一次"等价），
        没有循环时只记一条告警。
        """
        if usage is None or not session_id or getattr(self, "store", None) is None:
            return
        mutate = _metadata_mutator()
        if mutate is None:
            return

        def edit(metadata: Any) -> bool:
            metadata.last_usage = usage
            return True

        try:
            result = mutate(host_of(self), ctx, Session(id=session_id), edit)
        except Exception as exc:  # noqa: BLE001 - 持久化失败只告警
            _warn(self, "persist usage failed", session_id=session_id, error=str(exc))
            return
        _run_or_schedule(
            result, "persist usage", session_id=session_id, host=self
        )

    def record_llm_origin(
        self, ctx: Any, session: Session | None, selection: ModelSelection
    ) -> None:
        """记录这段对话由哪套协议产生。对应 Go ``recordLLMOrigin``。"""
        if (
            getattr(self, "store", None) is None
            or session is None
            or not getattr(session, "id", "")
            or not selection.provider
            or not selection.model
        ):
            return
        origin = self.llm_origin_for(selection.provider, selection.model)
        protocol = str(getattr(origin, "protocol", "") or "")
        provider = str(getattr(origin, "provider", "") or "")
        existing = _decode_metadata(getattr(session, "metadata", "") or "")
        if _same_origin(
            getattr(existing, "llm_origin", None), protocol, provider, selection.model
        ):
            return
        mutate = _metadata_mutator()
        if mutate is None:
            return

        def edit(metadata: Any) -> bool:
            if _same_origin(
                getattr(metadata, "llm_origin", None), protocol, provider, selection.model
            ):
                return False
            metadata.llm_origin = _new_origin_state(
                protocol, provider, selection.model
            )
            return True

        try:
            result = mutate(host_of(self), ctx, session, edit)
        except Exception as exc:  # noqa: BLE001 - 记录失败只告警
            _warn(
                self,
                "persist llm origin failed",
                session_id=session.id,
                error=str(exc),
            )
            return
        _run_or_schedule(
            result, "persist llm origin", session_id=session.id, host=self
        )

    def should_compact(
        self, ctx: Any, session: Session | None, selection: ModelSelection
    ) -> bool:
        """是否应该在下一轮前压缩。对应 Go ``shouldCompact``（同步返回布尔）。

        ``AgentSeams.should_compact`` 直接对返回值取 ``bool``，因此这里必须是同步的；
        窗口解析走 :meth:`ContextRuntimeState.window_for_sync`。
        """
        if not self.history_enabled(ctx):
            return False
        if session is None:
            return False
        from atri_x.agent.context_runtime import ensure_context_runtime

        return ensure_context_runtime(self).reached_compact_threshold(
            ctx, self.usage_for_session(session), selection
        )


def _decode_metadata(raw: str) -> Any:
    """解析会话 metadata（唯一出处是 agent-core 的 ``session_metadata.py``）。"""
    if not raw:
        return None
    import importlib

    try:
        module = importlib.import_module("atri_x.agent.session_metadata")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    decode = getattr(module, "decode_session_metadata", None)
    if not callable(decode):
        return None
    try:
        return decode(raw)
    except Exception:  # noqa: BLE001 - 脏数据按没有 metadata 处理
        return None


def _new_origin_state(protocol: str, provider: str, model: str) -> Any:
    """构造协议归属状态：优先用 agent-core 的类型，缺失时用本地等价值。"""
    import importlib

    try:
        module = importlib.import_module("atri_x.agent.session_metadata")
    except Exception:  # noqa: BLE001
        module = None
    state_cls = getattr(module, "LLMOriginState", None) if module else None
    if isinstance(state_cls, type):
        return state_cls(protocol=protocol, provider=provider, model=model)
    return LLMOriginState(protocol=protocol, provider=provider, model=model)


def _same_origin(
    existing: Any, protocol: str, provider: str, model: str
) -> bool:
    """``existing`` 是否已经等于目标归属（支持 dataclass 与 mapping 两种表示）。"""
    if existing is None:
        return False
    if isinstance(existing, dict):
        return (
            str(existing.get("protocol") or "") == protocol
            and str(existing.get("provider") or "") == provider
            and str(existing.get("model") or "") == model
        )
    return (
        str(getattr(existing, "protocol", "") or "") == protocol
        and str(getattr(existing, "provider", "") or "") == provider
        and str(getattr(existing, "model", "") or "") == model
    )


def _log_usage_recorded(host: Any, session_id: str, usage: Usage) -> None:
    """记一行「本轮到底记了什么用量」（含缓存是否上报）。

    排查「``/status`` 命中恒 0」时，这行给出落库前的现场值；info 级所以默认配置下
    就能看到（每轮一条，不随工具轮次放大）。
    """
    logger = getattr(host, "logger", None)
    if logger is None:
        return
    try:
        logger.log(
            "info",
            "usage recorded",
            "session_id", session_id,
            "prompt_tokens", usage.prompt_tokens,
            "completion_tokens", usage.completion_tokens,
            "total_tokens", usage.total_tokens,
            "cache_hit_tokens", usage.cache_hit_tokens,
            "cache_hit_reported", bool(getattr(usage, "cache_hit_reported", True)),
        )
    except Exception:  # noqa: BLE001 - 日志不能影响主流程
        pass


def _warn(host: Any, message: str, **attrs: Any) -> None:
    logger = getattr(host, "logger", None)
    if logger is None:
        return
    try:
        logger.warning(message, extra=attrs or None)
    except Exception:  # noqa: BLE001 - 日志不能影响主流程
        pass


def _metadata_mutator() -> Any:
    """取会话 metadata 的事务写入入口（agent-tools 的 ``mutate_session_metadata``）。"""
    import importlib

    try:
        module = importlib.import_module("atri_x.agent.tool_cache")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    fn = getattr(module, "mutate_session_metadata", None)
    return fn if callable(fn) else None


def _run_or_schedule(result: Any, what: str, *, session_id: str, host: Any) -> None:
    """同步契约下的异步写入：有事件循环就排队，否则告警。"""
    if result is None or not hasattr(result, "__await__"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _warn(host, f"{what} skipped: no running event loop", session_id=session_id)
        try:
            result.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return
    loop.create_task(result)
