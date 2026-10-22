"""Agent 提示用的工具/skill 名单与 schema 提供者。

对应 Go：``internal/tool/provider.go``。

``session.Scope`` 与 ``storage.Session`` 属于并行任务的模块，这里只按字段/方法
鸭子类型消费（``scope.platform`` / ``scope.actor_id``），不 import 它们。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.llm.llm import ToolSchema
from atri_x.storage.models import (
    SessionModeChat,
    SessionModeWork,
    session_mode_supports_tools,
)
from atri_x.tool.capability import ctx_raise_if_cancelled
from atri_x.tool.tool import (
    Actor,
    ActorFromContext,
    DefaultPolicy,
    Info,
    Policy,
    PolicyFromContext,
    Registry,
    RoleUser,
    SourceSkillAgent,
    SourceSkillGo,
    can_access_tool,
)

# 对应 Go 的 ``storage.SessionModeWork`` / ``SessionModeChat``（值来自 atri_x.storage）。
SESSION_MODE_WORK = SessionModeWork
SESSION_MODE_CHAT = SessionModeChat


@dataclass
class PromptNames:
    """提示里列出的工具名与 skill 名。对应 Go ``PromptNames``。"""

    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


def prompt_names_from_infos(infos: list[Info] | None) -> PromptNames:
    """对应 Go ``PromptNamesFromInfos``：隐藏工具与 ``discover_tool`` 不入名单。"""
    names = PromptNames()
    for info in infos or []:
        if info.name == "discover_tool" or info.hidden:
            continue
        if info.source in (SourceSkillAgent, SourceSkillGo):
            names.skills.append(info.name)
        else:
            names.tools.append(info.name)
    return names


@dataclass
class SchemaProvider:
    """把注册表投影成模型可见的 schema 与名单。对应 Go ``SchemaProvider``。"""

    registry: Registry | None = None
    policy: Policy | None = None

    def schemas(
        self,
        ctx: Any,
        mode: str,
        session: Any = None,
        scope: Any = None,
    ) -> list[ToolSchema]:
        """对应 Go ``SchemaProvider.Schemas``：work / chat 模式都注入工具。"""
        del session, scope
        ctx_raise_if_cancelled(ctx)
        if not session_mode_supports_tools(mode) or self.registry is None:
            return []
        return self.registry.schemas()

    def tool_names(
        self,
        ctx: Any,
        mode: str,
        session: Any = None,
        scope: Any = None,
    ) -> PromptNames:
        """对应 Go ``SchemaProvider.ToolNames``。"""
        del session
        ctx_raise_if_cancelled(ctx)
        if not session_mode_supports_tools(mode) or self.registry is None:
            return PromptNames()
        actor = actor_from_scope(ctx, scope)
        return prompt_names_from_infos(self.allowed_infos(actor))

    def allowed_infos(self, actor: Actor) -> list[Info]:
        """对应 Go ``SchemaProvider.allowedInfos``：``discover_tool`` 永远保留。"""
        policy = self.policy
        if policy is None:
            policy = DefaultPolicy()
        infos = self.registry.list() if self.registry is not None else []
        out: list[Info] = []
        for info in infos:
            if can_access_tool(actor, policy, info) or info.name == "discover_tool":
                out.append(info)
        return out


def actor_from_scope(ctx: Any, scope: Any) -> Actor:
    """对应 Go ``actorFromScope``：优先取 ctx 里的 actor，其次按 scope 合成。"""
    actor, ok = ActorFromContext(ctx)
    if ok and actor is not None:
        return actor
    policy, has_policy = PolicyFromContext(ctx)
    if has_policy and policy is not None:
        return policy.actor(
            _scope_attr(scope, "actor_id"),
            _scope_attr(scope, "platform"),
            _scope_attr(scope, "actor_id"),
            "",
        )
    actor_id = _scope_attr(scope, "actor_id")
    platform = _scope_attr(scope, "platform")
    return Actor(
        id=actor_id,
        platform=platform,
        platform_user_id=actor_id,
        role=RoleUser,
    )


def _scope_attr(scope: Any, name: str) -> str:
    """按字段名读 ``session.Scope``；scope 为 None 时返回空串。"""
    if scope is None:
        return ""
    value = getattr(scope, name, "")
    if isinstance(value, str):
        return value
    if isinstance(scope, dict):
        return str(scope.get(name) or "")
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
