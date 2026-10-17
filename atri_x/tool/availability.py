"""工具"为什么没被采用"的机器可读原因，以及产出它的唯一判定入口。

对应 Go：``internal/tool/availability.go``。

此前预载与发现路径把四类完全不同的判定压成一个自由字符串
``reason="not_found_or_not_allowed"``：注册表里没有、当前上下文不可用（后台专用 /
需要视觉能力）、角色或策略拒绝、被隐藏。事后从审计日志无法区分"策略拒绝"与
"工具不存在"，也无法据此统计拒绝率。判定顺序与 ``can_access_tool`` /
``info_available_in_context`` 保持一致，保证同一个工具在任何调用点得到同一个原因。

原因字符串只表达判定事实，不表达日志等级；等级与操作结果由来源（agent）决定。
"""

from __future__ import annotations

from typing import Any

from atri_x.tool.sandbox import info_available_in_context
from atri_x.tool.tool import (
    Actor,
    DefaultPolicy,
    Info,
    Policy,
    Registry,
    RiskHigh,
    RoleSuperadmin,
    Tool,
    normalize_risk,
)

# 注册表里没有这个名字。
TOOL_REASON_NOT_FOUND = "tool_not_found"
# 工具在当前上下文不可用（ForegroundOnly 工具在后台上下文中，或
# VisionRequired 工具在没有视觉能力的模型上）。
TOOL_REASON_CONTEXT_UNAVAILABLE = "tool_context_unavailable"
# 工具是隐藏工具，不在当前路径的暴露范围内。
TOOL_REASON_HIDDEN = "tool_hidden"
# 工具仅超级管理员可用，而当前 actor 不是超管。
TOOL_REASON_SUPERADMIN_ONLY = "tool_requires_superadmin"
# 工具风险等级高于策略允许的上限。
TOOL_REASON_RISK_DENIED = "tool_risk_above_allowed_level"
# 工具解析完成但没有可用 schema，无法注入模型。
TOOL_REASON_NO_SCHEMA = "tool_no_schema"

# Go 同名的 PascalCase 常量别名。
ToolReasonNotFound = TOOL_REASON_NOT_FOUND
ToolReasonContextUnavailable = TOOL_REASON_CONTEXT_UNAVAILABLE
ToolReasonHidden = TOOL_REASON_HIDDEN
ToolReasonSuperadminOnly = TOOL_REASON_SUPERADMIN_ONLY
ToolReasonRiskDenied = TOOL_REASON_RISK_DENIED
ToolReasonNoSchema = TOOL_REASON_NO_SCHEMA


def tool_availability_reason(
    ctx: Any, actor: Actor, policy: Policy | None, candidate: Tool | None
) -> str:
    """报告工具在当前上下文与策略下是否可用；可用时返回空字符串。

    对应 Go ``ToolAvailabilityReason``。它把 ``can_access_tool`` 与
    ``info_available_in_context`` 的判定拆成互斥的机器可读原因，供审计日志和
    预载路径使用。
    """
    if candidate is None:
        return TOOL_REASON_NOT_FOUND
    info = candidate.info()
    if not info_available_in_context(ctx, info):
        return TOOL_REASON_CONTEXT_UNAVAILABLE
    return tool_access_reason(actor, policy, info)


def tool_access_reason(actor: Actor, policy: Policy | None, info: Info) -> str:
    """只报告角色与策略层面的判定原因，不检查上下文可用性。

    对应 Go ``ToolAccessReason``。
    """
    if info.superadmin_only and getattr(actor, "role", None) != RoleSuperadmin:
        return TOOL_REASON_SUPERADMIN_ONLY
    if policy is None:
        policy = DefaultPolicy()
    if not policy.can_use_tool(
        actor, normalize_risk(info.risk, RiskHigh), bool(info.owner_scoped)
    ):
        return TOOL_REASON_RISK_DENIED
    return ""


def registry_tool_availability_reason(
    ctx: Any,
    registry: Registry | None,
    actor: Actor,
    policy: Policy | None,
    name: str,
    allow_hidden: bool,
) -> str:
    """按名字从注册表取工具并报告不可用原因。对应 Go ``RegistryToolAvailabilityReason``。

    名字不存在时返回 ``tool_not_found``；存在但隐藏时返回 ``tool_hidden``（隐藏
    工具不应出现在"允许暴露隐藏工具的路径"之外的审计里）。
    """
    name = (name or "").strip()
    if name == "" or registry is None:
        return TOOL_REASON_NOT_FOUND
    candidate, ok = registry.get(name)
    if not ok or candidate is None:
        return TOOL_REASON_NOT_FOUND
    reason = tool_availability_reason(ctx, actor, policy, candidate)
    if reason != "":
        return reason
    if candidate.info().hidden and not allow_hidden:
        return TOOL_REASON_HIDDEN
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
