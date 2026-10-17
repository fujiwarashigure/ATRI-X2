"""通用工具包。

对应 Go：``internal/utils/``。

Python 侧布局：

- :mod:`atri_x.utils.runes` —— Go ``[]rune`` 与 ``mattn/go-runewidth`` 的等价物
  （Go 里这两个能力分别来自语言内建与第三方库，Python 需要自己实现）。
- :mod:`atri_x.utils.fileops` —— 对应 Go ``internal/utils/fileops``。
"""

from __future__ import annotations

__all__ = ["fileops", "runes"]
