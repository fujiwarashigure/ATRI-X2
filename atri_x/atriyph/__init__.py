"""Atriyph 语言层：``SKILL.atriyph`` 的解析、校验与规则卡。

对应 Go：``internal/atriyph/{ast.go,parser.go,rules.go}``（648 行）。
消费方是 ``internal/tool/skill/``（``atriyph_warnings.go`` 用
``WarningDiagnostics`` + ``FormatDiagnostics`` 把 warning 拼进工具输出）。

Atriyph v0.3 语法要点（完整规则卡见 :func:`rule_card`）：

* 首条非空、非 ``//`` 行必须是 ``#skill <name> [- 描述]`` 或 ``#task ...``；
* 语句行首记号：``<-`` ``->`` ``$`` ``=>`` ``**`` ``~`` ``?if`` ``?else``
  ``each`` ``step`` ``>`` ``@tool`` ``@skill``，``}`` 必须独占一行；
* 块用 ``{``/``}``，``}`` 独行；``step`` 不可嵌套/空/重名，块内至少 2 句，
  单句用 ``step name: 语句``。

用法::

    from atri_x.atriyph import parse_skill, lint_warnings, format_diagnostics

    doc = parse_skill(text, "weather")          # 失败抛 AtriyphError
    warn = lint_warnings(text, expected_name="weather")
    print(format_diagnostics(warn))

本包是纯标准库实现，不依赖任何第三方包。
"""

from __future__ import annotations

from .ast import (
    DIAGNOSTIC_ERROR,
    DIAGNOSTIC_WARNING,
    AtriyphError,
    Diagnostic,
    DiagnosticSeverity,
    Document,
    Header,
    Step,
)
from .parser import (
    error_diagnostics,
    format_diagnostics,
    lint,
    lint_warnings,
    normalize_text,
    parse,
    parse_header,
    parse_skill,
    parse_task,
    validate_skill,
    validate_task,
    warning_diagnostics,
)
from .rules import FORMAT, SKILL_FILE_NAME, rule_card

__all__ = [
    "DIAGNOSTIC_ERROR",
    "DIAGNOSTIC_WARNING",
    "FORMAT",
    "SKILL_FILE_NAME",
    "AtriyphError",
    "Diagnostic",
    "DiagnosticSeverity",
    "Document",
    "Header",
    "Step",
    "error_diagnostics",
    "format_diagnostics",
    "lint",
    "lint_warnings",
    "normalize_text",
    "parse",
    "parse_header",
    "parse_skill",
    "parse_task",
    "rule_card",
    "validate_skill",
    "validate_task",
    "warning_diagnostics",
]
