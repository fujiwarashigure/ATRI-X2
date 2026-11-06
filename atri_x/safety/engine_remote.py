"""远程审核的编排：双供应商仲裁、失败策略、预算、缓存、熔断。

从 :mod:`atri_x.safety.engine` 拆出来的原因：``engine.py`` 会被行数棘轮卡住（只降不升），
而"本地层 + 热加载 + 裁决"与"远程编排"是两块独立职责。:class:`RemoteModerationMixin`
只依赖宿主对象上的几个属性（``settings`` / ``_providers`` / ``_clock`` / ``_tally`` /
``_cache`` / ``_health``），不自己建连接、不读配置。

**不做什么**：不读 ``safety.toml``、不装配供应商（那是 ``engine`` 的活）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from atri_x.safety.providers.base import ModerationProvider, ProviderError
from atri_x.safety.scope_policy import ScopeSettings
from atri_x.safety.settings import SafetySettings
from atri_x.safety.types import (
    FAIL_CLOSED,
    STAGE_IMAGE,
    STAGE_POLICY_CACHE_ONLY,
    STAGE_POLICY_LOCAL_ONLY,
    STAGE_POLICY_OFF,
    TIER_BLOCK,
    TIER_PASS,
    TIER_REVIEW,
    Verdict,
    most_severe,
)

__all__ = [
    "RemoteModerationMixin",
    "RemoteOutcome",
]

#: 缓存条目上限的兜底（``[remote].cache_max_entries`` 为 0 时用它）。
_DEFAULT_CACHE_ENTRIES = 2000

#: 熔断：连续失败多少次、冷却多久（秒）。
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN = 60.0
#: 认证类错误冷却更久：重试只会刷日志烧配额。
_BREAKER_AUTH_COOLDOWN = 600.0

#: 估算成本时"一次文本调用"按多少字符折算成 1 个计费单位。
_COST_CHARS_PER_UNIT = 1000


@dataclass(frozen=True)
class RemoteOutcome:
    """远程审核的最终仲裁结果（引擎产出的唯一远程值对象）。"""

    tier: str = TIER_PASS
    labels: tuple[str, ...] = ()
    score: float | None = None
    request_id: str = ""
    provider: str = ""
    providers: tuple[str, ...] = ()
    degraded: bool = False
    fail_mode: str = "open"
    latency_ms: int = 0
    error: str = ""


@dataclass
class _ProviderHealth:
    failures: int = 0
    open_until: float = 0.0
    auth_open_until: float = 0.0


@dataclass
class _Tally:
    local_hits: int = 0
    remote_calls: int = 0
    remote_block: int = 0
    remote_review: int = 0
    remote_pass: int = 0
    remote_errors: int = 0
    fail_open: int = 0
    fail_closed: int = 0
    cache_hits: int = 0
    budget_skips: int = 0
    reviews_enqueued: int = 0
    cost_units: int = 0


class RemoteModerationMixin:
    """远程审核编排（宿主需提供 ``settings`` / ``_providers`` / ``_clock`` 等属性）。"""

    # -- 远程审核 -----------------------------------------------------------
    async def moderate_text(
        self,
        text: str,
        *,
        stage: str,
        platform: str = "",
        scope_id: str = "",
        tool: str = "",
        biz_type: str = "",
    ) -> RemoteOutcome | None:
        """文本远程审核；返回 ``None`` 表示"这个阶段不调远程"。"""
        content = text or ""
        if not content.strip():
            return None
        scope = self.scope_for(platform, scope_id)
        policy = self.stage_policy(stage, tool, scope)
        if policy in (STAGE_POLICY_LOCAL_ONLY, STAGE_POLICY_OFF):
            return None
        key = _cache_key("text\x00" + content)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        if policy == STAGE_POLICY_CACHE_ONLY:
            return None
        if not self.settings.remote.enabled:
            return None
        if not self._providers:
            return self._fail_outcome(_scope_key(platform, scope_id), "no_provider", stage)
        if self._budget_exceeded():
            overage = self.budget_overage_outcome(scope, stage)
            if overage is not None:
                return overage
            return None
        submit = self._redact_for_submit(content)
        outcome = await self._run_providers(
            lambda provider: provider.moderate_text(submit, biz_type=biz_type),
            stage=stage,
            scope=scope,
            scope_key=_scope_key(platform, scope_id),
            kind="text",
        )
        self._record_cost(len(submit))
        self._cache_put(key, outcome)
        self._tally.remote_calls += 1
        return outcome

    async def moderate_image(
        self,
        data: bytes = b"",
        *,
        mime: str = "",
        url: str = "",
        stage: str = STAGE_IMAGE,
        platform: str = "",
        scope_id: str = "",
        tool: str = "",
        actor: str = "",
    ) -> RemoteOutcome | None:
        """图片远程审核；``data`` 与 ``url`` 至少给一个。

        ``actor`` 命中 ``[media].skip_actors`` 时直接放行（只走本地层）——用于
        "只审非白名单用户"的成本策略。
        """
        if actor and actor in self.settings.media.skip_actors:
            return None
        scope = self.scope_for(platform, scope_id)
        if self.images_mode(scope) == "off":
            return None
        policy = self.stage_policy(stage, tool, scope)
        if policy in (STAGE_POLICY_LOCAL_ONLY, STAGE_POLICY_OFF):
            return None
        if not data and not url:
            return None
        key = _cache_key("image:data:" + hashlib.sha256(data).hexdigest() if data else "image:url:" + url)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        if policy == STAGE_POLICY_CACHE_ONLY:
            return None
        if not self.settings.remote.enabled:
            return None
        if not self._providers:
            return self._fail_outcome(_scope_key(platform, scope_id), "no_provider", stage)
        if self._budget_exceeded():
            overage = self.budget_overage_outcome(scope, stage)
            if overage is not None:
                return overage
            return None
        outcome = await self._run_providers(
            lambda provider: provider.moderate_image(data, mime=mime, url=url),
            stage=stage,
            scope=scope,
            scope_key=_scope_key(platform, scope_id),
            kind="image",
        )
        self._record_cost(len(data) // max(1, self.settings.media.image_max_bytes) or 1)
        self._cache_put(key, outcome)
        self._tally.remote_calls += 1
        return outcome

    async def _run_providers(
        self,
        call: Any,
        *,
        stage: str,
        scope: ScopeSettings | None,
        scope_key: str,
        kind: str,
    ) -> RemoteOutcome:
        """按顺序调用供应商，按 quorum 仲裁；全部失败走失败策略。"""
        remote = self.settings.remote
        names = [name for name in remote.providers if name in self._providers]
        started = self._clock()
        verdicts: list[Verdict] = []
        errors: list[str] = []
        tried: list[str] = []
        quorum = (remote.quorum or "any_block").strip().lower()
        for name in names:
            if quorum == "primary_only" and verdicts:
                break
            if self._breaker_open(name):
                errors.append(f"{name}:circuit_open")
                continue
            tried.append(name)
            provider = self._providers[name]
            result = await self._call_with_retry(name, provider, call, errors)
            if result is None:
                continue
            verdicts.append(result)
            self._mark_success(name)
            if quorum == "any_block" and result.tier == TIER_BLOCK:
                break
        latency_ms = int(max(0.0, self._clock() - started) * 1000)
        if not verdicts:
            self._tally.remote_errors += 1
            if self.settings.remote.degraded_alert:
                self._warn("远程审核全部失败，按失败策略处置", "; ".join(errors[:3]))
            fallback = self._fail_outcome(scope_key, "; ".join(errors[:2]) or "no_provider", stage)
            return RemoteOutcome(
                tier=fallback.tier,
                labels=fallback.labels,
                providers=tuple(tried),
                degraded=True,
                fail_mode=fallback.fail_mode,
                latency_ms=latency_ms,
                error=fallback.error,
            )
        tier = _aggregate([item.tier for item in verdicts], quorum)
        primary = next((item for item in verdicts if item.tier == tier), verdicts[0])
        labels: list[str] = []
        for item in verdicts:
            for label in item.labels:
                if label not in labels:
                    labels.append(label)
        outcome = RemoteOutcome(
            tier=tier,
            labels=tuple(labels),
            score=primary.score,
            request_id=primary.request_id,
            provider=primary.provider,
            providers=tuple(tried),
            degraded=bool(errors),
            fail_mode=self.fail_mode(scope_key),
            latency_ms=latency_ms,
            error="; ".join(errors[:2]),
        )
        if tier == TIER_BLOCK:
            self._tally.remote_block += 1
        elif tier == TIER_REVIEW:
            self._tally.remote_review += 1
        else:
            self._tally.remote_pass += 1
        return outcome

    async def _call_with_retry(
        self,
        name: str,
        provider: ModerationProvider,
        call: Any,
        errors: list[str],
    ) -> Verdict | None:
        remote = self.settings.remote
        attempts = 1 + max(0, int(remote.retries))
        timeout = max(0.05, remote.timeout_ms / 1000.0)
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(call(provider), timeout=timeout)
            except ProviderError as exc:
                errors.append(f"{name}:{exc.code or type(exc).__name__}")
                if exc.auth:
                    self._open_breaker(name, auth=True)
                    return None
                if not exc.retryable:
                    return None
            except asyncio.TimeoutError:
                errors.append(f"{name}:timeout")
            except Exception as exc:  # noqa: BLE001 - 供应商异常绝不外溢
                errors.append(f"{name}:{type(exc).__name__}")
            self._mark_failure(name)
            if attempt < attempts:
                await asyncio.sleep(0.05 * attempt)
        return None

    def _fail_outcome(self, scope_key: str, reason: str, stage: str) -> RemoteOutcome:
        """失败策略：``open`` 放行 + 告警；``closed`` 按 block 处置。"""
        mode = self.fail_mode(scope_key)
        if mode == FAIL_CLOSED:
            self._tally.fail_closed += 1
            return RemoteOutcome(
                tier=TIER_BLOCK,
                labels=("safety_fail_closed",),
                degraded=True,
                fail_mode=FAIL_CLOSED,
                error=reason or stage,
            )
        self._tally.fail_open += 1
        return RemoteOutcome(
            tier=TIER_PASS,
            labels=("safety_fail_open",),
            degraded=True,
            fail_mode="open",
            error=reason or stage,
        )

    # -- 成本 ---------------------------------------------------------------
    def _redact_for_submit(self, text: str) -> str:
        """送审前把 PII/凭据在本地脱敏（避免把密钥送到云厂商）。"""
        if not self.settings.remote.redact_before_submit or self._rules is None:
            return text
        try:
            filtered, _hits = self._rules.inspect(text)
            return filtered or text
        except Exception:  # noqa: BLE001 - 脱敏失败就原样送审
            return text

    def _cost_day_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _record_cost(self, chars: int) -> None:
        cost = self.settings.cost
        if cost.daily_budget_cny <= 0:
            return
        units = max(1, (max(0, chars) + _COST_CHARS_PER_UNIT - 1) // _COST_CHARS_PER_UNIT)
        day = self._cost_day_key()
        if day != self._cost_day:
            self._cost_day = day
            self._cost_today = 0.0
        self._cost_today += units * (cost.text_price_cny_per_10k / 10000.0)
        self._tally.cost_units += units

    def _budget_exceeded(self) -> bool:
        """当日预算是否已超（``daily_budget_cny <= 0`` 表示不限）。"""
        cost = self.settings.cost
        if cost.daily_budget_cny <= 0:
            return False
        day = self._cost_day_key()
        if day != self._cost_day:
            self._cost_day = day
            self._cost_today = 0.0
            return False
        return self._cost_today >= cost.daily_budget_cny

    def budget_exceeded(self) -> bool:
        """给状态页/命令用：当日预算是否已超。"""
        return self._budget_exceeded()

    def budget_overage_outcome(
        self, scope: ScopeSettings | None, stage: str
    ) -> RemoteOutcome | None:
        """预算超限时的处置：``fail_close`` → block；其余 → ``None``（不调远程）。

        返回 ``None`` 表示"跳过远程、本地层继续生效"（``local_only`` / ``pass_open``）。
        """
        if not self._budget_exceeded():
            return None
        self._tally.budget_skips += 1
        mode = (self.settings.cost.on_budget_exceeded or "local_only").strip().lower()
        if mode == "fail_close":
            self._tally.fail_closed += 1
            return RemoteOutcome(
                tier=TIER_BLOCK,
                labels=("safety_budget_exceeded",),
                degraded=True,
                fail_mode=FAIL_CLOSED,
                error="budget_exceeded",
            )
        return None

    # -- 缓存 ---------------------------------------------------------------
    def _cache_get(self, key: str) -> RemoteOutcome | None:
        ttl = self.settings.remote.cache_ttl_seconds
        if ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, outcome = entry
        if self._clock() - stored_at > ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        self._tally.cache_hits += 1
        return outcome

    def _cache_put(self, key: str, outcome: RemoteOutcome) -> None:
        ttl = self.settings.remote.cache_ttl_seconds
        limit = self.settings.remote.cache_max_entries or _DEFAULT_CACHE_ENTRIES
        if ttl <= 0 or limit <= 0 or outcome.degraded:
            return
        self._cache[key] = (self._clock(), outcome)
        self._cache.move_to_end(key)
        while len(self._cache) > limit:
            self._cache.popitem(last=False)

    # -- 熔断 ---------------------------------------------------------------
    def _health_for(self, name: str) -> _ProviderHealth:
        return self._health.setdefault(name, _ProviderHealth())

    def _breaker_open(self, name: str) -> bool:
        health = self._health_for(name)
        now = self._clock()
        if health.auth_open_until > now:
            return True
        return health.open_until > now

    def _mark_success(self, name: str) -> None:
        health = self._health_for(name)
        health.failures = 0
        health.open_until = 0.0

    def _mark_failure(self, name: str) -> None:
        health = self._health_for(name)
        health.failures += 1
        if health.failures >= _BREAKER_THRESHOLD:
            health.open_until = self._clock() + _BREAKER_COOLDOWN
            health.failures = 0

    def _open_breaker(self, name: str, *, auth: bool = False) -> None:
        health = self._health_for(name)
        now = self._clock()
        if auth:
            health.auth_open_until = now + _BREAKER_AUTH_COOLDOWN
        else:
            health.open_until = now + _BREAKER_COOLDOWN
        health.failures = 0


def _scope_key(platform: str, scope_id: str) -> str:
    """审计/失败策略用的 scope 键：``平台:scope``（平台为空时只有 scope）。"""
    p = (platform or "").strip().lower()
    s = (scope_id or "").strip()
    return "%s:%s" % (p, s) if p else s


def _cache_key(text: str) -> str:
    """缓存键只按内容（不含阶段）：同一段文本在不同阶段可以复用审核结论。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _aggregate(tiers: list[str], quorum: str) -> str:
    if not tiers:
        return TIER_PASS
    if quorum == "both_pass":
        return TIER_PASS if all(item == TIER_PASS for item in tiers) else most_severe(tiers)
    if quorum == "primary_only":
        return tiers[0]
    return most_severe(tiers)


