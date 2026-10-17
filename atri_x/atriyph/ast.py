"""对应 Go：``internal/atriyph/ast.go`` —— Atriyph 文档的 AST 与诊断类型。

Atriyph 是 ATRI-X 自己的 Skill 描述语言（``SKILL.atriyph``）。本模块只承载
scanner/descriptor 需要的信息：kind/name/steps 与可回馈给 LLM 的诊断。

命名遵循 PORTING §1.4：Go 导出类型保持 PascalCase，字段转 snake_case。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.errors import InvalidError

__all__ = [
    "AtriyphError",
    "DIAGNOSTIC_ERROR",
    "DIAGNOSTIC_WARNING",
    "Diagnostic",
    "DiagnosticSeverity",
    "Document",
    "Header",
    "Step",
]

#: 诊断级别。Go：``DiagnosticSeverity``（``DiagnosticError``/``DiagnosticWarning``）。
DiagnosticSeverity = str

DIAGNOSTIC_ERROR: DiagnosticSeverity = "error"
DIAGNOSTIC_WARNING: DiagnosticSeverity = "warning"


@dataclass(slots=True)
class Document:
    """Atriyph 文档的轻量解析结果（Go：``Document``）。"""

    kind: str = ""
    name: str = ""
    steps: list[Step] = field(default_factory=list)


@dataclass(slots=True)
class Header:
    """首行 header 的轻量解析结果（Go：``Header``）。"""

    kind: str = ""
    name: str = ""
    description: str = ""


@dataclass(slots=True)
class Step:
    """一个 ``step`` 命名阶段块（Go：``Step``）。"""

    name: str
    line: int


@dataclass(slots=True)
class Diagnostic:
    """一条可反馈给 LLM 重写 Atriyph 的格式问题（Go：``Diagnostic``）。

    ``severity`` 为空串时按 error 处理（与 Go 的 ``errorDiagnostics`` 一致）。
    """

    line: int = 0
    message: str = ""
    severity: DiagnosticSeverity = ""


class AtriyphError(InvalidError):
    """Atriyph 文档校验失败。

    对应 Go：``fmt.Errorf("invalid Atriyph:\\n- %s", ...)``；``str(err)`` 即
    Go 侧 error 的 ``Error()`` 文本，便于逐字对照测试。
    """
