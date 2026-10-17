"""群策略字段的规范化与校验规则。

对应 Go：``internal/agent/group_policy.go`` 里的
``canonicalGroupPolicyField`` / ``splitGroupList`` / ``isClearValue`` /
``parseEnabled`` / ``validQuietHours`` / ``parseQuietHours`` /
``parseClockMinutes`` / ``quietHoursActive`` / ``groupPolicyFieldAuditValue``。

拆分原因：``group_policy.go``（Go 1026 行）超过本仓单文件上限，按职责拆成
"字段规则"（本文件）与"策略状态 + 视图"（``group_policy.py``）两部分。

与 Go 的差异（形式，不是简化）：

* Go 的 ``SetGroupPolicy`` 默认分支返回 ``error`` → 本模块抛
  :class:`atri_x.errors.InvalidError`，消息文本与 Go 逐字一致。
* ``apply_policy_field`` 需要解析模型别名 / 群可选模型目录时回调
  ``agent.resolve_group_model_selection`` / ``agent.group_model_selection_allowed``
  （由 ``group_policy.py`` 提供），仍然只依赖鸭子类型。
"""

from __future__ import annotations

from typing import Any, Sequence

from atri_x.config import (
    LEARNING_MODERATION_ACTIONS,
    GroupPolicyConfig,
    THREAD_MODE_GROUP,
)
from atri_x.errors import InvalidError

from atri_x.agent.tools import normalize_string_list

__all__ = [
    "FIELD_ALIASES",
    "FIELD_LABELS",
    "SUPERADMIN_ONLY_FIELDS",
    "apply_policy_field",
    "canonical_group_policy_field",
    "display_string_list",
    "enabled_text",
    "group_policy_field_audit_value",
    "is_clear_value",
    "parse_clock_minutes",
    "parse_enabled",
    "parse_quiet_hours",
    "quiet_hours_active",
    "reset_policy_field",
    "split_group_list",
    "valid_quiet_hours",
]

#: ``canonicalGroupPolicyField`` 的别名表（键是规范化后的小写字段名）。
FIELD_ALIASES: dict[str, str] = {
    "wake": "wake",
    "wakeup": "wake",
    "keywords": "wake",
    "wake-keywords": "wake",
    "response": "response",
    "response-mode": "response",
    "mode": "response",
    "thread-mode": "thread-mode",
    "thread": "thread-mode",
    "threads": "thread-mode",
    "shared-session": "thread-mode",
    "session-thread": "thread-mode",
    "merge-window": "merge-window",
    "merge": "merge-window",
    "merge-ms": "merge-window",
    "merge-window-ms": "merge-window",
    "default-mode": "default-mode",
    "session-mode": "default-mode",
    "default-model": "default-model",
    "model": "default-model",
    "allowed-models": "allowed-models",
    "model-catalog": "allowed-models",
    "models": "allowed-models",
    "tool-allow": "tool-allow",
    "tools": "tool-allow",
    "tool-allowlist": "tool-allow",
    "image-quota": "image-quota",
    "image": "image-quota",
    "vision-quota": "vision-quota",
    "vision": "vision-quota",
    "asr-quota": "asr-quota",
    "voice-quota": "asr-quota",
    "speech-quota": "asr-quota",
    "user-image-quota": "user-image-quota",
    "user-image": "user-image-quota",
    "user-vision-quota": "user-vision-quota",
    "user-vision": "user-vision-quota",
    "user-asr-quota": "user-asr-quota",
    "user-asr": "user-asr-quota",
    "user-voice-quota": "user-asr-quota",
    "chat-tokens-quota": "chat-tokens-quota",
    "chat-tokens": "chat-tokens-quota",
    "tokens-quota": "chat-tokens-quota",
    "chat-cost-quota": "chat-cost-quota",
    "chat-cost": "chat-cost-quota",
    "cost-quota": "chat-cost-quota",
    "quiet": "quiet",
    "quiet-hours": "quiet",
    "analysis": "analysis",
    "group-analysis": "analysis",
    "learning": "learning",
    "history": "history",
    "knowledge": "knowledge",
    "kb": "knowledge",
    "faq": "knowledge",
    "services": "services",
    "service": "services",
    "group-services": "services",
    "reminders": "services",
    "polls": "services",
    "signups": "services",
    "asr": "asr",
    "voice": "asr",
    "voice-transcription": "asr",
    "learning-moderation": "learning-moderation",
    "moderation": "learning-moderation",
    "learning-moderation-actions": "learning-moderation-actions",
    "moderation-actions": "learning-moderation-actions",
    "learning-actions": "learning-moderation-actions",
}

#: 只能由机器人超级管理员配置/重置的字段。对应 Go 的两处 switch。
SUPERADMIN_ONLY_FIELDS = (
    "allowed-models",
    "learning-moderation",
    "learning-moderation-actions",
)

#: 审计与状态文本里的字段标签（与 Go 的 switch 分支一一对应）。
FIELD_LABELS = {
    "image-quota": "生图日额度",
    "vision-quota": "视觉日额度",
    "asr-quota": "语音转写日额度",
    "user-image-quota": "单用户生图日额度",
    "user-vision-quota": "单用户视觉日额度",
    "user-asr-quota": "单用户语音转写日额度",
    "chat-tokens-quota": "群日聊天 token 额度",
    "chat-cost-quota": "群日聊天费用额度",
}

_ENABLED_FIELDS = {
    "analysis": "group_analysis",
    "learning": "learning",
    "history": "history",
    "knowledge": "knowledge",
    "services": "services",
    "asr": "asr",
}

_INT_FIELDS = {
    "image-quota": ("image_quota", "生图日额度必须是非负整数"),
    "vision-quota": ("vision_quota", "视觉日额度必须是非负整数"),
    "asr-quota": ("asr_quota", "语音转写日额度必须是非负整数"),
    "user-image-quota": ("user_image_quota", "单用户生图日额度必须是非负整数"),
    "user-vision-quota": ("user_vision_quota", "单用户视觉日额度必须是非负整数"),
    "user-asr-quota": ("user_asr_quota", "单用户语音转写日额度必须是非负整数"),
}


def canonical_group_policy_field(field: str) -> str:
    """规范化字段名（下划线转连字符 + 别名归一）。对应 Go ``canonicalGroupPolicyField``。"""
    text = str(field or "").strip().lower().replace("_", "-")
    if text in ("", "all"):
        return ""
    return FIELD_ALIASES.get(text, "")


def split_group_list(value: str) -> list[str]:
    """按中英文分隔符切分列表值。对应 Go ``splitGroupList``。"""
    text = str(value or "").strip()
    if not text or is_clear_value(text):
        return []
    for separator in ("，", ",", ";", "；", "、"):
        text = text.replace(separator, "\n")
    return normalize_string_list(text.split("\n"))


def is_clear_value(value: str) -> bool:
    """是否是"清空/关闭"值。对应 Go ``isClearValue``。"""
    return str(value or "").strip().lower() in (
        "",
        "clear",
        "none",
        "reset",
        "off",
        "disable",
        "disabled",
    )


def parse_enabled(value: str) -> bool:
    """解析开关值。对应 Go ``parseEnabled``（非法值抛 ``InvalidError``）。"""
    text = str(value or "").strip().lower()
    if text in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if text in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    raise InvalidError("布尔值应为 on/off")


def parse_clock_minutes(value: str) -> tuple[int, bool]:
    """``HH:MM`` → 一天中的分钟数。对应 Go ``parseClockMinutes``。"""
    text = str(value or "").strip()
    if ":" not in text:
        return 0, False
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text.strip())
        minute = int(minute_text.strip())
    except ValueError:
        return 0, False
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return 0, False
    return hour * 60 + minute, True


def parse_quiet_hours(value: str) -> tuple[int, int, bool]:
    """``HH:MM-HH:MM`` → ``(start, end, ok)``。对应 Go ``parseQuietHours``。"""
    text = str(value or "").strip()
    if not text or "-" not in text:
        return 0, 0, False
    left, _, right = text.partition("-")
    start, ok_start = parse_clock_minutes(left)
    end, ok_end = parse_clock_minutes(right)
    if not ok_start or not ok_end:
        return 0, 0, False
    return start, end, True


def valid_quiet_hours(value: str) -> bool:
    """对应 Go ``validQuietHours``。"""
    start, end, ok = parse_quiet_hours(value)
    return ok and start >= 0 and end >= 0


def quiet_hours_active(quiet: str, now: Any) -> bool:
    """当前是否落在静默时段。对应 Go ``quietHoursActive``。

    ``now`` 是带时区的 :class:`datetime.datetime`；本地时分与 Go 的 ``time.Now()`` 同义
    （Go 用进程本地时区，Python 侧调用方传本地时间）。
    """
    start, end, ok = parse_quiet_hours(quiet)
    if not ok:
        return False
    current = now.hour * 60 + now.minute
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def display_string_list(values: Sequence[str] | None) -> str:
    """列表展示。对应 Go ``displayStringList``。"""
    items = [str(value) for value in (values or ()) if str(value)]
    if not items:
        return "无"
    return "、".join(items)


def enabled_text(value: bool) -> str:
    """开关展示。对应 Go ``enabledText``。"""
    return "开" if value else "关"


def group_policy_field_audit_value(policy: GroupPolicyConfig, field: str) -> str:
    """审计用的字段当前值。对应 Go ``groupPolicyFieldAuditValue``。"""
    policy = policy.normalize()
    if field == "wake":
        return display_string_list(policy.wake_keywords)
    if field == "response":
        return policy.response_mode_value()
    if field == "thread-mode":
        return policy.thread_mode_value()
    if field == "merge-window":
        return str(policy.merge_window_ms)
    if field == "default-mode":
        return policy.default_mode or "inherit"
    if field == "default-model":
        return policy.default_model
    if field == "allowed-models":
        return display_string_list(policy.allowed_models)
    if field == "tool-allow":
        if not policy.tool_allowlist_restricted():
            return "inherit"
        if not policy.tool_allowlist:
            return "none"
        return display_string_list(policy.tool_allowlist)
    if field in FIELD_LABELS:
        if field == "chat-tokens-quota":
            return str(policy.chat_tokens_quota)
        if field == "chat-cost-quota":
            return _format_float(policy.chat_cost_quota)
        return str(getattr(policy, _int_field_name(field)))
    if field == "quiet":
        return policy.quiet_hours
    if field in _ENABLED_FIELDS:
        return enabled_text(_enabled_reader(field)(policy))
    if field == "learning-moderation":
        return enabled_text(policy.learning_moderation)
    if field == "learning-moderation-actions":
        return display_string_list(policy.learning_moderation_actions_value())
    return ""


def apply_policy_field(
    agent: Any, policy: GroupPolicyConfig, field: str, value: str
) -> GroupPolicyConfig:
    """把 ``field = value`` 应用到策略上。对应 Go ``SetGroupPolicy`` 的 switch。

    成功返回新的策略对象；非法值抛 :class:`atri_x.errors.InvalidError`
    （消息文本与 Go 逐字一致）。
    """
    raw = str(value or "")
    text = raw.strip()
    if field == "wake":
        policy.wake_keywords = split_group_list(raw)
    elif field == "response":
        mode = text.lower()
        if mode not in ("mention", "all", "keyword", "keywords", "reply", "replies", "off", "none", "disabled"):
            raise InvalidError(
                "无效的响应模式 %r，可选：mention、all、keyword、reply、off" % raw
            )
        policy.response_mode = mode
    elif field == "thread-mode":
        mode = text.lower()
        if mode in ("group", "shared", "multi", "multi_user", "multi-user"):
            policy.thread_mode = THREAD_MODE_GROUP
        elif mode in ("per_user", "per-user", "single", "isolated", "inherit", "clear", "reset", "default"):
            policy.thread_mode = ""
        else:
            raise InvalidError("无效的会话线程模式 %r，可选：per-user、group" % raw)
    elif field == "merge-window":
        try:
            ms = int(text)
        except ValueError:
            ms = -1
        if ms < 0 or ms > 10000:
            raise InvalidError("连续消息合并窗口必须是 0-10000 之间的整数毫秒")
        policy.merge_window_ms = ms
    elif field == "default-mode":
        mode = text.lower()
        if mode in ("inherit", "clear", "reset", "default"):
            policy.default_mode = ""
        elif mode in ("work", "chat"):
            policy.default_mode = mode
        else:
            raise InvalidError("无效的默认会话模式 %r，可选：work、chat、inherit" % raw)
    elif field == "default-model":
        if text.lower() in ("clear", "inherit"):
            policy.default_model = ""
        else:
            selection = _resolve_selection(agent, text)
            if selection is None:
                raise InvalidError(
                    "未知或不可用的模型 %r，请使用已配置的模型别名或 provider/model" % text
                )
            if not _selection_allowed(agent, policy, selection, text):
                raise InvalidError(
                    "模型 %r 不在本群可选模型目录内；请让超级管理员先配置 /grouppolicy allowed-models"
                    % text
                )
            policy.default_model = text
    elif field == "tool-allow":
        lowered = text.lower()
        if lowered in ("none", "deny", "denyall"):
            policy.tool_allowlist = []
            policy.tool_allowlist_set = True
        elif is_clear_value(raw):
            policy.tool_allowlist = None
            policy.tool_allowlist_set = False
        else:
            policy.tool_allowlist = split_group_list(raw)
            policy.tool_allowlist_set = True
    elif field == "allowed-models":
        if is_clear_value(raw):
            policy.allowed_models = []
        else:
            values = split_group_list(raw)
            if not values:
                raise InvalidError("allowed-models 不能为空；可发送 clear 恢复默认目录")
            for item in values:
                if item == "*":
                    continue
                if _resolve_selection(agent, item) is None:
                    raise InvalidError(
                        "allowed-models 中的 %r 不是已配置的模型别名或 provider/model" % item
                    )
            policy.allowed_models = values
    elif field in _INT_FIELDS or field == "chat-tokens-quota":
        if field == "chat-tokens-quota":
            try:
                number = int(text)
            except ValueError:
                number = -1
            if number < 0:
                raise InvalidError("群日聊天 token 额度必须是非负整数")
            policy.chat_tokens_quota = number
        else:
            attr, message = _INT_FIELDS[field]
            try:
                number = int(text)
            except ValueError:
                number = -1
            if number < 0:
                raise InvalidError(message)
            setattr(policy, attr, number)
    elif field == "chat-cost-quota":
        try:
            amount = float(text)
        except ValueError:
            amount = -1.0
        if amount < 0:
            raise InvalidError("群日聊天费用额度必须是非负数")
        policy.chat_cost_quota = amount
    elif field == "quiet":
        if is_clear_value(raw):
            policy.quiet_hours = ""
        elif not valid_quiet_hours(raw):
            raise InvalidError("静默时段格式应为 HH:MM-HH:MM，例如 23:00-07:30")
        else:
            policy.quiet_hours = text
    elif field in _ENABLED_FIELDS:
        setattr(policy, _ENABLED_FIELDS[field], parse_enabled(raw))
    elif field == "learning-moderation":
        enabled = parse_enabled(raw)
        policy.learning_moderation = enabled
        if not enabled:
            policy.learning_moderation_actions = []
    elif field == "learning-moderation-actions":
        if is_clear_value(raw):
            policy.learning_moderation_actions = []
        else:
            values = split_group_list(raw)
            if not values:
                raise InvalidError("learning-moderation-actions 不能为空")
            for item in values:
                if item.strip().lower() not in LEARNING_MODERATION_ACTIONS:
                    raise InvalidError(
                        "无效的学习审核权限 %r，可选：view、decide、mine、delete、export、policy"
                        % item
                    )
            policy.learning_moderation_actions = values
    else:
        raise InvalidError("未知的群策略字段")
    return policy


def reset_policy_field(policy: GroupPolicyConfig, field: str) -> GroupPolicyConfig:
    """把单个字段复位为"未设置"。对应 Go ``ResetGroupPolicy`` 的 switch。"""
    if field == "wake":
        policy.wake_keywords = []
    elif field == "response":
        policy.response_mode = ""
    elif field == "thread-mode":
        policy.thread_mode = ""
    elif field == "merge-window":
        policy.merge_window_ms = 0
    elif field == "default-mode":
        policy.default_mode = ""
    elif field == "default-model":
        policy.default_model = ""
    elif field == "allowed-models":
        policy.allowed_models = []
    elif field == "tool-allow":
        policy.tool_allowlist = None
        policy.tool_allowlist_set = False
    elif field in _INT_FIELDS or field == "chat-tokens-quota":
        if field == "chat-tokens-quota":
            policy.chat_tokens_quota = 0
        else:
            setattr(policy, _INT_FIELDS[field][0], 0)
    elif field == "chat-cost-quota":
        policy.chat_cost_quota = 0.0
    elif field == "quiet":
        policy.quiet_hours = ""
    elif field in _ENABLED_FIELDS:
        setattr(policy, _ENABLED_FIELDS[field], None)
    elif field == "learning-moderation":
        policy.learning_moderation = False
        policy.learning_moderation_actions = []
    elif field == "learning-moderation-actions":
        policy.learning_moderation_actions = []
    else:
        raise InvalidError("未知的群策略字段")
    return policy


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _int_field_name(field: str) -> str:
    return _INT_FIELDS[field][0]


def _enabled_reader(field: str) -> Any:
    return {
        "analysis": GroupPolicyConfig.is_group_analysis_enabled,
        "learning": GroupPolicyConfig.is_learning_enabled,
        "history": GroupPolicyConfig.is_history_enabled,
        "knowledge": GroupPolicyConfig.is_knowledge_enabled,
        "services": GroupPolicyConfig.is_services_enabled,
        "asr": GroupPolicyConfig.is_asr_enabled,
    }[field]


def _format_float(value: float) -> str:
    text = repr(float(value))
    if "e" not in text and "E" not in text:
        return text
    from decimal import Decimal

    return format(Decimal(text), "f")


def _resolve_selection(agent: Any, raw: str) -> Any:
    helper = getattr(agent, "resolve_group_model_selection", None)
    if helper is None:
        from atri_x.agent.group_policy import resolve_group_model_selection

        return resolve_group_model_selection(agent, raw)
    return helper(raw)


def _selection_allowed(
    agent: Any, policy: GroupPolicyConfig, selection: Any, raw: str
) -> bool:
    helper = getattr(agent, "group_model_selection_allowed", None)
    if helper is None:
        from atri_x.agent.group_policy import group_model_selection_allowed

        return bool(group_model_selection_allowed(agent, policy, selection, raw))
    return bool(helper(policy, selection, raw))
