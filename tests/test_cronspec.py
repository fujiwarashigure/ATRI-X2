"""``atri_x.net.cronspec`` 的单元测试。

注意：本机 ``zoneinfo`` 没有 tzdata（``ZoneInfo("UTC")`` 直接抛
``ZoneInfoNotFoundError``），所以时区用例用固定偏移 ``datetime.timezone`` 与
本文件自造的 :class:`_GapTz`（含一次春季跳变），不依赖真实时区数据库。
"""

from __future__ import annotations

import datetime as dt
import time
import unittest

from atri_x.net.cronspec import (
    ConstantDelaySchedule,
    CronParseError,
    Schedule,
    SpecSchedule,
    every,
    parse,
    parse_standard,
    parse_with_seconds,
)

CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc


class _GapTz(dt.tzinfo):
    """自造 DST 时区：2026-03-29 本地 02:00→03:00（该小时不存在），10-25 回拨。

    本地→偏移用本地窗口，UTC→本地用 UTC 窗口，两者互为逆映射；这样不存在的
    墙钟时间（如 03-29 02:30）往返后会变成 03:30，可被 ``_exists`` 检出。
    """

    LOCAL_SUMMER = (dt.datetime(2026, 3, 29, 3), dt.datetime(2026, 10, 25, 3))
    UTC_SUMMER = (dt.datetime(2026, 3, 29, 1), dt.datetime(2026, 10, 25, 1))

    def utcoffset(self, value: dt.datetime | None) -> dt.timedelta:
        if value is None:
            return dt.timedelta(hours=1)
        naive = value.replace(tzinfo=None)
        return dt.timedelta(hours=2 if self.LOCAL_SUMMER[0] <= naive < self.LOCAL_SUMMER[1] else 1)

    def dst(self, value: dt.datetime | None) -> dt.timedelta:
        return dt.timedelta(0) if value is None else self.utcoffset(value) - dt.timedelta(hours=1)

    def tzname(self, value: dt.datetime | None) -> str:
        return "GAP+2" if self.utcoffset(value) == dt.timedelta(hours=2) else "GAP+1"

    def fromutc(self, value: dt.datetime) -> dt.datetime:
        naive = value.replace(tzinfo=None)
        offset = dt.timedelta(hours=2 if self.UTC_SUMMER[0] <= naive < self.UTC_SUMMER[1] else 1)
        return (naive + offset).replace(tzinfo=self)


GAP_TZ = _GapTz()


class TestParsing(unittest.TestCase):
    def test_five_field_defaults_seconds_to_zero(self) -> None:
        schedule = parse("0 3 * * *")
        self.assertIsInstance(schedule, SpecSchedule)
        self.assertEqual(str(schedule), "0 3 * * *")
        self.assertFalse(schedule.has_seconds)  # type: ignore[union-attr]
        self.assertEqual(schedule.next(dt.datetime(2026, 1, 1, 3, 0)), dt.datetime(2026, 1, 2, 3, 0))

    def test_six_field_uses_seconds(self) -> None:
        schedule = parse("15 30 8 * * MON")
        self.assertEqual(str(schedule), "15 30 8 * * 1")
        self.assertEqual(
            schedule.next(dt.datetime(2026, 6, 1, 8, 30, 0)), dt.datetime(2026, 6, 1, 8, 30, 15)
        )

    def test_star_and_question(self) -> None:
        self.assertEqual(str(parse("0 0 ? * ?")), "0 0 * * *")
        self.assertEqual(str(parse("* * * * *")), "* * * * *")

    def test_step_forms(self) -> None:
        self.assertEqual(str(parse("*/15 * * * *")), "*/15 * * * *")
        self.assertEqual(str(parse("0 0-23/2 * * *")), "0 */2 * * *")
        self.assertEqual(str(parse("0 0 * * 1-5/2")), "0 0 * * 1,3,5")
        self.assertEqual(str(parse("5/20 * * * *")), "5,25,45 * * * *")
        self.assertEqual(str(parse("0 10-20/3 * * *")), "0 10,13,16,19 * * *")

    def test_lists(self) -> None:
        # 连续三段以上会被规范化成区间，语义不变。
        self.assertEqual(str(parse("0 1,2,3 * * *")), "0 1-3 * * *")
        self.assertEqual(str(parse("0 1,2,5 * * *")), "0 1,2,5 * * *")
        self.assertEqual(str(parse("0 0 * * SUN,WED,FRI")), "0 0 * * 0,3,5")

    def test_names_case_insensitive(self) -> None:
        self.assertEqual(str(parse("0 0 1 jan-mar *")), "0 0 1 1-3 *")
        self.assertEqual(str(parse("0 0 * * sun")), "0 0 * * 0")
        self.assertEqual(str(parse("0 0 * * Sun-Sat")), "0 0 * * *")

    def test_sunday_seven_normalised_to_zero(self) -> None:
        self.assertEqual(str(parse("0 0 * * 7")), "0 0 * * 0")
        self.assertEqual(str(parse("0 0 * * 5-7")), "0 0 * * 0,5,6")
        self.assertEqual(parse("0 0 * * 7").next(dt.datetime(2026, 6, 1)), dt.datetime(2026, 6, 7))

    def test_descriptors(self) -> None:
        expected = {
            "@yearly": "0 0 1 1 *",
            "@annually": "0 0 1 1 *",
            "@monthly": "0 0 1 * *",
            "@weekly": "0 0 * * 0",
            "@daily": "0 0 * * *",
            "@midnight": "0 0 * * *",
            "@hourly": "0 * * * *",
        }
        for text, canonical in expected.items():
            with self.subTest(text=text):
                self.assertEqual(str(parse(text)), canonical)
                self.assertEqual(str(parse(text.upper())), canonical)

    def test_every_descriptor(self) -> None:
        schedule = parse("@every 1h30m")
        self.assertIsInstance(schedule, ConstantDelaySchedule)
        self.assertEqual(str(schedule), "@every 1h30m0s")
        self.assertEqual(schedule.interval, 5400.0)  # type: ignore[union-attr]
        self.assertEqual(str(parse("@every 90s")), "@every 1m30s")
        self.assertEqual(str(parse("@every 500ms")), "@every 1s")  # <1s 抬到 1s
        self.assertEqual(str(parse("@every 1h30m10s")), "@every 1h30m10s")

    def test_parse_standard_and_with_seconds(self) -> None:
        self.assertIsInstance(parse_standard("0 3 * * *"), SpecSchedule)
        self.assertTrue(parse_with_seconds("0 0 3 * * *").has_seconds)  # type: ignore[union-attr]
        with self.assertRaises(CronParseError):
            parse_standard("0 0 3 * * *")
        with self.assertRaises(CronParseError):
            parse_with_seconds("0 3 * * *")


class TestNext(unittest.TestCase):
    def test_simple_minute(self) -> None:
        schedule = parse("* * * * *")
        self.assertEqual(
            schedule.next(dt.datetime(2026, 1, 1, 0, 0, 30)), dt.datetime(2026, 1, 1, 0, 1, 0)
        )

    def test_dom_dow_or_semantics(self) -> None:
        # robfig/cron 的经典例子：每月 1 号 + 每个周一都会触发。
        schedule = parse("0 0 1 * MON")
        cursor = dt.datetime(2026, 6, 1)
        got = []
        for _ in range(6):
            cursor = schedule.next(cursor)  # type: ignore[assignment]
            got.append(cursor)
        self.assertEqual(
            got,
            [
                dt.datetime(2026, 6, 8),
                dt.datetime(2026, 6, 15),
                dt.datetime(2026, 6, 22),
                dt.datetime(2026, 6, 29),
                dt.datetime(2026, 7, 1),
                dt.datetime(2026, 7, 6),
            ],
        )

    def test_dom_only_is_and_semantics(self) -> None:
        schedule = parse("0 0 1 * *")
        self.assertEqual(
            schedule.next(dt.datetime(2026, 6, 2)), dt.datetime(2026, 7, 1)
        )

    def test_dow_only(self) -> None:
        schedule = parse("0 0 * * MON")
        self.assertEqual(schedule.next(dt.datetime(2026, 6, 1)), dt.datetime(2026, 6, 8))

    def test_leap_year(self) -> None:
        schedule = parse("0 0 29 2 *")
        self.assertEqual(schedule.next(dt.datetime(2027, 1, 1)), dt.datetime(2028, 2, 29))
        self.assertEqual(schedule.next(dt.datetime(2028, 2, 29)), dt.datetime(2032, 2, 29))

    def test_month_end_skips_short_months(self) -> None:
        schedule = parse("0 0 31 * *")
        self.assertEqual(schedule.next(dt.datetime(2026, 4, 1)), dt.datetime(2026, 5, 31))
        self.assertEqual(schedule.next(dt.datetime(2026, 1, 31)), dt.datetime(2026, 3, 31))

    def test_cross_month_and_year(self) -> None:
        schedule = parse("0 0 1 1 *")
        self.assertEqual(schedule.next(dt.datetime(2026, 6, 1)), dt.datetime(2027, 1, 1))
        schedule = parse("59 23 31 12 *")
        self.assertEqual(
            schedule.next(dt.datetime(2026, 12, 31, 23, 59)), dt.datetime(2027, 12, 31, 23, 59)
        )

    def test_impossible_schedule_returns_none(self) -> None:
        self.assertIsNone(parse("0 0 31 2 *").next(dt.datetime(2026, 1, 1)))
        self.assertIsNone(parse("0 0 30 2 *").next(dt.datetime(2026, 1, 1)))

    def test_strictly_after(self) -> None:
        schedule = parse("0 0 * * *")
        self.assertEqual(
            schedule.next(dt.datetime(2026, 1, 1, 0, 0, 0)), dt.datetime(2026, 1, 2, 0, 0)
        )

    def test_six_field_second_step(self) -> None:
        schedule = parse("*/10 * * * * *")
        self.assertEqual(
            schedule.next(dt.datetime(2026, 1, 1, 0, 0, 7)), dt.datetime(2026, 1, 1, 0, 0, 10)
        )
        self.assertEqual(
            schedule.next(dt.datetime(2026, 1, 1, 0, 0, 55)), dt.datetime(2026, 1, 1, 0, 1, 0)
        )

    def test_worst_case_is_fast(self) -> None:
        schedule = parse("0 0 29 2 *")
        started = time.monotonic()
        self.assertEqual(schedule.next(dt.datetime(2026, 1, 1)), dt.datetime(2028, 2, 29))
        self.assertLess(time.monotonic() - started, 2.0)
        started = time.monotonic()
        self.assertIsNone(parse("0 0 31 2 *").next(dt.datetime(2026, 1, 1)))
        self.assertLess(time.monotonic() - started, 2.0)


class TestPrev(unittest.TestCase):
    def test_prev_is_strictly_before(self) -> None:
        schedule = parse("*/5 * * * *")
        self.assertEqual(schedule.prev(dt.datetime(2026, 1, 1, 0, 5)), dt.datetime(2026, 1, 1, 0, 0))
        self.assertEqual(schedule.prev(dt.datetime(2026, 1, 1, 0, 4)), dt.datetime(2026, 1, 1, 0, 0))

    def test_prev_across_month_and_year(self) -> None:
        schedule = parse("0 0 1 1 *")
        self.assertEqual(schedule.prev(dt.datetime(2026, 6, 1)), dt.datetime(2026, 1, 1))
        self.assertEqual(schedule.prev(dt.datetime(2026, 1, 1)), dt.datetime(2025, 1, 1))

    def test_prev_leap_day(self) -> None:
        schedule = parse("0 0 29 2 *")
        self.assertEqual(schedule.prev(dt.datetime(2029, 1, 1)), dt.datetime(2028, 2, 29))

    def test_prev_none_when_impossible(self) -> None:
        self.assertIsNone(parse("0 0 31 2 *").prev(dt.datetime(2026, 1, 1)))

    def test_prev_dom_dow_or(self) -> None:
        schedule = parse("0 0 1 * MON")
        self.assertEqual(schedule.prev(dt.datetime(2026, 6, 9)), dt.datetime(2026, 6, 8))
        self.assertEqual(schedule.prev(dt.datetime(2026, 7, 2)), dt.datetime(2026, 7, 1))


class TestTimezone(unittest.TestCase):
    def test_result_keeps_input_tzinfo(self) -> None:
        schedule = parse("0 9 * * *")
        after = dt.datetime(2026, 1, 1, 10, 0, tzinfo=CST)
        result = schedule.next(after)
        self.assertIsNotNone(result)
        self.assertIs(result.tzinfo, CST)  # type: ignore[union-attr]
        self.assertEqual(result, dt.datetime(2026, 1, 2, 9, 0, tzinfo=CST))

    def test_naive_stays_naive(self) -> None:
        result = parse("0 9 * * *").next(dt.datetime(2026, 1, 1, 10, 0))
        self.assertIsNone(result.tzinfo)  # type: ignore[union-attr]

    def test_utc_offset_is_respected(self) -> None:
        schedule = parse("@daily")
        result = schedule.next(dt.datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC))
        self.assertEqual(result, dt.datetime(2026, 1, 2, tzinfo=UTC))

    def test_nonexistent_local_time_is_skipped(self) -> None:
        # 2026-03-29 本地 02:30 不存在（02:00→03:00 跳变），应顺延到次日 02:30。
        schedule = parse("0 30 2 * * *")
        after = dt.datetime(2026, 3, 29, 0, 0, tzinfo=GAP_TZ)
        result = schedule.next(after)
        self.assertEqual(result, dt.datetime(2026, 3, 30, 2, 30, tzinfo=GAP_TZ))

    def test_normal_day_in_dst_zone(self) -> None:
        schedule = parse("0 30 2 * * *")
        after = dt.datetime(2026, 6, 1, 0, 0, tzinfo=GAP_TZ)
        self.assertEqual(
            schedule.next(after), dt.datetime(2026, 6, 1, 2, 30, tzinfo=GAP_TZ)
        )


class TestEvery(unittest.TestCase):
    def test_next_uses_delay_and_drops_microseconds(self) -> None:
        schedule = every(90)
        self.assertIsInstance(schedule, ConstantDelaySchedule)
        self.assertEqual(
            schedule.next(dt.datetime(2026, 1, 1, 0, 0, 0, 123456)),
            dt.datetime(2026, 1, 1, 0, 1, 30),
        )

    def test_sub_second_clamped_and_truncated(self) -> None:
        self.assertEqual(every(0.25).interval, 1.0)
        self.assertEqual(every(-5).interval, 1.0)
        self.assertEqual(every(2.75).interval, 2.0)
        self.assertEqual(every(dt.timedelta(seconds=45)).interval, 45.0)

    def test_prev_mirrors_next(self) -> None:
        schedule = every(60)
        self.assertEqual(
            schedule.prev(dt.datetime(2026, 1, 1, 0, 5)), dt.datetime(2026, 1, 1, 0, 4)
        )

    def test_str_uses_go_duration_syntax(self) -> None:
        self.assertEqual(str(every(1)), "@every 1s")
        self.assertEqual(str(every(60)), "@every 1m0s")
        self.assertEqual(str(every(3600)), "@every 1h0m0s")
        self.assertEqual(str(every(5400)), "@every 1h30m0s")

    def test_parsed_every_actually_fires_repeatedly(self) -> None:
        schedule = parse("@every 30s")
        cursor = dt.datetime(2026, 1, 1)
        stamps = []
        for _ in range(3):
            cursor = schedule.next(cursor)  # type: ignore[assignment]
            stamps.append(cursor)
        self.assertEqual(stamps, [
            dt.datetime(2026, 1, 1, 0, 0, 30),
            dt.datetime(2026, 1, 1, 0, 1, 0),
            dt.datetime(2026, 1, 1, 0, 1, 30),
        ])


class TestCanonicalString(unittest.TestCase):
    def test_str_is_stable_and_reparsable(self) -> None:
        for text in ["*/5 * * * *", "0 3 1,15 * 1-5", "0 0 29 2 *", "0 0 * * 7", "@daily"]:
            with self.subTest(text=text):
                first = str(parse(text))
                self.assertEqual(str(parse(first)), first)

    def test_str_of_six_field_keeps_six_fields(self) -> None:
        self.assertEqual(str(parse("0 30 9 * * *")), "0 30 9 * * *")

    def test_repr(self) -> None:
        self.assertIn("0 3 * * *", repr(parse("0 3 * * *")))


class TestParseErrors(unittest.TestCase):
    def _error(self, spec: str) -> CronParseError:
        with self.assertRaises(CronParseError) as ctx:
            parse(spec)
        return ctx.exception

    def test_field_count(self) -> None:
        error = self._error("0 1 2 3")
        self.assertEqual(error.field, "spec")
        self.assertEqual(error.index, 0)
        self.assertIn("4 段", str(error))
        self.assertEqual(self._error("0 1 2 3 4 5 6").field, "spec")

    def test_out_of_range_values(self) -> None:
        error = self._error("60 0 * * *")
        self.assertEqual((error.field, error.index), ("minute", 0))
        error = self._error("0 24 * * *")
        self.assertEqual((error.field, error.index), ("hour", 0))
        error = self._error("0 0 32 * *")
        self.assertEqual((error.field, error.index), ("dom", 0))
        error = self._error("0 0 * 13 *")
        self.assertEqual((error.field, error.index), ("month", 0))
        error = self._error("0 0 * * 8")
        self.assertEqual((error.field, error.index), ("dow", 0))

    def test_index_points_at_the_offending_character(self) -> None:
        error = self._error("5,70 * * * *")
        self.assertEqual(error.field, "minute")
        self.assertEqual(error.index, 2)  # "5,70" 里 70 从该字段第 3 个字符开始
        self.assertIn("70", error.message)

    def test_bad_ranges_and_steps(self) -> None:
        self.assertEqual(self._error("0 10-5 * * *").field, "hour")
        self.assertEqual(self._error("0 1--5 * * *").field, "hour")
        self.assertEqual(self._error("0 1- * * *").field, "hour")
        self.assertEqual(self._error("0 */0 * * *").index, 2)
        self.assertEqual(self._error("0 */x * * *").index, 2)

    def test_bad_names(self) -> None:
        error = self._error("0 0 * * FUNDAY")
        self.assertEqual((error.field, error.index), ("dow", 0))
        error = self._error("0 0 * JANUARY *")
        self.assertEqual((error.field, error.index), ("month", 0))

    def test_empty_field_and_element(self) -> None:
        # 空白折叠后段数不足，报段数错误。
        self.assertEqual(self._error("0  * * *").field, "spec")
        self.assertEqual(self._error("0 ,1 * * *").field, "hour")
        self.assertEqual(self._error("0 1,,2 * * *").index, 2)

    def test_bad_descriptors(self) -> None:
        self.assertEqual(self._error("@bogus").field, "descriptor")
        self.assertEqual(self._error("@every nonsense").field, "@every")
        self.assertEqual(self._error("@every 5x").field, "@every")

    def test_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(CronParseError, ValueError))
        with self.assertRaises(ValueError):
            parse("nonsense")

    def test_from_string_domain(self) -> None:
        error = self._error("0 0 L * *")
        self.assertEqual(error.field, "dom")


class TestProtocolConformance(unittest.TestCase):
    def test_schedules_satisfy_protocol(self) -> None:
        self.assertIsInstance(parse("@daily"), Schedule)
        self.assertIsInstance(every(60), Schedule)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
