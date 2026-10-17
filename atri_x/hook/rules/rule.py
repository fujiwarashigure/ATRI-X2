"""规则的校验、角色条件展开与执行。

对应 Go：``internal/hook/rules/rule.go``。
"""

from __future__ import annotations

from typing import Any

from atri_x.delivery.delivery import validate_delivery_timing
from atri_x.hook.match import Condition, Match, MATCH_ALWAYS, MATCH_FULL
from atri_x.hook.rules.types import Action, Rule
from atri_x.security import (
    GroupRole,
    ParseGroupRole,
    Role,
)

__all__ = [
    "RuleError",
    "validate_exec_action",
    "rule_registrations",
    "rule_role_conditions",
    "rule_conditions",
    "validate_rule",
    "validate_roles",
    "validate_action_timing",
    "run_rule",
    "matches_roles",
    "matches_unified_role",
    "contains_string",
]


class RuleError(RuntimeError):
    """规则执行失败（Go 里是 ``fmt.Errorf``）。"""


def validate_exec_action(action: Action) -> None:
    """``exec`` 动作的参数校验。对应 Go ``validateExecAction``。"""
    if not action.command or not (action.command[0] or "").strip():
        raise ValueError("command is required")
    if action.timeout_seconds < 0:
        raise ValueError("timeout_seconds cannot be negative")


def rule_registrations(rule: Rule) -> list[Match]:
    """按角色划分出多条注册。对应 Go ``ruleRegistrations``。"""
    role_conditions = rule_role_conditions(rule)
    if not role_conditions:
        return [Match(conditions=list(rule.match))]
    out: list[Match] = []
    for condition in role_conditions:
        conditions = list(rule.match)
        conditions.append(condition)
        out.append(Match(conditions=conditions))
    return out


def rule_role_conditions(rule: Rule) -> list[Condition]:
    """把角色声明展开成条件。对应 Go ``ruleRoleConditions``。"""
    seen: set[str] = set()
    out: list[Condition] = []

    def add(field: str, role: str) -> None:
        role = (role or "").strip()
        if not role:
            return
        key = f"{field}:{role}"
        if key in seen:
            return
        seen.add(key)
        out.append(Condition(field=field, op=MATCH_FULL, value=role))

    for role in rule.roles:
        role = (role or "").strip()
        if role in (Role.SUPERADMIN, Role.USER):
            add("actor.role", role)
        else:
            parsed = ParseGroupRole(role)
            if parsed != GroupRole.UNKNOWN or role == GroupRole.UNKNOWN:
                add("actor.group_role", parsed)
    for role in rule.actor_roles:
        add("actor.role", role)
    for role in rule.group_roles:
        add("actor.group_role", ParseGroupRole(role))
    return out


def rule_conditions(rule: Rule) -> list[Condition]:
    """把 ``if/op/value``、``always``、``match`` 归一到条件列表。

    对应 Go ``Rule.conditions``。
    """
    flat = bool((rule.if_ or "").strip()) or bool(
        (rule.op or "").strip()
    ) or bool(rule.value)
    if rule.always:
        if flat or rule.match:
            raise ValueError("always cannot be combined with if/op/value or match")
        return [Condition(op=MATCH_ALWAYS)]
    if flat and rule.match:
        raise ValueError("if/op/value cannot be combined with match")
    if flat:
        return [Condition(field=rule.if_, op=rule.op, value=rule.value)]
    return list(rule.match)


def validate_rule(rule: Rule) -> None:
    """一条规则的整体校验。对应 Go ``validateRule``。"""
    from atri_x.hook.event import known_point

    if not (rule.on or "").strip():
        raise ValueError(f"hook rule {rule.name!r} missing on")
    if not known_point(rule.on):
        raise ValueError(
            f"hook rule {rule.name!r} unknown hook point {rule.on!r}"
        )
    if rule.source.runtime_id:
        if rule.actions:
            raise ValueError(
                f"worker hook rule {rule.name!r} only declares event triggers "
                "and cannot set actions"
            )
    elif not rule.actions:
        raise ValueError(f"hook rule {rule.name!r} has no actions")
    if not rule.match:
        raise ValueError(f"hook rule {rule.name!r} missing match")
    try:
        Match(conditions=list(rule.match)).validate()
    except ValueError as exc:
        raise ValueError(f"hook rule {rule.name!r} matches: {exc}") from exc
    try:
        validate_roles(rule)
    except ValueError as exc:
        raise ValueError(f"hook rule {rule.name!r}: {exc}") from exc
    from atri_x.hook.manager import validate_wakeup_policy

    try:
        validate_wakeup_policy(rule.wakeup)
    except ValueError as exc:
        raise ValueError(f"hook rule {rule.name!r}: {exc}") from exc
    for action in rule.actions:
        if not (action.type or "").strip():
            raise ValueError(f"hook rule {rule.name!r} has action without type")
        try:
            validate_action_timing(action.timing)
        except Exception as exc:  # noqa: BLE001 - 交付层的 InvalidError
            raise ValueError(
                f"hook rule {rule.name!r} action "
                f"{_first_non_empty(action.action_name, action.type)!r}: {exc}"
            ) from exc
        if (action.type or "").strip() == "exec":
            try:
                validate_exec_action(action)
            except ValueError as exc:
                raise ValueError(
                    f"hook rule {rule.name!r} action "
                    f"{_first_non_empty(action.action_name, action.type)!r}: {exc}"
                ) from exc


def validate_roles(rule: Rule) -> None:
    """角色字段校验。对应 Go ``Rule.validateRoles``。"""
    for role in rule.roles:
        role = (role or "").strip()
        if role in (Role.SUPERADMIN, Role.USER):
            continue
        if ParseGroupRole(role) == GroupRole.UNKNOWN and role != GroupRole.UNKNOWN:
            raise ValueError(f"unsupported role {role!r}")
    for role in rule.actor_roles:
        if (role or "").strip() not in (Role.SUPERADMIN, Role.USER):
            raise ValueError(f"unsupported actor role {role!r}")
    for role in rule.group_roles:
        if (
            ParseGroupRole(role) == GroupRole.UNKNOWN
            and (role or "").strip() != GroupRole.UNKNOWN
        ):
            raise ValueError(f"unsupported group role {role!r}")


def validate_action_timing(timing: str) -> None:
    """动作 timing 校验。对应 Go ``validateActionTiming``。"""
    validate_delivery_timing(timing)


async def run_rule(module: Any, ctx: Any, rule: Rule, event: Any) -> Any:
    """执行一条规则。对应 Go ``Module.runRule``。"""
    from atri_x.hook.rules.action import State, run_action

    # Go 的 Event 是值语义（入参是副本）；Python 里显式克隆以保持同样的可观察行为。
    event = event.clone()
    if not matches_roles(rule, event):
        return event
    before = event.clone()
    state = State()
    pass_through: bool | None = None
    for index, action in enumerate(rule.actions):
        action.source = rule.source
        try:
            event, result = await run_action(module, ctx, event, action, state)
        except Exception as exc:  # noqa: BLE001 - 统一包装成规则错误
            raise RuleError(
                f"rule {rule.name!r} action {index + 1} {action.type}: {exc}"
            ) from exc
        if result.matched is not None and not result.matched:
            return before
        if result.pass_through is not None:
            pass_through = result.pass_through
    if pass_through is not None:
        if pass_through:
            event.control = before.control
        else:
            event.control.consume = True
            event.control.stop_propagation = True
    else:
        if rule.consume:
            event.control.consume = True
        if rule.stop_propagation:
            event.control.stop_propagation = True
    return event


def matches_roles(rule: Rule, event: Any) -> bool:
    """角色是否放行。对应 Go ``Rule.matchRoles``。"""
    if rule.roles and not matches_unified_role(rule.roles, event):
        return False
    if rule.actor_roles and not contains_string(rule.actor_roles, event.actor.role):
        return False
    if rule.group_roles and not contains_string(
        rule.group_roles, event.actor.group_role
    ):
        return False
    return True


def matches_unified_role(roles: list[str], event: Any) -> bool:
    """``roles`` 同时匹配 actor.role 与 actor.group_role。对应 Go ``matchesUnifiedRole``。"""
    for role in roles:
        role = (role or "").strip()
        if role == event.actor.role or role == event.actor.group_role:
            return True
    return False


def contains_string(values: list[str], target: str) -> bool:
    """对应 Go ``containsString``。"""
    target = (target or "").strip()
    return any((value or "").strip() == target for value in values)


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""
