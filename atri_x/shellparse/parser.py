"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：递归下降语法分析。

对应 Go：``mvdan.cc/sh/v3/syntax`` 的 ``Parse`` 前端（``parser.go`` 的
``p.stmt``/``p.callExpr``/``p.wordPart`` 调用顺序）。本文件是独立实现，不含
``bashlex``（GPLv3+）的任何代码。

拆分来源（PORTING §2：单文件 ≤ 600 行）：

* :mod:`atri_x.shellparse.clauses` —— 复合命令（if/for/while/case/块/函数/``[[``/``((``）
  以 :class:`ClauseParserMixin` 合成到本模块的 :class:`Parser`；
* :mod:`atri_x.shellparse.scanner` —— 字符级单词/展开扫描；
* :mod:`atri_x.shellparse.lexer` —— 记号流与 here-document。

入口：

* :func:`parse` —— ``parse(source) -> Script``，失败抛 :class:`ShellParseError`；
* :func:`parse_range` —— 内部使用（命令替换正文等子范围）。

硬约束：空输入、纯注释、CRLF、UTF-8（含 CJK）都有确定行为；源码含 ``\\0``
直接拒绝；嵌套深度超过 ``max_depth``（默认 :data:`MAX_NESTING_DEPTH`）抛
``ShellParseError``，不会 RecursionError。
"""

from __future__ import annotations

import re

from .ast import (
    Assignment,
    CommandSubstitution,
    DoubleQuoted,
    Heredoc,
    List,
    Node,
    ParamExpansion,
    Pipeline,
    ProcSubstitution,
    Redirect,
    Script,
    ShellParseError,
    SimpleCommand,
    TimeClause,
    Word,
    WordPart,
)
from .clauses import ClauseParserMixin
from .lexer import (
    KIND_EOF,
    KIND_IO_NUMBER,
    KIND_NEWLINE,
    KIND_OP,
    KIND_WORD,
    MAX_NESTING_DEPTH,
    REDIRECT_OPS,
    Lexer,
    SourceIndex,
    Token,
    scan_word_range,
)

__all__ = ["MAX_NESTING_DEPTH", "Parser", "ShellParseError", "parse", "parse_range"]

#: ``name=value`` 形式的赋值词（含 ``a+=``/``a[i]=``）。
_ASSIGN_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\[(?P<index>[^\]]*)\])?(?P<append>\+)?="
)


def parse(source: str, *, max_depth: int = MAX_NESTING_DEPTH) -> Script:
    """解析 shell 源码；失败抛 :class:`ShellParseError`（带行列与错误片段）。"""
    if not isinstance(source, str):
        raise TypeError(f"source 必须是 str，收到 {type(source).__name__}")
    index = SourceIndex(source)
    nul = source.find("\0")
    if nul >= 0:
        raise index.error(nul, "shell 源码包含 NUL 字节，拒绝解析")
    return parse_range(source, 0, len(source), index, 0, max_depth)


def parse_range(
    source: str,
    start: int,
    end: int,
    index: SourceIndex,
    depth: int = 0,
    max_depth: int = MAX_NESTING_DEPTH,
) -> Script:
    """解析 ``source[start:end]``（命令替换/进程替换正文用）。"""
    lexer = Lexer(source, start, end, index, max_depth)
    tokens, comments = lexer.tokenize()
    return Parser(source, tokens, comments, index, depth, max_depth).parse_script()


class Parser(ClauseParserMixin):
    """显式递归下降解析器（不用 ``eval``/``exec``，不用正则代替语法）。"""

    def __init__(
        self,
        source: str,
        tokens: list[Token],
        comments: list,
        index: SourceIndex,
        depth: int = 0,
        max_depth: int = MAX_NESTING_DEPTH,
    ) -> None:
        self.source = source
        self.tokens = tokens
        self.comments = comments
        self.index = index
        self.i = 0
        self.depth = depth
        self.max_depth = max_depth

    # -- 记号辅助 ---------------------------------------------------------
    def _peek(self, k: int = 0) -> Token:
        at = self.i + k
        return self.tokens[-1] if at >= len(self.tokens) else self.tokens[at]

    def _advance(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != KIND_EOF:
            self.i += 1
        return tok

    def _at_eof(self) -> bool:
        return self._peek().kind == KIND_EOF

    def _at_op(self, *ops: str) -> bool:
        tok = self._peek()
        return tok.kind == KIND_OP and tok.op in ops

    def _at_word(self, text: str) -> bool:
        tok = self._peek()
        return tok.kind == KIND_WORD and tok.word is not None and tok.word.bare_text() == text

    def _skip_newlines(self) -> None:
        while self._peek().kind == KIND_NEWLINE:
            self._advance()

    def _skip_separator(self) -> None:
        if self._at_op(";"):
            self._advance()
        self._skip_newlines()

    def _expect_word(self, text: str, message: str) -> Token:
        if not self._at_word(text):
            raise self._error(message)
        return self._advance()

    def _expect_op(self, op: str, message: str) -> Token:
        if not self._at_op(op):
            raise self._error(message)
        return self._advance()

    def _error(self, message: str, tok: Token | None = None) -> ShellParseError:
        return self.index.error((tok if tok is not None else self._peek()).pos.start, message)

    def _enter(self, offset: int) -> None:
        self.depth += 1
        if self.depth > self.max_depth:
            raise self.index.error(offset, f"shell 嵌套层级过深（上限 {self.max_depth}）")

    def _leave(self) -> None:
        self.depth -= 1

    def _consume_through(self, offset: int) -> None:
        while self._peek().kind != KIND_EOF and self._peek().pos.start < offset:
            self._advance()

    def _can_start_command(self) -> bool:
        tok = self._peek()
        if tok.kind in (KIND_WORD, KIND_IO_NUMBER):
            return True
        if tok.kind == KIND_OP:
            return tok.op == "(" or tok.op in REDIRECT_OPS
        return False

    def _at_stop(self, stop_words: frozenset[str], stop_ops: frozenset[str]) -> bool:
        tok = self._peek()
        if tok.kind in (KIND_EOF, KIND_NEWLINE):
            return True
        if tok.kind == KIND_OP and tok.op in stop_ops:
            return True
        if tok.kind == KIND_WORD and tok.word is not None:
            return tok.word.bare_text() in stop_words
        return False

    # -- 顶层与列表 -------------------------------------------------------
    def parse_script(self) -> Script:
        body: list[List] = []
        while True:
            self._skip_newlines()
            if self._at_eof():
                break
            body.append(self._parse_list(frozenset(), frozenset()))
        start = self.tokens[0].pos.start if self.tokens else 0
        end = self.tokens[-1].pos.end if self.tokens else start
        return Script(self.index.pos(start, end), body, list(self.comments))

    def _parse_body(self, stop_words: frozenset[str], stop_ops: frozenset[str]) -> list[List]:
        out: list[List] = []
        while True:
            self._skip_newlines()
            if self._at_eof() or self._at_stop(stop_words, stop_ops):
                return out
            out.append(self._parse_list(stop_words, stop_ops))

    def _parse_list(self, stop_words: frozenset[str], stop_ops: frozenset[str]) -> List:
        items: list[Node] = []
        seps: list[str] = []
        start = self._peek().pos.start
        end = start
        while True:
            node = self._parse_pipeline(stop_words, stop_ops)
            items.append(node)
            end = node.pos.end
            if self._at_stop(stop_words, stop_ops):
                seps.append("")
                break
            tok = self._peek()
            if tok.kind != KIND_OP or tok.op not in (";", "&", "&&", "||"):
                raise self._error("意外的记号", tok)
            seps.append(tok.op)
            self._advance()
            if tok.op in ("&&", "||"):
                self._skip_newlines()
                if self._at_stop(stop_words, stop_ops) or not self._can_start_command():
                    raise self._error(f"'{tok.op}' 之后缺少命令", tok)
            elif self._at_stop(stop_words, stop_ops) or not self._can_start_command():
                break
        return List(self.index.pos(start, end), items, seps)

    def _parse_pipeline(self, stop_words: frozenset[str], stop_ops: frozenset[str]) -> Node:
        start = self._peek().pos.start
        if not self._can_start_command():
            raise self._error("此处需要一个命令")
        negated = False
        while self._at_word("!"):
            self._advance()
            negated = not negated
            if not self._can_start_command():
                raise self._error("'!' 之后缺少命令")
        timed = self._at_word("time")
        posix_time = False
        if timed:
            self._advance()
            if self._at_word("-p"):
                posix_time = True
                self._advance()
            if not self._can_start_command():
                raise self._error("'time' 之后缺少命令")
        commands: list[Node] = [self._parse_command()]
        ops: list[str] = []
        while self._at_op("|", "|&"):
            ops.append(self._advance().op)
            self._skip_newlines()
            if not self._can_start_command():
                raise self._error("管道符之后缺少命令")
            commands.append(self._parse_command())
        end = commands[-1].pos.end
        if len(commands) == 1 and not negated and not timed:
            return commands[0]
        node: Node = Pipeline(self.index.pos(start, end), commands, ops, negated)
        if timed:
            node = TimeClause(self.index.pos(start, end), node, posix_time)
        return node

    # -- 简单命令 ---------------------------------------------------------
    def _parse_simple_command(self) -> SimpleCommand:
        assignments: list[Assignment] = []
        words: list[Word] = []
        redirects: list[Redirect] = []
        start = self._peek().pos.start
        end = start
        while True:
            tok = self._peek()
            if (
                tok.kind == KIND_IO_NUMBER
                and self._peek(1).kind == KIND_OP
                and self._peek(1).op in REDIRECT_OPS
            ):
                fd = self._advance()
                redirects.append(self._parse_redirect(fd.text, fd.pos.start))
                end = redirects[-1].pos.end
                continue
            if tok.kind == KIND_OP and tok.op in REDIRECT_OPS:
                redirects.append(self._parse_redirect(None, tok.pos.start))
                end = redirects[-1].pos.end
                continue
            if tok.kind == KIND_WORD and tok.word is not None:
                word = self._resolve_word(tok.word)
                self._advance()
                end = word.pos.end
                match = _ASSIGN_RE.match(word.raw) if not words else None
                if match is not None:
                    assignments.append(self._make_assignment(word, match))
                else:
                    words.append(word)
                continue
            break
        if not assignments and not words and not redirects:
            raise self._error("此处需要一个命令")
        return SimpleCommand(self.index.pos(start, end), assignments, words, redirects)

    def _make_assignment(self, word: Word, match: re.Match[str]) -> Assignment:
        value_start = word.pos.start + match.end()
        value: Word | None = None
        if value_start < word.pos.end:
            value = self._resolve_word(
                scan_word_range(self.source, value_start, word.pos.end, self.index, self.max_depth)
            )
        return Assignment(
            word.pos, match.group("name"), value, match.group("index"), bool(match.group("append"))
        )

    def _parse_redirect(self, fd: str | None, start_offset: int) -> Redirect:
        op_tok = self._advance()
        op = op_tok.op
        word: Word | None = None
        hdoc: Heredoc | None = None
        if op in ("<<", "<<-"):
            delim = op_tok.heredoc_delim
            if delim is None:
                raise self._error(f"'{op}' 缺少 here-document 界定符", op_tok)
            if op_tok.heredoc is None:
                raise self._error("here-document 正文缺失", op_tok)
            if self._peek().is_heredoc_delim:
                self._advance()
            word = self._resolve_word(delim)
            hdoc = op_tok.heredoc
            for part in hdoc.parts:
                self._resolve_part(part)
            end = hdoc.pos.end
        else:
            tok = self._peek()
            if tok.kind != KIND_WORD or tok.word is None:
                raise self._error(f"重定向 '{op}' 缺少目标", tok)
            self._advance()
            word = self._resolve_word(tok.word)
            end = word.pos.end
        return Redirect(self.index.pos(start_offset, end), op, word, fd, hdoc)

    # -- 展开正文 ---------------------------------------------------------
    def _resolve_word(self, word: Word) -> Word:
        for part in word.parts:
            self._resolve_part(part)
        return word

    def _resolve_part(self, part: WordPart) -> None:
        if isinstance(part, (CommandSubstitution, ProcSubstitution)):
            if part.script is None:
                self._enter(part.pos.start)
                try:
                    part.script = parse_range(
                        self.source,
                        part.body_start,
                        part.body_end,
                        self.index,
                        self.depth,
                        self.max_depth,
                    )
                finally:
                    self._leave()
        elif isinstance(part, DoubleQuoted):
            for inner in part.parts:
                self._resolve_part(inner)
        elif isinstance(part, ParamExpansion):
            for inner in part.operand_parts:
                self._resolve_part(inner)
