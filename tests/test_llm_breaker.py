"""``atri_x.llm.breaker`` 测试。

对应 Go：``internal/llm/breaker/breaker_test.go``（单个熔断状态机）与
``internal/app/breaker_llm_test.go`` / ``models.go``（按 Provider 各一个熔断器、
``ErrOpen`` 拒绝请求、状态供 ``/healthz`` / ``/metrics`` 展示）。
"""

from __future__ import annotations

import threading
import unittest

from atri_x.errors import OpenError
from atri_x.llm.breaker import (
    STATE_CLOSED,
    STATE_DISABLED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    Breaker,
    Config,
    ProviderBreakers,
)


class _Clock:
    """可注入的假时钟（秒）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestBreaker(unittest.TestCase):
    def test_open_cooldown_and_half_open_recovery(self) -> None:
        clock = _Clock()
        breaker = Breaker(Config(failure_threshold=2, open_cooldown=1.0, half_open_max=1), now=clock)

        self.assertTrue(breaker.allow())
        breaker.failure()
        self.assertTrue(breaker.allow(), "one failure should not open")
        breaker.failure()
        self.assertFalse(breaker.allow(), "breaker should be open")
        self.assertEqual(breaker.state(), STATE_OPEN)

        clock.advance(1.0)
        self.assertTrue(breaker.allow(), "breaker should permit one half-open probe")
        self.assertEqual(breaker.state(), STATE_HALF_OPEN)
        self.assertFalse(breaker.allow(), "breaker should allow only one half-open probe")
        breaker.success()
        self.assertTrue(breaker.allow(), "successful probe should close breaker")
        self.assertEqual(breaker.state(), STATE_CLOSED)

    def test_half_open_failure_reopens(self) -> None:
        clock = _Clock()
        breaker = Breaker(Config(failure_threshold=1, open_cooldown=1.0), now=clock)
        breaker.failure()
        self.assertFalse(breaker.allow())
        clock.advance(1.0)
        self.assertTrue(breaker.allow(), "half-open probe should be allowed")
        breaker.failure()
        self.assertFalse(breaker.allow(), "failed probe should reopen breaker")

    def test_failure_while_open_refreshes_cooldown(self) -> None:
        clock = _Clock()
        breaker = Breaker(Config(failure_threshold=1, open_cooldown=1.0), now=clock)
        breaker.failure()
        clock.advance(0.5)
        breaker.failure()  # open 状态下再失败：只刷新 opened_at
        clock.advance(0.5)
        self.assertFalse(breaker.allow(), "cooldown must restart from the latest failure")

    def test_disabled(self) -> None:
        breaker = Breaker(Config(failure_threshold=0))
        for _ in range(10):
            self.assertTrue(breaker.allow())
            breaker.failure()
        self.assertEqual(breaker.state(), STATE_DISABLED)
        self.assertFalse(breaker.enabled())
        self.assertEqual(breaker.snapshot()["state"], STATE_DISABLED)

    def test_defaults_match_go(self) -> None:
        breaker = Breaker(Config(failure_threshold=1, open_cooldown=0, half_open_max=0))
        self.assertEqual(breaker.cfg.open_cooldown, 60.0)
        self.assertEqual(breaker.cfg.half_open_max, 1)

    def test_half_open_max_allows_configured_probes(self) -> None:
        clock = _Clock()
        breaker = Breaker(
            Config(failure_threshold=1, open_cooldown=1.0, half_open_max=2), now=clock
        )
        breaker.failure()
        clock.advance(1.0)
        self.assertTrue(breaker.allow())
        self.assertTrue(breaker.allow())
        self.assertFalse(breaker.allow())

    def test_reset_clears_state(self) -> None:
        breaker = Breaker(Config(failure_threshold=1))
        breaker.failure()
        self.assertEqual(breaker.state(), STATE_OPEN)
        breaker.reset()
        self.assertEqual(breaker.state(), STATE_CLOSED)
        self.assertTrue(breaker.allow())


class TestProviderBreakers(unittest.TestCase):
    def test_per_provider_isolation(self) -> None:
        clock = _Clock()
        breakers = ProviderBreakers(
            Config(failure_threshold=2, open_cooldown=1.0), now=clock
        )
        breakers.record_failure("a")
        breakers.record_failure("a")
        self.assertTrue(breakers.is_open("a"))
        self.assertFalse(breakers.allow("a"))
        self.assertFalse(breakers.is_open("b"))
        self.assertTrue(breakers.allow("b"))
        self.assertEqual(breakers.state("a"), STATE_OPEN)
        self.assertEqual(breakers.state("b"), STATE_CLOSED)
        self.assertEqual(breakers.state(), {"a": STATE_OPEN, "b": STATE_CLOSED})

    def test_check_raises_err_open(self) -> None:
        breakers = ProviderBreakers(Config(failure_threshold=1))
        breakers.record_failure("primary")
        with self.assertRaises(OpenError) as caught:
            breakers.check("primary")
        self.assertIn("provider=primary", str(caught.exception))

    def test_is_open_peeks_without_consuming_half_open_slot(self) -> None:
        clock = _Clock()
        breakers = ProviderBreakers(
            Config(failure_threshold=1, open_cooldown=1.0, half_open_max=1), now=clock
        )
        breakers.record_failure("p")
        self.assertTrue(breakers.is_open("p"))
        clock.advance(1.0)
        # 冷却期已过：is_open 不再报「拒绝」（下一次 allow 会放行一个探测），
        # 且不消耗半开名额。
        self.assertFalse(breakers.is_open("p"))
        self.assertTrue(breakers.allow("p"))
        self.assertTrue(breakers.is_open("p"))

    def test_success_closes_and_reset_clears(self) -> None:
        breakers = ProviderBreakers(Config(failure_threshold=1))
        breakers.record_failure("p")
        self.assertTrue(breakers.is_open("p"))
        breakers.record_success("p")
        self.assertFalse(breakers.is_open("p"))
        breakers.record_failure("p")
        breakers.reset("p")
        self.assertFalse(breakers.is_open("p"))
        breakers.record_failure("p")
        breakers.reset()
        self.assertFalse(breakers.is_open("p"))

    def test_disabled_registry(self) -> None:
        breakers = ProviderBreakers(Config(failure_threshold=0))
        for _ in range(5):
            breakers.record_failure("p")
        self.assertFalse(breakers.is_open("p"))
        self.assertTrue(breakers.allow("p"))
        self.assertEqual(breakers.state("p"), STATE_DISABLED)
        breakers.check("p")  # 不抛

    def test_per_provider_config_override(self) -> None:
        breakers = ProviderBreakers(
            Config(failure_threshold=5), configs={"p": Config(failure_threshold=1)}
        )
        breakers.record_failure("p")
        self.assertTrue(breakers.is_open("p"))
        for _ in range(4):
            breakers.record_failure("q")
        self.assertFalse(breakers.is_open("q"))

    def test_snapshot_reports_counts(self) -> None:
        breakers = ProviderBreakers(Config(failure_threshold=3))
        breakers.record_failure("p")
        snapshot = breakers.snapshot()
        self.assertEqual(snapshot["p"]["state"], STATE_CLOSED)
        self.assertEqual(snapshot["p"]["failures"], 1)
        self.assertTrue(snapshot["p"]["enabled"])
        self.assertEqual(breakers.providers(), ["p"])

    def test_concurrent_use_is_safe(self) -> None:
        breakers = ProviderBreakers(Config(failure_threshold=10, open_cooldown=0.01))
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(200):
                    breakers.allow("p")
                    breakers.record_failure("p")
                    breakers.state("p")
                    breakers.record_success("p")
            except BaseException as exc:  # noqa: BLE001 - 测试要收集线程异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIn(breakers.state("p"), (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN))


if __name__ == "__main__":
    unittest.main()
