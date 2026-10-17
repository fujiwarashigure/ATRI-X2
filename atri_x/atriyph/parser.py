"""对应 Go：``internal/atriyph/parser.go`` —— Atriyph 解析与校验。

逐行对照移植 ``ParseSkill``/``ValidateSkill``/``ParseTask``/``ValidateTask``/
``ParseHeader`` 与全部内部函数；诊断文本与 Go 完全一致（含 ``line N:`` 前缀与
``invalid Atriyph:`` 包裹格式），因此 ``internal/atriyph/parser_test.go`` 的
每个断言都能逐字对照。

对外入口：:func:`parse_skill` / :func:`parse_task`（失败抛 :class:`AtriyphError`）、
:func:`validate_skill` / :func:`validate_task`（Go 语义 ``(Document, []Diagnostic,
error)``）、:func:`parse_header`（只解析首行）、:func:`lint`（永不抛异常，返回全部
诊断）、:func:`warning_diagnostics` / :func:`format_diagnostics`（Go 同名函数）。

与 Go 的差异只有两处（都无行为影响）：``str.strip()`` 与 ``strings.TrimSpace``
对 ``\\x1c``-``\\x1f`` 的处理略有不同；``strconv.Atoi`` 用 :func:`_go_atoi`
复刻（不接受 ``_`` 分隔符与 Unicode 数字，和 Go 一致）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .ast import (
    DIAGNOSTIC_ERROR, DIAGNOSTIC_WARNING, AtriyphError, Diagnostic, Document, Header, Step,
)

__all__ = [
    "error_diagnostics", "format_diagnostics", "lint", "lint_warnings", "normalize_text",
    "parse", "parse_header", "parse_skill", "parse_task", "validate_skill",
    "validate_task", "warning_diagnostics",
]

_BLOCK_IF = "if"
_BLOCK_ELSE = "else"
_BLOCK_EACH = "each"
_BLOCK_STEP = "step"


def _go_atoi(value: str) -> int:
    """对应 Go 的 ``strconv.Atoi``：只接受可选符号 + ASCII 十进制数字。"""
    text = value.strip()
    digits = text[1:] if text[:1] in ("+", "-") else text
    if not digits or any(ch < "0" or ch > "9" for ch in digits):
        raise ValueError(f"invalid syntax: {value!r}")
    return int(text)

def _is_digit(ch: str) -> bool:
    """对应 Go 的 ``unicode.IsDigit``（Nd 类），不是 Python 的 ``str.isdigit``。"""
    return unicodedata.category(ch) == "Nd"

def _valid_ident(value: str) -> bool:
    if not value:
        return False
    for idx, ch in enumerate(value):
        if idx == 0:
            if ch != "_" and not ch.isalpha():
                return False
            continue
        if ch != "_" and not ch.isalpha() and not _is_digit(ch):
            return False
    return True

def _valid_variable(value: str) -> bool:
    return value.startswith("$") and _valid_ident(value[1:])

def _valid_name(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    for idx, ch in enumerate(value):
        if idx == 0:
            if ch < "a" or ch > "z":
                return False
            continue
        if (ch < "a" or ch > "z") and (ch < "0" or ch > "9") and ch != "_" and ch != "-":
            return False
    return True

def _valid_step_name(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    for idx, ch in enumerate(value):
        if idx == 0:
            if (ch < "a" or ch > "z") and (ch < "0" or ch > "9"):
                return False
            continue
        if (ch < "a" or ch > "z") and (ch < "0" or ch > "9") and ch != "_" and ch != "-":
            return False
    return True

def _valid_type(value: str) -> bool:
    return _valid_ident(value)

def _split_top_level(text: str, sep: str) -> list[str]:
    """对应 Go 的 ``splitTopLevel``（这里只用于单字符分隔符 ``','``）。"""
    return text.split(sep)

def _parse_variable_ref(text: str) -> tuple[str, str, str, bool]:
    marker = ""
    if text.endswith("!") or text.endswith("?"):
        marker = text[-1:]
        text = text[:-1].strip()
    if ":" in text:
        left, _, right = text.partition(":")
        name, typ = left.strip(), right.strip()
        if not _valid_variable(name) or not _valid_type(typ):
            return "", "", "", False
        return name, typ, marker, True
    name = text.strip()
    if not _valid_variable(name):
        return "", "", "", False
    return name, "", marker, True

def _parse_typed_variable(text: str) -> tuple[str, str, str, bool]:
    marker = ""
    if text.endswith("!") or text.endswith("?"):
        marker = text[-1:]
        text = text[:-1].strip()
    if ":" not in text:
        return "", "", "", False
    left, _, right = text.partition(":")
    name, typ = left.strip(), right.strip()
    if not _valid_variable(name) or not _valid_type(typ):
        return "", "", "", False
    return name, typ, marker, True

def _parse_assignment_parts(text: str) -> tuple[str, str, str, str, bool]:
    if "=" not in text:
        return "", "", "", "", False
    left, _, right = text.partition("=")
    name, _, marker, ok = _parse_variable_ref(left.strip())
    if not ok or marker:
        return "", "", "", "", False
    return name, "", "", right.strip(), True

def _valid_call_args(args: str) -> bool:
    if not args:
        return True
    for part in _split_top_level(args, ","):
        part = part.strip()
        if not part:
            return False
        if "=" not in part:
            return False
        key, _, value = part.partition("=")
        if not _valid_ident(key.strip()) or not value.strip():
            return False
    return True

def error_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """对应 Go 的 ``errorDiagnostics``：``severity`` 为空或 ``error`` 的都算错误。"""
    return [d for d in diagnostics if d.severity in ("", DIAGNOSTIC_ERROR)]

def warning_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """对应 Go 的 ``WarningDiagnostics``。"""
    return [item for item in diagnostics if item.severity == DIAGNOSTIC_WARNING]

def format_diagnostics(diagnostics: list[Diagnostic]) -> str:
    """对应 Go 的 ``FormatDiagnostics``（``line N: msg``，行号 0 时只输出 msg）。"""
    parts = []
    for item in diagnostics:
        if item.line > 0:
            parts.append(f"line {item.line}: {item.message}")
        else:
            parts.append(item.message)
    return "\n".join(parts)

def _diagnostics_error(diagnostics: list[Diagnostic]) -> AtriyphError:
    body = format_diagnostics(diagnostics).replace("\n", "\n- ")
    return AtriyphError(f"invalid Atriyph:\n- {body}")

@dataclass(slots=True)
class _BlockFrame:
    kind: str
    direct_count: int = 0

class _Parser:
    """对应 Go 的 ``parser`` 结构（逐方法移植）。"""

    def __init__(self, expected_kind: str, expected_name: str) -> None:
        self.expected_kind = expected_kind
        self.expected_name = expected_name
        self.doc = Document()
        self.diagnostics: list[Diagnostic] = []
        self.seen_header = False
        self.blocks: list[_BlockFrame] = []
        self.last_closed = ""

    # -- 行级 -------------------------------------------------------------
    def parse_line(self, trimmed: str, line_no: int) -> None:
        if trimmed == "" or trimmed.startswith("//"):
            return
        if not self.seen_header:
            self.parse_header(trimmed, line_no)
            return
        if trimmed.startswith("#"):
            self.add(line_no, "header must appear only once")
            return
        if trimmed == "}":
            self.close_block(line_no)
            return
        if self.blocks:
            self.blocks[-1].direct_count += 1
        self.parse_statement_content(trimmed, line_no)

    def parse_statement_content(self, trimmed: str, line_no: int) -> None:
        """分发一条非控制行（非空、非注释、非 header、非 ``}``）的具体语法。"""
        if not trimmed.startswith("?else"):
            self.last_closed = ""
        if trimmed.startswith("<-"):
            self.parse_io(trimmed, line_no, True)
        elif trimmed.startswith("->"):
            self.parse_io(trimmed, line_no, False)
        elif trimmed.startswith("=>"):
            self.parse_derive(trimmed, line_no)
        elif trimmed.startswith("$"):
            self.parse_assignment(trimmed, line_no)
        elif trimmed.startswith("?if"):
            self.parse_if(trimmed, line_no)
        elif trimmed.startswith("?else"):
            self.parse_else(trimmed, line_no)
        elif trimmed.startswith("? else") or trimmed.startswith("else"):
            self.add(line_no, "else must be ?else{")
        elif trimmed.startswith("each"):
            self.parse_each(trimmed, line_no)
        elif trimmed.startswith("step"):
            self.parse_step(trimmed, line_no)
        elif trimmed.startswith(">"):
            self.parse_output(trimmed, line_no)
        elif trimmed.startswith("@tool"):
            self.parse_call(trimmed, line_no, "@tool")
        elif trimmed.startswith("@skill"):
            self.parse_call(trimmed, line_no, "@skill")
        elif trimmed.startswith("**"):
            self.parse_text_slot(trimmed, line_no, "**")
        elif trimmed.startswith("~"):
            self.parse_text_slot(trimmed, line_no, "~")
        else:
            self.add(line_no, "line must start with a valid Atriyph token")

    def parse_header(self, line: str, line_no: int) -> None:
        fields = line.split()
        if (
            len(fields) < 2
            or fields[0] not in ("#skill", "#task")
            or not _valid_name(fields[1])
        ):
            self.add(line_no, f"first statement must be #{self.expected_kind} <name>")
            return
        if len(fields) > 2:
            rest = line.removeprefix(fields[0] + " " + fields[1]).strip()
            if not rest.startswith("- ") or not rest.removeprefix("- ").strip():
                self.add(line_no, f"first statement must be #{self.expected_kind} <name>")
                return
        self.doc.kind = fields[0].removeprefix("#")
        self.doc.name = fields[1]
        self.seen_header = True

    def parse_io(self, line: str, line_no: int, input_: bool) -> None:
        prefix = "<-"
        message = "input must be <- $name:type, <- $name:type! or <- $name:type?"
        if not input_:
            prefix = "->"
            message = "output must be -> $name:type"
        rest = line.removeprefix(prefix).strip()
        name, typ, marker, ok = _parse_typed_variable(rest)
        if not ok or not name or not typ or (not input_ and marker != ""):
            self.add(line_no, message)

    def parse_assignment(self, line: str, line_no: int) -> None:
        name, _, _, rhs, ok = _parse_assignment_parts(line)
        if not ok or rhs.strip() == "":
            self.add(line_no, "assignment must be $name[:type] = value")
            return
        self.check_reserved_variable(name, line_no)

    def parse_derive(self, line: str, line_no: int) -> None:
        rest = line.removeprefix("=>").strip()
        name, _, _, rhs, ok = _parse_assignment_parts(rest)
        if not ok or rhs.strip() == "":
            self.add(line_no, "derive must be => $name[:type] = expression")
            return
        self.check_reserved_variable(name, line_no)
        self.check_derive_expression(rhs, line_no)

    def parse_if(self, line: str, line_no: int) -> None:
        if not line.endswith("{"):
            self.add(line_no, "?if must be ?if(condition) {")
            return
        body = line.removeprefix("?if").removesuffix("{").strip()
        if (
            not body.startswith("(")
            or not body.endswith(")")
            or body[1:-1].strip() == ""
            or "{" in body[1:-1]
            or "}" in body[1:-1]
        ):
            self.add(line_no, "?if must be ?if(condition) {")
            self.open_block(_BLOCK_IF)
            return
        self.open_block(_BLOCK_IF)

    def parse_else(self, line: str, line_no: int) -> None:
        rest = line.removeprefix("?else").strip()
        if rest != "{":
            self.add(line_no, "?else must be ?else{")
            return
        if self.last_closed != _BLOCK_IF:
            self.add(line_no, "?else must immediately follow a closed ?if block")
            return
        self.open_block(_BLOCK_ELSE)

    def parse_each(self, line: str, line_no: int) -> None:
        if not line.startswith("each(") or not line.endswith("{"):
            self.add(line_no, "each must be each($item in $items, limit=N) {")
            if line.endswith("{"):
                self.open_block(_BLOCK_EACH)
            return
        body = line.removeprefix("each(").removesuffix("{").strip()
        if not body.endswith(")"):
            self.add(line_no, "each must be each($item in $items, limit=N) {")
            self.open_block(_BLOCK_EACH)
            return
        body = body.removesuffix(")").strip()
        parts = _split_top_level(body, ",")
        if len(parts) != 2:
            self.add(line_no, "each must be each($item in $items, limit=N) {")
            self.open_block(_BLOCK_EACH)
            return
        loop = parts[0].split()
        if len(loop) != 3 or not _valid_variable(loop[0]) or loop[1] != "in" or not _valid_variable(loop[2]):
            self.add(line_no, "each must be each($item in $items, limit=N) {")
            self.open_block(_BLOCK_EACH)
            return
        limit_key, cut, limit_value = parts[1].strip().partition("=")
        if not cut or limit_key.strip() != "limit":
            self.add(line_no, "each must be each($item in $items, limit=N) {")
            self.open_block(_BLOCK_EACH)
            return
        try:
            count = _go_atoi(limit_value.strip())
        except ValueError:
            self.add(line_no, "each must be each($item in $items, limit=N) {")
            self.open_block(_BLOCK_EACH)
            return
        if count <= 0:
            self.add(line_no, "each limit must be positive")
            return
        self.open_block(_BLOCK_EACH)

    def parse_step(self, line: str, line_no: int) -> None:
        rest = line.removeprefix("step").strip()
        if rest.endswith("{"):
            self.parse_step_block(rest, line_no)
            return
        idx = rest.find(":")
        if idx > 0:
            self.parse_step_single(rest[:idx], rest[idx + 1 :], line_no)
            return
        self.add(line_no, "step must be step <name> { or step <name>: <statement>")

    def parse_step_block(self, rest: str, line_no: int) -> None:
        name = rest.removesuffix("{").strip()
        if name == "" or " " in name or "\t" in name:
            self.add(line_no, "step must be step <name> {")
            return
        if not _valid_step_name(name):
            self.add(
                line_no,
                "step name must be lowercase letter or digit followed by lowercase letters, digits, _ or -",
            )
            return
        if self.reject_step_context(name, line_no):
            return
        self.doc.steps.append(Step(name, line_no))
        self.open_block(_BLOCK_STEP)

    def parse_step_single(self, name: str, after: str, line_no: int) -> None:
        name = name.strip()
        after = after.strip()
        if name == "" or " " in name or "\t" in name:
            self.add(line_no, "step must be step <name>: <statement>")
            return
        if not _valid_step_name(name):
            self.add(
                line_no,
                "step name must be lowercase letter or digit followed by lowercase letters, digits, _ or -",
            )
            return
        if after == "":
            self.add(line_no, "step single-line body must not be empty")
            return
        if not _valid_step_single_body(after):
            self.add(line_no, "step single-line body must be a simple statement")
            return
        if self.reject_step_context(name, line_no):
            return
        self.doc.steps.append(Step(name, line_no))
        self.parse_statement_content(after, line_no)

    def reject_step_context(self, name: str, line_no: int) -> bool:
        """检查 step 嵌套与重名；返回 True 表示已报错。"""
        for frame in self.blocks:
            if frame.kind == _BLOCK_STEP:
                self.add(line_no, "step blocks must not be nested")
                return True
        for step in self.doc.steps:
            if step.name == name:
                self.add(line_no, f"step name {name} already used")
                return True
        return False

    def parse_output(self, line: str, line_no: int) -> None:
        if line != ">" and not line.startswith("> "):
            self.add(line_no, "output must be > text")

    def parse_call(self, line: str, line_no: int, prefix: str) -> None:
        rest = line.removeprefix(prefix).strip()
        open_idx = rest.find("(")
        if open_idx <= 0 or not rest.endswith(")"):
            self.add(line_no, "call must be @tool/@skill name(k=v)")
            return
        name = rest[:open_idx].strip()
        args = rest[open_idx + 1 : len(rest) - 1].strip()
        if ("(" in args) or (")" in args) or not _valid_name(name) or not _valid_call_args(args):
            self.add(line_no, "call must be @tool/@skill name(k=v)")

    def parse_text_slot(self, line: str, line_no: int, prefix: str) -> None:
        text = line.removeprefix(prefix).strip()
        if text == "":
            self.add(line_no, prefix + " must have text")
            return
        if text.endswith(":") or text.endswith("："):
            self.warn(
                line_no,
                prefix
                + " text ends with a colon; this looks like a Markdown-style heading, use an atomic rule instead",
            )

    def close_block(self, line_no: int) -> None:
        if not self.blocks:
            self.add(line_no, "unexpected }")
            self.last_closed = ""
            return
        frame = self.blocks.pop()
        if frame.kind == _BLOCK_STEP:
            if frame.direct_count == 0:
                self.add(line_no, "step block must not be empty")
            elif frame.direct_count == 1:
                self.add(
                    line_no,
                    "step block with single statement should use single-line form: step <name>: <statement>",
                )
        self.last_closed = frame.kind

    def open_block(self, kind: str) -> None:
        self.blocks.append(_BlockFrame(kind))
        self.last_closed = ""

    def finish(self) -> None:
        if not self.seen_header:
            self.add(0, f"missing #{self.expected_kind} <name>")
        if self.expected_kind and self.doc.kind and self.doc.kind != self.expected_kind:
            self.add(
                0,
                f'document kind "{self.doc.kind}" does not match "{self.expected_kind}"',
            )
        if self.expected_name and self.doc.name and self.doc.name != self.expected_name:
            self.add(
                0,
                f'#{self.doc.kind} name "{self.doc.name}" does not match "{self.expected_name}"',
            )
        if self.blocks:
            self.add(0, "unclosed { block")

    def check_reserved_variable(self, name: str, line_no: int) -> None:
        if name in ("$user", "$assistant"):
            self.add(line_no, f"reserved variable {name} cannot be redefined")

    def check_derive_expression(self, expr: str, line_no: int) -> None:
        question = expr.find("?")
        if question < 0:
            return
        colon_offset = expr.find(":", question + 1)
        if colon_offset < 0:
            self.add(line_no, "ternary must be condition ? true : false")
            return
        colon = colon_offset
        if (
            expr[:question].strip() == ""
            or expr[question + 1 : colon].strip() == ""
            or expr[colon + 1 :].strip() == ""
        ):
            self.add(line_no, "ternary must be condition ? true : false")

    def add(self, line: int, message: str) -> None:
        self.diagnostics.append(Diagnostic(line, message, DIAGNOSTIC_ERROR))

    def warn(self, line: int, message: str) -> None:
        self.diagnostics.append(Diagnostic(line, message, DIAGNOSTIC_WARNING))

def _valid_step_single_body(after: str) -> bool:
    """限定单行 step 的 after 只能是简单单行语句，禁止控制流块与 ``}``。"""
    return after.startswith(
        ("$", "=>", ">", "@tool", "@skill", "**", "~", "<-", "->")
    )

# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------

def _parse_document(
    raw: str, expected_kind: str, expected_name: str
) -> tuple[Document, list[Diagnostic]]:
    text = raw.replace("\r\n", "\n")
    if text.strip() == "":
        return Document(), [Diagnostic(0, "atriyph is required", "")]
    parser = _Parser(expected_kind.strip(), expected_name.strip())
    for idx, line in enumerate(text.split("\n")):
        parser.parse_line(line.strip(), idx + 1)
    parser.finish()
    return parser.doc, parser.diagnostics

def _validate_kind(
    raw: str, expected_kind: str, expected_name: str
) -> tuple[Document, list[Diagnostic]]:
    doc, diagnostics = _parse_document(raw, expected_kind, expected_name)
    errors = error_diagnostics(diagnostics)
    if errors:
        raise _diagnostics_error(errors)
    return doc, diagnostics

def parse_skill(raw: str, expected_name: str = "") -> Document:
    """解析并校验 Atriyph skill 文档（Go：``ParseSkill``）；失败抛 :class:`AtriyphError`。"""
    doc, _ = validate_skill(raw, expected_name)
    return doc

def validate_skill(raw: str, expected_name: str = "") -> tuple[Document, list[Diagnostic]]:
    """解析 skill 文档并返回非阻断 warning（Go：``ValidateSkill``）。"""
    return _validate_kind(raw, "skill", expected_name)

def parse_task(raw: str, expected_name: str = "") -> Document:
    """解析并校验 Atriyph task 文档（Go：``ParseTask``）。"""
    doc, _ = validate_task(raw, expected_name)
    return doc

def validate_task(raw: str, expected_name: str = "") -> tuple[Document, list[Diagnostic]]:
    """解析 task 文档并返回非阻断 warning（Go：``ValidateTask``）。"""
    return _validate_kind(raw, "task", expected_name)

def parse(raw: str, expected_kind: str = "skill", expected_name: str = "") -> Document:
    """按 kind 解析（``expected_kind`` 为 ``"skill"`` 或 ``"task"``）。"""
    if expected_kind == "task":
        return parse_task(raw, expected_name)
    return parse_skill(raw, expected_name)

def lint(
    raw: str, expected_kind: str = "skill", expected_name: str = ""
) -> list[Diagnostic]:
    """返回全部诊断（error + warning），**永不抛异常**。

    docstring 里的 "warning" 是 :data:`atri_x.atriyph.ast.DIAGNOSTIC_WARNING`
    级别的 :class:`Diagnostic`；只要 warning 用 :func:`lint_warnings`。
    """
    _, diagnostics = _parse_document(raw, expected_kind, expected_name)
    return diagnostics

def lint_warnings(
    raw: str, expected_kind: str = "skill", expected_name: str = ""
) -> list[Diagnostic]:
    """只返回 warning 级诊断（Go：``WarningDiagnostics(lint(...))``）。"""
    return warning_diagnostics(lint(raw, expected_kind, expected_name))

def normalize_text(raw: str) -> str:
    """CRLF → LF，并保证文末只有一个换行（Go 侧无对应函数，仅作工具用）。"""
    text = raw.replace("\r\n", "\n")
    return text.rstrip("\n") + "\n" if text.strip() else ""

def parse_header(raw: str) -> Header:
    """只解析首行 header（Go：``ParseHeader``），不做全文结构校验。"""
    text = raw.replace("\r\n", "\n")
    for line in text.split("\n"):
        trimmed = line.strip()
        if trimmed == "" or trimmed.startswith("//"):
            continue
        fields = trimmed.split()
        if (
            len(fields) < 2
            or fields[0] not in ("#skill", "#task")
            or not _valid_name(fields[1])
        ):
            raise AtriyphError("first statement must be #skill/#task <name>")
        description = ""
        rest = trimmed.removeprefix(fields[0] + " " + fields[1]).strip()
        if rest.startswith("- "):
            description = rest.removeprefix("- ").strip()
        return Header(fields[0].removeprefix("#"), fields[1], description)
    raise AtriyphError("atriyph is required")
