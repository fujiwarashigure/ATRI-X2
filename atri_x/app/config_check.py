"""``atri-x config check``：加载 app.toml 并给出人类可读的摘要与告警。

对应 Go：``internal/app/config_check.go``。

要点（逐条复刻 Go 的输出契约）：

* 第一行固定 ``config OK``，加载失败则抛异常（调用方决定怎么报）；
* ``warnings: none`` 与 ``warnings:`` + 逐条 ``- ...`` 两种收尾；
* 未知字段、内置资产漂移都会追加成告警。
"""

from __future__ import annotations

import os
from typing import Any

from atri_x.config import builtin_asset_drift, load as load_config
from atri_x.config import unknown_app_config_keys

__all__ = ["CheckConfig", "check_config", "checkConfigWarnings", "check_config_warnings"]


def check_config(config_path: str = "") -> str:
    """对应 Go ``CheckConfig``。"""
    cfg = load_config(config_path)
    lines: list[str] = ["config OK", "path: " + str(cfg.config_path)]
    if cfg.services_config_path:
        lines.append("services: " + cfg.services_config_path)
    if cfg.providers_config_path:
        lines.append("providers: " + cfg.providers_config_path)
    lines.append("state: " + str(cfg.state_config_path))
    lines.append(
        "commands.prefixes: " + " ".join(getattr(cfg.commands, "prefixes", None) or [])
    )
    character_library = getattr(cfg, "character_library", None)
    lines.append(
        "character_library: enabled=%s root=%s"
        % (_go_bool(_enabled(character_library)), getattr(character_library, "root", ""))
    )
    image_generation = getattr(cfg, "image_generation", None)
    if bool(getattr(image_generation, "enabled", False)):
        default_profile = getattr(image_generation, "default_profile_name", None)
        lines.append(
            "image_generation: model=%s profiles=%d default_profile=%r"
            % (
                getattr(image_generation, "model", ""),
                len(getattr(image_generation, "profiles", None) or {}),
                (default_profile() if callable(default_profile) else ""),
            )
        )
    else:
        lines.append("image_generation: disabled")
    lines.append("model_profiles: %d" % len(getattr(cfg, "model_profiles", None) or {}))
    lines.append("tool_profiles: %d" % len(getattr(cfg, "tool_profiles", None) or {}))

    directives = _normalized(getattr(cfg, "turn_directives", None))
    lines.append(
        "turn_directives: prefixes=%s model=%s image=%s tool=%s"
        % (
            " ".join(getattr(directives, "prefixes", None) or []),
            "/".join(getattr(directives, "model_keywords", None) or []),
            "/".join(getattr(directives, "image_keywords", None) or []),
            "/".join(getattr(directives, "tool_keywords", None) or []),
        )
    )
    report = getattr(getattr(cfg, "maintenance", None), "daily_report", None)
    lines.append(
        "daily_report: enabled=%s schedule=%r window_hours=%d provider=%r"
        % (
            _go_bool(bool(getattr(report, "enabled", False))),
            getattr(report, "schedule", ""),
            int(getattr(report, "window_hours", 0) or 0),
            getattr(report, "provider", ""),
        )
    )

    warnings = check_config_warnings(cfg)
    try:
        keys = unknown_app_config_keys(cfg.config_path)
    except Exception as exc:  # noqa: BLE001 - 检查失败只降级成告警
        warnings.append("无法检查主配置未知字段：" + str(exc))
    else:
        if keys:
            warnings.append(
                "主配置存在未知字段（可能拼写错误或被忽略）：" + ", ".join(keys)
            )
    warnings.extend(builtin_asset_drift(os.path.dirname(str(cfg.config_path or ""))))
    if not warnings:
        lines.append("warnings: none")
    else:
        lines.append("warnings:")
        lines.extend("- " + item for item in warnings)
    return "\n".join(lines) + "\n"


def check_config_warnings(cfg: Any) -> list[str]:
    """对应 Go ``checkConfigWarnings``。"""
    warnings: list[str] = []
    providers = getattr(cfg, "providers", None) or {}
    for name in sorted(getattr(cfg, "model_profiles", None) or {}):
        profile = cfg.model_profiles[name]
        provider = getattr(profile, "provider", "")
        if provider not in providers:
            warnings.append(
                f"model_profiles.{name} 的 provider {provider!r} 不在 providers 配置里"
            )
    image_generation = getattr(cfg, "image_generation", None)
    if not bool(getattr(image_generation, "enabled", False)) and (
        getattr(image_generation, "profiles", None) or {}
    ):
        warnings.append(
            "配置了 image_generation.profiles 但 enabled=false，生图工具不会注册"
        )
    if (
        bool(getattr(image_generation, "enabled", False))
        and not str(getattr(image_generation, "base_url", "") or "").strip()
        and not str(getattr(image_generation, "endpoint", "") or "").strip()
        and not (getattr(image_generation, "profiles", None) or {})
    ):
        warnings.append("image_generation 缺少 base_url/endpoint")
    report = getattr(getattr(cfg, "maintenance", None), "daily_report", None)
    if bool(getattr(report, "enabled", False)):
        if not (getattr(getattr(cfg, "security", None), "superadmins", None) or {}):
            warnings.append(
                "daily_report.enabled=true 但没有配置 security.superadmins，报告无法发送"
            )
        if not (getattr(report, "prices", None) or {}):
            warnings.append(
                "daily_report 没有配置 prices，报告只统计 token 不统计金额"
            )
    return warnings


def _enabled(section: Any) -> bool:
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))


def _normalized(section: Any) -> Any:
    method = getattr(section, "normalized", None)
    if callable(method):
        return method()
    return section


def _go_bool(value: bool) -> str:
    return "true" if value else "false"


CheckConfig = check_config
checkConfigWarnings = check_config_warnings
