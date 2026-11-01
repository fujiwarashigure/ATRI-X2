"""guard 的异步三档接口（从 :mod:`atri_x.safety.guard` 抽出）。

拆出来的原因：``guard.py`` 被行数棘轮冻结（只降不升），而 1.1.8 的异步/远程逻辑又必须
挂在同一个判定入口上。:class:`atri_x.safety.guard.SafetyGuard` 保留两个薄转发方法，
实现全部在这里；这样 ``guard.py`` 只增加几十行，而这块可以独立测试。

**不做什么**：不决定"要不要审"（那是引擎的 stage 策略），只负责"把本地判定和远程结论
合并成 block / review / pass"。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from atri_x.safety.mode_ctx import _coerce_scope
from atri_x.safety.policy import (
    ACTION_BLOCK,
    ACTION_PASS,
    ACTION_REDACT,
    SafetyPolicy,
)
from atri_x.safety.guard import (
    ACTION_REVIEW,
    DEFAULT_BLOCK_REPLACEMENT,
    MEDIA_BLOCK_TEXT,
    SafetyDecision,
)
from atri_x.safety.types import TIER_BLOCK, TIER_PASS, TIER_REVIEW, most_severe

__all__ = ["aallow_media_output", "afilter_text"]

    # -- 异步（1.1.8：本地层 + 远程层 + 三档裁决） ---------------------------
async def afilter_text(
    guard: Any,
    text: str,
    *,
    stage: str,
    scope: Any = None,
    block_text: str = "",
    meta: Mapping[str, Any] | None = None,
    mode: Any = None,
    tool: str = "",
    platform: str = "",
    scope_id: str = "",
) -> tuple[str, SafetyDecision]:
    """``filter_text`` 的异步版本：本地层判定后再问远程层，最后按三档裁决。

    返回值契约与 :meth:`filter_text` 一致（``audit`` / ``off`` 下永远返回原文），
    但 ``decision`` 额外带 ``tier`` / ``provider`` / ``request_id`` / ``score`` /
    ``degraded`` / ``hold``，供调用方决定"发 / 压住 / 进队列"。
    """
    original = text or ""
    policy, rules = guard._variant(mode)
    engine = guard.engine
    engine_active = engine is not None and bool(getattr(engine, "active", False))
    if not policy.active and not engine_active:
        return original, SafetyDecision(allowed=True, text=original, action=ACTION_PASS)
    if policy.active:
        inspected, hits = rules.inspect(original)
        decision = guard._decide(original, inspected, hits, block_text=block_text)
        if hits:
            guard._notify_local()
    else:
        decision = SafetyDecision(allowed=True, text=original, action=ACTION_PASS)
    scope_view = _coerce_scope(scope)
    if engine_active:
        decision = await _apply_remote_text(guard, 
            decision,
            original,
            policy=policy,
            engine=engine,
            stage=stage,
            platform=platform or scope_view.platform,
            scope_id=scope_id or scope_view.scope_id,
            tool=tool,
            block_text=block_text,
        )
    guard._record(decision, original, stage=stage, scope=scope, meta=meta, policy=policy)
    guard._tally(decision.action)
    if policy.enforcing and decision.action in (ACTION_BLOCK, ACTION_REDACT):
        return decision.text, decision
    return original, decision

async def _apply_remote_text(
    guard: Any,
    decision: SafetyDecision,
    original: str,
    *,
    policy: SafetyPolicy,
    engine: Any,
    stage: str,
    platform: str,
    scope_id: str,
    tool: str,
    block_text: str,
) -> SafetyDecision:
    """把远程结论并进本地判定（失败了就原样返回本地判定）。"""
    try:
        outcome = await engine.moderate_text(
            original, stage=stage, platform=platform, scope_id=scope_id, tool=tool
        )
    except Exception as exc:  # noqa: BLE001 - 远程层异常不能影响主链路
        guard._warn("远程审核不可用：%s: %s" % (type(exc).__name__, exc))
        return decision
    if outcome is None:
        return decision
    scope_settings = engine.scope_for(platform, scope_id)
    local_tier = decision.tier or (
        TIER_BLOCK if decision.action == ACTION_BLOCK else TIER_PASS
    )
    remote_tier = engine.resolve_tier(outcome.tier, scope=scope_settings)
    tier = most_severe([local_tier, remote_tier])
    labels = list(decision.labels)
    for label in outcome.labels:
        if label not in labels:
            labels.append(label)
    reason = decision.reason
    if outcome.tier != TIER_PASS:
        extra = "、".join(outcome.labels[:4]) or outcome.provider
        suffix = "远程：%s(%s)" % (outcome.provider or "?", extra)
        if outcome.score is not None:
            suffix += " %.2f" % outcome.score
        reason = (reason + "；" + suffix) if reason else suffix
    hold = bool(engine.should_hold(remote_tier, scope=scope_settings))
    common = {
        "labels": tuple(labels),
        "reason": reason,
        "provider": outcome.provider,
        "request_id": outcome.request_id,
        "score": outcome.score,
        "degraded": outcome.degraded,
        "fail_mode": outcome.fail_mode,
        "latency_ms": outcome.latency_ms,
    }
    if tier == TIER_BLOCK:
        text_out = (block_text or "").strip() or DEFAULT_BLOCK_REPLACEMENT
        return replace(
            decision,
            allowed=False,
            text=text_out,
            action=ACTION_BLOCK,
            tier=TIER_BLOCK,
            hold=hold,
            **common,
        )
    if tier == TIER_REVIEW:
        import hashlib as _hashlib

        _enqueue_review(
            guard,
            stage=stage,
            outcome=outcome,
            platform=platform,
            scope_id=scope_id,
            sha256=_hashlib.sha256(original.encode("utf-8")).hexdigest() if original else "",
            preview=guard.rules.mask(original)[:120],
        )
        return replace(
            decision,
            allowed=True,
            text=original,
            action=ACTION_REVIEW,
            tier=TIER_REVIEW,
            hold=hold,
            **common,
        )
    return replace(decision, tier=TIER_PASS, hold=False, **common)

async def aallow_media_output(
    guard: Any,
    kind: str,
    *,
    stage: str = "send",
    scope: Any = None,
    meta: Mapping[str, Any] | None = None,
    mode: Any = None,
    data: bytes = b"",
    mime: str = "",
    url: str = "",
    platform: str = "",
    scope_id: str = "",
    tool: str = "",
) -> SafetyDecision:
    """``allow_media_output`` 的异步版本：图片会额外走远程内容识别。

    ``kind = "image"`` 且引擎生效时，把图片字节（或 URL）送远程审核；命中 ``block``
    则 ``allowed = False``，命中 ``review`` 则 ``allowed = True`` 且 ``action = review``
    （``hold`` 为真时调用方应先压住）。
    """
    name = (kind or "").strip().lower()
    if name not in ("image", "file"):
        return SafetyDecision(allowed=True, text="", action=ACTION_PASS)
    policy = guard.policy_for(mode)
    engine = guard.engine
    engine_active = engine is not None and bool(getattr(engine, "active", False))
    if not policy.active and not engine_active:
        return SafetyDecision(allowed=True, text="", action=ACTION_PASS)
    decision = guard._local_media_decision(name, policy)
    scope_view = _coerce_scope(scope)
    if engine_active and name == "image":
        decision = await _apply_remote_image(guard, 
            decision,
            policy=policy,
            engine=engine,
            stage=stage,
            platform=platform or scope_view.platform,
            scope_id=scope_id or scope_view.scope_id,
            tool=tool,
            data=data,
            mime=mime,
            url=url,
        )
    if decision.action == ACTION_PASS:
        return decision
    payload = {"kind": name}
    for key, value in (meta or {}).items():
        if value not in (None, ""):
            payload[str(key)] = str(value)
    guard._record(decision, "", stage=stage, scope=scope, meta=payload, policy=policy)
    guard._tally(decision.action, media=decision.action == ACTION_BLOCK)
    return decision

async def _apply_remote_image(
    guard: Any,
    decision: SafetyDecision,
    *,
    policy: SafetyPolicy,
    engine: Any,
    stage: str,
    platform: str,
    scope_id: str,
    tool: str,
    data: bytes,
    mime: str,
    url: str,
) -> SafetyDecision:
    try:
        outcome = await engine.moderate_image(
            data,
            mime=mime,
            url=url,
            stage=stage,
            platform=platform,
            scope_id=scope_id,
            tool=tool,
        )
    except Exception as exc:  # noqa: BLE001
        guard._warn("远程图片审核不可用：%s: %s" % (type(exc).__name__, exc))
        return decision
    if outcome is None:
        return decision
    scope_settings = engine.scope_for(platform, scope_id)
    remote_tier = engine.resolve_tier(outcome.tier, scope=scope_settings)
    local_tier = decision.tier or (
        TIER_BLOCK if decision.action == ACTION_BLOCK else TIER_PASS
    )
    tier = most_severe([local_tier, remote_tier])
    labels = list(decision.labels)
    for label in outcome.labels:
        if label not in labels:
            labels.append(label)
    hold = bool(engine.should_hold(remote_tier, scope=scope_settings))
    note = MEDIA_BLOCK_TEXT["image"]
    common = {
        "labels": tuple(labels),
        "provider": outcome.provider,
        "request_id": outcome.request_id,
        "score": outcome.score,
        "degraded": outcome.degraded,
        "fail_mode": outcome.fail_mode,
        "latency_ms": outcome.latency_ms,
    }
    if tier == TIER_BLOCK:
        return replace(
            decision,
            allowed=False,
            text=note,
            action=ACTION_BLOCK,
            tier=TIER_BLOCK,
            reason="图片命中远程内容审核",
            hold=hold,
            **common,
        )
    if tier == TIER_REVIEW:
        import hashlib as _hashlib

        _enqueue_review(
            guard,
            stage=stage,
            outcome=outcome,
            platform=platform,
            scope_id=scope_id,
            sha256=_hashlib.sha256(data).hexdigest() if data else "",
        )
        return replace(
            decision,
            allowed=True,
            text=note,
            action=ACTION_REVIEW,
            tier=TIER_REVIEW,
            reason="图片待人工复核",
            hold=hold,
            **common,
        )
    return replace(decision, tier=TIER_PASS, **common)

# ---------------------------------------------------------------------------
# 人工复核入队（review 档的唯一入口）
# ---------------------------------------------------------------------------
def _enqueue_review(
    guard,
    *,
    stage: str,
    outcome,
    platform: str = "",
    scope_id: str = "",
    actor: str = "",
    sha256: str = "",
    preview: str = "",
    action_taken: str = "queued",
) -> None:
    """把一条 review 档事件写进人工复核队列（失败不影响主流程）。"""
    engine = getattr(guard, "engine", None)
    if engine is None:
        return
    try:
        engine.enqueue_review(
            stage=stage,
            tier="review",
            action_taken=action_taken,
            platform=platform,
            scope_id=scope_id,
            actor=actor,
            provider=outcome.provider,
            request_id=outcome.request_id,
            labels=outcome.labels,
            score=outcome.score,
            reason="远程审核命中，待人工复核",
            sha256=sha256,
            preview=preview,
        )
    except Exception:  # noqa: BLE001 - 队列写失败不能影响判定
        pass
