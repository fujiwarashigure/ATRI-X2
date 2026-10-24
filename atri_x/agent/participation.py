"""群聊参与状态机 MVP（评估 §D）：只做**抑制**，不主动找话题。

对应本仓《dsh-qq 借鉴落地评估》§D。设计边界：

* **per-scope 状态**：每个 ``platform:scope_id`` 一份
  :class:`ParticipationState`（最近直接互动、最近唤醒、未回应连续次数、
  主动参与冷却截止）；
* **默认关闭**：``[participation].guard = false`` 时所有函数都是 no-op，
  行为与 1.0.5 之前完全一致；
* **只做两件事**（评估里的"只做抑制"）：
  1. 主动参与冷却期内，**普通闲聊不再唤醒**（点名 / 回复 / 唤醒词仍然唤醒）；
  2. 连续无回应达到 ``no_reply_streak_max`` 后进入冷却，避免"自说自话"；
* **明确不做**：`readThroughSeq` / 未读浏览 / `set_wake_condition` 工具
  （需要新增群聊协议与工具面，属于更大的改动）。

状态只存在进程内存里（重启清空），不做持久化 —— 参与度是短周期信号，
持久化反而会把"昨天没人理"带到今天。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

#: 默认主动参与冷却（秒）。
DEFAULT_COOLDOWN_SECONDS = 120.0

#: 默认连续无回应上限。
DEFAULT_NO_REPLY_STREAK_MAX = 3


@dataclass
class ParticipationState:
    """一个群作用域的参与状态。"""

    last_message_at: float = 0.0
    last_direct_at: float = 0.0
    last_wake_at: float = 0.0
    last_reply_at: float = 0.0
    no_reply_streak: int = 0
    proactive_cooldown_until: float = 0.0
    last_batch_at: float = 0.0

    def cooldown_remaining(self, now: float) -> float:
        return max(0.0, self.proactive_cooldown_until - now)

    def to_payload(self, now: float) -> dict[str, Any]:
        return {
            "no_reply_streak": self.no_reply_streak,
            "cooldown_remaining": int(self.cooldown_remaining(now)),
            "last_direct_at": self.last_direct_at,
            "last_wake_at": self.last_wake_at,
            "last_reply_at": self.last_reply_at,
        }


def participation_config(agent: Any) -> Any:
    """取 Agent 上的 ``[participation]`` 配置；没有就返回 ``None``。"""
    return getattr(agent, "participation_config", None)


def guard_enabled(agent: Any) -> bool:
    """参与守卫是否启用（默认关闭）。"""
    return bool(getattr(participation_config(agent), "guard", False))


def cooldown_seconds(agent: Any) -> float:
    value = getattr(participation_config(agent), "cooldown_seconds", None)
    if value is None:
        return DEFAULT_COOLDOWN_SECONDS
    return max(0.0, float(value))


def streak_max(agent: Any) -> int:
    value = getattr(participation_config(agent), "no_reply_streak_max", None)
    if value is None:
        return DEFAULT_NO_REPLY_STREAK_MAX
    return max(0, int(value))


def scope_key(agent: Any, ctx: Any) -> str:
    """当前会话的作用域键（``<platform>:<scope_id>``）；取不到时返回空串。"""
    reader = getattr(agent, "scope", None)
    if ctx is None or not callable(reader):
        return ""
    try:
        scope = reader(ctx)
    except Exception:  # noqa: BLE001 - 取不到作用域就不记状态
        return ""
    platform = str(getattr(scope, "platform", "") or "").strip()
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip()
    if not scope_id:
        return ""
    return f"{platform}:{scope_id}" if platform else scope_id


def _lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "participation_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.participation_mu = lock
    return lock


def _states(agent: Any) -> dict[str, ParticipationState]:
    states = getattr(agent, "participation_states", None)
    if states is None:
        states = {}
        agent.participation_states = states
    return states


def state_for(agent: Any, ctx: Any, now: float | None = None) -> ParticipationState:
    """取（必要时创建）当前会话的参与状态。"""
    key = scope_key(agent, ctx)
    if key == "":
        return ParticipationState()
    with _lock(agent):
        states = _states(agent)
        state = states.get(key)
        if state is None:
            state = ParticipationState()
            states[key] = state
        return state


def note_inbound(
    agent: Any, ctx: Any, direct: bool, now: float | None = None
) -> ParticipationState:
    """记一条入站消息：``direct`` 表示点名 / 回复 / 唤醒词这类直接互动。"""
    now = time.monotonic() if now is None else now
    state = state_for(agent, ctx, now)
    state.last_message_at = now
    if direct:
        state.last_direct_at = now
        state.no_reply_streak = 0
        state.proactive_cooldown_until = 0.0
    return state


def note_wakeup(agent: Any, ctx: Any, now: float | None = None) -> None:
    """记一次"真的叫醒了模型"。"""
    if scope_key(agent, ctx) == "":
        return
    now = time.monotonic() if now is None else now
    state_for(agent, ctx, now).last_wake_at = now


def note_batch_finished(
    agent: Any, ctx: Any, replied: bool, now: float | None = None
) -> None:
    """记一次批次结束（回复了 / 没回复）。

    * 回复了 → 连击清零，并进入主动参与冷却（``cooldown_seconds``）；
    * 没回复 → 连击 +1，达到 ``no_reply_streak_max`` 时进入冷却。
    """
    if not guard_enabled(agent):
        return
    if scope_key(agent, ctx) == "":
        return
    now = time.monotonic() if now is None else now
    state = state_for(agent, ctx, now)
    state.last_batch_at = now
    if replied:
        state.last_reply_at = now
        state.no_reply_streak = 0
        state.proactive_cooldown_until = now + cooldown_seconds(agent)
        return
    state.no_reply_streak += 1
    limit = streak_max(agent)
    if limit > 0 and state.no_reply_streak >= limit:
        state.proactive_cooldown_until = now + cooldown_seconds(agent)


def cooldown_active(agent: Any, ctx: Any, now: float | None = None) -> bool:
    """当前会话是否处于主动参与冷却。"""
    if not guard_enabled(agent):
        return False
    if scope_key(agent, ctx) == "":
        return False
    now = time.monotonic() if now is None else now
    return state_for(agent, ctx, now).cooldown_remaining(now) > 0.0


def suppress_ambient_wakeup(
    agent: Any, ctx: Any, direct: bool, now: float | None = None
) -> bool:
    """这次唤醒是否应当被参与守卫压掉（只压普通闲聊，不压直接互动）。"""
    if direct:
        return False
    return cooldown_active(agent, ctx, now)


def state_snapshot(agent: Any, ctx: Any, now: float | None = None) -> dict[str, Any]:
    """给测试 / 诊断用的状态快照。"""
    now = time.monotonic() if now is None else now
    return state_for(agent, ctx, now).to_payload(now)


def reset(agent: Any, ctx: Any = None) -> int:
    """清空参与状态（``ctx`` 为空时清全部）；返回清掉的条数。"""
    with _lock(agent):
        states = _states(agent)
        if ctx is None:
            count = len(states)
            states.clear()
            return count
        key = scope_key(agent, ctx)
        if key and states.pop(key, None) is not None:
            return 1
        return 0


__all__ = [name for name in dir() if not name.startswith("_")]
