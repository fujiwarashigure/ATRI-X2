"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：遍历、查询与回写。

对应 Go：

* :func:`walk` ↔ ``syntax.Walk``（前序，返回 ``False`` 剪枝）；
* :func:`words` / :func:`commands` / :func:`command_names` ↔ ``shell_risk.go``、
  ``shell_sandbox.go`` 在 ``syntax.Walk`` 回调里收集的 ``*syntax.CallExpr`` /
  ``*syntax.Word``；
* :func:`literal_args` / :func:`command_base` ↔ 同文件里的 ``literalArgs`` /
  ``commandBase``；
* :func:`has_write_redirect` ↔ ``strings.Contains(op, ">")`` 判定；
* :func:`format` ↔ ``syntax.NewPrinter``（Go 侧 ATRI-X 未用；这里用于回写与自检）。

安全裁决相关的语义注意：

* :func:`is_dynamic` 比题目列举的"命令替换/算术/进程替换"更宽：它同时覆盖
  参数展开（``$x``、``"$x"``），因为 Go 的 ``literalWord`` 对它们同样返回 false，
  沙箱据此报"无法静态确认"。要只判那三类请用对应的 ``has_*``。
* :func:`command_names` 按 Go ``syntax.Walk`` 的文档顺序返回，因此
  ``shell_risk.go`` 那个"扁平 pipeline 列表"启发式（跨 ``&&``/``;`` 也成立，
  上游即如此，属已知过报）可以逐字复刻；要按真实管道分组请用
  :func:`pipelines`。
"""

from __future__ import annotations

from typing import Callable, Iterator

from .ast import (
    ArithCommand,
    ArithExpansion,
    Assignment,
    Block,
    CaseClause,
    CaseItem,
    CommandSubstitution,
    CompoundCommand,
    ForClause,
    FunctionDecl,
    Heredoc,
    IfClause,
    List,
    Node,
    ParamExpansion,
    Pipeline,
    ProcSubstitution,
    Redirect,
    Script,
    ShellFormatError,
    ShellParseError,
    SimpleCommand,
    Subshell,
    TestClause,
    TimeClause,
    WhileClause,
    Word,
)
from .lexer import KIND_EOF, KIND_NEWLINE, Lexer, SourceIndex
from .parser import parse

__all__ = [
    "command_base",
    "command_names",
    "commands",
    "format",
    "has_arithmetic",
    "has_background",
    "has_command_substitution",
    "has_heredoc",
    "has_parameter_expansion",
    "has_pipe",
    "has_process_substitution",
    "has_redirect",
    "has_write_redirect",
    "is_dynamic",
    "is_empty_or_comment_only",
    "iter_nodes",
    "literal_args",
    "pipelines",
    "walk",
    "words",
]

Visitor = Callable[[Node], "bool | None"]


# --------------------------------------------------------------------------
# 遍历
# --------------------------------------------------------------------------


def walk(node: Node, visitor: Visitor, *, order: str = "pre") -> None:
    """遍历 AST。

    ``order="pre"``：前序（等价 Go ``syntax.Walk``），``visitor`` 返回 ``False``
    时跳过该节点的子树。``order="post"``：后序（子节点先访问，返回值被忽略）。
    """
    if order == "pre":
        _walk_pre(node, visitor)
    elif order == "post":
        _walk_post(node, visitor)
    else:
        raise ValueError(f"order 必须是 'pre' 或 'post'，收到 {order!r}")


def _walk_pre(node: Node, visitor: Visitor) -> None:
    if visitor(node) is False:
        return
    for child in node.children():
        _walk_pre(child, visitor)


def _walk_post(node: Node, visitor: Visitor) -> None:
    for child in node.children():
        _walk_post(child, visitor)
    visitor(node)


def iter_nodes(node: Node) -> Iterator[Node]:
    """前序迭代所有节点。"""
    yield node
    for child in node.children():
        yield from iter_nodes(child)


def _collect(node: Node, want: type | tuple[type, ...]) -> list:
    out: list = []
    for item in iter_nodes(node):
        if isinstance(item, want):
            out.append(item)
    return out


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def words(node: Node) -> list[Word]:
    """全部 :class:`Word`（命令词、重定向目标、case 匹配对象、for 列表、赋值右值）。

    ``Assignment`` 不是 ``Word`` 子类，故不出现在这里；它的右值在
    ``Assignment.value``。每个词都带 ``raw`` 原文。
    """
    return _collect(node, Word)


def commands(node: Node) -> list[SimpleCommand]:
    """全部简单命令（Go：所有 ``*syntax.CallExpr``），按文档顺序。"""
    return _collect(node, SimpleCommand)


def pipelines(node: Node) -> list[Pipeline]:
    """全部管道（按文档顺序）；单条命令不会包成 :class:`Pipeline`。

    所以 ``len(pipelines(script)) > 0`` 才是真的用了 ``|``/``|&``。
    """
    return _collect(node, Pipeline)


def command_names(node: Node) -> list[str]:
    """静态可判定的命令名（按文档顺序，未做 ``command_base`` 归一化）。

    命令名是动态结构（``$CMD``、``$(...)``）时跳过——这类情况由
    :func:`commands` 里对应 ``SimpleCommand.name() is None`` 反映出来。
    """
    out: list[str] = []
    for cmd in commands(node):
        name = cmd.name()
        if name:
            out.append(name)
    return out


def literal_args(args: list[Word] | tuple[Word, ...]) -> list[str]:
    """等价 Go 的 ``literalArgs``：只保留可静态求值的词，丢掉动态词。"""
    out: list[str] = []
    for word in args:
        value = word.literal()
        if value is not None:
            out.append(value)
    return out


def command_base(name: str) -> str:
    """等价 Go 的 ``commandBase``：小写、去空白、``\\``→``/``、取 basename、去 ``.exe``。"""
    base = name.strip().lower().replace("\\", "/")
    if "/" in base:
        base = base[base.rfind("/") + 1 :]
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    return base


def has_command_substitution(node: Node) -> bool:
    """是否含 ``$(...)``/反引号命令替换（含双引号内、here-doc 正文内）。"""
    return any(True for item in iter_nodes(node) if isinstance(item, CommandSubstitution))


def has_arithmetic(node: Node) -> bool:
    """是否含 ``$((...))``、``((...))`` 或 ``for ((...))``。"""
    for item in iter_nodes(node):
        if isinstance(item, (ArithExpansion, ArithCommand)):
            return True
        if isinstance(item, ForClause) and item.arith is not None:
            return True
    return False


def has_process_substitution(node: Node) -> bool:
    """是否含 ``<(...)``/``>(...)``。"""
    return any(True for item in iter_nodes(node) if isinstance(item, ProcSubstitution))


def has_parameter_expansion(node: Node) -> bool:
    """是否含 ``$VAR``/``${VAR...}``。"""
    return any(True for item in iter_nodes(node) if isinstance(item, ParamExpansion))


def has_pipe(node: Node) -> bool:
    """是否真的用了管道（``|``/``|&``，至少两段命令）。"""
    return bool(pipelines(node))


def has_redirect(node: Node) -> bool:
    """是否含任意重定向（含 ``<<``/``<<<``/``<``）。"""
    return any(True for item in iter_nodes(node) if isinstance(item, Redirect))


def has_write_redirect(node: Node) -> bool:
    """是否含写入型重定向（``>``/``>>``/``&>``/``>&``/``>|``/``<>``）。

    与 ``shell_risk.go`` / ``shell_sandbox.go`` 的
    ``strings.Contains(op, ">") || strings.Contains(op, "<>")`` 判定一致：
    ``<``/``<<``/``<<-``/``<<<``/``<&`` 不算。
    """
    return any(
        item.is_write for item in iter_nodes(node) if isinstance(item, Redirect)
    )


def has_heredoc(node: Node) -> bool:
    """是否含 here-document。"""
    return any(True for item in iter_nodes(node) if isinstance(item, Heredoc))


def has_background(node: Node) -> bool:
    """是否含 ``&`` 后台分隔符（``a && b`` 与 ``&>`` 重定向都不算）。"""
    return any(item.background for item in iter_nodes(node) if isinstance(item, List))


def is_dynamic(node: Node) -> bool:
    """是否存在任何无法静态判定的展开。

    覆盖命令替换、算术、进程替换，以及参数展开（``$x``、``"$x"``、``${x:-y}``，
    含 here-document 正文里的参数展开）——后者的判定口径与 Go ``literalWord``
    一致（``shell_sandbox`` 把它记为"无法静态确认"）。``~`` 按 Go 侧行为算静态字面。
    """
    for item in iter_nodes(node):
        if isinstance(
            item,
            (
                CommandSubstitution,
                ArithExpansion,
                ArithCommand,
                ProcSubstitution,
                ParamExpansion,
            ),
        ):
            return True
    return any(word.literal() is None for word in words(node)) or has_arithmetic(node)


def is_empty_or_comment_only(source: str) -> bool:
    """源码是否既无语句也无重定向（只有空白、换行、整行注释）。

    解析失败（例如 ``\\0``）返回 ``False``：非法输入不算"空"。
    """
    try:
        index = SourceIndex(source)
        tokens, _ = Lexer(source, 0, len(source), index).tokenize()
    except ShellParseError:
        return False
    return all(tok.kind in (KIND_NEWLINE, KIND_EOF) for tok in tokens)


# --------------------------------------------------------------------------
# 回写
# --------------------------------------------------------------------------


def format(node: Node) -> str:
    """把 AST 回写成规范化 shell 文本（语义等价，不保证逐字节相同）。

    回写后必须能重新解析且结构签名一致；否则抛 :class:`ShellFormatError`
    （"无法安全回写时抛异常"）。
    """
    text = _Formatter().format_node(node)
    try:
        reparsed = parse(text)
    except ShellParseError as exc:
        raise ShellFormatError(f"回写结果无法重新解析：{exc}") from exc
    if _signature(reparsed) != _signature(node):
        raise ShellFormatError("回写结果与原 AST 不等价（结构签名不一致）")
    return text


def _signature(node: Node) -> tuple:
    kinds: list[str] = []
    literals: list[str | None] = []
    ops: list[str] = []
    for item in iter_nodes(node):
        kinds.append(type(item).__name__)
        if isinstance(item, Word):
            literals.append(item.literal())
        if isinstance(item, Redirect):
            ops.append(item.op)
    return (
        tuple(sorted(kinds)),
        tuple(literals),
        tuple(ops),
        is_dynamic(node),
        has_background(node),
        has_write_redirect(node),
    )


def _sep_text(sep: str) -> str:
    return sep if sep in (";", "&", "&&", "||") else ""


class _Formatter:
    """AST → shell 文本。无法判断的节点一律抛 :class:`ShellFormatError`。"""

    def __init__(self) -> None:
        self._heredocs: list[Heredoc] = []

    # -- 顶层与语句体 -----------------------------------------------------
    def format_node(self, node: Node) -> str:
        if isinstance(node, Script):
            return self._script_text(node)
        if isinstance(node, List):
            return self._list_text(node)
        return self._node_text(node)

    def _script_text(self, script: Script) -> str:
        lines = [comment.text for comment in script.comments]
        for lst in script.body:
            self._heredocs = []
            text = self._list_text(lst)
            for hdoc in self._heredocs:
                text += "\n" + hdoc.body + hdoc.delimiter
            lines.append(text)
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def _list_text(self, lst: List) -> str:
        pieces: list[str] = []
        for i, item in enumerate(lst.items):
            if i:
                pieces.append(_sep_text(lst.seps[i - 1]))
            pieces.append(self._node_text(item))
        if len(lst.seps) == len(lst.items) and lst.seps and lst.seps[-1] and pieces:
            pieces[-1] += _sep_text(lst.seps[-1])
        return " ".join(piece for piece in pieces if piece)

    def _body_text(self, lists: list[List]) -> str:
        out = ""
        for lst in lists:
            text = self._list_text(lst)
            if not (lst.seps and lst.seps[-1] in (";", "&")):
                text += ";"
            out += (" " if out else "") + text
        return out

    # -- 单节点 -----------------------------------------------------------
    def _node_text(self, node: Node) -> str:
        if isinstance(node, SimpleCommand):
            return self._simple_text(node)
        if isinstance(node, Pipeline):
            text = ""
            for i, cmd in enumerate(node.commands):
                if i:
                    text += " " + (node.ops[i - 1] or "|") + " "
                text += self._node_text(cmd)
            return ("! " if node.negated else "") + text
        if isinstance(node, CompoundCommand):
            return self._compound_text(node)
        if isinstance(node, Word):
            return node.text()
        if isinstance(node, List):
            return self._list_text(node)
        raise ShellFormatError(f"无法回写节点类型 {type(node).__name__}")

    def _compound_text(self, node: CompoundCommand) -> str:
        if isinstance(node, Subshell):
            text = "( " + self._body_text(node.body) + " )"
        elif isinstance(node, Block):
            text = "{ " + self._body_text(node.body) + " }"
        elif isinstance(node, IfClause):
            text = self._if_text(node, "if")
        elif isinstance(node, WhileClause):
            keyword = "until" if node.until else "while"
            text = f"{keyword} {self._body_text(node.condition)} do {self._body_text(node.body)} done"
        elif isinstance(node, ForClause):
            text = self._for_text(node)
        elif isinstance(node, CaseClause):
            text = self._case_text(node)
        elif isinstance(node, FunctionDecl):
            text = f"{node.name}() {self._node_text(node.body)}"
        elif isinstance(node, TestClause):
            text = node.raw
        elif isinstance(node, ArithCommand):
            text = node.raw
        elif isinstance(node, TimeClause):
            text = ("time -p " if node.posix else "time ") + self._node_text(node.pipeline)
        else:
            raise ShellFormatError(f"无法回写复合节点类型 {type(node).__name__}")
        for redir in node.redirects:
            text += " " + self._redirect_text(redir)
        return text

    def _if_text(self, clause: IfClause, keyword: str, top: bool = True) -> str:
        text = (
            f"{keyword} {self._body_text(clause.condition)} then "
            f"{self._body_text(clause.then_body)}"
        )
        if clause.elif_clause is not None:
            text += " " + self._if_text(clause.elif_clause, "elif", top=False)
        elif clause.else_body is not None:
            text += " else " + self._body_text(clause.else_body)
        return text + " fi" if top else text

    def _for_text(self, clause: ForClause) -> str:
        if clause.arith is not None:
            head = f"for (({clause.arith}));"
        elif clause.in_words is None:
            head = f"for {clause.var};"
        else:
            words_text = " ".join(word.text() for word in clause.in_words)
            head = f"for {clause.var} in {words_text};" if words_text else f"for {clause.var} in;"
        return f"{head} do {self._body_text(clause.body)} done"

    def _case_text(self, clause: CaseClause) -> str:
        items = [self._case_item_text(item) for item in clause.items]
        inner = " ".join(items)
        return f"case {clause.subject.text()} in {inner} esac" if inner else (
            f"case {clause.subject.text()} in esac"
        )

    def _case_item_text(self, item: CaseItem) -> str:
        patterns = " | ".join(word.text() for word in item.patterns)
        body = self._body_text(item.body)
        if body.endswith(";"):
            body = body[:-1]
        return f"{patterns}) {body}{item.terminator or ''}"

    def _simple_text(self, node: SimpleCommand) -> str:
        pieces: list[str] = []
        for assign in node.assignments:
            pieces.append(self._assignment_text(assign))
        pieces.extend(word.text() for word in node.words)
        pieces.extend(self._redirect_text(redir) for redir in node.redirects)
        return " ".join(piece for piece in pieces if piece)

    def _assignment_text(self, assign: Assignment) -> str:
        if isinstance(assign.index, str):
            return f"{assign.name}[{assign.index}]{'+' if assign.append else ''}={self._value_text(assign)}"
        return f"{assign.name}{'+' if assign.append else ''}={self._value_text(assign)}"

    def _value_text(self, assign: Assignment) -> str:
        return "" if assign.value is None else assign.value.text()

    def _redirect_text(self, redir: Redirect) -> str:
        if redir.hdoc is not None:
            self._heredocs.append(redir.hdoc)
        word = redir.word.text() if redir.word is not None else ""
        if not word:
            raise ShellFormatError(f"重定向 '{redir.op}' 缺少目标，无法回写")
        return f"{redir.fd or ''}{redir.op}{word}"
