"""诊断辅助：10 秒后把所有线程栈 dump 出来并退出。

用于定位挂起的测试（正常测试不会用到）。用法::

    python scripts/dump_hang.py <unittest 目标>

例如::

    python scripts/dump_hang.py tests.test_signal.TestQueue.test_drain_failure_is_diagnosed
"""

from __future__ import annotations

import faulthandler
import sys
import unittest
from pathlib import Path

# 直接跑 scripts/dump_hang.py 时 sys.path[0] 是 scripts/，需要补上仓库根，
# 否则 `tests.test_xxx` 这种全限定目标 import 不了。
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["tests.test_signal"]
    faulthandler.dump_traceback_later(10.0, exit=True)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for target in targets:
        suite.addTests(loader.loadTestsFromName(target))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
