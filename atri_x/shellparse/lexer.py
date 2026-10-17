"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：记号流与 here-document 装配。

对应 Go：``mvdan.cc/sh/v3/syntax`` 的 ``parser.go`` 里 ``Lexer``/``p.next``
与 ``p.doHeredocs``。本文件是**独立实现**，不含 ``bashlex``（GPLv3+）的任何代码。

拆分来源（PORTING §2：单文件 ≤ 600 行）：单词/展开的字符级扫描在
:mod:`atri_x.shellparse.scanner`（``SourceIndex``/``WordScanner``/``scan_arith``），
本模块负责运算符表、记号流与 ``<<``/``<<-`` 正文。

覆盖（够安全裁决用，不是完整 POSIX shell）：

* 运算符：``|`` ``|&`` ``||`` ``&&`` ``;`` ``&`` ``;;`` ``;&`` ``;;&`` ``(`` ``)``
  与全部重定向（``>`` ``>>`` ``<`` ``<<`` ``<<-`` ``<<<`` ``>&`` ``<&`` ``&>``
  ``&>>`` ``<>`` ``>|``，含 ``n>file`` 的 IO 号）；
* 词：单引号、双引号、``$'..'``、``$".."``、反斜杠转义、续行，以及
  ``$VAR``/``${VAR...}``/``$(...)``/反引号/``$((...))``/``<( )``/``>( )``/``~``
  的结构识别；
* here-document：``<<``/``<<-``、引号界定符（不展开）与未引号界定符
  （正文仍识别 ``$``/反引号展开，对齐 Go 的 ``Redirect.Hdoc``）；
* 注释、CRLF、UTF-8。

确定性行为（见 tests）：空/纯注释/纯换行 ⇒ 无语句；``\\r\\n`` 与 ``\\r`` 都算一个
换行；嵌套超过 ``MAX_NESTING_DEPTH`` 抛 ``ShellParseError``（不会 RecursionError）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ast import Comment, Heredoc, Literal, Pos, Word, WordPart
from .scanner import (
    MAX_NESTING_DEPTH,
    SourceIndex,
    WordScanner,
    scan_arith,
    scan_word_range,
    skip_newline,
)

__all__ = [
    "KIND_EOF",
    "KIND_IO_NUMBER",
    "KIND_NEWLINE",
    "KIND_OP",
    "KIND_WORD",
    "MAX_NESTING_DEPTH",
    "Lexer",
    "REDIRECT_OPS",
    "SourceIndex",
    "Token",
    "scan_arith",
    "scan_word_range",
]

KIND_WORD = "WORD"
KIND_OP = "OP"
KIND_IO_NUMBER = "IO_NUMBER"
KIND_NEWLINE = "NEWLINE"
KIND_EOF = "EOF"

REDIRECT_OPS = frozenset(
    {">", ">>", "<", "<<", "<<-", "<<<", ">&", "<&", "&>", "&>>", "<>", ">|"}
)

_OPERATORS = tuple(
    sorted(
        (";;&", "&>>", "<<<", "<<-", "&&", "||", "|&", ";;", ";&", ">>", "<<", ">&",
         "<&", "&>", "<>", ">|", "|", "&", ";", "(", ")", "<", ">"),
        key=len,
        reverse=True,
    )
)


@dataclass(slots=True)
class Token:
    """词法记号。``word`` 仅 ``WORD`` 有；heredoc 的界定符/正文挂在 ``<<`` 记号上。"""

    kind: str
    text: str
    pos: Pos
    op: str = ""
    word: Word | None = None
    heredoc_delim: Word | None = None
    heredoc: Heredoc | None = None
    is_heredoc_delim: bool = False


@dataclass(slots=True)
class _PendingHeredoc:
    token_index: int
    op: str
    delimiter_word: Word
    quoted: bool
    strip_tabs: bool


class Lexer:
    """把一段源码（默认整串，也可指定范围）切成记号表。"""

    def __init__(
        self,
        source: str,
        start: int = 0,
        end: int | None = None,
        index: SourceIndex | None = None,
        max_depth: int = MAX_NESTING_DEPTH,
    ) -> None:
        self.source = source
        self.start = start
        self.end = len(source) if end is None else end
        self.index = index if index is not None else SourceIndex(source)
        self.max_depth = max_depth
        self.i = start
        self.tokens: list[Token] = []
        self.comments: list[Comment] = []
        self._pending: list[_PendingHeredoc] = []
        self._await_delim: int | None = None

    def tokenize(self) -> tuple[list[Token], list[Comment]]:
        while True:
            tok = self._next_token()
            self.tokens.append(tok)
            if tok.kind == KIND_EOF:
                break
        return self.tokens, self.comments

    # -- 主循环 -----------------------------------------------------------
    def _peek_char(self, offset: int) -> str:
        at = self.i + offset
        return self.source[at] if at < self.end else ""

    def _next_token(self) -> Token:
        src = self.source
        index = self.index
        while True:
            if self.i >= self.end:
                self._await_delim = None
                self._flush_heredocs()
                return Token(KIND_EOF, "", index.pos(self.end, self.end))
            c = src[self.i]
            if c in " \t":
                self.i += 1
                continue
            if c in "\n\r" or (c == "\\" and self._peek_char(1) in "\n\r"):
                if c == "\\":
                    self.i = skip_newline(src, self.i + 1, self.end)
                    continue
                start = self.i
                self.i = skip_newline(src, self.i, self.end)
                self._await_delim = None
                self._flush_heredocs()
                return Token(KIND_NEWLINE, src[start : self.i], index.pos(start, self.i))
            if c == "#":
                start = self.i
                j = self.i
                while j < self.end and src[j] not in "\r\n":
                    j += 1
                self.comments.append(Comment(index.pos(start, j), src[start:j]))
                self.i = j
                continue
            if c in "0123456789":
                j = self.i
                while j < self.end and src[j] in "0123456789":
                    j += 1
                if j < self.end and src[j] in "<>":
                    tok = Token(KIND_IO_NUMBER, src[self.i : j], index.pos(self.i, j))
                    self.i = j
                    return tok
            if not (c in "<>" and self._peek_char(1) == "("):
                op = self._match_operator()
                if op:
                    start = self.i
                    self.i += len(op)
                    tok = Token(KIND_OP, op, index.pos(start, self.i), op=op)
                    if op in ("<<", "<<-"):
                        self._await_delim = len(self.tokens)
                    return tok
            return self._scan_word_token()

    def _match_operator(self) -> str:
        for op in _OPERATORS:
            if self.source.startswith(op, self.i):
                return op
        return ""

    def _scan_word_token(self) -> Token:
        scanner = WordScanner(self.source, self.i, self.end, self.index, self.max_depth)
        word = scanner.scan()
        self.i = scanner.i
        tok = Token(KIND_WORD, word.raw, word.pos, word=word)
        if self._await_delim is not None:
            self._attach_heredoc(tok.word)
            tok.is_heredoc_delim = True
            self._await_delim = None
        return tok

    # -- here-document ----------------------------------------------------
    def _attach_heredoc(self, word: Word | None) -> None:
        if word is None or self._await_delim is None:
            return
        token_index = self._await_delim
        op = self.tokens[token_index].op
        quoted = any(not (isinstance(p, Literal) and not p.quoted) for p in word.parts)
        self.tokens[token_index].heredoc_delim = word
        self._pending.append(_PendingHeredoc(token_index, op, word, quoted, op == "<<-"))

    def _flush_heredocs(self) -> None:
        pending, self._pending = self._pending, []
        for item in pending:
            body_start = self.i
            lines: list[str] = []
            body_end = body_start
            while True:
                if self.i >= self.end:
                    raise self.index.error(
                        body_start, f"here-document 未以界定符 {item.delimiter_word.raw} 结束"
                    )
                line_start = self.i
                j = self.i
                while j < self.end and self.source[j] not in "\r\n":
                    j += 1
                content = self.source[line_start:j]
                self.i = skip_newline(self.source, j, self.end)
                compare = content.lstrip("\t") if item.strip_tabs else content
                if compare == (item.delimiter_word.literal() or item.delimiter_word.raw):
                    body_end = line_start
                    break
                lines.append(content.lstrip("\t") if item.strip_tabs else content)
            body = "".join(line + "\n" for line in lines)
            parts: list[WordPart] = []
            if not item.quoted and lines:
                parts = WordScanner(
                    self.source, body_start, body_end, self.index, self.max_depth, mode="heredoc"
                ).scan().parts
            self.tokens[item.token_index].heredoc = Heredoc(
                self.index.pos(item.delimiter_word.pos.end, self.i),
                item.op,
                item.delimiter_word.literal() or item.delimiter_word.raw,
                item.delimiter_word.raw,
                body,
                parts,
                item.quoted,
                item.strip_tabs,
            )
