"""群报名：创建 / 列表 / 查看 / 加入 / 退出 / 关闭。

对应 Go：``internal/agent/group_services_signups.go``。

与 Go 的差异（形式，不是简化）：``*Agent`` 方法 → 模块级函数 + mixin 方法；
命令接口按 ``commands/deps.GroupServicesService`` 做成 ``async``。
``formatGroupSignup`` 的成员名按字典序排序（与 Go 的 ``sort.Strings`` 一致）。
"""

from __future__ import annotations

from typing import Any

from atri_x.config import GroupSignupConfig
from atri_x.errors import InvalidError

from atri_x.agent.group_services_polls import (
    PollServicesMixin,
    format_signup_capacity,
)
from atri_x.agent.group_services_reminders import can_manage_group_service_entry
from atri_x.agent.group_services_state import (
    format_group_service_time,
    group_services_config,
    group_signups_for_scope,
    next_group_service_id,
    require_group_services,
    set_group_signups_for_scope,
    with_group_services_write,
)
from atri_x.agent.tools import first_non_empty, rune_len

__all__ = [
    "GROUP_SIGNUP_PREFIX",
    "SIGNUP_STATUS_CLOSED",
    "SIGNUP_STATUS_OPEN",
    "SignupServicesMixin",
    "find_signup",
    "format_group_signup",
    "format_signup_capacity",
    "signup_close",
    "signup_create",
    "signup_join",
    "signup_leave",
    "signup_list",
    "signup_show",
]

#: 报名 ID 前缀。对应 Go ``groupSignupPrefix``。
GROUP_SIGNUP_PREFIX = "s"
SIGNUP_STATUS_OPEN = "open"
SIGNUP_STATUS_CLOSED = "closed"


def signup_create(agent: Any, ctx: Any, title: str, capacity: int) -> str:
    """创建报名。对应 Go ``SignupCreate``。"""
    require_group_services(agent, ctx)
    config = group_services_config(agent).normalized()
    name = str(title or "").strip()
    if not name:
        raise InvalidError("报名标题不能为空")
    if rune_len(name) > config.max_text_runes:
        raise InvalidError("报名标题超过 %d 个字符" % config.max_text_runes)
    if capacity < 0 or capacity > config.max_signup_capacity:
        raise InvalidError(
            "报名人数必须在 0-%d 之间（0 表示不限制）" % config.max_signup_capacity
        )
    scope = _scope(agent, ctx)
    actor = _actor(agent, ctx)
    entry = GroupSignupConfig()

    def _write() -> None:
        entries = group_signups_for_scope(agent, scope)
        open_count = 0
        ids: list[str] = []
        for existing in entries:
            ids.append(existing.id)
            if existing.status in ("", SIGNUP_STATUS_OPEN):
                open_count += 1
        if open_count >= config.max_signups_per_scope:
            raise InvalidError(
                "当前群进行中的报名已达到 %d 个上限" % config.max_signups_per_scope
            )
        entry.id = next_group_service_id(GROUP_SIGNUP_PREFIX, ids)
        entry.title = name
        entry.capacity = capacity
        entry.participants = {}
        entry.created_by = actor.id
        entry.created_by_name = _display_name(actor)
        entry.created_at = format_group_service_time(_now())
        entry.status = SIGNUP_STATUS_OPEN
        set_group_signups_for_scope(agent, scope, entries + [entry])

    with_group_services_write(agent, _write)
    _audit(
        agent,
        "group_signup_create",
        platform=scope.platform,
        scope=scope.platform_scope_id,
        actor_id=actor.id,
        signup_id=entry.id,
        result="succeeded",
    )
    return format_group_signup(entry, actor.id)


def signup_list(agent: Any, ctx: Any) -> str:
    """列出进行中的报名。对应 Go ``SignupList``。"""
    require_group_services(agent, ctx)
    entries = group_signups_for_scope(agent, _scope(agent, ctx))
    open_signups = [
        entry for entry in entries if entry.status in ("", SIGNUP_STATUS_OPEN)
    ]
    if not open_signups:
        return "当前群没有进行中的报名。"
    lines = ["当前群报名（%d 个）：" % len(open_signups)]
    for entry in open_signups:
        lines.append(
            "- %s  %s（%d/%s）"
            % (
                entry.id,
                entry.title,
                len(entry.participants or {}),
                format_signup_capacity(entry.capacity),
            )
        )
    return "\n".join(lines)


def signup_show(agent: Any, ctx: Any, id: str) -> str:
    """展示报名详情。对应 Go ``SignupShow``。"""
    require_group_services(agent, ctx)
    entry = find_signup(agent, ctx, id)
    if entry is None:
        return "没有找到报名 %r" % str(id or "").strip()
    return format_group_signup(entry, getattr(_actor(agent, ctx), "id", ""))


def signup_join(agent: Any, ctx: Any, id: str) -> str:
    """加入报名（满员时拒绝，已在名单里则幂等）。对应 Go ``SignupJoin``。"""
    require_group_services(agent, ctx)
    scope = _scope(agent, ctx)
    actor = _actor(agent, ctx)
    outcome: list[str] = []

    def _write() -> None:
        entries = group_signups_for_scope(agent, scope)
        for entry in entries:
            if entry.id.strip().lower() != str(id or "").strip().lower():
                continue
            if entry.status not in ("", SIGNUP_STATUS_OPEN):
                raise InvalidError("报名 %s 已关闭" % entry.id)
            if entry.participants is None:
                entry.participants = {}
            if (
                actor.id not in entry.participants
                and entry.capacity > 0
                and len(entry.participants) >= entry.capacity
            ):
                raise InvalidError(
                    "报名 %s 已满（%d/%d）"
                    % (entry.id, len(entry.participants), entry.capacity)
                )
            entry.participants[actor.id] = _display_name(actor)
            outcome.append(
                "已加入报名 %s：%s（%d/%s）"
                % (
                    entry.id,
                    entry.title,
                    len(entry.participants),
                    format_signup_capacity(entry.capacity),
                )
            )
            set_group_signups_for_scope(agent, scope, entries)
            break

    with_group_services_write(agent, _write)
    if not outcome:
        return "没有找到报名 %r" % str(id or "").strip()
    return outcome[0]


def signup_leave(agent: Any, ctx: Any, id: str) -> str:
    """退出报名（未加入也算成功）。对应 Go ``SignupLeave``。"""
    require_group_services(agent, ctx)
    scope = _scope(agent, ctx)
    actor = _actor(agent, ctx)
    outcome: list[str] = []

    def _write() -> None:
        entries = group_signups_for_scope(agent, scope)
        for entry in entries:
            if entry.id.strip().lower() != str(id or "").strip().lower():
                continue
            if entry.participants is not None:
                entry.participants.pop(actor.id, None)
            outcome.append(
                "已退出报名 %s：%s（%d/%s）"
                % (
                    entry.id,
                    entry.title,
                    len(entry.participants or {}),
                    format_signup_capacity(entry.capacity),
                )
            )
            set_group_signups_for_scope(agent, scope, entries)
            break

    with_group_services_write(agent, _write)
    if not outcome:
        return "没有找到报名 %r" % str(id or "").strip()
    return outcome[0]


def signup_close(agent: Any, ctx: Any, id: str) -> str:
    """关闭报名（仅创建者/群管/超管）。对应 Go ``SignupClose``。"""
    require_group_services(agent, ctx)
    scope = _scope(agent, ctx)
    closed: list[GroupSignupConfig] = []

    def _write() -> None:
        entries = group_signups_for_scope(agent, scope)
        for entry in entries:
            if entry.id.strip().lower() != str(id or "").strip().lower():
                continue
            if not can_manage_group_service_entry(agent, ctx, entry.created_by):
                raise InvalidError("只有报名创建者、当前群管理员或超级管理员可以关闭")
            entry.status = SIGNUP_STATUS_CLOSED
            closed.append(entry.copy())
            set_group_signups_for_scope(agent, scope, entries)
            break

    with_group_services_write(agent, _write)
    if not closed:
        return "没有找到报名 %r" % str(id or "").strip()
    _audit(
        agent,
        "group_signup_close",
        platform=scope.platform,
        scope=scope.platform_scope_id,
        actor_id=getattr(_actor(agent, ctx), "id", ""),
        signup_id=closed[0].id,
        result="succeeded",
    )
    return format_group_signup(closed[0], getattr(_actor(agent, ctx), "id", ""))


def find_signup(agent: Any, ctx: Any, id: str) -> GroupSignupConfig | None:
    """按 ID 查报名（大小写不敏感）。对应 Go ``findSignup``。"""
    target = str(id or "").strip()
    for entry in group_signups_for_scope(agent, _scope(agent, ctx)):
        if entry.id.strip().lower() == target.lower():
            return entry
    return None


def format_group_signup(entry: GroupSignupConfig, actor_id: str) -> str:
    """报名详情文案。对应 Go ``formatGroupSignup``。"""
    status = entry.status or SIGNUP_STATUS_OPEN
    participants = entry.participants or {}
    lines = [
        "[%s] %s（%s，%d/%s）"
        % (
            entry.id,
            entry.title,
            status,
            len(participants),
            format_signup_capacity(entry.capacity),
        )
    ]
    if not participants:
        lines.append("  暂无成员加入")
        return "\n".join(lines)
    lines.append("  成员：" + "、".join(sorted(participants.values())))
    if actor_id and actor_id in participants:
        lines.append("  你已加入")
    return "\n".join(lines)


class SignupServicesMixin(PollServicesMixin):
    """Go ``*Agent`` 的报名方法组（含提醒与投票方法，见基类）。"""

    async def signup_create(self, ctx: Any, title: str, capacity: int) -> str:
        return signup_create(self, ctx, title, capacity)

    async def signup_list(self, ctx: Any) -> str:
        return signup_list(self, ctx)

    async def signup_show(self, ctx: Any, id: str) -> str:
        return signup_show(self, ctx, id)

    async def signup_join(self, ctx: Any, id: str) -> str:
        return signup_join(self, ctx, id)

    async def signup_leave(self, ctx: Any, id: str) -> str:
        return signup_leave(self, ctx, id)

    async def signup_close(self, ctx: Any, id: str) -> str:
        return signup_close(self, ctx, id)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)


def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import actor

    return actor(agent, ctx)


def _display_name(actor: Any) -> str:
    return first_non_empty(
        getattr(actor, "group_card", ""),
        getattr(actor, "nickname", ""),
        getattr(actor, "display_name", ""),
    )


def _now() -> Any:
    from datetime import datetime

    return datetime.now().astimezone()


def _audit(agent: Any, event: str, **attributes: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in attributes.items():
        args.append(key)
        args.append(value)
    audit(*args)
