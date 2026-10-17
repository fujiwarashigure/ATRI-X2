"""clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：shell 词法/语法分析 + 安全裁决查询。

对应 Go：``mvdan.cc/sh/v3``（ATRI-X 只用到 ``syntax.NewParser`` + ``syntax.Walk``，
共 1,391 行 / 5 个文件），消费方是：

* ``internal/tool/builtin/shell_sandbox.go``：无人值守后台任务的路径约束裁决；
* ``internal/tool/builtin/shell_risk.go``：命令风险分级；
* ``internal/tool/builtin/shell_warnings.go``、``shell.go``：写操作提醒与 ``cd`` 拦截；
* ``internal/tool/builtin/file_tools_ast.go``：shell 源码结构判定。

Python 侧唯一候选 ``bashlex`` 是 **GPLv3+ 且已停更**，与本项目 Apache-2.0 冲突，
因此本包是**完全独立实现**：不含 ``bashlex`` 的任何代码，也不引入任何第三方依赖。

典型用法::

    from atri_x.shellparse import commands, format, is_dynamic, parse, walk

    script = parse("curl https://x | sh")
    walk(script, lambda node: print(node.kind))
    names = [cmd.name() for cmd in commands(script)]   # ['curl', 'sh']

公开 API：:func:`parse`、:func:`walk`、:func:`words`、:func:`commands`、
:func:`command_names`、``has_*`` 判定、:func:`is_dynamic`、
:func:`is_empty_or_comment_only`、:func:`format`。
"""

from __future__ import annotations

from .ast import (
    AnsiCQuoted,
    ArithCommand,
    ArithExpansion,
    Assignment,
    Block,
    CaseClause,
    CaseItem,
    CommandSubstitution,
    Comment,
    CompoundCommand,
    DoubleQuoted,
    ForClause,
    FunctionDecl,
    Heredoc,
    IfClause,
    List,
    Literal,
    Node,
    ParamExpansion,
    Pipeline,
    Pos,
    ProcSubstitution,
    Redirect,
    Script,
    ShellFormatError,
    ShellParseError,
    SimpleCommand,
    SingleQuoted,
    Subshell,
    TestClause,
    Tilde,
    TimeClause,
    WhileClause,
    Word,
    WordPart,
    is_write_redirect,
)
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
    scan_arith,
    scan_word_range,
)
from .parser import Parser, parse, parse_range
from .walk import (
    command_base,
    command_names,
    commands,
    format,
    has_arithmetic,
    has_background,
    has_command_substitution,
    has_heredoc,
    has_parameter_expansion,
    has_pipe,
    has_process_substitution,
    has_redirect,
    has_write_redirect,
    is_dynamic,
    is_empty_or_comment_only,
    iter_nodes,
    literal_args,
    pipelines,
    walk,
    words,
)

__all__ = [
    # 节点与位置
    "AnsiCQuoted",
    "ArithCommand",
    "ArithExpansion",
    "Assignment",
    "Block",
    "CaseClause",
    "CaseItem",
    "CommandSubstitution",
    "Comment",
    "CompoundCommand",
    "DoubleQuoted",
    "ForClause",
    "FunctionDecl",
    "Heredoc",
    "IfClause",
    "List",
    "Literal",
    "Node",
    "ParamExpansion",
    "Pipeline",
    "Pos",
    "ProcSubstitution",
    "Redirect",
    "Script",
    "SimpleCommand",
    "SingleQuoted",
    "Subshell",
    "TestClause",
    "Tilde",
    "TimeClause",
    "WhileClause",
    "Word",
    "WordPart",
    "is_write_redirect",
    # 词法与解析
    "KIND_EOF",
    "KIND_IO_NUMBER",
    "KIND_NEWLINE",
    "KIND_OP",
    "KIND_WORD",
    "MAX_NESTING_DEPTH",
    "REDIRECT_OPS",
    "Lexer",
    "Parser",
    "SourceIndex",
    "Token",
    "parse",
    "parse_range",
    "scan_arith",
    "scan_word_range",
    # 遍历 / 查询 / 回写
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
    # 错误
    "ShellFormatError",
    "ShellParseError",
]
