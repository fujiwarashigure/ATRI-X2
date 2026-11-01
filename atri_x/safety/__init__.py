"""内置内容安全闸门：本地词表/正则 + 远程审核 + 三档裁决 + 人工复核。

1.0.2 的外挂 PoC（Node Hook 插件）在 1.0.3 内置化；1.1.8 起升级成五层管道：

* :mod:`atri_x.safety.settings` —— 用户可编辑的 ``safety.toml``（热加载）；
* :mod:`atri_x.safety.rules` —— 内置 + 用户词表、敏感信息正则、自定义正则；
* :mod:`atri_x.safety.engine` —— 远程审核（腾讯云 / 阿里云）、双供应商仲裁、熔断、
  缓存、预算、按 scope 覆盖、复核队列；
* :mod:`atri_x.safety.guard` —— **所有集成点的唯一入口**（同步本地判定 + 异步三档裁决）；
* :mod:`atri_x.safety.audit` —— ``safety-audit.jsonl``（v2 留痕，不存原文）。

典型装配（见 ``docs/safety.md``）::

    engine = build_engine(config_dir=config_dir, logger=agent.logger)   # 无 safety.toml → None
    guard = SafetyGuard(policy, config_dir=config_dir, engine=engine)
    agent.safety_guard = guard

    text, decision = await guard.afilter_text(text, stage="send", scope=scope_from_host(agent, ctx))
    if decision.action == "block":
        ...
    elif decision.action == "review" and decision.hold:
        ...

``guard is None`` / ``mode = off`` / 没有 ``safety.toml`` 时所有集成点都**完全无行为变化**：
不配置就与 1.0.2 一致。审计只存 ``sha256`` + 脱敏预览 + 标签 + 档位 + 供应商 +
请求 id + 分数 + 阶段 + 平台/scope/actor + 时间，绝不把原文二次落盘。
"""

from __future__ import annotations

from atri_x.safety.audit import AuditLog, utc_now
from atri_x.safety.engine import RemoteOutcome, SafetyEngine, build_engine, safety_dirs
from atri_x.safety.guard import (
    ACTION_REVIEW,
    DEFAULT_BLOCK_REPLACEMENT,
    MEDIA_BLOCK_TEXT,
    REVIEW_HOLD_TEXT,
    TOOL_IMAGE_STRIPPED_TEXT,
    SafetyDecision,
    SafetyGuard,
)
from atri_x.safety.mode_ctx import (
    SESSION_MODES,
    ModeBoundGuard,
    SafetyScope,
    guard_from_context,
    mode_from_host,
    scope_from_context,
    scope_from_host,
    with_safety_guard,
    with_safety_scope,
)
from atri_x.safety.policy import (
    ACTION_BLOCK,
    ACTION_FLAG,
    ACTION_PASS,
    ACTION_REDACT,
    DEFAULT_AUDIT_FILE_NAME,
    DEFAULT_BLOCK_REPLY,
    OVERRIDE_FIELDS,
    SafetyMode,
    SafetyPolicy,
    merge_policy,
    policies_from_section,
    policy_from_section,
)
from atri_x.safety.review_queue import ReviewQueue
from atri_x.safety.rules import (
    CATEGORY_IDS,
    CATEGORY_LABELS,
    SENSITIVE_DEFAULT_ACTIONS,
    SENSITIVE_IDS,
    SENSITIVE_LABELS,
    RuleHit,
    RuleSet,
    available_wordlists,
    load_all_wordlists,
    load_custom_patterns,
    load_user_wordlists,
    load_wordlist,
    wordlist_path,
)
from atri_x.safety.scope_policy import ScopePolicies, ScopeSettings
from atri_x.safety.settings import (
    CostSettings,
    LocalSettings,
    MediaSettings,
    ProviderSettings,
    RemoteSettings,
    SafetySettings,
    TierSettings,
    default_settings,
    fail_mode_for,
    load_safety_settings,
    local_policy,
    safety_file_path,
    stage_policy,
)
from atri_x.safety.types import (
    FAIL_CLOSED,
    FAIL_OPEN,
    STAGE_INBOUND,
    STAGE_IMAGE,
    STAGE_LLM,
    STAGE_SEND,
    STAGE_TOOL,
    TIER_BLOCK,
    TIER_PASS,
    TIER_REVIEW,
    Verdict,
    most_severe,
    normalize_labels,
)

__all__ = [
    # 模式 / 动作 / 档位常量
    "ACTION_BLOCK",
    "ACTION_FLAG",
    "ACTION_PASS",
    "ACTION_REDACT",
    "ACTION_REVIEW",
    "DEFAULT_AUDIT_FILE_NAME",
    "DEFAULT_BLOCK_REPLY",
    "DEFAULT_BLOCK_REPLACEMENT",
    "MEDIA_BLOCK_TEXT",
    "REVIEW_HOLD_TEXT",
    "TOOL_IMAGE_STRIPPED_TEXT",
    "OVERRIDE_FIELDS",
    "SESSION_MODES",
    "STAGE_INBOUND",
    "STAGE_IMAGE",
    "STAGE_LLM",
    "STAGE_SEND",
    "STAGE_TOOL",
    "TIER_BLOCK",
    "TIER_PASS",
    "TIER_REVIEW",
    "FAIL_CLOSED",
    "FAIL_OPEN",
    # 契约 API
    "SafetyDecision",
    "SafetyGuard",
    "SafetyMode",
    "SafetyPolicy",
    "Verdict",
    "RemoteOutcome",
    "SafetyEngine",
    "build_engine",
    "ModeBoundGuard",
    "merge_policy",
    "mode_from_host",
    "most_severe",
    "normalize_labels",
    "policies_from_section",
    "policy_from_section",
    "safety_dirs",
    # 配置（safety.toml）
    "CostSettings",
    "LocalSettings",
    "MediaSettings",
    "ProviderSettings",
    "RemoteSettings",
    "SafetySettings",
    "TierSettings",
    "ScopePolicies",
    "ScopeSettings",
    "default_settings",
    "fail_mode_for",
    "load_safety_settings",
    "local_policy",
    "safety_file_path",
    "stage_policy",
    # 附带工具（审计 / 归属 / ctx 传递 / 复核队列）
    "AuditLog",
    "ReviewQueue",
    "SafetyScope",
    "guard_from_context",
    "scope_from_context",
    "scope_from_host",
    "utc_now",
    "with_safety_guard",
    "with_safety_scope",
    # 词表
    "CATEGORY_IDS",
    "CATEGORY_LABELS",
    "SENSITIVE_DEFAULT_ACTIONS",
    "SENSITIVE_IDS",
    "SENSITIVE_LABELS",
    "RuleHit",
    "RuleSet",
    "available_wordlists",
    "load_all_wordlists",
    "load_custom_patterns",
    "load_user_wordlists",
    "load_wordlist",
    "wordlist_path",
]
