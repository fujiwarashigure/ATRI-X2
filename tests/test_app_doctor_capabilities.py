"""``/doctor`` 的能力自检测试（Python 版新增）。

审计背景：doctor 原有检查只问配置/端口/平台/模型，从不问"核心装配器装上了吗、
脱敏接通了吗、入站限流配了吗、hook 控制面接线了吗"。这几类能力在真实启动里可以
静默消失（本轮静默降级审计所列的装配缺口），所以这里钉住三件事：

1. 有启动期快照时按快照断言（``passed`` / ``failed``），失败会把 ``config_ok``
   拉成 ``False``（``python -m atri_x doctor`` 的退出码依赖它）；
2. 没有快照时标 ``skipped``，**不伪造通过**；
3. :class:`DoctorService` 能把启动期快照透传进 ``DoctorOptions``。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from atri_x.app.doctor import (
    DoctorCheck,
    DoctorOptions,
    DoctorReport,
    DoctorService,
    _run_capability_checks,
    run_doctor,
)

#: 五项核心装配全部 installed 的快照（名字与 ``bootstrap._CORE_INSTALLERS`` 一致）。
CORE_INSTALLED = [
    ["models", "ModelService"],
    ["context_runtime", "ContextRuntimeService"],
    ["budget", "BudgetService"],
    ["ratelimit", "RateLimitService"],
    ["tool_directive", "ToolDirectiveService"],
]


def _snapshot(**overrides):
    payload = {
        "agent_services": {
            "installed": list(CORE_INSTALLED),
            "skipped": [],
            "failed": [],
            "warnings": [],
            "ok": True,
        },
        "rate_limit_configured": True,
        "hook_service": True,
        "redaction_available": True,
        "redaction": {"available": True, "mode": "strict"},
    }
    payload.update(overrides)
    return payload


def _collect(cfg=None, capabilities=None):
    """跑一遍能力检查，返回 ``(report, {name: check})``。"""
    report = DoctorReport()
    checks: list[DoctorCheck] = []

    def add(category, name, status, detail="", err=None):
        checks.append(
            DoctorCheck(
                category=category,
                name=name,
                status=status,
                detail=detail,
                error=str(err) if err is not None else "",
            )
        )

    _run_capability_checks(report, add, cfg, capabilities)
    return report, {check.name: check for check in checks}


class TestCapabilityChecksWithSnapshot(unittest.TestCase):
    """有启动期快照：按快照断言，四种能力各一项检查。"""

    def test_all_capabilities_pass(self):
        report, checks = _collect(capabilities=_snapshot())
        self.assertTrue(report.config_ok)
        for name in ("services", "hook_service"):
            self.assertEqual(checks[name].status, "passed", checks[name])
        self.assertEqual(checks["redaction"].status, "passed")
        self.assertEqual(checks["inbound_rate_limit"].status, "passed")
        self.assertEqual(
            {check.category for check in checks.values()},
            {"agent", "security", "ops", "media", "time"},
        )

    def test_missing_core_assembler_fails(self):
        installed = [pair for pair in CORE_INSTALLED if pair[0] != "ratelimit"]
        report, checks = _collect(
            capabilities=_snapshot(
                agent_services={
                    "installed": installed,
                    "skipped": [],
                    "failed": [],
                    "warnings": [],
                    "ok": True,
                }
            )
        )
        self.assertEqual(checks["services"].status, "failed")
        self.assertIn("ratelimit", checks["services"].detail)
        self.assertFalse(report.config_ok)

    def test_failed_assembler_reason_is_in_the_detail(self):
        _report, checks = _collect(
            capabilities=_snapshot(
                agent_services={
                    "installed": list(CORE_INSTALLED),
                    "skipped": [],
                    "failed": [["tool_directive", "RuntimeError: boom"]],
                    "warnings": ["tool_directive skipped: RuntimeError: boom"],
                    "ok": False,
                }
            )
        )
        self.assertEqual(checks["services"].status, "failed")
        self.assertIn("tool_directive", checks["services"].detail)
        self.assertIn("RuntimeError: boom", checks["services"].detail)

    def test_degraded_redaction_fails(self):
        report, checks = _collect(
            capabilities=_snapshot(redaction={"available": False, "mode": "fallback"})
        )
        self.assertEqual(checks["redaction"].status, "failed")
        self.assertIn("fallback", checks["redaction"].detail)
        self.assertFalse(report.config_ok)

    def test_missing_hook_service_fails(self):
        report, checks = _collect(capabilities=_snapshot(hook_service=False))
        self.assertEqual(checks["hook_service"].status, "failed")
        self.assertIn("hook service is not configured", checks["hook_service"].detail)
        self.assertFalse(report.config_ok)

    def test_unconfigured_rate_limit_is_not_a_failure(self):
        report, checks = _collect(
            capabilities=_snapshot(rate_limit_configured=False),
            cfg=SimpleNamespace(ops=SimpleNamespace(user_messages_per_minute=0)),
        )
        self.assertEqual(checks["inbound_rate_limit"].status, "skipped")
        self.assertIn("unlimited", checks["inbound_rate_limit"].detail)
        self.assertTrue(report.config_ok)


class TestCapabilityChecksWithoutSnapshot(unittest.TestCase):
    """没有 Agent（``python -m atri_x doctor``）：静态可判定 + 其余 skipped。"""

    def test_static_checks_are_skipped_not_faked(self):
        cfg = SimpleNamespace(
            ops=SimpleNamespace(user_messages_per_minute=0, group_messages_per_minute=0)
        )
        report, checks = _collect(cfg=cfg, capabilities=None)
        self.assertTrue(report.config_ok)
        self.assertEqual(checks["services"].status, "skipped")
        self.assertIn("no live assembly report", checks["services"].detail)
        self.assertEqual(checks["hook_service"].status, "skipped")
        self.assertEqual(checks["inbound_rate_limit"].status, "skipped")

    def test_rate_limit_configured_from_config_is_passed(self):
        cfg = SimpleNamespace(
            ops=SimpleNamespace(user_messages_per_minute=12, group_messages_per_minute=0)
        )
        _report, checks = _collect(cfg=cfg, capabilities=None)
        self.assertEqual(checks["inbound_rate_limit"].status, "passed")
        self.assertIn("user=12/min", checks["inbound_rate_limit"].detail)

    def test_unknown_rate_limit_when_config_is_not_loaded(self):
        """配置没加载成功时不能把"未知"写成"未配置"（更不能写成通过）。"""
        _report, checks = _collect(cfg=None, capabilities=None)
        self.assertEqual(checks["inbound_rate_limit"].status, "skipped")
        self.assertIn("config is not loaded", checks["inbound_rate_limit"].detail)

    def test_redaction_is_checked_without_an_agent(self):
        _report, checks = _collect(capabilities=None)
        self.assertIn(checks["redaction"].status, ("passed", "failed"))
        self.assertNotEqual(checks["redaction"].status, "skipped")


class TestImageBackendCheck(unittest.TestCase):
    """实际生效的图像后端要有明确出处（Pillow 加速 / 纯标准库都受支持）。

    本机（CI 与开发机）装没装 Pillow 不确定，所以断言"状态与 detail 自洽"，
    并用 mock 钉住两条固定分支，而不是断言本机恰好装了 Pillow。
    """

    def test_live_probe_is_reported_without_faking(self):
        _report, checks = _collect(capabilities=_snapshot())
        check = checks["image_backend"]
        self.assertEqual(check.category, "media")
        self.assertIn(check.status, ("passed", "skipped"))
        if check.status == "passed":
            self.assertIn("pillow", check.detail)
        else:
            self.assertIn("stdlib", check.detail)

    def test_pillow_backend_is_reported_with_version(self):
        with mock.patch(
            "atri_x.app.doctor.image_backend_info",
            return_value={"backend": "pillow", "version": "12.3.0"},
        ):
            report, checks = _collect(capabilities=None)
        self.assertEqual(checks["image_backend"].status, "passed")
        self.assertIn("pillow 12.3.0", checks["image_backend"].detail)
        self.assertTrue(report.config_ok)

    def test_stdlib_backend_is_not_a_failure(self):
        with mock.patch(
            "atri_x.app.doctor.image_backend_info",
            return_value={"backend": "stdlib", "version": ""},
        ):
            report, checks = _collect(capabilities=None)
        self.assertEqual(checks["image_backend"].status, "skipped")
        self.assertIn("Pillow not installed", checks["image_backend"].detail)
        self.assertTrue(report.config_ok)  # 纯标准库是受支持的默认形态

    def test_pillow_installed_but_not_in_effect_is_visible(self):
        """``ATRI_X_IMAGECODEC_BACKEND=pure`` 强制关掉加速时要能看出来。"""
        with mock.patch(
            "atri_x.app.doctor.image_backend_info",
            return_value={"backend": "stdlib", "version": "12.3.0"},
        ):
            _report, checks = _collect(capabilities=None)
        self.assertEqual(checks["image_backend"].status, "skipped")
        self.assertIn("12.3.0", checks["image_backend"].detail)
        self.assertIn("not in effect", checks["image_backend"].detail)


class TestDoctorServiceBinding(unittest.TestCase):
    """``DoctorService`` 把启动期快照透传进 ``DoctorOptions``。"""

    def test_capabilities_are_forwarded(self):
        service = DoctorService("config.toml")
        snapshot = _snapshot()
        service.bind_capabilities(snapshot)
        captured: dict[str, DoctorOptions] = {}

        async def fake_run(ctx, opts):
            captured["opts"] = opts
            return DoctorReport(
                checks=[DoctorCheck("agent", "services", "passed", "ok")]
            )

        with mock.patch("atri_x.app.doctor.run_doctor", fake_run):
            text = asyncio.run(service.doctor())
        self.assertEqual(captured["opts"].capabilities, snapshot)
        self.assertEqual(text, "Everything is OK")

    def test_bind_rejects_non_mapping(self):
        service = DoctorService("config.toml")
        service.bind_capabilities(["not-a-mapping"])  # type: ignore[arg-type]
        self.assertIsNone(service.capabilities)


class TestRunDoctorWiresCapabilityChecks(unittest.TestCase):
    """``run_doctor`` 把能力检查并进同一份报告（config_ok 也受影响）。"""

    def test_capability_checks_appear_in_the_report(self):
        cfg = SimpleNamespace(
            config_path="",
            mode_models={},
            providers={},
            ops=SimpleNamespace(user_messages_per_minute=0, group_messages_per_minute=0),
        )
        with (
            mock.patch("atri_x.app.doctor.load_config", return_value=cfg),
            mock.patch("atri_x.app.doctor.check_config", return_value="config OK"),
            mock.patch("atri_x.app.doctor.check_writable_dirs", return_value=None),
            mock.patch("atri_x.app.doctor.doctor_health_url", return_value=""),
            mock.patch(
                "atri_x.app.doctor.enabled_platform_names", return_value=[]
            ),
        ):
            report = asyncio.run(
                run_doctor(
                    None,
                    DoctorOptions(
                        skip_model=True, capabilities=_snapshot(redaction={"available": False})
                    ),
                )
            )
        checks = {check.name: check for check in report.checks}
        self.assertEqual(checks["services"].status, "passed")
        self.assertEqual(checks["redaction"].status, "failed")
        self.assertFalse(report.config_ok)

    def test_capability_checks_run_even_when_config_load_fails(self):
        """配置加载失败也要能报"脱敏/Hook 控制面"，它们不依赖配置。"""
        with mock.patch(
            "atri_x.app.doctor.load_config", side_effect=RuntimeError("no config")
        ):
            report = asyncio.run(
                run_doctor(
                    None,
                    DoctorOptions(
                        skip_model=True, capabilities=_snapshot(hook_service=False)
                    ),
                )
            )
        checks = {check.name: check for check in report.checks}
        self.assertEqual(checks["load"].status, "failed")
        self.assertEqual(checks["hook_service"].status, "failed")
        self.assertEqual(checks["redaction"].status, "passed")
        # 快照是权威的：配置没加载也照样按启动期事实判定限流。
        self.assertEqual(checks["inbound_rate_limit"].status, "passed")


if __name__ == "__main__":
    unittest.main()
