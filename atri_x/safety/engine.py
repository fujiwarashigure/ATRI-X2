"""内容安全引擎：本地层 + 远程层 + 三档裁决 + 双供应商 + 熔断 + 缓存 + 预算。

它是 1.1.8 新增的"总控"，但**不替代** :class:`atri_x.safety.guard.SafetyGuard`：
guard 仍是所有集成点的唯一入口，引擎只提供三件事：

1. **热加载**：``safety.toml`` + 用户词表/正则 + scope 覆盖，按文件指纹变更自动重建；
2. **远程审核**：按阶段策略调用腾讯云/阿里云，双供应商仲裁、失败策略、缓存、熔断；
3. **裁决**：把本地命中与远程建议合并成 ``block`` / ``review`` / ``pass`` 三档，
   并按 scope/工具覆盖决定"发 / hold / 进队列"。

线程模型：所有 I/O 都是 ``async``；重建与缓存用一把 ``threading.Lock`` 保护（同步快路径）。
**绝不抛给主链路**：任何异常都降级成 ``None``（= 只有本地判定）+ 计数 + 日志。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from atri_x.safety.normalize import normalize_skeleton
from atri_x.safety.engine_remote import (
    RemoteModerationMixin,
    RemoteOutcome,
    _ProviderHealth,
    _Tally,
    _cache_key,
    _scope_key,
)
from atri_x.safety.providers import build_providers
from atri_x.safety.providers.base import ModerationProvider, ProviderError
from atri_x.safety.review_queue import ReviewQueue
from atri_x.safety.rules import (
    RuleSet,
    load_custom_patterns,
    load_user_wordlists,
)
from atri_x.safety.scope_policy import ScopePolicies, ScopeSettings
from atri_x.safety.settings import (
    SafetySettings,
    fail_mode_for,
    load_safety_settings,
    local_policy,
    resolve_config_path,
    safety_file_path,
    stage_policy,
)
from atri_x.safety.types import (
    FAIL_CLOSED,
    STAGE_IMAGE,
    STAGE_POLICIES,
    STAGE_POLICY_CACHE_ONLY,
    STAGE_POLICY_LOCAL_ONLY,
    STAGE_POLICY_OFF,
    STAGE_POLICY_SYNC,
    TIER_BLOCK,
    TIER_PASS,
    TIER_REVIEW,
    Verdict,
    most_severe,
)

__all__ = [
    "RemoteOutcome",
    "SafetyEngine",
    "build_engine",
    "safety_dirs",
]

def safety_dirs(config_dir: str, settings: SafetySettings | None = None) -> dict[str, str]:
    """``safety.toml`` 引用的目录/文件绝对路径。"""
    base = (config_dir or "").strip()
    local = getattr(settings, "local", None)
    wordlist_dir = resolve_config_path(
        base, getattr(local, "wordlist_dir", "safety/wordlists") or "safety/wordlists"
    )
    sensitive_dir = resolve_config_path(
        base, getattr(local, "sensitive_dir", "safety/sensitive") or "safety/sensitive"
    )
    scopes_dir = resolve_config_path(base, "safety/scopes")
    audit_path = resolve_config_path(base, getattr(local, "audit_path", "") or "")
    return {
        "config": safety_file_path(base),
        "wordlists": wordlist_dir,
        "sensitive": sensitive_dir,
        "scopes": scopes_dir,
        "audit": audit_path,
        "review_db": resolve_config_path(base, "safety/review.db"),
    }


class SafetyEngine(RemoteModerationMixin):
    """内容安全总控（一份实例挂在一个 Agent 上，进程内共享）。"""

    def __init__(
        self,
        *,
        config_dir: str = "",
        logger: Any = None,
        transport: Any = None,
        clock: Any = None,
        now: Any = None,
    ) -> None:
        self.config_dir = (config_dir or "").strip()
        self.logger = logger
        self._transport = transport
        self._clock = clock or time.monotonic
        self._now = now
        self._lock = threading.Lock()
        self._settings: SafetySettings | None = None
        self._policy = None
        self._rules: RuleSet | None = None
        self._scopes = ScopePolicies()
        self._providers: dict[str, ModerationProvider] = {}
        self._health: dict[str, _ProviderHealth] = {}
        self._cache: OrderedDict[str, tuple[float, RemoteOutcome]] = OrderedDict()
        self._signature: tuple[Any, ...] = ()
        self._loaded_at = 0.0
        self._reload_errors: list[str] = []
        self._tally = _Tally()
        self._cost_day = ""
        self._cost_today = 0.0
        self.review = ReviewQueue(
            safety_dirs(self.config_dir)["review_db"], logger=logger, now=now
        )
        self.refresh(force=True)

    # -- 状态 ---------------------------------------------------------------
    @property
    def active(self) -> bool:
        """``safety.toml`` 存在且本地/远程至少一层启用。"""
        settings = self._settings
        if settings is None:
            return False
        return bool(settings.local.enabled or settings.remote.enabled)

    @property
    def settings(self) -> SafetySettings:
        assert self._settings is not None
        return self._settings

    @property
    def policy(self) -> Any:
        """本地层的不可变策略（guard 用它决定 block_reply / 模式）。"""
        return self._policy

    @property
    def rules(self) -> RuleSet:
        assert self._rules is not None
        return self._rules

    @property
    def providers(self) -> dict[str, ModerationProvider]:
        return dict(self._providers)

    @property
    def reload_errors(self) -> list[str]:
        return list(self._reload_errors)

    def rules_for(self, mode: str = "") -> RuleSet:
        """按会话模式取规则集（1.1.8 起 ``safety.toml`` 不分模式，参数保留兼容）。"""
        _ = mode
        return self.rules

    def policy_for(self, mode: str = "") -> Any:
        _ = mode
        return self.policy

    def scope_for(self, platform: str, scope_id: str) -> ScopeSettings | None:
        self.refresh()
        return self._scopes.match(platform, scope_id)

    def fail_mode(self, scope_key: str = "") -> str:
        """失败策略：scope 文件显式配置 > 全局 ``fail_close_scopes`` 名单 > 全局默认。"""
        self.refresh()
        key = (scope_key or "").strip()
        platform, _, scope_id = key.partition(":")
        scope = self._scopes.match(platform, scope_id) if platform or scope_id else None
        if scope is not None and scope.fail_mode:
            value = scope.fail_mode.strip().lower()
            if value in (FAIL_CLOSED, "open"):
                return value
        return fail_mode_for(self.settings, key)

    # -- 热加载 -------------------------------------------------------------
    def _file_signature(self) -> tuple[Any, ...]:
        dirs = safety_dirs(self.config_dir)
        return (
            _signature(dirs["config"]),
            _dir_signature(dirs["wordlists"], ".txt"),
            _dir_signature(dirs["sensitive"], ".regex"),
            self._scopes.signature(dirs["scopes"]),
            dirs["wordlists"],
            dirs["sensitive"],
            dirs["scopes"],
        )

    def refresh(self, *, force: bool = False) -> bool:
        """指纹变了就重建；``force=True`` 强制重建。返回"是否真的重建了"。"""
        now = self._clock()
        with self._lock:
            if not force and self._settings is not None:
                if now - self._loaded_at < 1.0:
                    return False
                try:
                    signature = self._file_signature()
                except OSError:
                    signature = ()
                if signature == self._signature:
                    self._loaded_at = now
                    return False
            self._loaded_at = now
            self._rebuild()
            return True

    def _rebuild(self) -> None:
        dirs = safety_dirs(self.config_dir)
        settings, errors = load_safety_settings(dirs["config"])
        self._reload_errors = errors
        if errors:
            # 坏配置：保留上一份可用策略（首次则用默认，等于没开新引擎）。
            self._warn("safety.toml 解析失败，保留上一份可用策略", "; ".join(errors))
            if self._settings is not None:
                return
        try:
            signature = self._file_signature()
        except OSError:
            signature = ()
        self._signature = signature
        self._settings = settings
        policy = local_policy(settings)
        extra_terms = load_user_wordlists(dirs["wordlists"])
        custom = load_custom_patterns(dirs["sensitive"])
        self._policy = policy
        self._rules = RuleSet(
            policy,
            extra_terms=extra_terms,
            custom_patterns=custom,
            normalize=bool(settings.local.normalize),
        )
        self._scopes = ScopePolicies.load(dirs["scopes"], logger=self.logger)
        for message in self._rules.warnings:
            self._warn(message, "")
        for message in self._scopes.errors:
            self._warn("scope 策略：%s" % message, "")
        self._providers = build_providers(
            settings,
            config_dir=self.config_dir,
            transport=self._transport,
        )
        self._health = {name: _ProviderHealth() for name in self._providers}
        if settings.remote.enabled and not self._providers:
            self._warn(
                "远程审核已开启但没有可用供应商（检查 *.env 凭据）", "providers=0"
            )
        if settings.local.enabled and not policy.active and not settings.remote.enabled:
            self._warn("safety.toml 本地层未生效且远程层关闭", "")

    # -- 裁决辅助 -----------------------------------------------------------
    def stage_policy(self, stage: str, tool: str = "", scope: ScopeSettings | None = None) -> str:
        self.refresh()
        if scope is not None:
            forced = scope.tool_policy(tool)
            if forced:
                return forced
        value = stage_policy(self.settings, stage, tool)
        return value if value in STAGE_POLICIES else STAGE_POLICY_LOCAL_ONLY

    def images_mode(self, scope: ScopeSettings | None = None) -> str:
        self.refresh()
        if scope is not None and scope.images:
            value = scope.images.strip().lower()
            if value in ("sync", "async", "off"):
                return value
        return self.settings.media.images

    def resolve_tier(self, tier: str, *, scope: ScopeSettings | None = None) -> str:
        """把远程档位按配置映射成实际处置档位。"""
        self.refresh()
        name = (tier or TIER_PASS).strip().lower()
        if name == TIER_BLOCK:
            mapped = self.settings.tiers.remote_block
        elif name == TIER_REVIEW:
            mapped = self.settings.tiers.remote_review
        else:
            return TIER_PASS
        if scope is not None:
            key = "remote_block" if name == TIER_BLOCK else "remote_review"
            mapped = scope.tiers.get(key, mapped)
        return mapped if mapped in (TIER_BLOCK, TIER_REVIEW, TIER_PASS) else TIER_PASS

    def should_hold(self, tier: str, *, scope: ScopeSettings | None = None) -> bool:
        """``review`` 档是否要"先压住等人工"（而不是照发/照送）。"""
        if (tier or "").strip().lower() != TIER_REVIEW:
            return False
        self.refresh()
        scope_key = scope.key if scope is not None else ""
        if scope_key and scope_key in self.settings.tiers.high_risk_scopes_hold:
            return True
        if scope is not None:
            inbound = scope.tiers.get("inbound_review", "")
            outbound = scope.tiers.get("outbound_review", "")
            if inbound == "hold" or outbound == "hold":
                return True
        return False

    # -- 复核队列 -----------------------------------------------------------
    def enqueue_review(
        self,
        *,
        stage: str,
        tier: str,
        action_taken: str,
        platform: str = "",
        scope_id: str = "",
        actor: str = "",
        provider: str = "",
        request_id: str = "",
        labels: object = (),
        score: float | None = None,
        reason: str = "",
        sha256: str = "",
        preview: str = "",
        payload_ref: str = "",
    ) -> str:
        """入队一条待人工复核事件（队列不可用时返回空串，不影响主流程）。"""
        item_id = self.review.add(
            stage=stage,
            tier=tier,
            action_taken=action_taken,
            platform=platform,
            scope_id=scope_id,
            actor=actor,
            provider=provider,
            request_id=request_id,
            labels=labels,
            score=score,
            reason=reason,
            sha256=sha256,
            preview=preview,
            payload_ref=payload_ref,
        )
        if item_id:
            self._tally.reviews_enqueued += 1
        return item_id

    # -- 统计 ---------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self._tally.__dict__)
        out.update(
            {
                "active": self.active,
                "providers": sorted(self._providers),
                "cache_entries": len(self._cache),
                "cost_today_cny": round(self._cost_today, 4),
                "review": self.review.stats(),
                "reload_errors": len(self._reload_errors),
            }
        )
        return out

    def notify_local_hit(self) -> None:
        self._tally.local_hits += 1

    # -- 日志 ---------------------------------------------------------------
    def _warn(self, message: str, error: str) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            warn = getattr(logger, "warning", None) or getattr(logger, "warn", None)
            if callable(warn):
                warn(message, error=error)
        except Exception:  # noqa: BLE001 - 日志坏了不能影响判定
            pass


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------
def _signature(path: str) -> tuple[int, int] | None:
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def _dir_signature(directory: str, suffix: str) -> tuple[tuple[str, int, int], ...]:
    base = (directory or "").strip()
    if not base or not os.path.isdir(base):
        return ()
    out: list[tuple[str, int, int]] = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ()
    for filename in names:
        if not filename.lower().endswith(suffix):
            continue
        path = os.path.join(base, filename)
        try:
            info = os.stat(path)
        except OSError:
            continue
        out.append((filename, int(info.st_mtime_ns), int(info.st_size)))
    return tuple(out)


def build_engine(
    *,
    config_dir: str = "",
    logger: Any = None,
    transport: Any = None,
    now: Any = None,
) -> SafetyEngine | None:
    """构造引擎；没有 ``safety.toml`` 时返回 ``None``（= 完全走 1.1.7 的旧路径）。"""
    base = (config_dir or "").strip()
    path = safety_file_path(base)
    if not path or not os.path.isfile(path):
        return None
    return SafetyEngine(config_dir=base, logger=logger, transport=transport, now=now)
