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

    def test_extended_credential_shapes(self) -> None:
        """本仓在 Go 规则之外补齐的形态（见 ``redact.py`` 的模块 docstring）。"""
        cases = (
            ("creds AKIAIOSFODNN7EXAMPLE end", "creds [REDACTED] end"),
            (
                "pat github_pat_11ABCDEFG0abcdefghijkl_abcdefghijklmnopqrstuvwxyz end",
                "pat [REDACTED] end",
            ),
            (
                "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig-part-123456 end",
                "auth [REDACTED] end",
            ),
            (
                "Authorization: Basic dXNlcjpwYXNzd29yZA==",
                "Authorization: Basic [REDACTED]",
            ),
            ("Cookie: session=abc123def456; Path=/", "Cookie: [REDACTED]"),
            ("Set-Cookie: sid=abc123def456; Path=/", "Set-Cookie: [REDACTED]"),
            (
                "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
                "[REDACTED]",
            ),
            (
                "dsn postgres://admin:s3cr3t@db:5432/app",
                "dsn postgres://[REDACTED]@db:5432/app",
            ),
            (
                "url https://b.s3.amazonaws.com/k?X-Amz-Signature=deadbeef0123456789",
                "url https://b.s3.amazonaws.com/k?X-Amz-Signature=[REDACTED]",
            ),
            ("raw bot123456:AAHfoo_bar-baz0123456789 end", "raw bot[REDACTED] end"),
        )
        for value, want in cases:
            with self.subTest(value=value):
                self.assertEqual(secrets(value), want)

    def test_query_secret_does_not_swallow_following_params(self) -> None:
        # URL query 规则先于 key=value：`&x=1` 必须留下（旧行为会被一起吃掉）。
        self.assertEqual(
            secrets("https://example.com/api?access_token=abcdef123456&x=1"),
            "https://example.com/api?access_token=[REDACTED]&x=1",
        )

    def test_pii_is_not_redacted_by_design(self) -> None:
        # 与 Go 一致：脱敏只管凭据，手机号/身份证等 PII 不在范围内。
        for value in ("phone 13812345678", "id 110101199003071234"):
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

    def test_porting_contract_names_are_reexported(self) -> None:
        # PORTING.md §3 的契约名必须能从包级 import（早期只在子模块里）。
        from atri_x.redact import redact_secrets, redact_text

        self.assertIs(redact_secrets, secrets)
        self.assertIs(redact_text, secrets)


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
