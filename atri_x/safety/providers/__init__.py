"""远程审核供应商的装配入口。

把 ``safety.toml`` 的 ``[remote.<provider>]`` 解成可调用的适配器：

* 凭据从**环境变量**读（``*_env`` 字段给的是变量名；先系统环境、再 ``<config_dir>/.env``）；
* 缺凭据 / 显式 ``enabled = false`` → **不构造**该供应商（不报错，等于只用另一家）；
* ``transport`` 可注入，测试用假传输离线覆盖签名与解析。

**不做什么**：不做重试/熔断/双供应商仲裁——那是 :mod:`atri_x.safety.engine`。
"""

from __future__ import annotations

from typing import Any, Mapping

from atri_x.safety.providers.base import (
    HttpTransport,
    ModerationProvider,
    ProviderConfig,
    Transport,
)
from atri_x.safety.providers.aliyun import AliyunProvider
from atri_x.safety.providers.tencent import TencentProvider

__all__ = [
    "AliyunProvider",
    "HttpTransport",
    "ModerationProvider",
    "ProviderConfig",
    "TencentProvider",
    "Transport",
    "build_provider",
    "build_providers",
    "provider_config",
]

_PROVIDER_NAMES: tuple[str, ...] = ("tencent", "aliyun")


def _env_value(name: str, config_dir: str) -> str:
    """读一个"密钥用环境变量名"：系统环境优先，其次 ``<config_dir>/.env``。"""
    key = (name or "").strip()
    if not key:
        return ""
    try:
        from atri_x.config import config_env

        return (config_env(key, config_dir) or "").strip()
    except Exception:  # noqa: BLE001 - 配置层缺件时退回只看系统环境
        import os

        return (os.environ.get(key) or "").strip()


def provider_config(name: str, section: Any, *, config_dir: str = "") -> ProviderConfig:
    """把 ``[remote.<name>]`` 段解成 :class:`ProviderConfig`（凭据从环境变量取）。"""
    return ProviderConfig(
        name=name,
        region=str(getattr(section, "region", "") or ""),
        endpoint=str(getattr(section, "endpoint", "") or ""),
        service=str(getattr(section, "service", "") or ""),
        version=str(getattr(section, "version", "") or ""),
        biz_type=str(getattr(section, "biz_type", "") or ""),
        secret_id=_env_value(getattr(section, "secret_id_env", ""), config_dir),
        secret_key=_env_value(getattr(section, "secret_key_env", ""), config_dir),
        access_key_id=_env_value(getattr(section, "access_key_id_env", ""), config_dir),
        access_key_secret=_env_value(getattr(section, "access_key_secret_env", ""), config_dir),
    )


def build_provider(
    name: str,
    section: Any,
    *,
    config_dir: str = "",
    transport: Transport | None = None,
    timeout_seconds: float = 0.8,
    clock: Any = None,
) -> ModerationProvider | None:
    """构造一个供应商适配器；缺凭据/未启用时返回 ``None``。"""
    key = (name or "").strip().lower()
    if key not in _PROVIDER_NAMES or section is None:
        return None
    if not bool(getattr(section, "enabled", True)):
        return None
    config = provider_config(key, section, config_dir=config_dir)
    if not config.has_credentials:
        return None
    channel = transport or HttpTransport()
    if key == "tencent":
        return TencentProvider(
            config, transport=channel, timeout_seconds=timeout_seconds, clock=clock
        )
    return AliyunProvider(
        config, transport=channel, timeout_seconds=timeout_seconds, clock=clock
    )


def build_providers(
    settings: Any,
    *,
    config_dir: str = "",
    transport: Transport | None = None,
    clock: Any = None,
) -> dict[str, ModerationProvider]:
    """按 ``[remote].providers`` 的顺序构造可用的供应商（缺失的静默跳过）。"""
    remote = getattr(settings, "remote", None)
    if remote is None or not bool(getattr(remote, "enabled", False)):
        return {}
    timeout_seconds = max(0.05, float(getattr(remote, "timeout_ms", 800) or 800) / 1000.0)
    out: dict[str, ModerationProvider] = {}
    for name in list(getattr(remote, "providers", ()) or ()):
        key = str(name or "").strip().lower()
        if not key or key in out:
            continue
        section = getattr(remote, key, None)
        provider = build_provider(
            key,
            section,
            config_dir=config_dir,
            transport=transport,
            timeout_seconds=timeout_seconds,
            clock=clock,
        )
        if provider is not None:
            out[key] = provider
    return out


def provider_health(providers: Mapping[str, Any]) -> dict[str, bool]:
    """给状态页/日志用：当前哪些供应商真的装配上了。"""
    return {name: True for name in providers}
