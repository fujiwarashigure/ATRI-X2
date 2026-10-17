"""群提醒：创建 / 列表 / 删除，以及到点派发。

对应 Go：``internal/agent/group_services_reminders.go``。

与 Go 的差异（形式，不是简化）：

* ``go func(){ ticker }`` → :func:`start_group_services` 建一个 ``asyncio.Task``，
  首次先跑一遍 ``process_due_reminders``（与 Go 相同），随后每 30 秒一次。
  重复调用是幂等的（``group_services_started`` 标记）。
* ``*Agent`` 方法 → 模块级函数（首个参数 ``agent``）+ :class:`GroupServicesMixin`。
  命令接口按 ``commands/deps.GroupServicesService`` 做成 ``async``。
* 时间一律带时区；落库格式由 ``group_services_state.format_group_service_time`` 统一。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from atri_x.config import GroupReminderConfig
from atri_x.delivery import Notice, Target, text
from atri_x.errors import InvalidError
from atri_x.security import GroupRoleAdmin, GroupRoleOwner, RoleSuperadmin

from atri_x.agent.group_policy import context_overflow_key, group_policy_for_scope
from atri_x.agent.group_services_state import (
    GROUP_SERVICE_TICK_SECONDS,
    MIN_REMINDER_DELAY,
    check_reminder_when,
    format_group_service_time,
    group_reminders_for_scope,
    group_service_duration_text,
    group_services_config,
    group_services_snapshot,
    next_group_service_id,
    parse_group_service_time,
    require_group_services,
    set_group_reminders_for_scope,
    set_group_services_snapshot,
    with_group_services_write,
)
from atri_x.agent.group_runtime import (
    GROUP_RUNTIME_REMOVED,
    GROUP_RUNTIME_UNAVAILABLE,
    group_runtime_blocked_scope,
    group_runtime_for_scope,
    normalize_group_runtime_state,
)
from atri_x.agent.tools import first_non_empty, rune_len, service_host

__all__ = [
    "GROUP_REMINDER_PREFIX",
    "REMINDER_STATUS_PENDING",
    "REMINDER_STATUS_SENT",
    "REMINDER_STATUS_SKIPPED",
    "GroupServicesMixin",
    "can_manage_group_service_entry",
    "process_due_reminders",
    "reminder_create",
    "reminder_list",
    "reminder_remove",
    "start_group_services",
]

#: 提醒 ID 前缀。对应 Go ``groupReminderPrefix``。
GROUP_REMINDER_PREFIX = "r"

REMINDER_STATUS_PENDING = "pending"
REMINDER_STATUS_SENT = "sent"
REMINDER_STATUS_SKIPPED = "skipped"


def reminder_create(agent: Any, ctx: Any, when_text: str, text_value: str) -> str:
    """创建一条提醒。对应 Go ``ReminderCreate``。"""
    require_group_services(agent, ctx)
    config = group_services_config(agent).normalized()
    body = str(text_value or "").strip()
    if not body:
        raise InvalidError("提醒内容不能为空")
    now = datetime.now().astimezone()
    due_at = check_reminder_when(when_text, body, config, now)
    scope = _scope(agent, ctx)
    actor = _actor(agent, ctx)
    entry = GroupReminderConfig()

    def _write() -> None:
        entries = group_reminders_for_scope(agent, scope)
        pending = 0
        ids: list[str] = []
        for existing in entries:
            ids.append(existing.id)
            if existing.status in ("", REMINDER_STATUS_PENDING):
                pending += 1
        if pending >= config.max_reminders_per_scope:
            raise InvalidError(
                "当前群待发送提醒已达到 %d 条上限" % config.max_reminders_per_scope
            )
        entry.id = next_group_service_id(GROUP_REMINDER_PREFIX, ids)
        entry.text = body
        entry.due_at = format_group_service_time(due_at)
        entry.created_by = actor.id
        entry.created_by_name = first_non_empty(
            getattr(actor, "group_card", ""),
            getattr(actor, "nickname", ""),
            getattr(actor, "display_name", ""),
        )
        entry.created_at = format_group_service_time(now)
        entry.status = REMINDER_STATUS_PENDING
        set_group_reminders_for_scope(agent, scope, entries + [entry])

    with_group_services_write(agent, _write)
    _audit(
        agent,
        "group_reminder_create",
        platform=scope.platform,
        scope=scope.platform_scope_id,
        actor_id=actor.id,
        reminder_id=entry.id,
        result="succeeded",
    )
    return "提醒已创建：%s（%s）\n%s" % (
        entry.id,
        due_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        entry.text,
    )


def reminder_list(agent: Any, ctx: Any) -> str:
    """列出待发送提醒。对应 Go ``ReminderList``。"""
    require_group_services(agent, ctx)
    entries = group_reminders_for_scope(agent, _scope(agent, ctx))
    pending = [
        entry for entry in entries if entry.status in ("", REMINDER_STATUS_PENDING)
    ]
    if not pending:
        return "当前群没有待发送提醒。"
    lines = ["当前群提醒（%d 条）：" % len(pending)]
    for entry in pending:
        due, ok = parse_group_service_time(entry.due_at)
        due_text = due.astimezone().strftime("%Y-%m-%d %H:%M:%S") if ok and due else entry.due_at
        lines.append("- %s  %s  %s" % (entry.id, due_text, entry.text))
    return "\n".join(lines)


def reminder_remove(agent: Any, ctx: Any, id: str) -> str:
    """删除一条提醒（仅创建者/群管/超管）。对应 Go ``ReminderRemove``。"""
    require_group_services(agent, ctx)
    target = str(id or "").strip()
    if not target:
        raise InvalidError("请指定要删除的提醒 id")
    scope = _scope(agent, ctx)
    removed = False

    def _write() -> None:
        nonlocal removed
        entries = group_reminders_for_scope(agent, scope)
        kept: list[GroupReminderConfig] = []
        for entry in entries:
            if entry.id.strip().lower() != target.lower():
                kept.append(entry)
                continue
            if not can_manage_group_service_entry(agent, ctx, entry.created_by):
                raise InvalidError("只有提醒创建者、当前群管理员或超级管理员可以删除")
            removed = True
        if removed:
            set_group_reminders_for_scope(agent, scope, kept)

    with_group_services_write(agent, _write)
    if not removed:
        return "没有找到提醒 %r" % target
    return "已删除提醒 %s" % target


def can_manage_group_service_entry(agent: Any, ctx: Any, created_by: str) -> bool:
    """条目管理权：创建者本人 / 群管 / 超管。对应 Go ``canManageGroupServiceEntry``。"""
    actor = _actor(agent, ctx)
    if getattr(actor, "role", "") == RoleSuperadmin:
        return True
    if getattr(actor, "group_role", "") in (GroupRoleOwner, GroupRoleAdmin):
        return True
    owner = str(created_by or "").strip()
    return bool(owner) and owner == getattr(actor, "id", "")


# ---------------------------------------------------------------------------
# 派发
# ---------------------------------------------------------------------------
def start_group_services(agent: Any, ctx: Any = None) -> Any:
    """启动提醒派发循环。对应 Go ``StartGroupServices``。

    返回 :class:`asyncio.Task`（没有运行中的事件循环时返回 ``None``）；重复调用
    是幂等的，返回同一个 task。
    """
    host = service_host(agent)
    if host is None or not group_services_config(host).is_enabled():
        return None
    if _started(host):
        return getattr(host, "group_services_task", None)
    _set_started(host)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_run_group_services(host, ctx))
    _safe_set(host, "group_services_task", task)
    return task


async def _run_group_services(agent: Any, ctx: Any = None) -> None:
    await dispatch_due_reminders(agent, ctx)
    while True:
        await asyncio.sleep(GROUP_SERVICE_TICK_SECONDS)
        await dispatch_due_reminders(agent, ctx)


async def dispatch_due_reminders(
    agent: Any, ctx: Any = None, now: datetime | None = None
) -> None:
    """派发所有到点的提醒（异步实现）。对应 Go ``processDueReminders``。"""
    host = service_host(agent)
    if host is None or not group_services_config(host).is_enabled():
        return
    moment = now or datetime.now().astimezone()
    state = _state(host)
    with state.write_lock:
        snapshot = group_services_snapshot(host)
        changed = False
        for key, entries in list(snapshot.reminders.items()):
            platform, _, scope_id = str(key).partition(":")
            if not platform.strip() or not scope_id.strip():
                continue
            scope = _scope_from(platform, scope_id)
            updated = [entry.copy() for entry in entries]
            for index, entry in enumerate(updated):
                if entry.status not in ("", REMINDER_STATUS_PENDING):
                    continue
                due_at, ok = parse_group_service_time(entry.due_at)
                if not ok or due_at is None or due_at > moment:
                    continue
                if group_runtime_blocked_scope(host, scope):
                    state_name = normalize_group_runtime_state(
                        group_runtime_for_scope(host, scope).state
                    )
                    if state_name in (GROUP_RUNTIME_REMOVED, GROUP_RUNTIME_UNAVAILABLE):
                        updated[index].status = REMINDER_STATUS_SKIPPED
                        changed = True
                        _audit(
                            host,
                            "group_reminder_skip",
                            platform=platform,
                            scope=scope_id,
                            reminder_id=entry.id,
                            reason=state_name,
                            result="skipped",
                        )
                    continue
                from atri_x.agent.group_policy import group_policy_for_scope

                if not group_policy_for_scope(host, scope).is_services_enabled():
                    continue
                body = str(entry.text or "").strip()
                if not body:
                    updated[index].status = REMINDER_STATUS_SKIPPED
                    changed = True
                    continue
                if entry.created_by_name:
                    body = "提醒：" + body + "（由 " + entry.created_by_name + " 创建）"
                else:
                    body = "提醒：" + body
                error = await _send_notice_async(
                    host,
                    platform=str(platform).strip(),
                    scope_id=str(scope_id).strip(),
                    body=body,
                )
                if error is not None:
                    _audit(
                        host,
                        "group_reminder_send_error",
                        platform=platform,
                        scope=scope_id,
                        reminder_id=entry.id,
                        error=str(error),
                        result="failed",
                    )
                    continue
                updated[index].status = REMINDER_STATUS_SENT
                changed = True
                _audit(
                    host,
                    "group_reminder_sent",
                    platform=platform,
                    scope=scope_id,
                    reminder_id=entry.id,
                    result="succeeded",
                )
            snapshot.reminders[key] = updated
        if changed:
            set_group_services_snapshot(host, snapshot)
            saver = getattr(host, "save_runtime_state", None)
            if saver is not None:
                try:
                    saver()
                except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：只记审计
                    _audit(
                        host,
                        "group_services_write_error",
                        kind="reminder_dispatch",
                        error=str(exc),
                        result="failed",
                    )


def process_due_reminders(agent: Any, ctx: Any = None, now: datetime | None = None) -> None:
    """同步入口：把 :func:`dispatch_due_reminders` 驱动到完成。

    Go 里这是同步阻塞的；Python 侧发送必须 ``await``，因此有运行中的事件循环时
    排成 task（返回后派发仍在进行），没有循环时用 ``asyncio.run`` 跑完。
    """
    loop = _running_loop()
    if loop is not None:
        loop.create_task(dispatch_due_reminders(agent, ctx, now))
        return
    asyncio.run(dispatch_due_reminders(agent, ctx, now))


class GroupServicesMixin:
    """Go ``*Agent`` 的提醒方法组。"""

    async def reminder_create(self, ctx: Any, when_text: str, text: str) -> str:
        return reminder_create(self, ctx, when_text, text)

    async def reminder_list(self, ctx: Any) -> str:
        return reminder_list(self, ctx)

    async def reminder_remove(self, ctx: Any, id: str) -> str:
        return reminder_remove(self, ctx, id)

    def start_group_services(self, ctx: Any = None) -> Any:
        return start_group_services(self, ctx)

    def process_due_reminders(self, ctx: Any = None, now: datetime | None = None) -> None:
        process_due_reminders(self, ctx, now)

    def require_group_services(self, ctx: Any) -> None:
        require_group_services(self, ctx)

    def can_manage_group_service_entry(self, ctx: Any, created_by: str) -> bool:
        return can_manage_group_service_entry(self, ctx, created_by)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)


def _scope_from(platform: str, scope_id: str) -> Any:
    from atri_x.session import Scope

    return Scope(platform=platform, platform_scope_id=scope_id)


def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import actor

    return actor(agent, ctx)


def _state(agent: Any) -> Any:
    from atri_x.agent.group_services_state import group_services_state

    return group_services_state(agent)


def _started(agent: Any) -> bool:
    return bool(getattr(agent, "group_services_started", False))


def _set_started(agent: Any) -> None:
    _safe_set(agent, "group_services_started", True)


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


def _running_loop() -> Any:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _send_notice_async(
    agent: Any, *, platform: str, scope_id: str, body: str
) -> BaseException | None:
    """发一条通知；失败返回异常（与 Go 的 ``(Receipt, error)`` 同义）。"""
    sender = getattr(agent, "send_notice", None)
    if sender is None:
        return None
    notice = Notice(target=Target(platform=platform, scope_id=scope_id), outputs=[text(body)])
    try:
        result = sender(None, notice)
        if hasattr(result, "__await__"):
            await result
        return None
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：记审计后继续下一条
        return exc


def _audit(agent: Any, event: str, **attributes: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in attributes.items():
        args.append(key)
        args.append(value)
    audit(*args)


#: 创建提醒时的最小提前量文案（供命令层复用）。
MIN_REMINDER_DELAY_TEXT = group_service_duration_text(MIN_REMINDER_DELAY)

#: 群服务作用域键的别名（``platform:scope``，与 ``context_overflow_key`` 同值）。
group_services_scope_key = context_overflow_key
