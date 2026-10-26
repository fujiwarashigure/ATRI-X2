"""时间相关诊断的只读聚合（``/diagnostics`` 的 ``time`` 段）。

对应 Go：Go 侧诊断没有时间源段。本模块只把已有的只读快照拼起来，**不触网**、
**不触发校验**、**不抛异常**：``/diagnostics`` 在线上被频繁拉取，任何网络行为都会
变成事故。

边界：

* 校验状态只进 ``/diagnostics``（受 ``ATRIX_OPS_TOKEN`` 保护）与 ``doctor``；
  **绝不进 ``/ready``**——网络抖动不得触发容器重启；
* 输出里不得出现凭据；URL 一律走 :func:`atri_x.timesync.policy.redact_url`。
"""

from __future__ import annotations

from typing import Any

__all__ = ["time_diagnostics"]

#: 没有校验器时的占位状态（与 ``[time.verification].enabled = false`` 一致）。
_DISABLED_SECTION: dict[str, Any] = {
    "state": "disabled",
    "mode": "verify_only",
    "enabled": False,
    "sources_configured": 0,
    "last_check_at": "",
    "last_success_at": "",
    "next_check_at": "",
    "sample_age_seconds": None,
    "sources": [],
    "alerts": [],
}


def time_diagnostics(time_service: Any, verifier: Any = None) -> dict[str, Any]:
    """返回诊断用的时间段（永不抛异常）。"""
    section: dict[str, Any] = {
        "effective_source": "system_clock",
        "timezone": "",
        "timezone_source": "",
        "tz_rules": "",
        "tz_data": "unknown",
        "tzdata_version": "",
        "tz_zones": 0,
        "utc_offset": "",
        "verification": dict(_DISABLED_SECTION),
    }
    if time_service is not None:
        try:
            described = time_service.describe()
        except Exception:  # noqa: BLE001 - 诊断自身不得抛
            described = {}
        if isinstance(described, dict):
            for key in (
                "effective_source",
                "timezone",
                "timezone_source",
                "tz_rules",
                "tz_data",
                "tzdata_version",
                "tz_zones",
                "utc_offset",
                "verification_state",
            ):
                if key in described and described[key] not in (None, ""):
                    section[key] = described[key]
    if verifier is not None:
        snapshot = getattr(verifier, "snapshot", None)
        if callable(snapshot):
            try:
                payload = snapshot()
            except Exception:  # noqa: BLE001
                payload = None
            if isinstance(payload, dict):
                section["verification"] = _sanitize(payload)
    return section


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉不该出现在诊断里的字段（防御性：目前 verifier 也不产出它们）。"""
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if key in ("credentials", "access_token", "token", "password"):
            continue
        cleaned[key] = value
    return cleaned
