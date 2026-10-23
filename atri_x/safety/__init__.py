"""内置安全闸门：道德 / 合规词表过滤 + 敏感信息（PII / 凭据）脱敏。

对应 1.0.2 的外挂 PoC ``ATRI-X-safety-gate``（Node Hook 插件）——这里是
**原生 Python 内置化**，不再需要 Node 运行时、不再每个 Hook 点起一个子进程。

模块分工（单文件都 ≤ 600 行）：

* :mod:`atri_x.safety.policy` —— ``[safety]`` 段 → :class:`SafetyPolicy`（不可变）；
* :mod:`atri_x.safety.rules` —— 词表数据文件、词表匹配、敏感信息正则（独立实现）；
* :mod:`atri_x.safety.guard` —— :class:`SafetyGuard`（判定 / 脱敏 / 媒体出口 / 计数）；
* :mod:`atri_x.safety.audit` —— ``safety-audit.jsonl`` 追加写入（不存原文）。

典型用法（宿主装配，见 ``docs/safety.md``）::

    guard = SafetyGuard(policy_from_section(cfg.app.safety), logger=agent.logger,
                        config_dir=config_dir)
    agent.safety_guard = guard            # 集成点用 getattr(host, "safety_guard", None)

    text, decision = guard.filter_text(text, stage="output", scope=scope_from_host(agent, ctx))
    if decision.action == "block":
        ...

``guard is None`` 或 ``mode = off`` 时所有集成点都**完全无行为变化**：不配置就
与 1.0.2 一致。审计只存 ``sha256`` + 脱敏预览 + 标签 + 阶段 + 平台/scope/actor + 时间，
绝不把原文二次落盘。
"""

from __future__ import annotations

from atri_x.safety.audit import AuditLog, utc_now
from atri_x.safety.guard import (
    DEFAULT_BLOCK_REPLACEMENT,
    MEDIA_BLOCK_TEXT,
    SESSION_MODES,
    TOOL_IMAGE_STRIPPED_TEXT,
    ModeBoundGuard,
    SafetyDecision,
    SafetyGuard,
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
    load_wordlist,
    wordlist_path,
)

__all__ = [
    # 模式 / 动作常量
    "ACTION_BLOCK",
    "ACTION_FLAG",
    "ACTION_PASS",
    "ACTION_REDACT",
    "DEFAULT_AUDIT_FILE_NAME",
    "DEFAULT_BLOCK_REPLY",
    "DEFAULT_BLOCK_REPLACEMENT",
    "MEDIA_BLOCK_TEXT",
    "TOOL_IMAGE_STRIPPED_TEXT",
    "OVERRIDE_FIELDS",
    "SESSION_MODES",
    # 契约 API
    "SafetyDecision",
    "SafetyGuard",
    "SafetyMode",
    "SafetyPolicy",
    "ModeBoundGuard",
    "merge_policy",
    "mode_from_host",
    "policies_from_section",
    "policy_from_section",
    # 附带工具（审计 / 归属 / ctx 传递）
    "AuditLog",
    "SafetyScope",
    "guard_from_context",
    "scope_from_context",
    "scope_from_host",
    "utc_now",
    "with_safety_guard",
    "with_safety_scope",
    # 词表（数据文件在 atri_x/safety/wordlists/）
    "CATEGORY_IDS",
    "CATEGORY_LABELS",
    "SENSITIVE_DEFAULT_ACTIONS",
    "SENSITIVE_IDS",
    "SENSITIVE_LABELS",
    "RuleHit",
    "RuleSet",
    "available_wordlists",
    "load_all_wordlists",
    "load_wordlist",
    "wordlist_path",
]
