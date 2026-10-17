"""工具权限与风险策略。

对应 Go：``internal/security/security.go``。

角色、风险等级、Actor 身份规范化与准入/确认判定都在这里；审计与工具运行
时共用同一套判定顺序，迁移调用点不得顺带改变准入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "Actor",
    "GroupRole",
    "GroupRoleAdmin",
    "GroupRoleMember",
    "GroupRoleOwner",
    "GroupRoleUnknown",
    "Policy",
    "RiskCritical",
    "RiskHigh",
    "RiskLevel",
    "RiskLow",
    "RiskMedium",
    "RiskSafe",
    "Role",
    "RoleSuperadmin",
    "RoleUser",
    "actor_id",
    "compare_risk",
    "default_policy",
    "new_policy",
    "normalize",
    "parse_group_role",
    "parse_risk",
    "risk_rank",
]


class Role:
    """平台角色。对应 Go ``security.Role``。"""

    SUPERADMIN = "superadmin"
    USER = "user"


RoleSuperadmin = Role.SUPERADMIN
RoleUser = Role.USER


class GroupRole:
    """群内角色。对应 Go ``security.GroupRole``。"""

    UNKNOWN = "unknown"
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


GroupRoleUnknown = GroupRole.UNKNOWN
GroupRoleOwner = GroupRole.OWNER
GroupRoleAdmin = GroupRole.ADMIN
GroupRoleMember = GroupRole.MEMBER


class RiskLevel:
    """工具风险等级。对应 Go ``security.RiskLevel``。"""

    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


RiskSafe = RiskLevel.SAFE
RiskLow = RiskLevel.LOW
RiskMedium = RiskLevel.MEDIUM
RiskHigh = RiskLevel.HIGH
RiskCritical = RiskLevel.CRITICAL


@dataclass
class Actor:
    """一次调用的权限主体。对应 Go ``security.Actor``。"""

    id: str = ""
    platform: str = ""
    platform_user_id: str = ""
    nickname: str = ""
    group_card: str = ""
    display_name: str = ""
    role: str = RoleUser
    group_role: str = GroupRoleUnknown


@dataclass
class Policy:
    """工具风险策略。对应 Go ``security.Policy``。"""

    user_max_tool_risk: str = RiskLow
    superadmin_confirm_risk: str = RiskHigh
    superadmins: dict[str, dict[str, bool]] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "Policy":
        """对应 Go ``security.DefaultPolicy``。"""
        return default_policy()

    def actor(
        self, actor_id_value: str, platform: str, platform_user_id: str, display_name: str
    ) -> Actor:
        """规范化平台身份并判定角色（对应 Go ``Policy.Actor``）。"""
        platform = normalize(platform)
        platform_user_id = _canonical_platform_user_id(platform, platform_user_id)
        if platform_user_id == "":
            platform_user_id = _canonical_platform_user_id(platform, actor_id_value)
        role = RoleUser
        if self.is_superadmin(platform, platform_user_id):
            role = RoleSuperadmin
        return Actor(
            id=actor_id(platform, platform_user_id),
            platform=platform,
            platform_user_id=platform_user_id,
            display_name=display_name,
            role=role,
            group_role=GroupRoleUnknown,
        )

    def is_superadmin(self, platform: str, platform_user_id: str) -> bool:
        """该平台身份是否是超管。"""
        ids = self.superadmins.get(normalize(platform))
        if not ids:
            return False
        return bool(ids.get((platform_user_id or "").strip()))

    def can_use_tool(self, actor: Actor, risk: str, owner_scoped: bool) -> bool:
        """普通用户能否直接使用某风险等级的工具。"""
        if actor.role == RoleSuperadmin:
            return True
        if owner_scoped:
            return True
        return compare_risk(risk, self.user_max_tool_risk) <= 0

    def needs_tool_confirmation(self, actor: Actor, risk: str) -> bool:
        """是否必须先向用户做风险确认。"""
        threshold = RiskHigh
        if actor.role == RoleSuperadmin:
            threshold = self.superadmin_confirm_risk
        return compare_risk(risk, threshold) >= 0


def new_policy(
    user_max_tool_risk: str,
    superadmin_confirm_risk: str,
    superadmins: Mapping[str, Sequence[str]] | None = None,
) -> Policy:
    """对应 Go ``security.NewPolicy``。"""
    policy = Policy(
        user_max_tool_risk=parse_risk(user_max_tool_risk, RiskLow),
        superadmin_confirm_risk=parse_risk(superadmin_confirm_risk, RiskHigh),
        superadmins={},
    )
    for platform, ids in (superadmins or {}).items():
        platform = normalize(platform)
        if platform == "":
            continue
        bucket = policy.superadmins.setdefault(platform, {})
        for value in ids:
            value = (value or "").strip()
            if value:
                bucket[value] = True
    return policy


def default_policy() -> Policy:
    """对应 Go ``security.DefaultPolicy``（cli/local 是超管）。"""
    return new_policy("low", "high", {"cli": ["local"]})


def parse_risk(value: str, fallback: str) -> str:
    """解析风险等级，无法识别时用 ``fallback``。"""
    normalized = normalize(value)
    if normalized in (RiskSafe, RiskLow, RiskMedium, RiskHigh, RiskCritical):
        return normalized
    return fallback


def parse_group_role(value: str) -> str:
    """解析群内角色（对应 Go ``security.ParseGroupRole``）。"""
    normalized = normalize(value)
    if normalized in ("owner", "group_owner", "creator"):
        return GroupRoleOwner
    if normalized in ("admin", "administrator", "group_admin"):
        return GroupRoleAdmin
    if normalized in ("member", "user", "group_member"):
        return GroupRoleMember
    return GroupRoleUnknown


def actor_id(platform: str, platform_user_id: str) -> str:
    """构造稳定的 Actor ID（``platform:user``）。"""
    platform = normalize(platform)
    platform_user_id = _canonical_platform_user_id(platform, platform_user_id)
    if platform == "":
        platform = "unknown"
    if platform_user_id == "":
        platform_user_id = "unknown"
    return platform + ":" + platform_user_id


def compare_risk(left: str, right: str) -> int:
    """风险等级比较。"""
    return risk_rank(left) - risk_rank(right)


def risk_rank(risk: str) -> int:
    """风险等级序数；未知值按 High 处理（与 Go 一致，刻意保守）。"""
    if risk == RiskSafe:
        return 0
    if risk == RiskLow:
        return 1
    if risk == RiskMedium:
        return 2
    if risk == RiskHigh:
        return 3
    if risk == RiskCritical:
        return 4
    return 3


def normalize(value: str) -> str:
    """小写 + 去空白。"""
    return (value or "").strip().lower()


def _canonical_platform_user_id(platform: str, value: str) -> str:
    value = (value or "").strip()
    prefix = normalize(platform) + ":"
    if prefix != ":" and value.startswith(prefix):
        return value[len(prefix) :].strip()
    return value
