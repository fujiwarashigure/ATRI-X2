"""DSH 子进程的凭据注入与**协议判定**（P0：内核只会说 Anthropic Messages）。

为什么需要协议判定：内置内核的 DeepSeek 适配器走的是 **Anthropic Messages** 协议
（``PUBLIC_BASE_URL = https://api.deepseek.com/anthropic``，请求打到
``<base_url>/v1/messages``）。而 ATRI-X 的 provider 表里常有 OpenAI 兼容网关
（``.../openai/v1``），把它的 ``base_url`` 原样注入 ``DEEPSEEK_BASE_URL`` 只会得到
``404 Not Found`` —— 用户侧表现为"路由从未生效"，还白付一次冷启动。

因此这里把"能不能喂给内核"变成一个显式计划 :class:`CredentialPlan`：

- ``auto``（默认）：只认**官方 DeepSeek 主机**、``base_url`` 里带 ``anthropic`` 的端点，
  以及用户自己写在进程环境里的 ``DEEPSEEK_BASE_URL``；其余一律判为不可用、**不注入、
  不拉起内核**（路由静默回原生，审计记一次原因）；
- ``anthropic``：用户明确知道该网关支持 Anthropic Messages，强制注入；
- ``openai`` / ``off``：明确不路由（前者是"协议不兼容"的自述，后者是"不走 DSH"）。

本模块只做解析，不启动子进程；任何异常都降级成"不可用"而不是抛出。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from atri_x.agent.dsh.router import (
    DEFAULT_CREDENTIAL_BASE_URL_ENV,
    DEFAULT_CREDENTIAL_ENV,
    DshRouteConfig,
)
from atri_x.storage import SessionModeWork

__all__ = [
    "CredentialPlan",
    "OFFICIAL_BASE_URL_HOSTS",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_AUTO",
    "PROTOCOL_OFF",
    "PROTOCOL_OPENAI",
    "normalize_protocol",
    "resolve_credential_env",
    "resolve_credential_plan",
]

#: 协议取值（``[agent.dsh].credential_protocol``）。
PROTOCOL_AUTO = "auto"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OPENAI = "openai"
PROTOCOL_OFF = "off"

#: 官方 DeepSeek 主机；``base_url`` 指向它时必然是 Anthropic Messages 端点。
OFFICIAL_BASE_URL_HOSTS = ("api.deepseek.com",)

#: 别名 → 规范值（大小写/连字符不敏感）。
_PROTOCOL_ALIASES = {
    "": PROTOCOL_AUTO,
    "auto": PROTOCOL_AUTO,
    "anthropic": PROTOCOL_ANTHROPIC,
    "messages": PROTOCOL_ANTHROPIC,
    "anthropic-messages": PROTOCOL_ANTHROPIC,
    "openai": PROTOCOL_OPENAI,
    "openai-compatible": PROTOCOL_OPENAI,
    "chat": PROTOCOL_OPENAI,
    "off": PROTOCOL_OFF,
    "none": PROTOCOL_OFF,
    "disabled": PROTOCOL_OFF,
}

#: provider 名字里出现它时，空 ``base_url`` 也按官方端点理解。
_DEEPSEEK_MARKER = "deepseek"


def _text_of(value: Any) -> str:
    return str(value or "").strip()


def normalize_protocol(value: Any) -> str:
    """把配置值归一成 :data:`PROTOCOL_AUTO` / ``anthropic`` / ``openai`` / ``off``。"""
    key = _text_of(value).lower()
    return _PROTOCOL_ALIASES.get(key, PROTOCOL_AUTO)


def _is_official_base_url(base_url: str) -> bool:
    """``base_url`` 是否就是官方 DeepSeek 端点（含 ``/v1`` 之类的路径）。"""
    text = _text_of(base_url)
    if not text:
        return False
    host = _text_of(urlsplit(text).hostname)
    return any(
        host == official or host.endswith("." + official)
        for official in OFFICIAL_BASE_URL_HOSTS
    )


def _looks_anthropic(base_url: str) -> bool:
    """``base_url`` 是否显式声明自己是 Anthropic 兼容端点。"""
    return "anthropic" in _text_of(base_url).lower()


def _provider_api_key(provider: Any) -> str:
    """provider 的可用密钥：``api_key`` 优先，其次 ``api_key_env`` 指向的宿主环境变量。"""
    key = _text_of(getattr(provider, "api_key", ""))
    if key:
        return key
    key_env = _text_of(getattr(provider, "api_key_env", ""))
    return _text_of(os.environ.get(key_env, "")) if key_env else ""


def _providers_table(agent: Any) -> Mapping[str, Any]:
    providers = getattr(agent, "providers", None)
    return providers if isinstance(providers, Mapping) else {}


def _pick_credential_provider(agent: Any, named: str) -> tuple[str, Any]:
    """挑一个能提供密钥的 provider（显式指定 > 名字含 deepseek 的 > work provider）。"""
    table = _providers_table(agent)
    candidates: list[tuple[str, Any]] = []
    if named and table.get(named) is not None:
        candidates.append((named, table[named]))
    for key, value in table.items():
        if _DEEPSEEK_MARKER in _text_of(key).lower():
            candidates.append((_text_of(key), value))
    selection = (getattr(agent, "mode_models", None) or {}).get(SessionModeWork)
    work_provider = _text_of(getattr(selection, "provider", ""))
    if work_provider and table.get(work_provider) is not None:
        candidates.append((work_provider, table[work_provider]))
    seen: set[int] = set()
    for name, provider in candidates:
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        if _provider_api_key(provider):
            return name, provider
    return "", None


@dataclass(frozen=True)
class CredentialPlan:
    """一次解析的结果：注入哪些变量、能不能路由、为什么。"""

    #: 要注入子进程的环境变量（只含凭据/端点，值不对外展示）。
    env: Mapping[str, str] = field(default_factory=dict)
    #: 是否适合交给 DSH 内核（``False`` 时调用方应静默回退原生）。
    usable: bool = True
    #: 归一后的协议：``anthropic`` / ``openai`` / ``off`` / ``auto``（仅用于解释）。
    protocol: str = PROTOCOL_ANTHROPIC
    #: 实际取到密钥的 provider 名（空 = 走继承环境或内核自己的凭据）。
    provider: str = ""
    #: 选中的 ``base_url``（不可用时用于解释，不含密钥）。
    base_url: str = ""
    #: 不可用原因（中文，直接给 ``status``/审计/功能页用）。
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """状态快照：**只报变量名**，绝不回显凭据值。"""
        return {
            "usable": self.usable,
            "protocol": self.protocol,
            "provider": self.provider,
            "baseUrl": self.base_url,
            "envKeys": sorted(str(key) for key in self.env),
            "reason": self.reason,
        }

    def __bool__(self) -> bool:
        return self.usable


def _unusable(protocol: str, provider: str, base_url: str, reason: str) -> CredentialPlan:
    return CredentialPlan(
        env={},
        usable=False,
        protocol=protocol,
        provider=provider,
        base_url=base_url,
        reason=reason,
    )


def resolve_credential_plan(agent: Any, config: DshRouteConfig) -> CredentialPlan:
    """解析"给 DSH 子进程注入什么、这个 provider 能不能用"。

    ``api_key`` 为空时**不注入空值**：退回继承值（``.credentials.yaml`` / 宿主环境
    仍然可用），此时按"内核自己会处理"放行，不做协议误判。
    """
    env_name = _text_of(getattr(config, "credential_env", "")) or DEFAULT_CREDENTIAL_ENV
    base_url_env = (
        _text_of(getattr(config, "credential_base_url_env", ""))
        or DEFAULT_CREDENTIAL_BASE_URL_ENV
    )
    protocol = normalize_protocol(getattr(config, "credential_protocol", ""))
    provider_name, provider = _pick_credential_provider(
        agent, _text_of(getattr(config, "credential_provider", ""))
    )
    api_key = _provider_api_key(provider)
    base_url = _text_of(getattr(provider, "base_url", ""))
    inherited_base_url = _text_of(os.environ.get(base_url_env, ""))
    inherited_key = _text_of(os.environ.get(env_name, ""))

    if protocol == PROTOCOL_OFF:
        return _unusable(protocol, provider_name, base_url, "credential_protocol=off")
    if protocol == PROTOCOL_OPENAI:
        return _unusable(
            protocol,
            provider_name,
            base_url,
            "credential_protocol=openai：DSH 内核只支持 Anthropic Messages 协议",
        )
    if not api_key and not inherited_key and not inherited_base_url:
        # 没有任何可注入的凭据：内核可能用 DSH_HOME 下的 .credentials.yaml，交给它决定。
        return CredentialPlan(protocol=PROTOCOL_ANTHROPIC)

    if protocol == PROTOCOL_ANTHROPIC:
        usable = True
    elif inherited_base_url:
        # 用户自己把 DEEPSEEK_BASE_URL 写进进程环境 = 明确声明端点，尊重它。
        usable = True
    elif _is_official_base_url(base_url) or _looks_anthropic(base_url):
        usable = True
    elif not base_url:
        # 没有 provider 只有继承密钥 = 内核按官方默认端点走；provider 无名无端点时无法判断。
        usable = (not provider_name) or _DEEPSEEK_MARKER in provider_name.lower()
    else:
        usable = False

    if not usable:
        return _unusable(
            protocol,
            provider_name,
            base_url,
            f"provider {provider_name or '<env>'} 的 base_url（{base_url or '未配置'}）"
            "不是 Anthropic Messages 端点：DSH 内核只会往 <base_url>/v1/messages 发请求"
            "（OpenAI 兼容网关必然 404）。请改配官方 DeepSeek 密钥，"
            "或在该网关支持 Anthropic 协议时设 credential_protocol=\"anthropic\"",
        )

    env: dict[str, str] = {}
    if api_key:
        env[env_name] = api_key
    elif inherited_key:
        env[env_name] = inherited_key
    # 非官方端点才需要下发 base_url；用户显式写进进程环境的优先，不再覆盖。
    if base_url and not _is_official_base_url(base_url) and not inherited_base_url:
        env[base_url_env] = base_url
    return CredentialPlan(
        env=env,
        usable=True,
        protocol=PROTOCOL_ANTHROPIC,
        provider=provider_name,
        base_url=base_url or inherited_base_url,
    )


def resolve_credential_env(agent: Any, config: DshRouteConfig) -> dict[str, str]:
    """兼容旧接口：只返回要注入的环境变量（不可用时返回空 dict）。"""
    return dict(resolve_credential_plan(agent, config).env)
