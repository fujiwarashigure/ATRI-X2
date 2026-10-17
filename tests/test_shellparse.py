"""``atri_x.shellparse`` 的单元测试。

覆盖：需求列出的每个公开 API（parse/walk/words/commands/command_names/``has_*``/
``is_dynamic``/``is_empty_or_comment_only``/format）、词法边界（引号、转义、续行、
``$VAR``/``${...}``/``$(...)``/反引号/``$((...))``/``<( )``/``~``、注释、CRLF、
UTF-8、``\\0``）、全部运算符与 here-document，以及 ``shell_risk.go`` /
``shell_sandbox.go`` 的每个判定维度（见两个 ``*Dimensions`` 测试类）。
"""
from __future__ import annotations

import unittest

from atri_x.shellparse import (
    MAX_NESTING_DEPTH,
    ArithCommand, ArithExpansion, Block, CaseClause, CommandSubstitution,
    CompoundCommand, DoubleQuoted, ForClause, FunctionDecl, Heredoc, IfClause,
    Literal, ParamExpansion, Pipeline, ProcSubstitution, Redirect, ShellFormatError,
    ShellParseError, SimpleCommand, SingleQuoted, Subshell, TestClause, Tilde,
    TimeClause, WhileClause, Word,
    command_base, command_names, commands, format, has_arithmetic, has_background,
    has_command_substitution, has_heredoc, has_parameter_expansion, has_pipe,
    has_process_substitution, has_redirect, has_write_redirect, is_dynamic,
    is_empty_or_comment_only, iter_nodes, literal_args, parse, pipelines, walk, words,
)


def first_command(src: str) -> SimpleCommand:
    return commands(parse(src))[0]


def top_command(src: str):
    """取第一条语句里最外层的命令节点（跳过 Pipeline 包装）。"""
    item = parse(src).body[0].items[0]
    return item.commands[0] if isinstance(item, Pipeline) else item


def find(script, want):
    return [n for n in iter_nodes(script) if isinstance(n, want)]


class TestLexingWords(unittest.TestCase):
    def test_plain_words_and_positions(self) -> None:
        script = parse("echo hello world")
        self.assertEqual(command_names(script), ["echo"])
        self.assertEqual([w.literal() for w in words(script)], ["echo", "hello", "world"])
        word = words(script)[1]
        self.assertEqual((word.pos.start, word.pos.end, word.pos.line, word.pos.col),
                         (5, 10, 1, 6))
        self.assertEqual(parse("echo a\necho b").body[1].pos.line, 2)
    def test_quotes(self) -> None:
        self.assertEqual(words(parse("echo 'a $x `c` \\n'"))[1].literal(), "a $x `c` \\n")
        self.assertIsInstance(words(parse("echo 'x'"))[1].parts[0], SingleQuoted)
        self.assertEqual(words(parse('echo "a b"'))[1].literal(), "a b")
        self.assertEqual(words(parse("echo $'a\\nb\\x41\\101\\u0042'"))[1].literal(), "a\nbAAB")
        self.assertEqual(words(parse("echo $'x\\qy'"))[1].literal(), "x\\qy")
        localized = words(parse('echo $"text $x"'))[1]
        self.assertIsInstance(localized.parts[0], DoubleQuoted)
        self.assertEqual(localized.parts[0].raw, '$"text $x"')
    def test_double_quoted_keeps_expansion_structure(self) -> None:
        script = parse('echo "a $x $(date) b"')
        quoted = words(script)[1].parts[0]
        self.assertIsNone(words(script)[1].literal())
        self.assertEqual(
            [type(p).__name__ for p in quoted.parts],
            ["Literal", "ParamExpansion", "Literal", "CommandSubstitution", "Literal"],
        )
        self.assertTrue(has_parameter_expansion(script))
        self.assertTrue(has_command_substitution(script))
    def test_backslash_and_continuation(self) -> None:
        word = words(parse("echo a\\ b"))[1]
        self.assertEqual(word.literal(), "a b")
        self.assertTrue(word.parts[0].quoted)
        self.assertEqual(words(parse("echo ab\\\ncd"))[1].literal(), "abcd")
        self.assertEqual([w.literal() for w in words(parse("find . -exec rm {} \\;"))],
                         ["find", ".", "-exec", "rm", "{}", ";"])
    def test_tilde_is_literal_but_structured(self) -> None:
        script = parse("cat ~/x ~user/y ~")
        self.assertIsInstance(words(script)[1].parts[0], Tilde)
        self.assertEqual(words(script)[1].parts[0].raw, "~")
        self.assertEqual([w.literal() for w in words(script)[1:]], ["~/x", "~user/y", "~"])
    def test_parameter_expansions(self) -> None:
        cases = {
            "$x": ("x", "", False), "${x}": ("x", "", True), "${x:-d}": ("x", ":-", True),
            "${x:=d}": ("x", ":=", True), "${x:?e}": ("x", ":?", True),
            "${x:+a}": ("x", ":+", True), "${x#p}": ("x", "#", True),
            "${x##p}": ("x", "##", True), "${x%s}": ("x", "%", True),
            "${x%%s}": ("x", "%%", True), "${x/a/b}": ("x", "/", True),
            "${x//a/b}": ("x", "//", True), "${#x}": ("#x", "", True),
            "${!x}": ("!x", "", True), "${x@Q}": ("x", "@Q", True),
            "${x:1:2}": ("x", ":", True), "$1": ("1", "", False),
            "$@": ("@", "", False), "$*": ("*", "", False), "$#": ("#", "", False),
            "$?": ("?", "", False), "$$": ("$", "", False), "$!": ("!", "", False),
            "$-": ("-", "", False), "$_": ("_", "", False),
        }
        for source, (name, op, braced) in cases.items():
            with self.subTest(source=source):
                word = words(parse(f"echo {source}"))[1]
                part = word.parts[0]
                self.assertIsInstance(part, ParamExpansion)
                self.assertEqual((part.name, part.op, part.braced, part.raw),
                                 (name, op, braced, source))
                self.assertIsNone(word.literal())
    def test_parameter_expansion_operand_and_literal_dollar(self) -> None:
        script = parse("echo ${x:-${y:-$(date)}}")
        part = words(script)[1].parts[0]
        self.assertIsInstance(part, ParamExpansion)
        self.assertEqual(part.op, ":-")
        self.assertTrue(has_command_substitution(script))
        self.assertEqual(words(parse("echo a$ b"))[1].literal(), "a$")
    def test_command_substitution_and_backticks(self) -> None:
        script = parse("echo $(date +%s) `whoami`")
        subs = find(script, CommandSubstitution)
        self.assertEqual([s.backquoted for s in subs], [False, True])
        self.assertEqual([s.raw for s in subs], ["$(date +%s)", "`whoami`"])
        inner = subs[0].script.body[0].items[0]
        self.assertIsInstance(inner, SimpleCommand)
        self.assertEqual(inner.words[0].literal(), "date")
        self.assertEqual(command_names(script), ["echo", "date", "whoami"])
        self.assertEqual(command_names(parse("echo $(echo $(echo deep))")), ["echo"] * 3)
    def test_arithmetic_and_process_substitution(self) -> None:
        script = parse("echo $((1 + 2)) $(( (3) * 4 ))")
        self.assertEqual(words(script)[1].parts[0].expr, "1 + 2")
        self.assertIsInstance(words(script)[1].parts[0], ArithExpansion)
        procs = find(parse("diff <(sort a) >(gzip > b)"), ProcSubstitution)
        self.assertEqual([p.direction for p in procs], ["<", ">"])
        self.assertEqual(procs[0].raw, "<(sort a)")
        self.assertEqual(command_names(parse("diff <(sort a) >(gzip > b)")),
                         ["diff", "sort", "gzip"])
        self.assertTrue(has_process_substitution(parse("diff <(a) <(b)")))


class TestLexingOperators(unittest.TestCase):
    def test_list_and_pipeline_operators(self) -> None:
        cases = {"a; b": [";", ""], "a & b": ["&", ""], "a && b": ["&&", ""],
                 "a || b": ["||", ""], "a && b || c; d &": ["&&", "||", ";", "&"]}
        for source, seps in cases.items():
            with self.subTest(source=source):
                self.assertEqual(parse(source).body[0].seps, seps)
        pipeline = pipelines(parse("a | b |& c"))[0]
        self.assertEqual(pipeline.ops, ["|", "|&"])
        self.assertTrue(has_pipe(parse("a | b |& c")))
        self.assertFalse(has_pipe(parse("a && b")))
        self.assertTrue(pipelines(parse("! grep -q x f"))[0].negated)
        self.assertEqual(command_names(parse("! grep -q x f")), ["grep"])
    def test_subshell_block_and_words(self) -> None:
        self.assertIsInstance(top_command("(cd x && ls)"), Subshell)
        block = top_command("{ echo a; echo b; }")
        self.assertIsInstance(block, Block)
        self.assertIsInstance(block, CompoundCommand)
        for source, want in (("echo }", ["echo", "}"]), ("echo a{b,c}d", ["echo", "a{b,c}d"]),
                             ("echo {}", ["echo", "{}"]), ("echo {", ["echo", "{"])):
            with self.subTest(source=source):
                self.assertEqual([w.literal() for w in words(parse(source))], want)
    def test_redirect_operators_and_fd(self) -> None:
        source = "cmd >a >>b <c <>d >|e 2>&1 3<&0 &>f &>>g <<<h"
        self.assertEqual([r.op for r in find(parse(source), Redirect)],
                         [">", ">>", "<", "<>", ">|", ">&", "<&", "&>", "&>>", "<<<"])
        redirects = find(parse("cmd 2>err 3<in"), Redirect)
        self.assertEqual([(r.fd, r.op, r.word.literal()) for r in redirects],
                         [("2", ">", "err"), ("3", "<", "in")])
        self.assertIsNone(find(parse("cmd >out"), Redirect)[0].fd)
    def test_assignments(self) -> None:
        command = first_command("A=1 B+=2 cmd x")
        self.assertEqual([(a.name, a.append, a.value.literal()) for a in command.assignments],
                         [("A", False, "1"), ("B", True, "2")])
        self.assertEqual((command.name(), command.args()[0].literal()), ("cmd", "x"))
        indexed = first_command("arr[0]=x empty=")
        self.assertEqual((indexed.assignments[0].index, indexed.assignments[0].value.literal()),
                         ("0", "x"))
        self.assertIsNone(indexed.assignments[1].value)
        self.assertEqual(indexed.words, [])
        quoted = first_command("\"a\"=b c=1 cmd")
        self.assertEqual([w.literal() for w in quoted.words], ["a=b", "c=1", "cmd"])
        self.assertEqual(quoted.assignments, [])
    def test_comment_boundaries(self) -> None:
        script = parse("echo a#b # trailing\n# whole line\n")
        self.assertEqual([w.literal() for w in words(script)], ["echo", "a#b"])
        self.assertEqual([c.text for c in script.comments], ["# trailing", "# whole line"])
        self.assertEqual(command_names(parse("echo http://x#frag")), ["echo"])


class TestHeredocs(unittest.TestCase):
    def test_plain_heredoc(self) -> None:
        script = parse("cat <<EOF\nline1\nline2\nEOF\n")
        redirect = find(script, Redirect)[0]
        self.assertEqual((redirect.op, redirect.word.literal()), ("<<", "EOF"))
        self.assertEqual(redirect.hdoc.body, "line1\nline2\n")
        self.assertFalse(redirect.hdoc.quoted)
        self.assertTrue(has_heredoc(script))
        self.assertTrue(has_redirect(script))
        self.assertFalse(has_write_redirect(script))
        self.assertEqual([w.literal() for w in commands(script)[0].words], ["cat"])
    def test_heredoc_body_expansions(self) -> None:
        script = parse("cat <<EOF\n$USER $(date)\nEOF\n")
        kinds = [type(p).__name__ for p in find(script, Heredoc)[0].parts]
        self.assertIn("ParamExpansion", kinds)
        self.assertIn("CommandSubstitution", kinds)
        self.assertTrue(has_command_substitution(script))
        self.assertTrue(is_dynamic(script))
        self.assertEqual(command_names(script), ["cat", "date"])
    def test_quoted_delimiter_disables_expansion(self) -> None:
        for source in ("cat <<'EOF'\n$x $(date)\nEOF\n", 'cat <<"EOF"\n$x\nEOF\n',
                       "cat <<\\EOF\n$x\nEOF\n"):
            with self.subTest(source=source):
                script = parse(source)
                hdoc = find(script, Heredoc)[0]
                self.assertTrue(hdoc.quoted)
                self.assertEqual(hdoc.parts, [])
                self.assertFalse(has_command_substitution(script))
                self.assertFalse(is_dynamic(script))
    def test_dash_heredoc_and_command_substitution(self) -> None:
        hdoc = find(parse("cat <<-EOF\n\t\ttabbed\n\tEOF\n"), Heredoc)[0]
        self.assertTrue(hdoc.strip_tabs)
        self.assertEqual(hdoc.body, "tabbed\n")
        hdocs = find(parse("cat <<A <<B\nfirst\nA\nsecond\nB\n"), Heredoc)
        self.assertEqual([h.body for h in hdocs], ["first\n", "second\n"])
        self.assertEqual([h.delimiter for h in hdocs], ["A", "B"])
        self.assertEqual(command_names(parse("x=$(cat <<EOF\nhi\nEOF\n)\necho $x")),
                         ["cat", "echo"])
    def test_unterminated_heredoc_raises(self) -> None:
        with self.assertRaises(ShellParseError) as ctx:
            parse("cat <<EOF\nbody\n")
        self.assertIn("here-document", str(ctx.exception))
        with self.assertRaises(ShellParseError):
            parse("cat <<\nEOF\n")


class TestControlStructures(unittest.TestCase):
    def test_if_else_and_elif(self) -> None:
        clause = top_command("if [ -f a ]; then echo yes; else echo no; fi")
        self.assertIsInstance(clause, IfClause)
        self.assertEqual((len(clause.condition), len(clause.then_body), len(clause.else_body)),
                         (1, 1, 1))
        self.assertEqual(command_names(parse("if [ -f a ]; then echo yes; else echo no; fi")),
                         ["[", "echo", "echo"])
        script = parse("if a; then b; elif c; then d; else e; fi")
        self.assertIsNotNone(script.body[0].items[0].elif_clause)
        self.assertEqual(command_names(script), ["a", "b", "c", "d", "e"])
        self.assertEqual(command_names(parse("if true\nthen\n  echo hi\nfi\n")), ["true", "echo"])
    def test_loops(self) -> None:
        clause = top_command("for f in a b; do echo $f; done")
        self.assertIsInstance(clause, ForClause)
        self.assertEqual((clause.var, [w.literal() for w in clause.in_words]), ("f", ["a", "b"]))
        self.assertIsNone(clause.arith)
        bare = top_command("for f; do echo $f; done")
        self.assertEqual((bare.var, bare.in_words), ("f", None))
        arith = top_command("for ((i=0;i<3;i++)); do echo $i; done")
        self.assertEqual(arith.arith, "i=0;i<3;i++")
        self.assertTrue(has_arithmetic(parse("for ((i=0;i<3;i++)); do echo $i; done")))
        loop = top_command("while read l; do echo $l; done < in.txt")
        self.assertIsInstance(loop, WhileClause)
        self.assertFalse(loop.until)
        self.assertEqual(loop.redirects[0].word.literal(), "in.txt")
        self.assertTrue(top_command("until false; do echo x; done").until)
    def test_case_clause(self) -> None:
        source = "case $x in a|b) echo ab;; c) echo c;& *) echo other;;& esac"
        script = parse(source)
        clause = top_command(source)
        self.assertIsInstance(clause, CaseClause)
        self.assertEqual(len(clause.items), 3)
        self.assertEqual([w.literal() for w in clause.items[0].patterns], ["a", "b"])
        self.assertEqual([i.terminator for i in clause.items], [";;", ";&", ";;&"])
        self.assertEqual(command_names(script), ["echo"] * 3)
        self.assertIsNone(top_command("case x in a) echo a\nesac").items[0].terminator)
    def test_functions_test_clause_and_arith(self) -> None:
        decl = top_command("f() { echo hi; }")
        self.assertIsInstance(decl, FunctionDecl)
        self.assertEqual((decl.name, decl.keyword), ("f", False))
        self.assertIsInstance(decl.body, Block)
        keyword_form = top_command("function g { echo hi; }")
        self.assertEqual((keyword_form.name, keyword_form.keyword), ("g", True))
        self.assertIsInstance(top_command("f() ( echo sub )").body, Subshell)
        clause = top_command("[[ -f x || -n $y ]]")
        self.assertIsInstance(clause, TestClause)
        self.assertEqual([w.literal() for w in clause.words], ["-f", "x", "-n", None])
        command = top_command("((i++))")
        self.assertIsInstance(command, ArithCommand)
        self.assertEqual(command.expr, "i++")
        self.assertTrue(has_arithmetic(parse("((i++))")))
    def test_time_background_and_trailing_redirects(self) -> None:
        clause = top_command("time -p ls | wc -l")
        self.assertIsInstance(clause, TimeClause)
        self.assertTrue(clause.posix)
        self.assertEqual(command_names(parse("time -p ls | wc -l")), ["ls", "wc"])
        self.assertEqual(command_names(parse("time ls")), ["ls"])
        self.assertTrue(has_background(parse("nohup run >/dev/null 2>&1 &")))
        self.assertTrue(has_background(parse("{ sleep 1 & }")))
        self.assertFalse(has_background(parse("a && b")))
        self.assertFalse(has_background(parse("cmd &> log")))
        for source in ("{ echo a; } >> log", "(echo a) > log", "if true; then echo a; fi 2>&1",
                       "while true; do break; done < in"):
            with self.subTest(source=source):
                script = parse(source)
                self.assertEqual(len(top_command(source).redirects), 1)
                self.assertTrue(has_redirect(script))


class TestErrorsAndLimits(unittest.TestCase):
    def test_error_has_line_col_and_snippet(self) -> None:
        with self.assertRaises(ShellParseError) as ctx:
            parse("echo ok\necho )\n")
        err = ctx.exception
        self.assertEqual((err.line, err.col, err.snippet), (2, 6, "echo )"))
        self.assertIn("line 2:6", str(err))
        self.assertIn("echo )", str(err))
    def test_parse_failures(self) -> None:
        bad = [
            "if then", "if [ x ]; then echo a", "echo 'unterminated", 'echo "unterminated',
            "echo $(unterminated", "echo ${unterminated", "echo )", "echo |", "| echo",
            "echo a &&", "then", "do", "fi", "esac", "}", "echo } )",
            "select x in a; do echo; done", "x=(1 2)", "time", "!",
        ]
        for source in bad:
            with self.subTest(source=source):
                with self.assertRaises(ShellParseError):
                    parse(source)
    def test_nul_crlf_and_comment_only(self) -> None:
        with self.assertRaises(ShellParseError) as ctx:
            parse("echo \x00 hi")
        self.assertEqual((ctx.exception.line, ctx.exception.col), (1, 6))
        self.assertFalse(is_empty_or_comment_only("\x00"))
        for source in ("", "   ", "\n\n", "\t", "# c\n", "  # c\n\n# d\n", "\r\n"):
            with self.subTest(source=source):
                self.assertTrue(is_empty_or_comment_only(source))
                self.assertEqual(parse(source).body, [])
        for source in ("echo", ">f", ";", "echo # c"):
            with self.subTest(source=source):
                self.assertFalse(is_empty_or_comment_only(source))
    def test_crlf_and_utf8(self) -> None:
        script = parse("echo a\r\necho b\r\necho c")
        self.assertEqual(len(script.body), 3)
        self.assertEqual((script.body[1].pos.line, script.body[1].pos.col), (2, 1))
        self.assertEqual(command_names(parse("echo a\recho b")), ["echo", "echo"])
        self.assertEqual(find(parse("cat <<EOF\r\nbody\r\nEOF\r\n"), Heredoc)[0].body, "body\n")
        cjk = parse("echo 中文 '参数' \"$变量\"")
        self.assertEqual([w.literal() for w in words(cjk)], ["echo", "中文", "参数", None])
        self.assertEqual(command_names(parse("中文 命令")), ["中文"])
        word = words(parse("echo 中文"))[1]
        self.assertEqual((word.pos.col, word.pos.end_col), (6, 8))
    def test_deep_nesting_hits_limit_not_recursion_error(self) -> None:
        with self.assertRaises(ShellParseError) as ctx:
            parse("( " * 200 + "echo deep" + " )" * 200)
        self.assertIn("嵌套", str(ctx.exception))
        with self.assertRaises(ShellParseError):
            parse("$(" * 200 + "echo deep" + ")" * 200)
        with self.assertRaises(ShellParseError):
            parse("${x:-" * 200 + "y" + "}" * 200)
        self.assertEqual(MAX_NESTING_DEPTH, 64)
        self.assertEqual(command_names(parse("( " * 10 + "echo deep" + " )" * 10)), ["echo"])
        parse("( ( ( echo x ) ) )", max_depth=3)
        with self.assertRaises(ShellParseError):
            parse("( ( ( ( echo x ) ) ) )", max_depth=3)
        with self.assertRaises(TypeError):
            parse(b"echo hi")  # type: ignore[arg-type]
        # ((...)) 是算术命令：括号由迭代扫描器配对，不产生嵌套递归。
        arith = parse("(" * 200 + "1 + 2" + ")" * 200)
        self.assertEqual(command_names(arith), [])
        self.assertTrue(has_arithmetic(arith))


class TestWalkAndQueries(unittest.TestCase):
    def test_walk_pre_post_order_and_pruning(self) -> None:
        script = parse("echo a | wc -l")
        seen: list[str] = []
        walk(script, lambda node: seen.append(node.kind))
        self.assertEqual(seen[:3], ["Script", "List", "Pipeline"])
        self.assertEqual(seen[-1], "Literal")
        post: list[str] = []
        walk(parse("echo a"), lambda node: post.append(node.kind), order="post")
        self.assertEqual((post[0], post[-1]), ("Literal", "Script"))
        with self.assertRaises(ValueError):
            walk(script, lambda node: None, order="sideways")

        pruned: list[str] = []

        def visitor(node):
            pruned.append(node.kind)
            return node.kind != "SimpleCommand"

        walk(script, visitor)
        self.assertEqual(pruned.count("SimpleCommand"), 2)
        self.assertNotIn("Word", pruned)
        self.assertNotIn("Literal", pruned)
    def test_queries(self) -> None:
        self.assertEqual([w.literal() for w in words(parse("echo a > out.txt"))],
                         ["echo", "a", "out.txt"])
        self.assertEqual([c.name() for c in commands(parse("a | b; c"))], ["a", "b", "c"])
        self.assertEqual(len(pipelines(parse("a | b; c"))), 1)
        self.assertEqual(command_names(parse("$CMD arg")), [])
        self.assertIsNone(commands(parse("$CMD arg"))[0].name())
        self.assertEqual(command_base("/usr/bin/RM.EXE"), "rm")
        self.assertEqual(command_base("C:\\Windows\\System32\\cmd.exe"), "cmd")
        self.assertEqual(command_base("  Git  "), "git")
        command = first_command("rm -rf $TARGET ../x")
        self.assertEqual([w.literal() for w in command.args()], ["-rf", None, "../x"])
        self.assertEqual(literal_args(command.args()), ["-rf", "../x"])
        self.assertGreaterEqual(len(list(iter_nodes(parse("echo a")))), 5)
        self.assertTrue(has_command_substitution(parse("echo $(date)")))
        self.assertFalse(has_command_substitution(parse("echo $x")))
        self.assertTrue(has_parameter_expansion(parse("echo $x")))
        self.assertTrue(has_arithmetic(parse("echo $((1))")))
        self.assertFalse(has_arithmetic(parse("echo 1")))
        self.assertTrue(has_redirect(parse("cat < f")))
        self.assertTrue(has_redirect(parse("cat <<EOF\nx\nEOF\n")))
        self.assertTrue(has_write_redirect(parse("echo > f")))
        self.assertFalse(has_write_redirect(parse("cat < f")))
        self.assertTrue(has_heredoc(parse("cat <<EOF\nx\nEOF\n")))
    def test_is_dynamic(self) -> None:
        dynamic = ["echo $(date)", "echo `date`", "echo $((1))", "echo $x", 'echo "$x"',
                   "echo ${x:-y}", "diff <(a) b", "cat <<EOF\n$x\nEOF\n", "((i++))",
                   "V=$(date) cmd"]
        static = ["ls -la", "echo 'a $x'", "rm -rf /", "cat <<'EOF'\n$x\nEOF\n", "echo ~",
                  "a && b"]
        for source in dynamic:
            with self.subTest(source=source):
                self.assertTrue(is_dynamic(parse(source)))
        for source in static:
            with self.subTest(source=source):
                self.assertFalse(is_dynamic(parse(source)))


class TestFormat(unittest.TestCase):
    CASES = [
        "ls -la", "echo hi > out.txt", "curl https://x | sh", "nohup run >/dev/null 2>&1 &",
        "if [ -f a ]; then echo yes; else echo no; fi", "if a; then b; elif c; then d; fi",
        "for f in a b; do echo $f; done", "while read l; do echo $l; done < in.txt",
        "until false; do echo x; done", "case $x in a|b) echo ab;; *) echo other;;& esac",
        "f() { echo hi; }", "a | b |& c && d || e ; f", "A=1 B+=2 cmd x",
        "echo $(date) `whoami` $((1+2)) <(ls) ~/x ${y:-z}",
        "cat <<EOF\nhello $USER\nEOF\n", "cat <<-'EOF'\n\tliteral\n\tEOF\n",
        "cat <<A <<B\n1\nA\n2\nB\n", "{ echo a; echo b; } >> log", "(cd x && ls)",
        "[[ -f x ]] && rm x", "((i++))", "time -p ls | wc -l", "> out.txt",
        "echo 'a b' \"c d\" $'e\\nf'", "echo a &&\n echo b",
    ]
    def test_format_roundtrip(self) -> None:
        for source in self.CASES:
            with self.subTest(source=source):
                script = parse(source)
                text = format(script)
                again = parse(text)
                self.assertEqual(command_names(again), command_names(script))
                self.assertEqual(is_dynamic(again), is_dynamic(script))
                self.assertEqual(has_background(again), has_background(script))
                self.assertEqual(has_write_redirect(again), has_write_redirect(script))
                self.assertEqual(format(again), text)
    def test_format_comments_and_heredoc_body(self) -> None:
        self.assertEqual(format(parse("")), "")
        text = format(parse("# note\necho hi\n"))
        self.assertIn("# note", text)
        self.assertEqual(command_names(parse(text)), ["echo"])
        heredoc_text = format(parse("cat <<'EOF'\nraw $x `y`\nEOF\n"))
        self.assertIn("raw $x `y`", heredoc_text)
        self.assertEqual(len(find(parse(heredoc_text), Heredoc)), 1)
        brace_text = format(parse("{ cat <<EOF; } \nbody\nEOF\n"))
        self.assertEqual(brace_text.count("EOF"), 2)
        self.assertEqual(command_names(parse(brace_text)), ["cat"])
    def _script_with(self, items, seps=("",)):
        from atri_x.shellparse.ast import List as List_, Pos, Script

        pos = Pos(0, 3, 1, 1, 1, 4)
        return Script(pos, [List_(pos, list(items), list(seps))], [])
    def test_format_rejects_unwritable_and_mismatch(self) -> None:
        from atri_x.shellparse.ast import Pos

        pos = Pos(0, 3, 1, 1, 1, 4)
        word = Word(pos, [Literal(pos, "cmd", "cmd")], "cmd")
        no_target = SimpleCommand(pos, [], [word], [Redirect(pos, ">", None)])
        with self.assertRaises(ShellFormatError):
            format(self._script_with([no_target]))
        bad_word = Word(pos, [Literal(pos, "a b", "a b")], "a b")
        with self.assertRaises(ShellFormatError):
            format(self._script_with([SimpleCommand(pos, [], [bad_word], [])]))


class TestShellRiskDimensions(unittest.TestCase):
    """``shell_risk.go`` 的每个判定维度 → 本包 API：空命令 →
    ``is_empty_or_comment_only``；解析失败 → ``parse`` 抛错；``*syntax.CmdSubst`` →
    ``has_command_substitution``；``literalWord(Args[0])`` 失败 →
    ``SimpleCommand.name() is None``；``commandBase`` → ``command_base``；
    ``literalArgs`` → ``literal_args``/``Word.literal()``；``*syntax.Redirect`` 的
    ``op`` 含 ``>`` → ``Redirect.is_write``；扁平 pipeline 相邻对 → ``command_names``。
    """
    def test_empty_and_parse_failure(self) -> None:
        for source in ("", "   ", "\n\t\n"):
            self.assertTrue(is_empty_or_comment_only(source))
        with self.assertRaises(ShellParseError):
            parse("if then")
    def test_command_substitution_detection(self) -> None:
        for source in ("echo $(date)", "echo `date`", 'echo "$(date)"',
                       "echo ${x:-$(date)}", "cat <<EOF\n$(date)\nEOF\n", "x=$(date)"):
            with self.subTest(source=source):
                self.assertTrue(has_command_substitution(parse(source)))
        self.assertFalse(has_command_substitution(parse("echo $x")))
    def test_dynamic_command_names(self) -> None:
        for source in ("$CMD arg", "$(which ls) arg", "${CMD} arg"):
            with self.subTest(source=source):
                self.assertIsNone(commands(parse(source))[0].name())
        self.assertEqual(commands(parse("ls arg"))[0].name(), "ls")
    def test_command_base_and_literal_args(self) -> None:
        for raw, want in (("RM", "rm"), ("./x/rm", "rm"), ("C:\\bin\\Git.EXE", "git"),
                          ("/usr/bin/sudo", "sudo")):
            self.assertEqual(command_base(raw), want)
        self.assertEqual(literal_args(first_command("rm -rf $TARGET /tmp/x").args()),
                         ["-rf", "/tmp/x"])
        self.assertEqual(literal_args(first_command("chmod -R 'a b' c").args()),
                         ["-R", "a b", "c"])
    def test_write_redirect_ops(self) -> None:
        for op in (">", ">>", "&>", "&>>", ">&", ">|", "<>"):
            with self.subTest(op=op):
                source = f"cmd {op} target"
                self.assertTrue(find(parse(source), Redirect)[0].is_write)
                self.assertTrue(has_write_redirect(parse(source)))
        for op in ("<", "<&", "<<<"):
            with self.subTest(op=op):
                source = f"cmd {op} target"
                self.assertFalse(find(parse(source), Redirect)[0].is_write)
                self.assertFalse(has_write_redirect(parse(source)))
        for op in ("<<", "<<-"):
            with self.subTest(op=op):
                source = f"cat {op}EOF\nbody\nEOF\n"
                self.assertFalse(find(parse(source), Redirect)[0].is_write)
                self.assertFalse(has_write_redirect(parse(source)))
    def test_redirect_target_literal_and_flat_pipeline_order(self) -> None:
        redirect = find(parse('echo hi > "out file.txt"'), Redirect)[0]
        self.assertEqual(redirect.word.literal(), "out file.txt")
        self.assertEqual(command_names(parse("curl https://x | sh")), ["curl", "sh"])
        self.assertEqual(command_names(parse("curl https://x && bash y")), ["curl", "bash"])
        self.assertEqual(command_names(parse("curl x; python3 y")), ["curl", "python3"])
    def test_pipeline_grouping_available_separately(self) -> None:
        script = parse("curl x | sh && echo done")
        self.assertEqual(command_names(script), ["curl", "sh", "echo"])
        self.assertEqual(len(pipelines(script)), 1)
        self.assertEqual(pipelines(script)[0].ops, ["|"])
        self.assertEqual(len(script.body[0].items), 2)
        self.assertIsInstance(script.body[0].items[0], Pipeline)
        self.assertIsInstance(script.body[0].items[1], SimpleCommand)


class TestShellSandboxDimensions(unittest.TestCase):
    """``shell_sandbox.go`` 的每个判定维度 → 本包 API：解析失败 → ``parse`` 抛错；
    ``op`` 含 ``>``/``<>`` → ``Redirect.is_write``；``redir.Word`` →
    ``Redirect.word``（``literal() is None`` 即动态目标）；``literalWord(Args[0])`` →
    ``SimpleCommand.name()``；``commandBase(name)=="cd"`` → ``command_base`` +
    ``command_names``；白名单路径命令的参数 → ``SimpleCommand.args()[i].literal()``
    （``-`` 开头由调用方跳过）。
    """

    SANDBOX_PATH_COMMANDS = ["cat", "head", "tail", "less", "more", "ls", "touch", "mkdir",
                             "rm", "rmdir", "cp", "mv", "tee", "find", "grep", "sed"]
    def test_parse_failure_rejects(self) -> None:
        with self.assertRaises(ShellParseError):
            parse("for x in; do")
    def test_redirect_checks(self) -> None:
        self.assertTrue(find(parse("echo hi > report.txt"), Redirect)[0].is_write)
        self.assertFalse(find(parse("cat < in.txt"), Redirect)[0].is_write)
        self.assertFalse(has_write_redirect(parse("cat <<EOF\nx\nEOF\n")))
        self.assertEqual(find(parse("echo hi > ../report.txt"), Redirect)[0].word.literal(),
                         "../report.txt")
        self.assertIsNone(find(parse('echo hi > "$file"'), Redirect)[0].word.literal())
    def test_command_name_and_cd(self) -> None:
        self.assertEqual(command_base(commands(parse("cd .."))[0].name()), "cd")
        self.assertIsNone(commands(parse("$DIR/x"))[0].name())
        for name in ("cd", "chdir", "pushd", "popd", "set-location", "sl"):
            with self.subTest(name=name):
                self.assertEqual(command_base(commands(parse(f"{name} .."))[0].name()), name)
        self.assertIn("cd", [command_base(n) for n in command_names(parse("(cd .. && ls)"))])
        self.assertIn("cd", [command_base(n) for n in command_names(parse("echo x | cd /tmp"))])
    def test_path_command_arguments(self) -> None:
        for name in self.SANDBOX_PATH_COMMANDS:
            with self.subTest(name=name):
                command = first_command(f"{name} ../x")
                self.assertEqual(command_base(command.name()), name)
                self.assertEqual([w.literal() for w in command.args()], ["../x"])
                self.assertIsNone(first_command(f'{name} "$HOME/x"').args()[0].literal())
                self.assertIsNone(first_command(f"{name} $(pwd)/x").args()[0].literal())
        command = first_command("rm -rf ../x")
        self.assertEqual([w.literal() for w in command.args()], ["-rf", "../x"])
        self.assertTrue(command.args()[0].literal().startswith("-"))
    def test_path_forms_and_quoting(self) -> None:
        cases = {"cat ~/x": "~/x", "cat C:/Windows/System32": "C:/Windows/System32",
                 "cat //server/share": "//server/share", "cat /etc/passwd": "/etc/passwd",
                 "cat '/etc/passwd'": "/etc/passwd", 'cat "/etc/passwd"': "/etc/passwd",
                 "cat ..\\report.txt": "..report.txt"}
        for source, want in cases.items():
            with self.subTest(source=source):
                self.assertEqual(first_command(source).args()[0].literal(), want)
    def test_fd_duplication_and_empty_commands(self) -> None:
        redirect = find(parse("echo hi 2>&1"), Redirect)[0]
        self.assertEqual((redirect.fd, redirect.is_write, redirect.word.literal()),
                         ("2", True, "1"))
        command = first_command("V=1")
        self.assertEqual((command.words, command.name()), ([], None))
        self.assertEqual(command_names(parse("V=1")), [])
        script = parse("> out.txt")
        self.assertEqual(commands(script)[0].words, [])
        self.assertTrue(has_write_redirect(script))


if __name__ == "__main__":
    unittest.main()