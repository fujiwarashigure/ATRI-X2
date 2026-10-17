"""``read_file`` 的 AST 搜索模式：Go / Shell 源码的标识符与函数名检索。

对应 Go：``internal/tool/builtin/file_tools_ast.go``。

与 Go 的差异（必须记录）：

* Go 用 ``go/parser`` + ``go/ast`` 做真正的语法分析。Python 标准库没有 Go 解析器，
  这里用 **clean-room 词法扫描**：剥掉注释与字符串字面量后按 token 归类，容器范围
  由 ``func`` / ``type`` 声明的花括号配对推出。结果是「近似 AST」——对合法 Go
  足够准确，但**不会**像 Go 那样报 ``Go AST 解析存在错误`` 的告警（我们没有解析器
  可以失败），语法损坏的文件会静默给出不完整结果。
* 列号按 **UTF-8 字节** 计算，与 Go 的 ``token.Position.Column`` 口径一致。
* Shell 侧用本仓 :mod:`atri_x.shellparse`（PORTING.md §4.1 指定的 mvdan/sh 替代物），
  节点语义与 ``syntax`` 对齐；不需要 ``LangVariant``（解析器自行识别）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError
from atri_x.shellparse import (
    DoubleQuoted,
    FunctionDecl,
    Literal,
    ParamExpansion,
    Script,
    ShellParseError,
    iter_nodes,
    parse as shell_parse,
)
from atri_x.tool.builtin.common import go_quote
from atri_x.tool.tool import Result
from atri_x.utils.fileops import (
    File,
    content_revision,
    normalize_grep_context_lines,
    normalize_max_matches,
    split_lines,
)

READ_FILE_MODE_READ = "read"
READ_FILE_MODE_GREP = "grep"
READ_FILE_MODE_AST = "ast"
READ_FILE_MODE_AST_FUNCTION = "ast_function"


@dataclass
class ASTMatch:
    """一个标识符命中。对应 Go ``astMatch``。"""

    line: int = 0
    column: int = 0
    kind: str = ""
    container: str = ""


@dataclass
class ASTFunctionMatch:
    """一个函数声明命中。对应 Go ``astFunctionMatch``。"""

    start_line: int = 0
    end_line: int = 0
    kind: str = ""
    name: str = ""


def normalize_read_file_mode(value: str) -> str:
    """对应 Go ``normalizeReadFileMode``。"""
    mode = (value or "").strip().lower()
    if mode == "":
        return READ_FILE_MODE_READ
    if mode in (
        READ_FILE_MODE_READ,
        READ_FILE_MODE_GREP,
        READ_FILE_MODE_AST,
        READ_FILE_MODE_AST_FUNCTION,
    ):
        return mode
    raise InvalidError("read_file mode must be read, grep, ast, or ast_function")


def read_file_ast_result(
    file: File,
    query: str,
    context_lines: int,
    max_matches: int,
    index: int,
    warnings: list[str],
) -> Any:
    """对应 Go ``readFileASTResult``。"""
    query = (query or "").strip()
    if query == "":
        raise InvalidError("query is required when mode is ast")
    language = detect_ast_language(file.path, file.text)
    if language == "go":
        matches = find_go_ast_matches(file.path, file.text, query)
    else:
        matches = find_shell_ast_matches(file.text, query)
    return format_ast_matches(
        file, query, language, matches, context_lines, max_matches, index, warnings
    )


def read_file_ast_function_result(
    file: File,
    query: str,
    max_matches: int,
    index: int,
    warnings: list[str],
) -> Any:
    """对应 Go ``readFileASTFunctionResult``。"""
    query = (query or "").strip()
    if query == "":
        raise InvalidError("query is required when mode is ast_function")
    language = detect_ast_language(file.path, file.text)
    if language == "go":
        matches = find_go_ast_functions(file.path, file.text, query)
    else:
        matches = find_shell_ast_functions(file.text, query)
    return format_ast_function_matches(
        file, query, language, matches, max_matches, index, warnings
    )


def detect_ast_language(path: str, text: str) -> str:
    """对应 Go ``detectASTLanguage``：只支持 Go 与 Shell。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".go":
        return "go"
    first_line = (text.split("\n", 1)[0] or "").strip().lower()
    if ext == ".mksh" or "mksh" in first_line:
        return "shell"
    if ext in (".sh", ".bash", ".bats") or (
        first_line.startswith("#!")
        and (
            "bash" in first_line
            or "/sh" in first_line
            or " dash" in first_line
            or " ash" in first_line
        )
    ):
        return "shell"
    raise InvalidError("AST search only supports Go and Shell files")


# ---------------------------------------------------------------------------
# Go：clean-room 词法扫描
# ---------------------------------------------------------------------------
_GO_KEYWORDS = frozenset(
    """break case chan const continue default defer else fallthrough for func go goto
    if import interface map package range return select struct switch type var""".split()
)
_GO_IDENT_RE = re.compile(r"[A-Za-z_\u00c0-\uffff][A-Za-z0-9_\u00c0-\uffff]*")


@dataclass
class _GoToken:
    text: str
    line: int
    col: int
    start: int
    end: int


@dataclass
class _GoRange:
    container: str
    start: int
    end: int


def _go_mask(source: str) -> str:
    """把注释与字符串/字符字面量替换成等长空白，保留换行以便定位。"""
    out = list(source)
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
            continue
        if ch in ('"', "'", "`"):
            quote = ch
            out[i] = " "
            i += 1
            while i < n:
                if quote != "`" and source[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    if source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if source[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _go_tokens(masked: str) -> list[_GoToken]:
    tokens: list[_GoToken] = []
    line = 1
    line_start = 0
    i = 0
    n = len(masked)
    while i < n:
        ch = masked[i]
        if ch == "\n":
            line += 1
            i += 1
            line_start = i
            continue
        if ch.isspace():
            i += 1
            continue
        match = _GO_IDENT_RE.match(masked, i)
        if match:
            tokens.append(
                _GoToken(match.group(0), line, i - line_start + 1, i, match.end())
            )
            i = match.end()
            continue
        tokens.append(_GoToken(ch, line, i - line_start + 1, i, i + 1))
        i += 1
    return tokens


def _match_brace(tokens: list[_GoToken], open_index: int) -> int:
    """返回与 ``tokens[open_index]``（必须是 ``{``）配对的 ``}`` 下标；找不到返回末尾。"""
    depth = 0
    for i in range(open_index, len(tokens)):
        text = tokens[i].text
        if text == "{":
            depth += 1
        elif text == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(tokens) - 1


def _go_ranges(tokens: list[_GoToken]) -> list[_GoRange]:
    """收集 Go 的 func / type 容器范围（对应 Go 的 FuncDecl / TypeSpec 栈）。"""
    ranges: list[_GoRange] = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token.text == "func":
            j = i + 1
            receiver = ""
            if j < n and tokens[j].text == "(":
                depth = 0
                start = j
                while j < n:
                    if tokens[j].text == "(":
                        depth += 1
                    elif tokens[j].text == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                receiver = _receiver_type(tokens[start + 1 : j])
                j += 1
            if j < n and _GO_IDENT_RE.fullmatch(tokens[j].text):
                name = tokens[j].text
                if receiver:
                    name = f"({receiver}).{name}"
                body = j
                while body < n and tokens[body].text not in ("{", "}"):
                    body += 1
                if body < n and tokens[body].text == "{":
                    close = _match_brace(tokens, body)
                    ranges.append(
                        _GoRange(
                            "function " + name.split(".")[-1],
                            tokens[i].start,
                            tokens[close].end,
                        )
                    )
                    i = close
        elif token.text == "type":
            j = i + 1
            if j < n and _GO_IDENT_RE.fullmatch(tokens[j].text):
                name = tokens[j].text
                k = j + 1
                if k < n and tokens[k].text == "=":
                    k += 1
                if k < n and tokens[k].text in ("struct", "interface"):
                    k += 1
                if k < n and tokens[k].text == "{":
                    close = _match_brace(tokens, k)
                    end = tokens[close].end
                else:
                    end = tokens[j].end
                    m = j + 1
                    while m < n and tokens[m].line == tokens[j].line:
                        end = tokens[m].end
                        m += 1
                ranges.append(_GoRange("type " + name, tokens[i].start, end))
        i += 1
    ranges.sort(key=lambda item: item.start)
    return ranges


def _receiver_type(tokens: list[_GoToken]) -> str:
    """对应 Go ``goASTFunctionName``：``format.Node(Recv.List[0].Type)``。"""
    texts = [token.text for token in tokens]
    if len(texts) >= 2 and _GO_IDENT_RE.fullmatch(texts[0]):
        texts = texts[1:]
    return "".join(texts)


def _go_container(ranges: list[_GoRange], offset: int) -> str:
    """对应 Go ``goASTContainer``：最内层 func/type 容器，否则 ``file``。"""
    best: _GoRange | None = None
    for item in ranges:
        if item.start <= offset < item.end and (
            best is None or item.start >= best.start
        ):
            best = item
    return best.container if best is not None else "file"


def _go_byte_column(line_text: str, char_index: int) -> int:
    return len(line_text[:char_index].encode("utf-8")) + 1


def find_go_ast_matches(path: str, source: str, query: str) -> list[ASTMatch]:
    """对应 Go ``findGoASTMatches``。"""
    if (source or "").strip() == "":
        return []
    masked = _go_mask(source)
    tokens = _go_tokens(masked)
    ranges = _go_ranges(tokens)
    matches: list[ASTMatch] = []
    for token in tokens:
        if token.text != query or token.text in _GO_KEYWORDS:
            continue
        matches.append(
            ASTMatch(
                line=token.line,
                column=token.col,
                kind="identifier",
                container=_go_container(ranges, token.start),
            )
        )
    return matches


def find_go_ast_functions(path: str, source: str, query: str) -> list[ASTFunctionMatch]:
    """对应 Go ``findGoASTFunctions``：按函数名精确匹配。"""
    if (source or "").strip() == "":
        return []
    masked = _go_mask(source)
    tokens = _go_tokens(masked)
    matches: list[ASTFunctionMatch] = []
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].text != "func":
            i += 1
            continue
        j = i + 1
        receiver = ""
        kind = "function"
        if j < n and tokens[j].text == "(":
            depth = 0
            start = j
            while j < n:
                if tokens[j].text == "(":
                    depth += 1
                elif tokens[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            receiver = _receiver_type(tokens[start + 1 : j])
            j += 1
        if j >= n or not _GO_IDENT_RE.fullmatch(tokens[j].text):
            i += 1
            continue
        short_name = tokens[j].text
        body = j
        while body < n and tokens[body].text not in ("{", "}"):
            body += 1
        close = _match_brace(tokens, body) if body < n else body
        if short_name == query:
            name = f"({receiver}).{short_name}" if receiver else short_name
            matches.append(
                ASTFunctionMatch(
                    start_line=tokens[i].line,
                    end_line=tokens[close].line if close < n else tokens[j].line,
                    kind="method" if receiver else kind,
                    name=name,
                )
            )
        i = close + 1 if close > i else i + 1
    return matches


# ---------------------------------------------------------------------------
# Shell：基于 atri_x.shellparse
# ---------------------------------------------------------------------------
def _shell_parse_tree(source: str) -> tuple[Script | None, str]:
    """解析 Shell 源码；失败时返回 ``(None, "")``，交由调用方决定。"""
    try:
        return shell_parse(source), ""
    except ShellParseError:
        return None, ""


def find_shell_ast_matches(source: str, query: str) -> list[ASTMatch]:
    """对应 Go ``findShellASTMatches``。"""
    tree, _ = _shell_parse_tree(source)
    if tree is None:
        return []
    matches: list[ASTMatch] = []
    _walk_shell(tree, [], matches, query)
    return matches


def _walk_shell(
    node: Any, stack: list[Any], matches: list[ASTMatch], query: str
) -> None:
    stack.append(node)
    if isinstance(node, ParamExpansion):
        if node.name == query:
            matches.append(
                ASTMatch(
                    line=node.pos.line,
                    column=node.pos.col + 1,
                    kind="parameter",
                    container=_shell_container(stack),
                )
            )
    elif isinstance(node, Literal) and node.value == query and not _shell_quoted(stack):
        matches.append(
            ASTMatch(
                line=node.pos.line,
                column=node.pos.col,
                kind="word",
                container=_shell_container(stack),
            )
        )
    for child in node.children():
        _walk_shell(child, stack, matches, query)
    stack.pop()


def _shell_quoted(stack: list[Any]) -> bool:
    """对应 Go ``shellQuotedLiteral``：位于双引号内且自身不是参数展开。"""
    return any(isinstance(item, DoubleQuoted) for item in stack)


def _shell_container(stack: list[Any]) -> str:
    """对应 Go ``shellASTContainer``。"""
    for item in reversed(stack):
        if isinstance(item, FunctionDecl):
            return "function " + item.name
    return "file"


def find_shell_ast_functions(source: str, query: str) -> list[ASTFunctionMatch]:
    """对应 Go ``findShellASTFunctions``。"""
    tree, _ = _shell_parse_tree(source)
    if tree is None:
        return []
    matches: list[ASTFunctionMatch] = []
    for item in iter_nodes(tree):
        if not isinstance(item, FunctionDecl) or item.name != query:
            continue
        matches.append(
            ASTFunctionMatch(
                start_line=item.pos.line,
                end_line=_shell_end_line(item, source),
                kind="function",
                name=item.name,
            )
        )
    return matches


def _shell_end_line(node: Any, source: str) -> int:
    """用节点源码区间推算结束行（shellparse 的 Pos 只给起止偏移）。"""
    end_offset = getattr(node.pos, "end", 0)
    if end_offset <= 0:
        return node.pos.line
    prefix = source[:end_offset]
    return prefix.count("\n") + 1


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def format_ast_matches(
    file: File,
    query: str,
    language: str,
    matches: list[ASTMatch],
    context_lines: int,
    max_matches: int,
    index: int,
    warnings: list[str],
) -> Any:
    """对应 Go ``formatASTMatches``。"""
    context_lines = normalize_grep_context_lines(context_lines)
    max_matches = normalize_max_matches(max_matches)
    matches = sorted(matches, key=lambda item: (item.line, item.column))
    match_count = len(matches)
    truncated = match_count > max_matches
    if index > 0:
        if index > match_count:
            return Result(
                content=f"index {index} is out of range; found {match_count} matches",
                warnings=warnings,
            )
        matches = matches[index - 1 : index]
        truncated = False
    elif truncated:
        matches = matches[:max_matches]
    lines = split_lines(file.text)
    out = [
        f"file: {file.path}\n",
        f"encoding: {file.encoding}\n",
        f"revision: {content_revision(file.bytes)}\n",
        f"ast: {go_quote(query)}\n",
        f"language: {language}\n",
        f"matches: {len(matches)}\n",
        f"truncated: {_go_bool(truncated)}\n",
    ]
    if index > 0:
        out.append(f"index: {index}\n")
    out.append("content:\n")
    width = len(str(len(lines)))
    for position, match in enumerate(matches):
        if position > 0:
            out.append("--\n")
        out.append(
            f"{position + 1}. match: {match.line}:{match.column} "
            f"[{match.kind}] in {match.container}\n"
        )
        start = max(1, match.line - context_lines)
        end = min(len(lines), match.line + context_lines)
        for line in range(start, end + 1):
            marker = ">" if line == match.line else " "
            out.append(f"{marker} {line:>{width}} | {lines[line - 1]}\n")
    return Result(content="".join(out), warnings=warnings)


def format_ast_function_matches(
    file: File,
    query: str,
    language: str,
    matches: list[ASTFunctionMatch],
    max_matches: int,
    index: int,
    warnings: list[str],
) -> Any:
    """对应 Go ``formatASTFunctionMatches``。"""
    max_matches = normalize_max_matches(max_matches)
    match_count = len(matches)
    truncated = match_count > max_matches
    if index > 0:
        if index > match_count:
            return Result(
                content=f"index {index} is out of range; found {match_count} matches",
                warnings=warnings,
            )
        matches = matches[index - 1 : index]
        truncated = False
    elif truncated:
        matches = matches[:max_matches]
    out = [
        f"file: {file.path}\n",
        f"encoding: {file.encoding}\n",
        f"revision: {content_revision(file.bytes)}\n",
        f"ast_function: {go_quote(query)}\n",
        f"language: {language}\n",
        f"matches: {len(matches)}\n",
        f"truncated: {_go_bool(truncated)}\n",
    ]
    if len(matches) > 1 and index == 0:
        out.append("selection_required: true\ncontent:\n")
        for position, match in enumerate(matches):
            out.append(
                f"{position + 1}. {match.name} - {file.path}:"
                f"{match.start_line}-{match.end_line}\n"
            )
        return Result(content="".join(out), warnings=warnings)
    if index > 0:
        out.append(f"index: {index}\n")
    out.append("content:\n")
    lines = split_lines(file.text)
    width = len(str(len(lines)))
    for match in matches:
        out.append(
            f"match: {match.start_line}-{match.end_line} [{match.kind}] {match.name}\n"
        )
        for line in range(match.start_line, match.end_line + 1):
            out.append(f"  {line:>{width}} | {lines[line - 1]}\n")
    return Result(content="".join(out), warnings=warnings)


def _go_bool(value: bool) -> str:
    return "true" if value else "false"


__all__ = [name for name in dir() if not name.startswith("_")]
