"""``packaging/qqproto.py`` 的用例：脱敏搬运、启动脚本、负载核验。

这里用**假的 NapCat Shell 结构**（几个字节的桩文件）跑全流程，证明：

- 只复制运行必需的部分，``resources\\app\\napcat`` 下的 ``config`` / ``cache`` / ``logs``
  与任意 ``*.log`` 都不会进发行包（那里面有本机令牌与登录痕迹）；
- 生成的 ``Start-NapCat.bat`` / ``Stop-NapCat.bat`` / 使用说明里没有盘符绝对路径；
- 负载里少了必需文件、混进敏感目录、或文本里出现个人目录，核验必须失败。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))

import qqproto  # noqa: E402 - 需要先修 sys.path

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_packaging_qqproto"


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _make_shell(root: Path) -> Path:
    """造一个最小的 NapCat Shell（含应该被脱敏掉的文件）。"""
    shell = root / "source" / "NapCat.52230.Shell"
    app = shell / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
    _write(shell / "NapCatWinBootMain.exe", "stub")
    _write(shell / "QQ.exe", "stub")
    _write(shell / "msvcp140.dll", "stub")
    _write(shell / "beacon_report.log", "should be skipped")
    _write(
        app / "napcat.mjs",
        'const sQ = {}, Vu = typeof sQ < "u" && "4.18.38" || "1.0.0-dev";\n',
    )
    _write(app / "node_modules" / "ws" / "index.js", "module.exports = {};\n")
    _write(app / "native" / "major.node", "stub")
    _write(app / "config" / "onebot11.json", '{"network":{"websocketServers":[]}}')
    _write(app / "config" / "webui.json", '{"token":"local-secret"}')
    _write(app / "cache" / "cache.bin", "x")
    _write(app / "logs" / "napcat.log", "login trace")
    return shell


class StageCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.shell = _make_shell(TEMP_ROOT)
        self.dest = TEMP_ROOT / "dist" / "qqproto"
        self._sink = io.StringIO()
        self._quiet = contextlib.redirect_stdout(self._sink)
        self._quiet.__enter__()

    def tearDown(self) -> None:
        self._quiet.__exit__(None, None, None)
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_stage_copies_runtime_and_sanitizes(self) -> None:
        summary = qqproto.stage(self.shell, self.dest)
        self.assertTrue(summary["sanitized"])
        self.assertEqual(summary["shell"], "NapCat.52230.Shell")
        self.assertEqual(summary["qqVersion"], "9.9.33-52230")
        self.assertEqual(summary["napcatVersion"], "4.18.38")
        target = self.dest / "NapCat.52230.Shell"
        self.assertTrue((target / "NapCatWinBootMain.exe").is_file())
        self.assertTrue((target / "QQ.exe").is_file())
        app = target / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
        self.assertTrue((app / "napcat.mjs").is_file())
        self.assertTrue((app / "node_modules" / "ws" / "index.js").is_file())
        for name in ("config", "cache", "logs"):
            self.assertFalse((app / name).exists(), f"{name} 不该被复制")
        self.assertFalse(list(self.dest.rglob("*.log")))

    def test_stage_writes_launchers_and_readme(self) -> None:
        qqproto.stage(self.shell, self.dest)
        start = (self.dest / "Start-NapCat.bat").read_text(encoding="utf-8")
        stop = (self.dest / "Stop-NapCat.bat").read_text(encoding="utf-8")
        self.assertIn("NapCat.52230.Shell", start)
        self.assertIn("NapCatWinBootMain.exe", start)
        self.assertIn("taskkill", stop)
        for text in (start, stop):
            self.assertNotRegex(text, qqproto._ABSOLUTE_PATH_RE)
        readme = (self.dest / "NapCat-使用说明.txt").read_bytes()
        self.assertTrue(readme.startswith(b"\xef\xbb\xbf"), "说明文件应有 UTF-8 BOM")
        self.assertIn(b"\r\n", readme)

    def test_proto_info_has_no_absolute_path(self) -> None:
        qqproto.stage(self.shell, self.dest)
        info = json.loads((self.dest / "proto-info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["kind"], "qqproto")
        self.assertTrue(info["sanitized"])
        raw = (self.dest / "proto-info.json").read_text(encoding="utf-8")
        self.assertIn("NapCat.52230.Shell", raw)
        self.assertNotRegex(raw, qqproto._ABSOLUTE_PATH_RE)

    def test_stage_replaces_stale_dest(self) -> None:
        _write(self.dest / "stale.txt", "old")
        qqproto.stage(self.shell, self.dest)
        self.assertFalse((self.dest / "stale.txt").exists())

    def test_accepts_parent_dir_or_shell_dir(self) -> None:
        self.assertEqual(qqproto.find_shell(self.shell), self.shell.resolve())
        self.assertEqual(qqproto.find_shell(self.shell.parent), self.shell.resolve())
        with self.assertRaises(qqproto.QqProtoError):
            qqproto.find_shell(TEMP_ROOT / "nope")

    def test_verify_payload_ok(self) -> None:
        qqproto.stage(self.shell, self.dest)
        summary = qqproto.verify_payload(self.dest)
        self.assertTrue(summary["sanitized"])
        self.assertGreater(summary["files"], 5)

    def test_verify_rejects_leaked_config(self) -> None:
        qqproto.stage(self.shell, self.dest)
        target = self.dest / "NapCat.52230.Shell"
        app = target / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
        _write(app / "config" / "onebot11.json", '{"token":"leak"}')
        with self.assertRaises(qqproto.QqProtoError) as ctx:
            qqproto.verify_payload(self.dest)
        self.assertIn("脱敏", str(ctx.exception))

    def test_verify_rejects_log_file(self) -> None:
        qqproto.stage(self.shell, self.dest)
        _write(self.dest / "NapCat.52230.Shell" / "debug.log", "x")
        with self.assertRaises(qqproto.QqProtoError):
            qqproto.verify_payload(self.dest)

    def test_verify_rejects_missing_shell_exe(self) -> None:
        qqproto.stage(self.shell, self.dest)
        (self.dest / "NapCat.52230.Shell" / "NapCatWinBootMain.exe").unlink()
        with self.assertRaises(qqproto.QqProtoError):
            qqproto.verify_payload(self.dest)

    def test_verify_rejects_absolute_path_in_readme(self) -> None:
        qqproto.stage(self.shell, self.dest)
        readme = self.dest / "NapCat-使用说明.txt"
        text = readme.read_text(encoding="utf-8-sig")
        payload = "路径：C:\\Users\\someone\\ATRI-X\r\n"  # secrets-scan: allow
        readme.write_bytes(b"\xef\xbb\xbf" + (text + payload).encode())
        with self.assertRaises(qqproto.QqProtoError):
            qqproto.verify_payload(self.dest)

    def test_cli_stage_check_json(self) -> None:
        buffer: list[str] = []

        def fake_log(message: str) -> None:
            buffer.append(message)

        original = qqproto._log
        qqproto._log = fake_log  # type: ignore[assignment]
        try:
            code = qqproto.main(["--source", str(self.shell), "--dest", str(self.dest), "--json"])
            self.assertEqual(code, 0, buffer)
            payload = json.loads(buffer[-1])
            self.assertTrue(payload["sanitized"])
            code = qqproto.main(["--check", str(self.dest), "--json"])
            self.assertEqual(code, 0, buffer)
        finally:
            qqproto._log = original  # type: ignore[assignment]

    def test_cli_bad_source(self) -> None:
        buffer: list[str] = []
        original = qqproto._log
        qqproto._log = buffer.append  # type: ignore[assignment]
        try:
            code = qqproto.main(["--source", str(TEMP_ROOT / "nope"), "--dest", str(self.dest)])
        finally:
            qqproto._log = original  # type: ignore[assignment]
        self.assertEqual(code, 1)
        self.assertTrue(any("FAILED" in line for line in buffer))


class VersionCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.shell = _make_shell(TEMP_ROOT)

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_versions(self) -> None:
        self.assertEqual(qqproto.find_qq_version(self.shell), "9.9.33-52230")
        app = self.shell / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
        self.assertEqual(qqproto.find_napcat_version(app), "4.18.38")

    def test_napcat_version_missing(self) -> None:
        app = TEMP_ROOT / "empty"
        (app).mkdir(parents=True, exist_ok=True)
        _write(app / "napcat.mjs", "console.log('no version');\n")
        self.assertEqual(qqproto.find_napcat_version(app), "")


class SanitizeGateCase(unittest.TestCase):
    """复制后的硬校验：残留 NapCat 运行期文件必须让构建**失败**。

    背景：``ignore`` 只覆盖 ``versions/<版本>/resources/app/napcat`` 这一处。万一以后
    NapCat 换布局，或者有人先在产物目录里跑了一遍程序（会把 config/onebot11.json 写回
    程序目录），复制看起来「成功」，但来源机的 OneBot / WebUI 令牌会跟着进安装包。
    """

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.dest = TEMP_ROOT / "dist" / "qqproto"

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_clean_tree_has_no_offenders(self) -> None:
        _write(self.dest / "NapCat.52230.Shell" / "NapCatWinBootMain.exe", "stub")
        _write(
            self.dest
            / "NapCat.52230.Shell"
            / "versions"
            / "9.9.33-52230"
            / "resources"
            / "app"
            / "napcat"
            / "napcat.mjs",
            "console.log(1);\n",
        )
        self.assertEqual(qqproto.find_sanitize_offenders(self.dest), [])

    def test_config_files_are_offenders(self) -> None:
        app = self.dest / "NapCat.52230.Shell" / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
        _write(app / "config" / "onebot11.json", '{"token":"source-machine"}')
        _write(app / "config" / "webui.json", '{"token":"source-machine"}')
        _write(app / "logs" / "napcat.log", "trace")
        _write(self.dest / "stray.log", "log")
        offenders = qqproto.find_sanitize_offenders(self.dest)
        self.assertIn("NapCat.52230.Shell/versions/9.9.33-52230/resources/app/napcat/config/onebot11.json", offenders)
        self.assertIn("NapCat.52230.Shell/versions/9.9.33-52230/resources/app/napcat/config/webui.json", offenders)
        self.assertIn("NapCat.52230.Shell/versions/9.9.33-52230/resources/app/napcat/logs/napcat.log", offenders)
        self.assertIn("stray.log", offenders)

    def test_planted_config_makes_assert_sanitized_raise(self) -> None:
        """目录被运行期写过配置（模拟「先跑了一遍程序」）→ 打包前门禁必须报错。"""
        app = self.dest / "NapCat.52230.Shell" / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
        _write(app / "config" / "onebot11.json", '{"token":"leaked"}')
        with self.assertRaises(qqproto.QqProtoError) as ctx:
            qqproto.assert_sanitized(self.dest)
        self.assertIn("脱敏失败", str(ctx.exception))

    def test_build_installer_refuses_dirty_qqproto(self) -> None:
        """``build_installer`` 拿现成 dist 重打包时也要被拦住（这是最容易漏的路径）。"""
        sys.path.insert(0, str(REPO_ROOT / "packaging"))
        import build_installer  # noqa: PLC0415 - 需要先修 sys.path

        dist = TEMP_ROOT / "dist"
        _write(dist / "atri-x.exe", "stub")
        _write(dist / "atri-x-gui.exe", "stub")
        _write(dist / "qqproto" / "Start-NapCat.bat", "stub")
        _write(
            dist
            / "qqproto"
            / "NapCat.52230.Shell"
            / "versions"
            / "9.9.33-52230"
            / "resources"
            / "app"
            / "napcat"
            / "config"
            / "webui.json",
            '{"token":"leaked"}',
        )
        with self.assertRaises(build_installer.InstallerError) as ctx:
            build_installer._assert_qqproto_sanitized(dist)
        self.assertIn("脱敏失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
