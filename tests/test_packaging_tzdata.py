"""打包层时区数据断言（1.0.10 硬依赖）。

这些是**文件级**检查：不要求装 PyInstaller、不要求有构建产物，CI 与开发机都能跑。
真正的冻结产物验证由 ``packaging/build.py`` 的 ``verify_tzdata_payload`` +
``-m atri_x.app.time_check`` 完成（构建时自动执行）。
"""

from __future__ import annotations

from pathlib import Path
import unittest

from atri_x.app.time_check import check_time_data

_REPO = Path(__file__).resolve().parent.parent
_SPEC = _REPO / "packaging" / "atri-x.spec"
_DOCKERFILE = _REPO / "deploy" / "Dockerfile"
_BUILD = _REPO / "packaging" / "build.py"


class TimeCheckProbeTest(unittest.TestCase):
    def test_probe_reports_ok(self) -> None:
        report = check_time_data()
        self.assertTrue(report.get("ok"), report)
        self.assertGreater(int(report.get("zones", 0)), 0)
        self.assertEqual(report.get("timezone"), "Asia/Shanghai")
        self.assertTrue(report.get("dst_probe", {}).get("ok"), report.get("dst_probe"))

    def test_probe_reports_unknown_timezone(self) -> None:
        report = check_time_data("Mars/Olympus")
        self.assertFalse(report.get("ok"))
        self.assertIn("error", report)


class PackagingDeclarationTest(unittest.TestCase):
    def test_spec_collects_tzdata_explicitly(self) -> None:
        text = _SPEC.read_text(encoding="utf-8")
        self.assertIn('collect_data_files("tzdata")', text)
        self.assertIn("tzdata/zoneinfo/Asia/Shanghai", text)
        self.assertIn("SystemExit", text)  # 缺数据要让构建失败，而不是静默出包

    def test_build_verifies_tzdata_payload(self) -> None:
        text = _BUILD.read_text(encoding="utf-8")
        self.assertIn("verify_tzdata_payload", text)
        self.assertIn("atri_x.app.time_check", text)

    def test_docker_installs_tzdata_and_probes(self) -> None:
        text = _DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("tzdata", text)
        self.assertIn("atri_x.app.time_check", text)

    def test_pyproject_declares_tzdata_dependency(self) -> None:
        text = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("tzdata", text)
        self.assertIn("dependencies = [", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
