"""``atri_x.redact`` 与 ``atri_x.textmatch`` 的单元测试。

对应 Go：``internal/redact/redact_test.go`` + ``internal/textmatch/textmatch_test.go``。
"""

from __future__ import annotations

import re
import unittest

from atri_x.redact import error, new_error_id, secrets, summarize
from atri_x.textmatch import keywords, like_pattern


class TestRedact(unittest.TestCase):
    def test_telegram_bot_url(self) -> None:
        value = "send failed: https://api.telegram.org/bot123456:AAHfoo_bar-baz/sendMessage"
        self.assertEqual(
            secrets(value),
            "send failed: https://api.telegram.org/bot[REDACTED]/sendMessage",
        )

    def test_url_userinfo(self) -> None:
        self.assertEqual(
            secrets("hook https://user:pass@example.com/hook failed"),
            "hook https://[REDACTED]@example.com/hook failed",
        )

    def test_prefixed_api_keys(self) -> None:
        self.assertEqual(secrets("key sk-abcdefghijklmnop"), "key [REDACTED]")
        self.assertEqual(
            secrets("key ghp_abcdefghijklmnopqrst"), "key [REDACTED]"
        )
        self.assertEqual(secrets("key xoxb-abcdefghij"), "key [REDACTED]")

    def test_bearer_token(self) -> None:
        self.assertEqual(
            secrets("Authorization: Bearer abcdef123456"),
            "Authorization: Bearer [REDACTED]",
        )

    def test_key_value_credentials(self) -> None:
        cases = (
            ("api_key=abcdef123456", "api_key=[REDACTED]"),
            ("api-key: abcdef123456", "api-key: [REDACTED]"),
            ('"password": "hunter2"', '"password": [REDACTED]'),
            ("bot_token=123:abc", "bot_token=[REDACTED]"),
            ("my_secret_key = value", "my_secret_key = [REDACTED]"),
            ("client_secret='quoted'", "client_secret=[REDACTED]"),
        )
        for value, want in cases:
            self.assertEqual(secrets(value), want, value)

    def test_plain_text_is_untouched(self) -> None:
        for value in (
            "hello world",
            "tokens 18,240 · cache 12,100",
            "secret sauce recipe",
            "the answer is 42",
        ):
            self.assertEqual(secrets(value), value)

    def test_error_redacts_and_handles_none(self) -> None:
        self.assertEqual(error(None), "")
        self.assertEqual(error(RuntimeError("api_key=abcdef123456")), "api_key=[REDACTED]")

    def test_summarize_truncates_on_rune_boundary(self) -> None:
        self.assertEqual(summarize("alpha"), "alpha")
        self.assertEqual(summarize("x" * 10, 4), "xxxx...（已截断）")
        self.assertEqual(summarize("中文测试内容", 3), "中文测...（已截断）")
        self.assertEqual(summarize("  padded  "), "padded")
        self.assertEqual(summarize("sk-abcdefghijklmnop", 0), "[REDACTED]")

    def test_new_error_id_is_uuid_like(self) -> None:
        value = new_error_id()
        self.assertRegex(
            value,
            re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
        )
        self.assertNotEqual(value, new_error_id())


class TestTextmatch(unittest.TestCase):
    def test_cjk_ngrams(self) -> None:
        terms = keywords("  今天天气怎么样  ")
        self.assertEqual(len(terms), len(set(terms)))
        self.assertEqual(terms[0], "今天")
        self.assertIn("今天天气", terms)
        self.assertLessEqual(len(terms), 24)

    def test_single_cjk_character_fallback(self) -> None:
        self.assertEqual(keywords("猫"), ["猫"])
        self.assertEqual(keywords(""), [])
        self.assertEqual(keywords("   "), [])

    def test_ascii_words_need_two_characters(self) -> None:
        self.assertEqual(keywords("a go python"), ["go", "python"])
        self.assertEqual(keywords("Python 代码 测试")[:1], ["代码"])

    def test_deduplicates_case_insensitively(self) -> None:
        self.assertEqual(keywords("Python python PYTHON"), ["python"])

    def test_ascii_is_capped_while_cjk_remains(self) -> None:
        text = "记忆测试内容很长很长很长很长很长很长很长很长很长很长很长很长 " + " ".join(
            f"word{index}" for index in range(20)
        )
        terms = keywords(text)
        self.assertEqual(len(terms), 24)
        ascii_terms = [term for term in terms if term.isascii()]
        self.assertEqual(len(ascii_terms), 8)

    def test_long_input_samples_head_and_tail(self) -> None:
        text = "啊" * 1300 + "记忆"
        self.assertIn("记忆", keywords(text))

    def test_like_pattern_escapes_wildcards(self) -> None:
        self.assertEqual(like_pattern(""), "%")
        self.assertEqual(like_pattern("  "), "%")
        self.assertEqual(like_pattern("abc"), "%abc%")
        self.assertEqual(like_pattern("a%b_c\\d"), "%a\\%b\\_c\\\\d%")
