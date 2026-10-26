"""``atri_x.timesync.verifier`` 的单元测试：状态机、单飞、退避、挂起、告警锁存。

全部离线：SNTP 传输层注入，不联网。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import unittest

from atri_x.config.time_sections import TimeSourceConfig, TimeVerificationConfig
from atri_x.time_service import (
    VERIFY_CHECKING,
    VERIFY_CONFLICT,
    VERIFY_CONSISTENT,
    VERIFY_INSUFFICIENT,
    VERIFY_STALE,
    VERIFY_UNAVAILABLE,
    TimeService,
)
from atri_x.timesync import sntp
from atri_x.timesync.verifier import Verifier, VerifierOptions

_BASE = dt.datetime(2026, 3, 1, 1, 0, 0, tzinfo=dt.timezone.utc)


class FakeClock:
    """可推进的假时钟（墙钟与单调时钟分开推进，用于模拟休眠/校时）。"""

    def __init__(self) -> None:
        self.wall = _BASE
        self.mono = 1000.0

    def now_utc(self) -> dt.datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall = self.wall + dt.timedelta(seconds=seconds)
        self.mono += seconds

    def advance_wall_only(self, seconds: float) -> None:
        """只推进墙钟（模拟系统校时 / 休眠由外部补偿）。"""
        self.wall = self.wall + dt.timedelta(seconds=seconds)


def _service(clock: FakeClock) -> TimeService:
    return TimeService(
        "Asia/Shanghai", now_fn=clock.now_utc, monotonic_fn=clock.monotonic
    )


def _source(name: str, group: str = "") -> TimeSourceConfig:
    return TimeSourceConfig(
        id=name,
        protocol="sntp",
        host="10.0.0.1",
        group=group or name,
        enabled=True,
        allow_private=True,
    )


class VerifierHarness:
    """按来源 id 路由假传输层。"""

    def __init__(self, clock: FakeClock, behaviour: dict[str, object]) -> None:
        self.clock = clock
        self.behaviour = behaviour  # id -> offset_ms(float) | "timeout" | "garbage"
        self.current_source = ""
        self.calls: list[str] = []

    async def sntp_transport(self, host: str, port: int, payload: bytes, timeout: float) -> bytes:
        source_id = self.current_source
        self.calls.append(source_id)
        action = self.behaviour.get(source_id, "timeout")
        if action == "timeout":
            raise asyncio.TimeoutError()
        if action == "garbage":
            return b"\x00" * 10
        remote = self.clock.wall + dt.timedelta(milliseconds=float(action))
        return self._response(remote, payload[40:48])

    @staticmethod
    def _response(remote: dt.datetime, originate: bytes) -> bytes:
        packet = bytearray(48)
        packet[0] = (0 << 6) | (4 << 3) | 4
        packet[1] = 1
        packet[24:32] = originate
        packet[32:40] = sntp.encode_timestamp(remote)
        packet[40:48] = sntp.encode_timestamp(remote)
        return bytes(packet)


def _build(
    harness: VerifierHarness,
    sources: list[TimeSourceConfig],
    *,
    max_sample_age: int = 21600,
    transport=None,
    **config_overrides,
) -> tuple[Verifier, FakeClock]:
    kwargs = dict(
        enabled=True,
        interval_seconds=1800,
        timeout_seconds=0.2,
        round_deadline_seconds=2.0,
        max_retries=0,
    )
    kwargs.update(config_overrides)
    config = TimeVerificationConfig(**kwargs)
    clock = harness.clock
    service = _service(clock)
    harness.current_source = ""  # 由 _observe 包装设置

    class _Verifier(Verifier):
        async def _observe(self, source):  # type: ignore[override]
            harness.current_source = str(getattr(source, "id", "") or "?")
            return await super()._observe(source)

    verifier = _Verifier(
        VerifierOptions(
            config=config,
            time_service=service,
            sources=sources,
            sntp_transport=transport or harness.sntp_transport,
            policy=_AllowAllPolicy(),
            max_sample_age_seconds=max_sample_age,
        )
    )
    return verifier, clock


class _AllowAllPolicy:
    async def check_source(self, source):
        return None


class StateMachineTest(unittest.IsolatedAsyncioTestCase):
    async def test_consistent_with_two_groups(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 120.0, "b": 160.0})
        verifier, _ = _build(harness, [_source("a", "g1"), _source("b", "g2")])
        result = await verifier.check_round("manual")
        self.assertEqual(result.state, VERIFY_CONSISTENT)
        self.assertEqual(result.groups, 2)
        self.assertIsNotNone(result.offset_ms)
        self.assertTrue(120 <= (result.offset_ms or 0) <= 160)
        self.assertEqual(verifier.state(), VERIFY_CONSISTENT)

    async def test_single_group_is_insufficient_evidence(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 10.0, "b": 20.0})
        verifier, _ = _build(
            harness, [_source("a", "same"), _source("b", "same")], min_groups=2
        )
        result = await verifier.check_round("manual")
        self.assertEqual(result.state, VERIFY_INSUFFICIENT)
        self.assertEqual(result.groups, 1)

    async def test_conflicting_sources(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 100.0, "b": 5000.0})
        verifier, _ = _build(
            harness,
            [_source("a", "g1"), _source("b", "g2")],
            conflict_offset_ms=1000,
            warn_offset_ms=100,
        )
        result = await verifier.check_round("manual")
        self.assertEqual(result.state, VERIFY_CONFLICT)
        self.assertGreater(result.spread_ms or 0, 1000)

    async def test_all_sources_failing_is_unavailable(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": "timeout", "b": "garbage"})
        verifier, _ = _build(harness, [_source("a", "g1"), _source("b", "g2")])
        result = await verifier.check_round("manual")
        self.assertEqual(result.state, VERIFY_UNAVAILABLE)
        self.assertEqual(result.ok_count, 0)

    async def test_large_local_offset_is_conflict(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 60000.0, "b": 60000.0})
        verifier, _ = _build(
            harness,
            [_source("a", "g1"), _source("b", "g2")],
            conflict_offset_ms=30000,
        )
        result = await verifier.check_round("manual")
        self.assertEqual(result.state, VERIFY_CONFLICT)

    async def test_suspect_state_not_cleared_by_failures(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 100.0, "b": 5000.0})
        verifier, _ = _build(
            harness, [_source("a", "g1"), _source("b", "g2")], conflict_offset_ms=1000
        )
        first = await verifier.check_round("manual")
        self.assertEqual(first.state, VERIFY_CONFLICT)
        harness.behaviour = {"a": "timeout", "b": "timeout"}
        second = await verifier.check_round("manual")
        self.assertEqual(second.state, VERIFY_CONFLICT)  # 网络失败不能洗掉告警
        self.assertIn("suspect state kept", second.message)

    async def test_clock_jump_during_round_invalidates_samples(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 100.0, "b": 120.0})
        original = harness.sntp_transport

        async def jumping_transport(host, port, payload, timeout):
            clock.advance_wall_only(3600)  # 一轮里墙钟前进一小时（系统校时）
            return await original(host, port, payload, timeout)

        verifier, _ = _build(
            harness,
            [_source("a", "g1"), _source("b", "g2")],
            transport=jumping_transport,
        )
        result = await verifier.check_round("manual")
        self.assertEqual(result.state, VERIFY_STALE)
        self.assertIn("clock jumped", result.message)


class SingleFlightTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_manual_call_reports_checking(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 100.0, "b": 100.0})
        gate = asyncio.Event()

        async def slow_transport(host, port, payload, timeout):
            await gate.wait()
            return await harness.sntp_transport(host, port, payload, timeout)

        verifier, _ = _build(
            harness,
            [_source("a", "g1"), _source("b", "g2")],
            transport=slow_transport,
            timeout_seconds=1.0,
            round_deadline_seconds=5.0,
        )
        task = asyncio.ensure_future(verifier.check_round("manual"))
        await asyncio.sleep(0.02)
        second = await verifier.check_round("manual")
        self.assertEqual(second.state, VERIFY_CHECKING)
        gate.set()
        first = await task
        self.assertEqual(first.state, VERIFY_CONSISTENT)
        self.assertEqual(verifier.state(), VERIFY_CONSISTENT)

    async def test_manual_cooldown(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 10.0, "b": 10.0})
        verifier, _ = _build(harness, [_source("a", "g1"), _source("b", "g2")])
        await verifier.check_round("manual")
        self.assertGreater(verifier.manual_cooldown_seconds(), 0.0)

    async def test_disabled_verifier_does_nothing(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 10.0})
        config = TimeVerificationConfig(enabled=False)
        verifier = Verifier(
            VerifierOptions(
                config=config,
                time_service=_service(clock),
                sources=[_source("a")],
                sntp_transport=harness.sntp_transport,
                policy=_AllowAllPolicy(),
            )
        )
        self.assertFalse(verifier.enabled())
        await verifier.start()
        self.assertEqual(verifier.state(), "disabled")
        self.assertEqual(harness.calls, [])


class SnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_reports_sources_and_age(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 10.0, "b": 20.0})
        verifier, _ = _build(
            harness, [_source("a", "g1"), _source("b", "g2")], max_sample_age=60
        )
        await verifier.check_round("manual")
        snapshot = verifier.snapshot()
        self.assertEqual(snapshot["state"], VERIFY_CONSISTENT)
        self.assertEqual(len(snapshot["sources"]), 2)
        self.assertIsNotNone(snapshot["last_check_at"])
        self.assertIsNotNone(snapshot["sample_age_seconds"])
        clock.advance(120)
        stale = verifier.snapshot()
        self.assertEqual(stale["state"], VERIFY_STALE)

    async def test_observations_cached(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 10.0, "b": 20.0})
        verifier, _ = _build(harness, [_source("a", "g1"), _source("b", "g2")])
        await verifier.check_round("manual")
        rows = verifier.observations()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["status"], "ok")
            self.assertFalse(row["authenticated"])  # SNTP 永不算"已认证"

    async def test_backoff_grows_after_failures(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": "timeout", "b": "timeout"})
        verifier, _ = _build(
            harness, [_source("a", "g1"), _source("b", "g2")], interval_seconds=60
        )
        base = verifier._next_interval()
        await verifier.check_round("manual")
        after = verifier._next_interval()
        self.assertGreater(after, base)

    async def test_start_stop_is_idempotent(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {"a": 10.0, "b": 10.0})
        verifier, _ = _build(harness, [_source("a", "g1"), _source("b", "g2")])
        await verifier.start()
        await verifier.start()
        await verifier.stop()
        await verifier.stop()


class WaitUntilTest(unittest.IsolatedAsyncioTestCase):
    async def test_past_target_returns_immediately(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {})
        verifier, _ = _build(harness, [_source("a")])
        self.assertTrue(await verifier._wait_until(clock.now_utc() - dt.timedelta(seconds=1)))

    async def test_large_backward_jump_recomputes(self) -> None:
        """等待期间墙钟被回拨超过 1 小时：不死等，返回 True 让外层重算。"""
        from unittest import mock

        clock = FakeClock()
        harness = VerifierHarness(clock, {})
        verifier, _ = _build(harness, [_source("a")])
        target = clock.now_utc() + dt.timedelta(hours=5)
        calls = {"n": 0}
        real_wait_for = asyncio.wait_for

        async def fake_wait_for(awaitable, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                clock.wall = clock.wall - dt.timedelta(hours=2)  # 第一段等待期间回拨
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout)

        with mock.patch("asyncio.wait_for", fake_wait_for):
            self.assertTrue(await verifier._wait_until(target))
        self.assertGreaterEqual(calls["n"], 1)

    async def test_stop_signal_returns_stopped(self) -> None:
        clock = FakeClock()
        harness = VerifierHarness(clock, {})
        verifier, _ = _build(harness, [_source("a")])
        verifier._stopping.set()
        self.assertEqual(
            await verifier._wait_until(clock.now_utc() + dt.timedelta(hours=1)),
            "stopped",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
