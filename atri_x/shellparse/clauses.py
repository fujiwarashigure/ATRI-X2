"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：复合命令语法。

对应 Go：``mvdan.cc/sh/v3/syntax`` 的 ``parser.go`` 里 ``p.gotStmt``/
``p.ifClause``/``p.loopClause``/``p.caseClause``/``p.funcDecl`` 一族。

拆分来源（PORTING §2：单文件 ≤ 600 行）：:mod:`atri_x.shellparse.parser` 负责
记号级辅助、``List``/``Pipeline``/简单命令与展开正文，本模块以 mixin 形式提供
``if``/``for``/``while``/``until``/``case``/``{}``/``()``/函数/``[[ ]]``/``(( ))``
的递归下降（最终装配成同一个 ``Parser`` 类，方法仍访问同一份 ``self`` 状态）。

有意简化（在 tests 与汇报里逐条列出）：不支持 ``select``、数组赋值
``a=(1 2)``、``coproc``；``time`` 走 :class:`TimeClause`（对齐 Go）；``[[ ]]``
按 :class:`TestClause` 收集条件词；``((...))`` 按 :class:`ArithCommand`。
"""

from __future__ import annotations

import re

from .ast import (
    ArithCommand,
    Block,
    CaseClause,
    CaseItem,
    CompoundCommand,
    ForClause,
    FunctionDecl,
    IfClause,
    List,
    Node,
    Subshell,
    TestClause,
    WhileClause,
    Word,
)
from .lexer import KIND_EOF, KIND_IO_NUMBER, KIND_NEWLINE, KIND_OP, KIND_WORD, REDIRECT_OPS
from .scanner import scan_arith

__all__ = ["ClauseParserMixin"]

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: 出现在语句首但不是合法语句的保留字（Go/mvdan 也报解析失败）。
_RESERVED_REJECT = frozenset({"then", "do", "done", "fi", "elif", "else", "esac", "in"})

_STOP_THEN = frozenset({"then"})
_STOP_DO = frozenset({"do"})
_STOP_DONE = frozenset({"done"})
_STOP_FI = frozenset({"fi"})
_STOP_FI_ELIF_ELSE = frozenset({"fi", "elif", "else"})
_STOP_ESAC = frozenset({"esac"})
_STOP_RBRACE = frozenset({"}"})
_CASE_TERMINATORS = frozenset({";;", ";&", ";;&"})


class ClauseParserMixin:
    """复合命令的递归下降实现（与 :class:`~atri_x.shellparse.parser.Parser` 合成）。"""

    # -- 单条命令 ---------------------------------------------------------
    def _parse_command(self) -> Node:
        node = self._parse_command_inner()
        if isinstance(node, CompoundCommand):
            self._parse_trailing_redirects(node)
        return node

    def _parse_trailing_redirects(self, node: CompoundCommand) -> None:
        """复合命令尾部的重定向（Go：语句级 ``Stmt.Redirs``）。"""
        while True:
            tok = self._peek()
            if (
                tok.kind == KIND_IO_NUMBER
                and self._peek(1).kind == KIND_OP
                and self._peek(1).op in REDIRECT_OPS
            ):
                self._advance()
                node.redirects.append(self._parse_redirect(tok.text, tok.pos.start))
                continue
            if tok.kind == KIND_OP and tok.op in REDIRECT_OPS:
                node.redirects.append(self._parse_redirect(None, tok.pos.start))
                continue
            break

    def _parse_command_inner(self) -> Node:
        tok = self._peek()
        if tok.kind == KIND_OP and tok.op == "(":
            nxt = self._peek(1)
            if nxt.kind == KIND_OP and nxt.op == "(" and nxt.pos.start == tok.pos.end:
                return self._parse_arith_command()
            return self._parse_subshell()
        if tok.kind == KIND_WORD:
            word = tok.word
            bare = word.bare_text() if word is not None else None
            if bare == "[[":
                return self._parse_test_clause()
            if bare == "{":
                return self._parse_block()
            if bare == "}":
                raise self._error("意外的 '}'（没有与之配对的 '{'）", tok)
            if bare == "if":
                return self._parse_if(True)
            if bare == "for":
                return self._parse_for()
            if bare == "while":
                return self._parse_while(False)
            if bare == "until":
                return self._parse_while(True)
            if bare == "case":
                return self._parse_case()
            if bare == "function":
                return self._parse_function(True)
            if bare == "select":
                raise self._error("select 语句暂不支持（有意简化）", tok)
            if bare in _RESERVED_REJECT:
                raise self._error(f"意外的保留字 '{bare}'", tok)
            if word is not None and _NAME_RE.match(word.literal() or ""):
                nxt, nxt2 = self._peek(1), self._peek(2)
                if nxt.kind == KIND_OP and nxt.op == "(" and nxt2.kind == KIND_OP and nxt2.op == ")":
                    return self._parse_function(False)
            return self._parse_simple_command()
        if tok.kind == KIND_IO_NUMBER or (tok.kind == KIND_OP and tok.op in REDIRECT_OPS):
            return self._parse_simple_command()
        raise self._error("此处需要一个命令", tok)

    # -- 复合命令 ---------------------------------------------------------
    def _parse_subshell(self) -> Subshell:
        open_tok = self._advance()
        self._enter(open_tok.pos.start)
        try:
            body = self._parse_body(frozenset(), frozenset({")"}))
            close = self._expect_op(")", "子 shell 未以 ')' 结束")
        finally:
            self._leave()
        return Subshell(self.index.pos(open_tok.pos.start, close.pos.end), body)

    def _parse_block(self) -> Block:
        open_tok = self._advance()
        self._enter(open_tok.pos.start)
        try:
            body = self._parse_body(_STOP_RBRACE, frozenset())
            close = self._expect_word("}", "语句块未以 '}' 结束")
        finally:
            self._leave()
        return Block(self.index.pos(open_tok.pos.start, close.pos.end), body)

    def _parse_if(self, first: bool) -> IfClause:
        kw = self._advance()
        self._enter(kw.pos.start)
        try:
            condition = self._parse_body(_STOP_THEN, frozenset())
            self._expect_word("then", "'if' 条件之后缺少 'then'")
            then_body = self._parse_body(_STOP_FI_ELIF_ELSE, frozenset())
            else_body: list[List] | None = None
            elif_clause: IfClause | None = None
            if self._at_word("elif"):
                # elif 分支自己吃掉共用的 fi，因此外层不再期待第二个 fi。
                elif_clause = self._parse_if(False)
                end_offset = elif_clause.pos.end
            else:
                if self._at_word("else"):
                    self._advance()
                    else_body = self._parse_body(_STOP_FI, frozenset())
                end_offset = self._expect_word("fi", "'if' 未以 'fi' 结束").pos.end
        finally:
            self._leave()
        return IfClause(
            self.index.pos(kw.pos.start, end_offset), condition, then_body, else_body, elif_clause
        )

    def _parse_while(self, until: bool) -> WhileClause:
        kw = self._advance()
        self._enter(kw.pos.start)
        try:
            condition = self._parse_body(_STOP_DO, frozenset())
            self._expect_word("do", "循环条件之后缺少 'do'")
            body = self._parse_body(_STOP_DONE, frozenset())
            end_tok = self._expect_word("done", "循环未以 'done' 结束")
        finally:
            self._leave()
        return WhileClause(self.index.pos(kw.pos.start, end_tok.pos.end), condition, body, until)

    def _parse_for(self) -> ForClause:
        kw = self._advance()
        self._enter(kw.pos.start)
        try:
            tok, nxt = self._peek(), self._peek(1)
            if (
                tok.kind == KIND_OP
                and tok.op == "("
                and nxt.kind == KIND_OP
                and nxt.op == "("
                and nxt.pos.start == tok.pos.end
            ):
                expr, end_off = scan_arith(self.source, tok.pos.start, self.index, self.max_depth)
                self._consume_through(end_off)
                self._skip_separator()
                self._expect_word("do", "'for' 之后缺少 'do'")
                body = self._parse_body(_STOP_DONE, frozenset())
                end_tok = self._expect_word("done", "'for' 未以 'done' 结束")
                return ForClause(
                    self.index.pos(kw.pos.start, end_tok.pos.end), "", None, body, expr
                )
            name = tok.word.literal() if tok.kind == KIND_WORD and tok.word is not None else None
            if name is None or not _NAME_RE.match(name):
                raise self._error("'for' 缺少循环变量名", tok)
            self._advance()
            in_words: list[Word] | None = None
            if self._at_word("in"):
                self._advance()
                in_words = []
                while self._peek().kind == KIND_WORD and self._peek().word is not None:
                    in_words.append(self._resolve_word(self._advance().word))
            self._skip_separator()
            self._expect_word("do", "'for' 之后缺少 'do'")
            body = self._parse_body(_STOP_DONE, frozenset())
            end_tok = self._expect_word("done", "'for' 未以 'done' 结束")
        finally:
            self._leave()
        return ForClause(self.index.pos(kw.pos.start, end_tok.pos.end), name, in_words, body, None)

    def _parse_case(self) -> CaseClause:
        kw = self._advance()
        self._enter(kw.pos.start)
        try:
            subject_tok = self._peek()
            if subject_tok.kind != KIND_WORD or subject_tok.word is None:
                raise self._error("'case' 缺少匹配对象", subject_tok)
            self._advance()
            subject = self._resolve_word(subject_tok.word)
            self._skip_newlines()
            self._expect_word("in", "'case' 需要 'in'")
            items: list[CaseItem] = []
            while True:
                self._skip_newlines()
                if self._at_word("esac"):
                    end_tok = self._advance()
                    break
                if self._at_eof():
                    raise self._error("'case' 未以 'esac' 结束")
                item_start = self._peek().pos.start
                if self._at_op("("):
                    self._advance()
                patterns: list[Word] = []
                while True:
                    pat_tok = self._peek()
                    if pat_tok.kind != KIND_WORD or pat_tok.word is None:
                        raise self._error("'case' 分支模式必须是单词", pat_tok)
                    self._advance()
                    patterns.append(self._resolve_word(pat_tok.word))
                    if self._at_op("|"):
                        self._advance()
                        continue
                    break
                self._expect_op(")", "'case' 分支模式缺少 ')'")
                body = self._parse_body(_STOP_ESAC, _CASE_TERMINATORS)
                terminator: str | None = None
                if self._at_op(";;", ";&", ";;&"):
                    terminator = self._advance().op
                item_end = self._peek().pos.start
                items.append(
                    CaseItem(self.index.pos(item_start, item_end), patterns, body, terminator)
                )
        finally:
            self._leave()
        return CaseClause(self.index.pos(kw.pos.start, end_tok.pos.end), subject, items)

    def _parse_function(self, keyword: bool) -> FunctionDecl:
        kw = self._advance()
        if keyword:
            name_tok = self._peek()
            name = (
                name_tok.word.literal()
                if name_tok.kind == KIND_WORD and name_tok.word is not None
                else None
            )
            if name is None or not _NAME_RE.match(name):
                raise self._error("'function' 缺少合法函数名", name_tok)
            self._advance()
            if self._at_op("("):
                self._expect_op("(", "函数名后面缺少 '('")
                self._expect_op(")", "函数名后面缺少 ')'")
        else:
            name = kw.word.literal() if kw.word is not None else None
            if name is None or not _NAME_RE.match(name):
                raise self._error("非法的函数名", kw)
            self._expect_op("(", "函数定义缺少 '('")
            self._expect_op(")", "函数定义缺少 ')'")
        self._skip_newlines()
        if not self._can_start_command():
            raise self._error("函数定义缺少函数体")
        self._enter(kw.pos.start)
        try:
            body = self._parse_command()
        finally:
            self._leave()
        return FunctionDecl(self.index.pos(kw.pos.start, body.pos.end), name, body, keyword)

    def _parse_test_clause(self) -> TestClause:
        start_tok = self._advance()
        words: list[Word] = []
        while True:
            tok = self._peek()
            if tok.kind in (KIND_EOF, KIND_NEWLINE):
                raise self._error("'[[' 条件未以 ']]' 结束", start_tok)
            if tok.kind == KIND_WORD and tok.word is not None:
                if tok.word.bare_text() == "]]":
                    end_tok = self._advance()
                    break
                self._advance()
                words.append(self._resolve_word(tok.word))
                continue
            if tok.kind == KIND_OP:
                self._advance()
                continue
            raise self._error("'[[' 条件中出现意外记号", tok)
        return TestClause(
            self.index.pos(start_tok.pos.start, end_tok.pos.end),
            self.source[start_tok.pos.start : end_tok.pos.end],
            words,
        )

    def _parse_arith_command(self) -> ArithCommand:
        tok = self._peek()
        expr, end_off = scan_arith(self.source, tok.pos.start, self.index, self.max_depth)
        self._consume_through(end_off)
        return ArithCommand(
            self.index.pos(tok.pos.start, end_off), self.source[tok.pos.start:end_off], expr
        )
