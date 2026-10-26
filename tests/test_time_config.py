"""``[time]`` 配置段与 ``config check`` / ``doctor`` 时间能力的测试。"""

from __future__ import annotations

import unittest

from atri_x.app.config_check import check_config_warnings
from atri_x.config import default as default_config
from atri_x.config.app import AppConfig
from atri_x.config.inspect import ConfigDecodeError
from atri_x.config.time_sections import (
    TimeConfig,
    TimeSourceConfig,
    TimeVerificationConfig,
)


class DefaultsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = default_config().time
        self.assertEqual(config.timezone, "Asia/Shanghai")
        self.assertTrue(config.inject_context)
        self.assertFalse(config.verification.enabled)
        self.assertEqual(config.verification.mode, "verify_only")
        self.assertEqual(config.sources, [])

    def test_legacy_config_without_time_gets_shanghai(self) -> None:
        """旧配置没有 ``[time]`` 时按新默认（Asia/Shanghai）——这是有意的行为变更。"""
        config = AppConfig.from_dict({})
        config.apply_app_defaults()
        self.assertEqual(config.time.timezone, "Asia/Shanghai")

    def test_unknown_time_key_rejected(self) -> None:
        with self.assertRaises(ConfigDecodeError):
            TimeConfig.from_dict({"timezone": "UTC", "no_such_key": 1})


class ValidationTest(unittest.TestCase):
    def test_invalid_timezone_rejected(self) -> None:
        with self.assertRaises(ConfigDecodeError):
            TimeConfig(timezone="Mars/Olympus").normalized()

    def test_fixed_offset_and_system_allowed(self) -> None:
        self.assertEqual(TimeConfig(timezone="fixed:+08:00").normalized().timezone, "fixed:+08:00")
        self.assertEqual(TimeConfig(timezone="system").normalized().timezone, "system")

    def test_takeover_mode_rejected(self) -> None:
        with self.assertRaises(ConfigDecodeError) as ctx:
            TimeVerificationConfig(mode="takeover").normalized()
        self.assertIn("not available", str(ctx.exception))

    def test_out_of_range_verification_settings_rejected(self) -> None:
        cases = (
            {"interval_seconds": 5},
            {"jitter_ratio": 0.9},
            {"timeout_seconds": 0.1},
            {"max_concurrency": 9},
            {"warn_offset_ms": 40000, "conflict_offset_ms": 30000},
        )
        for case in cases:
            with self.assertRaises(ConfigDecodeError, msg=str(case)):
                TimeVerificationConfig(**case).normalized()

    def test_source_requires_id_and_https(self) -> None:
        with self.assertRaises(ConfigDecodeError):
            TimeSourceConfig(id="", protocol="sntp").normalized()
        with self.assertRaises(ConfigDecodeError):
            TimeSourceConfig(id="a", protocol="ntp").normalized()
        with self.assertRaises(ConfigDecodeError):
            TimeSourceConfig(id="a", protocol="https", url="http://x/y").normalized()
        with self.assertRaises(ConfigDecodeError):
            TimeSourceConfig(id="a", protocol="https", url="https://u:p@x/y").normalized()

    def test_duplicate_source_ids_rejected(self) -> None:
        config = TimeConfig(
            sources=[
                TimeSourceConfig(id="a", protocol="sntp", host="10.0.0.1"),
                TimeSourceConfig(id="a", protocol="sntp", host="10.0.0.2"),
            ]
        )
        with self.assertRaises(ConfigDecodeError):
            config.normalized()

    def test_enabled_sources_and_gate(self) -> None:
        config = TimeConfig(
            verification=TimeVerificationConfig(enabled=True),
            sources=[TimeSourceConfig(id="a", protocol="sntp", host="10.0.0.1", enabled=True)],
        ).normalized()
        self.assertEqual(len(config.enabled_sources()), 1)
        self.assertTrue(config.verification_enabled())
        no_sources = TimeConfig(verification=TimeVerificationConfig(enabled=True)).normalized()
        self.assertFalse(no_sources.verification_enabled())


class ConfigCheckWarningsTest(unittest.TestCase):
    def test_warns_when_enabled_without_sources(self) -> None:
        config = default_config()
        config.time.verification.enabled = True
        config.time.sources = []
        warnings = check_config_warnings(config)
        self.assertTrue(any("没有任何启用" in item or "没有启用任何" in item for item in warnings))

    def test_warns_when_groups_below_minimum(self) -> None:
        config = default_config()
        config.time.verification.enabled = True
        config.time.verification.min_groups = 2
        config.time.sources = [
            TimeSourceConfig(id="a", protocol="sntp", host="10.0.0.1", group="g", enabled=True),
            TimeSourceConfig(id="b", protocol="sntp", host="10.0.0.2", group="g", enabled=True),
        ]
        warnings = check_config_warnings(config)
        self.assertTrue(any("独立分组" in item for item in warnings))


class AssetTest(unittest.TestCase):
    def test_builtin_asset_declares_time_section(self) -> None:
        from pathlib import Path

        asset = (
            Path(__file__).resolve().parent.parent
            / "atri_x"
            / "config"
            / "assets_data"
            / "app.toml"
        )
        self.assertTrue(asset.is_file(), asset)
        text = asset.read_text(encoding="utf-8")
        self.assertIn("[time]", text)
        self.assertIn('timezone = "Asia/Shanghai"', text)
        self.assertIn("[time.verification]", text)
        # 资产里不得内置任何公共时间端点（示例都必须注释掉）
        self.assertNotIn("\n[[time.sources]]", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
