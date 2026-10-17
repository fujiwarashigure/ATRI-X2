"""压缩种子（``context_compact``）的消费。

对应 Go：``internal/agent/context_seed.go``。

压缩出来的新会话带一个 ``pending`` 的 ``context_compact`` 摘要；它只应该被当成
**一次**种子注入（第一轮），之后必须把 ``pending`` 清掉，否则每次对话都会重新注入同一份
摘要。清除失败不属于致命错误：下一轮会再试一次，只是可能重复注入一轮。

metadata 的读写走 :mod:`atri_x.agent.context_compact` 的适配层（优先用 agent-core 的
``session_metadata``）。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.context_compact import (
    ContextCompactState,
    compact_state_from_metadata,
    decode_metadata,
)
from atri_x.agent.model import host_of

__all__ = [
    "ContextSeedMixin",
    "consume_context_compact_seed",
    "pending_context_compact",
]


def pending_context_compact(session: Any) -> ContextCompactState | None:
    """待消费的压缩种子。对应 Go ``pendingContextCompact``。"""
    if session is None:
        return None
    compact = compact_state_from_metadata(
        str(getattr(session, "metadata", "") or "")
    )
    if compact is None or not compact.pending or not compact.summary:
        return None
    return compact


async def consume_context_compact_seed(agent: Any, ctx: Any, session: Any) -> None:
    """把种子的 ``pending`` 清掉。对应 Go ``consumeContextCompactSeed``。"""
    host = host_of(agent)
    if getattr(host, "store", None) is None or session is None:
        return
    if not getattr(session, "id", ""):
        return
    mutate = _metadata_mutator()
    if mutate is None:
        return

    def edit(metadata: Any) -> bool:
        compact = getattr(metadata, "context_compact", None)
        pending = _pending_flag(compact)
        if pending is None or not pending:
            return False
        _set_pending(compact, False)
        return True

    try:
        await mutate(host, ctx, session, edit)
    except Exception as exc:  # noqa: BLE001 - 清种子失败只记日志，下一轮再试
        logger = getattr(host, "logger", None)
        if logger is not None:
            logger.warning(
                "consume compact context failed",
                extra={"session_id": getattr(session, "id", ""), "error": str(exc)},
            )


def _pending_flag(compact: Any) -> bool | None:
    if compact is None:
        return None
    if isinstance(compact, dict):
        return bool(compact.get("pending"))
    return bool(getattr(compact, "pending", False))


def _set_pending(compact: Any, value: bool) -> None:
    if isinstance(compact, dict):
        compact["pending"] = value
        return
    compact.pending = value


def _metadata_mutator() -> Any:
    """会话 metadata 的事务写入入口（agent-tools 的 ``mutate_session_metadata``）。"""
    import importlib

    try:
        module = importlib.import_module("atri_x.agent.tool_cache")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    fn = getattr(module, "mutate_session_metadata", None)
    return fn if callable(fn) else None


class ContextSeedMixin:
    """``internal/agent/context_seed.go`` 里挂在 ``*Agent`` 上的方法。"""

    async def consume_context_compact_seed(self, ctx: Any, session: Any) -> None:
        """对应 Go ``consumeContextCompactSeed``。"""
        await consume_context_compact_seed(self, ctx, session)

    def pending_context_compact(self, session: Any) -> ContextCompactState | None:
        """对应 Go ``pendingContextCompact``。"""
        return pending_context_compact(session)

    def session_metadata_view(self, session: Any) -> Any:
        """会话 metadata 的属性视图（供需要读 compact/tool_tags 的调用点复用）。"""
        return decode_metadata(str(getattr(session, "metadata", "") or ""))
