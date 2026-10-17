"""``atri_x.groupkb`` 的单元测试。

逐条移植上游 ``internal/groupkb/groupkb_test.go`` 的 4 个测试函数，并按任务要求
补齐：全角转半角、空白折叠（含首字符空白）、标点集合逐字符、``max_scan_runes``
按 rune 截断、三种模式优先级、同模式内存储顺序、keywords 全空不命中、
disabled entry 跳过、``normalize_match_mode`` 的别名与 default。

临时目录在仓库根下用 ``os.makedirs`` 创建（不用 ``tempfile``）。
"""

from __future__ import annotations

import os
import unittest

from atri_x.config.state import GroupKnowledgeEntry
from atri_x.groupkb import (
    MatchContains,
    MatchExact,
    MatchKeywords,
    PUNCTUATION,
    match,
    normalize,
    normalize_match_mode,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def entry(**kwargs) -> GroupKnowledgeEntry:
    return GroupKnowledgeEntry(**kwargs)


class TestGroupKbUpstream(unittest.TestCase):
    """上游 ``groupkb_test.go`` 的 4 个用例。"""

    def test_normalize(self) -> None:
        """移植 ``TestNormalize``。"""
        cases = {
            "  怎么绑定？ ": "怎么绑定",
            "Hello, WORLD!": "hello world",
            "ＡＢＣ　１２３": "abc 123",
            "a   b\n\tc": "a b c",
            "（测试）": "测试",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), want)

    def test_match_exact_and_aliases(self) -> None:
        """移植 ``TestMatchExactAndAliases``。"""
        entries = [
            entry(
                id="k1",
                question="怎么绑定？",
                answer="在设置里绑定",
                aliases=["如何绑定", "绑定方法"],
            )
        ]
        for text in ("怎么绑定", " 如何绑定 ", "绑定方法！"):
            with self.subTest(text=text):
                found, ok = match(entries, text, 100)
                self.assertTrue(ok)
                self.assertEqual(found.id, "k1")
        found, ok = match(entries, "怎么绑定账号", 100)
        self.assertFalse(ok, "exact match must not match extra text")
        self.assertIsNone(found)

    def test_match_contains_and_keywords(self) -> None:
        """移植 ``TestMatchContainsAndKeywords``。"""
        entries = [
            entry(id="contains", question="绑定", answer="A", match=MatchContains),
            entry(
                id="keywords",
                question="退款",
                answer="B",
                match=MatchKeywords,
                keywords=["退款", "流程"],
            ),
        ]
        found, ok = match(entries, "这个绑定怎么操作？", 100)
        self.assertTrue(ok)
        self.assertEqual(found.id, "contains")
        self.assertFalse(match(entries, "我想退款", 100)[1], "keywords must require every keyword")
        found, ok = match(entries, "退款流程是什么？", 100)
        self.assertTrue(ok)
        self.assertEqual(found.id, "keywords")

    def test_match_skips_disabled_and_bounds_scan(self) -> None:
        """移植 ``TestMatchSkipsDisabledAndBoundsScan``。"""
        entries = [
            entry(id="off", question="绑定", answer="A", enabled=False),
            entry(id="on", question="绑定", answer="B"),
        ]
        found, ok = match(entries, "绑定", 100)
        self.assertTrue(ok, "disabled entry was not skipped")
        self.assertEqual(found.id, "on")
        self.assertFalse(match(entries, "前缀绑定", 1)[1], "must not scan past max_scan_runes")


class TestNormalizeDetails(unittest.TestCase):
    """归一化的细节覆盖（全角 / 空白 / 标点）。"""

    def test_fullwidth_ascii_and_ideographic_space(self) -> None:
        # U+FF01–U+FF5E → U+0021–U+007E，U+3000 → 空格。
        self.assertEqual(normalize("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"), "abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(normalize("０１２３４５６７８９"), "0123456789")
        self.assertEqual(normalize("＃＄％＆＊＋－／＝＠＾＿"), "#$%&*+-/=@^_")
        self.assertEqual(normalize("Ａ　Ｂ"), "a b")
        # 全角标点先被折叠成 ASCII 标点，再被标点集合剥掉。
        self.assertEqual(normalize("！？。，、；："), "")
        self.assertEqual(normalize("（测试）【一】"), "测试一")

    def test_whitespace_collapse_including_leading(self) -> None:
        self.assertEqual(normalize("a   b"), "a b")
        self.assertEqual(normalize("\t\na b"), "a b", "首个字符是空白时不写前导空格")
        self.assertEqual(normalize("a b   "), "a b")
        self.assertEqual(normalize(" \u3000 a \u3000 b \u3000 "), "a b")
        self.assertEqual(normalize("a\n\t\r b"), "a b")
        self.assertEqual(normalize("   "), "")
        self.assertEqual(normalize(""), "")

    def test_punctuation_set_is_complete(self) -> None:
        self.assertEqual(len(PUNCTUATION), 30)
        for ch in PUNCTUATION:
            with self.subTest(ch=ch):
                if ch == "～":
                    # U+FF5E 在 Go 里先被 ``width.Narrow`` 折叠成 ``~``(U+007E)，
                    # 而 ``~`` 不在标点集合里 —— 所以这一条 switch case 实际上
                    # 不可达。这里把真实行为钉住，而不是钉住源码字面量。
                    self.assertEqual(normalize("a" + ch + "b"), "a~b")
                    continue
                if ch == "…":
                    # U+2026 是 EastAsianAmbiguous，x/text 对 A 类只在确有窄形时
                    # 才替换；本实现只折叠全角 ASCII 变体，因此它原样保留后被
                    # 标点集合剥掉。
                    self.assertEqual(normalize("a" + ch + "b"), "ab")
                    continue
                self.assertEqual(normalize("a" + ch + "b"), "ab")
        # 逐字对齐 Go 的 isSentencePunctuation 字面量。
        self.assertEqual(
            "".join(sorted(PUNCTUATION)),
            "".join(
                sorted(
                    ".,!?;:、，。！？；：…—～()（）[]【】\"'“”‘’"
                )
            ),
        )

    def test_punctuation_after_narrow_folding(self) -> None:
        # 全角标点在折叠后落到 ASCII 标点上，再被剥掉。
        for ch in "！？。，、；：（）【】“”‘’…—":
            with self.subTest(ch=ch):
                self.assertEqual(normalize("a" + ch + "b"), "ab")
        self.assertEqual(normalize("a～b"), "a~b")
        self.assertEqual(normalize("a~b"), "a~b")

    def test_non_punctuation_is_kept(self) -> None:
        self.assertEqual(normalize("a+b=c"), "a+b=c")
        self.assertEqual(normalize("Ｃ＋＋"), "c++")


class TestLimitRunesAndModes(unittest.TestCase):
    """``max_scan_runes``、模式优先级、存储顺序与 keywords 边界。"""

    def test_max_scan_runes_truncates_by_rune(self) -> None:
        entries = [entry(id="k1", question="绑定", answer="A")]
        # "绑定方法" 截到 2 个 rune 正好是 "绑定"；按字节截断会切坏 UTF-8。
        self.assertTrue(match(entries, "绑定方法", 2)[1])
        self.assertFalse(match(entries, "绑定方法", 1)[1])
        # maxScanRunes <= 0 表示不截断。
        entries_contains = [
            entry(id="c", question="绑定", answer="A", match=MatchContains)
        ]
        self.assertTrue(match(entries_contains, "请说说绑定方法", 0)[1])
        # Go 的 limitRunes 先 trim 再截断，所以前导空白不占截断预算。
        self.assertTrue(match(entries, "  绑定方法", 2)[1])
        self.assertFalse(match(entries_contains, "请说说绑定方法", 1)[1])

    def test_mode_priority_beats_storage_order(self) -> None:
        # keywords / contains 排在前面，exact 排在最后，仍必须命中 exact。
        entries = [
            entry(id="kw", question="绑定", answer="K", match=MatchKeywords, keywords=["绑定"]),
            entry(id="ct", question="绑定", answer="C", match=MatchContains),
            entry(id="ex", question="绑定", answer="E"),
        ]
        found, ok = match(entries, "绑定", 100)
        self.assertTrue(ok)
        self.assertEqual(found.id, "ex")
        # 去掉 exact 后 contains 先于 keywords。
        found, ok = match(entries[0:2], "绑定", 100)
        self.assertEqual(found.id, "ct")
        # 只剩 keywords。
        found, ok = match(entries[0:1], "绑定", 100)
        self.assertEqual(found.id, "kw")

    def test_storage_order_within_one_mode(self) -> None:
        entries = [
            entry(id="first", question="绑定", answer="A"),
            entry(id="second", question="绑定", answer="B"),
        ]
        found, _ = match(entries, "绑定", 100)
        self.assertEqual(found.id, "first")
        found, _ = match(list(reversed(entries)), "绑定", 100)
        self.assertEqual(found.id, "second")

    def test_default_mode_is_exact_for_unknown_values(self) -> None:
        entries = [
            entry(id="weird", question="绑定", answer="A", match="fuzzy"),
            entry(id="also", question="绑定", answer="B", match="  REGEX  "),
        ]
        found, ok = match(entries, "绑定", 100)
        self.assertTrue(ok, "unknown match mode must behave as exact")
        self.assertEqual(found.id, "weird")
        self.assertFalse(match(entries, "绑定方法", 100)[1])

    def test_keywords_all_empty_never_matches(self) -> None:
        entries = [
            entry(id="empty", question="退款", answer="A", match=MatchKeywords, keywords=[]),
            entry(id="blank", question="退款", answer="B", match=MatchKeywords, keywords=["", "   ", "！"]),
            entry(
                id="partial",
                question="退款",
                answer="C",
                match=MatchKeywords,
                keywords=["退款", "  "],
            ),
        ]
        self.assertFalse(match(entries[0:1], "退款流程", 100)[1])
        self.assertFalse(match(entries[1:2], "退款流程", 100)[1])
        # 空关键词被忽略，但"至少有一个非空关键词"仍然成立。
        self.assertTrue(match(entries[2:3], "退款流程", 100)[1])

    def test_keywords_must_all_be_substrings(self) -> None:
        entries = [
            entry(
                id="kw",
                question="退款",
                answer="A",
                match=MatchKeywords,
                keywords=["退款", "流程"],
            )
        ]
        self.assertTrue(match(entries, "退款流程是什么", 100)[1])
        self.assertTrue(match(entries, "流程走完就退款", 100)[1])
        self.assertFalse(match(entries, "只想退款", 100)[1])
        self.assertFalse(match(entries, "只想问流程", 100)[1])

    def test_empty_text_never_matches(self) -> None:
        entries = [entry(id="k1", question="绑定", answer="A")]
        for text in ("", "   ", "！？。", "\n\t"):
            with self.subTest(text=text):
                found, ok = match(entries, text, 100)
                self.assertFalse(ok)
                self.assertIsNone(found)
        self.assertFalse(match([], "绑定", 100)[1])

    def test_disabled_entry_skipped_in_every_mode(self) -> None:
        entries = [
            entry(id="ex-off", question="绑定", answer="A", enabled=False),
            entry(
                id="ct-off",
                question="绑定",
                answer="B",
                match=MatchContains,
                enabled=False,
            ),
            entry(
                id="kw-off",
                question="绑定",
                answer="C",
                match=MatchKeywords,
                keywords=["绑定"],
                enabled=False,
            ),
        ]
        self.assertFalse(match(entries, "绑定", 100)[1])
        entries[2].enabled = True
        self.assertEqual(match(entries, "绑定", 100)[0].id, "kw-off")

    def test_normalize_match_mode_aliases_and_default(self) -> None:
        cases = {
            "contains": MatchContains,
            "  CONTAINS  ": MatchContains,
            "keywords": MatchKeywords,
            "keyword": MatchKeywords,
            "all_keywords": MatchKeywords,
            "all-keywords": MatchKeywords,
            "  ALL_Keywords ": MatchKeywords,
            "": MatchExact,
            "   ": MatchExact,
            "exact": MatchExact,
            "EXACT": MatchExact,
            "fuzzy": MatchExact,
            "regex": MatchExact,
            "containsx": MatchExact,
            "keywordss": MatchExact,
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_match_mode(raw), want)

    def test_aliases_and_question_share_mode(self) -> None:
        entries = [
            entry(
                id="k1",
                question="怎么绑定",
                answer="A",
                aliases=["如何绑定"],
                match=MatchContains,
            )
        ]
        self.assertTrue(match(entries, "请问如何绑定账号", 100)[1])
        self.assertTrue(match(entries, "怎么绑定？", 100)[1])
        self.assertFalse(match(entries, "完全不相关", 100)[1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
