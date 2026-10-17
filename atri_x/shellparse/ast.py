"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：AST 节点与位置信息。

对应 Go：``mvdan.cc/sh/v3/syntax`` 的节点子集（``File``/``Stmt``/``CallExpr``/
``Assign``/``Redirect``/``Word``/``Lit``/``SglQuoted``/``DblQuoted``/``ParamExp``/
``CmdSubst``/``ArithmExp``/``ProcSubst``/``IfClause``/``ForClause``/``WhileClause``/
``CaseClause``/``FuncDecl``/``Subshell``/``Block``/``TestClause``/``ArithmCmd``）。

本模块是**独立实现**：不包含 ``bashlex``（GPLv3+，已停更）的任何代码、数据
结构或设计，也不依赖任何第三方包。上游把 ``syntax.Walk`` 当安全裁决器用，因此
节点形状按"裁决需要什么"裁剪，而不是完整 POSIX shell。

与 Go 的关键差异（有意为之，见各节点文档）：Go 用 ``Stmt``（含 ``Semicolon``/
``Background``/``Negated`` 标记）表达列表，这里用 :class:`List` + ``seps``；
块的体是 ``list[List]``（Go 是 ``[]*Stmt``），只有顶层是 :class:`Script`；
位置用 :class:`Pos`（字符偏移 + 1-based 行列）而非 ``token.Pos``；解析错误抛
:class:`ShellParseError`（Go 是 ``syntax.ParseError``），回写失败抛
:class:`ShellFormatError`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.errors import InvalidError

__all__ = [
    "AnsiCQuoted", "ArithCommand", "ArithExpansion", "Assignment", "Block",
    "CaseClause", "CaseItem", "CommandSubstitution", "Comment", "CompoundCommand",
    "DoubleQuoted", "ForClause", "FunctionDecl", "Heredoc", "IfClause", "List",
    "Literal", "Node", "ParamExpansion", "Pipeline", "Pos", "ProcSubstitution",
    "Redirect", "Script", "ShellFormatError", "ShellParseError", "SimpleCommand",
    "SingleQuoted", "Subshell", "TestClause", "Tilde", "TimeClause", "WhileClause",
    "Word", "WordPart", "is_write_redirect",
]

# --------------------------------------------------------------------------
# 位置与错误
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Pos:
    """源码位置：字符偏移（``end`` 开区间）+ 1-based 起止行列。

    偏移是 Python ``str`` 下标（不是字节偏移）；CJK 一个字符算一格。
    Go 侧等价物是 ``syntax.Pos`` 的 ``Offset()/Line()/Col()``。
    """

    start: int
    end: int
    line: int
    col: int
    end_line: int
    end_col: int

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"

class ShellParseError(InvalidError):
    """shell 源码无法解析（Go：``syntax.ParseError``）。

    带 ``line``/``col``/``offset``/``snippet``（出错那一行的原文），
    ``str(err)`` 渲染成 ``line L:C: msg`` + 原文 + 插入符。
    """

    def __init__(
        self,
        message: str,
        *,
        line: int = 0,
        col: int = 0,
        offset: int = 0,
        snippet: str = "",
    ) -> None:
        self.message = message
        self.line = line
        self.col = col
        self.offset = offset
        self.snippet = snippet
        super().__init__(self._render())

    def _render(self) -> str:
        if self.line <= 0:
            return self.message
        head = f"line {self.line}:{self.col}: {self.message}"
        if not self.snippet:
            return head
        return f"{head}\n    {self.snippet}\n    {' ' * max(self.col - 1, 0)}^"

class ShellFormatError(InvalidError):
    """AST 无法安全回写成等价 shell 文本。"""

# --------------------------------------------------------------------------
# 单词与其组成部分
# --------------------------------------------------------------------------

class Node:
    """AST 基类：所有节点都带 ``pos``，并能枚举子节点（供 ``walk`` 使用）。"""

    __slots__ = ()

    pos: Pos

    @property
    def kind(self) -> str:
        """节点类型名（用于签名比较与诊断）。"""
        return type(self).__name__

    def children(self) -> tuple[Node, ...]:
        """按源码顺序返回子节点；``walk`` 依赖该顺序等价 Go 的 ``syntax.Walk``。"""
        return ()

class WordPart(Node):
    """单词的组成部分：字面、各类引号、各类展开。"""

    __slots__ = ()

@dataclass(slots=True)
class Literal(WordPart):
    """未加引号的字面文本；``value`` 已解开转义，``raw`` 原文，``quoted`` 表示来自 ``\\x``。"""

    pos: Pos
    value: str
    raw: str
    quoted: bool = False

@dataclass(slots=True)
class SingleQuoted(WordPart):
    """``'...'``：内容完全字面。"""

    pos: Pos
    value: str
    raw: str

@dataclass(slots=True)
class AnsiCQuoted(WordPart):
    """``$'...'``：ANSI-C 引用，``value`` 是解码结果（Go：``SglQuoted{Dollar:true}``）。"""

    pos: Pos
    value: str
    raw: str

@dataclass(slots=True)
class Tilde(WordPart):
    """``~`` / ``~user``（词首）。Go 侧把它留在 ``Lit`` 里，故 ``Word.literal()`` 照旧返回原文。"""

    pos: Pos
    raw: str
    user: str = ""

    @property
    def value(self) -> str:
        return self.raw

@dataclass(slots=True)
class ParamExpansion(WordPart):
    """``$VAR`` / ``${VAR}`` / ``${VAR:-x}`` 等参数展开（只做结构识别，不求值）。"""

    pos: Pos
    raw: str
    name: str
    op: str = ""
    braced: bool = True
    operand_raw: str = ""
    operand_parts: list[WordPart] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return tuple(self.operand_parts)

@dataclass(slots=True)
class ArithExpansion(WordPart):
    """``$((expr))`` 算术展开；``expr`` 为原文，不求值。"""

    pos: Pos
    raw: str
    expr: str

@dataclass(slots=True)
class CommandSubstitution(WordPart):
    """``$(...)`` / ``\\`...\\```；``script`` 由解析器回填（词法阶段只切出范围）。"""

    pos: Pos
    raw: str
    body_start: int
    body_end: int
    backquoted: bool = False
    script: Script | None = None

    def children(self) -> tuple[Node, ...]:
        return () if self.script is None else (self.script,)

@dataclass(slots=True)
class ProcSubstitution(WordPart):
    """进程替换 ``<(cmd)`` / ``>(cmd)``（bash 扩展）。"""

    pos: Pos
    raw: str
    direction: str
    body_start: int
    body_end: int
    script: Script | None = None

    def children(self) -> tuple[Node, ...]:
        return () if self.script is None else (self.script,)

@dataclass(slots=True)
class DoubleQuoted(WordPart):
    """``"..."``：内层可含字面与 ``$``/反引号展开。"""

    pos: Pos
    raw: str
    parts: list[WordPart] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return tuple(self.parts)

@dataclass(slots=True)
class Word(Node):
    """命令词。``literal()`` 等价 Go 的 ``literalWord``（不可静态判定时返回 ``None``）。"""

    pos: Pos
    parts: list[WordPart] = field(default_factory=list)
    raw: str = ""

    def children(self) -> tuple[Node, ...]:
        return tuple(self.parts)

    def literal(self) -> str | None:
        """静态字面值；只要含参数/命令/算术/进程替换就返回 ``None``。

        对齐 Go 的 ``literalWord``：``Lit``/``SglQuoted``（含 ``$'..'``）可拼接，
        ``DblQuoted`` 内部必须全是 ``Lit``；其余（``$x``、``$(...)``、``$((..))``、
        ``<(..)``）一律不是字面。``~`` 按 Go 侧行为算字面（所以仍能被沙箱识别为
        home 路径）。
        """
        if not self.parts:
            return None
        out: list[str] = []
        for part in self.parts:
            if isinstance(part, Literal):
                out.append(part.value)
            elif isinstance(part, (SingleQuoted, AnsiCQuoted)):
                out.append(part.value)
            elif isinstance(part, Tilde):
                out.append(part.raw)
            elif isinstance(part, DoubleQuoted):
                for inner in part.parts:
                    if not isinstance(inner, Literal):
                        return None
                    out.append(inner.value)
            else:
                return None
        return "".join(out)

    def bare_text(self) -> str | None:
        """未加引号、未转义的单个字面词文本；用于关键字/保留字判定，否则 ``None``。"""
        if len(self.parts) != 1:
            return None
        part = self.parts[0]
        if isinstance(part, Literal) and not part.quoted:
            return part.value
        return None

    @property
    def dynamic(self) -> bool:
        """是否含无法静态判定的展开（等价 ``literal() is None``）。"""
        return self.literal() is None

    def text(self) -> str:
        """回写用原文。"""
        return self.raw or "".join(part.raw for part in self.parts)

    def __str__(self) -> str:
        return self.text()

# --------------------------------------------------------------------------
# 命令元素
# --------------------------------------------------------------------------

def is_write_redirect(op: str) -> bool:
    """是否为"写入型"重定向。

    对齐 Go 的 ``strings.Contains(op, ">") || strings.Contains(op, "<>")``：
    ``>``/``>>``/``&>``/``&>>``/``>&``/``>|``/``<>`` 为真；``<``/``<<``/``<<-``/
    ``<<<``/``<&`` 为假（heredoc、herestring 因此不会被 shell_risk 记为写入）。
    """
    return ">" in op or "<>" in op

@dataclass(slots=True)
class Assignment(Node):
    """``name=value``（只出现在命令词之前；``a+=``/``a[i]=`` 也在此）。

    ``value`` 为 ``None`` 表示空右值（``a=``），与 Go 的 ``Assign.Value`` 对齐；
    带引号的空值（``a=""``）是 parts 为空的 :class:`Word`。
    """

    pos: Pos
    name: str = ""
    value: Word | None = None
    index: str | None = None
    append: bool = False

    def children(self) -> tuple[Node, ...]:
        return () if self.value is None else (self.value,)

    def text(self) -> str:
        """回写用文本。"""
        index = f"[{self.index}]" if self.index is not None else ""
        return f"{self.name}{index}{'+' if self.append else ''}={self.value.text() if self.value else ''}"

@dataclass(slots=True)
class Heredoc(Node):
    """here-document 正文（Go：``Redirect.Hdoc`` 那个 Word）。"""

    pos: Pos
    op: str
    delimiter: str
    delimiter_raw: str
    body: str
    parts: list[WordPart] = field(default_factory=list)
    quoted: bool = False
    strip_tabs: bool = False

    def children(self) -> tuple[Node, ...]:
        return tuple(self.parts)

    @property
    def expand(self) -> bool:
        """界定符未加引号时正文会做参数/命令展开。"""
        return not self.quoted

@dataclass(slots=True)
class Redirect(Node):
    """重定向。``word`` 对 heredoc 而言是界定符（同 Go），正文在 ``hdoc``。"""

    pos: Pos
    op: str
    word: Word | None = None
    fd: str | None = None
    hdoc: Heredoc | None = None

    def children(self) -> tuple[Node, ...]:
        out: list[Node] = []
        if self.word is not None:
            out.append(self.word)
        if self.hdoc is not None:
            out.append(self.hdoc)
        return tuple(out)

    @property
    def is_write(self) -> bool:
        """见 :func:`is_write_redirect`。"""
        return is_write_redirect(self.op)

@dataclass(slots=True)
class SimpleCommand(Node):
    """简单命令（Go：``CallExpr``）。``words[0]`` 是命令名（可能为动态）。"""

    pos: Pos
    assignments: list[Assignment] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (*self.assignments, *self.words, *self.redirects)

    def name(self) -> str | None:
        """静态可判定的命令名（未做 ``command_base`` 归一化）；否则 ``None``。"""
        if not self.words:
            return None
        return self.words[0].literal()

    def args(self) -> list[Word]:
        """命令名之后的词（Go：``CallExpr.Args[1:]``）。"""
        return list(self.words[1:])

@dataclass(slots=True)
class Pipeline(Node):
    """管道。``ops[i]`` 是 ``commands[i]`` 与 ``commands[i+1]`` 之间的 ``|`` 或 ``|&``。"""

    pos: Pos
    commands: list[Node] = field(default_factory=list)
    ops: list[str] = field(default_factory=list)
    negated: bool = False

    def children(self) -> tuple[Node, ...]:
        return tuple(self.commands)

@dataclass(slots=True)
class List(Node):
    """``;``/``&``/``&&``/``||`` 连接的语句串（本实现里换行终止一个 List）。"""

    pos: Pos
    items: list[Node] = field(default_factory=list)
    seps: list[str] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return tuple(self.items)

    @property
    def background(self) -> bool:
        """是否含 ``&`` 后台分隔符（注意 ``a && b`` 与 ``&>`` 重定向都不算）。"""
        return "&" in self.seps

class CompoundCommand(Node):
    """复合命令基类：可挂尾部重定向（Go 的 ``Stmt.Redirs`` 与具体命令无关）。

    例如 ``while read l; do ...; done < f``、``{ ...; } >> log``、
    ``case ... esac 2>&1`` 里的重定向都落在各节点的 ``redirects`` 字段。
    """

    __slots__ = ()

    redirects: list[Redirect]

    def children(self) -> tuple[Node, ...]:
        return tuple(self.redirects)

@dataclass(slots=True)
class Subshell(CompoundCommand):
    """``( ... )``。"""

    pos: Pos
    body: list[List] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (*self.body, *self.redirects)

@dataclass(slots=True)
class Block(CompoundCommand):
    """``{ ...; }``。"""

    pos: Pos
    body: list[List] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (*self.body, *self.redirects)

@dataclass(slots=True)
class IfClause(CompoundCommand):
    """``if cond; then ...; [elif ...; then ...;] [else ...;] fi``。"""

    pos: Pos
    condition: list[List] = field(default_factory=list)
    then_body: list[List] = field(default_factory=list)
    else_body: list[List] | None = None
    elif_clause: IfClause | None = None
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        out: list[Node] = [*self.condition, *self.then_body]
        if self.elif_clause is not None:
            out.append(self.elif_clause)
        if self.else_body is not None:
            out.extend(self.else_body)
        return (*out, *self.redirects)

@dataclass(slots=True)
class ForClause(CompoundCommand):
    """``for x in a b; do ...; done``；``arith`` 非空表示 ``for ((;;))``。"""

    pos: Pos
    var: str = ""
    in_words: list[Word] | None = None
    body: list[List] = field(default_factory=list)
    arith: str | None = None
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        out: list[Node] = list(self.in_words or ())
        out.extend(self.body)
        return (*out, *self.redirects)

@dataclass(slots=True)
class WhileClause(CompoundCommand):
    """``while``/``until cond; do ...; done``。"""

    pos: Pos
    condition: list[List] = field(default_factory=list)
    body: list[List] = field(default_factory=list)
    until: bool = False
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (*self.condition, *self.body, *self.redirects)

@dataclass(slots=True)
class CaseItem(Node):
    """``case`` 的一个分支；``terminator`` 为 ``;;``/``;&``/``;;&`` 或 ``None``。"""

    pos: Pos
    patterns: list[Word] = field(default_factory=list)
    body: list[List] = field(default_factory=list)
    terminator: str | None = None

    def children(self) -> tuple[Node, ...]:
        return (*self.patterns, *self.body)

@dataclass(slots=True)
class CaseClause(CompoundCommand):
    """``case word in ... esac``。"""

    pos: Pos
    subject: Word
    items: list[CaseItem] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (self.subject, *self.items, *self.redirects)

@dataclass(slots=True)
class FunctionDecl(CompoundCommand):
    """``name() { ...; }`` 或 ``function name { ...; }``。"""

    pos: Pos
    name: str
    body: Node
    keyword: bool = False
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (self.body, *self.redirects)

@dataclass(slots=True)
class TestClause(CompoundCommand):
    """``[[ ... ]]``（bash）。条件词仍会被收集，条件本身不求值。"""

    pos: Pos
    raw: str
    words: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (*self.words, *self.redirects)

@dataclass(slots=True)
class ArithCommand(CompoundCommand):
    """``((expr))``（bash 算术命令）。"""

    pos: Pos
    raw: str
    expr: str
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return tuple(self.redirects)

@dataclass(slots=True)
class TimeClause(CompoundCommand):
    """``time [-p] pipeline``（Go：``TimeClause``，管道本身不是一个 CallExpr）。"""

    pos: Pos
    pipeline: Node
    posix: bool = False
    redirects: list[Redirect] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return (self.pipeline, *self.redirects)

@dataclass(slots=True)
class Comment:
    """整行或行尾注释（Go 需 ``KeepComments`` 才有）。"""

    pos: Pos
    text: str

@dataclass(slots=True)
class Script(Node):
    """顶层脚本（Go：``File``）。``body`` 每项是一条换行分隔的语句串。"""

    pos: Pos
    body: list[List] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)

    def children(self) -> tuple[Node, ...]:
        return tuple(self.body)
