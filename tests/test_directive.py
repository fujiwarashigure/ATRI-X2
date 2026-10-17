"""``atri_x.directive`` 的单元测试。

对应上游 ``internal/directive`` 的行为。
"""

from __future__ import annotations

import unittest

from atri_x import directive as d


class TestMatches(unittest.TestCase):
    def test_tool_matches_half_width(self) -> None:
        text = "帮我 @tool:read_file 一下"
        got = d.tool_matches(text)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "read_file")
        self.assertEqual(text[got[0].start:got[0].end], "@tool:read_file")

    def test_tool_matches_full_width_colon(self) -> None:
        got = d.tool_matches("用 @t：web_search 查")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "web_search")

    def test_long_option_wins_in_alternation(self) -> None:
        # `@tool:` 不能被当成 `@t:` + "ool:..."
        got = d.tool_matches("@tool:abc")
        self.assertEqual([m.name for m in got], ["abc"])
        self.assertNotIn("ool", [m.name for m in got])

    def test_name_charset_excludes_invalid(self) -> None:
        # 空格结束名字，后面的内容不进名字
        got = d.tool_matches("@tool:read_file 中文")
        self.assertEqual([m.name for m in got], ["read_file"])
        # `+` 不在允许字符集里，正则在那里停下 —— Go 的 FindAll 也是同样的
        # 子串语义（不要求整个 token 匹配），因此命中 "a" 而不是无命中。
        self.assertEqual([m.name for m in d.tool_matches("@tool:a+b")], ["a"])
        # 完全不含合法字符时不命中
        self.assertEqual(d.tool_matches("@tool:+"), [])

    def test_all_namespaces(self) -> None:
        self.assertEqual([m.name for m in d.skill_matches("@skill:foo")], ["foo"])
        self.assertEqual([m.name for m in d.character_matches("@char:atri")], ["atri"])
        self.assertEqual([m.name for m in d.model_profile_matches("@model:fast")], ["fast"])
        self.assertEqual([m.name for m in d.image_profile_matches("@img:v2")], ["v2"])
        self.assertEqual([m.name for m in d.tool_profile_matches("@use:minimal")], ["minimal"])

    def test_namespaced_patterns_do_not_cross_match(self) -> None:
        # `@m:` 不应被 `@model:` 的 pattern 命中后再看 @c: 之类
        self.assertEqual(d.model_profile_matches("@c:atri"), [])
        # `@image:` 短名是 `@img:`，`@i:` 不是合法前缀
        self.assertEqual(d.image_profile_matches("@i:v2"), [])

    def test_multiple_matches_offsets_are_ordered(self) -> None:
        text = "@tool:a 和 @t:b 以及 @tool:c"
        got = d.tool_matches(text)
        self.assertEqual([m.name for m in got], ["a", "b", "c"])
        starts = [m.start for m in got]
        self.assertEqual(starts, sorted(starts))


class TestStripToolMatches(unittest.TestCase):
    def test_removes_selected_and_collapses_whitespace(self) -> None:
        text = "请   @tool:a   把文件  读一下"
        matches = d.tool_matches(text)
        out = d.strip_tool_matches(text, matches, [True])
        self.assertEqual(out, "请 把文件 读一下")

    def test_keeps_unselected(self) -> None:
        text = "@tool:a @tool:b"
        matches = d.tool_matches(text)
        out = d.strip_tool_matches(text, matches, [False, True])
        self.assertEqual(out, "@tool:a")

    def test_all_removed_gives_empty(self) -> None:
        text = "@tool:a"
        matches = d.tool_matches(text)
        self.assertEqual(d.strip_tool_matches(text, matches, [True]), "")

    def test_shorter_remove_list_is_safe(self) -> None:
        text = "@tool:a @tool:b"
        matches = d.tool_matches(text)
        # remove 比 matches 短时，缺失项按不删除处理，不应抛异常
        self.assertEqual(d.strip_tool_matches(text, matches, [True]), "@tool:b")


class TestCompletionTokens(unittest.TestCase):
    def test_prefix_only(self) -> None:
        tok = d.parse_tool_completion_token("hello @to", 9)
        self.assertTrue(tok.ok)
        self.assertTrue(tok.prefix_only)
        self.assertEqual(tok.prefix, d.TOOL_PREFIX)
        self.assertEqual(tok.start, 6)

    def test_with_query(self) -> None:
        text = "hello @tool:read"
        tok = d.parse_tool_completion_token(text, len(text))
        self.assertTrue(tok.ok)
        self.assertFalse(tok.prefix_only)
        self.assertEqual(tok.query, "read")
        self.assertEqual(tok.prefix, d.TOOL_PREFIX)

    def test_short_prefix_with_query(self) -> None:
        text = "@t:web"
        tok = d.parse_tool_completion_token(text, len(text))
        self.assertTrue(tok.ok)
        self.assertEqual(tok.query, "web")
        self.assertEqual(tok.prefix, d.TOOL_SHORT_PREFIX)

    def test_full_width_prefix_only(self) -> None:
        # 完整写出的全角短前缀（尚未输入名字）：query 为空但不是 prefix_only
        tok = d.parse_tool_completion_token("@t：", 3)
        self.assertTrue(tok.ok)
        self.assertFalse(tok.prefix_only)
        self.assertEqual(tok.query, "")
        self.assertEqual(tok.prefix, d.TOOL_SHORT_FULL)
        # 只输入了前缀的一部分才是 prefix_only
        partial = d.parse_tool_completion_token("@t", 2)
        self.assertTrue(partial.ok)
        self.assertTrue(partial.prefix_only)

    def test_not_a_token(self) -> None:
        self.assertFalse(d.parse_tool_completion_token("hello", 5).ok)
        self.assertFalse(d.parse_tool_completion_token("a@tool:x", 8).ok)

    def test_invalid_name_char_aborts(self) -> None:
        self.assertFalse(d.parse_tool_completion_token("@tool:a/b", 9).ok)

    def test_cursor_clamped(self) -> None:
        text = "@tool:x"
        # cursor 越界上下界都被夹到文本末尾（与 Go 一致）
        self.assertTrue(d.parse_tool_completion_token(text, 999).ok)
        self.assertTrue(d.parse_tool_completion_token(text, -5).ok)

    def test_token_start_stops_at_whitespace(self) -> None:
        text = "a b @tool:x"
        tok = d.parse_tool_completion_token(text, len(text))
        self.assertEqual(tok.start, 4)

    def test_skill_token(self) -> None:
        tok = d.parse_skill_completion_token("@s:abc", 6)
        self.assertTrue(tok.ok)
        self.assertEqual(tok.query, "abc")
        self.assertEqual(tok.prefix, d.SKILL_SHORT_PREFIX)

    def test_character_token_prefix_only(self) -> None:
        tok = d.parse_character_completion_token("@ch", 3)
        self.assertTrue(tok.ok)
        self.assertTrue(tok.prefix_only)
        self.assertEqual(tok.prefix, d.CHARACTER_PREFIX)

    def test_is_tool_name_char(self) -> None:
        for ch in "aZ09_-.":
            self.assertTrue(d.is_tool_name_char(ch))
        for ch in " +/：@":
            self.assertFalse(d.is_tool_name_char(ch))


if __name__ == "__main__":
    unittest.main()
