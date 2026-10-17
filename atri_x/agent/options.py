"""构造期依赖与配置容器，以及若干 setter。

对应 Go：``internal/agent/options.go``。

Go 的 ``nil`` 可选项 → Python 的 ``None``；``validateOptions`` 的错误 →
抛 :class:`~atri_x.errors.InvalidError`（Go 返回 ``error``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from atri_x.errors import InvalidError
from atri_x.security import default_policy
from atri_x.session import IdleExpirationConfig
from atri_x.storage import SessionModeWork

__all__ = [
    "Options",
    "session_idle_expiration_config",
    "validate_model_selection",
    "validate_options",
    "validate_optional_model_selection",
]


@dataclass
class Options:
    """Agent 构造期的依赖与配置集合。对应 Go ``Options``。"""

    platform: Any = None
    clients: dict[str, Any] = field(default_factory=dict)
    mode_models: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)
    state_path: str = ""
    context_overflow: dict[str, Any] = field(default_factory=dict)
    group_policy: dict[str, Any] = field(default_factory=dict)
    group_knowledge: Any = None
    group_services: Any = None
    store: Any = None
    media: Any = None
    vision_describer: Any = None
    vision_selection: Any = None
    group_analysis_selection: Any = None
    vision_parallelism: int = 0
    vision_max_images: int = 0
    vision_budget: timedelta | None = None
    audio_transcriber: Any = None
    asr_selection: Any = None
    asr_parallelism: int = 0
    asr_max_segments: int = 0
    asr_max_audio_bytes: int = 0
    command_prefixes: list[str] = field(default_factory=lambda: ["/"])
    session_config: Any = None
    naming_selection: Any = None
    naming_notifier: Any = None
    soul_path: str = ""
    resident_memory_store: Any = None
    angel_memory: Any = None
    angel_memory_config: Any = None
    self_learning: Any = None
    character_store: Any = None
    llm_request_config: Any = None
    ops: Any = None
    budget_limits: Any = None
    pricing: Any = None
    hook_service: Any = None
    hook_manager: Any = None
    hook_runtime: Any = None
    output_manager: Any = None
    logs: Any = None
    tool_registry: Any = None
    file_backups: Any = None
    doctor: Any = None
    skills: Any = None
    tool_provider: Any = None
    security_policy: Any = None
    context_config: Any = None
    model_metadata: Any = None
    compact_model: Any = None
    session_list_page_size: int = 0
    cleanup_retention_days: int = 0
    media_retention_days: int = 0
    session_idle_expiration: Any = None
    sandbox_root: str = ""
    tools_config: Any = None
    tool_tags_path: str = ""
    tool_tags: Any = None
    model_profiles: dict[str, Any] = field(default_factory=dict)
    model_aliases: dict[str, str] = field(default_factory=dict)
    tool_profiles: dict[str, list[str]] = field(default_factory=dict)
    tool_aliases: dict[str, str] = field(default_factory=dict)
    image_profiles: dict[str, bool] = field(default_factory=dict)
    image_aliases: dict[str, str] = field(default_factory=dict)
    turn_directives: Any = None


def validate_options(opts: Options) -> None:
    """校验构造参数。对应 Go ``validateOptions``（失败抛 ``InvalidError``）。"""
    work_model = (opts.mode_models or {}).get(SessionModeWork)
    if (
        work_model is None
        or not getattr(work_model, "provider", "")
        or not getattr(work_model, "model", "")
    ):
        raise InvalidError("mode_models.work provider/model is required")
    if opts.store is None:
        raise InvalidError("store is required")
    if not opts.providers:
        raise InvalidError("at least one provider is required")
    for name in opts.providers:
        if (opts.clients or {}).get(name) is None:
            raise InvalidError(f"client not found for provider {name!r}")
    for mode, selection in (opts.mode_models or {}).items():
        validate_model_selection(f"mode_models.{mode}", selection, opts.providers)
    validate_optional_model_selection("naming_model", opts.naming_selection, opts.providers)
    validate_optional_model_selection("compact_model", opts.compact_model, opts.providers)
    validate_optional_model_selection("vision_model", opts.vision_selection, opts.providers)
    validate_optional_model_selection(
        "group_analysis_model", opts.group_analysis_selection, opts.providers
    )
    if opts.session_config is None or not getattr(opts.session_config, "default_mode", ""):
        raise InvalidError("session default mode is required")
    if opts.session_list_page_size <= 0:
        raise InvalidError("session list page size must be positive")
    if opts.cleanup_retention_days <= 0:
        raise InvalidError("cleanup retention days must be positive")
    if not (opts.sandbox_root or "").strip():
        raise InvalidError("sandbox root is required")
    if opts.tools_config is None or getattr(opts.tools_config, "max_rounds_per_turn", 0) <= 0:
        raise InvalidError("tools max rounds per turn must be positive")
    if opts.security_policy is None:
        raise InvalidError("security policy is required")


def validate_optional_model_selection(
    name: str, selection: Any, providers: dict[str, Any]
) -> None:
    """可缺省的模型选择校验。对应 Go ``validateOptionalModelSelection``。"""
    if selection is None:
        return
    provider = getattr(selection, "provider", "")
    model = getattr(selection, "model", "")
    if not provider and not model:
        return
    validate_model_selection(name, selection, providers)


def validate_model_selection(name: str, selection: Any, providers: dict[str, Any]) -> None:
    """模型选择校验。对应 Go ``validateModelSelection``。"""
    provider = getattr(selection, "provider", "")
    model = getattr(selection, "model", "")
    if not provider or not model:
        raise InvalidError(f"{name} provider/model must both be set")
    if provider not in (providers or {}):
        raise InvalidError(f"{name} provider {provider!r} not found")


# ---------------------------------------------------------------------------
# setter（对应 Go options.go 下半部分）
# ---------------------------------------------------------------------------


def set_session_list_page_size(agent: Any, size: int) -> None:
    """对应 Go ``Agent.SetSessionListPageSize``。"""
    if size <= 0:
        from atri_x.config import default as default_config

        size = default_config().view.session_list_page_size
    agent.session_commands.set_list_page_size(size)


def set_cleanup_retention_days(agent: Any, days: int) -> None:
    """对应 Go ``Agent.SetCleanupRetentionDays``。"""
    if days <= 0:
        days = 30
    agent.session_commands.set_retention_days(days)


def set_session_idle_expiration(agent: Any, cfg: Any) -> None:
    """对应 Go ``Agent.SetSessionIdleExpiration``。"""
    agent.idle_expiration = session_idle_expiration_config(cfg)


def session_idle_expiration_config(cfg: Any) -> IdleExpirationConfig:
    """``config.SessionIdleExpirationConfig`` → ``session.IdleExpirationConfig``。

    对应 Go ``sessionIdleExpirationConfig``。
    """
    if cfg is None:
        return IdleExpirationConfig()
    return IdleExpirationConfig(
        group_user_ttl_minutes=int(getattr(cfg, "group_user_ttl_minutes", 0) or 0),
        group_superadmin_ttl_minutes=int(
            getattr(cfg, "group_superadmin_ttl_minutes", 0) or 0
        ),
        private_user_ttl_minutes=int(getattr(cfg, "private_user_ttl_minutes", 0) or 0),
        private_superadmin_ttl_minutes=int(
            getattr(cfg, "private_superadmin_ttl_minutes", 0) or 0
        ),
    )


def set_sandbox_root(agent: Any, root: str) -> None:
    """对应 Go ``Agent.SetSandboxRoot``。"""
    cleaned = os.path.normpath(root) if (root or "").strip() else ""
    if cleaned in ("", "."):
        from atri_x.config import default as default_config

        cleaned = default_config().sandbox.root
    agent.sandbox_root = cleaned


def set_security_policy(agent: Any, policy: Any) -> None:
    """对应 Go ``Agent.SetSecurityPolicy``。"""
    agent.security_policy = policy if policy is not None else default_policy()
