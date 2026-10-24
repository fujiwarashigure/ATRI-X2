"""计算静默：静默时段里"哪些模型调用允许发生"的唯一判定入口。

对应本仓《dsh-qq 借鉴落地评估》§E（计算静默）。

边界先说清楚：

* **只压计算，不压数据**：消息照常入库、照常出现在历史里，只是不叫醒模型；
* **不补跑**：跳过就是跳过，宁可少说一句，也不在静默结束后堆积一批迟到回复
  （评估里明确"skip 不补跑"，避免补跑风暴）；
* **群聊另有群级静默**：``[group_policy]`` 的 ``quiet_hours`` 已经在
  :func:`atri_x.agent.wakeup.message_wakeup` 里生效；本模块的 ``[quiet]`` 是
  **全局**开关，两者取"或"（任一生效即静默）；
* **默认关闭**：``[quiet].enabled = false`` 时 :func:`admit` 恒为 allow，行为与
  1.0.5 之前完全一致。

调用原因（``cron`` / ``external_event`` / ``direct_message`` …）沿用
:mod:`atri_x.agent.call_reason` 的枚举，因此 ``/usage --by trigger`` 与准入判定
看的是同一套标签。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any

from atri_x.agent.call_reason import (
    AMBIENT_PARTICIPATION,
    CRON,
    DIRECT_MESSAGE,
    EXTERNAL_EVENT,
    MEMORY_LEARNING,
    normalize_reason,
)
from atri_x.agent.group_policy_rules import quiet_hours_active
from atri_x.security import RoleSuperadmin, actor_from_context

#: 允许调用。
ADMIT_ALLOW = "allow"

#: 跳过本次调用（不补跑）。
ADMIT_SKIP = "skip"

#: 被静默跳过的后台运行在 ``RunResult.outcome`` 里的标记（cron / Atrinis 据此
#: 识别"这次没有报告"，不要把空文本当解析失败去重试）。
ADMISSION_SKIP_OUTCOME = "quiet_skipped"

#: 后台运行种类 → 调用原因。没列出的种类不参与静默准入。
_BACKGROUND_REASONS = {
    "cron": CRON,
    "atrinis": EXTERNAL_EVENT,
}

#: 静默期默认可跳过的原因。
_SKIPPABLE = (CRON, EXTERNAL_EVENT, MEMORY_LEARNING, AMBIENT_PARTICIPATION)


@dataclass(frozen=True)
class Admission:
    """一次准入判定的结果。"""

    decision: str = ADMIT_ALLOW
    reason: str = ""
    detail: str = ""
    quiet: bool = False

    @property
    def skipped(self) -> bool:
        return self.decision == ADMIT_SKIP

    @property
    def allowed(self) -> bool:
        return self.decision == ADMIT_ALLOW


def quiet_config(agent: Any) -> Any:
    """取 Agent 上的 ``[quiet]`` 配置；没有就返回 ``None``。"""
    return getattr(agent, "quiet_config", None)


def quiet_enabled(agent: Any) -> bool:
    """全局静默是否启用（默认关闭）。"""
    return bool(getattr(quiet_config(agent), "enabled", False))


def quiet_window(agent: Any) -> str:
    """静默窗口字符串（``HH:MM-HH:MM``）。"""
    return str(getattr(quiet_config(agent), "window", "") or "")


def quiet_timezone(agent: Any) -> tzinfo:
    """静默判定用的时区：配置里的 IANA 名，非法或为空时用进程本地时区。"""
    name = str(getattr(quiet_config(agent), "timezone", "") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - 时区名非法就回落本地
            pass
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def quiet_now(agent: Any, now: datetime | None = None) -> datetime:
    """把 ``now`` 规范成带时区的时间（缺省取当前时刻）。"""
    if now is None:
        return datetime.now(quiet_timezone(agent))
    if now.tzinfo is None:
        return now.replace(tzinfo=quiet_timezone(agent))
    return now


def global_quiet_active(agent: Any, now: datetime | None = None) -> bool:
    """全局 ``[quiet]`` 是否生效。"""
    if not quiet_enabled(agent):
        return False
    window = quiet_window(agent)
    if not window:
        return False
    return quiet_hours_active(window, quiet_now(agent, now))


def quiet_window_active(
    agent: Any, ctx: Any = None, now: datetime | None = None
) -> bool:
    """全局静默 **或** 群级静默是否生效（任一生效即静默）。"""
    if global_quiet_active(agent, now):
        return True
    if ctx is None:
        return False
    checker = getattr(agent, "group_quiet_hours_active", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(ctx))
    except Exception:  # noqa: BLE001 - 群策略取不到就不按静默处理
        return False


def _actor_is_superadmin(agent: Any, ctx: Any) -> bool:
    actor, ok = actor_from_context(ctx)
    if ok and getattr(actor, "role", "") == RoleSuperadmin:
        return True
    reader = getattr(agent, "actor", None)
    if ctx is not None and callable(reader):
        try:
            return getattr(reader(ctx), "role", "") == RoleSuperadmin
        except Exception:  # noqa: BLE001
            return False
    return False


def admit(
    agent: Any, ctx: Any, reason: str, now: datetime | None = None
) -> Admission:
    """判定这一次调用能不能发生。

    静默期规则（默认值，均可在 ``[quiet]`` 里改）：

    * 超级管理员触发 → 允许（``allow_superadmin``）；
    * cron 报告 → 跳过（``allow_cron`` 可放行）；
    * 外部事件 / 记忆学习 / 主动参与 → 跳过（``allow_external`` 可放行）；
    * 用户直聊 → 允许（``allow_direct`` 可关掉）。
    """
    reason = normalize_reason(reason)
    if not quiet_window_active(agent, ctx, now):
        return Admission(reason=reason)
    config = quiet_config(agent)
    if _actor_is_superadmin(agent, ctx) and bool(
        getattr(config, "allow_superadmin", True)
    ):
        return Admission(reason=reason, quiet=True)
    if reason == CRON and bool(getattr(config, "allow_cron", False)):
        return Admission(reason=reason, quiet=True)
    if reason in (EXTERNAL_EVENT, MEMORY_LEARNING) and bool(
        getattr(config, "allow_external", False)
    ):
        return Admission(reason=reason, quiet=True)
    if reason == DIRECT_MESSAGE and bool(getattr(config, "allow_direct", True)):
        return Admission(reason=reason, quiet=True)
    if reason not in _SKIPPABLE:
        return Admission(reason=reason, quiet=True)
    return Admission(
        decision=ADMIT_SKIP,
        reason=reason,
        quiet=True,
        detail=(
            f"当前处于静默时段（{quiet_window(agent) or '群级静默'}），"
            f"已跳过 {reason} 调用；不会补跑。"
        ),
    )


def background_reason(kind: str) -> str:
    """后台运行种类 → 调用原因；不需要静默准入的种类返回空串。"""
    return _BACKGROUND_REASONS.get(str(kind or "").strip(), "")


def skip_audit_event(reason: str) -> str:
    """被静默跳过时写的审计事件名。"""
    if reason == CRON:
        return "cron_skipped_quiet"
    return "external_skipped_quiet"


__all__ = [name for name in dir() if not name.startswith("_")]
