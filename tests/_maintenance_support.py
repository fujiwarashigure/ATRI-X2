"""``atri_x.maintenance`` 测试的共享夹具（非测试模块，不被 unittest 收集）。

从 ``tests/test_maintenance.py`` 拆出：仓库根下的 ``tmp_maintenance_test/``
临时目录助手（PORTING §7.2：不用 ``tempfile``）、Asia/Shanghai 时区常量，
以及同步 / 异步两个基类。
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone

TMP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_maintenance_test"
)
SHANGHAI = timezone(timedelta(hours=8))


class _TmpHelpers:
    """临时目录夹具（仓库根下的 ``tmp_maintenance_test/``，与 PORTING §7.2 一致）。"""

    def _init_tmp(self) -> None:
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.case_dir = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.case_dir, ignore_errors=True)
        os.makedirs(self.case_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.case_dir, True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.case_dir, *parts)

    def write_file(self, path: str, content: bytes, age_days: float = 0) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        if age_days:
            stamp = datetime.now(timezone.utc).timestamp() - age_days * 86400
            os.utime(path, (stamp, stamp))
        return path


class _CaseWithTmp(_TmpHelpers, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._init_tmp()


class _AsyncCaseWithTmp(_TmpHelpers, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._init_tmp()


__all__ = ["SHANGHAI", "TMP_ROOT", "_AsyncCaseWithTmp", "_CaseWithTmp", "_TmpHelpers"]
