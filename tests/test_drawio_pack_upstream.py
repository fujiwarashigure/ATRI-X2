"""上游 drawio-skill 3.4.2 回归测试的一致性门禁（随包快照不许漂移）。

为什么把上游 247 个测试原样留在仓库里：``drawio_pack/skills/drawio-skill`` 的
``scripts/`` 是**上游源码快照**（只打了两处补丁：``diagramctl.py`` 里冻结形态的
子进程启动方式、测试的 UTF-8 解码）。随包脚本一旦被"顺手改坏"，ATRI-X 侧的单测
往往仍然绿（我们自己只覆盖少量动作），只有上游回归能发现。这条门禁把上游测试
当黑盒跑：``drawio_pack`` 目录保持上游相对布局（``skills/drawio-skill/...``），
所以测试文件不用改路径。

产物目录与 ``__pycache__`` 都在子进程里禁用/隔离，不会往仓库里写东西。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PACK = _REPO / "drawio_pack"
_EXPECTED = 247


class DrawioUpstreamConformanceTest(unittest.TestCase):
    """跑上游完整回归（约 20 秒）。"""

    def test_upstream_suite_is_green(self) -> None:
        if not (_PACK / "tests" / "test_scripts.py").is_file():
            raise unittest.SkipTest("drawio_pack/tests 不完整，跳过上游一致性测试")
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(_PACK),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
        output = proc.stdout.decode("utf-8", errors="replace")
        tail = output.strip().splitlines()[-8:]
        self.assertEqual(
            proc.returncode,
            0,
            "上游 drawio-skill 回归失败（随包脚本被改坏？）:\n" + "\n".join(tail),
        )
        self.assertIn("Ran %d tests" % _EXPECTED, output, "\n".join(tail))
        self.assertIn("OK", output, "\n".join(tail))


if __name__ == "__main__":
    unittest.main()
