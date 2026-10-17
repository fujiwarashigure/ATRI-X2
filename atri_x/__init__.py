"""ATRI-X2-Py：ATRI-X（Go）的 Python 全量重写。

对应上游 module ``github.com/fujiwarashigure/ATRI-X``。
纯标准库实现，入口 ``python -m atri_x``。

模块布局与 Go 包的映射关系见仓库根目录 ``PORTING.md``。
"""

from __future__ import annotations

__all__ = ["__version__", "NAME"]

NAME = "atri-x"
__version__ = "0.9.0"
