"""群投票：创建 / 列表 / 查看 / 投票 / 关闭。

对应 Go：``internal/agent/group_services_polls.go``。

与 Go 的差异（形式，不是简化）：``*Agent`` 方法 → 模块级函数 + mixin 方法；
命令接口按 ``commands/deps.GroupServicesService`` 做成 ``async``。选项计数与
排序逻辑逐字对齐 Go（``formatGroupPoll`` 的 "N 票" 与"你的选择"两行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.config import GroupPollConfig
from atri_x.errors import InvalidError

from atri_x.agent.group_services_reminders import (
    GroupServicesMixin,
    can_manage_group_service_entry,
)
from atri_x.agent.group_services_state import (
    format_group_service_time,
    group_polls_for_scope,
    group_services_config,
    next_group_service_id,
    require_group_services,
    set_group_polls_for_scope,
    with_group_services_write,
)
from atri_x.agent.tools import first_non_empty, rune_len

__all__ = [
    "GROUP_POLL_PREFIX",
    "POLL_STATUS_CLOSED",
    "POLL_STATUS_OPEN",
    "PollServicesMixin",
    "find_poll",
    "format_group_poll",
    "format_signup_capacity",
    "poll_close",
    "poll_create",
    "poll_list",
    "poll_show",
    "poll_vote",
]

#: 投票 ID 前缀。对应 Go ``groupPollPrefix``。
GROUP_POLL_PREFIX = "p"
POLL_STATUS_OPEN = "open"
POLL_STATUS_CLOSED = "closed"


def poll_create(agent: Any, ctx: Any, question: str, options: list[str] | None) -> str:
    """创建投票。对应 Go ``PollCreate``。"""
    require_group_services(agent, ctx)
    config = group_services_config(agent).normalized()
    prompt = str(question or "").strip()
    if not prompt:
        raise InvalidError("投票问题不能为空")
    if rune_len(prompt) > config.max_text_runes:
        raise InvalidError("投票问题超过 %d 个字符" % config.max_text_runes)
    clean: list[str] = []
    seen: set[str] = set()
    for option in options or ():
        text_value = str(option or "").strip()
        if not text_value:
            continue
        if rune_len(text_value) > config.max_text_runes:
            raise InvalidError("投票选项超过 %d 个字符" % config.max_text_runes)
        key = text_value.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(text_value)
    if len(clean) < 2:
        raise InvalidError("投票至少需要两个选项")
    if len(clean) > config.max_poll_options:
        raise InvalidError("投票最多 %d 个选项" % config.max_poll_options)
    scope = _scope(agent, ctx)
    actor = _actor(agent, ctx)
    entry = GroupPollConfig()

    def _write() -> None:
        entries = group_polls_for_scope(agent, scope)
        open_count = 0
        ids: list[str] = []
        for existing in entries:
            ids.append(existing.id)
            if existing.status in ("", POLL_STATUS_OPEN):
                open_count += 1
        if open_count >= config.max_polls_per_scope:
            raise InvalidError(
                "当前群进行中的投票已达到 %d 个上限" % config.max_polls_per_scope
            )
        entry.id = next_group_service_id(GROUP_POLL_PREFIX, ids)
        entry.question = prompt
        entry.options = list(clean)
        entry.votes = {}
        entry.voters = {}
        entry.created_by = actor.id
        entry.created_by_name = _display_name(actor)
        entry.created_at = format_group_service_time(_now())
        entry.status = POLL_STATUS_OPEN
        set_group_polls_for_scope(agent, scope, entries + [entry])

    with_group_services_write(agent, _write)
    _audit(
        agent,
        "group_poll_create",
        platform=scope.platform,
        scope=scope.platform_scope_id,
        actor_id=actor.id,
        poll_id=entry.id,
        result="succeeded",
    )
    return format_group_poll(entry, "")


def poll_list(agent: Any, ctx: Any) -> str:
    """列出进行中的投票。对应 Go ``PollList``。"""
    require_group_services(agent, ctx)
    entries = group_polls_for_scope(agent, _scope(agent, ctx))
    open_polls = [entry for entry in entries if entry.status in ("", POLL_STATUS_OPEN)]
    if not open_polls:
        return "当前群没有进行中的投票。"
    lines = ["当前群投票（%d 个）：" % len(open_polls)]
    for entry in open_polls:
        lines.append("- %s  %s（%d 票）" % (entry.id, entry.question, len(entry.votes or {})))
    return "\n".join(lines)


def poll_show(agent: Any, ctx: Any, id: str) -> str:
    """展示投票详情。对应 Go ``PollShow``。"""
    require_group_services(agent, ctx)
    entry = find_poll(agent, ctx, id)
    if entry is None:
        return "没有找到投票 %r" % str(id or "").strip()
    return format_group_poll(entry, getattr(_actor(agent, ctx), "id", ""))


def poll_vote(agent: Any, ctx: Any, id: str, option_text: str) -> str:
    """投一票（可改票）。对应 Go ``PollVote``。"""
    require_group_services(agent, ctx)
    target = str(id or "").strip()
    choice = str(option_text or "").strip()
    if not target or not choice:
        raise InvalidError("用法：/vote <投票id> <选项序号>")
    try:
        index = int(choice)
    except ValueError:
        index = 0
    if index < 1:
        raise InvalidError("选项序号必须是正整数")
    scope = _scope(agent, ctx)
    actor = _actor(agent, ctx)
    found: list[bool] = [False]
    option_name: list[str] = [""]

    def _write() -> None:
        entries = group_polls_for_scope(agent, scope)
        for entry in entries:
            if entry.id.strip().lower() != target.lower():
                continue
            if entry.status not in ("", POLL_STATUS_OPEN):
                raise InvalidError("投票 %s 已关闭" % target)
            if index > len(entry.options or []):
                raise InvalidError("选项序号超出范围（1-%d）" % len(entry.options or []))
            if entry.votes is None:
                entry.votes = {}
            if entry.voters is None:
                entry.voters = {}
            entry.votes[actor.id] = index
            entry.voters[actor.id] = _display_name(actor)
            option_name[0] = entry.options[index - 1]
            found[0] = True
            break
        if found[0]:
            set_group_polls_for_scope(agent, scope, entries)

    with_group_services_write(agent, _write)
    if not found[0]:
        return "没有找到投票 %r" % target
    return "已投票：%s 选项 %d %s" % (target, index, option_name[0])


def poll_close(agent: Any, ctx: Any, id: str) -> str:
    """关闭投票（仅创建者/群管/超管）。对应 Go ``PollClose``。"""
    require_group_services(agent, ctx)
    scope = _scope(agent, ctx)
    closed: list[GroupPollConfig] = []

    def _write() -> None:
        entries = group_polls_for_scope(agent, scope)
        for entry in entries:
            if entry.id.strip().lower() != str(id or "").strip().lower():
                continue
            if not can_manage_group_service_entry(agent, ctx, entry.created_by):
                raise InvalidError("只有投票创建者、当前群管理员或超级管理员可以关闭")
            entry.status = POLL_STATUS_CLOSED
            closed.append(entry.copy())
            set_group_polls_for_scope(agent, scope, entries)
            break

    with_group_services_write(agent, _write)
    if not closed:
        return "没有找到投票 %r" % str(id or "").strip()
    _audit(
        agent,
        "group_poll_close",
        platform=scope.platform,
        scope=scope.platform_scope_id,
        actor_id=getattr(_actor(agent, ctx), "id", ""),
        poll_id=closed[0].id,
        result="succeeded",
    )
    return format_group_poll(closed[0], getattr(_actor(agent, ctx), "id", ""))


def find_poll(agent: Any, ctx: Any, id: str) -> GroupPollConfig | None:
    """按 ID 查投票（大小写不敏感）。对应 Go ``findPoll``。"""
    target = str(id or "").strip()
    for entry in group_polls_for_scope(agent, _scope(agent, ctx)):
        if entry.id.strip().lower() == target.lower():
            return entry
    return None


def format_group_poll(entry: GroupPollConfig, actor_id: str) -> str:
    """投票详情文案。对应 Go ``formatGroupPoll``。"""
    status = entry.status or POLL_STATUS_OPEN
    options = list(entry.options or [])
    lines = ["[%s] %s（%s）" % (entry.id, entry.question, status)]
    counts = [0] * len(options)
    votes = entry.votes or {}
    for actor, option in votes.items():
        if 1 <= int(option) <= len(counts):
            counts[int(option) - 1] += 1
        if actor_id and actor == actor_id:
            if 1 <= int(option) <= len(options):
                lines.append("你的选择：%d %s" % (option, options[int(option) - 1]))
    for index, option in enumerate(options):
        lines.append("  %d. %s - %d 票" % (index + 1, option, counts[index]))
    lines.append("共 %d 票" % len(votes))
    return "\n".join(lines)


def format_signup_capacity(capacity: int) -> str:
    """报名容量文案（``0`` = 不限）。对应 Go ``formatSignupCapacity``。"""
    if capacity <= 0:
        return "不限"
    return str(capacity)


class PollServicesMixin(GroupServicesMixin):
    """Go ``*Agent`` 的投票方法组（含提醒方法，见基类）。"""

    async def poll_create(self, ctx: Any, question: str, options: list[str]) -> str:
        return poll_create(self, ctx, question, options)

    async def poll_list(self, ctx: Any) -> str:
        return poll_list(self, ctx)

    async def poll_show(self, ctx: Any, id: str) -> str:
        return poll_show(self, ctx, id)

    async def poll_vote(self, ctx: Any, id: str, option: str) -> str:
        return poll_vote(self, ctx, id, option)

    async def poll_close(self, ctx: Any, id: str) -> str:
        return poll_close(self, ctx, id)


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
