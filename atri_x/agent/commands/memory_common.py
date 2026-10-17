"""长期记忆命令的共享助手。

对应 Go：``internal/agent/commands/memory_common.go``（152 行）。

``commandAudit`` 在 Go 里同时存在于 ``session_lifecycle.go`` 与
``memory_common.go``（同一包内的重复定义）；本仓只保留
:func:`atri_x.agent.commands.session_lifecycle.audit_command` 一份，这里再导出，
避免出现第二份实现。
"""

from __future__ import annotations

from typing import Any

from atri_x.angelmemory import Memory, SourceFilter
from atri_x.errors import AmbiguousIDError, NotFoundError
from atri_x.security import (
    GroupRoleAdmin,
    GroupRoleOwner,
    RoleSuperadmin,
    actor_from_context,
)
from atri_x.agent.commands.session_lifecycle import audit_command  # noqa: F401

__all__ = [
    "audit_command",
    "is_group_scope",
    "memory_can_delete",
    "memory_can_manage_all",
    "memory_entry_detail",
    "memory_entry_line",
    "memory_parse_limit",
    "memory_source_text",
    "memory_visible_filter",
    "resolve_scoped_memory",
    "short_memory_id",
]


def resolve_scoped_memory(
    ctx: Any,
    service: Any,
    platform: str,
    scope_id: str,
    prefix: str,
    filter: SourceFilter | None,
) -> Memory:
    """按唯一 id 前缀在当前 scope 内解析并取出记忆。对应 Go ``resolveScopedMemory``。"""
    if service is None or not service.ready():
        raise ValueError("angel memory 未启用或未配置")
    prefix = (prefix or "").strip()
    if prefix == "":
        raise ValueError("请提供记忆 id")
    try:
        id_ = service.resolve_id(platform, scope_id, prefix, filter)
    except NotFoundError:
        raise ValueError(f"没有找到 id 前缀为 {prefix!r} 的记忆") from None
    except AmbiguousIDError:
        raise ValueError(f"id 前缀 {prefix!r} 不唯一，请提供更多字符") from None
    return service.get(platform, scope_id, id_)


def memory_visible_filter(ctx: Any, scope: Any) -> SourceFilter:
    """普通群成员只能看到"来源为自己"的记忆。对应 Go ``memoryVisibleFilter``。

    私聊 scope 与群主/管理员/超管拿到空过滤器（整个 scope）。
    """
    if not is_group_scope(scope):
        return SourceFilter()
    actor, _ = actor_from_context(ctx)
    if (
        actor.role == RoleSuperadmin
        or actor.group_role in (GroupRoleOwner, GroupRoleAdmin)
    ):
        return SourceFilter()
    actor_id = (actor.id or "").strip()
    if actor_id == "":
        return SourceFilter(actor_id="\x00")
    return SourceFilter(actor_id=actor_id)


def memory_can_manage_all(ctx: Any, scope: Any) -> bool:
    """当前 actor 是否能管理整个 scope 的记忆。对应 Go ``memoryCanManageAll``。"""
    if not is_group_scope(scope):
        return True
    actor, _ = actor_from_context(ctx)
    return actor.role == RoleSuperadmin or actor.group_role in (
        GroupRoleOwner,
        GroupRoleAdmin,
    )


def memory_can_delete(ctx: Any, scope: Any, memory: Memory | None) -> bool:
    """当前 actor 是否能删除这条记忆。对应 Go ``memoryCanDelete``。"""
    if memory is None:
        return False
    if memory_can_manage_all(ctx, scope):
        return True
    actor, _ = actor_from_context(ctx)
    actor_id = (actor.id or "").strip()
    return actor_id != "" and actor_id == (memory.source_actor_id or "").strip()


def is_group_scope(scope: Any) -> bool:
    """scope 是不是群作用域。对应 Go ``isGroupScope``。"""
    scope_id = (getattr(scope, "platform_scope_id", "") or "").strip()
    return scope_id.startswith("group:") or scope_id.startswith("supergroup:")


def short_memory_id(id_: str) -> str:
    """把记忆 id 截到 12 个码点。对应 Go ``shortMemoryID``。"""
    id_ = (id_ or "").strip()
    if len(id_) <= 12:
        return id_
    return id_[:12]


def memory_entry_line(index: int, memory: Memory) -> str:
    """列表里的一行记忆。对应 Go ``memoryEntryLine``。"""
    content = " ".join((memory.content or "").split())
    if len(content) > 80:
        content = content[:80] + "…"
    return (
        f"{index}. [{memory.strength}] id={short_memory_id(memory.id)} "
        f"{content}{memory_source_text(memory)}"
    )


def memory_source_text(memory: Memory) -> str:
    """记忆来源的紧凑描述。对应 Go ``memorySourceText``。"""
    parts: list[str] = []
    kind = (memory.source_kind or "").strip()
    if kind != "":
        parts.append("kind=" + kind)
    actor_id = (memory.source_actor_id or "").strip()
    if actor_id != "":
        parts.append("actor=" + actor_id)
    message_id = (memory.source_message_id or "").strip()
    if message_id != "":
        parts.append("message=" + message_id)
    session_id = (memory.source_session_id or "").strip()
    if session_id != "":
        parts.append("session=" + session_id)
    if not parts:
        label = (memory.source or "").strip()
        if label != "":
            parts.append("source=" + label)
    if not parts:
        return ""
    return " (" + " ".join(parts) + ")"


def memory_entry_detail(memory: Memory) -> str:
    """``/memory show`` 的详情块。对应 Go ``memoryEntryDetail``。"""
    tags = (memory.tags or "").strip()
    lines = [
        f"id：{memory.id}",
        f"强度：{memory.strength}",
        f"标签：{tags if tags != '' else '(无)'}",
        f"内容：{memory.content}",
    ]
    source = memory_source_text(memory).strip()
    if source != "":
        lines.append("来源：" + source.strip(" ()"))
    return "\n".join(lines)


def memory_parse_limit(raw: str, fallback: int, maximum: int) -> int:
    """解析 ``[n]`` 形式的数量上限。对应 Go ``memoryParseLimit``。

    Go 用 ``fmt.Sscanf(raw, "%d", &value)``：前导数字即命中（``"3x"`` → 3）。
    这里复刻同样的宽松前缀解析。
    """
    raw = (raw or "").strip()
    if raw == "":
        return fallback
    digits = ""
    start = 0
    if raw[0] in "+-":
        start = 1
    index = start
    while index < len(raw) and raw[index].isdigit():
        index += 1
    if index == start:
        return fallback
    digits = raw[:index]
    try:
        value = int(digits)
    except ValueError:
        return fallback
    if value <= 0:
        return fallback
    if value > maximum:
        return maximum
    return value
