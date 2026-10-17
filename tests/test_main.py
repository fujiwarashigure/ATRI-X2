"""``atri_x/__main__.py``（对应 Go ``cmd/atri-x``）的单元测试。

覆盖：

* 进程入口的三条硬路径：``--help``（**没有配置文件也必须返回 0**）、``--version``、
  参数错误（退出码 2 + 用法进 stderr）；
* 子命令分发：``config check`` / ``doctor``（含 ``--json`` 的键名契约）/ ``completion``；
* 应用路径：``cli`` / ``auto``（含 ``ErrCLIClientFallback`` 回落）/ 全前台，
  以及「取消 → 0、其它异常 → 1」的退出码语义；
* **Python 版新增的 ``--health-probe``**：真起一个回环 HTTP 服务器验证 0/1 退出码、
  地址优先级、以及"不依赖 ``atri_x.app`` 也能跑"这条约束。

``atri_x.app`` 一律用 :class:`FakeAppModule` 注入 ``sys.modules``（入口层是
**延迟导入**，所以注入即可，不需要碰生产代码的接缝）。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import http.server
import io
import json
import os
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from atri_x import __main__ as entry
from atri_x.errors import AtriXError, ErrCLIClientFallback

# ---------------------------------------------------------------------------
# 假 app 模块
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeOptions:
    config_path: str = ""
    version: str = ""
    started_at: object = None
    mode: str = ""


@dataclasses.dataclass
class FakeCLIClientOptions:
    config_path: str = ""
    client_name: str = ""


@dataclasses.dataclass
class FakeDoctorOptions:
    config_path: str = ""
    e2e: bool = False
    json: bool = False
    skip_model: bool = False
    require_platform: bool = False


@dataclasses.dataclass
class FakeDoctorCheck:
    category: str = ""
    name: str = ""
    status: str = ""
    detail: str = ""
    error: str = ""


@dataclasses.dataclass
class FakeDoctorReport:
    config_ok: bool = True
    platform_ok: bool = True
    e2e_ok: bool = False
    checks: list = dataclasses.field(default_factory=list)
    started_at: datetime = dataclasses.field(
        default_factory=lambda: datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
    )
    finished_at: datetime | None = None


class FakeAppModule:
    """记录调用并按需抛错的 ``atri_x.app`` 替身。"""

    def __init__(self) -> None:
        self.cli_calls: list = []
        self.try_calls: list = []
        self.run_calls: list = []
        self.check_calls: list = []
        self.doctor_calls: list = []
        self.cli_error: BaseException | None = None
        self.try_error: BaseException | None = None
        self.run_error: BaseException | None = None
        self.check_summary = "config OK\npath: /cfg/app.toml\n"
        self.check_error: BaseException | None = None
        self.report = FakeDoctorReport()
        self.doctor_error: BaseException | None = None

    def module(self) -> types.ModuleType:
        module = types.ModuleType("atri_x.app")
        module.RUN_MODE_AUTO = "auto"
        module.RUN_MODE_FULL = "full"
        module.RUN_MODE_CLI_ONLY = "cli"
        module.RUN_MODE_SERVICE = "service"
        module.Options = FakeOptions
        module.CLIClientOptions = FakeCLIClientOptions
        module.DoctorOptions = FakeDoctorOptions
        module.DoctorCheck = FakeDoctorCheck
        module.DoctorReport = FakeDoctorReport
        module.check_config = self._check_config
        module.run_doctor = self._run_doctor
        module.run_cli_client = self._run_cli_client
        module.try_run_cli_client = self._try_run_cli_client
        module.run = self._run
        return module

    def _check_config(self, config_path: str) -> str:
        self.check_calls.append(config_path)
        if self.check_error is not None:
            raise self.check_error
        return self.check_summary

    async def _run_cli_client(self, ctx, opts) -> None:
        self.cli_calls.append((ctx, opts))
        if self.cli_error is not None:
            raise self.cli_error

    async def _try_run_cli_client(self, ctx, opts) -> None:
        self.try_calls.append((ctx, opts))
        if self.try_error is not None:
            raise self.try_error

    async def _run(self, ctx, opts) -> None:
        self.run_calls.append((ctx, opts))
        if self.run_error is not None:
            raise self.run_error

    async def _run_doctor(self, ctx, opts):
        self.doctor_calls.append((ctx, opts))
        if self.doctor_error is not None:
            raise self.doctor_error
        return self.report


class MainRunnerMixin:
    """把 :func:`atri_x.__main__.main` 跑一遍并捕获输出。"""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(argv)
        return code, out.getvalue(), err.getvalue()


class MainTestBase(MainRunnerMixin, unittest.TestCase):
    def setUp(self):
        self.fake = FakeAppModule()
        patcher = mock.patch.dict(sys.modules, {"atri_x.app": self.fake.module()})
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# 进程入口的三条硬路径
# ---------------------------------------------------------------------------


class TestHelpAndVersion(unittest.TestCase):
    """**``--help`` 在没有配置文件的情况下必须正常打印帮助并返回 0。**"""

    def test_help_without_config(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage:", out.getvalue())
        self.assertIn("atri-x doctor", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_help_short_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(entry.main(["-h"]), 0)
        self.assertIn("Usage:", out.getvalue())

    def test_help_does_not_import_app(self):
        """``--help`` 不依赖装配层：``atri_x.app`` 不可用时也必须返回 0。"""
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"atri_x.app": None}):
            with contextlib.redirect_stdout(out):
                code = entry.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage:", out.getvalue())

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(entry.main(["--version"]), 0)
        self.assertEqual(out.getvalue(), f"atri-x {entry.version}\n")

    def test_version_ignores_missing_config(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = entry.main(["--version", "--config", "does/not/exist.toml"])
        self.assertEqual(code, 0)


class TestArgumentErrors(MainRunnerMixin, unittest.TestCase):
    def test_unknown_command_exits_2_with_usage(self):
        code, out, err = self.run_main(["wat"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("atri-x: unknown command: wat", err)
        self.assertIn("Usage:", err)

    def test_missing_value_exits_2(self):
        code, _out, err = self.run_main(["--config"])
        self.assertEqual(code, 2)
        self.assertIn("--config requires a value", err)

    def test_unknown_option_exits_2(self):
        code, _out, err = self.run_main(["--name", "local"])
        self.assertEqual(code, 2)
        self.assertIn("unknown option: --name", err)


class TestCompletionCommand(MainRunnerMixin, unittest.TestCase):
    def test_bash_completion(self):
        code, out, err = self.run_main(["completion", "bash"])
        self.assertEqual(code, 0)
        self.assertIn("atri-x", out)
        self.assertEqual(err, "")

    def test_unknown_shell_exits_2(self):
        code, _out, err = self.run_main(["completion", "csh"])
        self.assertEqual(code, 2)
        self.assertIn("usage: atri-x completion", err)


# ---------------------------------------------------------------------------
# config check / doctor
# ---------------------------------------------------------------------------


class TestConfigCheck(MainTestBase):
    def test_prints_summary_verbatim(self):
        code, out, err = self.run_main(["config", "check", "--config", "/cfg/app.toml"])
        self.assertEqual(code, 0)
        self.assertEqual(out, self.fake.check_summary)
        self.assertEqual(err, "")
        self.assertEqual(self.fake.check_calls, ["/cfg/app.toml"])

    def test_failure_exits_1(self):
        self.fake.check_error = AtriXError("bad config")
        code, _out, err = self.run_main(["config", "check"])
        self.assertEqual(code, 1)
        self.assertIn("atri-x: bad config", err)


class TestDoctor(MainTestBase):
    def test_text_report(self):
        self.fake.report = FakeDoctorReport(
            config_ok=True,
            platform_ok=True,
            e2e_ok=False,
            checks=[
                FakeDoctorCheck("config", "load", "passed", "config OK"),
                FakeDoctorCheck("port", "health_listen", "failed", "", "boom"),
            ],
        )
        code, out, _err = self.run_main(["doctor"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        # Go: fmt.Sprintf("[%s] %-16s %-7s %s", category, name, status, detail)
        self.assertEqual(
            lines[0], "[config] " + "load".ljust(16) + " " + "passed".ljust(7) + " config OK"
        )
        self.assertEqual(
            lines[1],
            "[port] " + "health_listen".ljust(16) + " " + "failed".ljust(7) + "  error=boom",
        )
        self.assertEqual(lines[2], "config_ok=true platform_ok=true e2e_ok=false")

    def test_failed_check_exits_1(self):
        self.fake.report = FakeDoctorReport(config_ok=False)
        code, out, _err = self.run_main(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("config_ok=false", out)

    def test_e2e_flag_makes_e2e_failure_fatal(self):
        self.fake.report = FakeDoctorReport(e2e_ok=False)
        self.assertEqual(self.run_main(["doctor"])[0], 0)
        self.assertEqual(self.run_main(["doctor", "--e2e"])[0], 1)

    def test_doctor_options_are_forwarded(self):
        self.run_main(
            [
                "doctor",
                "--e2e",
                "--json",
                "--no-model",
                "--require-platform",
                "--config",
                "/cfg/app.toml",
            ]
        )
        _ctx, opts = self.fake.doctor_calls[-1]
        self.assertEqual(opts.config_path, "/cfg/app.toml")
        self.assertTrue(opts.e2e and opts.json and opts.skip_model and opts.require_platform)
        self.assertIsNone(_ctx)

    def test_json_report_keys(self):
        self.fake.report = FakeDoctorReport(
            config_ok=False,
            platform_ok=True,
            e2e_ok=False,
            checks=[
                FakeDoctorCheck("config", "load", "failed", "detail", "err"),
                FakeDoctorCheck("config", "check", "passed", "ok", ""),
            ],
            started_at=datetime(2026, 10, 13, 12, 0, 0, 500000, tzinfo=timezone.utc),
            finished_at=None,
        )
        code, out, _err = self.run_main(["doctor", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        # 键名与顺序逐字对应 Go 的 struct tag；`config_ok=false` 不能因为
        # omitempty 被丢掉（Go 的 doctor 结构体这些字段都没带 omitempty）。
        self.assertEqual(
            list(payload),
            [
                "config_ok",
                "platform_ok",
                "e2e_ok",
                "checks",
                "started_at",
                "finished_at",
            ],
        )
        self.assertFalse(payload["config_ok"])
        self.assertFalse(payload["e2e_ok"])
        self.assertEqual(
            list(payload["checks"][0]), ["category", "name", "status", "detail", "error"]
        )
        self.assertEqual(list(payload["checks"][1]), ["category", "name", "status", "detail"])
        self.assertEqual(payload["started_at"], "2026-10-13T12:00:00.5Z")
        self.assertEqual(payload["finished_at"], entry.ZERO_TIME_TEXT)
        self.assertTrue(out.endswith("\n"))

    def test_doctor_failure_exits_1(self):
        self.fake.doctor_error = AtriXError("nope")
        code, _out, err = self.run_main(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("atri-x doctor: nope", err)


# ---------------------------------------------------------------------------
# 应用路径
# ---------------------------------------------------------------------------


class TestRunApplication(MainTestBase):
    def test_auto_prefers_remote_cli_client(self):
        code, _out, err = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(self.fake.try_calls), 1)
        self.assertEqual(self.fake.run_calls, [])
        _ctx, opts = self.fake.try_calls[0]
        self.assertEqual(opts.config_path, "")
        self.assertEqual(opts.client_name, "")

    def test_auto_falls_back_on_err_cli_client_fallback(self):
        self.fake.try_error = ErrCLIClientFallback
        code, _out, err = self.run_main(["--client", "local"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.run_calls), 1)
        _ctx, opts = self.fake.run_calls[0]
        self.assertEqual(opts.mode, "auto")
        self.assertEqual(err, "")

    def test_auto_reports_other_errors(self):
        self.fake.try_error = AtriXError("client boom")
        code, _out, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("atri-x: client boom", err)
        self.assertEqual(self.fake.run_calls, [])

    def test_auto_cancelled_returns_zero(self):
        self.fake.try_error = asyncio.CancelledError()
        code, _out, err = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(self.fake.run_calls, [])

    def test_cli_mode_runs_remote_client_only(self):
        code, _out, err = self.run_main(["cli", "-c", "local"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(self.fake.cli_calls), 1)
        self.assertEqual(self.fake.run_calls, [])
        _ctx, opts = self.fake.cli_calls[0]
        self.assertEqual(opts.client_name, "local")

    def test_cli_mode_error_exits_1(self):
        self.fake.cli_error = AtriXError("cli boom")
        code, _out, err = self.run_main(["cli"])
        self.assertEqual(code, 1)
        self.assertIn("atri-x: cli boom", err)

    def test_run_mode_forwards_options(self):
        code, _out, err = self.run_main(["run", "--config", "/cfg/app.toml"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(self.fake.try_calls, [])
        self.assertEqual(len(self.fake.run_calls), 1)
        ctx, opts = self.fake.run_calls[0]
        self.assertEqual(opts.mode, "full")
        self.assertEqual(opts.config_path, "/cfg/app.toml")
        self.assertEqual(opts.version, entry.version)
        # 时间必须带时区（PORTING §1.7）。
        self.assertIsNotNone(opts.started_at.tzinfo)
        # ctx 必须同时支持 is_set() 与 wait()，供 app 层的取消判定使用。
        self.assertTrue(callable(ctx.is_set))
        self.assertTrue(callable(ctx.wait))
        self.assertFalse(ctx.is_set())

    def test_service_mode_forwards_mode(self):
        self.run_main(["service", "run"])
        _ctx, opts = self.fake.run_calls[0]
        self.assertEqual(opts.mode, "service")

    def test_run_failure_exits_1(self):
        self.fake.run_error = AtriXError("run boom")
        code, _out, err = self.run_main(["run"])
        self.assertEqual(code, 1)
        self.assertIn("atri-x: run boom", err)

    def test_run_cancelled_returns_zero(self):
        self.fake.run_error = asyncio.CancelledError()
        code, _out, err = self.run_main(["run"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_run_keyboard_interrupt_returns_zero(self):
        """Go 侧 Ctrl+C = ctx 取消 = 直接 return（退出码 0）。"""
        self.fake.run_error = KeyboardInterrupt()
        code, _out, err = self.run_main(["run"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_empty_message_error_falls_back_to_type_name(self):
        self.fake.run_error = AtriXError("")
        code, _out, err = self.run_main(["run"])
        self.assertEqual(code, 1)
        self.assertIn("AtriXError", err)

    def test_signals_are_restored_after_run(self):
        import signal

        before = signal.getsignal(signal.SIGINT)
        self.run_main(["run"])
        self.assertIs(signal.getsignal(signal.SIGINT), before)


class TestMainEntryPoint(unittest.TestCase):
    def test_main_reads_sys_argv(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["atri_x", "--version"]):
            with contextlib.redirect_stdout(out):
                self.assertEqual(entry.main(), 0)
        self.assertEqual(out.getvalue(), f"atri-x {entry.version}\n")


# ---------------------------------------------------------------------------
# Python 版新增：--health-probe
# ---------------------------------------------------------------------------


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口名
        if self.path in ("/live", "/ready"):
            body = b'{"status":"ok","path":"' + self.path.encode() + b'"}'
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # pragma: no cover - 静音
        pass


class TestHealthProbeUrl(unittest.TestCase):
    def test_default_address(self):
        self.assertEqual(
            entry.health_probe_url(""), "http://127.0.0.1:32171/live"
        )

    def test_full_url_without_path_gets_live(self):
        self.assertEqual(
            entry.health_probe_url("http://127.0.0.1:9"), "http://127.0.0.1:9/live"
        )

    def test_full_url_is_used_verbatim(self):
        url = "http://127.0.0.1:9/ready?x=1"
        self.assertEqual(entry.health_probe_url(url), url)

    def test_bare_path_uses_default_address(self):
        self.assertEqual(entry.health_probe_url("/ready"), "http://127.0.0.1:32171/ready")

    def test_host_port(self):
        self.assertEqual(entry.health_probe_url("10.0.0.1:1234"), "http://10.0.0.1:1234/live")

    def test_host_only_uses_default_port(self):
        self.assertEqual(entry.health_probe_url("example.test"), "http://example.test:32171/live")

    def test_wildcard_hosts_map_to_loopback(self):
        for spec in ("0.0.0.0:32171", ":32171", "0.0.0.0"):
            with self.subTest(spec=spec):
                self.assertEqual(entry.health_probe_url(spec), "http://127.0.0.1:32171/live")

    def test_ipv6(self):
        self.assertEqual(entry.health_probe_url("[::1]:32171"), "http://[::1]:32171/live")
        self.assertEqual(entry.health_probe_url("[::]:32171"), "http://127.0.0.1:32171/live")

    def test_invalid_address_raises(self):
        with self.assertRaises(ValueError):
            entry.health_probe_url("host:notaport")
        with self.assertRaises(ValueError):
            entry.health_probe_url("[::1")


class TestHealthProbeCommand(unittest.TestCase):
    """真起一个回环 HTTP 服务器，验证退出码与输出。"""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def run_probe(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_healthy_returns_zero(self):
        code, out, err = self.run_probe(
            [f"--health-probe=http://127.0.0.1:{self.port}/live"]
        )
        self.assertEqual(code, 0)
        self.assertIn('"status":"ok"', out)
        self.assertEqual(err, "")

    def test_bare_host_port_gets_live_path(self):
        code, out, _err = self.run_probe([f"--health-probe=127.0.0.1:{self.port}"])
        self.assertEqual(code, 0)
        self.assertIn("/live", out)

    def test_path_only_uses_env_address(self):
        with mock.patch.dict(os.environ, {"ATRIX_HEALTH_ADDR": f"127.0.0.1:{self.port}"}):
            code, out, _err = self.run_probe(["--health-probe=/ready"])
        self.assertEqual(code, 0)
        self.assertIn("/ready", out)

    def test_non_2xx_returns_one(self):
        code, out, err = self.run_probe(
            [f"--health-probe=http://127.0.0.1:{self.port}/nope"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("HTTP 404", err)

    def test_unreachable_returns_one(self):
        # 端口 1 上不会有监听者（连不上 → 不健康）。
        code, out, err = self.run_probe(["--health-probe=127.0.0.1:1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("health probe", err)

    def test_invalid_address_returns_one(self):
        code, _out, err = self.run_probe(["--health-probe=host:notaport"])
        self.assertEqual(code, 1)
        self.assertIn("health probe address", err)

    def test_does_not_touch_config_or_app(self):
        """HEALTHCHECK 场景没有配置文件；``atri_x.app`` 也不该被导入。"""
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"atri_x.app": None}):
            with contextlib.redirect_stdout(out):
                code = entry.main(
                    [f"--health-probe=http://127.0.0.1:{self.port}/live"]
                )
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# 编码助手
# ---------------------------------------------------------------------------


class TestDoctorJsonHelpers(unittest.TestCase):
    def test_rfc3339_nano_matches_go(self):
        plus8 = timezone(timedelta(hours=8))
        cases = [
            (datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc), "2026-10-13T12:00:00Z"),
            (
                datetime(2026, 10, 13, 12, 0, 0, 100000, tzinfo=timezone.utc),
                "2026-10-13T12:00:00.1Z",
            ),
            (
                datetime(2026, 10, 13, 20, 0, 0, 123456, tzinfo=timezone.utc),
                "2026-10-13T20:00:00.123456Z",
            ),
            # 非 UTC 输入先归一化（Go 的 json 编码走 MarshalJSON → RFC3339 带原偏移；
            # 本仓契约统一 UTC，见 PORTING §1.7）。
            (datetime(2026, 10, 13, 20, 0, 0, tzinfo=plus8), "2026-10-13T12:00:00Z"),
            (None, entry.ZERO_TIME_TEXT),
            (datetime(1, 1, 1, tzinfo=timezone.utc), entry.ZERO_TIME_TEXT),
        ]
        for value, want in cases:
            with self.subTest(value=value):
                self.assertEqual(entry.rfc3339_nano(value), want)

    def test_json_escapes_html_like_go(self):
        report = FakeDoctorReport(
            checks=[FakeDoctorCheck("config", "load", "failed", "<a & b>", "")]
        )
        text = entry.doctor_report_json(report)
        self.assertIn("\\u003ca \\u0026 b\\u003e", text)
        self.assertNotIn("<a", text)

    def test_real_doctor_types_are_accepted(self):
        """真的 ``atri_x.app.DoctorReport`` / ``DoctorCheck`` 也能编码。"""
        from atri_x.app import DoctorCheck, DoctorReport

        report = DoctorReport(
            config_ok=True,
            platform_ok=False,
            e2e_ok=True,
            checks=[DoctorCheck("platform", "connection", "failed", "d", "e")],
            started_at=datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 10, 13, 12, 0, 1, tzinfo=timezone.utc),
        )
        text = entry.doctor_report_json(report)
        self.assertIn('"platform_ok": false', text)
        self.assertIn('"e2e_ok": true', text)
        self.assertIn('"finished_at": "2026-10-13T12:00:01Z"', text)


class TestOutputEncoding(unittest.TestCase):
    def test_unencodable_text_falls_back_to_ascii(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", newline="")
        entry._write(stream, "emoji \u20ac ok\n")
        stream.flush()
        stream.buffer.seek(0)
        self.assertIn(b"\\u20ac", stream.buffer.read())

    def test_plain_text_passes_through(self):
        buffer = io.StringIO()
        entry._write(buffer, "hello\n")
        self.assertEqual(buffer.getvalue(), "hello\n")


if __name__ == "__main__":
    unittest.main()
