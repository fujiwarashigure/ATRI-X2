"""安装器用例：复制/快捷方式/卸载项/自启，全部用假实现 + 临时目录。

真的安装会写注册表、桌面和 ``%LOCALAPPDATA%``；这里通过**注入**的
``shortcut_writer`` / ``registry`` 与临时 ``LOCALAPPDATA`` 隔离，验证的是安装器的
**决策与文件效果**，不是 Windows 的那些副作用本身。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import importlib

#: 直接取子模块：包 ``__init__`` 导出的 ``install_app`` 是函数，不能当模块用。
installer = importlib.import_module("atri_x.dist.install")

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_dist_install"


class FakeRegistry:
    """记录调用的注册表后端。"""

    def __init__(self) -> None:
        self.uninstall_payloads: list[dict[str, Any]] = []
        self.deleted = 0
        self.path_added: list[str] = []
        self.path_removed: list[str] = []

    def write_uninstall(self, payload: dict[str, Any]) -> None:
        self.uninstall_payloads.append(payload)

    def delete_uninstall(self) -> None:
        self.deleted += 1

    def add_to_user_path(self, directory: str) -> None:
        self.path_added.append(directory)

    def remove_from_user_path(self, directory: str) -> None:
        self.path_removed.append(directory)


class FakeShortcuts:
    """记录快捷方式创建/删除，并真的落一个空文件（便于断言存在性）。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.removed: list[str] = []

    def write(self, path: str, target: str, *, arguments: str = "", working_dir: str = "",
              description: str = "", **_: Any) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(f"{target} {arguments}".strip(), encoding="utf-8")
        self.created.append((path, target, arguments))
        return path

    def remove(self, path: str) -> None:
        self.removed.append(path)
        if os.path.isfile(path):
            os.remove(path)


class InstallerTestCase(unittest.TestCase):
    """安装/卸载/状态。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.root = TEMP_ROOT
        self.local_appdata = self.root / "localappdata"
        self.appdata = self.root / "appdata"
        self.home = self.root / "home"
        for directory in (self.local_appdata, self.appdata, self.home):
            directory.mkdir(parents=True)
        self.shortcuts = FakeShortcuts()
        self.registry = FakeRegistry()
        self.start_menu = self.root / "start-menu"
        self.desktop = self.root / "desktop"
        self._env = mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.local_appdata),
                "APPDATA": str(self.appdata),
                "USERPROFILE": str(self.home),
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def do_install(self, **kwargs: Any):
        """带假后端跑一次安装。"""
        options = {
            "desktop": True,
            "shortcut_writer": self.shortcuts.write,
            "registry": self.registry,
            "start_menu": str(self.start_menu),
            "desktop_folder": str(self.desktop),
        }
        options.update(kwargs)
        return installer.install(**options)

    def test_source_mode_install_writes_launcher_and_marker(self) -> None:
        report = self.do_install()
        self.assertTrue(report.ok)
        target = Path(report.target)
        self.assertTrue((target / "atri-x.cmd").is_file())
        marker = json.loads((target / installer.INSTALL_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["app"], installer.APP_NAME)
        self.assertFalse(marker["frozen"])
        self.assertEqual(len(report.shortcuts), 2)
        self.assertEqual(len(self.shortcuts.created), 2)
        self.assertEqual(len(self.registry.uninstall_payloads), 1)
        self.assertTrue(report.registry)

    def test_frozen_install_copies_bundle_and_uses_gui_exe(self) -> None:
        source = self.root / "bundle"
        (source / "_internal").mkdir(parents=True)
        (source / "atri-x.exe").write_bytes(b"console")
        (source / "atri-x-gui.exe").write_bytes(b"gui")
        (source / "_internal" / "data.bin").write_bytes(b"data")
        with mock.patch.object(installer, "is_frozen", return_value=True), \
                mock.patch.object(installer, "source_root", return_value=str(source)):
            report = self.do_install()
        self.assertTrue(report.ok)
        self.assertTrue(report.frozen)
        target = Path(report.target)
        self.assertTrue((target / "atri-x.exe").is_file())
        self.assertTrue((target / "atri-x-gui.exe").is_file())
        self.assertTrue((target / "_internal" / "data.bin").is_file())
        self.assertTrue(self.shortcuts.created[0][1].endswith("atri-x-gui.exe"))
        marker = json.loads((target / installer.INSTALL_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["launchExe"], str(target / "atri-x-gui.exe"))

    def test_existing_target_requires_force(self) -> None:
        target = self.local_appdata / installer.APP_NAME / "app"
        target.mkdir(parents=True)
        (target / "old.txt").write_text("old", encoding="utf-8")
        source = self.root / "bundle"
        source.mkdir()
        (source / "atri-x.exe").write_bytes(b"x")
        with mock.patch.object(installer, "is_frozen", return_value=True), \
                mock.patch.object(installer, "source_root", return_value=str(source)):
            with self.assertRaises(installer.InstallError):
                self.do_install()
            report = self.do_install(force=True)
        self.assertTrue(report.ok)

    def test_dry_run_does_not_touch_disk(self) -> None:
        report = self.do_install(dry_run=True)
        self.assertTrue(report.ok)
        self.assertFalse(Path(report.target).exists())
        self.assertEqual(self.shortcuts.created, [])
        self.assertEqual(self.registry.uninstall_payloads, [])

    def test_uninstall_removes_app_but_keeps_user_data(self) -> None:
        report = self.do_install()
        target = Path(report.target)
        data_dir = self.appdata / installer.APP_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "app.toml").write_text("x", encoding="utf-8")

        result = installer.uninstall(
            yes=True,
            shortcut_remover=self.shortcuts.remove,
            registry=self.registry,
        )
        self.assertTrue(result.ok)
        self.assertFalse(target.exists())
        self.assertTrue(data_dir.is_dir())
        self.assertTrue(result.kept_data)
        self.assertEqual(self.registry.deleted, 1)
        self.assertEqual(len(self.shortcuts.removed), 2)

    def test_uninstall_purge_data_removes_user_data(self) -> None:
        self.do_install()
        data_dir = self.appdata / installer.APP_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        result = installer.uninstall(
            yes=True,
            purge_data=True,
            shortcut_remover=self.shortcuts.remove,
            registry=self.registry,
        )
        self.assertTrue(result.ok)
        self.assertFalse(data_dir.exists())
        self.assertFalse(result.kept_data)

    def test_uninstall_without_yes_only_returns_plan(self) -> None:
        report = self.do_install()
        target = Path(report.target)
        plan = installer.uninstall(shortcut_remover=self.shortcuts.remove, registry=self.registry)
        self.assertTrue(plan.ok)
        self.assertTrue(target.exists())
        self.assertIn("未执行", plan.detail)

    def test_status_reports_install(self) -> None:
        self.do_install()
        info = installer.status()
        self.assertTrue(info["installed"])
        self.assertTrue(info["targetExists"])
        self.assertEqual(len(info["shortcuts"]), 2)
        self.assertTrue(all(item["exists"] for item in info["shortcuts"]))

    def test_status_reports_installer_kind(self) -> None:
        self.do_install()
        self.assertEqual(installer.status()["installer"], "portable")

    def test_uninstall_refuses_inno_managed_install(self) -> None:
        target = self.local_appdata / installer.APP_NAME / "app"
        target.mkdir(parents=True)
        marker = {
            "installer": "inno",
            "app": installer.APP_NAME,
            "target": str(target),
            "uninstaller": str(target / "unins000.exe"),
            "shortcuts": [],
        }
        (target / installer.INSTALL_MARKER).write_text(
            json.dumps(marker), encoding="utf-8"
        )
        self.assertEqual(installer.status()["installer"], "inno")
        refused = installer.uninstall(
            yes=True, shortcut_remover=self.shortcuts.remove, registry=self.registry
        )
        self.assertFalse(refused.ok)
        self.assertIn("unins000.exe", refused.detail)
        self.assertTrue(target.exists())
        self.assertEqual(self.registry.deleted, 0)
        forced = installer.uninstall(
            yes=True,
            force=True,
            shortcut_remover=self.shortcuts.remove,
            registry=self.registry,
        )
        self.assertTrue(forced.ok)
        self.assertFalse(target.exists())

    def test_status_finds_marker_in_current_exe_dir(self) -> None:
        """Inno 装到自定义目录 / 解压后直接跑 status：标记就在 exe 旁边。"""
        bundle = self.root / "portable"
        bundle.mkdir(parents=True)
        marker = {
            "installer": "inno",
            "app": installer.APP_NAME,
            "target": str(bundle),
            "uninstaller": str(bundle / "unins000.exe"),
            "shortcuts": [],
        }
        (bundle / installer.INSTALL_MARKER).write_text(
            json.dumps(marker), encoding="utf-8"
        )
        with mock.patch.object(installer, "is_frozen", return_value=True), \
                mock.patch("atri_x.dist.marker.source_root", return_value=str(bundle)):
            info = installer.status()
        self.assertTrue(info["installed"])
        self.assertEqual(info["installer"], "inno")
        self.assertEqual(info["target"], str(bundle))

    def test_uninstall_payload_targets_console_exe(self) -> None:
        payload = installer._uninstall_payload(r"C:\ATRI-X\app", frozen=True)
        self.assertIn("atri-x.exe", payload["uninstall"])
        self.assertIn("-m atri_x.dist.install uninstall --yes", payload["uninstall"])
        self.assertEqual(payload["display_name"], installer.APP_NAME)

    def test_remove_install_dir_reports_clean_removal(self) -> None:
        target = self.root / "cleanup"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "file.txt").write_text("x", encoding="utf-8")
        leftover = installer._remove_install_dir(str(target))
        self.assertEqual(leftover, "")
        self.assertFalse(target.exists())

    def test_install_autostart_flag_is_forwarded(self) -> None:
        with mock.patch.object(installer, "_enable_autostart", return_value="") as enable:
            report = self.do_install(autostart=True)
        self.assertTrue(report.autostart)
        enable.assert_called_once()
        marker = json.loads(
            (Path(report.target) / installer.INSTALL_MARKER).read_text(encoding="utf-8")
        )
        self.assertTrue(marker["autostart"])


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
