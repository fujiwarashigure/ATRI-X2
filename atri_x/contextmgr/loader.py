"""上下文加载与 Fork 回溯。

对应 Go：``internal/contextmgr/loader.go``。

两条语义要点（都写在 Go 注释里，容易漏）：

1. **摘要优先**：若本 Session 已有摘要，则直接返回「摘要 + 摘要之后的消息」，
   **不再**向父 Session 回溯。
2. **``ListBySessionAfter`` 找不到时降级**：Fork 出来的摘要可能覆盖了祖先范围，
   于是 ``after`` 在**本分支**里不存在 —— 这时退回「本 Session 的全部消息」，
   而不是报错（Go 用 ``errors.Is(err, storage.ErrNotFound)`` 判定）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import ErrNotFound, NotFoundError
from atri_x.storage.models import ContextSummary, Message

#: Fork 链的最大深度。对应 Go ``maxForkDepth``。
MAX_FORK_DEPTH = 32

#: 上下文加载错误。对应 Go 的 ``fmt.Errorf("store is required")`` /
#: ``fmt.Errorf("fork depth exceeds %d")``。
STORE_REQUIRED = "store is required"


@dataclass
class LoadedContext:
    """加载出来的上下文。对应 Go ``LoadedContext``。"""

    summary: ContextSummary | None = None
    messages: list[Message] = field(default_factory=list)


class Loader:
    """按 Session 加载上下文。对应 Go ``Loader``。

    ``store`` 按鸭子类型消费 `atri_x.storage` 的 ``Store`` 接口
    （``sessions()`` / ``messages()`` / ``context_summaries()``）。
    注意：Python 侧的 repository 方法**不接收 ctx**（存储层是同步的），
    所以 ``ctx`` 参数只为签名兼容而保留。
    """

    def __init__(self, store: Any = None) -> None:
        self.store = store

    # -- 公开入口 -----------------------------------------------------------
    async def load(self, ctx: Any, session_id: str) -> LoadedContext:
        """对应 Go ``Load``。"""
        if self.store is None:
            raise ValueError(STORE_REQUIRED)
        return await self._load(ctx, session_id, "", 0)

    async def load_raw_messages(self, ctx: Any, session_id: str) -> list[Message]:
        """只取消息（不要摘要）。对应 Go ``LoadRawMessages``。"""
        if self.store is None:
            raise ValueError(STORE_REQUIRED)
        return await self._load_raw_messages(ctx, session_id, "", 0)

    # -- 实现 ---------------------------------------------------------------
    async def _load(
        self, ctx: Any, session_id: str, up_to_message_id: str, depth: int
    ) -> LoadedContext:
        if depth > MAX_FORK_DEPTH:
            raise ValueError(f"fork depth exceeds {MAX_FORK_DEPTH}")
        session = self.store.sessions().get(session_id)

        loaded = await self._load_own_summary(ctx, session.id, up_to_message_id)
        if loaded is not None:
            return loaded

        out = LoadedContext()
        parent_id = getattr(session, "parent_session_id", "") or ""
        fork_from = getattr(session, "fork_from_message_id", "") or ""
        if parent_id and fork_from:
            parent = await self._load(ctx, parent_id, fork_from, depth + 1)
            out.summary = parent.summary
            out.messages.extend(parent.messages)

        out.messages.extend(
            await self._list_own_messages(ctx, session.id, up_to_message_id)
        )
        return out

    async def _load_own_summary(
        self, ctx: Any, session_id: str, up_to_message_id: str
    ) -> LoadedContext | None:
        summaries = self.store.context_summaries()
        try:
            if up_to_message_id == "":
                summary = summaries.latest_by_session(session_id)
            else:
                summary = summaries.latest_by_session_up_to(session_id, up_to_message_id)
        except NotFoundError:
            # 没有摘要 -> 走父链回溯（对应 Go 的 errors.Is(err, storage.ErrNotFound)）
            return None
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise
        if summary is None:
            return None

        messages = await self._list_own_messages_after(
            ctx, session_id, summary.to_message_id, up_to_message_id
        )
        return LoadedContext(summary=summary, messages=messages)

    async def _list_own_messages(
        self, ctx: Any, session_id: str, up_to_message_id: str
    ) -> list[Message]:
        messages = self.store.messages()
        if up_to_message_id == "":
            return list(messages.list_by_session(session_id))
        return list(messages.list_by_session_up_to(session_id, up_to_message_id))

    async def _list_own_messages_after(
        self,
        ctx: Any,
        session_id: str,
        after_message_id: str,
        up_to_message_id: str,
    ) -> list[Message]:
        messages = self.store.messages()
        try:
            if up_to_message_id == "":
                return list(messages.list_by_session_after(session_id, after_message_id))
            return list(
                messages.list_by_session_after_up_to(
                    session_id, after_message_id, up_to_message_id
                )
            )
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise
            # Fork 摘要可能覆盖祖先范围，导致本分支里没有 after 锚点 —— 退回全量。
            return await self._list_own_messages(ctx, session_id, up_to_message_id)

    async def _load_raw_messages(
        self, ctx: Any, session_id: str, up_to_message_id: str, depth: int
    ) -> list[Message]:
        if depth > MAX_FORK_DEPTH:
            raise ValueError(f"fork depth exceeds {MAX_FORK_DEPTH}")
        session = self.store.sessions().get(session_id)
        out: list[Message] = []
        parent_id = getattr(session, "parent_session_id", "") or ""
        fork_from = getattr(session, "fork_from_message_id", "") or ""
        if parent_id and fork_from:
            out.extend(
                await self._load_raw_messages(ctx, parent_id, fork_from, depth + 1)
            )
        out.extend(await self._list_own_messages(ctx, session.id, up_to_message_id))
        return out


def _is_not_found(exc: BaseException) -> bool:
    """识别"没找到"：哨兵 ``ErrNotFound`` 或自己声明了 not-found 语义的异常。

    存储层既可能抛 :data:`atri_x.errors.ErrNotFound`，也可能让 ``ValueError``/
    ``LookupError`` 逃出来（不同实现）；只要不是这两种就继续抛。
    """
    if exc is ErrNotFound:
        return True
    if isinstance(exc, ErrNotFound.__class__):
        return True
    return isinstance(exc, (LookupError,)) or getattr(exc, "not_found", False) is True


__all__ = ["MAX_FORK_DEPTH", "STORE_REQUIRED", "LoadedContext", "Loader"]
