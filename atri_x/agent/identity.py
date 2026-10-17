"""身份、作用域与对话元数据推导。

对应 Go：``internal/agent/identity.go``。

``security.Actor`` / ``session.Scope`` 的唯一出处分别是
:mod:`atri_x.security` 与 :mod:`atri_x.session`（PORTING.md §3），本模块只消费。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.platform import (
    ConversationChannel,
    ConversationGroup,
    ConversationPrivate,
    message_context_from,
)
from atri_x.security import (
    GroupRoleUnknown,
    RoleSuperadmin,
    actor_from_context,
    default_policy,
    parse_group_role,
)
from atri_x.session import Scope

__all__ = [
    "ConversationMeta",
    "actor",
    "base_scope",
    "conversation_id",
    "conversation_kind_from_scope",
    "conversation_meta",
    "first_non_empty",
    "scope",
    "shared_group_thread",
]


@dataclass
class ConversationMeta:
    """会话元数据。对应 Go ``ConversationMeta``。"""

    platform: str = ""
    kind: str = ""
    id: str = ""
    display_name: str = ""
    user_id: str = ""


def scope(agent: Any, ctx: Any) -> Scope:
    """当前作用域，含"共享会话"标志。对应 Go ``Agent.scope``。"""
    result = base_scope(agent, ctx)
    if shared_group_thread(agent, result):
        result.shared = True
    return result


def shared_group_thread(agent: Any, scope_value: Scope) -> bool:
    """该群作用域是否使用同一个共享 Session。

    对应 Go ``sharedGroupThread``：直接读已加载的群策略，避免再次走
    ``agent.scope`` 递归。
    """
    if agent is None:
        return False
    scope_id = (scope_value.platform_scope_id or "").strip()
    if not scope_id.startswith(("group:", "supergroup:")):
        return False
    policy = getattr(agent, "raw_group_policy", None)
    if not callable(policy):
        return False
    thread_mode = (getattr(policy(scope_value), "thread_mode", "") or "").strip()
    from atri_x.config import THREAD_MODE_GROUP

    return thread_mode.lower() == THREAD_MODE_GROUP.lower()


def base_scope(agent: Any, ctx: Any) -> Scope:
    """不含共享标志的基础作用域。对应 Go ``baseScope``。"""
    current = actor(agent, ctx)
    platform = getattr(agent, "platform", None)
    platform_name = platform.name() if platform is not None else ""
    scope_id = getattr(agent, "scope_id", "local")
    msg, ok = message_context_from(ctx)
    if ok:
        if msg.platform:
            platform_name = msg.platform
        if msg.scope_id:
            scope_id = msg.scope_id
    return Scope(
        actor_id=current.id,
        platform=platform_name,
        platform_scope_id=scope_id,
        is_cli=platform_name == "cli" and current.role == RoleSuperadmin,
    )


def conversation_meta(agent: Any, ctx: Any, scope_value: Scope) -> ConversationMeta:
    """推导对话元数据。对应 Go ``Agent.conversationMeta``。"""
    meta = ConversationMeta(platform=(scope_value.platform or "").strip())
    msg, ok = message_context_from(ctx)
    if not ok:
        return meta

    if msg.conversation_kind == ConversationGroup:
        meta.kind = "group"
    elif msg.conversation_kind == ConversationPrivate:
        meta.kind = "private"
    elif msg.conversation_kind == ConversationChannel:
        meta.kind = "channel"
    if not meta.kind:
        meta.kind = conversation_kind_from_scope(scope_value.platform_scope_id)

    current = actor(agent, ctx)
    meta.user_id = (current.platform_user_id or "").strip()
    if meta.kind == "group":
        meta.id = conversation_id(scope_value.platform_scope_id, "group:", "supergroup:")
        meta.display_name = first_non_empty(current.group_card, current.nickname)
    elif meta.kind == "private":
        meta.id = (current.platform_user_id or "").strip()
        meta.display_name = current.nickname
    elif meta.kind == "channel":
        meta.id = conversation_id(scope_value.platform_scope_id, "channel:")
        meta.display_name = current.nickname
    return meta


def conversation_kind_from_scope(scope_id: str) -> str:
    """从 scope id 前缀推导对话种类。对应 Go ``conversationKindFromScope``。"""
    if scope_id.startswith(("group:", "supergroup:")):
        return "group"
    if scope_id.startswith(("private:", "c2c:")):
        return "private"
    if scope_id.startswith("channel:"):
        return "channel"
    return ""


def conversation_id(scope_id: str, *prefixes: str) -> str:
    """剥掉 scope id 的平台前缀。对应 Go ``conversationID``。"""
    for prefix in prefixes:
        if scope_id.startswith(prefix):
            return scope_id[len(prefix) :].strip()
    return ""


def first_non_empty(*values: str) -> str:
    """第一个去空白后非空的值。对应 Go ``firstNonEmpty``。"""
    for value in values:
        trimmed = (value or "").strip()
        if trimmed:
            return trimmed
    return ""


def actor(agent: Any, ctx: Any) -> Any:
    """当前发言人。对应 Go ``Agent.actor``。"""
    current, ok = actor_from_context(ctx)
    if ok and (current.id or current.role):
        return current
    platform = getattr(agent, "platform", None)
    platform_name = platform.name() if platform is not None else ""
    platform_user_id = getattr(agent, "actor_id", "cli:local")
    display_name = ""
    actor_id_value = ""
    group_role = GroupRoleUnknown
    nickname = ""
    group_card = ""
    msg, has_msg = message_context_from(ctx)
    if has_msg:
        if msg.platform:
            platform_name = msg.platform
        if msg.platform_user_id:
            platform_user_id = msg.platform_user_id
        actor_id_value = msg.actor_id
        display_name = msg.display_name
        nickname = msg.nickname
        group_card = msg.group_card
        group_role = parse_group_role(msg.group_role)
    prefix = platform_name + ":"
    if platform_user_id.startswith(prefix):
        platform_user_id = platform_user_id[len(prefix) :]
    policy = getattr(agent, "security_policy", None) or default_policy()
    result = policy.actor(actor_id_value, platform_name, platform_user_id, display_name)
    result.nickname = nickname
    result.group_card = group_card
    result.group_role = group_role
    return result
