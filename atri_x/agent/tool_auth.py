"""群级工具授权与执行闸门。对应 Go：``internal/agent/tool_auth.go``。

职责：``authorizeToolCall`` / ``toolAuthorizationNames`` / ``groupToolAllowlist`` /
``beforeExecuteToolCall`` / ``filterGroupFeatureSchemas`` / ``isHistoryToolName`` /
``toolNamesInclude`` / ``toolExecutionQuotaKind`` / ``shortHash``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``*Agent`` 方法 → 模块级函数（首个参数 ``agent``，鸭子类型）+
  :class:`ToolAuthMixin`，这样不依赖尚未落地的 ``core.py``。
* ``beforeExecuteToolCall`` 里 Go 返回 ``error``，Python 抛异常；
  需要回喂给模型的地方由 ``RunRequest.before_execute`` 捕回来（runner 已按
  "返回 Exception 实例" 处理）。
* 群策略快照 / 预算账本 / 模型目录分别属于 ``group_policy.py``、``budget.py``、
  ``model_policy.py``：全部走函数内延迟导入，缺失时按 Go 的零值语义降级
  （无群策略 = 不限制；额度账本缺失 = 不预占）。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from atri_x.toolrun.toolrun import ResolvedTool

from atri_x.agent.tools import compact_arguments, short_hash

__all__ = [
    "ToolAuthMixin",
    "artist_extract_uses_vision",
    "authorize_tool_call",
    "before_execute_tool_call",
    "filter_group_feature_schemas",
    "group_tool_allowlist",
    "image_generate_is_dry_run",
    "is_history_tool_name",
    "tool_authorization_names",
    "tool_execution_quota_kind",
    "tool_names_include",
]

#: 由群策略开关控制的历史类工具。对应 Go ``isHistoryToolName`` 的 switch。
HISTORY_TOOL_NAMES = (
    "search_chat_history",
    "get_chat_history_around",
    "reply_to_chat_history_message",
)

#: ``canonicalGroupPolicyField`` 里会被展开成具体工具名的 profile 别名来源。
_ALLOW_ALL_VALUES = ("*", "all")
_DENY_ALL_VALUES = ("", "none", "deny", "denyall")


def authorize_tool_call(
    agent: Any, ctx: Any, call: Any, resolved: ResolvedTool
) -> tuple[bool, str]:
    """服务端的工具执行闸门（与提示词和参数无关）。对应 Go ``authorizeToolCall``。"""
    allowlist = group_tool_allowlist(agent, ctx)
    if allowlist is None:
        return True, ""
    for name in tool_authorization_names(call, resolved):
        if not name:
            continue
        if allowlist.get(name.strip().lower()):
            return True, ""
    return False, "tool is not in this group's allowlist"


def tool_authorization_names(call: Any, resolved: ResolvedTool | None) -> list[str]:
    """一次调用可能命中的全部名字（别名解析前后）。对应 Go ``toolAuthorizationNames``。"""
    names: list[str] = []

    def append_name(name: Any) -> None:
        text = str(name or "").strip()
        if text:
            names.append(text)

    append_name(getattr(call, "name", ""))
    if resolved is not None:
        append_name(resolved.name)
        cached = getattr(resolved, "cached", None)
        if cached is not None:
            append_name(getattr(cached, "name", ""))
            append_name(getattr(cached, "canonical_name", ""))
    return names


def group_tool_allowlist(agent: Any, ctx: Any) -> dict[str, bool] | None:
    """当前群的工具白名单。对应 Go ``groupToolAllowlist``。

    ``None`` = 继承全局工具策略（不限制）；非 None 的空 dict = 禁止全部群工具。
    """
    if agent is None or not _is_group_scope(agent, ctx):
        return None
    policy = _group_policy_for_scope(agent, ctx)
    if not policy.tool_allowlist_restricted():
        return None
    allowed: dict[str, bool] = {}
    aliases = getattr(agent, "tool_aliases", None) or {}
    profiles = getattr(agent, "tool_profiles", None) or {}
    for raw in policy.tool_allowlist or ():
        value = str(raw or "").strip().lower()
        if value in _DENY_ALL_VALUES:
            continue
        if value in _ALLOW_ALL_VALUES:
            return None
        profile_name = value
        resolved = aliases.get(value)
        if resolved is not None:
            profile_name = str(resolved or "").strip().lower()
        tools = profiles.get(profile_name) or []
        if tools:
            for tool_name in tools:
                text = str(tool_name or "").strip().lower()
                if text:
                    allowed[text] = True
            continue
        allowed[value] = True
    return allowed


def before_execute_tool_call(agent: Any, ctx: Any, call: Any, resolved: ResolvedTool) -> None:
    """执行前最后一道闸门。对应 Go ``beforeExecuteToolCall``（Go 返回 error）。"""
    if agent is None or getattr(agent, "platform", None) is None:
        return
    # 排队期间策略可能被撤销 —— 执行前再授权一次。
    allowed, reason = authorize_tool_call(agent, ctx, call, resolved)
    if not allowed:
        raise ValueError("工具授权已变更：%s" % reason)
    policy = _group_policy_for_scope(agent, ctx)
    if not policy.is_group_analysis_enabled() and tool_names_include(
        call, resolved, "group_analysis"
    ):
        raise ValueError("本群已关闭群分析")
    if not policy.is_history_enabled() and tool_names_include(
        call, resolved, *HISTORY_TOOL_NAMES
    ):
        raise ValueError("本群已关闭历史记录")
    if not policy.is_learning_enabled() and tool_names_include(
        call, resolved, "self_learning_review"
    ):
        raise ValueError("本群已关闭学习观察")
    analysis_model = getattr(agent, "group_analysis_model", None)
    if (
        tool_names_include(call, resolved, "group_analysis")
        and analysis_model is not None
        and str(getattr(analysis_model, "provider", "") or "").strip()
        and str(getattr(analysis_model, "model", "") or "").strip()
    ):
        _authorize_execution_model(agent, ctx, analysis_model, "本群模型目录不允许群分析摘要模型")
    kind = tool_execution_quota_kind(call, resolved)
    if not kind:
        return
    vision = getattr(agent, "vision_selection", None)
    if (
        kind == "vision"
        and vision is not None
        and str(getattr(vision, "provider", "") or "").strip()
        and str(getattr(vision, "model", "") or "").strip()
    ):
        _authorize_execution_model(agent, ctx, vision, "本群模型目录不允许视觉模型")
    call_id = str(getattr(call, "id", "") or "").strip()
    execution_id = call_id
    if not call_id:
        call_id = short_hash("%s\x00%s\x00%s" % (kind, call.name, call.arguments))
    digest = short_hash(
        "%s\x00%s\x00%s"
        % (kind, str(call.name or "").strip().lower(), compact_arguments(call.arguments))
    )
    ok, reason = _reserve_tool_execution(agent, ctx, execution_id, digest)
    if not ok:
        raise ValueError("工具执行幂等校验失败：%s" % reason)
    ok, reason = _reserve_budget_request(agent, ctx, kind, call_id, digest)
    if not ok:
        _release_tool_execution(agent, ctx, execution_id)
        raise ValueError("受限调用被额度账本拒绝：%s" % reason)


def filter_group_feature_schemas(agent: Any, ctx: Any, schemas: Sequence[Any]) -> list[Any]:
    """按群策略裁剪工具 schema。对应 Go ``filterGroupFeatureSchemas``。"""
    if agent is None or not _is_group_scope(agent, ctx) or not schemas:
        return list(schemas or [])
    policy = _group_policy_for_scope(agent, ctx)
    out: list[Any] = []
    for schema in schemas:
        name = str(getattr(getattr(schema, "function", None), "name", "") or "").strip().lower()
        if not policy.is_group_analysis_enabled() and name == "group_analysis":
            continue
        if not policy.is_history_enabled() and is_history_tool_name(name):
            continue
        if not policy.is_learning_enabled() and name == "self_learning_review":
            continue
        out.append(schema)
    return out


def is_history_tool_name(name: str) -> bool:
    """对应 Go ``isHistoryToolName``。"""
    return str(name or "").strip().lower() in HISTORY_TOOL_NAMES


def tool_names_include(call: Any, resolved: ResolvedTool | None, *want: str) -> bool:
    """调用名（含别名）是否命中给定集合。对应 Go ``toolNamesInclude``。"""
    candidates = [str(item or "").strip().lower() for item in want]
    for name in tool_authorization_names(call, resolved):
        if name.strip().lower() in candidates:
            return True
    return False


def tool_execution_quota_kind(call: Any, resolved: ResolvedTool | None) -> str:
    """这次调用消耗哪一类日额度。对应 Go ``toolExecutionQuotaKind``。"""
    for name in tool_authorization_names(call, resolved):
        lowered = name.strip().lower()
        if lowered == "image_generate":
            # dry run 只编译提示词、不碰图片端点，因此不占生图额度。
            if image_generate_is_dry_run(getattr(call, "arguments", "")):
                return ""
            return "image"
        if lowered == "image_to_prompt":
            return "vision"
        if lowered == "artist_extract":
            # 只有走视觉兜底的那次才占视觉额度。
            if artist_extract_uses_vision(getattr(call, "arguments", "")):
                return "vision"
            return ""
    return ""


def artist_extract_uses_vision(arguments: str) -> bool:
    """``artist_extract`` 是否显式选择视觉兜底。对应 Go ``artistExtractUsesVision``。

    参数不可解析时算"不用视觉"：畸形调用不该预占它根本不会发起的模型额度。
    """
    args = _parse_arguments(arguments)
    if args is None:
        return False
    return bool(args.get("visual_fallback"))


def image_generate_is_dry_run(arguments: str) -> bool:
    """``image_generate`` 是否只是 dry run。对应 Go ``imageGenerateIsDryRun``。

    参数不可解析时算"不是 dry run"：额度上的保守答案是继续计费。
    """
    args = _parse_arguments(arguments)
    if args is None:
        return False
    return bool(args.get("dry_run"))


class ToolAuthMixin:
    """Go ``*Agent`` 的群级工具授权方法组。"""

    def authorize_tool_call(self, ctx: Any, call: Any, resolved: ResolvedTool) -> tuple[bool, str]:
        return authorize_tool_call(self, ctx, call, resolved)

    def group_tool_allowlist(self, ctx: Any) -> dict[str, bool] | None:
        return group_tool_allowlist(self, ctx)

    def before_execute_tool_call(self, ctx: Any, call: Any, resolved: ResolvedTool) -> None:
        before_execute_tool_call(self, ctx, call, resolved)

    def filter_group_feature_schemas(self, ctx: Any, schemas: Sequence[Any]) -> list[Any]:
        return filter_group_feature_schemas(self, ctx, schemas)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _parse_arguments(arguments: str) -> dict[str, Any] | None:
    text = str(arguments or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _is_group_scope(agent: Any, ctx: Any) -> bool:
    helper = getattr(agent, "is_group_scope", None)
    if helper is not None:
        return bool(helper(ctx))
    try:
        from atri_x.agent.group_policy import is_group_scope

        return bool(is_group_scope(agent, ctx))
    except Exception:  # noqa: BLE001
        return False


def _group_policy_for_scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "group_policy_for_scope", None)
    if helper is not None:
        scope = _scope(agent, ctx)
        return helper(scope)
    from atri_x.agent.group_policy import group_policy_for_scope

    return group_policy_for_scope(agent, _scope(agent, ctx))


def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.session import Scope

    return Scope()


def _authorize_execution_model(agent: Any, ctx: Any, selection: Any, label: str) -> None:
    helper = getattr(agent, "authorize_execution_model_selection", None)
    if helper is None:
        return
    try:
        outcome = helper(ctx, selection)
    except BaseException as exc:  # noqa: BLE001 - 与 Go 的 %w 包装同义
        raise ValueError("%s：%s" % (label, exc)) from exc
    if isinstance(outcome, BaseException):
        raise ValueError("%s：%s" % (label, outcome))


def _reserve_tool_execution(
    agent: Any, ctx: Any, execution_id: str, digest: str
) -> tuple[bool, str]:
    helper = getattr(agent, "reserve_tool_execution", None)
    if helper is None:
        return True, ""
    return helper(ctx, execution_id, digest)


def _release_tool_execution(agent: Any, ctx: Any, execution_id: str) -> None:
    helper = getattr(agent, "release_tool_execution", None)
    if helper is not None:
        helper(ctx, execution_id)


def _reserve_budget_request(
    agent: Any, ctx: Any, kind: str, call_id: str, digest: str
) -> tuple[bool, str]:
    helper = getattr(agent, "reserve_budget_request", None)
    if helper is None:
        return True, ""
    try:
        from atri_x.agent.budget import BudgetReservationRequest
    except Exception:  # noqa: BLE001 - budget 模块未落地时按需构造鸭子对象
        BudgetReservationRequest = None  # type: ignore[assignment]
    if BudgetReservationRequest is None:
        return helper(ctx, kind, {"call_id": call_id, "digest": digest})
    return helper(ctx, kind, BudgetReservationRequest(call_id=call_id, digest=digest))
