"""群级策略：状态快照、视图、模型目录与授权闸门。

对应 Go：``internal/agent/group_policy.go``（字段规则部分见
:mod:`atri_x.agent.group_policy_rules`）。

职责：``cloneGroupPolicy`` / ``setGroupPolicySnapshot`` / ``groupPolicySnapshot`` /
``groupPolicyForScope`` / ``setGroupPolicyForScope`` / ``isGroupScope`` /
``canManageCurrentGroupPolicy`` / ``GroupPolicyStatus`` / ``rawGroupPolicy`` /
``groupMergeWindow`` / ``SetGroupPolicy`` / ``ResetGroupPolicy`` /
``groupQuietHoursActive`` / ``requestFairKey`` / ``requestScopeKey`` /
``resolveGroupModelSelection`` / ``groupModelSelectionAllowed`` /
``groupDefaultModelSelection`` / ``GroupLearningEnabled`` /
``AuthorizeLearningAction`` / ``groupDefaultSessionMode`` /
``groupAdminCommandGrant`` / ``GroupHistoryEnabledForScope`` / ``historyEnabled`` /
``learningEnabledForScope``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``sync.RWMutex`` → 每次访问 :class:`GroupPolicyState` 的
  :class:`threading.Lock`（快照读写是纯内存 + 文件落盘路径，与 Go 相同是同步的）。
* ``SetGroupPolicy`` / ``ResetGroupPolicy`` 按 ``commands/deps.GroupPolicyService``
  的契约做成 ``async``（落盘走 ``save_runtime_state``，可能与外部编辑合并）。
* ``contextOverflowKey`` 是 agent-model 的 ``context_policy.go``：这里优先调用
  ``atri_x.agent.context_policy.context_overflow_key``，未落地时用本模块的等价实现
  （键格式 ``platform`` 或 ``platform:scope_id``，空平台按 ``unknown``）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atri_x.config import GroupPolicyConfig, ModelSelection
from atri_x.errors import InvalidError
from atri_x.security import GroupRoleAdmin, GroupRoleOwner, RoleSuperadmin
from atri_x.session import Scope

from atri_x.agent.group_policy_rules import (
    SUPERADMIN_ONLY_FIELDS,
    apply_policy_field,
    canonical_group_policy_field,
    display_string_list,
    enabled_text,
    group_policy_field_audit_value,
    reset_policy_field,
)
from atri_x.agent.tools import first_non_empty, service_host  # noqa: F401  (再导出)

__all__ = [
    "GroupPolicyMixin",
    "GroupPolicyState",
    "authorize_learning_action",
    "budget_text",
    "can_manage_current_group_policy",
    "clone_group_policy",
    "context_overflow_key",
    "cost_budget_text",
    "group_admin_command_grant",
    "group_default_model_selection",
    "group_default_session_mode",
    "group_history_enabled_for_scope",
    "group_learning_enabled",
    "group_merge_window",
    "group_model_selection_allowed",
    "group_policy_for_scope",
    "group_policy_snapshot",
    "group_policy_state",
    "group_quiet_hours_active",
    "group_tool_allowlist_text",
    "history_enabled",
    "int64_budget_text",
    "is_group_scope",
    "learning_enabled_for_scope",
    "merge_window_text",
    "raw_group_policy",
    "request_fair_key",
    "request_scope_key",
    "request_scope_key_from_scope",
    "reset_group_policy",
    "resolve_group_model_selection",
    "set_group_policy",
    "set_group_policy_for_scope",
    "set_group_policy_snapshot",
]


@dataclass
class GroupPolicyState:
    """群策略快照视图。对应 Go ``Agent.groupPolicy`` + ``groupPolicyMu``。

    ``policies`` / ``lock`` 直接指向宿主上的 ``agent.group_policy`` 与
    ``agent.group_policy_mu``（前者 agent-core 已按 Go 字段名建好），因此不会
    出现第二份互不相认的策略表。
    """

    policies: dict[str, GroupPolicyConfig] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATE_ATTR = "_group_policy_state"


def group_policy_state(agent: Any) -> GroupPolicyState:
    """取宿主上的群策略状态视图。"""
    host = service_host(agent)
    policies = getattr(host, "group_policy", None)
    if not isinstance(policies, dict):
        policies = {}
        _safe_set(host, "group_policy", policies)
    lock = getattr(host, "group_policy_mu", None)
    if lock is None:
        lock = threading.Lock()
        _safe_set(host, "group_policy_mu", lock)
    return GroupPolicyState(policies=policies, lock=lock)


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


def context_overflow_key(scope: Any) -> str:
    """群/会话分片键。对应 Go ``contextOverflowKey``（``context_policy.go``）。"""
    try:  # agent-model 的模块可能尚未落地 —— 延迟导入
        from atri_x.agent.context_policy import context_overflow_key as shared

        return shared(scope)
    except Exception:  # noqa: BLE001
        pass
    platform = str(getattr(scope, "platform", "") or "").strip() or "unknown"
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip()
    if not scope_id:
        return platform
    return platform + ":" + scope_id


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def clone_group_policy(
    input_value: dict[str, GroupPolicyConfig] | None,
) -> dict[str, GroupPolicyConfig]:
    """深拷贝并对每条策略归一化。对应 Go ``cloneGroupPolicy``。"""
    if not input_value:
        return {}
    return {key: value.normalize() for key, value in input_value.items()}


def set_group_policy_snapshot(
    agent: Any, snapshot: dict[str, GroupPolicyConfig] | None
) -> None:
    """整表替换（**就地**改宿主上的字典）。对应 Go ``setGroupPolicySnapshot``。"""
    state = group_policy_state(agent)
    with state.lock:
        state.policies.clear()
        state.policies.update(clone_group_policy(snapshot))


def group_policy_snapshot(agent: Any) -> dict[str, GroupPolicyConfig]:
    """整表副本。对应 Go ``groupPolicySnapshot``。"""
    state = group_policy_state(agent)
    with state.lock:
        return clone_group_policy(state.policies)


def _normalized_map(agent: Any) -> dict[str, GroupPolicyConfig]:
    return clone_group_policy(group_policy_state(agent).policies)


def group_policy_for_scope(agent: Any, scope: Any) -> GroupPolicyConfig:
    """单个作用域的生效策略。对应 Go ``groupPolicyForScope``。"""
    key = context_overflow_key(scope)
    state = group_policy_state(agent)
    with state.lock:
        policy = state.policies.get(key)
    if policy is None:
        return GroupPolicyConfig().normalize()
    return policy.normalize()


def set_group_policy_for_scope(
    agent: Any, scope: Any, value: GroupPolicyConfig
) -> None:
    """写回单个作用域（零值即删除条目）。对应 Go ``setGroupPolicyForScope``。"""
    key = context_overflow_key(scope)
    value = value.normalize()
    state = group_policy_state(agent)
    with state.lock:
        if value.is_zero():
            state.policies.pop(key, None)
            return
        state.policies[key] = value


def raw_group_policy(agent: Any, scope: Any) -> GroupPolicyConfig:
    """未归一化的原始策略。对应 Go ``rawGroupPolicy``。"""
    if agent is None:
        return GroupPolicyConfig()
    key = context_overflow_key(scope)
    state = group_policy_state(agent)
    with state.lock:
        return state.policies.get(key) or GroupPolicyConfig()


# ---------------------------------------------------------------------------
# 作用域 / 权限
# ---------------------------------------------------------------------------
def is_group_scope(agent: Any, ctx: Any) -> bool:
    """当前是否群聊作用域。对应 Go ``isGroupScope``。"""
    if agent is None or getattr(agent, "platform", None) is None:
        return False
    scope = _scope(agent, ctx)
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip()
    return scope_id.startswith("group:") or scope_id.startswith("supergroup:")


def can_manage_current_group_policy(agent: Any, ctx: Any) -> bool:
    """当前发言人是否有权改群策略。对应 Go ``canManageCurrentGroupPolicy``。"""
    if agent is None or not is_group_scope(agent, ctx):
        return False
    actor = _actor(agent, ctx)
    if getattr(actor, "role", "") == RoleSuperadmin:
        return True
    return getattr(actor, "group_role", "") in (GroupRoleOwner, GroupRoleAdmin)


def group_quiet_hours_active(agent: Any, ctx: Any, now: datetime | None = None) -> bool:
    """当前是否处于静默时段。对应 Go ``groupQuietHoursActive``。"""
    from atri_x.agent.group_policy_rules import quiet_hours_active

    if not is_group_scope(agent, ctx):
        return False
    policy = group_policy_for_scope(agent, _scope(agent, ctx))
    if not str(policy.quiet_hours or "").strip():
        return False
    return quiet_hours_active(policy.quiet_hours, now or datetime.now().astimezone())


def group_merge_window(agent: Any, ctx: Any) -> float:
    """连续消息合并窗口（秒）。对应 Go ``groupMergeWindow``。"""
    if agent is None:
        return 0.0
    policy = raw_group_policy(agent, _base_scope(agent, ctx))
    ms = int(policy.merge_window_ms or 0)
    if ms <= 0:
        return 0.0
    if ms > 10000:
        ms = 10000
    return ms / 1000.0


def merge_window_text(ms: int) -> str:
    """对应 Go ``mergeWindowText``。"""
    if ms <= 0:
        return "关闭"
    return "%d ms" % ms


def request_fair_key(agent: Any, ctx: Any) -> str:
    """按用户公平排队的键。对应 Go ``requestFairKey``。"""
    scope = _scope(agent, ctx)
    platform = str(getattr(scope, "platform", "") or "").strip() or "unknown"
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip() or "unknown"
    actor_id = str(getattr(scope, "actor_id", "") or "").strip() or "unknown"
    return platform + ":" + scope_id + ":" + actor_id


def request_scope_key(agent: Any, ctx: Any) -> str:
    """按群/私聊分桶的键（**不含** actor）。对应 Go ``requestScopeKey``。"""
    return request_scope_key_from_scope(_scope(agent, ctx))


def request_scope_key_from_scope(scope: Any) -> str:
    """对应 Go ``requestScopeKeyFromScope``。"""
    platform = str(getattr(scope, "platform", "") or "").strip() or "unknown"
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip() or "unknown"
    return platform + ":" + scope_id


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------
def resolve_group_model_selection(agent: Any, raw: str) -> ModelSelection | None:
    """把群策略里的模型值解析成具体选择。对应 Go ``resolveGroupModelSelection``。

    只有 provider 客户端存在时才算解析成功；裸 ``provider/model`` 同样要求客户端存在。
    """
    if agent is None:
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    aliases = getattr(agent, "model_aliases", None) or {}
    profiles = getattr(agent, "model_profiles", None) or {}
    name = aliases.get(text.lower())
    if name:
        selection = profiles.get(name)
        if selection is not None and _client_exists(agent, selection.provider):
            return selection
    selection = profiles.get(text)
    if selection is not None and _client_exists(agent, selection.provider):
        return selection
    if "/" not in text:
        return None
    provider, _, model = text.partition("/")
    provider = provider.strip()
    model = model.strip()
    if not provider or not model or not _client_exists(agent, provider):
        return None
    return ModelSelection(provider=provider, model=model)


def group_model_selection_allowed(
    agent: Any, policy: GroupPolicyConfig, selection: Any, raw: str
) -> bool:
    """群可选模型目录闸门。对应 Go ``groupModelSelectionAllowed``。

    目录为空时只允许"已配置的别名/profile"：群管不能自选任意昂贵 provider/model。
    """
    catalog = policy.normalize().allowed_models
    text = str(raw or "").strip()
    aliases = getattr(agent, "model_aliases", None) or {}
    profiles = getattr(agent, "model_profiles", None) or {}
    if not catalog:
        if text.lower() in aliases:
            return True
        return text in profiles
    for item in catalog:
        item = str(item or "").strip()
        if not item:
            continue
        if item == "*":
            return True
        if item.lower() == text.lower():
            return True
        candidate = resolve_group_model_selection(agent, item)
        if candidate is not None and candidate == selection:
            return True
        if selection is not None and item.lower() == (
            "%s/%s" % (selection.provider, selection.model)
        ).lower():
            return True
    return False


def group_default_model_selection(agent: Any, ctx: Any) -> ModelSelection | None:
    """本群默认模型（须同时通过目录闸门）。对应 Go ``groupDefaultModelSelection``。"""
    if agent is None or not is_group_scope(agent, ctx):
        return None
    policy = group_policy_for_scope(agent, _scope(agent, ctx))
    value = str(policy.default_model or "").strip()
    if not value:
        return None
    selection = resolve_group_model_selection(agent, value)
    if selection is None:
        return None
    if not group_model_selection_allowed(agent, policy, selection, value):
        return None
    return selection


def group_default_session_mode(agent: Any, ctx: Any) -> str:
    """本群默认会话模式。对应 Go ``groupDefaultSessionMode``。"""
    if agent is None or not is_group_scope(agent, ctx):
        return ""
    return group_policy_for_scope(agent, _scope(agent, ctx)).default_mode


# ---------------------------------------------------------------------------
# 功能开关
# ---------------------------------------------------------------------------
def group_learning_enabled(agent: Any, ctx: Any) -> bool:
    """本群是否允许学习层（非群作用域沿用全局）。对应 Go ``GroupLearningEnabled``。"""
    if agent is None or not is_group_scope(agent, ctx):
        return True
    return group_policy_for_scope(agent, _scope(agent, ctx)).is_learning_enabled()


def authorize_learning_action(agent: Any, ctx: Any, action: str) -> bool:
    """群管学习审核授权闸门。对应 Go ``AuthorizeLearningAction``。"""
    if agent is None or not is_group_scope(agent, ctx):
        return False
    actor = _actor(agent, ctx)
    if getattr(actor, "role", "") == RoleSuperadmin:
        return True
    if getattr(actor, "group_role", "") not in (GroupRoleOwner, GroupRoleAdmin):
        return False
    policy = group_policy_for_scope(agent, _scope(agent, ctx))
    if not policy.learning_moderation:
        return False
    wanted = str(action or "").strip().lower()
    return wanted in policy.learning_moderation_actions_value()


def group_admin_command_grant(agent: Any, ctx: Any, command_name: str) -> bool:
    """群管命令授权（当前只有 ``learning``）。对应 Go ``groupAdminCommandGrant``。"""
    if str(command_name or "").strip() != "learning":
        return False
    if agent is None or not is_group_scope(agent, ctx):
        return False
    actor = _actor(agent, ctx)
    if getattr(actor, "group_role", "") not in (GroupRoleOwner, GroupRoleAdmin):
        return False
    return bool(group_policy_for_scope(agent, _scope(agent, ctx)).learning_moderation)


def group_history_enabled_for_scope(agent: Any, platform: str, scope_id: str) -> bool:
    """按 (platform, scope_id) 查历史开关。对应 Go ``GroupHistoryEnabledForScope``。"""
    return _scope_flag_enabled(
        agent, platform, scope_id, GroupPolicyConfig.is_history_enabled
    )


def learning_enabled_for_scope(agent: Any, platform: str, scope_id: str) -> bool:
    """按 (platform, scope_id) 查学习开关。对应 Go ``learningEnabledForScope``。"""
    return _scope_flag_enabled(
        agent, platform, scope_id, GroupPolicyConfig.is_learning_enabled
    )


def history_enabled(agent: Any, ctx: Any) -> bool:
    """当前作用域是否允许写新的会话/历史行。对应 Go ``historyEnabled``。"""
    if agent is None or not is_group_scope(agent, ctx):
        return True
    return group_policy_for_scope(agent, _scope(agent, ctx)).is_history_enabled()


def _scope_flag_enabled(agent: Any, platform: str, scope_id: str, reader: Any) -> bool:
    if agent is None:
        return True
    platform = str(platform or "").strip()
    scope_id = str(scope_id or "").strip()
    if not platform or not scope_id:
        return False
    if not scope_id.startswith("group:") and not scope_id.startswith("supergroup:"):
        return True
    key = context_overflow_key(Scope(platform=platform, platform_scope_id=scope_id))
    state = group_policy_state(agent)
    with state.lock:
        policy = state.policies.get(key)
    if policy is None:
        return reader(GroupPolicyConfig())
    return bool(reader(policy))


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------
def group_policy_status(agent: Any, ctx: Any) -> str:
    """``/grouppolicy`` 的状态视图。对应 Go ``GroupPolicyStatus``。"""
    scope = _scope(agent, ctx)
    key = context_overflow_key(scope)
    if not is_group_scope(agent, ctx):
        return "群级策略只适用于群聊；当前不是群聊作用域。"
    policy = group_policy_for_scope(agent, scope)
    quiet = policy.quiet_hours or "未设置"
    lines = [
        "群级策略",
        "作用域：%s" % key,
        "唤醒词：%s" % display_string_list(policy.wake_keywords),
        "响应模式：%s（mention=默认，all=全部，keyword=仅唤醒词，reply=仅回复，off=关闭）"
        % policy.response_mode_value(),
        "会话线程：%s（per_user=每人独立，group=全群共享并串行）" % policy.thread_mode_value(),
        "连续消息合并窗口：%s" % merge_window_text(policy.merge_window_ms),
        "默认会话模式：%s" % (policy.default_mode or "继承全局"),
        "默认模型别名：%s" % (policy.default_model or "继承全局"),
        "群可选模型目录：%s"
        % (
            display_string_list(policy.allowed_models)
            if policy.allowed_models
            else "默认（仅已配置模型别名/profile）"
        ),
    ]
    if policy.tool_allowlist_restricted() and not policy.tool_allowlist:
        tool_text = "空（当前群禁止全部工具）"
    elif policy.tool_allowlist_restricted():
        tool_text = display_string_list(policy.tool_allowlist)
    else:
        tool_text = "继承全局"
    lines.append("工具白名单：%s" % tool_text)
    image_used, vision_used = _budget_usage(agent, ctx)
    asr_used = _asr_budget_usage(agent, ctx)
    lines.append("生图日额度：%s" % budget_text(policy.image_quota, image_used))
    lines.append("视觉日额度：%s" % budget_text(policy.vision_quota, vision_used))
    lines.append("语音转写日额度：%s" % budget_text(policy.asr_quota, asr_used))
    lines.append("单用户生图日额度：%s" % budget_text(policy.user_image_quota, 0))
    lines.append("单用户视觉日额度：%s" % budget_text(policy.user_vision_quota, 0))
    lines.append("单用户语音转写日额度：%s" % budget_text(policy.user_asr_quota, 0))
    chat_tokens, chat_costs = _chat_budget_usage(agent, ctx)
    lines.append("群日聊天 token 额度：%s" % int64_budget_text(policy.chat_tokens_quota, chat_tokens))
    lines.append("群日聊天费用额度：%s" % cost_budget_text(policy.chat_cost_quota, chat_costs))
    lines.append("静默时段：%s" % quiet)
    lines.append(
        "群分析：%s；学习：%s；历史记录：%s；知识库：%s；语音转写：%s；提醒/投票/报名：%s"
        % (
            enabled_text(policy.is_group_analysis_enabled()),
            enabled_text(policy.is_learning_enabled()),
            enabled_text(policy.is_history_enabled()),
            enabled_text(policy.is_knowledge_enabled()),
            enabled_text(policy.is_asr_enabled()),
            enabled_text(policy.is_services_enabled()),
        )
    )
    actions = policy.learning_moderation_actions_value() or ["无"]
    lines.append(
        "群管理员学习审核授权：%s（权限：%s）"
        % (enabled_text(policy.learning_moderation), "、".join(actions))
    )
    lines.append("修改：/grouppolicy <field> <value>；重置：/grouppolicy reset [field]")
    return "\n".join(lines) + "\n"


def group_tool_allowlist_text(agent: Any, ctx: Any, session: Any = None) -> str:
    """当前群工具白名单的视图（``/tools`` 用，也便于排障）。

    与 :func:`atri_x.agent.tool_auth.group_tool_allowlist` 同一套语义：
    ``继承全局`` = 不受群白名单限制；``空`` = 当前模式禁止全部工具。
    本仓扩展了 ``chat`` / ``work`` 各自的白名单：没有分模式配置时回退到通用列表，
    这里的文本会把三种情况都写清楚。
    """
    if agent is None or not is_group_scope(agent, ctx):
        return "非群聊，工具不受群白名单限制"
    policy = group_policy_for_scope(agent, _scope(agent, ctx))

    def describe(restricted: bool, names: list[str] | None, *, generic: bool) -> str:
        if not restricted:
            return "继承全局（不限制）" if generic else "继承通用"
        if not names:
            return "禁止全部"
        return f"仅白名单（{len(names)}）：{display_string_list(names)}"

    generic = describe(
        policy.tool_allowlist_restricted(),
        list(policy.tool_allowlist or []),
        generic=True,
    )
    parts: list[str] = []
    if session is not None:
        mode = str(getattr(session, "mode", "") or "").strip().lower()
        if mode in ("chat", "work"):
            restricted, names = policy.mode_tool_allowlist(mode)
            parts.append(f"当前模式 {mode}：{describe(restricted, names, generic=False)}")
    for mode in ("chat", "work"):
        configured = policy.mode_tool_allowlist_configured(mode)
        restricted, names = policy.mode_tool_allowlist(mode)
        if configured:
            parts.append(f"{mode}：{describe(restricted, names, generic=False)}")
        else:
            parts.append(f"{mode}：继承通用")
    parts.append(f"通用（/tools allow 写入的兜底）：{generic}")
    return "；".join(parts)


def budget_text(limit: int, used: int) -> str:
    """带上限的用量文本。对应 Go ``budgetText``。"""
    if limit <= 0:
        return "已用 %d，未设置上限" % used
    remaining = max(0, int(limit) - int(used))
    return "已用 %d / %d（剩余 %d）" % (used, limit, remaining)


def int64_budget_text(limit: int, used: int) -> str:
    """对应 Go ``int64BudgetText``。"""
    if limit <= 0:
        return "已用 %d，未设置上限" % used
    remaining = max(0, int(limit) - int(used))
    return "已用 %d / %d（剩余 %d）" % (used, limit, remaining)


def cost_budget_text(limit: float, used_micros: int) -> str:
    """费用额度文本（micros → 元）。对应 Go ``costBudgetText``。"""
    used = float(used_micros) / 1_000_000
    if limit <= 0:
        return "已用 %.6f，未设置上限" % used
    remaining = max(0.0, float(limit) - used)
    return "已用 %.6f / %.6f（剩余 %.6f）" % (used, limit, remaining)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
async def set_group_policy(agent: Any, ctx: Any, field: str, value: str) -> str:
    """改一个群策略字段。对应 Go ``SetGroupPolicy``（成功返回提示文本）。"""
    if not can_manage_current_group_policy(agent, ctx):
        if not is_group_scope(agent, ctx):
            raise InvalidError("群级策略只能由当前群的群主/管理员修改")
        raise InvalidError("需要当前群群主/管理员或机器人超级管理员权限")
    canonical = canonical_group_policy_field(field)
    if not canonical:
        raise InvalidError("未知的群策略字段")
    scope = _scope(agent, ctx)
    policy_key = context_overflow_key(scope)
    previous = group_policy_snapshot(agent)
    previous_policy = previous.get(policy_key) or GroupPolicyConfig()
    actor = _actor(agent, ctx)
    if canonical in SUPERADMIN_ONLY_FIELDS and getattr(actor, "role", "") != RoleSuperadmin:
        raise InvalidError(
            "%s 只能由机器人超级管理员配置；请让超级管理员在本群执行该命令" % canonical
        )
    policy = group_policy_for_scope(agent, scope)
    policy = apply_policy_field(agent, policy, canonical, value)
    policy = policy.normalize()
    set_group_policy_for_scope(agent, scope, policy)
    try:
        _save_runtime_state(agent)
    except BaseException:
        set_group_policy_snapshot(agent, previous)
        raise
    _audit(
        agent,
        "group_policy_update",
        actor_id=actor.id,
        scope=policy_key,
        field=canonical,
        old=group_policy_field_audit_value(previous_policy, canonical),
        new=group_policy_field_audit_value(policy, canonical),
        result="succeeded",
    )
    return "群策略已更新：%s = %s" % (canonical, value)


async def reset_group_policy(agent: Any, ctx: Any, field: str) -> str:
    """重置群策略（``field`` 为空/``all`` 时整表重置）。对应 Go ``ResetGroupPolicy``。"""
    if not can_manage_current_group_policy(agent, ctx):
        raise InvalidError("需要当前群群主/管理员或机器人超级管理员权限")
    scope = _scope(agent, ctx)
    previous = group_policy_snapshot(agent)
    raw_field = str(field or "").strip().lower()
    actor = _actor(agent, ctx)
    if not raw_field or raw_field == "all":
        set_group_policy_for_scope(agent, scope, GroupPolicyConfig())
        try:
            _save_runtime_state(agent)
        except BaseException:
            set_group_policy_snapshot(agent, previous)
            raise
        _audit(
            agent,
            "group_policy_reset",
            actor_id=actor.id,
            scope=context_overflow_key(scope),
            field="all",
            result="succeeded",
        )
        return "当前群策略已重置为全局默认。"
    canonical = canonical_group_policy_field(raw_field)
    if not canonical:
        raise InvalidError("未知的群策略字段")
    if canonical in SUPERADMIN_ONLY_FIELDS and getattr(actor, "role", "") != RoleSuperadmin:
        raise InvalidError("%s 只能由机器人超级管理员重置" % canonical)
    policy = reset_policy_field(group_policy_for_scope(agent, scope), canonical)
    set_group_policy_for_scope(agent, scope, policy.normalize())
    try:
        _save_runtime_state(agent)
    except BaseException:
        set_group_policy_snapshot(agent, previous)
        raise
    _audit(
        agent,
        "group_policy_reset",
        actor_id=actor.id,
        scope=context_overflow_key(scope),
        field=canonical,
        result="succeeded",
    )
    return "群策略字段已重置：%s" % canonical


class GroupPolicyMixin:
    """Go ``*Agent`` 的群策略方法组。"""

    # -- 快照 ---------------------------------------------------------------
    def set_group_policy_snapshot(
        self, snapshot: dict[str, GroupPolicyConfig] | None
    ) -> None:
        set_group_policy_snapshot(self, snapshot)

    def group_policy_snapshot(self) -> dict[str, GroupPolicyConfig]:
        return group_policy_snapshot(self)

    def group_policy_for_scope(self, scope: Any) -> GroupPolicyConfig:
        return group_policy_for_scope(self, scope)

    def set_group_policy_for_scope(self, scope: Any, value: GroupPolicyConfig) -> None:
        set_group_policy_for_scope(self, scope, value)

    def raw_group_policy(self, scope: Any) -> GroupPolicyConfig:
        return raw_group_policy(self, scope)

    # -- 视图 / 权限 --------------------------------------------------------
    def is_group_scope(self, ctx: Any) -> bool:
        return is_group_scope(self, ctx)

    def can_manage_current_group_policy(self, ctx: Any) -> bool:
        return can_manage_current_group_policy(self, ctx)

    def group_policy_status(self, ctx: Any) -> str:
        return group_policy_status(self, ctx)

    def group_merge_window(self, ctx: Any) -> float:
        return group_merge_window(self, ctx)

    def group_quiet_hours_active(self, ctx: Any) -> bool:
        return group_quiet_hours_active(self, ctx)

    def request_fair_key(self, ctx: Any) -> str:
        return request_fair_key(self, ctx)

    def request_scope_key(self, ctx: Any) -> str:
        return request_scope_key(self, ctx)

    def group_default_session_mode(self, ctx: Any) -> str:
        return group_default_session_mode(self, ctx)

    def group_admin_command_grant(self, ctx: Any, command_name: str) -> bool:
        return group_admin_command_grant(self, ctx, command_name)

    def group_history_enabled_for_scope(self, platform: str, scope_id: str) -> bool:
        return group_history_enabled_for_scope(self, platform, scope_id)

    def learning_enabled_for_scope(self, platform: str, scope_id: str) -> bool:
        return learning_enabled_for_scope(self, platform, scope_id)

    def history_enabled(self, ctx: Any) -> bool:
        return history_enabled(self, ctx)

    def group_learning_enabled(self, ctx: Any) -> bool:
        return group_learning_enabled(self, ctx)

    def authorize_learning_action(self, ctx: Any, action: str) -> bool:
        return authorize_learning_action(self, ctx, action)

    # -- 模型目录 -----------------------------------------------------------
    def resolve_group_model_selection(self, raw: str) -> ModelSelection | None:
        return resolve_group_model_selection(self, raw)

    def group_model_selection_allowed(
        self, policy: GroupPolicyConfig, selection: Any, raw: str
    ) -> bool:
        return group_model_selection_allowed(self, policy, selection, raw)

    def group_default_model_selection(self, ctx: Any) -> ModelSelection | None:
        return group_default_model_selection(self, ctx)

    # -- 写入 ---------------------------------------------------------------
    async def set_group_policy(self, ctx: Any, field: str, value: str) -> str:
        return await set_group_policy(self, ctx, field, value)

    async def reset_group_policy(self, ctx: Any, field: str) -> str:
        return await reset_group_policy(self, ctx, field)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)


def _base_scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "base_scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import base_scope

    return base_scope(agent, ctx)


def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import actor

    return actor(agent, ctx)


def _client_exists(agent: Any, provider: str) -> bool:
    helper = getattr(agent, "client_for_provider", None)
    if helper is None:
        return True
    return helper(provider) is not None


def _save_runtime_state(agent: Any) -> None:
    helper = getattr(agent, "save_runtime_state", None)
    if helper is not None:
        helper()


def _budget_usage(agent: Any, ctx: Any) -> tuple[int, int]:
    helper = getattr(agent, "budget_usage_for_scope", None)
    if helper is None:
        return 0, 0
    return helper(ctx)


def _asr_budget_usage(agent: Any, ctx: Any) -> int:
    helper = getattr(agent, "asr_budget_usage_for_scope", None)
    if helper is None:
        return 0
    return helper(ctx)


def _chat_budget_usage(agent: Any, ctx: Any) -> tuple[int, int]:
    helper = getattr(agent, "chat_budget_usage_for_scope", None)
    if helper is None:
        return 0, 0
    return helper(ctx)


def _audit(agent: Any, event: str, **attributes: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in attributes.items():
        args.append(key)
        args.append(value)
    audit(*args)
