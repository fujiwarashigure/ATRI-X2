"""``atri_x.time_service`` 的单元测试：快照一致性、naive 拒绝、跳变检测、校验状态。"""

from __future__ import annotations

import datetime as dt
import unittest

from atri_x.config.time_sections import TimeConfig
from atri_x.errors import InvalidError
from atri_x.time_service import (
    CLOCK_JUMP_THRESHOLD_SECONDS,
    VERIFICATION_STATES,
    VERIFY_CONFLICT,
    VERIFY_DISABLED,
    TimeService,
    clock_delta_mismatch,
    default_time_service,
)

_FIXED = dt.datetime(2026, 3, 1, 1, 12, 33, tzinfo=dt.timezone.utc)


def _service(**kwargs) -> TimeService:
    return TimeService(
        "Asia/Shanghai",
        now_fn=lambda: _FIXED,
        monotonic_fn=lambda: 1000.0,
        **kwargs,
    )


class SnapshotTest(unittest.TestCase):
    def test_all_fields_come_from_one_sample(self) -> None:
        snapshot = _service().snapshot()
        self.assertEqual(snapshot.utc_time, _FIXED)
        self.assertEqual(snapshot.sampled_at, _FIXED)
        self.assertEqual(snapshot.current_time, _FIXED.astimezone(dt.timezone(dt.timedelta(hours=8))))
        self.assertEqual(snapshot.timezone, "Asia/Shanghai")
        self.assertEqual(snapshot.utc_offset, dt.timedelta(hours=8))
        self.assertEqual(snapshot.weekday, snapshot.current_time.weekday())
        self.assertEqual(snapshot.unix_timestamp, int(_FIXED.timestamp()))
        self.assertEqual(snapshot.source, "system_clock")
        self.assertFalse(snapshot.verified)
        self.assertEqual(snapshot.verification_state, VERIFY_DISABLED)
        self.assertEqual(snapshot.monotonic_seconds, 1000.0)
        # 同一 instant：本地与 UTC 表示同一个时刻，偏移就是 utc_offset
        self.assertEqual(snapshot.current_time.utcoffset(), snapshot.utc_offset)

    def test_snapshot_dict_is_json_ready(self) -> None:
        payload = _service().snapshot().to_dict()
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertTrue(payload["current_time"].endswith("+08:00"))
        self.assertEqual(payload["utc_offset"], "+08:00")

    def test_snapshot_timezone_override_is_resolved(self) -> None:
        snapshot = _service().snapshot("UTC")
        self.assertEqual(snapshot.timezone, "UTC")
        self.assertEqual(snapshot.utc_offset, dt.timedelta(0))
        self.assertEqual(snapshot.timezone_source, "argument")

    def test_snapshot_invalid_timezone_raises(self) -> None:
        with self.assertRaises(Exception):
            _service().snapshot("Mars/Base")

    def test_naive_now_is_rejected(self) -> None:
        service = TimeService(
            "UTC",
            now_fn=lambda: dt.datetime(2026, 1, 1, 0, 0, 0),
            monotonic_fn=lambda: 1.0,
        )
        with self.assertRaises(InvalidError):
            service.now_utc()
        with self.assertRaises(InvalidError):
            service.snapshot()

    def test_age_seconds(self) -> None:
        service = _service()
        earlier = _FIXED - dt.timedelta(seconds=35)
        self.assertAlmostEqual(service.age_seconds(earlier) or 0.0, 35.0, places=3)
        self.assertIsNone(service.age_seconds(None))
        future = _FIXED + dt.timedelta(seconds=5)
        self.assertAlmostEqual(service.age_seconds(future) or 0.0, -5.0, places=3)


class ConfigAndStateTest(unittest.TestCase):
    def test_from_config_reads_timezone_and_inject_flag(self) -> None:
        config = TimeConfig(timezone="Europe/London", inject_context=False)
        service = TimeService.from_config(config, now_fn=lambda: _FIXED)
        self.assertEqual(service.timezone_name(), "Europe/London")
        self.assertFalse(service.inject_context)

    def test_verification_state_roundtrip(self) -> None:
        service = _service()
        service.set_verification_state(VERIFY_CONFLICT, "sources disagree")
        self.assertEqual(service.verification_state(), VERIFY_CONFLICT)
        self.assertTrue(service.is_suspect())
        self.assertEqual(service.snapshot().verification_state, VERIFY_CONFLICT)

    def test_unknown_state_rejected(self) -> None:
        with self.assertRaises(InvalidError):
            _service().set_verification_state("totally-made-up")

    def test_states_are_documented_set(self) -> None:
        self.assertIn("insufficient_evidence", VERIFICATION_STATES)
        self.assertNotIn("accuracy_guaranteed", VERIFICATION_STATES)

    def test_describe(self) -> None:
        payload = _service().describe()
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["effective_source"], "system_clock")
        self.assertIn(payload["tz_data"], ("present", "missing"))

    def test_default_service_is_singleton(self) -> None:
        self.assertIs(default_time_service(), default_time_service())


class ClockJumpTest(unittest.TestCase):
    def test_identical_deltas_mean_no_jump(self) -> None:
        before = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        after = before + dt.timedelta(seconds=10)
        self.assertAlmostEqual(clock_delta_mismatch(before, 5.0, after, 15.0), 0.0)

    def test_forward_clock_jump_detected(self) -> None:
        before = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        after = before + dt.timedelta(hours=1)
        mismatch = clock_delta_mismatch(before, 0.0, after, 1.0)
        self.assertGreater(mismatch, CLOCK_JUMP_THRESHOLD_SECONDS)

    def test_suspend_detected(self) -> None:
        """休眠：墙钟走了 30 分钟，单调时钟只走了 1 秒。"""
        before = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        after = before + dt.timedelta(minutes=30)
        self.assertGreater(
            clock_delta_mismatch(before, 0.0, after, 1.0),
            CLOCK_JUMP_THRESHOLD_SECONDS,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
