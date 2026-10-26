"""``atri_x.timezones`` 的单元测试：解析、固定偏移、DST、缺数据硬失败。"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from atri_x import timezones
from atri_x.errors import InvalidTimezoneError, TimezoneDataUnavailableError


class TimezoneResolveTest(unittest.TestCase):
    def test_default_constant_is_shanghai(self) -> None:
        self.assertEqual(timezones.DEFAULT_TIMEZONE, "Asia/Shanghai")

    def test_resolve_iana_and_dst_offsets(self) -> None:
        zone = timezones.resolve_timezone("America/New_York")
        self.assertEqual(zone.rules, "iana")
        winter = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc)
        summer = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(timezones.format_offset(zone.tz, winter), "-05:00")
        self.assertEqual(timezones.format_offset(zone.tz, summer), "-04:00")

    def test_empty_uses_default(self) -> None:
        zone = timezones.resolve_timezone("")
        self.assertEqual(zone.name, timezones.DEFAULT_TIMEZONE)

    def test_fixed_offset_is_naive_and_canonical(self) -> None:
        zone = timezones.resolve_timezone("fixed:+08:00")
        self.assertEqual(zone.name, "fixed:+08:00")
        self.assertEqual(zone.rules, "naive_offset")
        # 固定偏移不跟随 DST
        winter = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc)
        summer = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(
            timezones.format_offset(zone.tz, winter),
            timezones.format_offset(zone.tz, summer),
        )

    def test_invalid_fixed_offset_rejected(self) -> None:
        for bad in ("fixed:8", "fixed:+25:00", "fixed:+08:60", "fixed:abc"):
            with self.assertRaises(InvalidTimezoneError, msg=bad):
                timezones.resolve_timezone(bad)

    def test_system_mode_is_explicit(self) -> None:
        zone = timezones.resolve_timezone("system")
        self.assertEqual(zone.name, "system")
        self.assertEqual(zone.rules, "system_offset")

    def test_unknown_zone_fails_without_fallback(self) -> None:
        with self.assertRaises(InvalidTimezoneError):
            timezones.resolve_timezone("Mars/Olympus")

    def test_missing_tzdata_fails_loudly(self) -> None:
        """模拟干净 Windows（没有 tzdata）：必须报"数据缺失"，不能回落 UTC/本地。"""
        from zoneinfo import ZoneInfoNotFoundError

        with mock.patch.object(timezones, "_zoneinfo_names", return_value=frozenset()):
            with mock.patch(
                "zoneinfo.ZoneInfo",
                side_effect=ZoneInfoNotFoundError("No time zone found with key Asia/Shanghai"),
            ):
                with self.assertRaises(TimezoneDataUnavailableError):
                    timezones.resolve_timezone("Asia/Shanghai")

    def test_is_known_timezone(self) -> None:
        self.assertTrue(timezones.is_known_timezone("UTC"))
        self.assertTrue(timezones.is_known_timezone("fixed:-05:00"))
        self.assertFalse(timezones.is_known_timezone("Nope/Nope"))

    def test_available_timezones_non_empty_with_tzdata(self) -> None:
        zones = timezones.available_timezones()
        if not zones:
            self.skipTest("no IANA data in this environment")
        self.assertIn("Asia/Shanghai", zones)
        self.assertEqual(timezones.iana_zone_count(), len(zones))

    def test_canonical_name(self) -> None:
        self.assertEqual(timezones.canonical_name(" fixed:+8:00 "), "fixed:+08:00")
        self.assertEqual(timezones.canonical_name("system"), "system")
        self.assertEqual(timezones.canonical_name(""), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
