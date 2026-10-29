#!/usr/bin/env python3
"""ATRI-X 科研研究引擎的命令行入口。

AgentSkill 的 ``ATRIX_SKILL.toml`` 以
``[<venv python>, "<install>/engine/run.py", "<tool>"]`` 的形式调用本文件，
因此这里只做三件事：把引擎目录放进 ``sys.path``、统一标准流编码、交给
:func:`atrix_research.cli.main`。

也可以直接 ``python -m atrix_research``（见 ``atrix_research/__main__.py``）。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _force_utf8_stdio() -> None:
    """避免 Windows 控制台 cp936 把结果里的非 GBK 字符打成异常。

    工具输出是给模型读的 JSON，统一 UTF-8 才可复现；失败（例如管道）时静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> int:
    _force_utf8_stdio()
    from atrix_research.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
