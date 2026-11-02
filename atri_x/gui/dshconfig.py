"""内置 DSH 内核的桌面端配置（``app.toml`` 的 ``[agent.dsh]``）。

本模块只管"配置 + 诊断"三件事：

- :class:`DshSettings` + :func:`load_settings` / :func:`save_settings`：路由开关、
  凭据 Provider 与协议、内核模型、复杂度阈值、预热与回收；
- :func:`hint_for_provider` / :func:`suggest_anthropic_base_url`：判断某个
  ATRI-X Provider 能不能喂给内核。内核只会往 ``<base_url>/v1/messages`` 发请求
  （Anthropic Messages 协议），OpenAI 兼容网关必然 404 —— 这是"深度路由从没
  生效"的最常见原因，界面要能直接说清楚并给出建议地址；
- :func:`collect_status` / :func:`status_lines`：读 ``[agent.dsh]`` 之后的状态快照
  （不拉子进程），包括凭据是否可用与不可用原因。

内核**模型目录**补丁在 :mod:`atri_x.gui.dshcatalog`；端到端自检在
:mod:`atri_x.gui.dshsmoke`。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    patch_section,
    read_toml,
    verify_config,
)
from atri_x.gui.configmodel import load_providers, provider_key_state

__all__ = [
    "AUTO_PROVIDER_LABEL",
    "DSH_SECTION",
    "DshSettings",
    "PROTOCOLS",
    "PROTOCOL_LABELS",
    "available_providers",
    "collect_status",
    "hint_for_provider",
    "is_anthropic_base_url",
    "is_official_base_url",
    "load_settings",
    "make_model_value",
    "parse_model_value",
    "provider_is_usable_for_dsh",
    "provider_key_ready",
    "save_settings",
    "status_lines",
    "suggest_anthropic_base_url",
    "validate_settings",
]

#: ``app.toml`` 里的配置段名。
DSH_SECTION = "agent.dsh"

#: 内核模型目录里的 provider 名（内核自己的注册名，不是 ATRI-X 的 provider 名）。
DEFAULT_KERNEL_PROVIDER = "deepseek-official"

#: 协议取值（与 :mod:`atri_x.agent.dsh.credentials` 的常量同义）。
PROTOCOLS: tuple[str, ...] = ("auto", "anthropic", "openai", "off")

#: 协议 → 界面显示名（顺序即下拉顺序）。
PROTOCOL_LABELS: dict[str, str] = {
    "auto": "auto（自动判定：官方 DeepSeek / base_url 含 anthropic 才可用）",
    "anthropic": "anthropic（强制 Anthropic Messages：网关支持时用）",
    "openai": "openai（不可用：内核只说 Messages 协议）",
    "off": "off（关闭深度路由）",
}

#: 「凭据 Provider」下拉里的自动项。
AUTO_PROVIDER_LABEL = "（自动：优先名字含 deepseek 的 Provider）"


# ---------------------------------------------------------------------------
# [agent.dsh] 设置
# ---------------------------------------------------------------------------
@dataclass
class DshSettings:
    """``[agent.dsh]`` 在界面上可编辑的字段（默认值与 :class:`AgentDshConfig` 一致）。"""

    enabled: bool = True
    credential_provider: str = ""
    credential_protocol: str = "auto"
    #: ACP ``session/set_config_option`` 的取值：``["<内核provider>","<模型 id>"]`` 的 JSON 文本。
    model: str = ""
    min_score: float = 0.40
    prewarm: bool = True
    idle_timeout_seconds: float = 900.0
    timeout_seconds: float = 180.0
    session_affinity: bool = True
    max_concurrent_prompts: int = 2

    def model_pair(self) -> tuple[str, str]:
        """解析 :attr:`model`；格式不对时返回 ``("", "")``。"""
        return parse_model_value(self.model)

    def model_id(self) -> str:
        """当前选中的内核模型 id（没配置时为空串）。"""
        return self.model_pair()[1]

    def with_model(
        self, model_id: str, *, provider: str = DEFAULT_KERNEL_PROVIDER
    ) -> "DshSettings":
        """返回一份把内核模型换成 ``model_id`` 的副本（空串表示删掉该键）。"""
        text = "" if not (model_id or "").strip() else make_model_value(provider, model_id)
        return replace(self, model=text)


def parse_model_value(value: Any) -> tuple[str, str]:
    """``'["provider","model"]'`` → ``(provider, model)``；裸模型 id 也接受。"""
    text = str(value or "").strip()
    if not text:
        return "", ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return "", text
    if isinstance(parsed, (list, tuple)) and len(parsed) == 2:
        return str(parsed[0] or "").strip(), str(parsed[1] or "").strip()
    if isinstance(parsed, str):
        return "", parsed.strip()
    return "", ""


def make_model_value(provider: str, model: str) -> str:
    """把 ``(provider, model)`` 渲染成内核 ACP 取值（JSON 数组文本）。

    必须是**紧凑 JSON**（逗号后不带空格）：内核公布的可选值逐字符比对，
    ``'["deepseek-official", "x"]'`` 会被判 ``unknown model option``。
    """
    return json.dumps(
        [provider or DEFAULT_KERNEL_PROVIDER, model],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _section_raw(paths: ConfigPaths) -> Mapping[str, Any]:
    """读 ``app.toml`` 里 ``[agent.dsh]`` 原始表（没有时返回空表）。"""
    data = read_toml(paths.config_path)
    agent = data.get("agent")
    section = agent.get("dsh") if isinstance(agent, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def load_settings(paths: ConfigPaths) -> DshSettings:
    """从 ``app.toml`` 读 ``[agent.dsh]``；缺段/缺键都退回默认值。"""
    section = _section_raw(paths)
    defaults = DshSettings()
    if not section:
        return defaults
    return DshSettings(
        enabled=_as_bool(section.get("enabled"), defaults.enabled),
        credential_provider=_as_text(section.get("credential_provider")),
        credential_protocol=_as_protocol(section.get("credential_protocol")),
        model=_as_text(section.get("model")),
        min_score=_as_float(section.get("min_score"), defaults.min_score),
        prewarm=_as_bool(section.get("prewarm"), defaults.prewarm),
        idle_timeout_seconds=_as_float(
            section.get("idle_timeout_seconds"), defaults.idle_timeout_seconds
        ),
        timeout_seconds=_as_float(section.get("timeout_seconds"), defaults.timeout_seconds),
        session_affinity=_as_bool(section.get("session_affinity"), defaults.session_affinity),
        max_concurrent_prompts=_as_int(
            section.get("max_concurrent_prompts"), defaults.max_concurrent_prompts
        ),
    )


def validate_settings(settings: DshSettings) -> DshSettings:
    """校验界面输入，返回可写盘的值；非法时抛 :class:`ConfigEditError`。"""
    if not (0.0 <= settings.min_score <= 1.0):
        raise ConfigEditError(f"路由阈值要在 0~1 之间（当前 {settings.min_score}）")
    if settings.timeout_seconds <= 0:
        raise ConfigEditError("单轮超时必须是正数（秒）")
    if settings.idle_timeout_seconds < 0:
        raise ConfigEditError("空闲回收时间不能是负数（0 = 不回收）")
    if settings.max_concurrent_prompts < 1:
        raise ConfigEditError("并发上限至少 1")
    if settings.credential_protocol not in PROTOCOLS:
        raise ConfigEditError(f"未知协议：{settings.credential_protocol!r}")
    provider, model = settings.model_pair()
    if settings.model.strip() and not model:
        raise ConfigEditError('内核模型取值格式不对：应形如 ["deepseek-official","模型 id"]')
    if model and provider != DEFAULT_KERNEL_PROVIDER:
        raise ConfigEditError(
            f"内核模型里的 provider 应该是 {DEFAULT_KERNEL_PROVIDER!r}（内核自己的注册名），"
            f"当前是 {provider!r}"
        )
    if model:
        # 统一成内核公布值的**紧凑**写法（手改/旧版本可能写了带空格的 JSON）。
        return replace(settings, model=make_model_value(provider, model))
    return settings


def save_settings(paths: ConfigPaths, settings: DshSettings) -> bool:
    """写 ``[agent.dsh]``，返回是否改动；写完回读严格解码，配置坏了会抛错。"""
    settings = validate_settings(settings)
    provider, model = settings.model_pair()
    values: dict[str, Any] = {
        "enabled": bool(settings.enabled),
        "credential_provider": settings.credential_provider.strip() or None,
        "credential_protocol": settings.credential_protocol,
        "model": make_model_value(provider, model) if model else None,
        "min_score": round(float(settings.min_score), 4),
        "prewarm": bool(settings.prewarm),
        "idle_timeout_seconds": float(settings.idle_timeout_seconds),
        "timeout_seconds": float(settings.timeout_seconds),
        "session_affinity": bool(settings.session_affinity),
        "max_concurrent_prompts": int(settings.max_concurrent_prompts),
    }
    changed = patch_section(
        paths.config_path,
        DSH_SECTION,
        values,
        header_comment="内置 DSH 内核的深度路由（桌面端「DSH」页维护；改完重启后台服务生效）",
    )
    verify_config(paths)
    return changed


# ---------------------------------------------------------------------------
# Provider 侧信息（"用哪份凭据"）
# ---------------------------------------------------------------------------
def available_providers(paths: ConfigPaths) -> dict[str, Any]:
    """``services.toml`` 里的 provider 表（``name -> ProviderView``，保持文件顺序）。"""
    return load_providers(paths)


def provider_is_usable_for_dsh(view: Any) -> bool:
    """这个 provider 的 ``base_url`` 是否**看起来**能被内核接受。

    ``True`` 只表示"值得试"：官方 DeepSeek 主机、或 ``base_url`` 里带
    ``anthropic``。其余（OpenAI 兼容路径）需要用户改端点或显式强制协议。
    """
    base_url = _as_text(getattr(view, "base_url", ""))
    return is_official_base_url(base_url) or is_anthropic_base_url(base_url)


def provider_key_ready(paths: ConfigPaths, view: Any) -> bool:
    """该 provider 的密钥是否已配置（进程环境或 ``.env``）。"""
    try:
        return provider_key_state(paths, view).configured
    except ConfigEditError:
        return False


def suggest_anthropic_base_url(base_url: str) -> str:
    """把 OpenAI 兼容端点改成同一网关的 Anthropic Messages 路径（只是建议值）。

    ``https://host/x/openai/v1`` → ``https://host/x/anthropic``；
    已经是 anthropic 路径或官方端点时返回空串（无需改）。
    """
    text = _as_text(base_url)
    if not text or is_anthropic_base_url(text) or is_official_base_url(text):
        return ""
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    for suffix in ("/openai/v1", "/openai", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, (path + "/anthropic") or "/anthropic", "", ""))


def hint_for_provider(paths: ConfigPaths, name: str) -> str:
    """一句话诊断：该 provider 能不能喂给内核、缺什么、建议怎么改。"""
    providers = available_providers(paths)
    if not name:
        for candidate, view in providers.items():
            if provider_is_usable_for_dsh(view):
                name = candidate
                break
    view = providers.get(name) if name else None
    if view is None:
        return "未指定凭据 Provider：内核会退回自己 shell 里的凭据（通常没有），建议在下面选一个。"
    base_url = _as_text(view.base_url)
    key_state = provider_key_state(paths, view)
    pieces: list[str] = []
    if is_official_base_url(base_url):
        pieces.append(f"base_url 是官方 DeepSeek 端点 ✓（{base_url or '内核默认'}）")
    elif is_anthropic_base_url(base_url):
        pieces.append(f"base_url 是 Anthropic Messages 路径 ✓（{base_url}）")
    elif not base_url:
        pieces.append("没配 base_url：内核按官方 DeepSeek 端点走（只有在配了官方密钥时才对）")
    else:
        suggestion = suggest_anthropic_base_url(base_url)
        pieces.append(
            f"base_url ✗（{base_url}）不是 Anthropic Messages 端点，内核只会请求 "
            f"<base_url>/v1/messages（OpenAI 兼容网关必然 404）"
        )
        if suggestion:
            pieces.append(f"建议改成：{suggestion}")
        pieces.append("或把协议改成「anthropic」强制注入（仅当该网关确实支持 Messages 协议）")
    headers = getattr(view, "extra_headers", None) or {}
    if headers:
        pieces.append(f"声明了 {len(headers)} 个自定义请求头 → 内核经本地回环代理补齐（开箱可用）")
    if key_state.configured:
        pieces.append(f"密钥已配置（{view.api_key_env or '进程环境'}）")
    else:
        pieces.append(f"密钥未配置：{view.api_key_env or '未设置 api_key_env'}")
    return "；".join(pieces)


def is_official_base_url(base_url: str) -> bool:
    """``base_url`` 是否指向官方 DeepSeek 主机（主机名口径与凭据模块一致）。"""
    text = _as_text(base_url)
    if not text:
        return False
    try:
        from atri_x.agent.dsh.credentials import OFFICIAL_BASE_URL_HOSTS

        hosts = OFFICIAL_BASE_URL_HOSTS
    except Exception:  # noqa: BLE001 - 配置层独立可用：退回内置常量
        hosts = ("api.deepseek.com",)
    host = _as_text(urlsplit(text).hostname)
    return any(host == official or host.endswith("." + official) for official in hosts)


def is_anthropic_base_url(base_url: str) -> bool:
    """``base_url`` 是否显式声明自己是 Anthropic Messages 端点。"""
    return "anthropic" in _as_text(base_url).lower()


# ---------------------------------------------------------------------------
# 状态快照
# ---------------------------------------------------------------------------
def collect_status(config_path: str, scores: Sequence[str] = ()) -> dict[str, Any]:
    """收集 DSH 状态快照（纯函数式，不拉子进程）；失败转成 ``error`` 字段。"""
    try:
        from atri_x.agent.dsh.status import collect_status as _collect

        return _collect(config_path, scores)
    except Exception as exc:  # noqa: BLE001 - 状态页不因为自检崩掉
        return {"error": f"{type(exc).__name__}: {exc}", "loaded": False}


def status_lines(report: Mapping[str, Any]) -> list[str]:
    """把状态快照渲染成给用户看的几行文本。"""
    if report.get("error"):
        return [f"配置读取失败：{report['error']}"]
    lines: list[str] = []
    kernel = report.get("kernel") or {}
    if kernel.get("found"):
        lines.append(
            "内核：已找到（dsh {} / node {}，来源 {}）".format(
                kernel.get("dshVersion", "?"),
                kernel.get("nodeVersion", "?"),
                kernel.get("root", "?"),
            )
        )
    else:
        lines.append("内核：未找到（会退回 PATH 上的 dsh；内置内核缺失时深度路由不可用）")
    lines.append(
        "路由：{}，内核可用={}".format(
            "已启用" if report.get("enabled") else "未启用",
            "是" if report.get("available") else "否",
        )
    )
    credential = report.get("credential") or {}
    lines.append(
        "凭据：可用={}，协议={}，Provider={}".format(
            "是" if credential.get("usable") else "否",
            credential.get("protocol", "?"),
            credential.get("provider") or "（未指定）",
        )
    )
    if credential.get("reason"):
        lines.append(f"凭据原因：{credential['reason']}")
    if credential.get("gateway"):
        lines.append(
            "网关头代理：{}（补齐请求头 {}）".format(
                credential.get("gatewayUpstream", "?"),
                ", ".join(credential.get("gatewayHeaders") or []) or "无",
            )
        )
    config = report.get("config") or {}
    _provider, model_id = parse_model_value(config.get("model"))
    lines.append(
        "参数：阈值={}，模型={}，预热={}，空闲回收={}s，单轮超时={}s".format(
            config.get("minScore", "?"),
            model_id or "（内核默认）",
            "开" if config.get("prewarm") else "关",
            config.get("idleTimeoutSeconds", "?"),
            config.get("timeoutSeconds", "?"),
        )
    )
    for item in report.get("scores") or []:
        decision = item.get("decision") or {}
        lines.append(
            "打分：{:.3f} → {}（{}）  {}".format(
                float(item.get("score") or 0.0),
                "交给 DSH" if decision.get("useDsh") else "走原生",
                decision.get("reason", ""),
                item.get("text", ""),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_bool(value: Any, fallback: bool) -> bool:
    return bool(value) if isinstance(value, bool) else fallback


def _as_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value)


def _as_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return int(value)


def _as_protocol(value: Any) -> str:
    text = _as_text(value).lower()
    return text if text in PROTOCOLS else "auto"
