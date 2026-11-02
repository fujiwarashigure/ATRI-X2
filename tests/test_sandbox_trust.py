"""进程启动点信任分级清单的门禁测试（修改清单 §13）。

规则：``atri_x`` 里出现 ``create_subprocess_exec`` / ``subprocess.run`` 等进程启动
写法的模块，必须在 :data:`atri_x.sandbox.trust.INVENTORY` 里登记信任类别；漏登记的
模块会让本测试失败，避免「新加了一条执行链但没人做信任分类」。
"""

from __future__ import annotations

import os
import unittest

from atri_x.sandbox import trust

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 允许不登记的模块：沙箱自身的抽象层（由 manager/backend 统一管控）。
_ALLOWED_UNLISTED = {
    "atri_x.sandbox.backend",
    "atri_x.sandbox.manager",
}


class TestTrustInventory(unittest.TestCase):
    def test_all_process_sites_are_classified(self) -> None:
        sites = trust.scan_process_sites(REPO_ROOT)
        self.assertTrue(sites, "没有扫描到任何进程启动点，扫描逻辑可能失效")
        registered = {entry.module for entry in trust.INVENTORY}
        unclassified = sorted(
            module
            for module in sites
            if module not in registered and module not in _ALLOWED_UNLISTED
        )
        self.assertEqual(
            unclassified,
            [],
            "以下模块有进程启动点但没有登记信任类别：\n" + "\n".join(unclassified),
        )

    def test_entries_point_at_real_modules(self) -> None:
        for entry in trust.INVENTORY:
            with self.subTest(module=entry.module):
                self.assertTrue(
                    os.path.isfile(
                        os.path.join(REPO_ROOT, *entry.module.split(".")) + ".py"
                    ),
                    entry.module,
                )
                self.assertIn(entry.trust, trust.CLASSES)

    def test_summary_counts_every_entry(self) -> None:
        summary = trust.summary()
        self.assertEqual(sum(summary.values()), len(trust.INVENTORY))
        self.assertGreater(summary["sandboxed"], 0)
        self.assertGreater(summary["control_plane"], 0)


if __name__ == "__main__":
    unittest.main()
