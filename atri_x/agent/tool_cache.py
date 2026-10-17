"""会话级工具缓存（发现结果的持久化）。对应 Go：``internal/agent/tool_cache.go``。

职责：``rememberDiscoveredTools`` / ``rememberCachedTools`` /
``discoveredToolSchemas`` / ``restoreDiscoveredToolsFromMetadata`` /
``mutateSessionMetadata`` / ``persistCachedTools`` / ``cachedToolNameSet`` /
``persistToolTags`` / ``persistShownRuleCardFormats``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``sync.Mutex``（``autoConfirmMu``）→ 惰性创建在 :class:`ToolCacheState`
  上的 :class:`asyncio.Lock`，避免在同步构造期绑定事件循环。
* ``sessionMetadata`` 的强类型视图复用 agent-core 的
  :mod:`atri_x.agent.session_metadata`（同一份 Go 结构，键名逐字对齐）。
* ``a.store.Sessions().Mutate`` → ``store.sessions().mutate(id, update)``：
  ``atri_x.storage`` 的仓储是**同步** SQLite 直连（无 ctx），回调抛异常即回滚
  （与 Go 的 ``func(*Session) error`` 同义）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from atri_x.agent.session_metadata import (
    SessionMetadata,
    decode_session_metadata,
    encode_session_metadata_into,
    sorted_unique,
)
from atri_x.agent.tools import service_host
from atri_x.storage import Session
from atri_x.storage.times import now as storage_now
from atri_x.tool.tool import DiscoveryResult
from atri_x.toolrun import (
    CachedTool,
    merge_cached_tools,
    native_cached_tools_from_discovery,
)

__all__ = [
    "ToolCacheMixin",
    "ToolCacheState",
    "cached_tool_name_set",
    "cached_tool_names",
    "decode_session_metadata",
    "discovered_tool_schemas",
    "encode_session_metadata_into",
    "mutate_session_metadata",
    "persist_cached_tools",
    "persist_shown_rule_card_formats",
    "persist_tool_tags",
    "remember_cached_tools",
    "remember_discovered_tools",
    "restore_discovered_tools_from_metadata",
    "tool_cache_state",
]


@dataclass
class ToolCacheState:
    """:class:`ToolCacheMixin` 需要的运行时状态。

    ``discovered_tools`` 与 ``lock`` 直接指向宿主上的 ``agent.discovered_tools`` /
    ``agent.auto_confirm_mu``（与 Go 的字段名一致，agent-core 已经建好这两个属性），
    因此不存在"第二份互不相认的发现缓存"。
    """

    discovered_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock)

    def mutex(self) -> Any:
        """返回宿主的锁（Go ``autoConfirmMu``）。"""
        return self.lock


_STATE_ATTR = "_tool_cache_state"


def tool_cache_state(agent: Any) -> ToolCacheState:
    """取（必要时惰性创建）宿主上的工具缓存状态视图。"""
    host = service_host(agent)
    discovered = getattr(host, "discovered_tools", None)
    if not isinstance(discovered, dict):
        discovered = {}
        _safe_set(host, "discovered_tools", discovered)
    lock = getattr(host, "auto_confirm_mu", None)
    if lock is None:
        lock = threading.Lock()
        _safe_set(host, "auto_confirm_mu", lock)
    return ToolCacheState(discovered_tools=discovered, lock=lock)


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


# ---------------------------------------------------------------------------
# 记忆发现结果
# ---------------------------------------------------------------------------
async def remember_discovered_tools(
    agent: Any, ctx: Any, session: Any, result: DiscoveryResult | None
) -> None:
    """记录一次 ``discover_tool`` 的结果。对应 Go ``rememberDiscoveredTools``。"""
    if result is None or session is None or not getattr(session, "id", ""):
        return
    await remember_cached_tools(agent, ctx, session, native_cached_tools_from_discovery(result))


async def remember_cached_tools(
    agent: Any, ctx: Any, session: Any, cached: list[CachedTool]
) -> None:
    """把发现到的工具写进内存缓存并落库。对应 Go ``rememberCachedTools``。"""
    if session is None or not getattr(session, "id", ""):
        return
    if not cached:
        return
    state = tool_cache_state(agent)
    with state.mutex():
        bucket = state.discovered_tools.setdefault(session.id, {})
        for item in cached:
            if not item.name:
                continue
            bucket[item.name] = item.schema
    await persist_cached_tools(agent, ctx, session, cached)


def discovered_tool_schemas(agent: Any, session: Any) -> list[Any]:
    """当前 Session 已发现工具的 schema（按名字排序）。对应 Go ``discoveredToolSchemas``。"""
    if session is None:
        return []
    restore_discovered_tools_from_metadata(agent, session)
    tools = tool_cache_state(agent).discovered_tools.get(getattr(session, "id", "")) or {}
    if not tools:
        return []
    return [tools[name] for name in sorted(tools.keys())]


def restore_discovered_tools_from_metadata(agent: Any, session: Any) -> None:
    """从会话 metadata 恢复已发现工具。对应 Go ``restoreDiscoveredToolsFromMetadata``。"""
    if session is None:
        return
    metadata = decode_session_metadata(getattr(session, "metadata", "") or "")
    if not metadata.tool_cache and not metadata.discovered_tools:
        return
    state = tool_cache_state(agent)
    bucket = state.discovered_tools.setdefault(getattr(session, "id", ""), {})
    registry = _registry(agent)
    for name in metadata.discovered_tools:
        if not name or name in bucket or registry is None:
            continue
        target, ok = registry.get(name)
        if ok and target is not None:
            bucket[name] = target.schema()
    for item in metadata.tool_cache:
        if not item.name or item.source == "atriwisp":
            continue
        if item.name in bucket:
            continue
        bucket[item.name] = item.schema


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
async def mutate_session_metadata(
    agent: Any, ctx: Any, session: Any, mutate: Callable[[SessionMetadata], bool]
) -> None:
    """在一个事务里改会话 metadata。对应 Go ``mutateSessionMetadata``。

    从过期快照做读-改-写会静默丢掉并发写者（活动时间、工作区、命名、另一次发现）
    刚写进去的字段，因此这里必须走 ``Sessions().Mutate``。``mutate`` 返回 ``False``
    表示"无需写回"；回调抛异常即回滚（对应 Go 返回 error）。
    """
    if session is None or not getattr(session, "id", "") or mutate is None:
        return
    store = getattr(agent, "store", None)
    if store is None:
        return

    def _apply(current: Session) -> None:
        metadata = decode_session_metadata(current.metadata or "")
        if not mutate(metadata):
            return
        encoded = encode_session_metadata_into(current.metadata or "", metadata)
        if encoded == (current.metadata or ""):
            return
        current.metadata = encoded
        current.updated_at = storage_now()

    updated = store.sessions().mutate(session.id, _apply)
    if hasattr(updated, "__await__"):
        updated = await updated
    if updated is not None:
        session.metadata = updated.metadata


async def persist_cached_tools(
    agent: Any, ctx: Any, session: Any, cached: list[CachedTool]
) -> None:
    """把工具缓存合并进会话 metadata。对应 Go ``persistCachedTools``。"""

    def _mutate(metadata: SessionMetadata) -> bool:
        metadata.tool_cache = merge_cached_tools(metadata.tool_cache, cached)
        metadata.discovered_tools = sorted_unique(
            list(metadata.discovered_tools) + cached_tool_names(cached)
        )
        return True

    try:
        await mutate_session_metadata(agent, ctx, session, _mutate)
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：只记警告，不影响本轮
        _warn(agent, "persist cached tools failed", session, exc)


def cached_tool_names(items: list[CachedTool]) -> list[str]:
    """对应 Go ``cachedToolNames``。"""
    return [item.name for item in items if item.name]


async def cached_tool_name_set(agent: Any, ctx: Any, session: Any) -> dict[str, bool]:
    """一次调用可用的全部名字（metadata + 内存缓存）。对应 Go ``cachedToolNameSet``。"""
    out: dict[str, bool] = {}
    if session is None:
        return out
    metadata_raw = getattr(session, "metadata", "") or ""
    session_id = getattr(session, "id", "") or ""
    store = getattr(agent, "store", None)
    if session_id and store is not None:
        try:
            latest = store.sessions().get(session_id)
            if hasattr(latest, "__await__"):
                latest = await latest
            if latest is not None:
                metadata_raw = latest.metadata or metadata_raw
        except BaseException:  # noqa: BLE001 - 与 Go 相同：读不出来就用调用方手上的副本
            pass
    metadata = decode_session_metadata(metadata_raw)
    for name in metadata.discovered_tools:
        if name:
            out[name] = True
    for item in metadata.tool_cache:
        if item.name:
            out[item.name] = True
    for name in tool_cache_state(agent).discovered_tools.get(session_id) or {}:
        if name:
            out[name] = True
    return out


async def persist_tool_tags(agent: Any, ctx: Any, session: Any, tags: list[str]) -> None:
    """把工具标签并进会话 metadata。对应 Go ``persistToolTags``。"""
    if session is None or not getattr(session, "id", "") or not tags:
        return

    def _mutate(metadata: SessionMetadata) -> bool:
        metadata.tool_tags = sorted_unique(list(metadata.tool_tags) + list(tags))
        return True

    try:
        await mutate_session_metadata(agent, ctx, session, _mutate)
    except BaseException as exc:  # noqa: BLE001
        _warn(agent, "persist tool tags failed", session, exc)


async def persist_shown_rule_card_formats(
    agent: Any, ctx: Any, session: Any, formats: list[str]
) -> None:
    """记录已经展示过的规则卡格式。对应 Go ``persistShownRuleCardFormats``。"""
    if session is None or not getattr(session, "id", "") or not formats:
        return

    def _mutate(metadata: SessionMetadata) -> bool:
        metadata.shown_rule_card_formats = sorted_unique(
            list(metadata.shown_rule_card_formats) + list(formats)
        )
        return True

    try:
        await mutate_session_metadata(agent, ctx, session, _mutate)
    except BaseException as exc:  # noqa: BLE001
        _warn(agent, "persist rule card formats failed", session, exc)


class ToolCacheMixin:
    """Go ``*Agent`` 的工具缓存方法组。"""

    async def remember_discovered_tools(
        self, ctx: Any, session: Any, result: DiscoveryResult | None
    ) -> None:
        await remember_discovered_tools(self, ctx, session, result)

    async def remember_cached_tools(
        self, ctx: Any, session: Any, cached: list[CachedTool]
    ) -> None:
        await remember_cached_tools(self, ctx, session, cached)

    def discovered_tool_schemas(self, session: Any) -> list[Any]:
        return discovered_tool_schemas(self, session)

    def restore_discovered_tools_from_metadata(self, session: Any) -> None:
        restore_discovered_tools_from_metadata(self, session)

    async def persist_cached_tools(
        self, ctx: Any, session: Any, cached: list[CachedTool]
    ) -> None:
        await persist_cached_tools(self, ctx, session, cached)

    async def cached_tool_name_set(self, ctx: Any, session: Any) -> dict[str, bool]:
        return await cached_tool_name_set(self, ctx, session)

    async def persist_tool_tags(self, ctx: Any, session: Any, tags: list[str]) -> None:
        await persist_tool_tags(self, ctx, session, tags)

    async def persist_shown_rule_card_formats(
        self, ctx: Any, session: Any, formats: list[str]
    ) -> None:
        await persist_shown_rule_card_formats(self, ctx, session, formats)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _registry(agent: Any) -> Any:
    from atri_x.agent.tools import tool_runtime_state

    return tool_runtime_state(agent).registry


def _warn(agent: Any, message: str, session: Any, exc: BaseException) -> None:
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    warn = getattr(logger, "warn", None)
    if warn is None:
        return
    warn(message, session_id=getattr(session, "id", ""), error=str(exc))
