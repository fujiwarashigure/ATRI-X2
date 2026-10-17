"""``atri_x.tool.runtimeinfo`` 的单元测试（对应 Go ``internal/tool/runtimeinfo``）。"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.config import FileDeliveryConfig, default as default_config
from atri_x.errors import AtriXError
from atri_x.tool.runtimeinfo import Info, atriyph_rule_card, first


class TestInfoNormalize(unittest.TestCase):
    def test_config_path_alone_derives_config_dir(self):
        info = Info(config_path=os.path.join("C:" + os.sep, "cfg", "app.toml")).normalize()
        self.assertEqual(info.config_dir, os.path.join("C:" + os.sep, "cfg"))
        self.assertEqual(info.config_path, os.path.join("C:" + os.sep, "cfg", "app.toml"))

    def test_config_dir_alone_derives_config_path(self):
        info = Info(config_dir=os.path.join("C:" + os.sep, "cfg")).normalize()
        self.assertEqual(info.config_dir, os.path.join("C:" + os.sep, "cfg"))
        self.assertEqual(info.config_path, os.path.join("C:" + os.sep, "cfg", "app.toml"))

    def test_both_empty_stay_empty(self):
        info = Info().normalize()
        self.assertEqual(info.config_dir, "")
        self.assertEqual(info.config_path, "")

    def test_sandbox_root_filled_from_defaults(self):
        info = Info().normalize()
        self.assertEqual(info.sandbox_root, default_config().sandbox.root)
        self.assertTrue(info.sandbox_root)

    def test_explicit_sandbox_root_kept(self):
        info = Info(sandbox_root="/custom/root").normalize()
        self.assertEqual(info.sandbox_root, "/custom/root")

    def test_file_delivery_defaults_filled(self):
        defaults = default_config().file_delivery
        info = Info().normalize()
        self.assertEqual(info.file_delivery.max_direct_base64_bytes, defaults.max_direct_base64_bytes)
        self.assertEqual(info.file_delivery.backend, defaults.backend)
        self.assertEqual(info.file_delivery.s3_region, defaults.s3_region)

    def test_file_delivery_non_zero_fields_preserved(self):
        given = FileDeliveryConfig(
            max_direct_base64_bytes=123,
            backend="s3",
            s3_region="cn-north-1",
            s3_bucket="bucket",
        )
        info = Info(file_delivery=given).normalize()
        self.assertEqual(info.file_delivery.max_direct_base64_bytes, 123)
        self.assertEqual(info.file_delivery.backend, "s3")
        self.assertEqual(info.file_delivery.s3_region, "cn-north-1")
        # 未显式给出的字段仍按默认补齐。
        self.assertEqual(
            info.file_delivery.s3_bucket, "bucket"
        )

    def test_zero_max_direct_base64_falls_back(self):
        info = Info(file_delivery=FileDeliveryConfig(max_direct_base64_bytes=0)).normalize()
        self.assertEqual(
            info.file_delivery.max_direct_base64_bytes,
            default_config().file_delivery.max_direct_base64_bytes,
        )

    def test_now_defaults_to_callable(self):
        info = Info().normalize()
        self.assertTrue(callable(info.now))
        value = info.current_time()
        self.assertIsInstance(value, datetime)
        self.assertIsNotNone(value.tzinfo)

    def test_normalize_preserves_injected_clock(self):
        marker = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
        info = Info(now=lambda: marker).normalize()
        self.assertEqual(info.current_time(), marker)

    def test_normalize_is_pure(self):
        original = Info()
        normalized = original.normalize()
        self.assertEqual(original.sandbox_root, "")
        self.assertTrue(normalized.sandbox_root)


class TestCurrentTime(unittest.TestCase):
    def test_current_time_without_normalize(self):
        info = Info()
        self.assertIsNone(info.now)
        value = info.current_time()
        self.assertIsInstance(value, datetime)
        self.assertIsNotNone(value.tzinfo)

    def test_current_time_uses_injected_clock(self):
        marker = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(Info(now=lambda: marker).current_time(), marker)


class TestFirst(unittest.TestCase):
    def test_no_values_returns_normalized_empty(self):
        info = first()
        self.assertEqual(info.config_dir, "")
        self.assertTrue(info.sandbox_root)

    def test_returns_first_normalized(self):
        a = Info(sandbox_root="/a")
        b = Info(sandbox_root="/b")
        info = first(a, b)
        self.assertEqual(info.sandbox_root, "/a")

    def test_first_normalizes(self):
        path = os.path.join("C:" + os.sep, "x", "app.toml")
        info = first(Info(config_path=path))
        self.assertEqual(info.config_dir, os.path.join("C:" + os.sep, "x"))


class TestAtriyphRuleCard(unittest.TestCase):
    def test_returns_text(self):
        card = atriyph_rule_card()
        self.assertIsInstance(card, str)
        self.assertTrue(card.strip())


class TestErrorsAreAtriXErrors(unittest.TestCase):
    def test_sentinels_are_exceptions(self):
        self.assertTrue(issubclass(AtriXError, Exception))


if __name__ == "__main__":
    unittest.main()
