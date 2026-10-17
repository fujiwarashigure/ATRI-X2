r"""``atri_x.redact.secrets`` 的幂等性回归测试。

背景（一次真实缺陷）：Go 的 key=value 规则里无引号分支是 ``[^\s,;]+``，它会把
**已经脱敏过**的值连同后面的 ``}`` / ``]`` / ``)`` 一起吃掉：

    secrets('{"password": "pw-123456"}')        -> '{"password": [REDACTED]}'
    secrets(secrets('{"password": ...}'))       -> '{"password": [REDACTED]'   # 丢了 }

再往下还会继续啃掉后面的字符。Go 侧只在日志行上调用一次，所以上游看不见这个问题；
Python 版把 :func:`atri_x.redact.secrets` 用在了**会重复扫描同一份文件**的部署脚本
（``deploy/atri_x_watchdog.py``）上，非幂等就会真的损坏日志。

修法是给无引号分支加 ``(?!\[REDACTED\])`` 先行断言：首次脱敏的输出与 Go 逐字一致，
只有「值已经是 [REDACTED]」的二次扫描不再重复匹配。本文件同时锁住这两条性质。
"""

from __future__ import annotations

import unittest

from atri_x.redact import secrets


#: 直接抄自上游 ``internal/redact/redact_test.go`` 的用例（Go 侧期望值）。
GO_CASES: list[tuple[str, str]] = [
    (
        'Post "https://api.telegram.org/bot123456:AAHsecret/getUpdates": unexpected EOF',
        'Post "https://api.telegram.org/bot[REDACTED]/getUpdates": unexpected EOF',
    ),
    (
        "request failed: Authorization: Bearer abcdef123456 header",
        "request failed: Authorization: Bearer [REDACTED] header",
    ),
    (
        '{"refresh_token":"json-secret-value","password": "pw-123456"}',
        '{"refresh_token":[REDACTED],"password": [REDACTED]}',
    ),
]


class TestSecretsGoParity(unittest.TestCase):
    """首次脱敏必须与 Go 逐字一致（否则就不是移植，而是改行为）。"""

    def test_upstream_expected_outputs(self) -> None:
        for raw, want in GO_CASES:
            with self.subTest(raw=raw):
                self.assertEqual(secrets(raw), want)


class TestSecretsIdempotent(unittest.TestCase):
    """重复应用不得改变结果（Python 版新增的硬要求）。"""

    def test_go_cases_are_idempotent(self) -> None:
        for raw, _want in GO_CASES:
            with self.subTest(raw=raw):
                once = secrets(raw)
                self.assertEqual(secrets(once), once)

    def test_json_object_keeps_closing_brace(self) -> None:
        # 这正是被咬掉 ``}`` 的最小复现。
        raw = '{"password": "pw-123456"}'
        once = secrets(raw)
        self.assertEqual(once, '{"password": [REDACTED]}')
        self.assertTrue(once.endswith("}"), once)
        self.assertEqual(secrets(once), once)

    def test_repeated_application_converges(self) -> None:
        cases = [
            'password=pw-123456',
            "api_key=sk-abcdefgh1234",
            "Authorization: Bearer abcdef123456",
            '{"api_key": "sk-abcdefgh1234", "x": 1}',
            "https://api.telegram.org/bot123456:AAHsecret/getUpdates",
            "https://user:pass@example.com/hook",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                once = secrets(raw)
                twice = secrets(once)
                self.assertEqual(twice, once, "second pass changed the text")
                self.assertEqual(secrets(twice), once, "third pass changed the text")

    def test_already_redacted_value_is_left_alone(self) -> None:
        already = '{"password": [REDACTED]}'
        self.assertEqual(secrets(already), already)


if __name__ == "__main__":
    unittest.main()
