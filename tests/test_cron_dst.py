"""cron 的 DST / 跳时 / ``run_at_utc`` 契约测试（1.0.10 阶段 6）。

覆盖：

* 一次性任务：DST 不存在的时刻**拒绝**、重复时刻取第一次并标记 ambiguous；
* ``run_at_utc`` 优先于本地墙钟（改时区/换机器不再漂移）；
* ``Manager._stamp_once_run_at`` 在创建入口补 UTC 时刻（幂等）；
* 周期任务：DST 不存在时段跳过、重复时段只执行第一次（钉死现有 cronspec 行为）；
* 调度循环等待：目标已过立即返回、停止信号/墙钟回拨的处理。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest
from unittest import mock

from atri_x.cron import (
    METADATA_KIND,
    Manager,
    ScheduleOnce,
    compute_next_run_at,
    user_once_run_at,
)
from atri_x.cron.model import (
    CronActor,
    CronSchedule,
    Metadata,
    format_run_at_utc,
    parse_run_at_utc,
    resolve_once_run_at,
)
from atri_x.errors import InvalidError
from atri_x.net.cronspec import parse_standard
from atri_x.storage.models import UpsertCronJobRequest

try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - 没有 tzdata 的环境
    NY = None


def _once_metadata(run_at: str = "", run_at_utc: str = "") -> str:
    meta = Metadata(
        kind=METADATA_KIND,
        version=1,
        title="t",
        created_by=CronActor(actor_id="cli:local", platform="cli"),
        schedule=CronSchedule(mode=ScheduleOnce, run_at=run_at, run_at_utc=run_at_utc),
        trigger=_trigger(),
        target=_target(),
    )
    return meta.encode()


def _trigger():
    from atri_x.cron import CronTrigger, TriggerDirect

    return CronTrigger(mode=TriggerDirect, message="hi")


def _target():
    from atri_x.cron import CronTarget

    return CronTarget(source_platform="cli")


class RunAtUtcContractTest(unittest.TestCase):
    def test_dst_gap_rejected(self) -> None:
        if NY is None:
            self.skipTest("no tzdata")
        with self.assertRaises(InvalidError) as ctx:
            resolve_once_run_at("2026-03-08 02:30:00", NY)
        self.assertIn("daylight-saving gap", str(ctx.exception))

    def test_dst_ambiguous_uses_first_occurrence(self) -> None:
        if NY is None:
            self.skipTest("no tzdata")
        moment, ambiguous = resolve_once_run_at("2026-11-01 01:30:00", NY)
        self.assertTrue(ambiguous)
        # 第一次出现是 EDT（UTC-4）→ 05:30Z
        self.assertEqual(moment, dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc))

    def test_normal_time_not_ambiguous(self) -> None:
        if NY is None:
            self.skipTest("no tzdata")
        moment, ambiguous = resolve_once_run_at("2026-06-01 12:00:00", NY)
        self.assertFalse(ambiguous)
        self.assertEqual(moment, dt.datetime(2026, 6, 1, 16, 0, tzinfo=dt.timezone.utc))

    def test_utc_roundtrip(self) -> None:
        moment = dt.datetime(2026, 3, 1, 1, 2, 3, tzinfo=dt.timezone.utc)
        text = format_run_at_utc(moment)
        self.assertTrue(text.endswith("Z"))
        self.assertEqual(parse_run_at_utc(text), moment)

    def test_bad_run_at_utc_rejected(self) -> None:
        for bad in ("", "2026-03-01", "not-a-time"):
            with self.assertRaises(InvalidError, msg=bad):
                parse_run_at_utc(bad)

    def test_user_once_run_at_prefers_utc_stamp(self) -> None:
        metadata = _once_metadata(
            run_at="1999-01-01 00:00:00", run_at_utc="2031-05-04T03:02:01Z"
        )
        moment, ok, err = user_once_run_at(metadata, NY)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(moment, dt.datetime(2031, 5, 4, 3, 2, 1, tzinfo=dt.timezone.utc))

    def test_legacy_metadata_still_interprets_local_wall_clock(self) -> None:
        metadata = _once_metadata(run_at="2031-05-04 11:02:01")  # 无 run_at_utc
        moment, ok, err = user_once_run_at(metadata, NY)
        self.assertTrue(ok)
        self.assertIsNone(err)
        # 2031-05-04 是 EDT（UTC-4）
        self.assertEqual(moment, dt.datetime(2031, 5, 4, 15, 2, 1, tzinfo=dt.timezone.utc))

    def test_compute_next_run_at_uses_utc_stamp(self) -> None:
        now = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        metadata = _once_metadata(
            run_at="1999-01-01 00:00:00", run_at_utc="2030-06-01T00:00:00Z"
        )
        next_run = compute_next_run_at("0 0 * * *", metadata, True, now, NY)
        self.assertEqual(next_run, dt.datetime(2030, 6, 1, tzinfo=dt.timezone.utc))

    def test_stamp_is_idempotent(self) -> None:
        manager = Manager(None, None, location=NY, now=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        request = UpsertCronJobRequest(
            name="user.cron.t",
            handler="builtin.cron",
            schedule="30 2 8 3 *",
            metadata=_once_metadata(run_at="2026-06-01 12:00:00"),
            enabled=False,
        )
        stamped = manager._stamp_once_run_at(request)
        meta = json.loads(stamped.metadata)
        self.assertEqual(meta["schedule"]["run_at_utc"], "2026-06-01T16:00:00Z")
        self.assertEqual(meta["schedule"]["run_at_tz"], "America/New_York")
        again = manager._stamp_once_run_at(stamped)
        self.assertEqual(again.metadata, stamped.metadata)


class CronSpecDstTest(unittest.TestCase):
    def test_missing_hour_is_skipped(self) -> None:
        if NY is None:
            self.skipTest("no tzdata")
        schedule = parse_standard("30 2 * * *")
        after = dt.datetime(2026, 3, 7, 12, 0, tzinfo=NY)
        nxt = schedule.next(after)
        assert nxt is not None
        self.assertEqual(
            nxt.astimezone(dt.timezone.utc),
            dt.datetime(2026, 3, 9, 6, 30, tzinfo=dt.timezone.utc),
        )

    def test_repeated_hour_runs_once(self) -> None:
        if NY is None:
            self.skipTest("no tzdata")
        schedule = parse_standard("30 1 * * *")
        first = schedule.next(dt.datetime(2026, 11, 1, 0, 0, tzinfo=NY))
        assert first is not None
        # 第一次出现（EDT，-04:00）
        self.assertEqual(first.astimezone(dt.timezone.utc), dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc))
        second = schedule.next(first)
        assert second is not None
        # 同一堵墙时刻的第二次出现被跳过，直接到下一天
        self.assertEqual(second.date(), dt.date(2026, 11, 2))


class LoopWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_past_target_returns_true(self) -> None:
        clock = {"now": dt.datetime(2026, 3, 1, 0, 0, tzinfo=dt.timezone.utc)}
        manager = Manager(None, None, now=lambda: clock["now"])
        self.assertTrue(
            await manager._wait_until(clock["now"] - dt.timedelta(seconds=1))
        )

    async def test_stop_signal_returns_false(self) -> None:
        manager = Manager(None, None, now=lambda: dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc))
        manager._stopping.set()
        self.assertFalse(
            await manager._wait_until(dt.datetime(2026, 3, 2, tzinfo=dt.timezone.utc))
        )

    async def test_backward_jump_recomputes(self) -> None:
        clock = {"now": dt.datetime(2026, 3, 1, 0, 0, tzinfo=dt.timezone.utc)}
        manager = Manager(None, None, now=lambda: clock["now"])
        target = clock["now"] + dt.timedelta(hours=5)

        async def fake_wait_for(awaitable, timeout):
            clock["now"] = clock["now"] - dt.timedelta(hours=2)
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError()

        with mock.patch("asyncio.wait_for", fake_wait_for):
            # 第一段等待期间墙钟回拨 2 小时 → 判定为"回拨过大"，返回 True 让外层重算
            self.assertTrue(await manager._wait_until(target))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
