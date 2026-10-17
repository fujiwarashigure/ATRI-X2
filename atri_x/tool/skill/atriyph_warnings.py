"""把 Atriyph 诊断拼进工具输出。

对应 Go：``internal/tool/skill/atriyph_warnings.go``。

只有 warning 级诊断会被拼上（error 级由调用方自己决定是否中止）。
"""

from __future__ import annotations

from typing import Sequence

from atri_x.atriyph import Diagnostic, format_diagnostics, warning_diagnostics
from atri_x.tool.tool import append_warnings

__all__ = ["append_atriyph_warnings"]


def append_atriyph_warnings(content: str, diagnostics: Sequence[Diagnostic] | None) -> str:
    """有 warning 时把诊断块追加到 ``content`` 后面。对应 Go ``appendAtriyphWarnings``。"""
    warnings = warning_diagnostics(list(diagnostics or []))
    if not warnings:
        return content
    return append_warnings(content, [format_diagnostics(warnings).strip()])
