"""``atri_x.maintenance`` 的日报测试：空闲档判定、计价与报告渲染。

从 ``tests/test_maintenance.py`` 拆出（原文件超过 PORTING §2 的单文件行数上限）。
共享夹具见 ``tests/_maintenance_support.py``。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.config import DailyReportConfig, MaintenanceCleanupConfig, ModelPriceConfig
from atri_x.errors import InvalidError
from atri_x.logging import LogEntry, LogQuery, QueryResult
from atri_x.maintenance import (
    ImageReportStats,
    ModelUsageReport,
    Service,
    compute_entry_cost,
    currency_symbol,
    format_daily_report,
    format_money,
    human_int,
    is_off_peak,
    log_query_days,
    new_service,
    offpeak_price,
    parse_log_int,
    price_for,
)
from atri_x.sysinfo import Snapshot

from tests._maintenance_support import SHANGHAI, _AsyncCaseWithTmp, _CaseWithTmp


class TestOffPeak(unittest.TestCase):
    def test_weekend_is_off_peak(self) -> None:
        # 2026-10-11 是周日。
        self.assertTrue(is_off_peak(datetime(2026, 10, 11, 10, 0, tzinfo=SHANGHAI), []))

    def test_weekday_peak_windows(self) -> None:
        # 2026-10-13 是周二。
        self.assertFalse(is_off_peak(datetime(2026, 10, 13, 9, 0, tzinfo=SHANGHAI), []))
        self.assertFalse(is_off_peak(datetime(2026, 10, 13, 11, 59, tzinfo=SHANGHAI), []))
        self.assertFalse(is_off_peak(datetime(2026, 10, 13, 14, 0, tzinfo=SHANGHAI), []))
        self.assertFalse(is_off_peak(datetime(2026, 10, 13, 17, 59, tzinfo=SHANGHAI), []))
        self.assertTrue(is_off_peak(datetime(2026, 10, 13, 12, 0, tzinfo=SHANGHAI), []))
        self.assertTrue(is_off_peak(datetime(2026, 10, 13, 8, 59, tzinfo=SHANGHAI), []))
        self.assertTrue(is_off_peak(datetime(2026, 10, 13, 18, 0, tzinfo=SHANGHAI), []))

    def test_holiday_is_off_peak(self) -> None:
        peak_time = datetime(2026, 10, 13, 10, 0, tzinfo=SHANGHAI)
        self.assertFalse(is_off_peak(peak_time, []))
        self.assertTrue(is_off_peak(peak_time, ["2026-10-13"]))

    def test_unknown_time_is_peak(self) -> None:
        # 时间未知按高峰处理（避免低估账单）。
        self.assertFalse(is_off_peak(None, []))

    def test_utc_input_is_converted(self) -> None:
        # 02:00 UTC == 10:00 北京时间 → 高峰。
        self.assertFalse(is_off_peak(datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc), []))


class TestPricing(unittest.TestCase):
    def _price(self) -> ModelPriceConfig:
        return ModelPriceConfig(
            input_per_million=2.0,
            output_per_million=8.0,
            cache_input_per_million=0.5,
            offpeak_input_per_million=1.0,
            offpeak_output_per_million=4.0,
            offpeak_cache_input_per_million=0.0,
        )

    def test_offpeak_price_falls_back_per_field(self) -> None:
        out = offpeak_price(self._price())
        self.assertEqual(out.input_per_million, 1.0)
        self.assertEqual(out.output_per_million, 4.0)
        # offpeak_cache_input_per_million 为 0 → 保留基准缓存价。
        self.assertEqual(out.cache_input_per_million, 0.5)

    def test_price_for_returns_none_for_unknown_model(self) -> None:
        cfg = DailyReportConfig(prices={"m": self._price()})
        self.assertIsNone(price_for(cfg, "other", None))
        self.assertIsNotNone(price_for(cfg, "m", None))

    def test_price_for_switches_to_offpeak(self) -> None:
        cfg = DailyReportConfig(prices={"m": self._price()})
        offpeak = datetime(2026, 10, 11, 10, 0, tzinfo=SHANGHAI)
        self.assertEqual(price_for(cfg, "m", offpeak).input_per_million, 1.0)

    def test_peak_pricing_disabled_keeps_base(self) -> None:
        cfg = DailyReportConfig(peak_pricing=False, prices={"m": self._price()})
        offpeak = datetime(2026, 10, 11, 10, 0, tzinfo=SHANGHAI)
        self.assertEqual(price_for(cfg, "m", offpeak).input_per_million, 2.0)

    def test_compute_entry_cost_clamps_cache_hit(self) -> None:
        price = self._price()
        # 全部命中缓存：100 * 0.5 / 1e6，无未命中输入。
        self.assertAlmostEqual(
            compute_entry_cost(100, 0, 100, price), 100 * 0.5 / 1_000_000
        )
        # cache_hit 超过 prompt 时钳到 prompt。
        self.assertAlmostEqual(
            compute_entry_cost(100, 0, 500, price), 100 * 0.5 / 1_000_000
        )
        # 负命中钳到 0。
        self.assertAlmostEqual(compute_entry_cost(100, 0, -5, price), 100 * 2.0 / 1_000_000)
        # 混合：50 命中 + 50 未命中 + 10 输出。
        expected = (
            50 * 2.0 / 1_000_000 + 50 * 0.5 / 1_000_000 + 10 * 8.0 / 1_000_000
        )
        self.assertAlmostEqual(compute_entry_cost(100, 10, 50, price), expected)

    def test_compute_entry_cost_falls_back_to_input_price(self) -> None:
        price = ModelPriceConfig(input_per_million=3.0, cache_input_per_million=0.0)
        self.assertAlmostEqual(
            compute_entry_cost(10, 0, 10, price), 10 * 3.0 / 1_000_000
        )


class TestFormatHelpers(unittest.TestCase):
    def test_human_int(self) -> None:
        self.assertEqual(human_int(0), "0")
        self.assertEqual(human_int(999), "999")
        self.assertEqual(human_int(1000), "1,000")
        self.assertEqual(human_int(1234567), "1,234,567")
        self.assertEqual(human_int(-1000), "-1,000")
        self.assertEqual(human_int(-1), "-1")

    def test_currency_symbol(self) -> None:
        for value in ("", "CNY", "cny", "RMB", "¥", "￥"):
            self.assertEqual(currency_symbol(value), "¥")
        self.assertEqual(currency_symbol("USD"), "$")
        self.assertEqual(currency_symbol("$"), "$")
        self.assertEqual(currency_symbol("EUR"), "EUR ")

    def test_format_money(self) -> None:
        self.assertEqual(format_money(1.5, "¥"), "¥1.5000")
        self.assertEqual(format_money(0.0, "$"), "$0.0000")

    def test_parse_log_int(self) -> None:
        self.assertEqual(parse_log_int(" 42 "), 42)
        self.assertEqual(parse_log_int("-7"), -7)
        self.assertEqual(parse_log_int("abc"), 0)
        self.assertEqual(parse_log_int(""), 0)
        self.assertEqual(parse_log_int(None), 0)  # type: ignore[arg-type]

    def test_log_query_days(self) -> None:
        since = datetime(2026, 10, 13, tzinfo=timezone.utc)
        self.assertEqual(log_query_days(since, since), 2)
        self.assertEqual(log_query_days(since, since + timedelta(hours=12)), 2)
        self.assertEqual(log_query_days(since, since + timedelta(days=3)), 5)
        # 钳到 7。
        self.assertEqual(log_query_days(since, since + timedelta(days=30)), 7)


class TestFormatDailyReport(unittest.TestCase):
    def _snapshot(self) -> Snapshot:
        return Snapshot(
            dir_bytes=2048,
            disk_total_bytes=10_000,
            disk_free_bytes=2_500,
            heap_alloc_bytes=1024,
            heap_sys_bytes=4096,
            rss_bytes=8192,
            goroutines=7,
        )

    def _cfg(self) -> DailyReportConfig:
        return DailyReportConfig(
            window_hours=12,
            provider="deepseek",
            currency="CNY",
            image_price_per_image=0.05,
        )

    def test_with_usage(self) -> None:
        since = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
        until = since + timedelta(hours=12)
        usage = [
            ModelUsageReport(
                model="deepseek-chat",
                calls=3,
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cache_hit_tokens=200,
                cost=0.1234,
                priced=True,
            ),
            ModelUsageReport(model="other-model", calls=1, total_tokens=10),
        ]
        text = format_daily_report(self._cfg(), ImageReportStats(2, 1), usage, self._snapshot(), since, until)
        self.assertTrue(text.startswith("ATRI-X 定时报告\n周期：2026-10-13 00:00 ~ 2026-10-13 12:00\n窗口：最近 12 小时"))
        self.assertIn("【生图】\n- 成功：2 次\n- 失败：1 次\n- 合计：3 次", text)
        self.assertIn("- deepseek-chat：调用 3 次｜输入 1,000｜输出 500｜缓存命中 200｜合计 1,500", text)
        self.assertIn("- 合计：调用 4 次｜输入 1,000｜输出 500｜缓存命中 200｜合计 1,510", text)
        self.assertIn("- deepseek-chat：¥0.1234", text)
        self.assertIn("- 生图：2 张 × ¥0.0500 = ¥0.1000", text)
        self.assertIn("- 合计：¥0.2234", text)
        self.assertIn("- 计价：高峰/空闲双档", text)
        self.assertIn("- 未配置单价：other-model（只统计 token，不计金额）", text)
        self.assertIn("已用 7.3 KB / 9.8 KB（剩余 2.4 KB，75.0%）", text)
        self.assertIn("- 进程 RSS：8.0 KB", text)
        self.assertIn("- Python 堆：1.0 KB / 系统 4.0 KB", text)
        self.assertIn("- 线程：7", text)
        self.assertFalse(text.endswith("\n"))

    def test_without_data(self) -> None:
        since = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
        cfg = DailyReportConfig(window_hours=6, provider="", currency="USD", image_price_per_image=0.0)
        text = format_daily_report(cfg, ImageReportStats(), [], Snapshot(), since, since)
        self.assertIn("- 无 llm_usage 记录（全部 provider）", text)
        self.assertIn("- 无数据", text)
        self.assertIn("窗口：最近 6 小时", text)
        # 没有磁盘数据也没有 disk_err → 不打文件系统行。
        self.assertNotIn("文件系统", text)
        # rss 为 0 → 不打 RSS 行。
        self.assertNotIn("进程 RSS", text)

    def test_disk_error_line(self) -> None:
        since = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
        snapshot = Snapshot(disk_err="unsupported on this platform")
        text = format_daily_report(DailyReportConfig(), ImageReportStats(), [], snapshot, since, since)
        self.assertIn("- 文件系统：暂不支持（unsupported on this platform）", text)


class _FakeLogManager:
    def __init__(self, log_dir: str) -> None:
        self._dir = log_dir

    def log_dir(self) -> str:
        return self._dir


class _DailyReportService(_AsyncCaseWithTmp):
    def _service(self, entries: list[LogEntry], *, fail_query: bool = False) -> Service:
        log_dir = self.path("logs")
        os.makedirs(log_dir, exist_ok=True)
        service = new_service(_FakeLogManager(log_dir), None, MaintenanceCleanupConfig(), None)
        service.report = DailyReportConfig(
            enabled=True,
            window_hours=12,
            provider="",
            currency="CNY",
            data_root=log_dir,
            image_price_per_image=0.1,
            prices={"deepseek-chat": ModelPriceConfig(input_per_million=1.0, output_per_million=2.0)},
        )
        captured: list[LogQuery] = []

        def _query(ctx=None, query=None):  # noqa: ANN001
            if fail_query:
                raise OSError("log read failed")
            captured.append(query)
            return QueryResult(entries=list(entries))

        service._reader_query = _query  # type: ignore[attr-defined]
        service._captured = captured  # type: ignore[attr-defined]
        return service

    def _patch_reader(self, service: Service) -> None:
        """把 ``atri_x.maintenance.report.Reader`` 换成假实现。"""
        from atri_x.maintenance import report as report_module

        query = service._reader_query  # type: ignore[attr-defined]
        original = report_module.Reader

        class _FakeReader:
            def __init__(self, dir: str = "") -> None:  # noqa: A002
                self.dir = dir

            def query(self, ctx=None, q=None):  # noqa: ANN001
                return query(ctx, q)

        report_module.Reader = _FakeReader  # type: ignore[assignment]
        self.addCleanup(setattr, report_module, "Reader", original)


class TestDailyReport(_DailyReportService):
    async def test_build_report_aggregates_and_prices(self) -> None:
        entries = [
            LogEntry(
                time=datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc),
                fields={
                    "event": "llm_usage",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "prompt_tokens": "1000",
                    "completion_tokens": "500",
                    "total_tokens": "1500",
                    "cache_hit_tokens": "200",
                },
            ),
            LogEntry(
                time=datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc),
                fields={"event": "llm_usage", "model": "", "total_tokens": "10"},
            ),
        ]
        service = self._service(entries)
        self._patch_reader(service)
        since = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
        until = since + timedelta(hours=12)
        text = await service.build_daily_report(None, since, until)
        self.assertIn("- deepseek-chat：调用 1 次", text)
        self.assertIn("(unknown)", text)
        self.assertIn("未配置单价：(unknown)", text)

    async def test_run_daily_report_requires_sender(self) -> None:
        service = self._service([])
        sent: list[str] = []
        service.report_sender = None
        service.warn = lambda msg, *a: sent.append("warn:" + msg)  # type: ignore[method-assign]
        await service.run_daily_report(None)
        self.assertEqual(sent, ["warn:daily report skipped: no delivery target configured"])

    async def test_run_daily_report_delivers_text(self) -> None:
        service = self._service([])
        self._patch_reader(service)
        got: list[tuple] = []

        async def _send(ctx, text):  # noqa: ANN001
            got.append((ctx, text))

        service.report_sender = _send
        await service.run_daily_report(None)
        self.assertEqual(len(got), 1)
        self.assertIn("ATRI-X 定时报告", got[0][1])
        self.assertIn("窗口：最近 12 小时", got[0][1])

    async def test_run_daily_report_accepts_sync_sender(self) -> None:
        service = self._service([])
        self._patch_reader(service)
        got: list[str] = []
        service.report_sender = lambda ctx, text: got.append(text)  # noqa: ARG005
        await service.run_daily_report(None)
        self.assertEqual(len(got), 1)

    async def test_window_falls_back_to_12_hours(self) -> None:
        service = self._service([])
        self._patch_reader(service)
        service.report.window_hours = 0
        got: list[str] = []
        service.report_sender = lambda ctx, text: got.append(text)  # noqa: ARG005
        await service.run_daily_report(None)
        # 与 Go 一致：「窗口：最近 N 小时」直接打印配置值（这里是 0），
        # 但实际统计周期回落到 12 小时。
        self.assertIn("窗口：最近 0 小时", got[0])
        period = got[0].splitlines()[1]
        stamp = period.split("周期：", 1)[1]
        start_text, end_text = stamp.split(" ~ ")
        fmt = "%Y-%m-%d %H:%M"
        start = datetime.strptime(start_text, fmt)
        end = datetime.strptime(end_text, fmt)
        self.assertAlmostEqual((end - start).total_seconds(), 12 * 3600, delta=60)

    async def test_build_report_requires_log_manager(self) -> None:
        service = new_service(None, None, None, None)
        service.report = DailyReportConfig()
        from atri_x.errors import InvalidError

        with self.assertRaises(InvalidError):
            await service.build_daily_report(
                None,
                datetime(2026, 10, 11, tzinfo=timezone.utc),
                datetime(2026, 10, 12, tzinfo=timezone.utc),
            )

    async def test_query_failure_propagates(self) -> None:
        service = self._service([], fail_query=True)
        self._patch_reader(service)
        with self.assertRaises(OSError):
            await service.build_daily_report(
                None,
                datetime(2026, 10, 11, tzinfo=timezone.utc),
                datetime(2026, 10, 12, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
