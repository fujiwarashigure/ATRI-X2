"""用户数据迁移（``atri_x.dist.migrate``）的用例。

全部落在仓库内的临时目录（``tests/_tmp_dist_migrate``），不碰真实用户数据。
覆盖：复制/移动/覆盖、参数校验（相对路径、源目标相同或互相包含）、dry-run、
逐文件核对失败时不删源。
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from atri_x.dist.migrate import MigrationError, migrate, tree_stats

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_dist_migrate"


class MigrateTest(unittest.TestCase):
    """复制与移动。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.root = TEMP_ROOT
        self.root.mkdir(parents=True)
        self.source = self.root / "old-data"
        self.source.mkdir()
        (self.source / "app.toml").write_text("toml\n", encoding="utf-8")
        (self.source / "data").mkdir()
        (self.source / "data" / "sessions.db").write_bytes(b"\x00" * 128)
        (self.source / "gui").mkdir()
        (self.source / "gui" / "members.json").write_text("[]", encoding="utf-8")
        self.target = self.root / "new-data"

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_copy_keeps_source_and_reports_stats(self) -> None:
        report = migrate(str(self.source), str(self.target))
        self.assertTrue(report.ok)
        self.assertEqual(report.mode, "copy")
        self.assertEqual(report.files, 3)
        self.assertGreater(report.bytes, 0)
        self.assertTrue(self.source.is_dir())
        self.assertEqual(
            (self.target / "app.toml").read_text(encoding="utf-8"), "toml\n"
        )
        self.assertEqual((self.target / "data" / "sessions.db").read_bytes(), b"\x00" * 128)
        self.assertEqual(tree_stats(str(self.target))[0], 3)

    def test_move_deletes_source_after_verification(self) -> None:
        report = migrate(str(self.source), str(self.target), move=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.mode, "move")
        self.assertFalse(self.source.exists())
        self.assertTrue((self.target / "gui" / "members.json").is_file())
        self.assertEqual(report.warnings, [])

    def test_non_empty_target_requires_overwrite(self) -> None:
        self.target.mkdir()
        (self.target / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(MigrationError):
            migrate(str(self.source), str(self.target))
        report = migrate(str(self.source), str(self.target), overwrite=True)
        self.assertTrue(report.ok)
        self.assertTrue((self.target / "keep.txt").is_file())
        self.assertTrue((self.target / "app.toml").is_file())

    def test_empty_source_is_noop(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        report = migrate(str(empty), str(self.target))
        self.assertTrue(report.ok)
        self.assertEqual(report.files, 0)
        self.assertIn("无需迁移", report.detail)
        self.assertFalse(self.target.exists())

    def test_dry_run_does_not_touch_disk(self) -> None:
        report = migrate(str(self.source), str(self.target), dry_run=True)
        self.assertTrue(report.ok)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.files, 3)
        self.assertFalse(self.target.exists())

    def test_missing_source_raises(self) -> None:
        with self.assertRaises(MigrationError):
            migrate(str(self.root / "nope"), str(self.target))

    def test_relative_paths_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            migrate("old-data", str(self.target))
        with self.assertRaises(MigrationError):
            migrate(str(self.source), "new-data")

    def test_same_source_and_target_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            migrate(str(self.source), str(self.source))

    def test_overlapping_paths_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            migrate(str(self.source), str(self.source / "nested"))
        with self.assertRaises(MigrationError):
            migrate(str(self.source), str(self.root))

    def test_copy_failure_keeps_source(self) -> None:
        """目标不可建（拿一个已存在的文件当目录）时不删源、不吞异常。"""
        blocker = self.root / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(MigrationError):
            migrate(str(self.source), str(blocker / "data"), move=True)
        self.assertTrue((self.source / "app.toml").is_file())


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
