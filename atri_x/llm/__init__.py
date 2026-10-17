"""LLM 抽象层。

对应 Go：``internal/llm``。对外表面等价于 Go 的 ``package llm``。
"""

from __future__ import annotations

from atri_x.llm.llm import *  # noqa: F401,F403
from atri_x.llm.llm import __all__ as _llm_all

__all__ = list(_llm_all)
