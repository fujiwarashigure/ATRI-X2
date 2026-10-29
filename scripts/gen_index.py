#!/usr/bin/env python3
"""生成 ``devdocs/generated/`` 下的全部索引产物（或校验它们没漂移）。

这是把"文档索引"从**手写副本**变成**生成物**的入口。产物清单与渲染实现都在
:mod:`atri_x.devtools.render`，本脚本只做路径解析与退出码：

::

    python scripts/gen_index.py            # 重新生成全部产物
    python scripts/gen_index.py --check    # 只校验：磁盘产物 == 重新渲染结果
    python scripts/gen_index.py metrics    # 只打印指标（发行说明用）

``--check`` 的语义与 ``scripts/gen_stats.py --check`` 一致：**有漂移即 exit 1**。
CI 里跑它，于是"改了代码但忘了跑生成器"和"手改生成物"都会变成红灯。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from atri_x.devtools.cli import main as devtools_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    if not args:
        args = ["all"]
    elif args[0] in {"-c", "--check", "check"}:
        # ``python scripts/gen_index.py --check`` / ``... check`` 都当"校验全部产物"。
        args = ["all", "--check"]
    elif args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    elif args[0] not in {"all", "metrics", "map", "deps", "entrypoints", "test-map", "go-map"}:
        print(f"未知子命令：{args[0]}（可用：all / check / metrics / map / deps / "
              "entrypoints / test-map / go-map）")
        return 1
    return devtools_main([*args, "--root", str(REPO)])


if __name__ == "__main__":
    raise SystemExit(main())
