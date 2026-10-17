"""入站限流：用户桶 + 群桶、计数与可观测状态。

对应 Go：``internal/agent/ratelimit.go``。

判定顺序是**先用户、后群**：群配额保护整个群不被一个活跃成员吃光，用户配额则阻止单个
成员独占群配额。超级管理员直接放行（不被自己的群限流挡住管理操作）。

宿主（``Agent``）上的字段沿用 Go 的名字：``rate_limit_user`` / ``rate_limit_group``
（:class:`atri_x.ops.ratelimit.Limiter`）、``rate_limit_user_per_minute`` /
``rate_limit_user_burst`` / ``rate_limit_group_per_minute`` / ``rate_limit_group_burst``
（core.py 目前没有这几个字段，用 ``getattr`` 取默认值 0 = 不限流）。
计数与"最近一次拒绝"收进 :class:`RateLimitState`（``agent.rate_limit_state``）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.agent.model import AgentService, host_of, install_methods
from atri_x.ops.ratelimit import Limiter
from atri_x.platform import ConversationGroup, message_context_from
from atri_x.security import RoleSuperadmin

__all__ = [
    "RATE_LIMIT_METHODS",
    "RateLimitMixin",
    "RateLimitService",
    "RateLimitState",
    "RateLimitStatus",
    "allow_inbound_detailed",
    "ensure_rate_limit_state",
    "install_ratelimit_services",
    "is_group_context",
    "rate_limit_retry_text",
]


@dataclass
class RateLimitStatus:
    """入站限流的可观测视图。对应 Go ``RateLimitStatus``（JSON 键名照抄 Go tag）。"""

    allowed: int = 0
    rejected: int = 0
    user_rejected: int = 0
    group_rejected: int = 0
    keys: int = 0
    user_messages_per_minute: int = 0
    user_burst: int = 0
    group_messages_per_minute: int = 0
    group_burst: int = 0
    last_reason: str = ""
    last_rejected_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """按 Go 的 ``omitempty`` 语义输出。"""
        out: dict[str, Any] = {
            "allowed": self.allowed,
            "rejected": self.rejected,
            "user_rejected": self.user_rejected,
            "group_rejected": self.group_rejected,
            "keys": self.keys,
            "user_messages_per_minute": self.user_messages_per_minute,
            "user_burst": self.user_burst,
            "group_messages_per_minute": self.group_messages_per_minute,
            "group_burst": self.group_burst,
        }
        if self.last_reason:
            out["last_reason"] = self.last_reason
        if self.last_rejected_at is not None:
            out["last_rejected_at"] = self.last_rejected_at.isoformat()
        return out


@dataclass
class RateLimitState:
    """限流计数与最近一次拒绝。对应 Go ``rateLimitAllowed`` 等原子计数 + ``rateLimitMu``。"""

    allowed: int = 0
    rejected: int = 0
    user_rejected: int = 0
    group_rejected: int = 0
    last_reason: str = ""
    last_rejected_at: datetime | None = None
    mu: threading.Lock = field(default_factory=threading.Lock, repr=False)


def ensure_rate_limit_state(host: Any) -> RateLimitState:
    """取宿主的限流计数；缺失时补建。"""
    agent = host_of(host)
    state = getattr(agent, "rate_limit_state", None)
    if isinstance(state, RateLimitState):
        return state
    state = RateLimitState()
    agent.rate_limit_state = state
    return state


def is_group_context(host: Any, ctx: Any) -> bool:
    """当前是否群上下文。对应 Go ``isGroupContext``。"""
    message = message_context_from(ctx)
    if message is not None and getattr(message, "conversation_kind", "") == ConversationGroup:
        return True
    scope_id = str(getattr(host.scope(ctx), "platform_scope_id", "") or "").strip()
    return scope_id.startswith("group:") or scope_id.startswith("supergroup:")


def allow_inbound_detailed(host: Any, ctx: Any) -> tuple[bool, float, str]:
    """先用户、后群的限流判定。对应 Go ``allowInboundDetailed``。

    返回 ``(是否放行, 稍后重试秒数, 拒绝原因)``；原因取值 ``"user"`` / ``"group"`` / ``""``。
    """
    user_limiter = getattr(host, "rate_limit_user", None)
    group_limiter = getattr(host, "rate_limit_group", None)
    if user_limiter is None and group_limiter is None:
        return True, 0.0, ""
    actor = host.actor(ctx)
    if getattr(actor, "role", "") == RoleSuperadmin:
        return True, 0.0, ""
    scope = host.scope(ctx)
    platform_name = str(getattr(scope, "platform", "") or "").strip() or "unknown"
    actor_id = str(getattr(scope, "actor_id", "") or "").strip() or "unknown"

    if user_limiter is not None:
        decision = user_limiter.allow(platform_name + ":user:" + actor_id)
        if not decision.allowed:
            _record_rejection(host, "user", decision.retry_after)
            return False, float(decision.retry_after or 0.0), "user"
    if is_group_context(host, ctx) and group_limiter is not None:
        scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip() or "unknown"
        decision = group_limiter.allow(platform_name + ":group:" + scope_id)
        if not decision.allowed:
            _record_rejection(host, "group", decision.retry_after)
            return False, float(decision.retry_after or 0.0), "group"
    ensure_rate_limit_state(host).allowed += 1
    return True, 0.0, ""


def _record_rejection(host: Any, reason: str, retry_after: float) -> None:
    """对应 Go ``recordRateLimitRejection``。"""
    state = ensure_rate_limit_state(host)
    with state.mu:
        state.rejected += 1
        if reason == "user":
            state.user_rejected += 1
        elif reason == "group":
            state.group_rejected += 1
        state.last_reason = reason
        state.last_rejected_at = datetime.now(tz=timezone.utc)


def rate_limit_retry_text(retry_after: float) -> str:
    """限流提示文本。对应 Go ``rateLimitRetryText``。"""
    if not retry_after or retry_after <= 0:
        return "消息发送过于频繁，请稍后再试。"
    seconds = int(round(retry_after))
    if seconds < 1:
        seconds = 1
    return f"消息发送过于频繁，请在 {seconds} 秒后重试。"


class RateLimitMixin:
    """``internal/agent/ratelimit.go`` 里挂在 ``*Agent`` 上的方法。"""

    def allow_inbound(self, ctx: Any) -> tuple[bool, float]:
        """对应 Go ``allowInbound``（``(放行, 稍后重试秒数)``）。"""
        allowed, retry_after, _ = allow_inbound_detailed(host_of(self), ctx)
        return allowed, retry_after

    def allow_inbound_detailed(self, ctx: Any) -> tuple[bool, float, str]:
        """对应 Go ``allowInboundDetailed``。"""
        return allow_inbound_detailed(host_of(self), ctx)

    def is_group_context(self, ctx: Any) -> bool:
        """对应 Go ``isGroupContext``。"""
        return is_group_context(host_of(self), ctx)

    def rate_limit_retry_text(self, retry_after: float) -> str:
        """对应 Go ``rateLimitRetryText``。"""
        return rate_limit_retry_text(retry_after)

    def rate_limit_target(self, ctx: Any) -> tuple[Limiter | None, str]:
        """兼容入口：单个 key 的老选择逻辑。对应 Go ``rateLimitTarget``。"""
        host = host_of(self)
        scope = host.scope(ctx)
        platform_name = str(getattr(scope, "platform", "") or "").strip() or "unknown"
        if is_group_context(host, ctx) and getattr(host, "rate_limit_group", None) is not None:
            scope_id = str(getattr(scope, "platform_scope_id", "") or "")
            return host.rate_limit_group, platform_name + ":group:" + scope_id
        limiter = getattr(host, "rate_limit_user", None)
        if limiter is None:
            return None, ""
        actor_id = str(getattr(scope, "actor_id", "") or "").strip() or "unknown"
        return limiter, platform_name + ":private:" + actor_id

    def rate_limit_stats(self) -> tuple[int, int, int]:
        """聚合计数与跟踪的 key 数。对应 Go ``RateLimitStats``。"""
        host = host_of(self)
        state = ensure_rate_limit_state(host)
        keys = 0
        user_limiter = getattr(host, "rate_limit_user", None)
        if user_limiter is not None:
            keys += user_limiter.keys()
        group_limiter = getattr(host, "rate_limit_group", None)
        if group_limiter is not None:
            keys += group_limiter.keys()
        return state.allowed, state.rejected, keys

    def rate_limit_status(self) -> RateLimitStatus:
        """详细状态。对应 Go ``RateLimitStatus``。"""
        host = host_of(self)
        state = ensure_rate_limit_state(host)
        status = RateLimitStatus(
            allowed=state.allowed,
            rejected=state.rejected,
            user_rejected=state.user_rejected,
            group_rejected=state.group_rejected,
            user_messages_per_minute=int(
                getattr(host, "rate_limit_user_per_minute", 0) or 0
            ),
            user_burst=int(getattr(host, "rate_limit_user_burst", 0) or 0),
            group_messages_per_minute=int(
                getattr(host, "rate_limit_group_per_minute", 0) or 0
            ),
            group_burst=int(getattr(host, "rate_limit_group_burst", 0) or 0),
        )
        user_limiter = getattr(host, "rate_limit_user", None)
        if user_limiter is not None:
            status.keys += user_limiter.keys()
        group_limiter = getattr(host, "rate_limit_group", None)
        if group_limiter is not None:
            status.keys += group_limiter.keys()
        with state.mu:
            status.last_reason = state.last_reason
            status.last_rejected_at = state.last_rejected_at
        return status

    def attach_llm_retry_notifier(self, client: Any, provider_name: str) -> None:
        """重试通知（定义在 ``model.go``，但接缝登记在 ratelimit 服务下）。"""
        from atri_x.agent.model_agent import ModelMixin

        ModelMixin.attach_llm_retry_notifier(self, client, provider_name)


#: 可以装到 Agent 实例上的方法。
RATE_LIMIT_METHODS: tuple[str, ...] = (
    "allow_inbound",
    "allow_inbound_detailed",
    "is_group_context",
    "rate_limit_retry_text",
    "rate_limit_target",
    "rate_limit_stats",
    "rate_limit_status",
    "attach_llm_retry_notifier",
)


class RateLimitService(AgentService):
    """限流服务门面（``agent.services['ratelimit']``）。"""


def install_ratelimit_services(agent: Any) -> RateLimitService:
    """装配限流服务：装方法 + 注册门面（幂等）。"""
    install_methods(agent, RateLimitMixin, RATE_LIMIT_METHODS)
    ensure_rate_limit_state(agent)
    services = getattr(agent, "services", None)
    existing = services.get("ratelimit") if isinstance(services, dict) else None
    if isinstance(existing, RateLimitService):
        return existing
    service = RateLimitService(agent)
    if isinstance(services, dict):
        services["ratelimit"] = service
    return service
