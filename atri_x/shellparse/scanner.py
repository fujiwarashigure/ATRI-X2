"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：单词/展开扫描层。

对应 Go：``mvdan.cc/sh/v3/syntax`` 的 ``parser.go`` 里 ``p.wordPart``/
``p.dqWord``/``p.paramExp`` 一族。本文件是**独立实现**，不含 ``bashlex``
（GPLv3+）的任何代码。

拆分来源（PORTING §2：单文件 ≤ 600 行）：本模块承载"源码字符/偏移层"
（:class:`SourceIndex`、嵌套上限、``$``/引号/括号扫描器），
:mod:`atri_x.shellparse.lexer` 只负责记号流与 here-document 正文装配。

覆盖：单/双引号、``$'..'``、``$".."``、反斜杠转义与续行、``$VAR``/``${VAR...}``、
``$(...)``/反引号、``$((...))``、``<( )``/``>( )``、``~``。全部只做结构识别，
不求值。
"""
from __future__ import annotations

from bisect import bisect_right

from .ast import (
    AnsiCQuoted,
    ArithExpansion,
    CommandSubstitution,
    DoubleQuoted,
    Literal,
    ParamExpansion,
    Pos,
    ProcSubstitution,
    ShellParseError,
    SingleQuoted,
    Tilde,
    Word,
    WordPart,
)

__all__ = ["MAX_NESTING_DEPTH", "SourceIndex", "WordScanner", "scan_arith", "scan_word_range"]

#: 词法/语法嵌套上限（``((((...))))``、``$($($(...`` 之类在这里报错）。
MAX_NESTING_DEPTH = 64

_WORD_BREAK = frozenset(" \t\n\r|&;()<>")
_SPECIAL_PARAMS = frozenset("@*#?-$!0123456789")
_ANSI_C_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}

class SourceIndex:
    """源码偏移 ↔ 行列换算（CRLF 与 CR 都算一个换行）。"""

    def __init__(self, source: str) -> None:
        self.source = source
        starts = [0]
        i = 0
        n = len(source)
        while i < n:
            c = source[i]
            if c == "\n":
                i += 1
                starts.append(i)
            elif c == "\r":
                i += 2 if i + 1 < n and source[i + 1] == "\n" else 1
                starts.append(i)
            else:
                i += 1
        self.line_starts = starts

    def line_col(self, offset: int) -> tuple[int, int]:
        offset = max(0, min(offset, len(self.source)))
        idx = bisect_right(self.line_starts, offset) - 1
        if idx < 0:
            idx = 0
        return idx + 1, offset - self.line_starts[idx] + 1

    def line_text(self, offset: int) -> str:
        line, _ = self.line_col(offset)
        start = self.line_starts[line - 1]
        end = start
        while end < len(self.source) and self.source[end] not in "\r\n":
            end += 1
        return self.source[start:end]

    def pos(self, start: int, end: int) -> Pos:
        line, col = self.line_col(start)
        end_line, end_col = self.line_col(end)
        return Pos(start, end, line, col, end_line, end_col)

    def error(self, offset: int, message: str) -> ShellParseError:
        line, col = self.line_col(offset)
        return ShellParseError(
            message, line=line, col=col, offset=offset, snippet=self.line_text(offset)
        )

def skip_newline(source: str, i: int, end: int) -> int:
    """跳过 ``\\n``/``\\r\\n``/``\\r`` 之一（CRLF 算一个换行）。"""
    if i >= end:
        return i
    if source[i] == "\n":
        return i + 1
    if source[i] == "\r":
        return i + 2 if i + 1 < end and source[i + 1] == "\n" else i + 1
    return i

def decode_ansi_c(text: str) -> str:
    """解码 ``$'...'`` 内容（``\\n`` ``\\xHH`` ``\\nnn`` ``\\uHHHH`` 等）。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
            continue
        if nxt in "01234567":
            j, digits = i + 1, ""
            while j < n and len(digits) < 3 and text[j] in "01234567":
                digits += text[j]
                j += 1
            out.append(chr(int(digits, 8) & 0xFF))
            i = j
            continue
        if nxt == "x":
            j, digits = i + 2, ""
            while j < n and len(digits) < 2 and text[j] in "0123456789abcdefABCDEF":
                digits += text[j]
                j += 1
            out.append(chr(int(digits, 16)) if digits else "x")
            i = j
            continue
        if nxt in "uU":
            width = 4 if nxt == "u" else 8
            j, digits = i + 2, ""
            while j < n and len(digits) < width and text[j] in "0123456789abcdefABCDEF":
                digits += text[j]
                j += 1
            out.append(chr(int(digits, 16)) if digits else nxt)
            i = j
            continue
        out.extend(("\\", nxt))
        i += 2
    return "".join(out)

def split_param_op(rest: str) -> tuple[str, str]:
    """切出 ``${name<op><operand>}`` 里的运算符与操作数（只识别结构）。"""
    if not rest:
        return "", ""
    head = rest[0]
    if head == ":":
        if len(rest) >= 2 and rest[1] in "-=?+":
            return rest[:2], rest[2:]
        return ":", rest[1:]
    if head in "-=?+":
        return head, rest[1:]
    if head in "#%":
        return (rest[:2], rest[2:]) if len(rest) >= 2 and rest[1] == head else (head, rest[1:])
    if head == "/":
        return (rest[:2], rest[2:]) if len(rest) >= 2 and rest[1] in "/#%" else ("/", rest[1:])
    if head in "^,":
        return (rest[:2], rest[2:]) if len(rest) >= 2 and rest[1] == head else (head, rest[1:])
    if head == "@" and len(rest) >= 2:
        return rest[:2], rest[2:]
    return "", rest

def scan_arith(source: str, open_offset: int, index: SourceIndex, max_depth: int) -> tuple[str, int]:
    """扫描 ``((expr))``：``open_offset`` 指向第一个 ``(``；返回 (表达式原文, 结束偏移)。"""
    end = len(source)
    i = open_offset + 2
    depth = 1
    while i < end:
        c = source[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = source.find("'", i + 1)
            i = end if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < end and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                if i + 1 < end and source[i + 1] == ")":
                    return source[open_offset + 2 : i], i + 2
                raise index.error(open_offset, "算术展开缺少配对的 )) ")
        i += 1
    raise index.error(open_offset, "未闭合的算术展开 ((")

def scan_word_range(source: str, start: int, end: int, index: SourceIndex,
                    max_depth: int = MAX_NESTING_DEPTH) -> Word:
    """按普通单词规则扫描 ``source[start:end]`` 成一个 :class:`Word`（赋值右值等用）。"""
    return WordScanner(source, start, end, index, max_depth).scan()

class WordScanner:
    """单词扫描器。``mode="heredoc"`` 时按 here-document 正文规则扫描（引号字面）。"""

    def __init__(
        self,
        source: str,
        start: int,
        end: int,
        index: SourceIndex,
        max_depth: int = MAX_NESTING_DEPTH,
        nest: int = 0,
        mode: str = "word",
    ) -> None:
        self.source = source
        self.start = start
        self.end = end
        self.index = index
        self.max_depth = max_depth
        self.nest = nest
        self.mode = mode
        self.i = start
        self.parts: list[WordPart] = []
        self._buf: list[str] = []
        self._buf_start = start
        self._escaped = False

    # -- 字面缓冲 ---------------------------------------------------------
    def _append(self, ch: str, at: int, escaped: bool = False) -> None:
        if not self._buf:
            self._buf_start = at
            self._escaped = False
        self._buf.append(ch)
        if escaped:
            self._escaped = True

    def _flush(self) -> None:
        if self._buf:
            self.parts.append(
                Literal(
                    self.index.pos(self._buf_start, self.i),
                    "".join(self._buf),
                    self.source[self._buf_start : self.i],
                    self._escaped,
                )
            )
            self._buf = []
            self._escaped = False
        self._buf_start = self.i

    def _guard(self, at: int) -> None:
        if self.nest >= self.max_depth:
            raise self.index.error(at, f"shell 嵌套层级过深（上限 {self.max_depth}）")

    # -- 主循环 -----------------------------------------------------------
    def scan(self) -> Word:
        src = self.source
        heredoc_mode = self.mode == "heredoc"
        while self.i < self.end:
            c = src[self.i]
            if c == "\\":
                nxt = src[self.i + 1] if self.i + 1 < self.end else ""
                if nxt in ("\n", "\r"):
                    self.i = skip_newline(src, self.i + 1, self.end)
                    continue
                if nxt == "" or (heredoc_mode and nxt not in "$`\\"):
                    self._append("\\", self.i)
                    self.i += 1
                    continue
                self._append(nxt, self.i, escaped=True)
                self.i += 2
                continue
            if c == "'" and not heredoc_mode:
                self._flush()
                self.i = self._scan_single(self.i)
                continue
            if c == '"' and not heredoc_mode:
                self._flush()
                self.i = self._scan_double(self.i)
                continue
            if c == "$":
                self._flush()
                if not self._scan_dollar(self.i, in_double=heredoc_mode):
                    self._append("$", self.i)
                    self.i += 1
                continue
            if c == "`":
                self._flush()
                self.i = self._scan_backquote(self.i)
                continue
            at_word_start = not self.parts and not self._buf
            if not heredoc_mode and at_word_start and c in "<>" and src[self.i + 1 : self.i + 2] == "(":
                self._flush()
                self.i = self._scan_proc_sub(self.i)
                continue
            if not heredoc_mode and at_word_start and c == "~":
                self._scan_tilde(self.i)
                continue
            if not heredoc_mode and c in _WORD_BREAK:
                break
            self._append(c, self.i)
            self.i += 1
        self._flush()
        return Word(self.index.pos(self.start, self.i), self.parts, src[self.start : self.i])

    # -- 引号与替换 -------------------------------------------------------
    def _scan_single(self, i: int) -> int:
        j = self.source.find("'", i + 1, self.end)
        if j < 0:
            raise self.index.error(i, "未闭合的单引号")
        raw = self.source[i : j + 1]
        self.parts.append(SingleQuoted(self.index.pos(i, j + 1), self.source[i + 1 : j], raw))
        return j + 1

    def _scan_double(self, i: int, dollar_prefix: bool = False) -> int:
        self._guard(i)
        self.nest += 1
        try:
            src = self.source
            begin = i - 1 if dollar_prefix else i
            i += 1
            buf: list[str] = []
            start = i
            parts: list[WordPart] = []
            while i < self.end:
                c = src[i]
                if c == '"':
                    if buf:
                        parts.append(Literal(self.index.pos(start, i), "".join(buf), src[start:i]))
                    quoted = DoubleQuoted(self.index.pos(begin, i + 1), src[begin : i + 1], parts)
                    self.parts.append(quoted)
                    return i + 1
                if c == "\\":
                    nxt = src[i + 1] if i + 1 < self.end else ""
                    if nxt in ("\n", "\r"):
                        i = skip_newline(src, i + 1, self.end)
                        continue
                    if nxt in ("$", "`", '"', "\\"):
                        if buf:
                            parts.append(Literal(self.index.pos(start, i), "".join(buf), src[start:i]))
                        buf = [nxt]
                        start = i
                        i += 2
                        continue
                    buf.append("\\")
                    i += 1
                    continue
                if c in "$`":
                    if buf:
                        parts.append(Literal(self.index.pos(start, i), "".join(buf), src[start:i]))
                        buf = []
                    saved = self.parts
                    self.parts = parts
                    try:
                        if c == "$":
                            if self._scan_dollar(i, in_double=True):
                                i = self.i
                            else:
                                self.parts.append(Literal(self.index.pos(i, i + 1), "$", "$"))
                                i += 1
                        else:
                            i = self._scan_backquote(i)
                    finally:
                        parts = self.parts
                        self.parts = saved
                    start = i
                    continue
                buf.append(c)
                i += 1
            raise self.index.error(begin, "未闭合的双引号")
        finally:
            self.nest -= 1

    def _scan_backquote(self, i: int) -> int:
        self._guard(i)
        src = self.source
        j = i + 1
        while j < self.end:
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == "`":
                raw = src[i : j + 1]
                self.parts.append(CommandSubstitution(self.index.pos(i, j + 1), raw, i + 1, j, True))
                return j + 1
            j += 1
        raise self.index.error(i, "未闭合的反引号命令替换")

    def _scan_tilde(self, i: int) -> None:
        src = self.source
        j = i + 1
        while j < self.end and src[j] not in _WORD_BREAK and src[j] not in "/:":
            j += 1
        self.parts.append(Tilde(self.index.pos(i, j), src[i:j], src[i + 1 : j]))
        self.i = j
        self._buf_start = j

    def _scan_proc_sub(self, i: int) -> int:
        direction = self.source[i]
        j = self._scan_paren_group(i + 1)
        raw = self.source[i:j]
        self.parts.append(ProcSubstitution(self.index.pos(i, j), raw, direction, i + 2, j - 1))
        return j

    # -- $ 展开 -----------------------------------------------------------
    def _scan_dollar(self, i: int, in_double: bool = False) -> bool:
        src = self.source
        end = self.end
        nxt = src[i + 1] if i + 1 < end else ""
        if nxt == "(":
            if i + 2 < end and src[i + 2] == "(":
                expr, j = scan_arith(src, i + 1, self.index, self.max_depth)
                self.parts.append(ArithExpansion(self.index.pos(i, j), src[i:j], expr))
            else:
                j = self._scan_paren_group(i + 1)
                raw = src[i:j]
                self.parts.append(CommandSubstitution(self.index.pos(i, j), raw, i + 2, j - 1, False))
            self.i = j
            return True
        if nxt == "{":
            j = self._scan_braced(i + 1)
            self._append_param(i, j)
            self.i = j
            return True
        if nxt == "'" and not in_double:
            j = i + 2
            while j < end:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "'":
                    break
                j += 1
            if j >= end:
                raise self.index.error(i, "未闭合的 $' 引用")
            raw = src[i : j + 1]
            self.parts.append(AnsiCQuoted(self.index.pos(i, j + 1), decode_ansi_c(src[i + 2 : j]), raw))
            self.i = j + 1
            return True
        if nxt == '"' and not in_double:
            self.i = self._scan_double(i + 1, dollar_prefix=True)
            return True
        if nxt and (nxt.isalpha() or nxt == "_"):
            j = i + 1
            while j < end and (src[j].isalnum() or src[j] == "_"):
                j += 1
            self.parts.append(ParamExpansion(self.index.pos(i, j), src[i:j], src[i + 1 : j], "", False))
            self.i = j
            return True
        if nxt in _SPECIAL_PARAMS:
            raw = src[i : i + 2]
            self.parts.append(ParamExpansion(self.index.pos(i, i + 2), raw, nxt, "", False))
            self.i = i + 2
            return True
        return False

    def _append_param(self, i: int, j: int) -> None:
        self._guard(i)
        src = self.source
        inner_start, inner_end = i + 2, j - 1
        k = inner_start
        prefix = ""
        if k < inner_end and src[k] in "!#" and not (src[k] == "#" and k + 1 == inner_end):
            prefix = src[k]
            k += 1
        n = k
        while n < inner_end and (src[n].isalnum() or src[n] == "_"):
            n += 1
        if n == k and k < inner_end:
            n = k + 1
        rest = src[n:inner_end]
        op, operand = split_param_op(rest)
        parts: list[WordPart] = []
        if operand:
            sub = WordScanner(
                src, n + len(op), inner_end, self.index, self.max_depth, self.nest + 1
            )
            parts = sub.scan().parts
        name = prefix + src[k:n]
        self.parts.append(
            ParamExpansion(self.index.pos(i, j), src[i:j], name, op, True, operand, parts)
        )

    # -- 括号配对（迭代，含嵌套） -----------------------------------------
    def _scan_braced(self, i: int) -> int:
        """``i`` 指向 ``${`` 的 ``{``；返回 ``}`` 之后的位置。"""
        self._guard(i)
        self.nest += 1
        try:
            src = self.source
            end = self.end
            depth = 1
            j = i + 1
            while j < end:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "'":
                    k = src.find("'", j + 1, end)
                    j = end if k < 0 else k + 1
                    continue
                if c == '"':
                    j = self._skip_quoted_region(j)
                    continue
                if c == "`":
                    j = self._skip_backquote_region(j)
                    continue
                if c == "$" and j + 1 < end and src[j + 1] == "{":
                    depth += 1
                    j += 2
                    continue
                if c == "$" and j + 1 < end and src[j + 1] == "(":
                    j = self._scan_paren_group(j + 1)
                    continue
                if c == "}":
                    depth -= 1
                    if depth == 0:
                        return j + 1
                j += 1
            raise self.index.error(i - 1, "未闭合的 ${ 参数展开")
        finally:
            self.nest -= 1

    def _skip_quoted_region(self, i: int) -> int:
        src = self.source
        end = self.end
        j = i + 1
        while j < end:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
            if c == "$" and j + 1 < end and src[j + 1] in "{(":
                j = self._scan_braced(j + 1) if src[j + 1] == "{" else self._scan_paren_group(j + 1)
                continue
            if c == "`":
                j = self._skip_backquote_region(j)
                continue
            j += 1
        raise self.index.error(i, "未闭合的双引号")

    def _skip_backquote_region(self, i: int) -> int:
        src = self.source
        j = i + 1
        while j < self.end:
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == "`":
                return j + 1
            j += 1
        raise self.index.error(i, "未闭合的反引号命令替换")

    def _scan_paren_group(self, open_at: int) -> int:
        """``open_at`` 指向 ``(``；返回配对的 ``)`` 之后的位置。"""
        self._guard(open_at)
        self.nest += 1
        try:
            src = self.source
            end = self.end
            depth = 0
            j = open_at
            while j < end:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "'":
                    k = src.find("'", j + 1, end)
                    j = end if k < 0 else k + 1
                    continue
                if c == '"':
                    j = self._skip_quoted_region(j)
                    continue
                if c == "`":
                    j = self._skip_backquote_region(j)
                    continue
                if c == "$" and j + 1 < end and src[j + 1] == "{":
                    j = self._scan_braced(j + 1)
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return j + 1
                j += 1
            raise self.index.error(open_at, "未闭合的 ( ... )")
        finally:
            self.nest -= 1
