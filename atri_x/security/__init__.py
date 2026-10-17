"""工具权限与风险策略。

对应 Go：``internal/security/``（``security.go`` + ``context.go``）。

**这是 ``Actor`` / ``Policy`` / ``Role`` / ``GroupRole`` / ``RiskLevel`` 的全仓唯一出处。**
其他模块（``atri_x.tool.capability``、``atri_x.command.types`` 等）从这里 import，
不要再复制一份 —— 各留一份会让 ``isinstance`` 跨模块失效。

为便于交叉检索 Go 的导出名，这里同时提供 ``snake_case`` 与 ``PascalCase`` 两套别名
（与 ``atri_x.redact`` 的做法一致）。
"""

from __future__ import annotations

from atri_x.security.context import (
    ACTOR_CONTEXT_KEY,
    POLICY_CONTEXT_KEY,
    actor_from_context,
    policy_from_context,
    with_actor,
    with_policy,
)
from atri_x.security.security import (
    Actor,
    GroupRole,
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    GroupRoleUnknown,
    Policy,
    RiskCritical,
    RiskHigh,
    RiskLevel,
    RiskLow,
    RiskMedium,
    RiskSafe,
    Role,
    RoleSuperadmin,
    RoleUser,
    actor_id,
    compare_risk,
    default_policy,
    new_policy,
    normalize,
    parse_group_role,
    parse_risk,
    risk_rank,
)

# -- PascalCase 别名（对齐 Go 的导出名，便于交叉检索） ----------------------
ActorFromContext = actor_from_context
PolicyFromContext = policy_from_context
WithActor = with_actor
WithPolicy = with_policy
ActorID = actor_id
CompareRisk = compare_risk
DefaultPolicy = default_policy
NewPolicy = new_policy
Normalize = normalize
ParseGroupRole = parse_group_role
ParseRisk = parse_risk
RiskRank = risk_rank

__all__ = [
    "ACTOR_CONTEXT_KEY",
    "POLICY_CONTEXT_KEY",
    "Actor",
    "ActorFromContext",
    "ActorID",
    "CompareRisk",
    "DefaultPolicy",
    "GroupRole",
    "GroupRoleAdmin",
    "GroupRoleMember",
    "GroupRoleOwner",
    "GroupRoleUnknown",
    "NewPolicy",
    "Normalize",
    "ParseGroupRole",
    "ParseRisk",
    "Policy",
    "PolicyFromContext",
    "RiskCritical",
    "RiskHigh",
    "RiskLevel",
    "RiskLow",
    "RiskMedium",
    "RiskRank",
    "RiskSafe",
    "Role",
    "RoleSuperadmin",
    "RoleUser",
    "WithActor",
    "WithPolicy",
    "actor_from_context",
    "actor_id",
    "compare_risk",
    "default_policy",
    "new_policy",
    "normalize",
    "parse_group_role",
    "parse_risk",
    "policy_from_context",
    "risk_rank",
    "with_actor",
    "with_policy",
]
