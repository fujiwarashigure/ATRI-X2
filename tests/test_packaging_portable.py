"""``packaging/build_portable.py`` 的单元测试：便携版 zip 的内容契约。

不跑 PyInstaller、不跑 exe（那是 ``build_portable.py --verify`` 的活），只验证
「打包出来的 zip 里有什么」：顶层目录、启动器、说明文件、构建元数据、SHA256 旁注，
以及不该进包的东西（IME 日志、安装版标记）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))

import build_portable  # noqa: E402 - 需要先修 sys.path

WORK_ROOT = REPO_ROOT / "tests" / "_tmp_portable_build"
VERSION = "9.9.9"
FOLDER = "ATRI-X-9.9.9"


class PortableBuildTest(unittest.TestCase):
    """用假发行目录验证 zip 结构；速度在 1 秒内。"""

    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(WORK_ROOT, ignore_errors=True)
        cls.dist = WORK_ROOT / "dist" / "atri-x"
        internal = cls.dist / "_internal"
        internal.mkdir(parents=True)
        (cls.dist / "atri-x.exe").write_bytes(b"MZ-fake-console")
        (cls.dist / "atri-x-gui.exe").write_bytes(b"MZ-fake-gui")
        (internal / "data.bin").write_bytes(b"x" * 4096)
        (internal / "nested").mkdir()
        (internal / "nested" / "money.md").write_text("ok", encoding="utf-8")
        # 不该进便携包的顶层垃圾：IME 日志 / 安装版标记
        (cls.dist / "debug.log").write_text("ime log", encoding="utf-8")
        (cls.dist / "install.json").write_text("{}", encoding="utf-8")
        cls.summary = build_portable.build(
            dist=cls.dist,
            output=WORK_ROOT / "out",
            version=VERSION,
        )
        cls.zip_path = Path(cls.summary["zip"])
        with zipfile.ZipFile(cls.zip_path) as archive:
            cls.names = archive.namelist()
            cls.payload = {
                name: archive.read(name)
                for name in (
                    f"{FOLDER}/{build_portable.README_NAME}",
                    f"{FOLDER}/{build_portable.INFO_NAME}",
                    f"{FOLDER}/{build_portable.LAUNCHER_PORTABLE}",
                    f"{FOLDER}/{build_portable.LAUNCHER_NORMAL}",
                    f"{FOLDER}/{build_portable.LAUNCHER_DEBUG}",
                )
            }

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(WORK_ROOT, ignore_errors=True)

    def test_zip_name_and_folder(self) -> None:
        self.assertEqual(self.zip_path.name, f"ATRI-X-{VERSION}-portable-win64.zip")
        tops = {name.split("/", 1)[0] for name in self.names if name}
        self.assertEqual(tops, {FOLDER})

    def test_carries_onedir_payload(self) -> None:
        for expected in (
            f"{FOLDER}/atri-x.exe",
            f"{FOLDER}/atri-x-gui.exe",
            f"{FOLDER}/_internal/data.bin",
            f"{FOLDER}/_internal/nested/money.md",
        ):
            self.assertIn(expected, self.names)

    def test_excludes_junk(self) -> None:
        self.assertNotIn(f"{FOLDER}/debug.log", self.names)
        self.assertNotIn(f"{FOLDER}/install.json", self.names)

    def test_launchers_present_and_portable(self) -> None:
        portable = self.payload[f"{FOLDER}/{build_portable.LAUNCHER_PORTABLE}"].decode("ascii")
        self.assertIn("data\\appdata", portable)
        self.assertIn("atri-x-gui.exe", portable)
        self.assertIn('set "APPDATA=', portable)
        self.assertIn("exit /b %ERRORLEVEL%", portable)
        # 批处理必须 CRLF，否则部分旧版 cmd 解析异常
        self.assertIn("\r\n", portable)
        self.assertNotIn(b"\xef\xbb\xbf", self.payload[f"{FOLDER}/{build_portable.LAUNCHER_PORTABLE}"])

        normal = self.payload[f"{FOLDER}/{build_portable.LAUNCHER_NORMAL}"].decode("ascii")
        self.assertIn("atri-x-gui.exe", normal)
        self.assertNotIn("set \"APPDATA=", normal)

        debug = self.payload[f"{FOLDER}/{build_portable.LAUNCHER_DEBUG}"].decode("ascii")
        self.assertIn("atri-x.exe", debug)
        self.assertIn("doctor --no-model", debug)
        self.assertIn("cmd /k", debug)

    def test_readme_is_notepad_friendly(self) -> None:
        raw = self.payload[f"{FOLDER}/{build_portable.README_NAME}"]
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # UTF-8 BOM：记事本按 UTF-8 打开
        text = raw.decode("utf-8-sig")
        self.assertIn("便携版", text)
        self.assertIn("start-portable.bat", text)
        self.assertIn(VERSION, text)
        self.assertIn("\r\n", text)

    def test_build_info_metadata(self) -> None:
        info = json.loads(self.payload[f"{FOLDER}/{build_portable.INFO_NAME}"].decode("utf-8"))
        self.assertEqual(info["app"], "ATRI-X")
        self.assertEqual(info["kind"], "portable")
        self.assertEqual(info["version"], VERSION)
        self.assertEqual(info["folder"], FOLDER)
        self.assertEqual(info["files"], 4)  # 两个 exe + data.bin + money.md
        self.assertGreater(info["bytes"], 0)
        self.assertIsNone(info["kernel"])
        self.assertIn("atri-x.exe", info["exes"])
        self.assertIn("atri-x-gui.exe", info["exes"])
        self.assertEqual(info["dataDirs"]["portable"], "data\\appdata\\ATRI-X")
        self.assertNotIn("sha256", info)  # 不能自我引用
        parsed = json.loads(json.dumps(info, ensure_ascii=False))
        self.assertEqual(parsed["version"], VERSION)

    def test_zip_bytes_not_in_info(self) -> None:
        """build-info.json 写在 zip 里时 zip 体积未知，不能写死。"""
        self.assertNotIn("zipBytes", json.loads(self.payload[f"{FOLDER}/{build_portable.INFO_NAME}"]))
        self.assertGreater(self.summary["zipBytes"], 0)
        self.assertEqual(self.summary["zipBytes"], self.zip_path.stat().st_size)

    def test_sha256_sidecar_matches_zip(self) -> None:
        sidecar = Path(self.summary["sidecar"])
        self.assertTrue(sidecar.is_file())
        digest, name = sidecar.read_text(encoding="ascii").split()
        self.assertEqual(name, self.zip_path.name)
        actual = hashlib.sha256(self.zip_path.read_bytes()).hexdigest()
        self.assertEqual(digest, actual)
        self.assertEqual(digest, self.summary["sha256"])
        self.assertEqual(len(self.summary["sha256"]), 64)

    def test_missing_exe_is_rejected(self) -> None:
        broken = WORK_ROOT / "dist" / "broken"
        broken.mkdir(parents=True)
        (broken / "README.txt").write_text("no exe", encoding="utf-8")
        with self.assertRaises(build_portable.PortableError):
            build_portable.dist_tree(broken)

    def test_missing_dist_is_rejected(self) -> None:
        with self.assertRaises(build_portable.PortableError):
            build_portable.dist_tree(WORK_ROOT / "dist" / "nope")

    def test_cli_builds_and_prints(self) -> None:
        """CLI 路径（main）也要能跑：``argv=None`` 读 sys.argv 是常见回归点。"""
        out = WORK_ROOT / "cli-out"
        code = build_portable.main(
            ["--dist", str(self.dist), "--output", str(out), "--version", VERSION, "--json"]
        )
        self.assertEqual(code, 0)
        self.assertTrue((out / f"ATRI-X-{VERSION}-portable-win64.zip").is_file())
        self.assertTrue((out / f"ATRI-X-{VERSION}-portable-win64.zip.sha256").is_file())

    def test_readme_mentions_kernel_state(self) -> None:
        """没内核时要提示 ATRIX_DSH_KERNEL；有内核时写版本号。"""
        base = {
            "version": VERSION,
            "folder": FOLDER,
            "files": 1,
            "bytes": 1024,
        }
        no_kernel = build_portable.readme_text(dict(base, kernel=None))
        self.assertIn("ATRIX_DSH_KERNEL", no_kernel)
        with_kernel = build_portable.readme_text(
            dict(base, kernel={"dshVersion": "0.2.0-rc.2", "nodeVersion": "v24.19.0", "packages": 384})
        )
        self.assertIn("0.2.0-rc.2", with_kernel)


if __name__ == "__main__":
    unittest.main()
