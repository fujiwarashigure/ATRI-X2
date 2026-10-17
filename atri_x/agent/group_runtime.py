"""群运行态状态机：禁言 / 退群 / 不可用。

对应 Go：``internal/agent/group_runtime.go``。

职责：``groupRuntime*`` 常量、``cloneGroupRuntime`` / ``normalizeGroupRuntimeState``、
``withGroupRuntimeNoticeBypass`` / ``groupRuntimeNoticeBypass`` /
``groupRuntimeBlockedState`` / 快照读写 / ``transitionGroupRuntimeForScope`` /
``groupRuntimeBlocked`` / ``applyGroupRuntimeState`` / ``cancelScopeWork`` /
``notifyGroupRuntimeChange`` / ``noticeTargetBlocked`` / ``isGroupScopeID`` /
``updateGroupRuntimeFromEvent``。

另外本模块承载"运行态分区"的宿主钩子
（:meth:`RuntimeSectionsMixin.apply_extra_runtime_sections` /
``publish_extra_runtime_sections``）——它们是 agent-model 的 ``model_state.py``
为宿主预留的扩展点，负责群策略 / 群知识 / 群服务 / 群运行态这四段。

与 Go 的差异（形式，不是简化）：

* ``ctx.Value`` 的绕过标记 → :func:`with_group_runtime_notice_bypass` 用
  ``atri_x.delivery.ctx_with_value`` 挂同一个键（与 Go 的私有 key 类型同义）。
* ``sync.RWMutex`` → 宿主上的 ``group_runtime_mu``（:class:`threading.Lock`）。
* Go 的 ``context.Background()`` → ``None`` 传递（下游助手都接受 ``ctx=None``）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.config import GroupRuntimeConfig, StateConfig
from atri_x.delivery import Notice, Target, ctx_value, ctx_with_value, text
from atri_x.session import Scope

from atri_x.agent.group_policy import context_overflow_key, request_scope_key_from_scope
from atri_x.agent.tools import service_host

#: 通知级别（对应 Go ``slog.LevelWarn``；与 ``context_policy.WARN_LEVEL`` 同值）。
WARN_LEVEL = "warning"

__all__ = [
    "GROUP_RUNTIME_ACTIVE",
    "GROUP_RUNTIME_MUTED",
    "GROUP_RUNTIME_REMOVED",
    "GROUP_RUNTIME_UNAVAILABLE",
    "GroupRuntimeMixin",
    "GroupRuntimeState",
    "RuntimeSectionsMixin",
    "apply_group_runtime_state",
    "clone_group_runtime",
    "group_runtime_blocked",
    "group_runtime_blocked_scope",
    "group_runtime_blocked_state",
    "group_runtime_for_scope",
    "group_runtime_notice_bypass",
    "group_runtime_snapshot",
    "group_runtime_state",
    "is_group_scope_id",
    "normalize_group_runtime_state",
    "notice_target_blocked",
    "notify_group_runtime_change",
    "transition_group_runtime_for_scope",
    "update_group_runtime_from_event",
    "with_group_runtime_notice_bypass",
]

GROUP_RUNTIME_ACTIVE = "active"
GROUP_RUNTIME_MUTED = "muted"
GROUP_RUNTIME_REMOVED = "removed"
GROUP_RUNTIME_UNAVAILABLE = "unavailable"

#: 通知绕过群运行态闸门的 ctx 键（Go 的 ``groupRuntimeNoticeBypassKey{}``）。
NOTICE_BYPASS_KEY = "atri_x.agent.group_runtime.notice_bypass"


@dataclass
class GroupRuntimeState:
    """群运行态视图。对应 Go ``Agent.groupRuntime`` + ``groupRuntimeMu``。"""

    states: dict[str, GroupRuntimeConfig] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def group_runtime_state(agent: Any) -> GroupRuntimeState:
    """取宿主上的群运行态视图。"""
    host = service_host(agent)
    states = getattr(host, "group_runtime", None)
    if not isinstance(states, dict):
        states = {}
        _safe_set(host, "group_runtime", states)
    lock = getattr(host, "group_runtime_mu", None)
    if lock is None:
        lock = threading.Lock()
        _safe_set(host, "group_runtime_mu", lock)
    return GroupRuntimeState(states=states, lock=lock)


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


# ---------------------------------------------------------------------------
# 归一化与快照
# ---------------------------------------------------------------------------
def clone_group_runtime(
    input_value: dict[str, GroupRuntimeConfig] | None,
) -> dict[str, GroupRuntimeConfig]:
    """深拷贝并丢掉空状态。对应 Go ``cloneGroupRuntime``。"""
    if not input_value:
        return {}
    out: dict[str, GroupRuntimeConfig] = {}
    for key, value in input_value.items():
        key = str(key or "").strip()
        if not key:
            continue
        copied = value.copy()
        copied.state = normalize_group_runtime_state(copied.state)
        if not copied.state:
            continue
        out[key] = copied
    return out


def normalize_group_runtime_state(state: str) -> str:
    """状态名归一。对应 Go ``normalizeGroupRuntimeState``。"""
    text = str(state or "").strip().lower()
    if text in (
        GROUP_RUNTIME_ACTIVE,
        GROUP_RUNTIME_MUTED,
        GROUP_RUNTIME_REMOVED,
        GROUP_RUNTIME_UNAVAILABLE,
    ):
        return text
    return ""


def group_runtime_blocked_state(state: str) -> bool:
    """该状态是否禁止新请求与出站。对应 Go ``groupRuntimeBlockedState``。"""
    return normalize_group_runtime_state(state) in (
        GROUP_RUNTIME_MUTED,
        GROUP_RUNTIME_REMOVED,
        GROUP_RUNTIME_UNAVAILABLE,
    )


def set_group_runtime_snapshot(
    agent: Any, snapshot: dict[str, GroupRuntimeConfig] | None
) -> None:
    """整表替换。对应 Go ``setGroupRuntimeSnapshot``。"""
    state = group_runtime_state(agent)
    with state.lock:
        state.states.clear()
        state.states.update(clone_group_runtime(snapshot))


def group_runtime_snapshot(agent: Any) -> dict[str, GroupRuntimeConfig]:
    """整表副本。对应 Go ``groupRuntimeSnapshot``。"""
    state = group_runtime_state(agent)
    with state.lock:
        return clone_group_runtime(state.states)


def group_runtime_for_scope(agent: Any, scope: Any) -> GroupRuntimeConfig:
    """单作用域运行态（零值 = 正常）。对应 Go ``groupRuntimeForScope``。"""
    key = context_overflow_key(scope)
    state = group_runtime_state(agent)
    with state.lock:
        found = state.states.get(key)
    return found.copy() if found is not None else GroupRuntimeConfig()


def transition_group_runtime_for_scope(
    agent: Any, scope: Any, value: GroupRuntimeConfig
) -> tuple[str, bool]:
    """原子地应用一次状态迁移，返回 ``(旧状态, 是否变化)``。

    对应 Go ``transitionGroupRuntimeForScope``：比较与交换在同一把写锁内，两个
    同时到达的禁言事件不会都发出迁移通知。
    """
    key = context_overflow_key(scope)
    target = value.copy()
    target.state = normalize_group_runtime_state(target.state)
    state = group_runtime_state(agent)
    with state.lock:
        previous = normalize_group_runtime_state(
            (state.states.get(key) or GroupRuntimeConfig()).state
        )
        if previous == target.state:
            return previous, False
        if target.state in ("", GROUP_RUNTIME_ACTIVE):
            state.states.pop(key, None)
        else:
            state.states[key] = target
        return previous, True


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------
def with_group_runtime_notice_bypass(ctx: Any) -> Any:
    """打上"这条通知绕过运行态闸门"的标记。对应 Go ``withGroupRuntimeNoticeBypass``。"""
    return ctx_with_value(ctx, NOTICE_BYPASS_KEY, True)


def group_runtime_notice_bypass(ctx: Any) -> bool:
    """对应 Go ``groupRuntimeNoticeBypass``。"""
    return bool(ctx_value(ctx, NOTICE_BYPASS_KEY, False))


def group_runtime_blocked(agent: Any, ctx: Any) -> bool:
    """当前群作用域是否已暂停（新付费调用与出站消息都要停）。对应 Go 同名方法。"""
    if agent is None:
        return False
    if ctx is not None:
        from atri_x.platform import message_context_from

        msg, ok = message_context_from(ctx)
        if ok:
            return group_runtime_blocked_scope(
                agent, Scope(platform=msg.platform, platform_scope_id=msg.scope_id)
            )
    return False


def group_runtime_blocked_scope(agent: Any, scope: Any) -> bool:
    """按 scope 判定（私聊永不阻塞）。对应 Go ``groupRuntimeBlockedScope``。"""
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip()
    if not is_group_scope_id(scope_id):
        return False
    return group_runtime_blocked_state(group_runtime_for_scope(agent, scope).state)


def is_group_scope_id(scope_id: str) -> bool:
    """群作用域 ID 判定。对应 Go ``isGroupScopeID``（与 ``tools.is_group_scope_id`` 同义）。"""
    text = str(scope_id or "").strip()
    return text.startswith("group:") or text.startswith("supergroup:")


def notice_target_blocked(agent: Any, target: Any) -> bool:
    """显式通知目标是否指向被暂停的群。对应 Go ``noticeTargetBlocked``。"""
    if agent is None:
        return False
    scope_id = str(getattr(target, "scope_id", "") or "").strip()
    if not scope_id:
        group_id = str(getattr(target, "group_id", "") or "").strip()
        if group_id:
            scope_id = "group:" + group_id
    if not is_group_scope_id(scope_id):
        return False
    return group_runtime_blocked_scope(
        agent,
        Scope(
            platform=str(getattr(target, "platform", "") or "").strip(),
            platform_scope_id=scope_id,
        ),
    )


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------
def apply_group_runtime_state(
    agent: Any, scope: Any, state: str, reason: str
) -> bool:
    """记录一次迁移、取消在途工作、发一条超管通知。对应 Go ``applyGroupRuntimeState``。

    重复的同状态事件是幂等的，不会重复通知。
    """
    normalized = normalize_group_runtime_state(state)
    if not normalized:
        return False
    _, changed = transition_group_runtime_for_scope(
        agent,
        scope,
        GroupRuntimeConfig(
            state=normalized,
            reason=str(reason or "").strip(),
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    if not changed:
        return False
    if group_runtime_blocked_state(normalized):
        cancel_scope_work(agent, scope, normalized, reason)
    notify_group_runtime_change(agent, scope, normalized, reason)
    _save_runtime_state(agent)
    _audit(
        agent,
        "group_runtime_state",
        platform=getattr(scope, "platform", ""),
        scope=getattr(scope, "platform_scope_id", ""),
        state=normalized,
        reason=str(reason or "").strip(),
        result="succeeded",
    )
    return True


def cancel_scope_work(agent: Any, scope: Any, state: str, reason: str) -> None:
    """取消该 scope 在途请求与排队输入。对应 Go ``cancelScopeWork``。"""
    if agent is None:
        return
    requests = getattr(agent, "requests", None)
    if requests is None:
        return
    scope_key = request_scope_key_from_scope(scope)
    marker = getattr(agent, "mark_turn_canceled", None)
    for request_id in requests.scope_ids(scope_key):
        if marker is not None:
            marker(request_id)
    count = requests.cancel_scope(scope_key)
    cancel_inbox = getattr(agent, "cancel_inbox_scope", None)
    if cancel_inbox is not None:
        count += cancel_inbox(scope_key)
    _audit(
        agent,
        "group_runtime_cancel",
        platform=getattr(scope, "platform", ""),
        scope=getattr(scope, "platform_scope_id", ""),
        state=state,
        reason=str(reason or "").strip(),
        count=count,
        result="canceled",
    )


def notify_group_runtime_change(
    agent: Any, scope: Any, state: str, reason: str
) -> None:
    """给超管发一条聚合通知。对应 Go ``notifyGroupRuntimeChange``。"""
    if agent is None:
        return
    platform_name = str(getattr(scope, "platform", "") or "").strip()
    if not platform_name:
        platform = getattr(agent, "platform", None)
        if platform is not None:
            platform_name = platform.name()
    if not platform_name:
        return
    text_value = "群 %s 运行状态变为 %s（%s）。已暂停该群新请求；在途输出已取消。" % (
        str(getattr(scope, "platform_scope_id", "") or "").strip(),
        state,
        str(reason or "").strip(),
    )
    sender = getattr(agent, "send_notice", None)
    if sender is None:
        return
    notice = Notice(
        target=Target(
            platform=platform_name,
            scope_id=getattr(scope, "platform_scope_id", ""),
            superadmins=True,
        ),
        outputs=[text(text_value)],
        level=WARN_LEVEL,
    )
    try:
        result = sender(with_group_runtime_notice_bypass(None), notice)
        if hasattr(result, "__await__"):
            result.close()
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：只记警告
        _warn(agent, "notify group runtime state failed", exc)


def update_group_runtime_from_event(agent: Any, event: Any) -> bool:
    """把机器人自身被禁言/被移出群的事件映射成运行态。对应 Go ``同名``。

    返回 ``True`` 表示该事件已被消费。识别的 meta 键与 Go 逐字一致。
    """
    if agent is None:
        return False
    scope_id = str(getattr(event, "scope_id", "") or "").strip()
    if not is_group_scope_id(scope_id):
        return False
    meta = getattr(event, "meta", None) or {}
    self_id = _meta_text(meta.get("qq_onebot.self_id"))
    if not self_id:
        return False
    notice_type = _meta_text(meta.get("qq_onebot.notice_type")).lower() or str(
        getattr(event, "type", "") or ""
    ).lower()
    sub_type = _meta_text(meta.get("qq_onebot.sub_type")).lower()
    target_id = _meta_text(meta.get("qq_onebot.target_id"))
    user_id = str(getattr(event, "user_id", "") or "")
    if user_id != self_id and target_id != self_id:
        return False
    scope = Scope(
        platform=str(getattr(event, "platform", "") or "").strip(),
        platform_scope_id=scope_id,
    )
    if "ban" in notice_type:
        if sub_type == "lift_ban":
            return apply_group_runtime_state(agent, scope, GROUP_RUNTIME_ACTIVE, "self_unmuted")
        return apply_group_runtime_state(agent, scope, GROUP_RUNTIME_MUTED, "self_muted")
    if "decrease" in notice_type:
        return apply_group_runtime_state(agent, scope, GROUP_RUNTIME_REMOVED, "self_removed")
    if "increase" in notice_type:
        return apply_group_runtime_state(agent, scope, GROUP_RUNTIME_ACTIVE, "self_joined")
    return False


class GroupRuntimeMixin:
    """Go ``*Agent`` 的群运行态方法组。"""

    def set_group_runtime_snapshot(
        self, snapshot: dict[str, GroupRuntimeConfig] | None
    ) -> None:
        set_group_runtime_snapshot(self, snapshot)

    def group_runtime_snapshot(self) -> dict[str, GroupRuntimeConfig]:
        return group_runtime_snapshot(self)

    def group_runtime_for_scope(self, scope: Any) -> GroupRuntimeConfig:
        return group_runtime_for_scope(self, scope)

    def group_runtime_blocked(self, ctx: Any) -> bool:
        return group_runtime_blocked(self, ctx)

    def group_runtime_blocked_scope(self, scope: Any) -> bool:
        return group_runtime_blocked_scope(self, scope)

    def group_runtime_notice_bypass(self, ctx: Any) -> bool:
        return group_runtime_notice_bypass(ctx)

    def notice_target_blocked(self, target: Any) -> bool:
        return notice_target_blocked(self, target)

    def apply_group_runtime_state(self, scope: Any, state: str, reason: str) -> bool:
        return apply_group_runtime_state(self, scope, state, reason)

    def update_group_runtime_from_event(self, event: Any) -> bool:
        return update_group_runtime_from_event(self, event)


class RuntimeSectionsMixin:
    """``state.toml`` 里群策略 / 群知识 / 群服务 / 群运行态四段的搬运。

    对应 Go ``applyRuntimeState`` / ``saveRuntimeState`` 里属于本组的那几行；
    agent-model 的 ``RuntimeStateMixin`` 把这两处留成了宿主钩子。
    """

    def apply_extra_runtime_sections(self, state: StateConfig) -> None:
        """把 ``state`` 的四个分区合并进内存。"""
        from atri_x.agent.group_knowledge import (
            normalize_group_knowledge_snapshot,
            set_group_knowledge_snapshot,
        )
        from atri_x.agent.group_policy import set_group_policy_snapshot
        from atri_x.agent.group_services_state import (
            normalize_group_services_snapshot,
            set_group_services_snapshot,
        )

        set_group_policy_snapshot(self, state.group_policy)
        set_group_knowledge_snapshot(
            self,
            normalize_group_knowledge_snapshot(
                state.group_knowledge, _knowledge_config(self)
            ),
        )
        set_group_services_snapshot(
            self,
            normalize_group_services_snapshot(state.group_services, _services_config(self)),
        )
        set_group_runtime_snapshot(self, state.group_runtime)

    def publish_extra_runtime_sections(self, state: StateConfig) -> None:
        """把内存里的四个分区写进 ``state``。"""
        from atri_x.agent.group_knowledge import group_knowledge_snapshot
        from atri_x.agent.group_policy import group_policy_snapshot
        from atri_x.agent.group_services_state import group_services_snapshot

        state.group_policy = group_policy_snapshot(self)
        state.group_knowledge = group_knowledge_snapshot(self)
        state.group_services = group_services_snapshot(self)
        state.group_runtime = group_runtime_snapshot(self)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _meta_text(value: Any) -> str:
    if value is None:
        return ""
    text_value = str(value).strip()
    return "" if text_value in ("", "<nil>", "None") else text_value


def _audit(agent: Any, event: str, **attributes: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in attributes.items():
        args.append(key)
        args.append(value)
    audit(*args)


def _warn(agent: Any, message: str, exc: BaseException) -> None:
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    warn = getattr(logger, "warn", None)
    if warn is not None:
        warn(message, error=str(exc))


def _save_runtime_state(agent: Any) -> None:
    helper = getattr(agent, "save_runtime_state", None)
    if helper is None:
        return
    try:
        helper()
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：只记警告
        _warn(agent, "persist group runtime state failed", exc)


def _knowledge_config(agent: Any) -> Any:
    from atri_x.agent.group_knowledge import _knowledge_config as reader  # noqa: PLC2701

    return reader(agent)


def _services_config(agent: Any) -> Any:
    from atri_x.agent.group_services_state import group_services_config

    return group_services_config(agent)
