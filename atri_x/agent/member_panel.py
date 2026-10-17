"""成员自助面板：任务与额度视图。

对应 Go：``internal/agent/member_panel.go``。

职责：``MemberPanel`` / ``currentMemberSession`` / ``writeMemberSessionSection`` /
``writeMemberTaskSection`` / ``writeMemberQuotaSection`` / ``formatMemberRequest`` /
``memberPanelDuration`` / ``memberCallQuotaLines`` / ``memberChatQuotaLines`` /
``formatMemberCostMicros``。

与 Go 的差异（形式，不是简化）：

* ``strings.Builder`` → 列表拼接；输出逐字对齐 Go（含缩进与"本群/当前作用域"标签）。
* ``a.budgetMu`` + 三张账本 → agent-core 的 ``BudgetLedger``（``ensure_budget``）。
  这里在调用前先 ``prune``（与 Go 相同：面板也会顺手清理过期账目）。
* ``turn.ToolsString`` → ``atri_x.turn.tools_string``；``request.Request`` 用鸭子类型
  读 ``kind`` / ``label`` / ``started_at`` / ``id`` / ``fair_key``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from atri_x.errors import InvalidError
from atri_x.turn import PhaseIdle, tools_string

from atri_x.agent.group_policy import (
    budget_text,
    context_overflow_key,
    cost_budget_text,
    int64_budget_text,
    is_group_scope,
)
from atri_x.agent.tools import first_non_empty, service_host

__all__ = [
    "MemberPanelMixin",
    "format_member_cost_micros",
    "format_member_request",
    "member_call_quota_lines",
    "member_chat_quota_lines",
    "member_panel",
    "member_panel_duration",
]

VIEW_ALL = "all"
_VIEW_TASKS = ("tasks", "task", "requests", "queue")
_VIEW_QUOTA = ("quota", "budget", "limits", "usage")
_VIEW_ALL_ALIASES = ("all", "panel", "status", "me")


def member_panel(agent: Any, ctx: Any, view: str) -> str:
    """构造当前发言人的自助视图。对应 Go ``MemberPanel``。"""
    if agent is None:
        raise InvalidError("agent is nil")
    host = service_host(agent)
    wanted = str(view or "").strip().lower() or VIEW_ALL
    show_tasks, show_quota = True, True
    if wanted in _VIEW_ALL_ALIASES:
        pass
    elif wanted in _VIEW_TASKS:
        show_quota = False
    elif wanted in _VIEW_QUOTA:
        show_tasks = False
    else:
        raise InvalidError("未知的面板视图 %r，可选：tasks、quota、all" % wanted)

    scope = _scope(host, ctx)
    actor = _actor(host, ctx)
    display_name = first_non_empty(
        getattr(actor, "group_card", ""),
        getattr(actor, "nickname", ""),
        getattr(actor, "display_name", ""),
    ) or "未知成员"
    lines = [
        "我的面板",
        "平台：%s" % getattr(scope, "platform", ""),
        "作用域：%s" % getattr(scope, "platform_scope_id", ""),
        "身份：%s（id:%s）" % (display_name, getattr(actor, "id", "")),
    ]
    session = _current_member_session(host, ctx)
    if show_tasks:
        lines.extend(_member_session_section(host, ctx, session))
        lines.extend(_member_task_section(host, ctx))
    if show_quota:
        lines.extend(_member_quota_section(host, ctx))
    return "\n".join(lines)


def _current_member_session(agent: Any, ctx: Any) -> Any:
    sessions = getattr(agent, "sessions", None)
    if sessions is None:
        return None
    try:
        return sessions.current(ctx, _scope(agent, ctx))
    except BaseException:  # noqa: BLE001 - 与 Go 相同：拿不到就是"无会话"
        return None


def _member_session_section(agent: Any, ctx: Any, session: Any) -> list[str]:
    lines = ["", "当前会话："]
    if session is None:
        lines.append("  无（下一条消息会创建或恢复当前会话）")
        return lines
    title = str(getattr(session, "title", "") or "").strip() or "(未命名)"
    lines.append("  标题：%s" % title)
    lines.append("  模式：%s" % getattr(session, "mode", ""))
    turns = getattr(agent, "turns", None)
    if turns is None:
        return lines
    snapshot = turns.snapshot(getattr(session, "id", ""))
    phase = getattr(snapshot, "phase", "") or PhaseIdle
    lines.append("  阶段：%s" % phase)
    lines.append("  待处理输入：%d" % int(getattr(snapshot, "pending_count", 0) or 0))
    tools = tools_string(getattr(snapshot, "tools", None))
    if tools:
        lines.append("  本轮工具：%s" % tools)
    return lines


def _member_task_section(agent: Any, ctx: Any) -> list[str]:
    lines = ["", "我的任务："]
    fair_key = _request_fair_key(agent, ctx)
    queued = _inbox_pending(agent, fair_key)
    own: list[Any] = []
    requests = getattr(agent, "requests", None)
    if requests is not None:
        for request in requests.list():
            if str(getattr(request, "fair_key", "") or "").strip() == fair_key:
                own.append(request)
    own.sort(key=lambda item: getattr(item, "started_at", None) or datetime.min.replace(tzinfo=timezone.utc))
    if not own and not queued:
        lines.append("  当前没有进行中的任务。")
        return lines
    for request in own:
        lines.append("  - " + format_member_request(request))
    if queued > 0:
        lines.append("  排队消息：%d 条" % queued)
    return lines


def format_member_request(request: Any) -> str:
    """一条进行中任务的文案。对应 Go ``formatMemberRequest``。"""
    kind = str(getattr(request, "kind", "") or "").strip()
    label = str(getattr(request, "label", "") or "").strip() or kind
    started = getattr(request, "started_at", None)
    elapsed = _elapsed_seconds(started)
    return "[%s] %s，已运行 %s（request %s）" % (
        kind,
        label,
        member_panel_duration(elapsed),
        getattr(request, "id", ""),
    )


def member_panel_duration(seconds: float) -> str:
    """时长文案（秒精度）。对应 Go ``memberPanelDuration``。"""
    if seconds < 1:
        return "0s"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    out = ""
    if hours:
        out += "%dh" % hours
    if minutes:
        out += "%dm" % minutes
    if secs or not out:
        out += "%ds" % secs
    return out


def _member_quota_section(agent: Any, ctx: Any) -> list[str]:
    """额度视图。对应 Go ``writeMemberQuotaSection``。"""
    host = service_host(agent)
    scope = _scope(host, ctx)
    scope_key = context_overflow_key(scope)
    actor = _actor(host, ctx)
    actor_id = first_non_empty(getattr(actor, "id", ""), getattr(actor, "platform_user_id", ""))
    group_label = "本群" if is_group_scope(host, ctx) else "当前作用域"

    image_specs = _call_limit_specs(host, ctx, "image")
    vision_specs = _call_limit_specs(host, ctx, "vision")
    asr_specs = _call_limit_specs(host, ctx, "asr")
    chat_specs = _chat_budget_limit_specs(host, ctx)

    ledger = _ledger(host)
    reservations, tokens, costs = _prune(ledger)
    writable = _writable(ledger)

    image_lines = member_call_quota_lines(reservations, "image", scope_key, actor_id, image_specs)
    vision_lines = member_call_quota_lines(reservations, "vision", scope_key, actor_id, vision_specs)
    asr_lines = member_call_quota_lines(reservations, "asr", scope_key, actor_id, asr_specs)
    chat_lines = member_chat_quota_lines(tokens, costs, scope_key, actor_id, chat_specs)
    if not image_lines:
        image_lines = _fallback_call_lines(ledger, reservations, "image", scope_key, actor_id, group_label)
    if not vision_lines:
        vision_lines = _fallback_call_lines(ledger, reservations, "vision", scope_key, actor_id, group_label)
    if not asr_lines:
        asr_lines = _fallback_call_lines(ledger, reservations, "asr", scope_key, actor_id, group_label)
    if not chat_lines:
        group_tokens = _count_tokens(ledger, tokens, "chat", scope_key, actor_id, True, False)
        group_costs = _count_costs(ledger, costs, "chat", scope_key, actor_id, True, False)
        actor_tokens = _count_tokens(ledger, tokens, "chat", scope_key, actor_id, True, True)
        actor_costs = _count_costs(ledger, costs, "chat", scope_key, actor_id, True, True)
        chat_lines = [
            "    %s今日：%d token，%s（未设置上限）"
            % (group_label, group_tokens, format_member_cost_micros(group_costs)),
            "    我今日：%d token，%s" % (actor_tokens, format_member_cost_micros(actor_costs)),
        ]

    lines = ["", "我的额度（今日）：", "  生图："]
    lines.extend(image_lines)
    lines.append("  视觉：")
    lines.extend(vision_lines)
    lines.append("  语音转写：")
    lines.extend(asr_lines)
    lines.append("  聊天：")
    lines.extend(chat_lines)
    if _chat_hard_limit(host):
        lines.append("    硬预算：开启（调用前预占）")
    if not writable:
        lines.append("  注意：额度账本当前不可写，受限调用会被拒绝。")
    return lines


def member_call_quota_lines(
    reservations: dict[str, int],
    kind: str,
    scope_key: str,
    actor_id: str,
    specs: list[Any],
) -> list[str]:
    """次数限额行。对应 Go ``memberCallQuotaLines``。

    普通成员只看得到群维度限额与自己名下的用户维度限额；跨群的全局聚合用量
    仍然只对管理员可见。
    """
    lines: list[str] = []
    for spec in specs:
        if not getattr(spec, "scope_match", False) and not getattr(spec, "actor_match", False):
            continue
        used = _count_reservations(
            None,
            reservations,
            kind,
            scope_key,
            actor_id,
            bool(spec.scope_match),
            bool(spec.actor_match),
        )
        lines.append("    - %s：%s" % (spec.label, budget_text(int(spec.limit), used)))
    return lines


def member_chat_quota_lines(
    tokens: dict[str, int],
    costs: dict[str, int],
    scope_key: str,
    actor_id: str,
    specs: list[Any],
) -> list[str]:
    """聊天 token / 费用限额行。对应 Go ``memberChatQuotaLines``。"""
    lines: list[str] = []
    for spec in specs:
        if not getattr(spec, "scope_match", False) and not getattr(spec, "actor_match", False):
            continue
        if getattr(spec, "metric", "tokens") == "cost":
            used = _count_costs(
                None,
                costs,
                "chat",
                scope_key,
                actor_id,
                bool(spec.scope_match),
                bool(spec.actor_match),
            )
            lines.append(
                "    - %s：%s"
                % (spec.label, cost_budget_text(float(spec.limit) / 1_000_000, used))
            )
        else:
            used = _count_tokens(
                None,
                tokens,
                "chat",
                scope_key,
                actor_id,
                bool(spec.scope_match),
                bool(spec.actor_match),
            )
            lines.append("    - %s：%s" % (spec.label, int64_budget_text(spec.limit, used)))
    return lines


def format_member_cost_micros(micros: int) -> str:
    """micros → 元。对应 Go ``formatMemberCostMicros``。"""
    return "%.6f" % (float(micros) / 1_000_000)


class MemberPanelMixin:
    """Go ``*Agent`` 的成员面板方法组。"""

    async def member_panel(self, ctx: Any, view: str) -> str:
        return member_panel(self, ctx, view)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _fallback_call_lines(
    ledger: Any,
    reservations: dict[str, int],
    kind: str,
    scope_key: str,
    actor_id: str,
    group_label: str,
) -> list[str]:
    group_used = _count_reservations(ledger, reservations, kind, scope_key, actor_id, True, False)
    actor_used = _count_reservations(ledger, reservations, kind, scope_key, actor_id, True, True)
    return [
        "    %s今日已用 %d（未设置上限）" % (group_label, group_used),
        "    我今日已用 %d" % actor_used,
    ]


def _ledger(agent: Any) -> Any:
    from atri_x.agent.budget import ensure_budget

    return ensure_budget(agent)


def _prune(ledger: Any) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """取三张账本的当前快照（内部已 prune）。对应 Go 面板里的 prune 调用。"""
    reservations: dict[str, int] = {}
    tokens: dict[str, int] = {}
    costs: dict[str, int] = {}
    for attr, target in (
        ("reservations_snapshot", reservations),
        ("tokens_snapshot", tokens),
        ("costs_snapshot", costs),
    ):
        getter = getattr(ledger, attr, None)
        if callable(getter):
            target.update(getter() or {})
    if not reservations:
        snapshot = getattr(ledger, "reservations_snapshot", None)
        if callable(snapshot):
            reservations.update(snapshot() or {})
    usage = getattr(ledger, "usage_snapshot", None)
    if callable(usage) and (not tokens or not costs):
        result = usage()
        if isinstance(result, tuple) and len(result) >= 2:
            if not tokens:
                tokens.update(result[0] or {})
            if not costs:
                costs.update(result[1] or {})
    return reservations, tokens, costs


def _count_reservations(
    ledger: Any,
    reservations: dict[str, int],
    kind: str,
    scope_key: str,
    actor_id: str,
    scope_match: bool,
    actor_match: bool,
) -> int:
    from atri_x.agent.budget import count_budget_reservation_matches

    if not reservations and ledger is not None:
        getter = getattr(ledger, "reservations_snapshot", None)
        if callable(getter):
            reservations = getter() or {}
    return count_budget_reservation_matches(
        reservations, kind, scope_key, actor_id, scope_match, actor_match
    )


def _count_tokens(
    ledger: Any,
    tokens: dict[str, int],
    kind: str,
    scope_key: str,
    actor_id: str,
    scope_match: bool,
    actor_match: bool,
) -> int:
    from atri_x.agent.budget import count_budget_usage_matches

    if not tokens and ledger is not None:
        usage = getattr(ledger, "usage_snapshot", None)
        if callable(usage):
            result = usage()
            if isinstance(result, tuple) and result:
                tokens = result[0] or {}
    return count_budget_usage_matches(
        tokens, kind, scope_key, actor_id, scope_match, actor_match
    )


def _count_costs(
    ledger: Any,
    costs: dict[str, int],
    kind: str,
    scope_key: str,
    actor_id: str,
    scope_match: bool,
    actor_match: bool,
) -> int:
    from atri_x.agent.budget import count_budget_usage_matches

    if not costs and ledger is not None:
        usage = getattr(ledger, "usage_snapshot", None)
        if callable(usage):
            result = usage()
            if isinstance(result, tuple) and len(result) >= 2:
                costs = result[1] or {}
    return count_budget_usage_matches(costs, kind, scope_key, actor_id, scope_match, actor_match)


def _writable(ledger: Any) -> bool:
    checker = getattr(ledger, "writable", None)
    if callable(checker):
        return bool(checker())
    return True


def _chat_hard_limit(agent: Any) -> bool:
    limits = getattr(agent, "budget_limits", None)
    if limits is None:
        return False
    normalizer = getattr(limits, "normalized", None)
    normalized = normalizer() if callable(normalizer) else limits
    return bool(getattr(normalized, "chat_hard_limit", False))


def _call_limit_specs(agent: Any, ctx: Any, kind: str) -> list[Any]:
    helper = getattr(agent, "call_limit_specs", None)
    if helper is None:
        return []
    return list(helper(ctx, kind) or [])


def _chat_budget_limit_specs(agent: Any, ctx: Any) -> list[Any]:
    helper = getattr(agent, "chat_budget_limit_specs", None)
    if helper is None:
        return []
    return list(helper(ctx) or [])


def _request_fair_key(agent: Any, ctx: Any) -> str:
    helper = getattr(agent, "request_fair_key", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.group_policy import request_fair_key

    return request_fair_key(agent, ctx)


def _inbox_pending(agent: Any, fair_key: str) -> int:
    try:  # agent-core 的 inbox 模块可能尚未落地
        from atri_x.agent.inbox import inbox_pending_for_fair_key

        return int(inbox_pending_for_fair_key(agent, fair_key))
    except Exception:  # noqa: BLE001
        return 0


def _elapsed_seconds(started: Any) -> float:
    if not isinstance(started, datetime):
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


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
