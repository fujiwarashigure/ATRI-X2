"""用户数据根解析（``atri_x.config.home``）的用例。

覆盖三条规则与它们的优先级：``ATRIX_HOME`` 环境变量 → 冻结包旁边的
``install.json`` 的 ``dataDir`` → 平台默认；以及坏标记/相对路径的容错。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from atri_x.config import loader
from atri_x.config.home import (
    ENV_HOME,
    describe_home,
    home_source,
    installer_data_dir,
    user_data_dir,
    user_home_dir,
)

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_config_home"


def _is_windows() -> bool:
    return os.name == "nt"


class HomeResolveTest(unittest.TestCase):
    """``user_home_dir`` / ``user_data_dir`` 的解析顺序。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.root = TEMP_ROOT
        self.appdata = self.root / "appdata"
        self.appdata.mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ,
            {"APPDATA": str(self.appdata), "AppData": str(self.appdata)},
            clear=False,
        )
        self._env.start()
        os.environ.pop(ENV_HOME, None)

    def tearDown(self) -> None:
        self._env.stop()
        os.environ.pop(ENV_HOME, None)
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_default_home_follows_appdata(self) -> None:
        if not _is_windows():
            self.skipTest("平台默认目录的断言只针对 Windows")
        self.assertEqual(user_home_dir(), str(self.appdata / "ATRI-X"))
        self.assertEqual(user_data_dir(), str(self.appdata / "ATRI-X" / "data"))
        self.assertEqual(home_source(), "default")

    def test_env_override_wins_and_data_dir_is_under_home(self) -> None:
        target = self.root / "portable-data"
        target.mkdir()
        with mock.patch.dict(os.environ, {ENV_HOME: str(target)}, clear=False):
            self.assertEqual(user_home_dir(), str(target))
            self.assertEqual(user_data_dir(), str(target / "data"))
            self.assertEqual(home_source(), "env")

    def test_relative_env_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {ENV_HOME: "relative\\data"}, clear=False):
            if _is_windows():
                self.assertEqual(user_home_dir(), str(self.appdata / "ATRI-X"))
                self.assertEqual(home_source(), "default")

    def test_installer_marker_is_used_when_frozen(self) -> None:
        app_dir = self.root / "installed-app"
        app_dir.mkdir()
        target = self.root / "installer-data"
        target.mkdir()
        (app_dir / "install.json").write_text(
            json.dumps({"installer": "inno", "dataDir": str(target)}), encoding="utf-8"
        )
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(app_dir / "atri-x.exe")
        ):
            self.assertEqual(installer_data_dir(), str(target))
            self.assertEqual(user_home_dir(), str(target))
            self.assertEqual(user_data_dir(), str(target / "data"))
            self.assertEqual(home_source(), "installer")

    def test_env_beats_installer_marker(self) -> None:
        app_dir = self.root / "installed-app"
        app_dir.mkdir()
        marker_target = self.root / "marker-data"
        marker_target.mkdir()
        env_target = self.root / "env-data"
        env_target.mkdir()
        (app_dir / "install.json").write_text(
            json.dumps({"dataDir": str(marker_target)}), encoding="utf-8"
        )
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(app_dir / "atri-x.exe")
        ), mock.patch.dict(os.environ, {ENV_HOME: str(env_target)}, clear=False):
            self.assertEqual(user_home_dir(), str(env_target))
            self.assertEqual(home_source(), "env")

    def test_broken_or_relative_marker_is_ignored(self) -> None:
        app_dir = self.root / "installed-app"
        app_dir.mkdir()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(app_dir / "atri-x.exe")
        ):
            # 标记文件不存在
            self.assertEqual(installer_data_dir(), "")
            # 坏 JSON
            (app_dir / "install.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(installer_data_dir(), "")
            # 相对路径 dataDir
            (app_dir / "install.json").write_text(
                json.dumps({"dataDir": "data"}), encoding="utf-8"
            )
            self.assertEqual(installer_data_dir(), "")
            # 不是字符串
            (app_dir / "install.json").write_text(
                json.dumps({"dataDir": 42}), encoding="utf-8"
            )
            self.assertEqual(installer_data_dir(), "")

    def test_source_mode_never_reads_marker(self) -> None:
        """源码运行（未冻结）时 ``sys.executable`` 是解释器，旁边即便有标记也不读。"""
        interpreter_dir = self.root / "python"
        interpreter_dir.mkdir()
        target = self.root / "marker-data"
        target.mkdir()
        (interpreter_dir / "install.json").write_text(
            json.dumps({"dataDir": str(target)}), encoding="utf-8"
        )
        with mock.patch.object(sys, "frozen", False, create=True), mock.patch.object(
            sys, "executable", str(interpreter_dir / "python.exe")
        ):
            self.assertEqual(installer_data_dir(), "")
            if _is_windows():
                self.assertEqual(home_source(), "default")

    def test_describe_home_reports_paths_and_source(self) -> None:
        if not _is_windows():
            self.skipTest("平台默认目录的断言只针对 Windows")
        info = describe_home()
        self.assertEqual(info["homeDir"], str(self.appdata / "ATRI-X"))
        self.assertEqual(info["dataDir"], str(self.appdata / "ATRI-X" / "data"))
        self.assertEqual(info["source"], "default")
        self.assertEqual(info["envVariable"], ENV_HOME)
        self.assertEqual(info["installerDataDir"], "")


class LoaderIntegrationTest(unittest.TestCase):
    """``loader.platform_default_*`` 必须跟着数据根走。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.root = TEMP_ROOT
        self.appdata = self.root / "appdata"
        self.appdata.mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ,
            {"APPDATA": str(self.appdata), "AppData": str(self.appdata)},
            clear=False,
        )
        self._env.start()
        os.environ.pop(ENV_HOME, None)

    def tearDown(self) -> None:
        self._env.stop()
        os.environ.pop(ENV_HOME, None)
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_default_paths_without_override(self) -> None:
        if not _is_windows():
            self.skipTest("平台默认目录的断言只针对 Windows")
        self.assertEqual(
            loader.platform_default_config_path(), str(self.appdata / "ATRI-X" / "app.toml")
        )
        self.assertEqual(
            loader.platform_default_data_dir(), str(self.appdata / "ATRI-X" / "data")
        )

    def test_paths_follow_env_override(self) -> None:
        target = self.root / "chosen-data"
        target.mkdir()
        with mock.patch.dict(os.environ, {ENV_HOME: str(target)}, clear=False):
            self.assertEqual(
                loader.platform_default_config_path(), str(target / "app.toml")
            )
            self.assertEqual(loader.platform_default_data_dir(), str(target / "data"))
        # 环境变量撤销后回到默认
        self.assertEqual(home_source(), "default" if _is_windows() else home_source())


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
