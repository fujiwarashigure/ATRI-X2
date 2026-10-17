"""``deploy/pack/prepare_offline.py`` 打包清单的自检。

回归背景：``PACK_FILES`` 曾把外部 watchdog 的源文件名写成连字符形式
``atri-x-watchdog.py``，而仓库里实际的脚本是 ``atri_x_watchdog.py``；于是
``python deploy/pack/prepare_offline.py --skip-images`` 在组装源码离线包时
直接报「缺少部署文件」。本用例钉住「清单里的每个源文件都真实存在」与
「生成的包内 README 使用仓库里的真实文件名」，避免打包路径再次静默失效
（该路径此前没有其它用例覆盖）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
PACK = DEPLOY / "pack"
for candidate in (str(REPO), str(DEPLOY), str(PACK)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import prepare_offline  # noqa: E402


class TestOfflinePackManifest(unittest.TestCase):
    """离线包清单与仓库实际文件的一致性。"""

    def test_pack_file_sources_exist(self) -> None:
        missing = [
            source
            for source, _ in prepare_offline.PACK_FILES
            if not (DEPLOY / source).is_file()
        ]
        self.assertEqual(missing, [], f"PACK_FILES 引用了不存在的源文件：{missing}")

    def test_source_entries_exist(self) -> None:
        missing = [
            entry
            for entry in prepare_offline.SOURCE_ENTRIES
            if not (REPO / entry).exists()
        ]
        self.assertEqual(missing, [], f"SOURCE_ENTRIES 引用了不存在的条目：{missing}")

    def test_watchdog_uses_repository_filename(self) -> None:
        """watchdog 在包内必须保持与 systemd unit 相同的文件名。"""
        packed = dict(prepare_offline.PACK_FILES)
        self.assertIn("atri_x_watchdog.py", packed)
        self.assertEqual(packed["atri_x_watchdog.py"], "atri_x_watchdog.py")
        self.assertTrue((DEPLOY / "atri_x_watchdog.py").is_file())

    def test_generated_readme_uses_real_watchdog_name(self) -> None:
        readme = prepare_offline.offline_readme("0.9.0", "amd64", False)
        self.assertIn("`atri_x_watchdog.py`", readme)
        self.assertNotIn("atri-x-watchdog.py", readme)


if __name__ == "__main__":
    unittest.main()
