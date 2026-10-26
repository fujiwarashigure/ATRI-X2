"""``/time``：查看统一时间服务状态，手动触发一次网络校验，列出可用时区。

Python 版新增（Go 侧没有）。三条硬约束：

* **仅超管**（``Info.min_role`` 留空 = 历史默认的仅超管）：时间口径属于部署事实，
  普通成员/群管理员既不需要看，也不允许触发联网校验；
* ``verify`` 只是**触发一次观测**，不会修改系统时钟、不会接管业务时间
  （首版 ``verify_only``），返回结果里明确写"仅观测"；
* 限流与单飞都在 :class:`atri_x.timesync.verifier.Verifier` 里：本轮已在跑时直接
  返回"进行中"，手动最小间隔由配置控制。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.command import Info, Request, Result, new_func

__all__ = ["TimeModule", "new_time"]

_DESCRIPTION = (
    "Show the effective time zone, UTC offset, tzdata status and network verification "
    "state. '/time verify' runs one network verification round (observation only: it "
    "never changes the system clock or the business time). '/time zones [pattern]' "
    "lists installed IANA time zones. Superadmin only."
)


def new_time(deps: Deps) -> Any:
    """构造 ``/time`` 命令。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        args = (getattr(req, "args", "") or "").strip()
        parts = args.split()
        sub = parts[0].lower() if parts else "status"
        service = deps.time_service
        if service is None:
            return Result(content="时间服务未装配（time service is not wired）。")
        if sub in ("", "status"):
            return Result(content=_status_text(service, deps.time_verifier))
        if sub == "verify":
            return Result(content=await _verify_text(service, deps.time_verifier))
        if sub in ("zones", "zone"):
            return Result(content=_zones_text(" ".join(parts[1:])))
        return Result(
            content="用法：/time [status | verify | zones [pattern]]\n"
            "status: 当前时区/偏移/tzdata/校验状态；verify: 手动触发一轮网络校验（仅观测）；"
            "zones: 列出已安装的 IANA 时区（可跟关键字过滤）。"
        )

    return new_func(
        Info(
            name="time",
            usage="/time [status|verify|zones [pattern]]",
            description=_DESCRIPTION,
            help=(
                "/time            查看当前时间口径与校验状态\n"
                "/time verify     手动触发一轮网络时间校验（verify_only，仅观测）\n"
                "/time zones 上海  列出匹配关键字的 IANA 时区"
            ),
        ),
        handle,
    )


def _status_text(service: Any, verifier: Any) -> str:
    snapshot = service.snapshot()
    offset = _offset_text(snapshot.utc_offset)
    lines = [
        "时间服务（业务时间源 = 系统时钟）",
        "时区：%s（%s，规则 %s）" % (snapshot.timezone, offset, snapshot.tz_rules),
        "当前：%s（%s，unix=%d）"
        % (
            snapshot.current_time.isoformat(timespec="seconds"),
            _weekday(snapshot.weekday),
            snapshot.unix_timestamp,
        ),
        "UTC：%s" % snapshot.utc_time.isoformat(timespec="seconds"),
        "时区来源：%s；注入提示词：%s"
        % (snapshot.timezone_source, "是" if _inject_enabled(service) else "否"),
    ]
    described = service.describe()
    lines.append(
        "tzdata：%s（%s，%d 个时区）"
        % (
            described.get("tz_data", "unknown"),
            described.get("tzdata_version", "") or "unknown",
            int(described.get("tz_zones", 0) or 0),
        )
    )
    if verifier is None:
        lines.append("网络校验：未装配（/diagnostics 会显示 disabled）")
        return "\n".join(lines)
    status = verifier.snapshot()
    lines.append(
        "网络校验：%s（%s，%s；来源 %d，最近成功 %s）"
        % (
            status.get("state", "unknown"),
            status.get("mode", "verify_only"),
            "已启用" if status.get("enabled") else "未启用",
            int(status.get("sources_configured", 0) or 0),
            status.get("last_success_at", "") or "-",
        )
    )
    if status.get("message"):
        lines.append("说明：%s" % status["message"])
    if status.get("offset_ms") is not None:
        lines.append(
            "最近偏移：%s ms（来源间极差 %s ms，独立分组 %d）"
            % (_fmt_ms(status.get("offset_ms")), _fmt_ms(status.get("spread_ms")), int(status.get("groups", 0) or 0))
        )
    sample_age = status.get("sample_age_seconds")
    if sample_age is not None:
        lines.append("样本年龄：%s（上限 %s s）" % (_fmt_ms(float(sample_age) * 1000), status.get("max_sample_age_seconds")))
    if status.get("next_check_at"):
        lines.append("下次校验：%s" % status["next_check_at"])
    for source in status.get("sources") or []:
        lines.append(
            "  - %s [%s/%s] status=%s rtt=%sms offset=%sms auth=%s"
            % (
                source.get("provider_id", "?"),
                source.get("protocol", "?"),
                source.get("group", "?"),
                source.get("status", "?"),
                _fmt_ms(source.get("rtt_ms")),
                _fmt_ms(source.get("offset_ms")),
                "yes(TLS 端点)" if source.get("authenticated") else "no",
            )
        )
    lines.append("注意：系统时间来自本机时钟；网络校验只说明“看到什么”，不代表已校准。")
    return "\n".join(lines)


async def _verify_text(service: Any, verifier: Any) -> str:
    if verifier is None:
        return "网络时间校验未装配。"
    if not verifier.enabled():
        return (
            "网络时间校验未启用。请在 app.toml 的 [time.verification] 里设置 "
            "enabled = true，并至少配置一个启用的 [[time.sources]]。"
        )
    cooldown = float(verifier.manual_cooldown_seconds() or 0.0)
    if cooldown > 0:
        return "手动校验过于频繁，请 %.0f 秒后再试。" % cooldown
    result = await verifier.check_round("manual")
    if result.state == "checking":
        return "已有一轮校验在进行中，请稍后查看 /time。"
    lines = [
        "本轮校验完成（verify_only：只观测，不修改系统时钟、不接管业务时间）",
        "状态：%s" % result.state,
        "有效来源/独立分组：%d / %d" % (result.ok_count, result.groups),
    ]
    if result.offset_ms is not None:
        lines.append("中位偏移：%s ms（正数表示本地慢）" % _fmt_ms(result.offset_ms))
    if result.spread_ms is not None and result.ok_count > 1:
        lines.append("来源间极差：%s ms" % _fmt_ms(result.spread_ms))
    if result.message:
        lines.append("说明：%s" % result.message)
    for observation in result.observations:
        lines.append(
            "  - %s [%s] %s%s"
            % (
                observation.provider_id,
                observation.protocol,
                observation.status,
                ("（%s）" % observation.error_code) if observation.error_code else "",
            )
        )
    return "\n".join(lines)


def _zones_text(pattern: str) -> str:
    from atri_x.timezones import DEFAULT_TIMEZONE, available_timezones, tzdata_version

    zones = available_timezones()
    needle = (pattern or "").strip().lower()
    matched = [zone for zone in zones if needle in zone.lower()] if needle else list(zones)
    if not matched:
        return "没有匹配的 IANA 时区：%s（已安装 %d 个）" % (pattern, len(zones))
    shown = matched[:60]
    lines = [
        "已安装 IANA 时区：%d 个（tzdata %s）；默认 %s"
        % (len(zones), tzdata_version() or "unknown", DEFAULT_TIMEZONE),
    ]
    lines.extend("  " + zone for zone in shown)
    if len(matched) > len(shown):
        lines.append("  ……（还有 %d 个，缩小关键字可继续查看）" % (len(matched) - len(shown)))
    lines.append("时区只能在 app.toml 的 [time].timezone 里配置（需要重启生效）。")
    return "\n".join(lines)


def _inject_enabled(service: Any) -> bool:
    return bool(getattr(service, "inject_context", True))


def _offset_text(offset: Any) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "UTC%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)


def _weekday(index: int) -> str:
    names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    if 0 <= int(index) < len(names):
        return names[int(index)]
    return "?"


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "-"
    return "%.1f" % float(value)


class TimeModule:
    """``/time`` 命令模块。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_time)
