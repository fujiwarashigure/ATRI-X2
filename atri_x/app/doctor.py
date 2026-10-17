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
from typing import Any

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
    """对应 Go ``DoctorOptions``。"""

    config_path: str = ""
    e2e: bool = False
    json: bool = False
    skip_model: bool = False
    require_platform: bool = False
    timeout: float = 0.0


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

    async def doctor(self, ctx: Any = None) -> str:
        """对应 Go ``Doctor``。"""
        report = await run_doctor(
            ctx,
            DoctorOptions(
                config_path=self.config_path, skip_model=True, timeout=self.timeout
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
