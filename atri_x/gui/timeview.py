"""「时间」页的数据层（无 Tk）：读 ``[time]``、算实时快照、跑时区探针、拉后台 ``/diagnostics``。

分层与「在线」页一致：数据在价值层模块（:mod:`atri_x.gui.linkstate` / 本模块），
界面在 :mod:`atri_x.gui.tabs.timepanel`。这样"界面能读到什么、算出来的值对不对"
可以脱离 Tk 单测。

四类数据：

* **配置**（``app.toml``）：:func:`load_time_config` 把 ``[time]`` /
  ``[time.verification]`` / ``[[time.sources]]`` 读成已合并默认值的形式，界面直接渲染；
* **实时快照**：:func:`describe_clock` 用运行时同一个 :class:`atri_x.time_service.TimeService`
  取一次采样，保证界面显示的时间口径与后台服务、提示词、``/time`` 完全一致
  （业务时间源是系统时钟，网络校验不接管）；
* **时区探针**：:func:`probe_timezone` 复用 ``python -m atri_x.app.time_check`` 的实现
  （tzdata 是否可读 + DST 冬夏偏移是否正确），**不触网**；
* **后台运行状态**：:func:`fetch_time_diagnostics` 拉一次后台 ``/diagnostics`` 的
  ``time`` 段（网络校验状态 / 每个来源最近一次的观测 / 告警）。这一步只读本地回环
  接口，鉴权用 ``.env`` 里的 ``ATRIX_OPS_TOKEN``。

这里所有函数都不碰 Tk，失败一律抛 :class:`TimeViewError`（消息可直接展示给用户）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from atri_x.config.time_sections import (
    TimeConfig,
    TimeSourceConfig,
    TimeVerificationConfig,
)
from atri_x.errors import AtriXError
from atri_x.gui.configedit import ConfigEditError, ConfigPaths, read_env, read_toml
from atri_x.time_service import TimeService
from atri_x.timezones import (
    DEFAULT_TIMEZONE,
    available_timezones,
    iana_zone_count,
    tzdata_version,
    zoneinfo_available,
)

__all__ = [
    "COMMON_TIMEZONES",
    "DIAGNOSTICS_PATH",
    "HEALTH_ADDR_ENV",
    "HEALTH_TOKEN_ENV",
    "TZ_RULES_LABELS",
    "TIMEZONE_SOURCE_LABELS",
    "VERIFICATION_STATE_LABELS",
    "WEEKDAY_NAMES",
    "TimeConfigView",
    "TimeViewError",
    "describe_clock",
    "diagnostics_credentials",
    "diagnostics_endpoint",
    "fetch_time_diagnostics",
    "format_offset_text",
    "load_time_config",
    "probe_lines",
    "probe_timezone",
    "source_defaults",
    "state_label",
    "time_defaults",
    "timezone_choices",
    "tzdata_summary",
    "verification_alerts",
    "verification_defaults",
    "verification_lines",
    "verification_sources",
]

#: 后台只读状态接口：地址与令牌的 ``.env`` 键名（与 :mod:`atri_x.gui.service` 一致）。
HEALTH_ADDR_ENV = "ATRIX_HEALTH_ADDR"
HEALTH_TOKEN_ENV = "ATRIX_OPS_TOKEN"
#: 诊断端点路径（健康服务上额外注册的只读 JSON 端点）。
DIAGNOSTICS_PATH = "/diagnostics"

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: ``[time].timezone`` 的取值来源 → 界面说明（``TimezoneResolution.source``）。
TIMEZONE_SOURCE_LABELS = {
    "config": "配置文件 [time].timezone",
    "default": "默认值（配置里没写）",
    "argument": "本次调用显式指定",
}

#: 时区解析规则 → 界面说明（``TimezoneResolution.rules``）。
TZ_RULES_LABELS = {
    "iana": "IANA 规则（跟随夏令时）",
    "naive_offset": "固定偏移（无夏令时）",
    "system_offset": "进程本地时区（固定偏移）",
}

#: 网络时间校验状态 → 界面说明（取值见 :data:`atri_x.time_service.VERIFICATION_STATES`）。
VERIFICATION_STATE_LABELS = {
    "disabled": "未启用（不发任何网络请求）",
    "checking": "校验中",
    "consistent": "一致（多来源证据吻合）",
    "insufficient_evidence": "证据不足（还没有足够独立分组）",
    "conflict": "冲突（本地时间可疑）",
    "stale": "过期（样本太旧或已作废）",
    "unavailable": "不可用（这一轮拿不到有效证据）",
}

#: 时区输入框候选里排在全部 IANA 名前面的常用值。
COMMON_TIMEZONES = (
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Singapore",
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
    "system",
    "fixed:+08:00",
)

#: 「样本年龄」超过这个秒数时按分钟/小时显示（纯展示用）。
_SECONDS_PER_MINUTE = 60


class TimeViewError(Exception):
    """时间页的可预期失败（消息直接展示给用户）。"""


@dataclass(frozen=True)
class TimeConfigView:
    """``app.toml`` 里时间相关配置的只读视图（已合并默认值）。"""

    timezone: str
    inject_context: bool
    max_sample_age_seconds: int
    verification: dict[str, Any]
    sources: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 配置：默认值 / 读取
# ---------------------------------------------------------------------------
def time_defaults() -> dict[str, Any]:
    """``[time]`` 的生效默认值（唯一出处：:class:`atri_x.config.time_sections.TimeConfig`）。"""
    section = TimeConfig()
    return {
        "timezone": section.timezone,
        "inject_context": section.inject_context,
        "max_sample_age_seconds": section.max_sample_age_seconds,
    }


def verification_defaults() -> dict[str, Any]:
    """``[time.verification]`` 的生效默认值。"""
    return asdict(TimeVerificationConfig())


def source_defaults() -> dict[str, Any]:
    """一个 ``[[time.sources]]`` 条目的生效默认值。"""
    return asdict(TimeSourceConfig())


def load_time_config(paths: ConfigPaths) -> TimeConfigView:
    """读 ``app.toml`` 的时间相关配置（文件缺失 / 解析失败时报可展示的错误）。"""
    try:
        data = read_toml(paths.config_path)
    except ConfigEditError as exc:
        raise TimeViewError(str(exc)) from exc
    section = _mapping(data.get("time"))
    verification = dict(verification_defaults())
    verification.update(_scalars(_mapping(section.get("verification"))))
    sources = [
        _merge(source_defaults(), item)
        for item in section.get("sources") or []
        if isinstance(item, Mapping)
    ]
    defaults = time_defaults()
    timezone = section.get("timezone")
    return TimeConfigView(
        timezone=str(timezone).strip() if isinstance(timezone, str) and timezone.strip() else str(defaults["timezone"]),
        inject_context=_as_bool(section.get("inject_context"), bool(defaults["inject_context"])),
        max_sample_age_seconds=_as_int(
            section.get("max_sample_age_seconds"), int(defaults["max_sample_age_seconds"])
        ),
        verification=verification,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# 实时快照
# ---------------------------------------------------------------------------
def describe_clock(timezone: str, *, inject_context: bool = True) -> list[tuple[str, str]]:
    """按某个时区生成"现在"的只读展示项（``[(界面名, 值)]``）。

    与后台服务同口径：业务时间源恒为系统时钟；时区非法时硬失败（抛
    :class:`TimeViewError`，不静默回落到默认时区）。
    """
    name = (timezone or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        service = TimeService(name, timezone_source="config", inject_context=inject_context)
        snapshot = service.snapshot()
        described = service.describe()
        resolution = service.resolution()
    except AtriXError as exc:
        raise TimeViewError(f"时区 {name!r} 不可用：{exc}") from exc
    weekday = WEEKDAY_NAMES[snapshot.weekday] if 0 <= snapshot.weekday < 7 else "?"
    return [
        ("业务时间源", "system_clock（系统时钟；网络校验不接管业务时间）"),
        ("时区（配置）", name),
        ("时区（解析后）", resolution.name),
        ("时区来源", TIMEZONE_SOURCE_LABELS.get(resolution.source, resolution.source)),
        ("时区规则", TZ_RULES_LABELS.get(resolution.rules, resolution.rules)),
        ("当前时间", snapshot.current_time.isoformat(timespec="seconds")),
        ("UTC 时间", snapshot.utc_time.isoformat(timespec="seconds")),
        ("UTC 偏移", format_offset_text(snapshot.utc_offset)),
        ("星期", weekday),
        ("unix 时间戳", str(snapshot.unix_timestamp)),
        ("注入系统提示", "是（每轮请求一份当前时间）" if inject_context else "否"),
        ("tzdata 版本", str(described.get("tzdata_version") or "未知")),
        ("IANA 时区数", str(int(described.get("tz_zones") or 0))),
        ("时区数据", "可用" if described.get("tz_data") == "present" else "缺失"),
    ]


# ---------------------------------------------------------------------------
# 时区探针（tzdata + DST）
# ---------------------------------------------------------------------------
def probe_timezone(timezone: str) -> dict[str, Any]:
    """跑一次时区数据探针（不触网），返回 ``check_time_data`` 的报告。"""
    from atri_x.app.time_check import check_time_data

    return check_time_data((timezone or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE)


def probe_lines(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    """把探针报告转成展示项。"""
    rules = str(report.get("rules") or "")
    lines: list[tuple[str, str]] = [
        ("探针结果", "通过" if report.get("ok") else "失败"),
        ("时区", str(report.get("timezone") or "")),
        ("时区规则", TZ_RULES_LABELS.get(rules, rules)),
        ("UTC 偏移", str(report.get("utc_offset") or "")),
        ("tzdata 版本", str(report.get("tzdata") or "未知")),
        ("IANA 时区数", str(int(report.get("zones") or 0))),
        ("时区数据", "可用" if report.get("zoneinfo_available") else "缺失"),
    ]
    dst = report.get("dst_probe")
    dst = dst if isinstance(dst, Mapping) else {}
    if dst:
        lines.extend(
            [
                ("夏令时探针", "通过" if dst.get("ok") else "失败"),
                ("DST 冬季", str(dst.get("winter") or "")),
                ("DST 夏季", str(dst.get("summer") or "")),
            ]
        )
    error = str(report.get("error") or "").strip()
    if error:
        lines.append(("错误", error))
    return lines


# ---------------------------------------------------------------------------
# 后台状态：``/diagnostics`` 的 ``time`` 段
# ---------------------------------------------------------------------------
def diagnostics_credentials(paths: ConfigPaths) -> tuple[str, str]:
    """从 ``.env`` 读状态接口地址与令牌（进程环境变量优先，与配置层规则一致）。"""
    values: dict[str, str] = {}
    if paths.env_path:
        try:
            values = read_env(paths.env_path)
        except ConfigEditError:
            values = {}
    addr = (os.environ.get(HEALTH_ADDR_ENV) or values.get(HEALTH_ADDR_ENV) or "").strip()
    token = (os.environ.get(HEALTH_TOKEN_ENV) or values.get(HEALTH_TOKEN_ENV) or "").strip()
    return addr, token


def diagnostics_endpoint(addr: str) -> str:
    """``127.0.0.1:32171`` → ``http://127.0.0.1:32171/diagnostics``。"""
    text = (addr or "").strip()
    if not text:
        raise TimeViewError(
            f"未配置后台状态接口地址（.env 的 {HEALTH_ADDR_ENV}）；桌面端启动时会自动补齐"
        )
    host, _, port = text.rpartition(":")
    if not host:
        host, port = text, "80"
    if host in ("0.0.0.0", "::", "[::]", "*"):
        host = "127.0.0.1"
    if not port:
        raise TimeViewError(f"状态接口地址缺少端口：{text}")
    return f"http://{host}:{port}{DIAGNOSTICS_PATH}"


def fetch_time_diagnostics(
    addr: str, token: str, *, timeout: float = 2.5
) -> dict[str, Any]:
    """拉一次后台 ``/diagnostics``，返回其中的 ``time`` 段（永不返回非 dict）。

    显式禁用代理：要访问的是本机回环地址，走系统代理只会超时或 407。
    """
    url = diagnostics_endpoint(addr)
    if not token:
        raise TimeViewError(
            f"缺少后台状态接口令牌（.env 的 {HEALTH_TOKEN_ENV}）；桌面端启动时会自动补齐"
        )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(0.2, float(timeout))) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        try:
            exc.read()
        except OSError:  # 读错误体失败不该盖住真正的状态码
            pass
        finally:
            exc.close()
        if code in (401, 403):
            raise TimeViewError(
                "状态接口鉴权失败：请检查 .env 的 ATRIX_OPS_TOKEN 与后台服务是否一致"
            ) from exc
        if code == 404:
            raise TimeViewError(
                "后台没有注册 /diagnostics（旧版本，或没配 ATRIX_OPS_TOKEN）"
            ) from exc
        raise TimeViewError(f"状态接口返回 HTTP {code}") from exc
    except urllib.error.URLError as exc:
        raise TimeViewError(f"状态接口不可达：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise TimeViewError(f"状态接口不可达：{exc}") from exc
    if not payload:
        return {}
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TimeViewError(f"状态接口返回了无法解析的内容：{exc}") from exc
    if not isinstance(body, Mapping):
        return {}
    section = body.get("time")
    return dict(section) if isinstance(section, Mapping) else {}


def state_label(state: Any) -> str:
    """校验状态 → 中文说明（未知状态原样显示，不吞掉新状态）。"""
    text = str(state or "disabled")
    return VERIFICATION_STATE_LABELS.get(text, text)


def verification_lines(section: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``/diagnostics.time.verification`` → 展示项。"""
    lines: list[tuple[str, str]] = [
        ("校验状态", state_label(section.get("state"))),
        ("模式", _mode_label(section.get("mode"))),
        ("总开关", "已启用" if section.get("enabled") else "未启用"),
        ("已配置来源", f"{int(section.get('sources_configured') or 0)} 个"),
    ]
    for key, label in (
        ("last_check_at", "最近一轮"),
        ("last_success_at", "最近成功"),
        ("next_check_at", "下次计划"),
    ):
        lines.append((label, str(section.get(key) or "").strip() or "—"))
    lines.append(("样本年龄", _age_text(section.get("sample_age_seconds"))))
    lines.append(("本地偏差", _ms_text(section.get("offset_ms"), sign_hint=True)))
    if section.get("spread_ms") is not None:
        lines.append(("来源间极差", _ms_text(section.get("spread_ms"))))
    if section.get("groups"):
        lines.append(("独立分组", str(int(section.get("groups") or 0))))
    message = str(section.get("message") or "").strip()
    if message:
        lines.append(("说明", message))
    return lines


def verification_sources(section: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``/diagnostics.time.verification.sources`` → 一行一个来源的观测。"""
    rows = section.get("sources")
    if not isinstance(rows, list):
        return []
    return [dict(item) for item in rows if isinstance(item, Mapping)]


def verification_alerts(section: Mapping[str, Any]) -> list[str]:
    """``/diagnostics.time.verification.alerts`` → 可读文本行。"""
    alerts = section.get("alerts")
    if not isinstance(alerts, list):
        return []
    lines: list[str] = []
    for item in alerts:
        if isinstance(item, Mapping):
            parts = [
                str(item.get("at") or "").strip(),
                str(item.get("code") or "").strip(),
                str(item.get("reason") or "").strip(),
            ]
            lines.append(" ".join(part for part in parts if part))
        else:
            lines.append(str(item))
    return lines


# ---------------------------------------------------------------------------
# 展示助手
# ---------------------------------------------------------------------------
def format_offset_text(value: Any) -> str:
    """``timedelta`` / 已是文本的偏移 → ``UTC+08:00``。"""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dt.timedelta):
        return ""
    total = int(value.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def timezone_choices() -> tuple[str, ...]:
    """时区输入框候选：常用值在前，后面接全部 IANA 名（仍可手输固定偏移）。"""
    zones = list(COMMON_TIMEZONES)
    for zone in available_timezones():
        if zone not in zones:
            zones.append(zone)
    return tuple(zones)


def _mode_label(mode: Any) -> str:
    text = str(mode or "").strip()
    if text == "verify_only":
        return "verify_only（只观测，不修改系统时钟、不接管业务时间）"
    return text or "verify_only"


def _age_text(value: Any) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds} 秒前"
    minutes = seconds // _SECONDS_PER_MINUTE
    if minutes < _SECONDS_PER_MINUTE:
        return f"{minutes} 分 {seconds % _SECONDS_PER_MINUTE} 秒前"
    return f"{minutes // _SECONDS_PER_MINUTE} 小时 {minutes % _SECONDS_PER_MINUTE} 分前"


def _ms_text(value: Any, *, sign_hint: bool = False) -> str:
    if value is None:
        return "—"
    text = f"{float(value):.1f} ms"
    return text + "（正数 = 本地慢）" if sign_hint else text


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalars(section: Mapping[str, Any]) -> dict[str, Any]:
    """只保留标量键（子表 / 数组由上层各自处理）。"""
    return {
        key: value
        for key, value in section.items()
        if not isinstance(value, (Mapping, list, tuple))
    }


def _merge(defaults: dict[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(_scalars(item))
    return merged


def _as_bool(value: Any, default: bool) -> bool:
    return bool(value) if isinstance(value, bool) else default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def tzdata_summary() -> str:
    """tzdata 的一句话摘要（界面左下角用；缺数据时说清楚）。"""
    if not zoneinfo_available():
        return "tzdata 缺失：任何 IANA 时区都无法解析"
    return f"tzdata {tzdata_version() or '未知'} · {iana_zone_count()} 个 IANA 时区可用"
