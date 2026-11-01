"""安装器命令行（``atri_x.dist.cli``）的用例。

只跑 ``status`` / ``install --dry-run`` / ``migrate`` 这类不碰真实注册表与桌面的路径，
并通过重定向 stdout 校验 ``--json`` 输出；临时目录在 ``tests/_tmp_dist_cli``。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from atri_x.dist import cli
from atri_x.dist.migrate import tree_stats

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_dist_cli"


def _run(argv: list[str]) -> tuple[int, dict]:
    """跑一次 CLI，返回 ``(退出码, 解析出的 JSON（可能为空）)``。"""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    payload: dict = {}
    for line in reversed(out.getvalue().strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            payload = value
            break
    return code, payload


class InstallerCliTest(unittest.TestCase):
    """CLI 参数解析与输出。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.root = TEMP_ROOT
        self.local_appdata = self.root / "localappdata"
        self.appdata = self.root / "appdata"
        self.home = self.root / "home"
        for directory in (self.local_appdata, self.appdata, self.home):
            directory.mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.local_appdata),
                "APPDATA": str(self.appdata),
                "AppData": str(self.appdata),
                "USERPROFILE": str(self.home),
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("ATRIX_HOME", None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_status_json_includes_data_dir_and_runtime(self) -> None:
        code, payload = _run(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("dataDir", payload)
        self.assertIn("runtime", payload)
        self.assertIn(payload["runtime"]["source"], ("default", "env", "installer"))

    def test_install_dry_run_reports_data_dir(self) -> None:
        target = self.root / "app"
        code, payload = _run(["install", "--target", str(target), "--dry-run", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["dataDir"], str(target / "data"))
        self.assertFalse(target.exists())

    def test_migrate_subcommand_copies_data(self) -> None:
        source = self.root / "old"
        source.mkdir()
        (source / "app.toml").write_text("x", encoding="utf-8")
        target = self.root / "new"
        code, payload = _run(
            ["migrate", "--from", str(source), "--to", str(target), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["files"], 1)
        self.assertTrue((target / "app.toml").is_file())
        self.assertTrue(source.is_dir())

    def test_migrate_subcommand_moves_data(self) -> None:
        source = self.root / "old"
        source.mkdir()
        (source / "data").mkdir()
        (source / "data" / "db").write_bytes(b"x")
        target = self.root / "new"
        code, payload = _run(
            ["migrate", "--from", str(source), "--to", str(target), "--move", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(source.exists())
        self.assertEqual(tree_stats(str(target))[0], 1)

    def test_migrate_error_returns_non_zero(self) -> None:
        code, _payload = _run(
            ["migrate", "--from", str(self.root / "missing"), "--to", str(self.root / "t"), "--json"]
        )
        self.assertEqual(code, 1)

    def test_finalize_writes_install_marker_and_data_flag(self) -> None:
        target = self.root / "app"
        target.mkdir()
        data_dir = self.root / "app-data"
        data_dir.mkdir()
        code, payload = _run(
            [
                "finalize",
                "--target",
                str(target),
                "--data-dir",
                str(data_dir),
                "--migrated",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["dataDir"], str(data_dir))
        self.assertTrue(payload["migrated"])
        marker = json.loads(
            (target / "install.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["installer"], "inno")
        self.assertEqual(marker["dataDir"], str(data_dir))
        self.assertTrue(marker["dataMigrated"])
        self.assertEqual(marker["target"], str(target))
        self.assertEqual(
            (target / "data-dir.txt").read_text(encoding="utf-8").strip(),
            str(data_dir),
        )

    def test_finalize_rejects_relative_data_dir(self) -> None:
        code, _payload = _run(
            ["finalize", "--target", str(self.root / "app"), "--data-dir", "relative"]
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
