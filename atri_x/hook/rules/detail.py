"""``/hooks`` 列表里展示的规则详情文本。

对应 Go：``internal/hook/rules/detail.go``。
"""

from __future__ import annotations

import json
from typing import Any

from atri_x.hook.manager import WAKEUP_REQUIRED
from atri_x.hook.output import Target
from atri_x.hook.rules.types import Rule

__all__ = ["format_rule_detail", "rule_description", "target_string", "strconv_quote"]


def format_rule_detail(rule: Rule) -> str:
    """拼出规则的详情块。对应 Go ``formatRuleDetail``。"""
    parts: list[str] = ["on: " + (rule.on or "").strip()]
    if rule.always:
        parts.append("match: always")
    elif rule.match:
        parts.append("match:")
        for cond in rule.match:
            parts.append(f"  {cond.field} {cond.op} {strconv_quote(cond.value)}")
    elif (rule.if_ or "").strip():
        parts.append(f"match: {rule.if_} {rule.op} {strconv_quote(rule.value)}")

    if rule.roles:
        parts.append("roles: " + ", ".join(rule.roles))
    if rule.actor_roles:
        parts.append("actor_roles: " + ", ".join(rule.actor_roles))
    if rule.group_roles:
        parts.append("group_roles: " + ", ".join(rule.group_roles))
    if rule.blocked_platforms:
        parts.append("blocked_platform: " + ", ".join(rule.blocked_platforms))
    if rule.blocked_groups:
        parts.append("blocked_group: " + ", ".join(rule.blocked_groups))
    if rule.blocked_ids:
        parts.append("blocked_id: " + ", ".join(rule.blocked_ids))

    for index, action in enumerate(rule.actions):
        line = f"action[{index + 1}]: {action.type}"
        if action.action_name:
            line += " (" + action.action_name + ")"
        if action.field:
            line += " field=" + action.field
        if action.text:
            line += " text=" + strconv_quote(action.text)
        if action.match:
            line += " pattern=" + strconv_quote(action.match)
        if action.replace:
            line += " replace=" + strconv_quote(action.replace)
        if action.tool:
            line += " tool=" + action.tool
        if action.arguments:
            line += " args=" + strconv_quote(action.arguments)
        if action.command:
            line += " command=" + _command_repr(action.command)
        if action.timing:
            line += " timing=" + action.timing
        if (
            action.target.platform
            or action.target.scope_id
            or action.target.private_user_id
            or action.target.group_id
        ):
            line += " target=" + target_string(action.target)
        if action.all:
            line += " all=true"
        if action.outputs:
            line += f" outputs={len(action.outputs)}"
        parts.append(line)

    if rule.consume:
        parts.append("consume: true")
    if rule.stop_propagation:
        parts.append("stop_propagation: true")
    if rule.wakeup and rule.wakeup != WAKEUP_REQUIRED:
        parts.append("wakeup: " + rule.wakeup)
    if rule.priority:
        parts.append(f"priority: {rule.priority}")
    return "\n".join(parts)


def rule_description(rule: Rule) -> str:
    """规则描述，缺省取插件描述。对应 Go ``ruleDescription``。"""
    description = (rule.description or "").strip()
    if description:
        return description
    return (rule.source.plugin_description or "").strip()


def target_string(target: Target) -> str:
    """``platform=...,scope_id=...``。对应 Go ``targetString``。"""
    parts: list[str] = []
    if target.platform:
        parts.append("platform=" + target.platform)
    if target.scope_id:
        parts.append("scope_id=" + target.scope_id)
    if target.private_user_id:
        parts.append("private_user_id=" + target.private_user_id)
    if target.group_id:
        parts.append("group_id=" + target.group_id)
    if target.superadmins:
        parts.append("superadmins=true")
    return ",".join(parts)


def strconv_quote(value: str) -> str:
    """Go ``%q`` 风格的双引号字符串。对应 Go ``strconvQuote``。"""
    if value == "":
        return '""'
    return json.dumps(value, ensure_ascii=False)


def _command_repr(command: list[str]) -> str:
    """Go 的 ``%q`` 作用在 ``[]string`` 上的形态：``["a" "b"]``。"""
    return "[" + " ".join(strconv_quote(item) for item in command) + "]"


# 交叉检索：Go 里用到 ``fmt`` 的 ``Sprintf``。
_FMT: Any = None
