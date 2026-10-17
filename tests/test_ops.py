"""``atri_x.ops``、``atri_x.ratelimit`` 与 ``atri_x.sysinfo`` 的单元测试。

对应 Go：``internal/ops/*/``、``internal/ratelimit/``、``internal/sysinfo/``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
import uuid

from atri_x.errors import QueueFullError, QueueTimeoutError
from atri_x.ops.concurrency import Config as LimiterConfig
from atri_x.ops.concurrency import ErrFull, ErrTimeout, Limiter
from atri_x.ops.diskguard import (
    Config as DiskConfig,
)
from atri_x.ops.diskguard import Guard, Level, LevelCritical, LevelOK, LevelWarn
from atri_x.ops.ratelimit import Limit, Limiter as KeyLimiter
from atri_x.ratelimit import Window
from atri_x.safecontext import CancelledError, with_cancel
from atri_x.sysinfo import (
    Snapshot,
    collect,
    directory_size,
    disk_usage,
    format_bytes,
    format_uint_bytes,
    process_rss,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_temp_dir(case: unittest.TestCase) -> str:
    """工作区内的临时目录（0o777，见 tests/test_fileops.py 的说明）。"""
    while True:
        directory = os.path.join(_REPO_ROOT, ".ops-test-" + uuid.uuid4().hex[:12])
        if not os.path.exists(directory):
            break
    os.makedirs(directory, mode=0o777)
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return directory


class TestConcurrencyLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_returns_noop(self) -> None:
        limiter = Limiter(LimiterConfig(max=0))
        release = await limiter.acquire(None)
        release()
        release()  # 幂等
        self.assertEqual(limiter.stats(), (0, 0))

    async def test_concurrency_and_fifo_handoff(self) -> None:
        limiter = Limiter(LimiterConfig(max=1, queue_size=4, wait_timeout=2.0))
        release = await limiter.acquire(None)
        self.assertEqual(limiter.stats(), (1, 0))
        waiting = asyncio.ensure_future(limiter.acquire(None))
        for _ in range(500):
            if limiter.stats()[1] == 1:
                break
            await asyncio.sleep(0.002)
        self.assertEqual(limiter.stats(), (1, 1))
        release()
        release_waiter = await asyncio.wait_for(waiting, 2)
        # 让位不减少 active：槽位直接交给等待者。
        self.assertEqual(limiter.stats(), (1, 0))
        release_waiter()
        self.assertEqual(limiter.stats(), (0, 0))

    async def test_queue_full(self) -> None:
        limiter = Limiter(LimiterConfig(max=1, queue_size=1, wait_timeout=2.0))
        release = await limiter.acquire(None)
        waiter = asyncio.ensure_future(limiter.acquire(None))
        for _ in range(500):
            if limiter.stats()[1] == 1:
                break
            await asyncio.sleep(0.002)
        with self.assertRaises(QueueFullError) as caught:
            await limiter.acquire(None)
        self.assertIs(caught.exception, ErrFull)
        release()
        (await asyncio.wait_for(waiter, 2))()

    async def test_wait_timeout(self) -> None:
        limiter = Limiter(LimiterConfig(max=1, queue_size=4, wait_timeout=0.01))
        release = await limiter.acquire(None)
        with self.assertRaises(QueueTimeoutError) as caught:
            await limiter.acquire(None)
        self.assertIs(caught.exception, ErrTimeout)
        self.assertEqual(limiter.stats(), (1, 0))
        release()

    async def test_context_cancel(self) -> None:
        limiter = Limiter(LimiterConfig(max=1, queue_size=4, wait_timeout=5.0))
        release = await limiter.acquire(None)
        ctx = with_cancel(None)
        waiter = asyncio.ensure_future(limiter.acquire(ctx))
        for _ in range(500):
            if limiter.stats()[1] == 1:
                break
            await asyncio.sleep(0.002)
        ctx.cancel("test")
        with self.assertRaises(CancelledError):
            await asyncio.wait_for(waiter, 2)
        self.assertEqual(limiter.stats()[1], 0)
        release()

    async def test_queue_size_zero_rejects_waiters(self) -> None:
        limiter = Limiter(LimiterConfig(max=1))
        release = await limiter.acquire(None)
        with self.assertRaises(QueueFullError):
            await limiter.acquire(None)
        release()


class TestDiskGuard(unittest.IsolatedAsyncioTestCase):
    async def test_levels_from_injected_probe(self) -> None:
        state = {"total": 1000, "free": 500}
        clock = {"now": 100.0}
        guard = Guard("/tmp", DiskConfig(cache_ttl=5.0), lambda root: (state["total"], state["free"]))
        level, critical = await guard.check(None)
        self.assertEqual((level, critical), (LevelOK, False))
        state["free"] = 100  # used = 0.9 -> warn
        guard._checked -= 10
        self.assertEqual(await guard.check(None), (LevelWarn, False))
        state["free"] = 40  # used = 0.96 -> critical
        guard._checked -= 10
        self.assertEqual(await guard.check(None), (LevelCritical, True))
        state["free"] = 1000
        guard._checked -= 10
        self.assertEqual(await guard.check(None), (LevelOK, False))
        _ = clock

    async def test_cache_ttl_avoids_reprobe(self) -> None:
        calls = {"count": 0}

        def probe(root: str) -> tuple[int, int]:
            calls["count"] += 1
            return 1000, 10

        guard = Guard("/tmp", DiskConfig(cache_ttl=60.0), probe)
        self.assertEqual((await guard.check(None))[0], LevelCritical)
        self.assertEqual((await guard.check(None))[0], LevelCritical)
        self.assertEqual(calls["count"], 1)

    async def test_min_free_bytes_overrides_ratio(self) -> None:
        guard = Guard(
            "/tmp",
            DiskConfig(min_free_bytes=500, cache_ttl=60.0),
            lambda root: (10_000, 400),
        )
        level, critical = await guard.check(None)
        self.assertEqual(level, LevelCritical)
        self.assertTrue(critical)
        self.assertIsNotNone(guard.error(level))
        self.assertIsNone(guard.error(LevelOK))

    async def test_defaults_and_probe_failure(self) -> None:
        guard = Guard("/tmp", DiskConfig())
        self.assertEqual(guard.config.warn_ratio, 0.85)
        self.assertEqual(guard.config.critical_ratio, 0.95)
        self.assertEqual(guard.config.cache_ttl, 5.0)

        def broken(root: str) -> tuple[int, int]:
            raise OSError("no such device")

        failing = Guard("/tmp", DiskConfig(cache_ttl=60.0), broken)
        level, critical = await failing.check(None)
        self.assertEqual((level, critical), (LevelOK, False))
        self.assertIsInstance(failing.last_error, OSError)

    async def test_cancelled_context_raises(self) -> None:
        guard = Guard("/tmp", DiskConfig(), lambda root: (1000, 500))
        ctx = with_cancel(None)
        ctx.cancel("test")
        with self.assertRaises(CancelledError):
            await guard.check(ctx)

    async def test_zero_total_reports_ok(self) -> None:
        guard = Guard("/tmp", DiskConfig(cache_ttl=60.0), lambda root: (0, 0))
        self.assertEqual(await guard.check(None), (LevelOK, False))


class TestKeyedRateLimiter(unittest.TestCase):
    def test_disabled_and_blank_keys_are_allowed(self) -> None:
        limiter = KeyLimiter(Limit(rate_per_second=0, burst=1))
        for _ in range(5):
            self.assertTrue(limiter.allow("k").allowed)
        self.assertEqual(limiter.keys(), 0)
        enabled = KeyLimiter(Limit(rate_per_second=1, burst=1))
        self.assertTrue(enabled.allow("   ").allowed)
        self.assertEqual(enabled.keys(), 0)

    def test_burst_then_retry_after(self) -> None:
        now = {"value": 0.0}
        limiter = KeyLimiter(Limit(rate_per_second=2.0, burst=2))
        limiter.now = lambda: now["value"]
        first = limiter.allow("k")
        self.assertTrue(first.allowed)
        self.assertEqual(first.remaining, 1)
        second = limiter.allow("k")
        self.assertTrue(second.allowed)
        self.assertEqual(second.remaining, 0)
        third = limiter.allow("k")
        self.assertFalse(third.allowed)
        self.assertEqual(third.remaining, 0)
        # 还差一个令牌，速率为 2/s → 0.5 秒
        self.assertAlmostEqual(third.retry_after, 0.5, places=6)

    def test_refill_is_capped_by_burst(self) -> None:
        now = {"value": 0.0}
        limiter = KeyLimiter(Limit(rate_per_second=1.0, burst=2))
        limiter.now = lambda: now["value"]
        limiter.allow("k")
        limiter.allow("k")
        now["value"] = 100.0
        decision = limiter.allow("k")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.remaining, 1)

    def test_idle_keys_are_cleaned(self) -> None:
        now = {"value": 0.0}
        limiter = KeyLimiter(Limit(rate_per_second=1.0, burst=100, idle_ttl=10.0))
        limiter.now = lambda: now["value"]
        for index in range(3):
            limiter.allow(f"k{index}")
        self.assertEqual(limiter.keys(), 3)
        now["value"] = 1000.0
        # 触发 cleanup（每 1024 次 allow）
        for _ in range(1024):
            limiter.allow("hot")
        self.assertLess(limiter.keys(), 3)

    def test_cleanup_when_bucket_count_grows(self) -> None:
        now = {"value": 0.0}
        limiter = KeyLimiter(Limit(rate_per_second=1.0, burst=100, idle_ttl=1.0))
        limiter.now = lambda: now["value"]
        for index in range(4100):
            limiter.allow(f"k{index}")
        now["value"] = 10_000.0
        limiter.allow("trigger")  # 桶数 >= 4096 时也会清理
        self.assertLess(limiter.keys(), 4100)


class TestSlidingWindow(unittest.TestCase):
    def test_disabled_window_allows_everything(self) -> None:
        for window in (Window(), Window(0, 10.0), Window(5, 0.0)):
            for _ in range(10):
                self.assertTrue(window.allow("k"))
            self.assertEqual(len(window), 0)

    def test_max_hits_per_window(self) -> None:
        now = {"value": 0.0}
        window = Window(2, 10.0, lambda: now["value"])
        self.assertTrue(window.allow("k"))
        self.assertTrue(window.allow("k"))
        self.assertFalse(window.allow("k"))
        now["value"] = 5.0
        self.assertFalse(window.allow("k"))
        now["value"] = 10.5
        self.assertTrue(window.allow("k"))

    def test_refund_releases_one_hit(self) -> None:
        window = Window(1, 10.0, lambda: 0.0)
        self.assertTrue(window.allow("k"))
        self.assertFalse(window.allow("k"))
        window.refund("k")
        self.assertTrue(window.allow("k"))
        window.refund("missing")
        self.assertEqual(len(window), 1)
        window.refund("k")
        self.assertEqual(len(window), 0)
        self.assertEqual(window.max_hits, 1)
        self.assertEqual(window.window, 10.0)

    def test_sweep_drops_stale_keys(self) -> None:
        now = {"value": 0.0}
        window = Window(5, 10.0, lambda: now["value"])
        window.allow("a")
        window.allow("b")
        self.assertEqual(len(window), 2)
        now["value"] = 100.0
        window.allow("c")
        self.assertEqual(len(window), 1)


class TestSysinfo(unittest.TestCase):
    def test_format_bytes_matches_go(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1023), "1023 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(10 * 1024), "10.0 KB")
        self.assertEqual(format_bytes(200 * 1024), "200 KB")
        self.assertEqual(format_bytes(1024**3), "1.0 GB")
        self.assertEqual(format_uint_bytes(2048), "2.0 KB")

    def test_directory_size_and_collect(self) -> None:
        directory = make_temp_dir(self)
        sub = os.path.join(directory, "sub")
        os.makedirs(sub, mode=0o777, exist_ok=True)
        with open(os.path.join(directory, "a.txt"), "wb") as handle:
            handle.write(b"x" * 10)
        with open(os.path.join(sub, "b.txt"), "wb") as handle:
            handle.write(b"y" * 5)
        self.assertEqual(directory_size(directory), 15)
        self.assertEqual(directory_size(""), 0)
        self.assertEqual(directory_size(os.path.join(directory, "missing")), 0)
        snapshot = collect(directory)
        self.assertIsInstance(snapshot, Snapshot)
        self.assertEqual(snapshot.dir_bytes, 15)
        self.assertGreater(snapshot.disk_total_bytes, 0)
        self.assertGreaterEqual(snapshot.goroutines, 1)

    def test_disk_usage_and_rss_are_platform_safe(self) -> None:
        total, free = disk_usage(os.getcwd())
        self.assertGreater(total, 0)
        self.assertLessEqual(free, total)
        self.assertIsInstance(process_rss(), int)
        self.assertGreaterEqual(process_rss(), 0)
