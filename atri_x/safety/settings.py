"""用户可编辑的策略文件 ``safety.toml``：结构、装载与归一化。

为什么不放 ``app.toml``：``atri_x/config/app.py`` 与 ``app_sections.py`` 已被
``scripts/line_budget_baseline.json`` 冻结（只降不升），加字段必须先去别处腾行数；
而这类"用户要经常改、改完要热加载"的策略本来就该是独立文件。
解码复用 :class:`atri_x.config.inspect.StrictConfig`（与 ``tool_tags.toml`` 同一套）。

三条硬约束：

1. **文件缺失 = 全默认 = 不改变既有行为**（远程层默认关闭）；
2. **坏配置不静默失效**：解析失败时返回"全默认 + 错误列表"，由调用方保留上一份可用策略；
3. **未知键忽略**（``strict=False``）：用户手写 ``safety.toml`` 时把拼错的键当成致命错误
   体验太差，改由 :func:`unknown_keys_hint` 在日志里提示。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.config import StrictConfig, load_toml

from atri_x.safety.policy import SafetyPolicy, policy_from_section
from atri_x.safety.types import (
    FAIL_CLOSED,
    FAIL_MODES,
    FAIL_OPEN,
    STAGE_POLICIES,
    STAGE_POLICY_LOCAL_ONLY,
    STAGE_POLICY_SYNC,
    STAGES,
)

__all__ = [
    "DEFAULT_AUDIT_FILE_NAME",
    "DEFAULT_SAFETY_FILE",
    "CostSettings",
    "LocalSettings",
    "MediaSettings",
    "ProviderSettings",
    "RemoteSettings",
    "SafetySettings",
    "TierSettings",
    "default_settings",
    "fail_mode_for",
    "load_safety_settings",
    "local_policy",
    "resolve_config_path",
    "safety_file_path",
    "stage_policy",
    "stages_defaults",
    "tools_defaults",
]

DEFAULT_SAFETY_FILE = "safety.toml"
DEFAULT_AUDIT_FILE_NAME = "safety-audit.jsonl"

#: 阶段 → 远程策略的默认值（对应设计稿 §3.3）。
_STAGE_DEFAULTS: dict[str, str] = {
    "inbound": STAGE_POLICY_SYNC,
    "llm": STAGE_POLICY_SYNC,
    "send": STAGE_POLICY_SYNC,
    "tool": "cache_only",
    "image_prompt": STAGE_POLICY_LOCAL_ONLY,
    "image": STAGE_POLICY_SYNC,
    "audio": STAGE_POLICY_SYNC,
    "video": STAGE_POLICY_SYNC,
}

#: 工具 → 远程策略的默认值（``[tools]`` 段可覆盖）。
_TOOL_DEFAULTS: dict[str, str] = {
    "web_search": STAGE_POLICY_LOCAL_ONLY,
    "web_extract": STAGE_POLICY_LOCAL_ONLY,
    "image_generate": STAGE_POLICY_SYNC,
    "comic_render": STAGE_POLICY_SYNC,
    "send_file": STAGE_POLICY_SYNC,
}


def stages_defaults() -> dict[str, str]:
    """阶段默认远程策略的副本。"""
    return dict(_STAGE_DEFAULTS)


def tools_defaults() -> dict[str, str]:
    """工具默认远程策略的副本。"""
    return dict(_TOOL_DEFAULTS)


@dataclass
class LocalSettings(StrictConfig):
    """本地规则层（沿用 1.0.3 起的 ``[safety]`` 语义，字段名保持一致）。"""

    enabled: bool = True
    mode: str = "enforce"
    categories: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)
    sensitive_actions: dict[str, str] = field(default_factory=dict)
    allowlist: list[str] = field(default_factory=list)
    block_reply: str = ""
    audit_path: str = ""
    audit_preview_chars: int = 120
    strip_tool_images: bool = False
    block_image_output: bool = False
    block_file_output: bool = False
    max_scan_chars: int = 8000
    #: 匹配前做归一化（NFKC / 去零宽 / 去 CJK 间分隔符 / 大小写折叠）。
    normalize: bool = True
    #: 用户词表目录（相对 ``config_dir``）。
    wordlist_dir: str = "safety/wordlists"
    #: 用户自定义敏感信息正则目录（相对 ``config_dir``）。
    sensitive_dir: str = "safety/sensitive"


@dataclass
class ProviderSettings(StrictConfig):
    """单个云厂商的凭据与端点。

    ``*_env`` 是**环境变量名**，不是密钥本身：密钥放 ``.env`` 或系统环境变量，
    绝不进 ``safety.toml``/仓库（``scripts/check_secrets.py`` 会扫）。
    """

    enabled: bool = True
    region: str = ""
    endpoint: str = ""
    service: str = ""
    version: str = ""
    biz_type: str = ""
    timeout_ms: int = 0
    secret_id_env: str = ""
    secret_key_env: str = ""
    access_key_id_env: str = ""
    access_key_secret_env: str = ""


@dataclass
class RemoteSettings(StrictConfig):
    """远程审核层（默认关闭）。"""

    enabled: bool = False
    providers: list[str] = field(default_factory=lambda: ["tencent", "aliyun"])
    quorum: str = "any_block"
    timeout_ms: int = 800
    retries: int = 1
    fail_mode: str = FAIL_OPEN
    fail_close_scopes: list[str] = field(default_factory=list)
    degraded_alert: bool = True
    cache_ttl_seconds: int = 600
    cache_max_entries: int = 2000
    redact_before_submit: bool = True
    stages: dict[str, str] = field(default_factory=dict)
    tencent: ProviderSettings = field(default_factory=ProviderSettings)
    aliyun: ProviderSettings = field(default_factory=ProviderSettings)


@dataclass
class TierSettings(StrictConfig):
    """档位映射：判定结果 → 处置。"""

    local_wordlist: str = "block"
    local_sensitive_redact: str = "redact"
    remote_block: str = "block"
    remote_review: str = "review"
    inbound_review: str = "pass_and_queue"
    outbound_review: str = "send_and_queue"
    high_risk_scopes_hold: list[str] = field(default_factory=list)


@dataclass
class MediaSettings(StrictConfig):
    """多模态分路。"""

    images: str = "sync"
    image_max_bytes: int = 4 * 1024 * 1024
    image_base64_max_bytes: int = 1024 * 1024
    object_store: str = ""
    audio: str = "async"
    video: str = "async"
    async_poll_seconds: int = 60
    quarantine_ttl_seconds: int = 3600


@dataclass
class CostSettings(StrictConfig):
    """成本与预算熔断。"""

    daily_budget_cny: float = 0.0
    on_budget_exceeded: str = "local_only"
    text_price_cny_per_10k: float = 22.0


@dataclass
class SafetySettings(StrictConfig):
    """整个 ``safety.toml``。"""

    version: int = 1
    local: LocalSettings = field(default_factory=LocalSettings)
    remote: RemoteSettings = field(default_factory=RemoteSettings)
    tiers: TierSettings = field(default_factory=TierSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    cost: CostSettings = field(default_factory=CostSettings)
    tools: dict[str, str] = field(default_factory=dict)


def default_settings() -> SafetySettings:
    """全默认设置（= 1.1.7 行为 + 远程层关闭）。"""
    return SafetySettings()


def safety_file_path(config_dir: str) -> str:
    """``<config_dir>/safety.toml``；``config_dir`` 为空时返回裸文件名。"""
    base = (config_dir or "").strip()
    return os.path.join(base, DEFAULT_SAFETY_FILE) if base else DEFAULT_SAFETY_FILE


def resolve_config_path(config_dir: str, value: str) -> str:
    """把 ``safety.toml`` 里的相对路径解析成绝对路径（绝对路径原样返回）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if os.path.isabs(text):
        return os.path.normpath(text)
    base = (config_dir or "").strip()
    return os.path.normpath(os.path.join(base, text)) if base else os.path.normpath(text)


def _clean_stage_map(raw: Mapping[str, Any] | None, defaults: Mapping[str, str]) -> dict[str, str]:
    """把阶段/工具策略表归一：未知阶段忽略、非法策略回落默认。"""
    out = dict(defaults)
    for key, value in (raw or {}).items():
        name = str(key or "").strip().lower()
        policy = str(value or "").strip().lower()
        if not name or policy not in STAGE_POLICIES:
            continue
        out[name] = policy
    return out


def _sanitize(settings: SafetySettings) -> SafetySettings:
    """补齐/纠正字段（非法枚举回落安全值，与 ``SafetyConfig.normalized`` 同风格）。"""
    local = settings.local
    mode = (local.mode or "").strip().lower()
    if mode not in ("off", "audit", "enforce"):
        mode = "audit"
    local.mode = mode
    if local.audit_preview_chars <= 0:
        local.audit_preview_chars = 120
    if local.max_scan_chars <= 0:
        local.max_scan_chars = 8000
    local.wordlist_dir = (local.wordlist_dir or "").strip() or "safety/wordlists"
    local.sensitive_dir = (local.sensitive_dir or "").strip() or "safety/sensitive"
    local.categories = [str(item).strip().lower() for item in local.categories if str(item).strip()]
    local.sensitive = [str(item).strip().lower() for item in local.sensitive if str(item).strip()]
    local.allowlist = [str(item) for item in local.allowlist if str(item).strip()]

    remote = settings.remote
    if (remote.fail_mode or "").strip().lower() not in FAIL_MODES:
        remote.fail_mode = FAIL_OPEN
    else:
        remote.fail_mode = remote.fail_mode.strip().lower()
    if remote.timeout_ms <= 0:
        remote.timeout_ms = 800
    if remote.retries < 0:
        remote.retries = 0
    if remote.cache_max_entries < 0:
        remote.cache_max_entries = 0
    if remote.cache_ttl_seconds < 0:
        remote.cache_ttl_seconds = 0
    names = [str(item).strip().lower() for item in remote.providers if str(item).strip()]
    remote.providers = names or ["tencent", "aliyun"]
    if (remote.quorum or "").strip().lower() not in ("any_block", "both_pass", "primary_only"):
        remote.quorum = "any_block"
    remote.fail_close_scopes = [
        str(item).strip() for item in remote.fail_close_scopes if str(item).strip()
    ]
    remote.stages = _clean_stage_map(remote.stages, _STAGE_DEFAULTS)

    tiers = settings.tiers
    if (tiers.local_wordlist or "").strip().lower() not in ("block", "review"):
        tiers.local_wordlist = "block"
    for name in ("remote_block", "remote_review"):
        value = (getattr(tiers, name) or "").strip().lower()
        if value not in ("block", "review", "pass"):
            setattr(tiers, name, "block" if name == "remote_block" else "review")
    for name in ("inbound_review", "outbound_review"):
        value = (getattr(tiers, name) or "").strip().lower()
        if value not in ("hold", "pass_and_queue", "send_and_queue"):
            setattr(tiers, name, "hold" if name == "inbound_review" else "send_and_queue")

    media = settings.media
    if (media.images or "").strip().lower() not in ("sync", "async", "off"):
        media.images = "sync"
    else:
        media.images = media.images.strip().lower()
    for name in ("image_max_bytes", "image_base64_max_bytes"):
        if getattr(media, name) <= 0:
            setattr(media, name, 4 * 1024 * 1024 if name == "image_max_bytes" else 1024 * 1024)
    for name in ("audio", "video"):
        if (getattr(media, name) or "").strip().lower() not in ("sync", "async", "off"):
            setattr(media, name, "async")

    cost = settings.cost
    if (cost.on_budget_exceeded or "").strip().lower() not in ("local_only", "pass_open", "fail_close"):
        cost.on_budget_exceeded = "local_only"
    if cost.daily_budget_cny < 0:
        cost.daily_budget_cny = 0.0
    if cost.text_price_cny_per_10k < 0:
        cost.text_price_cny_per_10k = 0.0

    settings.tools = _clean_stage_map(settings.tools, _TOOL_DEFAULTS)
    return settings


def load_safety_settings(path: str | os.PathLike[str] | None) -> tuple[SafetySettings, list[str]]:
    """读 ``safety.toml``，返回 ``(设置, 错误列表)``。

    * 路径为空或文件不存在 → ``(全默认, [])``，**不报错**（等于没开这个功能）；
    * 解析/解码失败 → ``(全默认, [错误])``；调用方应保留上一份可用设置；
    * 未知键 → 追加一条提示（不阻断）。
    """
    errors: list[str] = []
    if not path:
        return default_settings(), errors
    if not os.path.isfile(path):
        return default_settings(), errors
    try:
        decoded = load_toml(path, SafetySettings, strict=False)
    except Exception as exc:  # noqa: BLE001 - 坏配置只能降级，不能拖垮服务
        return default_settings(), [f"{os.path.basename(str(path))}: {exc}"]
    if not isinstance(decoded, SafetySettings):  # pragma: no cover - load_toml 保证类型
        return default_settings(), ["safety.toml: 解码结果类型异常"]
    return _sanitize(decoded), errors


def local_policy(settings: SafetySettings) -> SafetyPolicy:
    """``[local]`` → 不可变 :class:`SafetyPolicy`（复用 1.0.3 的鸭子类型转换）。"""
    section = {
        "enabled": settings.local.enabled,
        "mode": settings.local.mode,
        "categories": list(settings.local.categories),
        "sensitive": list(settings.local.sensitive),
        "sensitive_actions": dict(settings.local.sensitive_actions),
        "allowlist": list(settings.local.allowlist),
        "block_reply": settings.local.block_reply,
        "audit_path": settings.local.audit_path,
        "audit_preview_chars": settings.local.audit_preview_chars,
        "strip_tool_images": settings.local.strip_tool_images,
        "block_image_output": settings.local.block_image_output,
        "block_file_output": settings.local.block_file_output,
        "max_scan_chars": settings.local.max_scan_chars,
    }
    return policy_from_section(section)


def stage_policy(settings: SafetySettings, stage: str, tool: str = "") -> str:
    """查某个阶段（可带工具名）的远程策略：工具覆盖 > 阶段默认。

    ``tool`` 为空时用 ``stage`` 去 ``[tools]`` 里找（便于"按工具名当阶段名"的调用）。
    """
    name = (tool or stage or "").strip().lower()
    if name:
        value = settings.tools.get(name, "")
        if value:
            return value
    return settings.remote.stages.get((stage or "").strip().lower(), STAGE_POLICY_LOCAL_ONLY)


def fail_mode_for(settings: SafetySettings, scope_key: str = "") -> str:
    """某个 scope 的失败策略：命中 ``fail_close_scopes`` 时 close，否则取全局默认。"""
    key = (scope_key or "").strip()
    if key and key in settings.remote.fail_close_scopes:
        return FAIL_CLOSED
    return settings.remote.fail_mode or FAIL_OPEN


def unknown_keys_hint(sample: Mapping[str, Any] | None = None) -> str:
    """给日志用的一句话提示（``load_toml`` 的 ``strict=False`` 不返回未知键）。"""
    _ = sample
    return "safety.toml 里存在未知键时会被忽略；可用 `python -m atri_x --check-config` 排查"


#: 供调用方判断"这个阶段名是否认识"。
KNOWN_STAGES: tuple[str, ...] = STAGES
