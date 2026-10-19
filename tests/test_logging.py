"""日志中心测试。

覆盖：级别/审计下限、文本键值序列化（与 reader 的解析互为逆运算）、按日轮转、
三通道 Manager、保留期清理、脱敏挂钩、来源/结果/运行来源契约。
"""

from __future__ import annotations

import contextlib
import importlib
import io
import itertools
import os
import shutil
import sys
import unittest
from datetime import datetime
from unittest import mock

import atri_x.logging.logging as logging_module
from atri_x.errors import ClosedError
from atri_x.logging import contract
from atri_x.logging.logging import (
    DEFAULT_RETENTION_DAYS,
    DailyFileWriter,
    Logger,
    Manager,
    audit_floor_level,
    cleanup_old_logs,
    is_managed_log_file,
    level_name,
    level_rank,
    log_dir_for,
    needs_quoting,
    new_logger,
    normalize_retention_days,
    quote_go,
)
from atri_x.logging.reader import parse_text_attrs, unquote_go

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMP_ROOT = os.path.join(_REPO_ROOT, "tmp_atrix_test")
_COUNTER = itertools.count()


def make_temp_dir() -> str:
    """建一个临时目录（固定在仓库根下）。

    刻意不用 ``tempfile.mkdtemp``：它在 Windows 上把目录 ACL 收成 0o700，
    在受限（沙箱）令牌下之后连创建它的进程都写不进去。这里直接 ``os.makedirs``
    继承父目录权限，由调用方删除。
    """
    path = os.path.join(_TEMP_ROOT, f"case_{os.getpid()}_{next(_COUNTER)}")
    os.makedirs(path, exist_ok=True)
    return path


def remove_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    try:
        os.rmdir(_TEMP_ROOT)
    except OSError:
        pass


def read_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)


class TestLevels(unittest.TestCase):
    def test_level_rank(self) -> None:
        self.assertEqual(0, level_rank("debug"))
        self.assertEqual(1, level_rank("info"))
        self.assertEqual(2, level_rank("warn"))
        self.assertEqual(2, level_rank("warning"))  # Go 的 levelRank 接受两种写法
        self.assertEqual(3, level_rank("ERROR"))
        self.assertEqual(1, level_rank("trace"))  # 未知级别按 info（Go 的默认分支）
        self.assertEqual(1, level_rank(""))

    def test_level_name_canonicalizes(self) -> None:
        self.assertEqual("WARN", level_name("warning"))
        self.assertEqual("INFO", level_name("nonsense"))

    def test_audit_floor_level_keeps_info(self) -> None:
        # 运行级别高于 info 时，审计通道仍至少保留 info。
        self.assertEqual("info", audit_floor_level("warn"))
        self.assertEqual("info", audit_floor_level("error"))
        self.assertEqual("info", audit_floor_level("info"))
        self.assertEqual("debug", audit_floor_level("debug"))


class TestTextCodec(unittest.TestCase):
    def test_needs_quoting(self) -> None:
        self.assertFalse(needs_quoting("value"))
        self.assertTrue(needs_quoting(""))
        self.assertTrue(needs_quoting("two words"))
        self.assertTrue(needs_quoting("a=b"))
        self.assertTrue(needs_quoting('say "hi"'))
        self.assertTrue(needs_quoting("tab\there"))
        self.assertTrue(needs_quoting("中文 空格"))
        # 反斜杠本身不触发引号（与 Go slog 的 needsQuoting 一致）。
        self.assertFalse(needs_quoting("back\\slash"))

    def test_quote_go_matches_go_style(self) -> None:
        self.assertEqual('"a b"', quote_go("a b"))
        self.assertEqual('"line\\nbreak"', quote_go("line\nbreak"))
        self.assertEqual('"say \\"hi\\""', quote_go('say "hi"'))
        self.assertEqual('"back\\\\slash"', quote_go("back\\slash"))
        self.assertEqual('"\\abell"', quote_go("\abell"))  # Go 的 Quote 对 \a 用命名转义
        self.assertEqual('"\\x1bbell"', quote_go("\x1bbell"))  # 其余控制字符用 \xHH
        self.assertEqual('"中文"', quote_go("中文"))  # 可打印非 ASCII 不加转义

    def test_quote_and_unquote_round_trip(self) -> None:
        for value in (
            "plain",
            "two words",
            'quote " inside',
            "back\\slash",
            "tab\tnew\nline",
            "中文 与 emoji 🐟",
            "a=b",
        ):
            with self.subTest(value=value):
                self.assertEqual(value, unquote_go(quote_go(value)))

    def test_parse_text_attrs_reads_back_serialized_line(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 6, 3, 15, 0, 0))
        logger.info(
            "audit event",
            "event",
            "tool_call",
            "risk",
            "high",
            "error",
            "bad thing",
            "tags",
            '["windows","onedrive"]',
        )
        parsed = parse_text_attrs(output.getvalue().strip())
        self.assertEqual("audit event", parsed["msg"])
        self.assertEqual("INFO", parsed["level"])
        self.assertEqual("2026-06-03 15:00:00", parsed["time"])
        self.assertEqual("tool_call", parsed["event"])
        self.assertEqual("bad thing", parsed["error"])
        self.assertEqual('["windows","onedrive"]', parsed["tags"])


class TestLogger(TempDirTestCase):
    def test_exact_line_format(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        logger.info("hello world", "event", "tool_call", "count", 3, "success", True)
        self.assertEqual(
            'time="2026-10-13 12:00:00" level=INFO msg="hello world"'
            " event=tool_call count=3 success=true\n",
            output.getvalue(),
        )

    def test_new_logger_factory(self) -> None:
        output = io.StringIO()
        logger = new_logger("info", output)
        self.assertIsInstance(logger, Logger)
        logger.info("hi")
        self.assertIn("level=INFO msg=hi", output.getvalue())

    def test_keyword_fields_and_go_style_pairs(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        logger.info("a", "left", 1)
        logger.info("b", right=2)
        self.assertIn("msg=a left=1", output.getvalue())
        self.assertIn("msg=b right=2", output.getvalue())

    def test_odd_arg_becomes_badkey_like_go(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        logger.info("x", "lonely")
        self.assertIn("lonely=!BADKEY", output.getvalue())

    def test_level_threshold_and_enabled(self) -> None:
        output = io.StringIO()
        logger = Logger("warn", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        self.assertFalse(logger.enabled("info"))
        self.assertTrue(logger.enabled("error"))
        logger.info("dropped")
        logger.debug("dropped")
        self.assertEqual("", output.getvalue())
        logger.warn("kept")
        logger.warning("also kept")
        lines = output.getvalue().splitlines()
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[0].startswith('time="2026-10-13 12:00:00" level=WARN '))

    def test_error_level_and_exception_value(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        logger.error("provider failed", "error", ValueError("boom"))
        # 短值不加引号（Go 的 slog 只在需要时引号）。
        self.assertIn('level=ERROR msg="provider failed" error=boom', output.getvalue())

    def test_error_value_with_spaces_is_quoted(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        logger.error("failed", "error", ValueError("bad thing happened"))
        self.assertIn('error="bad thing happened"', output.getvalue())

    def test_preset_fields_via_with(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        child = logger.with_("session_id", "s1").with_(module="agent")
        child.info("hello")
        self.assertIn("msg=hello session_id=s1 module=agent", output.getvalue())

    def test_missing_output_raises(self) -> None:
        with self.assertRaises(ValueError):
            Logger("info", None).info("x")

    def test_redaction_hook_is_applied_to_string_and_error_values(self) -> None:
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        secrets = mock.Mock(side_effect=lambda value: value.replace("AAHsecret", "[REDACTED]"))
        texts = mock.Mock(side_effect=lambda value: value.replace("secret-pass", "[REDACTED]"))
        with (
            mock.patch.object(logging_module, "redact_secrets", secrets),
            mock.patch.object(logging_module, "redact_text", texts),
        ):
            logger.error(
                "provider failed",
                "url",
                "https://api.telegram.org/bot123:AAHsecret/getMe",
                "error",
                ValueError("POST https://user:secret-pass@example.com/hook failed"),
            )
        text = output.getvalue()
        self.assertNotIn("AAHsecret", text)
        self.assertNotIn("secret-pass", text)
        self.assertIn("[REDACTED]", text)
        secrets.assert_called_once()
        texts.assert_called_once()

    @unittest.skipUnless(logging_module.REDACT_AVAILABLE, "脱敏模块未接通，跳过真实脱敏用例")
    def test_real_redaction_module_keeps_secrets_out_of_logs(self) -> None:
        # 对应 Go 的 TestLoggerRedactsSecretsInStringAndErrorAttributes：
        # 这里故意不 mock，直接验证与 atri_x.redact.redact 的接线是通的。
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        logger.error(
            "provider failed",
            "url",
            "https://api.telegram.org/bot123456789:AAHsecretTokenValue/getMe",
            "error",
            ValueError("POST https://user:secret-pass@example.com/hook failed"),
        )
        text = output.getvalue()
        for secret in ("AAHsecretTokenValue", "secret-pass"):
            self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)

    def test_message_body_is_not_redacted_like_go(self) -> None:
        # Go 的 ReplaceAttr 只作用于属性，msg 不经过脱敏；这里保持一致并在测试里固化。
        output = io.StringIO()
        logger = Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
        secrets = mock.Mock(side_effect=lambda value: "[REDACTED]")
        with mock.patch.object(logging_module, "redact_secrets", secrets):
            logger.info("literal SECRET in message")
        self.assertIn("msg=\"literal SECRET in message\"", output.getvalue())
        secrets.assert_not_called()


class TestDailyFileWriter(TempDirTestCase):
    def test_rotates_when_date_changes(self) -> None:
        current = [datetime(2026, 6, 16, 23, 59, 0)]
        log_dir = os.path.join(self.tmp, "logs")
        writer = DailyFileWriter(log_dir, "atri-x", lambda: current[0])
        logger = Logger("info", writer, now=lambda: current[0])
        logger.info("before midnight")
        current[0] = datetime(2026, 6, 17, 0, 1, 0)
        logger.info("after midnight")
        writer.close()

        old = read_file(os.path.join(log_dir, "atri-x-2026-06-16.log"))
        new = read_file(os.path.join(log_dir, "atri-x-2026-06-17.log"))
        self.assertIn("before midnight", old)
        self.assertNotIn("after midnight", old)
        self.assertIn("after midnight", new)
        self.assertNotIn("before midnight", new)

    def test_close_prevents_further_writes(self) -> None:
        writer = DailyFileWriter(os.path.join(self.tmp, "logs"), "atri-x")
        writer.close()
        with self.assertRaises(ClosedError):
            writer.write("after close\n")

    def test_creates_log_dir_and_rejects_empty_config(self) -> None:
        writer = DailyFileWriter(os.path.join(self.tmp, "nested", "logs"), "audit")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "nested", "logs")))
        self.assertTrue(os.path.exists(writer.path_for_day(writer.current_day())))
        writer.close()
        with self.assertRaises(ValueError):
            DailyFileWriter("", "atri-x")
        with self.assertRaises(ValueError):
            DailyFileWriter(os.path.join(self.tmp, "logs"), "   ")

    def test_lines_use_lf_only(self) -> None:
        writer = DailyFileWriter(os.path.join(self.tmp, "logs"), "atri-x")
        writer.write("a\n")
        writer.close()
        with open(writer.path_for_day(writer.current_day()), "rb") as handle:
            self.assertEqual(b"a\n", handle.read())


class TestManager(TempDirTestCase):
    def test_creates_three_channels_with_audit_floor(self) -> None:
        db_path = os.path.join(self.tmp, "atrix_sessions.db")
        manager = Manager("warn", db_path, 30)
        self.assertEqual(os.path.join(self.tmp, "logs"), manager.log_dir())
        self.assertEqual(30, manager.retention_days())
        manager.runtime().info("dropped by level")
        manager.runtime().warn("runtime log")
        manager.audit().info("audit log")
        manager.atrinis().warn("atrinis log")
        manager.close()

        today = datetime.now().strftime("%Y-%m-%d")
        runtime = read_file(os.path.join(self.tmp, "logs", f"atri-x-{today}.log"))
        audit = read_file(os.path.join(self.tmp, "logs", f"audit-{today}.log"))
        atrinis = read_file(os.path.join(self.tmp, "logs", f"atrinis-{today}.log"))
        self.assertNotIn("dropped by level", runtime)
        self.assertIn("runtime log", runtime)
        self.assertIn("audit log", audit)
        self.assertIn("atrinis log", atrinis)

    def test_log_dir_is_next_to_sqlite_file(self) -> None:
        self.assertEqual("logs", log_dir_for("atrix_sessions.db"))
        self.assertEqual(os.path.join("data", "logs"), log_dir_for(os.path.join("data", "x.db")))


class TestRetention(TempDirTestCase):
    def test_defaults_and_normalization(self) -> None:
        self.assertEqual(30, DEFAULT_RETENTION_DAYS)
        self.assertEqual(30, normalize_retention_days(0))
        self.assertEqual(30, normalize_retention_days(-5))
        self.assertEqual(7, normalize_retention_days(7))

    def test_is_managed_log_file(self) -> None:
        for name in ("atri-x-2026-06-16.log", "audit-2026-06-16.log", "atrinis-2026-06-16.log"):
            self.assertTrue(is_managed_log_file(name), name)
        for name in ("other.log", "atri-x-2026-06-16.txt", "notes.md"):
            self.assertFalse(is_managed_log_file(name), name)

    def test_cleanup_removes_only_expired_managed_files(self) -> None:
        log_dir = os.path.join(self.tmp, "logs")
        os.makedirs(log_dir, exist_ok=True)
        expired = [
            os.path.join(log_dir, "atri-x-2000-01-01.log"),
            os.path.join(log_dir, "atrinis-2000-01-01.log"),
        ]
        kept = [
            os.path.join(log_dir, "audit-2000-01-01.log"),  # 新文件：mtime 不旧
            os.path.join(log_dir, "unmanaged-2000-01-01.log"),  # 不受管前缀
        ]
        for path in expired + kept:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x\n")
        for path in expired + [kept[1]]:
            os.utime(path, (0, 0))

        cleanup_old_logs(log_dir, DEFAULT_RETENTION_DAYS)

        for path in expired:
            self.assertFalse(os.path.exists(path), path)
        for path in kept:
            self.assertTrue(os.path.exists(path), path)


class TestContract(unittest.TestCase):
    """对应 Go ``internal/logging/contract.go``。"""

    def test_log_modules_order_matches_go(self) -> None:
        self.assertEqual(
            [
                "app",
                "agent",
                "session",
                "hook",
                "platform",
                "delivery",
                "cron",
                "atrinis",
                "model",
                "storage",
                "media",
                "maintenance",
                "tool",
            ],
            contract.log_modules(),
        )

    def test_valid_log_module(self) -> None:
        self.assertTrue(contract.valid_log_module("hook"))
        self.assertTrue(contract.valid_log_module("  hook  "))
        self.assertFalse(contract.valid_log_module("HOOK"))  # 大小写敏感（Go 的 map 查找）
        self.assertFalse(contract.valid_log_module(""))
        self.assertFalse(contract.valid_log_module("events"))

    def test_valid_log_result(self) -> None:
        for value in ("succeeded", "failed", "canceled", "rejected", "skipped"):
            self.assertTrue(contract.valid_log_result(value), value)
        self.assertFalse(contract.valid_log_result("success"))
        self.assertFalse(contract.valid_log_result(""))

    def test_valid_run_source(self) -> None:
        self.assertTrue(contract.valid_run_source(contract.RunSourceForeground))
        self.assertTrue(contract.valid_run_source(contract.RunSourceCron))
        self.assertTrue(contract.valid_run_source(contract.RunSourceAtrinis))
        self.assertFalse(contract.valid_run_source("background"))
        self.assertEqual("run_source", contract.FieldRunSource)


# ---------------------------------------------------------------------------
# 脱敏降级：不能原样透传，且必须可观测
# ---------------------------------------------------------------------------

#: 常见密钥样例：降级兜底必须全部遮掉。
_SECRET_SAMPLES = (
    "sk-abcdefghijklmnopqrstuvwxyz0123456789",          # OpenAI/DeepSeek 风格
    "Bearer abcdefgh12345678",                          # Authorization Bearer
    "api_key=supersecretvalue",                         # api_key=...
    "token: another-secret-value",                      # token: ...
    "https://user:pa55word@example.com/hook",           # URL 里的 user:password@
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl",  # JWT
    "123456789:AAHsecretTokenValue1234567890",          # Telegram bot token
    # 与严格模式对齐后补的形态（旧兜底对它们漏遮）。
    "https://api.telegram.org/bot123456:AAHsecretTokenValue1234567890/sendMessage",
    "AKIAIOSFODNN7EXAMPLE",                             # AWS access key id
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",        # Basic
    "postgres://admin:s3cr3t@db:5432/app",              # DSN userinfo
    '"password": "pw-123456"',                          # JSON 引号键
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
)


@contextlib.contextmanager
def degraded_logging_module():
    """强制 ``atri_x.redact`` 导入失败并重新加载日志模块（模拟脱敏模块缺失）。"""
    module = logging_module
    try:
        with mock.patch.dict(sys.modules, {"atri_x.redact": None}):
            importlib.reload(module)
            yield module
    finally:
        importlib.reload(module)


class TestStructuredAttrRedaction(unittest.TestCase):
    """结构化属性值（dict / list / tuple / set）也必须走脱敏。

    旧实现最后一个分支是裸 ``str(value)``，等于给凭据留了一条绕过路径；
    ``cron`` 等调用点正用 ``logger.warning(msg, extra=dict)`` 落结构化属性。
    """

    def test_dict_attr_is_redacted(self) -> None:
        text = logging_module._attr_text({"password": "pw-123456", "api_key": "abcdef123456"})
        self.assertIn("REDACTED", text)
        self.assertNotIn("pw-123456", text)
        self.assertNotIn("abcdef123456", text)

    def test_nested_and_sequence_attrs_are_redacted(self) -> None:
        cases = (
            ["api_key=abcdef123456"],
            ("token", "sk-abcdefghijklmnop"),
            {"outer": {"token": "abcdef123456"}},
            {"Authorization": "Bearer abcdef123456"},
        )
        for value in cases:
            with self.subTest(value=value):
                text = logging_module._attr_text(value)
                self.assertNotIn("abcdef123456", text)
                self.assertNotIn("sk-abcdefghijklmnop", text)
                self.assertIn("REDACTED", text)

    def test_ordinary_structured_attr_is_untouched(self) -> None:
        value = {"model": "gpt-4", "count": 3}
        self.assertEqual(logging_module._attr_text(value), str(value))

    def test_logger_renders_structured_attr_redacted(self) -> None:
        output = io.StringIO()
        logger = logging_module.Logger(
            "info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0)
        )
        logger.error(
            "cron failed",
            "extra",
            {"error": "api_key=abcdef123456"},
            "headers",
            {"Authorization": "Bearer abcdef123456"},
        )
        text = output.getvalue()
        self.assertNotIn("abcdef123456", text)
        self.assertIn("REDACTED", text)


class TestRedactFallback(unittest.TestCase):
    """``REDACT_AVAILABLE=False`` 时使用正则兜底，而不是恒等透传。"""

    def test_fallback_masks_common_secret_shapes(self) -> None:
        for sample in _SECRET_SAMPLES:
            with self.subTest(sample=sample):
                masked = logging_module._fallback_redact(sample)
                self.assertNotEqual(masked, sample)
                self.assertIn("REDACTED", masked)

    def test_fallback_keeps_ordinary_text(self) -> None:
        text = "connection timed out after 30s while contacting the provider"
        self.assertEqual(logging_module._fallback_redact(text), text)

    def test_degraded_logger_does_not_write_raw_secrets(self) -> None:
        with degraded_logging_module() as module:
            self.assertFalse(module.REDACT_AVAILABLE)
            output = io.StringIO()
            # 用模块里的 Logger（重新加载后的那一个）。
            logger = module.Logger("info", output, now=lambda: datetime(2026, 10, 13, 12, 0, 0))
            logger.error(
                "provider failed",
                "api_key",
                "sk-abcdefghijklmnopqrstuvwxyz0123456789",
                "authorization",
                "Bearer abcdefgh12345678",
                "error",
                ValueError("POST https://user:pa55word@example.com/hook failed"),
            )
            text = output.getvalue()
        for secret in ("sk-abcdefghijklmnopqrstuvwxyz0123456789", "abcdefgh12345678", "pa55word"):
            self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)

    def test_degradation_alert_is_emitted_once(self) -> None:
        with degraded_logging_module() as module:
            logger = mock.Mock()
            self.assertFalse(module.announce_redact_degradation(logger))
            self.assertFalse(module.announce_redact_degradation(logger))
            self.assertEqual(logger.error.call_count, 1)
            self.assertIn("log redaction is unavailable", logger.error.call_args[0][0])

    def test_alert_is_quiet_when_redaction_is_available(self) -> None:
        logger = mock.Mock()
        self.assertTrue(logging_module.announce_redact_degradation(logger))
        logger.error.assert_not_called()

    def test_status_shape(self) -> None:
        status = logging_module.redact_status()
        self.assertIn("available", status)
        self.assertIn("mode", status)
        with degraded_logging_module() as module:
            degraded = module.redact_status()
            self.assertFalse(degraded["available"])
            self.assertEqual(degraded["mode"], "fallback")
            self.assertGreater(degraded["fallback_rules"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
