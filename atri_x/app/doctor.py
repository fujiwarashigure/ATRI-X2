"""部署验收（doctor）：配置、本地依赖、平台连通性与端到端往返。

对应 Go：``internal/app/doctor.go``。

Go 用一个 575 行的单文件承载四件事，Python 侧按职责拆分：

* 本模块 —— :class:`DoctorService`（``/doctor`` 命令用的只读检查）与
  :func:`run_doctor` 主流程、:func:`format_doctor_report`；
* :mod:`atri_x.app.doctor_checks` —— 单项检查实现（可写目录、密钥解析、
  健康地址探测、平台连通性判定、模型真实调用、CLI 往返）。

与 Go 的差异：

* Go 的 ``http.Client`` 探测用 :mod:`urllib.request`（超时 5s，语义一致）；
* Go 的 ``websocket.Dial`` + ``wsjson`` 用 :func:`atri_x.net.websocket.connect`
  + ``json``（帧内容是 JSON 文本，等价 ``wsjson``）；
* ``os.CreateTemp`` 的可写性探测不用 :mod:`tempfile`（PORTING §7.2 明确禁用
  ``mkdtemp``/``TemporaryDirectory``），改为 ``O_CREAT|O_EXCL`` 手写随机名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from atri_x.app.config_check import check_config
from atri_x.app.doctor_checks import (
    DEFAULT_DOCTOR_TIMEOUT,
    check_writable_dirs,
    doctor_cli_round_trip,
    doctor_health_url,
    doctor_model_call,
    doctor_platform_connection,
    enabled_platform_names,
    fetch_health_snapshot,
    first_line,
    probe_application_health,
    resolve_doctor_api_key,
    resolve_doctor_ops_token,
)
from atri_x.config import load as load_config
from atri_x.errors import ErrHealthzDisabled
from atri_x.media.imagecodec.backend_probe import image_backend_info
from atri_x.redact import error as redact_error

__all__ = [
    "DEFAULT_DOCTOR_TIMEOUT",
    "DoctorService",
    "doctorService",
    "DoctorOptions",
    "DoctorCheck",
    "DoctorReport",
    "RunDoctor",
    "run_doctor",
    "formatDoctorReport",
    "format_doctor_report",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DoctorOptions:
    """对应 Go ``DoctorOptions``。

    ``capabilities`` 是 Python 版新增：启动期 :func:`atri_x.app.runtime_agent.build_agent`
    把自己的能力自检快照绑到 :class:`DoctorService` 上，``/doctor`` 命令因此能检查
    "能力装上了吗"而不必再装配一个 Agent。``python -m atri_x doctor``（无 Agent）
    没有快照，相应检查会标 ``skipped`` 而不是伪造通过。
    """

    config_path: str = ""
    e2e: bool = False
    json: bool = False
    skip_model: bool = False
    require_platform: bool = False
    timeout: float = 0.0
    capabilities: dict[str, Any] | None = None


@dataclass
class DoctorCheck:
    """对应 Go ``DoctorCheck``（JSON 键名逐字照抄 struct tag）。"""

    category: str = ""
    name: str = ""
    status: str = ""
    detail: str = ""
    error: str = ""


@dataclass
class DoctorReport:
    """对应 Go ``DoctorReport``。"""

    config_ok: bool = True
    platform_ok: bool = True
    e2e_ok: bool = False
    checks: list[DoctorCheck] = field(default_factory=list)
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None


class DoctorService:
    """``/doctor`` 命令背后的只读检查。对应 Go ``doctorService``。

    跳过模型探测，所以斜杠命令永远不会花 token。
    """

    def __init__(self, config_path: str = "", timeout: float = 20.0) -> None:
        self.config_path = config_path
        #: Go 固定 20s；这里留成字段便于测试调小。
        self.timeout = timeout
        #: 启动期能力自检快照（见 :meth:`bind_capabilities`）。
        self.capabilities: dict[str, Any] | None = None

    def bind_capabilities(self, capabilities: dict[str, Any] | None) -> None:
        """绑定启动期能力快照（可选）。

        由 :func:`atri_x.app.runtime_agent.build_agent` 在装配完成后调用；拿不到快照时
        ``/doctor`` 的能力检查标 ``skipped``，不会把"没检查"伪装成"通过"。
        """
        self.capabilities = (
            dict(capabilities) if isinstance(capabilities, dict) else None
        )

    async def doctor(self, ctx: Any = None) -> str:
        """对应 Go ``Doctor``。"""
        report = await run_doctor(
            ctx,
            DoctorOptions(
                config_path=self.config_path,
                skip_model=True,
                timeout=self.timeout,
                capabilities=self.capabilities,
            ),
        )
        return format_doctor_report(report)

    # Go 命名别名。
    Doctor = doctor


def format_doctor_report(report: DoctorReport) -> str:
    """渲染只读检查结果：列出每个失败项，全好时返回 ``Everything is OK``。

    对应 Go ``formatDoctorReport``。
    """
    failed = [check for check in report.checks if check.status == "failed"]
    if not failed:
        return "Everything is OK"
    parts = [f"配置检查发现 {len(failed)} 个问题："]
    for check in failed:
        line = f"\n- [{check.category}] {check.name}"
        if (check.detail or "").strip():
            line += "：" + check.detail
        if (check.error or "").strip():
            line += "：" + check.error
        parts.append(line)
    parts.append("\n\n请按上面的条目修复配置；本命令只读，不会修改任何文件。")
    return "".join(parts)


async def run_doctor(ctx: Any = None, opts: DoctorOptions | None = None) -> DoctorReport:
    """对应 Go ``RunDoctor``：确定性的部署验收。

    配置与本地依赖检查总是执行；端到端往返是 opt-in（它会把一条真实消息发过配置好
    的 CLI 远程服务器）。
    """
    options = opts if opts is not None else DoctorOptions()
    if options.timeout <= 0:
        options.timeout = DEFAULT_DOCTOR_TIMEOUT
    report = DoctorReport(
        config_ok=True, platform_ok=True, started_at=_now(), finished_at=None
    )

    def add(
        category: str,
        name: str,
        status: str,
        detail: str = "",
        err: BaseException | None = None,
    ) -> None:
        report.checks.append(
            DoctorCheck(
                category=category,
                name=name,
                status=status,
                detail=detail,
                error=redact_error(err) if err is not None else "",
            )
        )

    try:
        cfg = load_config(options.config_path)
    except Exception as exc:  # noqa: BLE001 - 加载失败即报告里的第一条失败项
        add("config", "load", "failed", err=exc)
        report.config_ok = False
        # 能力自检不依赖配置：脱敏、装配报告、Hook 控制面照样能查（限流需要配置，
        # 这里会明确标"配置未加载"而不是伪造"未配置"）。
        _run_capability_checks(report, add, None, options.capabilities)
        report.finished_at = _now()
        return report

    try:
        summary = check_config(cfg.config_path)
        add("config", "check", "passed", first_line(summary))
    except Exception as exc:  # noqa: BLE001
        add("config", "check", "failed", err=exc)
        report.config_ok = False

    try:
        check_writable_dirs(cfg)
    except Exception as exc:  # noqa: BLE001
        add("config", "storage_writable", "failed", err=exc)
        report.config_ok = False
    else:
        add(
            "config",
            "storage_writable",
            "passed",
            "sessions/chat-history directories are writable",
        )

    from atri_x.storage import SessionModeWork

    work_model = (getattr(cfg, "mode_models", None) or {}).get(SessionModeWork)
    provider = (getattr(cfg, "providers", None) or {}).get(
        getattr(work_model, "provider", "")
    )
    api_key = ""
    key_error: BaseException | None = None
    try:
        api_key = resolve_doctor_api_key(provider, os.path.dirname(cfg.config_path))
    except Exception as exc:  # noqa: BLE001
        key_error = exc
    label = f"{getattr(work_model, 'provider', '')}/{getattr(work_model, 'model', '')}"
    if key_error is not None:
        add("config", "model_credentials", "failed", label, key_error)
        report.config_ok = False
    else:
        add("config", "model_credentials", "passed", label)

    _run_capability_checks(report, add, cfg, options.capabilities)

    health_url = ""
    try:
        health_url = doctor_health_url(cfg)
    except Exception as exc:  # noqa: BLE001
        add("port", "health_listen", "failed", err=exc)
        report.config_ok = False
    else:
        if not health_url:
            add("port", "health_listen", "skipped", "ATRIX_HEALTH_ADDR is not configured")
        else:
            try:
                probe_application_health(ctx, health_url)
            except Exception as exc:  # noqa: BLE001
                add("port", "health_listen", "failed", health_url, exc)
                report.config_ok = False
            else:
                add("port", "health_listen", "passed", health_url)

    enabled = enabled_platform_names(cfg)
    add(
        "platform",
        "configured",
        "passed",
        "no platform is enabled" if not enabled else ", ".join(enabled),
    )
    if health_url:
        status, detail, err = await _platform_connection(
            ctx, cfg, health_url, enabled, options.require_platform
        )
        add("platform", "connection", status, detail, err)
        if status == "failed":
            report.platform_ok = False
    elif options.require_platform and enabled:
        add(
            "platform",
            "connection",
            "failed",
            "health endpoint is not configured",
            LookupError("cannot verify platform connectivity without ATRIX_HEALTH_ADDR"),
        )
        report.platform_ok = False
    else:
        add("platform", "connection", "skipped", "health endpoint is not configured")

    if options.skip_model:
        add("model", "chat_call", "skipped", "model call skipped by option")
    elif key_error is not None:
        add("model", "chat_call", "failed", label, key_error)
        report.config_ok = False
    else:
        try:
            await doctor_model_call(
                ctx,
                cfg,
                getattr(work_model, "provider", ""),
                provider,
                api_key,
                getattr(work_model, "model", ""),
            )
        except Exception as exc:  # noqa: BLE001
            add("model", "chat_call", "failed", label, exc)
            report.config_ok = False
        else:
            add("model", "chat_call", "passed", label)

    if options.e2e:
        try:
            await doctor_cli_round_trip(ctx, cfg, options.timeout)
        except Exception as exc:  # noqa: BLE001
            add("e2e", "cli_roundtrip", "failed", err=exc)
            report.e2e_ok = False
        else:
            add(
                "e2e",
                "cli_roundtrip",
                "passed",
                "CLI remote message round-trip completed",
            )
            report.e2e_ok = True
    else:
        add(
            "e2e",
            "cli_roundtrip",
            "skipped",
            "use --e2e to send a real message through the CLI remote server",
        )
        report.e2e_ok = False

    report.finished_at = _now()
    return report


# ---------------------------------------------------------------------------
# 能力自检（Python 版新增）
# ---------------------------------------------------------------------------

#: 五项核心装配（与 :data:`atri_x.agent.bootstrap._CORE_INSTALLERS` 同名）。
_CORE_SERVICE_NAMES: tuple[str, ...] = (
    "models",
    "context_runtime",
    "budget",
    "ratelimit",
    "tool_directive",
)

#: 未配置入站限流时的说明（``[ops]`` 里两个 per-minute 阈值都为 0 = 不限制）。
_RATE_LIMIT_DISABLED_DETAIL = (
    "not configured: [ops] user/group_messages_per_minute are 0 (0 = unlimited)"
)


def _run_capability_checks(
    report: DoctorReport, add: Callable[..., None], cfg: Any, capabilities: Any
) -> None:
    """能力自检：核心装配、脱敏、入站限流、Hook 控制面、图像后端。

    有启动期快照（``/doctor`` 在 service 模式下由 runtime_agent 绑定）时按快照断言；
    没有快照（``python -m atri_x doctor``）时只做静态可判定的事，其余标 ``skipped``。
    检查不到 ≠ 通过：这里绝不把"没有证据"写成 ``passed``。
    """
    snapshot = capabilities if isinstance(capabilities, dict) else None
    _check_agent_services(report, add, snapshot)
    _check_redaction(report, add, snapshot)
    _check_inbound_rate_limit(report, add, cfg, snapshot)
    _check_hook_service(report, add, snapshot)
    _check_image_backend(report, add)
    _check_time_service(report, add, cfg)


def _check_time_service(report: DoctorReport, add: Callable[..., None], cfg: Any) -> None:
    """时间能力自检（Python 版新增，1.0.10）。

    这是**冻结产物里"能不能真加载 IANA 时区"的唯一探针**：``doctor`` 不依赖
    Agent / 网络 / 数据库，所以可以在干净的 Windows 安装包、便携包与容器里直接跑。
    """
    from atri_x.time_service import TimeService
    from atri_x.timezones import iana_zone_count, tzdata_version, zoneinfo_available
    from atri_x.timezones import resolve_timezone

    section = getattr(cfg, "time", None)
    configured = str(getattr(section, "timezone", "") or "")
    zones = iana_zone_count()
    version = tzdata_version()
    if not zoneinfo_available():
        add(
            "time",
            "tzdata",
            "failed",
            "no IANA timezone data: install the tzdata package or check that the frozen "
            "build bundles _internal/tzdata",
        )
        report.config_ok = False
        return
    add(
        "time",
        "tzdata",
        "passed",
        "zones=%d tzdata=%s" % (zones, version or "unknown"),
    )
    try:
        resolution = resolve_timezone(configured or "Asia/Shanghai")
        service = TimeService.from_config(section)
        snapshot = service.snapshot()
        offset = snapshot.current_time.utcoffset()
        add(
            "time",
            "timezone",
            "passed",
            "timezone=%s rules=%s utc_offset=%s tzdata=%s"
            % (
                snapshot.timezone,
                resolution.rules,
                _format_utc_offset(offset),
                version or "unknown",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 时区不可用就是部署失败
        add("time", "timezone", "failed", "%s: %s" % (type(exc).__name__, exc))
        report.config_ok = False
        return
    verification = getattr(section, "verification", None)
    enabled = bool(getattr(verification, "enabled", False))
    sources = [s for s in (getattr(section, "sources", None) or []) if getattr(s, "enabled", False)]
    inject = bool(getattr(section, "inject_context", True))
    add(
        "time",
        "injection",
        "passed" if inject else "skipped",
        "prompt time injection is %s" % ("on" if inject else "off ([time].inject_context=false)"),
    )
    if enabled and not sources:
        add(
            "time",
            "verification",
            "skipped",
            "network verification is enabled but no source is enabled; "
            "add [[time.sources]] with enabled = true",
        )
    elif enabled:
        add(
            "time",
            "verification",
            "passed",
            "verify_only, sources=%d (network state is not checked here)"
            % len(sources),
        )
    else:
        add(
            "time",
            "verification",
            "skipped",
            "network verification is disabled ([time.verification].enabled=false)",
        )


def _format_utc_offset(offset: Any) -> str:
    if offset is None:
        return "?"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)


def _check_image_backend(report: DoctorReport, add: Callable[..., None]) -> None:
    """报告实际生效的图像后端（Pillow 加速 / 纯标准库）。

    探测唯一出处是 :func:`atri_x.media.imagecodec.backend_probe.image_backend_info`。
    纯标准库是**受支持**的默认形态（功能完整、速度较慢），所以这里只报告事实、
    不判失败；``version`` 是本机装着的 Pillow 版本，装了但被
    ``ATRI_X_IMAGECODEC_BACKEND=pure`` 强制关掉时会同时出现。
    """
    del report  # 后端选择不影响 config_ok
    info = image_backend_info()
    version = str(info.get("version") or "")
    if str(info.get("backend") or "") == "pillow":
        add("media", "image_backend", "passed", "pillow %s" % (version or "unknown"))
        return
    detail = "stdlib (pure-Python codec); "
    detail += "Pillow %s installed but not in effect" % version if version else "Pillow not installed"
    add("media", "image_backend", "skipped", detail)


def _check_agent_services(
    report: DoctorReport, add: Callable[..., None], snapshot: dict[str, Any] | None
) -> None:
    """五项核心装配器是否全部 ``installed``（有快照才做运行期断言）。"""
    services = (snapshot or {}).get("agent_services")
    if isinstance(services, dict):
        installed = _entry_names(services.get("installed"))
        missing = [name for name in _CORE_SERVICE_NAMES if name not in installed]
        reasons = "; ".join(
            "%s: %s" % (pair[0], pair[1])
            for pair in services.get("failed") or []
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        )
        if missing or services.get("failed"):
            detail = "core assemblers missing: " + (", ".join(missing) or "-")
            if reasons:
                detail += "; failed: " + reasons
            add("agent", "services", "failed", detail)
            report.config_ok = False
            return
        add(
            "agent",
            "services",
            "passed",
            "core assemblers installed: " + ",".join(_CORE_SERVICE_NAMES),
        )
        return

    available, total = _core_assembler_availability()
    detail = (
        "no live assembly report (doctor runs without an Agent); "
        "core installer modules importable: %d/%d" % (available, total)
    )
    if available < total:
        add("agent", "services", "failed", detail)
        report.config_ok = False
        return
    add("agent", "services", "skipped", detail)


def _check_redaction(
    report: DoctorReport, add: Callable[..., None], snapshot: dict[str, Any] | None
) -> None:
    """日志脱敏是否接通（``REDACT_AVAILABLE``）。"""
    status = (snapshot or {}).get("redaction")
    if not isinstance(status, dict):
        status = _redaction_status()
    if bool(status.get("available", False)):
        add("security", "redaction", "passed", "strict redaction is wired (atri_x.redact)")
        return
    add(
        "security",
        "redaction",
        "failed",
        "degraded to regex fallback: atri_x.redact is unavailable; "
        "log attributes may leak secrets",
    )
    report.config_ok = False


def _check_inbound_rate_limit(
    report: DoctorReport,
    add: Callable[..., None],
    cfg: Any,
    snapshot: dict[str, Any] | None,
) -> None:
    """入站限流是否配置（``[ops]`` 的两个 per-minute 阈值；0 = 不限制）。"""
    del report  # 未配置按文档语义是合法选择，不算失败
    if snapshot is not None and "rate_limit_configured" in snapshot:
        configured = bool(snapshot.get("rate_limit_configured"))
    elif cfg is None:
        add(
            "ops",
            "inbound_rate_limit",
            "skipped",
            "config is not loaded; rate limit configuration is unknown",
        )
        return
    else:
        configured = _rate_limit_configured_from_config(cfg)
    if configured:
        detail = _rate_limit_config_detail(cfg) or "limiter objects are wired"
        add("ops", "inbound_rate_limit", "passed", detail)
        return
    add("ops", "inbound_rate_limit", "skipped", _RATE_LIMIT_DISABLED_DETAIL)


def _check_hook_service(
    report: DoctorReport, add: Callable[..., None], snapshot: dict[str, Any] | None
) -> None:
    """Hook 控制面（``deps.hooks``）是否接线。"""
    if snapshot is None or "hook_service" not in snapshot:
        add(
            "agent",
            "hook_service",
            "skipped",
            "requires a live Agent; doctor ran without one",
        )
        return
    if bool(snapshot.get("hook_service")):
        add(
            "agent",
            "hook_service",
            "passed",
            "hook control service is wired (deps.hooks)",
        )
        return
    add(
        "agent",
        "hook_service",
        "failed",
        "hook control service is not wired: /hooks would report "
        "'hook service is not configured'",
    )
    report.config_ok = False


def _entry_names(entries: Any) -> set[str]:
    """把 ``report.to_dict()`` 里的 ``[(name, detail), ...]`` 折成名字集合。"""
    names: set[str] = set()
    for entry in entries or ():
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, (list, tuple)) and entry:
            names.add(str(entry[0]))
    return names


def _core_assembler_availability() -> tuple[int, int]:
    """静态探测核心装配器入口是否都在（不执行装配，也不构造 Agent）。"""
    from importlib import import_module

    try:
        from atri_x.agent.bootstrap import _CORE_INSTALLERS as installers
    except Exception:  # noqa: BLE001 - 装配表都取不到时按"全部不可用"处理
        return 0, len(_CORE_SERVICE_NAMES)
    available = 0
    for _name, module_name, function_name in installers:
        try:
            module = import_module(module_name)
        except Exception:  # noqa: BLE001 - 缺模块就是不可用，异常细节不重要
            continue
        if callable(getattr(module, function_name, None)):
            available += 1
    return available, len(installers)


def _redaction_status() -> dict[str, Any]:
    """脱敏自检快照；导入失败按"不可用"处理（保守方向）。"""
    try:
        from atri_x.logging.logging import redact_status

        return dict(redact_status())
    except Exception:  # noqa: BLE001 - 自检失败按不可用处理，不阻断 doctor
        return {"available": False, "mode": "unknown"}


def _rate_limit_configured_from_config(cfg: Any) -> bool:
    """从 ``[ops]`` 读入站限流是否配置（与 runtime_agent 的构造条件一致）。"""
    ops = getattr(cfg, "ops", None)
    for name in ("user_messages_per_minute", "group_messages_per_minute"):
        try:
            if int(getattr(ops, name, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _rate_limit_config_detail(cfg: Any) -> str:
    """把已配置的阈值渲染成一行（``user=12/min, group=60/min``）。"""
    ops = getattr(cfg, "ops", None)
    parts: list[str] = []
    for label, name in (
        ("user", "user_messages_per_minute"),
        ("group", "group_messages_per_minute"),
    ):
        try:
            value = int(getattr(ops, name, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            parts.append("%s=%d/min" % (label, value))
    return ", ".join(parts)


async def _platform_connection(
    ctx: Any, cfg: Any, health_url: str, enabled: list[str], require: bool
) -> tuple[str, str, BaseException | None]:
    """对应 Go 里 ``/healthz`` 快照 + ``doctorPlatformConnection`` 的那两段。"""
    ops_token = resolve_doctor_ops_token(cfg)
    try:
        snapshot = fetch_health_snapshot(ctx, health_url, ops_token)
    except Exception as exc:  # noqa: BLE001
        if not ops_token and exc is ErrHealthzDisabled and not require:
            return "skipped", str(exc), None
        return "failed", health_url, exc
    return doctor_platform_connection(snapshot, enabled, require)


# Go 命名别名。
doctorService = DoctorService
RunDoctor = run_doctor
formatDoctorReport = format_doctor_report
