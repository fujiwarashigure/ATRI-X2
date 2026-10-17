"""``atri_x.atriyph`` 的单元测试。

逐条移植 ``internal/atriyph/parser_test.go``（41 个 Go 测试函数 / 全部子用例，
共 105 个断言用例），并额外覆盖 Python 侧 API（``lint``/``lint_warnings``/
``format_diagnostics``/``normalize_text``/``rule_card`` 常量）。

Go 的 ``(Document, error)`` 在 Python 里是「返回 Document，失败抛
``AtriyphError``」，因此断言口径为「异常的 str 包含 Go 侧的同一子串」。
"""

from __future__ import annotations

import unittest

from atri_x.atriyph import (
    DIAGNOSTIC_ERROR,
    DIAGNOSTIC_WARNING,
    FORMAT,
    SKILL_FILE_NAME,
    AtriyphError,
    Diagnostic,
    Document,
    Header,
    Step,
    error_diagnostics,
    format_diagnostics,
    lint,
    lint_warnings,
    normalize_text,
    parse,
    parse_header,
    parse_skill,
    parse_task,
    rule_card,
    validate_skill,
    validate_task,
    warning_diagnostics,
)


def assert_error_contains(test: unittest.TestCase, raw: str, want: str, kind: str = "skill", name: str = "") -> None:
    with test.assertRaises(AtriyphError) as ctx:
        parse_task(raw, name) if kind == "task" else parse_skill(raw, name)
    test.assertIn(want, str(ctx.exception))


class TestAtriyphValidDocuments(unittest.TestCase):
    """Go：TestParseSkillValidDocument / TestParseTaskValidDocument / TestParseAllowsNewAssignments。"""

    def test_parse_skill_valid_document(self) -> None:
        doc = parse_skill(
            "#skill weather - 查天气\n"
            "<- $city:str!\n"
            "-> $remind:str\n"
            "** 根据查询结果判断\n"
            "~ 编造天气\n"
            "$q:str = $city + 天气\n"
            "$w = @tool web_search(query=$q)\n"
            "=> $rain = $w 是否下雨 ? 是 : 否\n",
            "weather",
        )
        self.assertEqual((doc.kind, doc.name), ("skill", "weather"))

    def test_parse_task_valid_document(self) -> None:
        doc = parse_task("#task daily_weather - 每日天气\n> 查询天气\n", "daily_weather")
        self.assertEqual((doc.kind, doc.name), ("task", "daily_weather"))
        doc2, diagnostics = validate_task("#task daily_weather\n> 查询天气\n", "daily_weather")
        self.assertEqual(doc2, doc)
        self.assertEqual(error_diagnostics(diagnostics), [])

    def test_parse_allows_new_assignments(self) -> None:
        raw = (
            "#skill vars\n"
            "$you = atri-x\n"
            "$me:str = 用户\n"
            "$count:int = 1 + 2\n"
            "$text:str = 1 + 2\n"
            "$url:str = http://example.com\n"
        )
        parse_skill(raw, "vars")

    def test_parse_only_treats_line_start_comment_as_comment(self) -> None:
        raw = (
            "// leading comment\n"
            "#skill comments - header desc\n"
            "$you = 芙莉丝 // this is literal text now\n"
            "$url:str = http://example.com\n"
            "?if($you 存在) {\n"
            "}\n"
        )
        parse_skill(raw, "comments")

    def test_parse_derive_ternary(self) -> None:
        raw = (
            "#skill ternary\n"
            "=> $rain = $w 是否下雨 ? 是 : 否\n"
            "=> $city:str = $input 提到的城市\n"
            "=> $title = 标题：天气提醒\n"
            "=> $status = $w 下雨?是:否\n"
        )
        parse_skill(raw, "ternary")

    def test_parse_valid_control_blocks(self) -> None:
        raw = (
            "#task flow\n"
            "?if($weather 下雨) {\n"
            "  ?if($user 在外面) {\n"
            "    > 提醒带伞\n"
            "  }\n"
            "  ?else {\n"
            "    > 不提醒\n"
            "  }\n"
            "}\n"
            "?else{\n"
            "  each($day in $days, limit=3) {\n"
            "    @skill weather_day(city=$city, day=$day)\n"
            "  }\n"
            "}\n"
        )
        parse_task(raw, "flow")

    def test_parse_step_blocks(self) -> None:
        raw = (
            "#skill weather - 查天气\n"
            "<- $city:str!\n"
            "-> $remind:str\n"
            "step fetch {\n"
            "  $q:str = $city + 天气\n"
            "  $w = @tool web_search(query=$q)\n"
            "}\n"
            "step decide {\n"
            "  => $rain = $w 是否下雨 ? 是 : 否\n"
            "  ?if($rain) {\n"
            "    > 记得带伞\n"
            "  }\n"
            "  ?else {\n"
            "    > 今天不用带伞\n"
            "  }\n"
            "}\n"
            "> 完成\n"
        )
        doc = parse_skill(raw, "weather")
        self.assertEqual(len(doc.steps), 2)
        self.assertEqual((doc.steps[0].name, doc.steps[0].line), ("fetch", 4))
        self.assertEqual((doc.steps[1].name, doc.steps[1].line), ("decide", 8))

    def test_parse_step_numeric_name(self) -> None:
        doc = parse_task("#task nums\nstep 1: > first\nstep 2: > second\n", "nums")
        self.assertEqual([s.name for s in doc.steps], ["1", "2"])

    def test_parse_step_mixes_with_bare_statements(self) -> None:
        raw = (
            "#task mix\n"
            "step fetch: $w = @tool web_search(query=$q)\n"
            "each($day in $days, limit=3) {\n"
            "  @skill weather_day(city=$city, day=$day)\n"
            "}\n"
            "> done\n"
        )
        parse_task(raw, "mix")

    def test_parse_step_with_control_flow_inside(self) -> None:
        raw = (
            "#task flow\n"
            "step decide {\n"
            "  ?if($rain) {\n"
            "    each($d in $days, limit=2) {\n"
            "      > $d\n"
            "    }\n"
            "  }\n"
            "  ?else {\n"
            "    > skip\n"
            "  }\n"
            "}\n"
        )
        parse_task(raw, "flow")

    def test_parse_step_single_line(self) -> None:
        raw = (
            "#skill weather - 查天气\n"
            "<- $city:str!\n"
            "-> $remind:str\n"
            "step fetch: $w = @tool web_search(query=$city)\n"
            "step notify: > 已通知 $city\n"
        )
        doc = parse_skill(raw, "weather")
        self.assertEqual([s.name for s in doc.steps], ["fetch", "notify"])
        self.assertEqual([s.line for s in doc.steps], [4, 5])

    def test_parse_step_single_line_mixed_with_block(self) -> None:
        raw = (
            "#task mix\n"
            "step fetch: $w = @tool web_search(query=$q)\n"
            "step decide {\n"
            "  => $rain = $w 是否下雨 ? 是 : 否\n"
            "  ?if($rain) {\n"
            "    > 带伞\n"
            "  }\n"
            "}\n"
        )
        parse_task(raw, "mix")


class TestAtriyphRejections(unittest.TestCase):
    """Go：TestParseRejects*（header/kind/name/IO/assignment/call/output/brace）。"""

    def test_parse_rejects_wrong_document_kind(self) -> None:
        assert_error_contains(
            self, "#task daily\n", 'document kind "task" does not match "skill"', "skill", "daily"
        )
        assert_error_contains(
            self, "#skill weather\n", 'document kind "skill" does not match "task"', "task", "weather"
        )

    def test_parse_rejects_missing_or_invalid_header(self) -> None:
        cases = [
            ("empty", "\n  \n", "atriyph is required", "skill", ""),
            ("missing header", ":desc x\n> do\n", "first statement must be #skill <name>", "skill", ""),
            ("invalid kind", "#cron daily\n", "first statement must be #task <name>", "task", ""),
            ("invalid name", "#skill BadName\n", "first statement must be #skill <name>", "skill", ""),
        ]
        for name, raw, want, kind, expected in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, kind, expected)

    def test_parse_rejects_name_mismatch(self) -> None:
        assert_error_contains(
            self, "#skill weather\n", '#skill name "weather" does not match "other"', "skill", "other"
        )

    def test_parse_rejects_reserved_variable_redefinition(self) -> None:
        for line in ("$user = 用户", "$assistant:str = atri-x", "=> $user = 输入里的用户"):
            with self.subTest(line=line):
                with self.assertRaises(AtriyphError) as ctx:
                    parse_skill("#skill vars\n" + line + "\n", "vars")
                message = str(ctx.exception)
                self.assertIn("reserved variable", message)
                self.assertIn("line 2", message)

    def test_parse_rejects_invalid_if_else_syntax(self) -> None:
        cases = [
            ("if missing paren", "#task bad\n?if $rain {\n}\n", "line 2: ?if must be ?if(condition) {"),
            ("if missing brace", "#task bad\n?if($rain)\n", "line 2: ?if must be ?if(condition) {"),
            ("else missing question", "#task bad\nelse {\n}\n", "line 2: else must be ?else{"),
            ("else spaced question", "#task bad\n? else {\n}\n", "line 2: else must be ?else{"),
            ("else trailing text", "#task bad\n?else now {\n}\n", "line 2: ?else must be ?else{"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")

    def test_parse_rejects_invalid_each_syntax(self) -> None:
        cases = [
            ("missing limit", "#task bad\neach($x in $xs) {\n}\n", "line 2: each must be each($item in $items, limit=N) {"),
            ("zero limit", "#task bad\neach($x in $xs, limit=0) {\n}\n", "line 2: each limit must be positive"),
            ("non variable item", "#task bad\neach(x in $xs, limit=1) {\n}\n", "line 2: each must be each($item in $items, limit=N) {"),
            ("non variable list", "#task bad\neach($x in xs, limit=1) {\n}\n", "line 2: each must be each($item in $items, limit=N) {"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")

    def test_parse_rejects_brace_mismatch(self) -> None:
        assert_error_contains(self, "#task bad\n}\n", "line 2: unexpected }", "task", "bad")
        assert_error_contains(self, "#task bad\n?if($rain) {\n", "unclosed { block", "task", "bad")

    def test_parse_rejects_old_syntax_and_bare_natural_language(self) -> None:
        cases = [
            ("old desc", "#skill bad\n:desc x\n", "line 2: line must start with a valid Atriyph token"),
            ("old input", "#skill bad\n<- city text required\n", "line 2: input must be <- $name:type, <- $name:type! or <- $name:type?"),
            ("bare language", "#skill bad\n根据情况提醒\n", "line 2: line must start with a valid Atriyph token"),
            ("duplicate header", "#skill bad\n#task other\n", "line 2: header must appear only once"),
            ("target output unsupported", "#skill bad\n>$user hi\n", "line 2: output must be > text"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "skill", "bad")

    def test_parse_rejects_invalid_derive_ternary(self) -> None:
        cases = [
            ("missing false", "#skill bad\n=> $x = 条件 ? 是\n"),
            ("empty condition", "#skill bad\n=> $x = ? 是 : 否\n"),
            ("empty true", "#skill bad\n=> $x = 条件 ? : 否\n"),
            ("empty false", "#skill bad\n=> $x = 条件 ? 是 : \n"),
        ]
        for name, raw in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, "line 2: ternary must be condition ? true : false", "skill", "bad")

    def test_parse_header_description_boundaries(self) -> None:
        parse_skill("#skill weather  -  查天气\n", "weather")
        for name, raw in (("empty desc", "#skill weather -\n"), ("old inline comment desc", "#skill weather // 查天气\n")):
            with self.subTest(name=name):
                assert_error_contains(self, raw, "first statement must be #skill <name>", "skill", "weather")

    def test_parse_io_boundaries(self) -> None:
        valid = (
            "#skill io\n"
            "<- $required:str!\n"
            "<- $optional:str?\n"
            "<- $plain:str\n"
            "-> $result:str\n"
        )
        parse_skill(valid, "io")
        cases = [
            ("input missing dollar", "<- city:str!", "input must be"),
            ("input missing type", "<- $city", "input must be"),
            ("input empty type", "<- $city:", "input must be"),
            ("input numeric type", "<- $city:1", "input must be"),
            ("input double marker", "<- $city:str!!", "input must be"),
            ("output required marker", "-> $result:str!", "output must be"),
            ("output optional marker", "-> $result:str?", "output must be"),
        ]
        for name, line, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, "#skill bad\n" + line + "\n", want, "skill", "bad")

    def test_parse_assignment_boundaries(self) -> None:
        cases = [
            ("empty value", "$x =", "assignment must be"),
            ("typed empty value", "$x:int =", "assignment must be"),
            ("empty type", "$x: = a", "assignment must be"),
            ("numeric type", "$x:1 = a", "assignment must be"),
            ("reserved typed", "$user:int = 1", "reserved variable"),
        ]
        for name, line, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, "#skill bad\n" + line + "\n", want, "skill", "bad")

    def test_parse_control_block_boundaries(self) -> None:
        parse_task("#task ok\n?if($x) {\n}\n?else{\n}\n", "ok")
        cases = [
            ("empty if", "#task bad\n?if() {\n}\n", "line 2: ?if must be ?if(condition) {"),
            ("if trailing text", "#task bad\n?if($x) { text\n", "line 2: ?if must be ?if(condition) {"),
            ("else spaced question", "#task bad\n? else {\n}\n", "line 2: else must be ?else{"),
            ("orphan else", "#task bad\n?else {\n}\n", "line 2: ?else must immediately follow a closed ?if block"),
            ("double else", "#task bad\n?if($x) {\n}\n?else {\n}\n?else {\n}\n", "line 6: ?else must immediately follow a closed ?if block"),
            ("else after output", "#task bad\n?if($x) {\n}\n> done\n?else {\n}\n", "line 5: ?else must immediately follow a closed ?if block"),
            ("close else same line", "#task bad\n} ?else {\n", "line 2: line must start with a valid Atriyph token"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")

    def test_parse_call_boundaries(self) -> None:
        valid = (
            "#skill calls\n"
            "@tool web_search()\n"
            "@tool web_search(query=$q)\n"
            "@skill weather-day(city=$city)\n"
        )
        parse_skill(valid, "calls")
        cases = [
            ("tool missing name", "@tool"),
            ("tool uppercase", "@tool WebSearch(query=$q)"),
            ("tool missing parens", "@tool web_search"),
            ("tool missing parens around args", "@tool web_search query=$q"),
            ("tool missing value", "@tool web_search(query=)"),
            ("tool missing equals", "@tool web_search(query)"),
            ("tool duplicate comma", "@tool web_search(query=$q,,limit=1)"),
            ("tool bad key", "@tool web_search(1query=$q)"),
            ("tool nested paren", "@tool web_search(query=foo(bar))"),
            ("skill missing name", "@skill"),
        ]
        for name, line in cases:
            with self.subTest(name=name):
                assert_error_contains(self, "#skill bad\n" + line + "\n", "call must be @tool/@skill name(k=v)", "skill", "bad")

    def test_parse_output_boundaries(self) -> None:
        parse_skill("#skill out\n>\n> 文本\n", "out")
        for line in (">text", ">$user text"):
            with self.subTest(line=line):
                assert_error_contains(self, "#skill bad\n" + line + "\n", "output must be > text", "skill", "bad")

    def test_parse_rejects_empty_constraint_and_forbid(self) -> None:
        cases = [
            ("empty constraint", "**", "** must have text"),
            ("blank constraint", "**   ", "** must have text"),
            ("empty forbid", "~", "~ must have text"),
            ("blank forbid", "~   ", "~ must have text"),
        ]
        for name, line, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, "#skill bad\n" + line + "\n", want, "skill", "bad")

    def test_parse_rejects_invalid_step_syntax(self) -> None:
        cases = [
            ("missing brace", "#task bad\nstep fetch\n", "line 2: step must be step <name> {"),
            ("empty name", "#task bad\nstep {\n}\n", "line 2: step must be step <name> {"),
            ("space in name", "#task bad\nstep a b {\n}\n", "line 2: step must be step <name> {"),
            ("uppercase name", "#task bad\nstep Fetch {\n}\n", "line 2: step name must be"),
            ("dot in name", "#task bad\nstep 1.2 {\n}\n", "line 2: step name must be"),
            ("leading underscore", "#task bad\nstep _x {\n}\n", "line 2: step name must be"),
            ("leading dash", "#task bad\nstep -x {\n}\n", "line 2: step name must be"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")

    def test_parse_rejects_nested_step(self) -> None:
        raw = "#task bad\nstep outer {\n  step inner {\n  }\n}\n"
        assert_error_contains(self, raw, "line 3: step blocks must not be nested", "task", "bad")

    def test_parse_rejects_empty_step_block(self) -> None:
        assert_error_contains(self, "#task bad\nstep empty {\n}\n", "line 3: step block must not be empty", "task", "bad")

    def test_parse_rejects_duplicate_step_name(self) -> None:
        raw = "#task bad\nstep fetch {\n  > a\n}\nstep fetch {\n  > b\n}\n"
        assert_error_contains(self, raw, "line 5: step name fetch already used", "task", "bad")

    def test_parse_rejects_step_block_with_single_statement(self) -> None:
        cases = [
            ("single output", "#task bad\nstep a {\n  > x\n}\n", "line 4: step block with single statement should use single-line form"),
            ("single if", "#task bad\nstep a {\n  ?if($x) {\n    > y\n  }\n}\n", "line 6: step block with single statement should use single-line form"),
            ("single each", "#task bad\nstep a {\n  each($x in $xs, limit=1) {\n    > y\n  }\n}\n", "line 6: step block with single statement should use single-line form"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")

    def test_parse_rejects_step_single_line_invalid_body(self) -> None:
        cases = [
            ("empty body", "#task bad\nstep a:\n", "line 2: step single-line body must not be empty"),
            ("if body", "#task bad\nstep a: ?if($x)\n", "line 2: step single-line body must be a simple statement"),
            ("each body", "#task bad\nstep a: each($x in $xs, limit=1)\n", "line 2: step single-line body must be a simple statement"),
            ("step body", "#task bad\nstep a: step b: > x\n", "line 2: step single-line body must be a simple statement"),
            ("close body", "#task bad\nstep a: }\n", "line 2: step single-line body must be a simple statement"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")

    def test_parse_rejects_step_single_line_duplicate_name(self) -> None:
        raw = "#task bad\nstep fetch: > a\nstep fetch: > b\n"
        assert_error_contains(self, raw, "line 3: step name fetch already used", "task", "bad")

    def test_parse_rejects_step_single_line_bad_name(self) -> None:
        cases = [
            ("empty name", "#task bad\nstep : > x\n", "line 2: step must be step <name>"),
            ("space name", "#task bad\nstep a b: > x\n", "line 2: step must be step <name>"),
            ("uppercase", "#task bad\nstep Fetch: > x\n", "line 2: step name must be"),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                assert_error_contains(self, raw, want, "task", "bad")


class TestAtriyphDiagnostics(unittest.TestCase):
    """Go：TestParseWarnsTextSlotEndingColon / TestParseReportsMultipleDiagnostics。"""

    def test_parse_warns_text_slot_ending_colon(self) -> None:
        raw = "#skill headings\n** 清单：\n~ 禁止事项:\n"
        parse_skill(raw, "headings")
        _, diagnostics = validate_skill(raw, "headings")
        warnings = warning_diagnostics(diagnostics)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[0].severity, DIAGNOSTIC_WARNING)
        self.assertEqual(warnings[0].line, 2)
        self.assertIn("ends with a colon", warnings[0].message)
        self.assertEqual(warnings[1].severity, DIAGNOSTIC_WARNING)
        self.assertEqual(warnings[1].line, 3)
        self.assertIn("ends with a colon", warnings[1].message)

    def test_parse_reports_multiple_diagnostics(self) -> None:
        raw = "#task bad\n$user = x\n?if $rain {\neach($x in $xs) {\n"
        with self.assertRaises(AtriyphError) as ctx:
            parse_task(raw, "bad")
        message = str(ctx.exception)
        for want in ("line 2: reserved variable", "line 3: ?if must", "line 4: each must", "unclosed { block"):
            self.assertIn(want, message)
        self.assertTrue(message.startswith("invalid Atriyph:\n- "))

    def test_error_diagnostics_treats_empty_severity_as_error(self) -> None:
        diagnostics = [
            Diagnostic(0, "atriyph is required", ""),
            Diagnostic(2, "warn", DIAGNOSTIC_WARNING),
            Diagnostic(3, "err", DIAGNOSTIC_ERROR),
        ]
        self.assertEqual([d.message for d in error_diagnostics(diagnostics)], ["atriyph is required", "err"])
        self.assertEqual([d.message for d in warning_diagnostics(diagnostics)], ["warn"])

    def test_format_diagnostics(self) -> None:
        self.assertEqual(
            format_diagnostics([Diagnostic(2, "a"), Diagnostic(0, "b"), Diagnostic(0, "c", DIAGNOSTIC_WARNING)]),
            "line 2: a\nb\nc",
        )

    def test_lint_never_raises_and_returns_all_diagnostics(self) -> None:
        raw = "#task bad\n$user = x\n** 标题：\n"
        diagnostics = lint(raw, "task", "bad")
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0].severity, DIAGNOSTIC_ERROR)
        self.assertEqual(diagnostics[0].line, 2)
        self.assertEqual(diagnostics[1].severity, DIAGNOSTIC_WARNING)
        self.assertEqual([d.line for d in lint_warnings(raw, "task", "bad")], [3])

    def test_lint_on_invalid_document(self) -> None:
        diagnostics = lint("", "skill", "")
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].message, "atriyph is required")

    def test_parse_dispatches_by_kind(self) -> None:
        self.assertEqual(parse("#task t\n> x\n", "task", "t").kind, "task")
        self.assertEqual(parse("#skill s\n> x\n", "skill", "s").kind, "skill")

    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("#skill a\r\n> x\r\n"), "#skill a\n> x\n")
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text("  \n"), "")


class TestAtriyphHeader(unittest.TestCase):
    """Go：TestParseHeader / TestParseHeaderErrors。"""

    def test_parse_header(self) -> None:
        cases = [
            ("skill with desc", "#skill weather - 查天气\n<- $city:str!\n", Header("skill", "weather", "查天气")),
            ("task with desc", "// note\n#task daily - 每日天气\n> do\n", Header("task", "daily", "每日天气")),
            ("no desc", "#skill foo\n", Header("skill", "foo", "")),
            ("blank lines before header", "\n\n#skill foo - Foo\n", Header("skill", "foo", "Foo")),
        ]
        for name, raw, want in cases:
            with self.subTest(name=name):
                got = parse_header(raw)
                self.assertEqual(got, want)

    def test_parse_header_errors(self) -> None:
        cases = [
            ("empty", "\n  \n"),
            ("missing header", ":desc x\n> do\n"),
            ("invalid name", "#skill BadName\n"),
            ("only comments", "// just a comment\n"),
        ]
        for name, raw in cases:
            with self.subTest(name=name):
                with self.assertRaises(AtriyphError):
                    parse_header(raw)


class TestAtriyphRules(unittest.TestCase):
    """Go：TestRuleCardExampleParses，外加常量与 lint 消费路径。"""

    def test_rule_card_example_parses(self) -> None:
        card = rule_card()
        marker = "例：\n"
        idx = card.find(marker)
        self.assertGreaterEqual(idx, 0)
        example = card[idx + len(marker) :].strip()
        parse_skill(example, "weather")

    def test_rule_card_mentions_tokens(self) -> None:
        card = rule_card()
        for token in ("#skill/#task", "?if", "?else", "each", "step", "@tool", "@skill"):
            self.assertIn(token, card)

    def test_format_and_file_name_constants(self) -> None:
        self.assertEqual(SKILL_FILE_NAME, "SKILL.atriyph")
        self.assertEqual(FORMAT, "atriyph")

    def test_document_defaults(self) -> None:
        doc = Document()
        self.assertEqual((doc.kind, doc.name, doc.steps), ("", "", []))
        self.assertEqual(Step("a", 1), Step("a", 1))


if __name__ == "__main__":
    unittest.main()
