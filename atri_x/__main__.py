"""``cmd/atri-x`` 的 Python 移植：进程入口 ``python -m atri_x``。

对应 Go：``cmd/atri-x/main.go``（116 行生产代码）。

职责（逐条对齐 Go 的 ``main``）：

1. 记下进程启动时间；
2. :func:`atri_x.launcher.parse_args` 解析命令行，失败 → stderr 报错 + 用法，退出码 **2**；
3. ``--help`` → 用法到 stdout，退出 0；``--version`` → ``atri-x <版本>``，退出 0；
4. ``config check`` / ``doctor`` / ``completion`` 三个子命令；
5. 其余走应用：``cli`` 模式只跑远程 CLI 客户端；``auto`` 模式先试远程客户端、
   失败且未点名 client 时回落全前台；最后 :func:`atri_x.app.run` 全前台。

与 Go 的形式差异：

* ``signal.NotifyContext(ctx, os.Interrupt, SIGTERM)`` → 一处 :class:`asyncio.Event`
  （``stop``）+ :func:`signal.signal` 处理器；它同时充当 Go 的 ``ctx`` 传给
  :mod:`atri_x.app`（该层只要求 ``is_set()`` / ``wait()``，``asyncio.Event`` 两者都有）。
  Windows 上 :meth:`asyncio.loop.add_signal_handler` 不可用，所以必须走
  :func:`signal.signal`（本函数在主线程运行）。
* ``errors.Is(err, context.Canceled)`` → :func:`_is_cancelled`（``asyncio.CancelledError``
  或 :class:`atri_x.safecontext.CancelledError`）。
* ``json.NewEncoder(os.Stdout)`` 的 ``SetIndent("", "  ")`` → :func:`_doctor_report_json`：
  键名 / 字段顺序（``config_ok`` / ``platform_ok`` / ``e2e_ok`` / ``checks`` /
  ``started_at`` / ``finished_at``）、``omitempty``（``detail`` / ``error``）与 Go 的
  HTML 转义逐条复刻，因为它是 ``doctor --json`` 的对外契约。
* **``atri_x.app``（以及各平台包）一律函数内延迟导入**：``--help`` / ``--version`` /
  ``completion`` / ``--health-probe`` 因此不依赖装配层，并行移植期也能跑。

**Python 版新增**：``--health-probe``（Go 上游没有这个开关；由 ``deploy/Dockerfile``
的 ``HEALTHCHECK`` 与 ``devdocs/porting-status.md`` 第 2 节第 7 条约定）。契约见
:func:`run_health_probe`——探 ``/live``，2xx → 退出码 0，其余 → 1。待 Lead 确认。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import signal
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence, TextIO
from urllib.parse import urlsplit

from atri_x import __version__
from atri_x import launcher
from atri_x.errors import CLIClientFallbackError
from atri_x.safecontext import CancelledError as ContextCancelledError

__all__ = [
    "main",
    "version",
    "run_health_probe",
    "health_probe_url",
    "doctor_report_json",
    "doctor_check_payload",
    "split_address",
    "join_host_port",
    "rfc3339_nano",
    "ZERO_TIME_TEXT",
    "HEALTH_PROBE_TIMEOUT",
    "DEFAULT_HEALTH_PORT",
]

#: Go ``var version = "dev"``（构建期用 ``-ldflags`` 注入）；Python 侧取包版本，
#: 打包脚本要覆盖它直接改 :mod:`atri_x` 的 ``__version__`` 即可。
version = __version__

#: Go ``time.Time{}`` 的 JSON 形态（``json.Marshal`` 对零值时间就输出它）。
ZERO_TIME_TEXT = "0001-01-01T00:00:00Z"

#: ``--health-probe`` 的单次请求超时；与 ``deploy/health_probe.py`` 的默认值一致。
HEALTH_PROBE_TIMEOUT = 3.0

#: 健康接口默认端口（``deploy/Dockerfile`` 的 ``ATRIX_HEALTH_ADDR``）。
DEFAULT_HEALTH_PORT = 32171


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """进程入口，返回退出码。对应 Go ``main``（Go 用 ``os.Exit``，这里用返回值）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    # Go 的 ``startedAt`` 在解析参数**之前**取，这里保持一致。
    started_at = datetime.now(timezone.utc)

    try:
        opts = launcher.parse_args(args)
    except Exception as exc:  # noqa: BLE001 - 参数错误一律报错 + 用法 + 退出码 2
        _err(f"atri-x: {_error_text(exc)}\n\n")
        launcher.write_usage(sys.stderr)
        return 2

    if opts.help:
        launcher.write_usage(sys.stdout)
        return 0
    if opts.version:
        _out(f"atri-x {version}\n")
        return 0
    if opts.health_probe:
        # Python 版新增（见模块 docstring），Go 上游没有这个开关。
        return run_health_probe(opts)

    if str(opts.command) == str(launcher.COMMAND_CONFIG_CHECK):
        return _run_config_check(opts)
    if str(opts.command) == str(launcher.COMMAND_DOCTOR):
        return _run_doctor(opts)
    if str(opts.command) == str(launcher.COMMAND_COMPLETION):
        try:
            launcher.write_completion(sys.stdout, opts.completion)
        except Exception as exc:  # noqa: BLE001 - 退出码 2，与 Go 一致
            _err(f"atri-x: {_error_text(exc)}\n")
            return 2
        return 0

    return _run_application(opts, started_at)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _run_config_check(opts: launcher.Options) -> int:
    """``atri-x config check``。对应 Go ``app.CheckConfig`` 那一段。"""
    app = _app()
    try:
        summary = app.check_config(opts.config_path)
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - 与 Go 一致：退出码 1
        _err(f"atri-x: {_error_text(exc)}\n")
        return 1
    _out(str(summary))
    return 0


def _run_doctor(opts: launcher.Options) -> int:
    """``atri-x doctor``。对应 Go ``app.RunDoctor`` 那一段。"""
    app = _app()
    try:
        report = asyncio.run(
            app.run_doctor(
                None,  # Go 传 context.Background()
                app.DoctorOptions(
                    config_path=opts.config_path,
                    e2e=opts.doctor_e2e,
                    json=opts.doctor_json,
                    skip_model=opts.doctor_no_model,
                    require_platform=opts.doctor_require_platform,
                ),
            )
        )
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - 同 Go：报错即 1
        _err(f"atri-x doctor: {_error_text(exc)}\n")
        return 1

    if opts.doctor_json:
        try:
            _out(doctor_report_json(report))
        except Exception as exc:  # noqa: BLE001
            _err(f"atri-x doctor: {_error_text(exc)}\n")
            return 1
    else:
        for check in getattr(report, "checks", None) or []:
            line = "[%s] %-16s %-7s %s" % (
                check.category,
                check.name,
                check.status,
                check.detail,
            )
            if check.error:
                line += " error=" + check.error
            _out(line + "\n")
        _out(
            "config_ok=%s platform_ok=%s e2e_ok=%s\n"
            % (
                _go_bool(report.config_ok),
                _go_bool(report.platform_ok),
                _go_bool(report.e2e_ok),
            )
        )

    if (
        not report.config_ok
        or not report.platform_ok
        or (opts.doctor_e2e and not report.e2e_ok)
    ):
        return 1
    return 0


def _run_application(opts: launcher.Options, started_at: datetime) -> int:
    """跑应用：``cli`` / ``auto`` / 全前台。对应 Go ``main`` 的后半段。"""
    try:
        return asyncio.run(_run_application_async(opts, started_at))
    except KeyboardInterrupt:  # 与 Go 的 ctx.Canceled 等价：优雅退出
        return 0


async def _run_application_async(opts: launcher.Options, started_at: datetime) -> int:
    app = _app()
    # Go ``signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)``。
    stop = asyncio.Event()
    installed = _install_stop_signals(stop)
    try:
        client_opts = app.CLIClientOptions(
            config_path=opts.config_path, client_name=opts.client_name
        )
        mode = str(opts.mode)
        if mode == str(app.RUN_MODE_CLI_ONLY):
            try:
                await app.run_cli_client(stop, client_opts)
            except BaseException as exc:  # noqa: BLE001
                return _run_outcome(exc)
            return 0
        if mode == str(app.RUN_MODE_AUTO):
            try:
                await app.try_run_cli_client(stop, client_opts)
                return 0
            except BaseException as exc:  # noqa: BLE001
                if not isinstance(exc, CLIClientFallbackError):
                    return _run_outcome(exc)
                # ErrCLIClientFallback：回落全前台模式。
        try:
            await app.run(
                stop,
                app.Options(
                    config_path=opts.config_path,
                    version=version,
                    started_at=started_at,
                    mode=opts.mode,
                ),
            )
        except BaseException as exc:  # noqa: BLE001
            return _run_outcome(exc)
        return 0
    finally:
        _restore_signals(installed)


def _run_outcome(exc: BaseException) -> int:
    """统一收尾：取消 → 0（Go ``errors.Is(err, context.Canceled)``），其余 → 1。

    ``KeyboardInterrupt`` 也走 0：Go 侧 Ctrl+C 会让 ``ctx`` 被取消、``main`` 直接
    ``return``，退出码同样是 0。
    """
    if _is_cancelled(exc) or isinstance(exc, KeyboardInterrupt):
        return 0
    _err(f"atri-x: {_error_text(exc)}\n")
    return 1


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------


def _install_stop_signals(stop: asyncio.Event) -> list[tuple[int, Any]]:
    """把 SIGINT / SIGTERM 变成 ``stop.set()``，返回 ``[(信号, 原处理器)]``。

    Windows 上 :meth:`asyncio.loop.add_signal_handler` 未实现，所以走
    :func:`signal.signal`；非主线程调用会抛 ``ValueError``，此时跳过（不致命）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 只会在没有事件循环时发生
        return []

    def _handler(_signum: int, _frame: Any, _loop: Any = loop, _stop: Any = stop) -> None:
        try:
            _loop.call_soon_threadsafe(_stop.set)
        except RuntimeError:  # pragma: no cover - 事件循环已关闭
            pass

    installed: list[tuple[int, Any]] = []
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:  # pragma: no cover - 平台缺该信号
            continue
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, _handler)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover - 非主线程
            continue
        installed.append((signum, previous))
    return installed


def _restore_signals(installed: list[tuple[int, Any]]) -> None:
    for signum, previous in installed:
        try:
            signal.signal(signum, previous)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover
            pass


def _is_cancelled(exc: BaseException) -> bool:
    """``errors.Is(err, context.Canceled)`` 的等价物。"""
    return isinstance(exc, (asyncio.CancelledError, ContextCancelledError))


# ---------------------------------------------------------------------------
# doctor --json
# ---------------------------------------------------------------------------


def doctor_report_json(report: Any) -> str:
    """把 :class:`atri_x.app.DoctorReport` 编码成 Go ``json.Encoder`` 的形态。

    Go 的 ``json.Encoder`` 默认开启 HTML 转义，且 ``Encode`` 末尾补一个换行；
    两者都在这里复刻。
    """
    payload = {
        "config_ok": bool(report.config_ok),
        "platform_ok": bool(report.platform_ok),
        "e2e_ok": bool(report.e2e_ok),
        "checks": [doctor_check_payload(check) for check in (report.checks or [])],
        "started_at": rfc3339_nano(getattr(report, "started_at", None)),
        "finished_at": rfc3339_nano(getattr(report, "finished_at", None)),
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    return _escape_json_html(text) + "\n"


def doctor_check_payload(check: Any) -> dict[str, Any]:
    """单项检查的 JSON 对象；``detail`` / ``error`` 带 ``omitempty``。"""
    payload: dict[str, Any] = {
        "category": check.category,
        "name": check.name,
        "status": check.status,
    }
    if check.detail:
        payload["detail"] = check.detail
    if check.error:
        payload["error"] = check.error
    return payload


def rfc3339_nano(value: datetime | None) -> str:
    """``datetime`` → Go ``time.Time`` 的 JSON 文本（``RFC3339Nano``）。

    ``None`` 与 Go 的零值时间都输出 :data:`ZERO_TIME_TEXT`。小数秒按 Go 的规则
    去掉末尾的 0（``.100000`` → ``.1``），整秒时不输出小数点。
    """
    if value is None or value.year <= 1:
        return ZERO_TIME_TEXT
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    text = "%04d-%02d-%02dT%02d:%02d:%02d" % (
        moment.year,
        moment.month,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
    )
    if moment.microsecond:
        text += "." + ("%06d" % moment.microsecond).rstrip("0")
    return text + "Z"


def _escape_json_html(text: str) -> str:
    """Go ``encoding/json`` 的默认 HTML 转义（``<`` / ``>`` / ``&`` / U+2028/9）。"""
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# ---------------------------------------------------------------------------
# Python 版新增：--health-probe（待 Lead 确认）
# ---------------------------------------------------------------------------


def run_health_probe(opts: launcher.Options) -> int:
    """跑一次健康探针并按结果返回退出码。**Python 版新增**，Go 上游无对应物。

    契约（来自 ``deploy/Dockerfile`` 的 ``HEALTHCHECK`` 与
    ``devdocs/porting-status.md`` 第 2 节第 7 条）：

    * 探 ``GET <addr>/live``，2xx → **0**；非 2xx、连不上、超时 → **1**；
    * ``<addr>`` 取 ``--health-probe=<URL|host:port|/path>``，其次 ``ATRIX_HEALTH_ADDR``，
      最后 ``127.0.0.1:32171``；**不读配置文件**（HEALTHCHECK 场景下没有配置）；
    * 语义边界与 ``/live`` 一致：只看进程是否还能响应，不判断调度心跳、模型 API
      与平台连通性；要判断"就绪"用 ``--health-probe=/ready``（或完整 URL）。
    """
    try:
        explicit = (opts.health_probe_url or "").strip()
        if explicit.startswith("/"):
            # ``--health-probe=/ready``：把路径套在环境变量 / 默认地址上。
            url = health_probe_url(explicit, launcher.health_probe_default_address())
        else:
            url = health_probe_url(launcher.health_probe_address(opts))
    except Exception as exc:  # noqa: BLE001 - 地址非法同样算"不健康"
        _err(f"atri-x: health probe address: {_error_text(exc)}\n")
        return 1
    try:
        status, body = _http_get(url, HEALTH_PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 连不上/超时/协议错误
        _err(f"atri-x: health probe {url}: {_error_text(exc)}\n")
        return 1
    if 200 <= status < 300:
        text = body.decode("utf-8", "replace").strip() if body else ""
        if text:
            _out(text + "\n")
        return 0
    _err(f"atri-x: health probe {url}: HTTP {status}\n")
    return 1


def health_probe_url(spec: str, base: str = "") -> str:
    """把探测目标规格化成完整 URL。

    接受三种写法（与 ``deploy/health_probe.py`` 的取舍一致）：

    * 完整 URL —— ``http://host:port/live``；没有路径时自动补 ``/live``；
    * 路径 —— ``/live`` / ``/ready`` / ``/healthz``，套在 ``base``
      （默认 :data:`atri_x.launcher.DEFAULT_HEALTH_PROBE_ADDR`）上；
    * ``host:port`` / ``host`` —— 补成 ``http://host[:port]/live``；``0.0.0.0`` /
      ``::`` / 空 host 一律归一成回环（探测端不能真的去连连通地址）。
    """
    text = (spec or "").strip()
    if text.startswith("/"):
        return "http://" + (base or launcher.DEFAULT_HEALTH_PROBE_ADDR).rstrip("/") + text
    if not text:
        text = base or launcher.DEFAULT_HEALTH_PROBE_ADDR
    if "://" in text:
        parsed = urlsplit(text)
        if not parsed.path:
            return text.rstrip("/") + "/live"
        return text
    host, port = split_address(text)
    return f"http://{join_host_port(host, port)}/live"


def split_address(text: str) -> tuple[str, str]:
    """``host:port`` → ``(host, port)``；省略端口时用 :data:`DEFAULT_HEALTH_PORT`。"""
    host = ""
    port = ""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError(f"invalid address {text!r}")
        host = text[1:end]
        rest = text[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"invalid address {text!r}")
            port = rest[1:]
    elif ":" in text:
        host, _, port = text.rpartition(":")
    else:
        host = text
    port = port.strip() or str(DEFAULT_HEALTH_PORT)
    if not port.isdigit():
        raise ValueError(f"invalid address {text!r}")
    if host.strip() in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return host, port


def join_host_port(host: str, port: str) -> str:
    """拼接 ``host:port``；IPv6 补方括号。"""
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _http_get(url: str, timeout: float) -> tuple[int, bytes]:
    """GET 一次；HTTP 错误码也当结果返回（不抛），只有连接层错误才抛。"""
    request = urllib.request.Request(url, headers={}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------


def _app() -> Any:
    """延迟导入 :mod:`atri_x.app`。

    这样 ``--help`` / ``--version`` / ``completion`` / ``--health-probe`` 不依赖
    装配层（也不依赖并行移植期尚未交付的兄弟模块）。
    """
    return importlib.import_module("atri_x.app")


def _error_text(exc: BaseException) -> str:
    """``fmt`` 的 ``%v``：空消息时退回异常类名，避免打出空行。"""
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _go_bool(value: Any) -> str:
    """Go ``%v`` 对 ``bool`` 的输出。"""
    return "true" if value else "false"


def _out(text: str) -> None:
    _write(sys.stdout, text)


def _err(text: str) -> None:
    _write(sys.stderr, text)


def _write(stream: TextIO | None, text: str) -> None:
    """写文本，并按 PORTING §7.4 兜底编码（GBK 控制台不能表示时退到 ASCII 转义）。"""
    target = stream if stream is not None else sys.stdout
    try:
        target.write(text)
    except UnicodeEncodeError:
        # 目标流写不了 → 降级成纯 ASCII 的 ``\\uXXXX`` 转义，保证这一行一定写得出去。
        # 否则"报告一次编码问题"会变成"报告器自己崩掉"。
        target.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    flush = getattr(target, "flush", None)
    if callable(flush):
        flush()


if __name__ == "__main__":  # pragma: no cover - 进程引导
    raise SystemExit(main())
