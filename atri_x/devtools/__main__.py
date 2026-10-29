"""``python -m atri_x.devtools`` 的入口。

用法见 :mod:`atri_x.devtools.cli`：``map`` / ``deps`` / ``entrypoints`` / ``test-map`` /
``go-map`` / ``all`` / ``metrics``，全部支持 ``--check`` 与 ``--out``。
"""

from __future__ import annotations

from atri_x.devtools.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
