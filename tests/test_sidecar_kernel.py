"""内置运行时（``atri_x.sidecar``）的发现与解析用例。

不依赖真实的 DSH 内核：用临时目录伪造 ``runtime/<id>/kernel.json`` 与伪装的可执行文件，
覆盖「找到 / 找不到 / 文件缺失 / 清单非法 / 显式环境变量优先」这几种状态。
真机核验是 ``python -m atri_x.sidecar verify --acp``（见 ``docs/windows-app.md``）。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from atri_x.sidecar import kernel as kernel_module
from atri_x.sidecar import registry

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``，并进 .gitignore）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_sidecar"


def write_kernel(directory: Path, *, manifest: dict[str, Any] | None = None, node: bool = True,
                 entry: bool = True) -> Path:
    """在 ``directory`` 造一份内核布局：清单 + node.exe + 入口 js。"""
    directory.mkdir(parents=True, exist_ok=True)
    payload = manifest if manifest is not None else {
        "id": "dsh-kernel",
        "kind": "dsh-kernel",
        "format": 1,
        "profile": "acp",
        "dshVersion": "0.2.0-rc.2",
        "nodeVersion": "v24.19.0",
        "node": "node/node.exe",
        "entry": "node_modules/@deepseek-ai/dsh/lib/bin.js",
        "verified": True,
    }
    (directory / "kernel.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    if node:
        node_path = directory / "node" / "node.exe"
        node_path.parent.mkdir(parents=True, exist_ok=True)
        node_path.write_bytes(b"fake-node")
    if entry:
        entry_path = directory / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        entry_path.write_text("// fake\n", encoding="utf-8", newline="\n")
    return directory


class SidecarRegistryTestCase(unittest.TestCase):
    """清单解析与目录发现。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_load_manifest_rejects_unknown_format_and_missing_id(self) -> None:
        directory = TEMP_ROOT / "weird"
        write_kernel(directory, manifest={"id": "x", "format": 99, "node": "n", "entry": "e"})
        self.assertIsNone(registry.load_manifest(str(directory)))

        directory2 = TEMP_ROOT / "noid"
        write_kernel(directory2, manifest={"format": 1, "node": "n", "entry": "e"})
        self.assertIsNone(registry.load_manifest(str(directory2)))

    def test_load_manifest_accepts_valid_kernel(self) -> None:
        directory = write_kernel(TEMP_ROOT / "good")
        manifest = registry.load_manifest(str(directory))
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["id"], "dsh-kernel")

    def test_discover_and_find_use_runtime_dir_env(self) -> None:
        runtime_root = TEMP_ROOT / "runtime"
        write_kernel(runtime_root / "dsh-kernel")
        (runtime_root / "not-a-runtime").mkdir()
        with mock.patch.dict(os.environ, {registry.RUNTIME_DIR_ENV: str(runtime_root)}, clear=False):
            found = registry.discover()
            self.assertEqual([item.id for item in found], ["dsh-kernel"])
            runtime = registry.find("dsh-kernel")
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.version, "0.2.0-rc.2")
            self.assertTrue(runtime.path("node").endswith("node.exe"))
            self.assertIsNone(registry.find("no-such-runtime"))

    def test_runtime_dirs_skips_missing_paths(self) -> None:
        missing = TEMP_ROOT / "does-not-exist"
        with mock.patch.dict(os.environ, {registry.RUNTIME_DIR_ENV: str(missing)}, clear=False):
            directories = registry.runtime_dirs()
        self.assertNotIn(str(missing), directories)


class DshKernelTestCase(unittest.TestCase):
    """内核命令组装与缺失检测。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_kernel_from_dir_builds_acp_command(self) -> None:
        directory = write_kernel(TEMP_ROOT / "kernel")
        kernel = kernel_module.kernel_from_dir(str(directory))
        self.assertIsNotNone(kernel)
        assert kernel is not None
        self.assertEqual(kernel.missing(), [])
        command = kernel.command()
        self.assertTrue(command[0].endswith("node.exe"))
        self.assertEqual(command[2:], ("--profile", "acp"))
        self.assertTrue(command[1].endswith("bin.js"))

    def test_missing_files_are_reported_and_command_refused(self) -> None:
        directory = write_kernel(TEMP_ROOT / "kernel", node=False, entry=False)
        with mock.patch.dict(os.environ, {kernel_module.DSH_KERNEL_ENV: str(directory)}, clear=False):
            kernel = kernel_module.find_kernel()
            self.assertIsNotNone(kernel)
            assert kernel is not None
            self.assertEqual(len(kernel.missing()), 2)
            self.assertIsNone(kernel_module.dsh_command())

    def test_env_var_prefers_explicit_kernel_dir(self) -> None:
        directory = write_kernel(TEMP_ROOT / "explicit")
        with mock.patch.dict(os.environ, {kernel_module.DSH_KERNEL_ENV: str(directory)}, clear=False):
            kernel = kernel_module.find_kernel()
            self.assertIsNotNone(kernel)
            assert kernel is not None
            self.assertEqual(os.path.normcase(kernel.root), os.path.normcase(str(directory)))
            self.assertEqual(kernel_module.dsh_command("acp"), kernel.command("acp"))

    def test_describe_reports_not_found_when_nothing_is_installed(self) -> None:
        empty = TEMP_ROOT / "empty-bundle"
        empty.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {kernel_module.DSH_KERNEL_ENV: ""}, clear=False), \
                mock.patch.object(registry, "bundle_root", return_value=str(empty)), \
                mock.patch.object(registry, "config_runtime_dir", return_value=""), \
                mock.patch.object(registry, "RUNTIME_DIR_ENV", "ATRIX_RUNTIME_DIR_TEST_NONE"):
            info = kernel_module.describe()
        self.assertFalse(info["found"])
        self.assertEqual(info["id"], "dsh-kernel")

    def test_describe_reports_found_kernel(self) -> None:
        directory = write_kernel(TEMP_ROOT / "describe")
        with mock.patch.dict(os.environ, {kernel_module.DSH_KERNEL_ENV: str(directory)}, clear=False):
            info = kernel_module.describe()
        self.assertTrue(info["found"])
        self.assertEqual(info["dshVersion"], "0.2.0-rc.2")
        self.assertEqual(info["missing"], [])
        self.assertEqual(info["command"][2:], ["--profile", "acp"])


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
