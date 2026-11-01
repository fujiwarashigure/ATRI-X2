"""DSH 1.1.8 调度层离线测试：并发槽、忙回退、看门狗、空闲回收、会话亲和。

全部用 ``tests/_dsh_fake_acp_server.py`` 真跑子进程（不依赖 Node / 真 dsh），
覆盖三层：

1. :class:`atri_x.agent.dsh.scheduler.TurnScheduler` —— 槽位/等待/忙拒/看门狗/空闲；
2. :class:`atri_x.agent.dsh.affinity.SessionAffinity` —— 连续性校验/TTL/LRU；
3. :class:`atri_x.agent.dsh.runtime.DshRuntime` —— 多会话真并发、卡死取消、预热、
   空闲回收、会话复用（只发增量）。
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

from atri_x.agent.dsh.affinity import SessionAffinity
from atri_x.agent.dsh.router import DshRouteConfig
from atri_x.agent.dsh.runtime import DshRuntime
from atri_x.agent.dsh.scheduler import DshBusyError, TurnScheduler
from atri_x.agent.dsh.turn import DshRunResult, DshTurnPlan
from atri_x.agent.dsh.workspace import WorkspacePaths
from atri_x.llm import (
    LLMMessage,
    RoleAssistant,
    RoleSystem,
    RoleUser,
    text_segments,
)

FAKE_SERVER = Path(__file__).resolve().parent / "_dsh_fake_acp_server.py"
TMP_ROOT = Path(__file__).resolve().parent / "_tmp_dsh_sched"


def _posix(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _fake_command() -> str:
    return f"{_posix(sys.executable)} -u {_posix(FAKE_SERVER)}"


def _config(**overrides: object) -> DshRouteConfig:
    values: dict[str, object] = {
        "enabled": True,
        "min_score": 0.0,
        "lambda_penalty": 0.0,
        "cost_ratio": 1.0,
        "command": _fake_command(),
        "timeout_seconds": 20.0,
        "max_history_messages": 8,
        "prompt_char_budget": 12000,
        "workspace": _posix(TMP_ROOT / "workspace"),
        "home": _posix(TMP_ROOT / "home"),
        "circuit_breaker_failures": 3,
        "circuit_breaker_cooldown_seconds": 300.0,
        "max_concurrent_prompts": 2,
        "busy_wait_seconds": 1.0,
        "stall_timeout_seconds": 0.0,
        "idle_timeout_seconds": 0.0,
        "session_affinity": False,
        "prewarm": False,
    }
    values.update(overrides)
    return DshRouteConfig(**values)  # type: ignore[arg-type]


def _runtime(config: DshRouteConfig, *, monitor_interval: float = 0.0) -> DshRuntime:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return DshRuntime(
        config,
        command=_fake_command(),
        paths=WorkspacePaths(
            workspace=str(TMP_ROOT / "ws"), home=str(TMP_ROOT / "home")
        ),
        monitor_interval=monitor_interval,
    )


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage(role=role, segments=text_segments(text))


def _messages(text: str = "问题一") -> list[LLMMessage]:
    return [_msg(RoleSystem, "你是 ATRI。"), _msg(RoleUser, text)]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class SchedulerTest(unittest.TestCase):
    """并发槽 / 忙拒 / 看门狗 / 空闲回收的纯单元测试。"""

    def test_admit_and_busy_reject(self) -> None:
        scheduler = TurnScheduler(max_concurrent=2, busy_wait_seconds=0.0)
        scheduler.admit()
        scheduler.admit()
        with self.assertRaises(DshBusyError):
            scheduler.admit()
        self.assertEqual(scheduler.stats.busy_rejected, 1)
        self.assertEqual(scheduler.snapshot()["active"], 0)
        scheduler.release()
        scheduler.release()
        scheduler.admit()  # 释放后可以再拿
        scheduler.release()

    def test_admit_waits_for_release(self) -> None:
        scheduler = TurnScheduler(max_concurrent=1, busy_wait_seconds=2.0)
        scheduler.admit()

        def release_later() -> None:
            time.sleep(0.15)
            scheduler.release()

        import threading

        worker = threading.Thread(target=release_later)
        worker.start()
        waited = scheduler.admit()
        worker.join(timeout=2.0)
        self.assertGreater(waited, 0.05)
        self.assertEqual(scheduler.stats.busy_rejected, 0)
        scheduler.release()

    def test_watchdog_cancels_stalled_turn(self) -> None:
        scheduler = TurnScheduler(
            max_concurrent=1,
            stall_timeout_seconds=0.2,
            interval=0.05,
        )
        cancelled: list[str] = []
        scheduler.start()
        try:
            scheduler.register(object(), "sess-1", lambda: cancelled.append("sess-1"))
            self.assertTrue(_wait_for(lambda: bool(cancelled), 3.0))
            self.assertEqual(scheduler.stats.stalled, 1)
        finally:
            scheduler.stop()

    def test_idle_callback(self) -> None:
        calls: list[bool] = []
        scheduler = TurnScheduler(
            max_concurrent=1,
            idle_timeout_seconds=0.2,
            on_idle=lambda: calls.append(True),
            interval=0.05,
        )
        scheduler.start()
        try:
            self.assertTrue(_wait_for(lambda: bool(calls), 3.0))
        finally:
            scheduler.stop()

    def test_snapshot_shape(self) -> None:
        scheduler = TurnScheduler(max_concurrent=3, busy_wait_seconds=0.5)
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["maxConcurrent"], 3)
        self.assertEqual(snapshot["active"], 0)
        self.assertEqual(snapshot["availableSlots"], 3)
        self.assertEqual(snapshot["stats"]["admitted"], 0)


class AffinityTest(unittest.TestCase):
    """会话亲和的连续性校验 / TTL / LRU。"""

    def test_continues_and_hits(self) -> None:
        affinity = SessionAffinity(max_entries=2, ttl_seconds=60.0)
        first = _messages("第一问")
        affinity.store("chat-1", "sess-1", first)
        second = first + [
            _msg(RoleAssistant, "第一答"),
            _msg(RoleUser, "第二问"),
        ]
        entry = affinity.lookup("chat-1", second)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.session_id, "sess-1")
        self.assertEqual(entry.hits, 1)

    def test_wrong_length_is_stale(self) -> None:
        affinity = SessionAffinity(max_entries=2)
        first = _messages("第一问")
        affinity.store("chat-1", "sess-1", first)
        # 中间只多了一条助手回复（没有再轮用户消息）→ 上下文不连续。
        broken = first + [_msg(RoleAssistant, "第一答")]
        entry, stale = affinity.consume("chat-1", broken)
        self.assertIsNone(entry)
        self.assertEqual([item.session_id for item in stale], ["sess-1"])
        self.assertEqual(len(affinity), 0)

    def test_ttl_expiry(self) -> None:
        clock = [1000.0]
        affinity = SessionAffinity(max_entries=2, ttl_seconds=10.0, clock=lambda: clock[0])
        first = _messages("第一问")
        affinity.store("chat-1", "sess-1", first)
        second = first + [_msg(RoleAssistant, "答"), _msg(RoleUser, "第二问")]
        clock[0] += 11.0
        self.assertIsNone(affinity.lookup("chat-1", second))

    def test_lru_eviction(self) -> None:
        affinity = SessionAffinity(max_entries=1)
        affinity.store("chat-1", "sess-1", _messages("一"))
        evicted = affinity.store("chat-2", "sess-2", _messages("二"))
        self.assertEqual(len(evicted), 1)
        self.assertEqual(evicted[0].session_id, "sess-1")
        self.assertEqual(affinity.keys(), ["chat-2"])

    def test_replaced_entry_is_returned(self) -> None:
        affinity = SessionAffinity(max_entries=2)
        first = _messages("一")
        affinity.store("chat-1", "sess-1", first)
        evicted = affinity.store("chat-1", "sess-9", first)
        self.assertEqual([item.session_id for item in evicted], ["sess-1"])


class RuntimeConcurrencyTest(unittest.TestCase):
    """真子进程：并发、忙回退、看门狗、预热、空闲回收、会话复用。"""

    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.runtime: DshRuntime | None = None

    def tearDown(self) -> None:
        if self.runtime is not None:
            self.runtime.close()

    def make(self, **overrides: object) -> DshRuntime:
        self.runtime = _runtime(_config(**overrides))
        return self.runtime

    def test_two_prompts_run_concurrently(self) -> None:
        runtime = self.make(max_concurrent_prompts=2, busy_wait_seconds=2.0)

        async def go() -> tuple[object, object]:
            token = object()
            hanging = asyncio.ensure_future(
                runtime.run("__HANG__ 并发第一轮", cancel_token=token)
            )
            await asyncio.sleep(0.4)  # 让第一轮先占住槽并挂在内核里
            second = await asyncio.wait_for(runtime.run("并发第二轮"), timeout=8.0)
            runtime.cancel(token)
            stopped = await asyncio.wait_for(hanging, timeout=8.0)
            return second, stopped

        second, stopped = asyncio.run(go())
        self.assertIsInstance(second, DshRunResult)
        self.assertTrue(second.ok)  # 第二轮没被第一轮阻塞
        self.assertEqual(second.text, "你好，世界")
        self.assertEqual(stopped.stop_reason, "cancelled")
        self.assertEqual(runtime.describe()["scheduler"]["maxConcurrent"], 2)

    def test_busy_falls_back_quickly(self) -> None:
        runtime = self.make(max_concurrent_prompts=1, busy_wait_seconds=0.2)

        async def go() -> float:
            token = object()
            hanging = asyncio.ensure_future(runtime.run("__HANG__ 占槽", cancel_token=token))
            await asyncio.sleep(0.4)
            started = time.monotonic()
            with self.assertRaises(DshBusyError):
                await asyncio.wait_for(runtime.run("第二轮"), timeout=8.0)
            elapsed = time.monotonic() - started
            runtime.cancel(token)
            await asyncio.wait_for(hanging, timeout=8.0)
            return elapsed

        elapsed = asyncio.run(go())
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(runtime.describe()["scheduler"]["stats"]["busy_rejected"], 1)

    def test_stall_watchdog_cancels(self) -> None:
        runtime = self.make(stall_timeout_seconds=0.4)
        runtime._scheduler._interval = 0.05  # 测试里把监控轮询调快

        async def go() -> None:
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(runtime.run("__HANG__ 卡死"), timeout=8.0)
            self.assertIn("卡死", str(caught.exception))
            self.assertLess(time.monotonic() - started, 5.0)

        asyncio.run(go())
        self.assertEqual(runtime.describe()["scheduler"]["stats"]["stalled"], 1)

    def test_prewarm_starts_kernel(self) -> None:
        runtime = self.make()
        self.assertTrue(runtime.prewarm())
        described = runtime.describe()
        self.assertTrue(described["running"])
        self.assertTrue(described["prewarmed"])
        self.assertEqual(described["scheduler"]["stats"]["prewarmed"], 1)

    def test_idle_timeout_closes_kernel(self) -> None:
        runtime = self.make(idle_timeout_seconds=0.3)
        runtime._scheduler._interval = 0.05
        asyncio.run(runtime.run("普通提问"))
        self.assertTrue(runtime.describe()["running"])
        self.assertTrue(
            _wait_for(lambda: not runtime.describe()["running"], 5.0), "内核没有被空闲回收"
        )
        self.assertEqual(runtime.describe()["scheduler"]["stats"]["idle_closes"], 1)

    def test_session_affinity_sends_increment_only(self) -> None:
        runtime = self.make(session_affinity=True)
        first = _messages("第一问")
        plan = runtime.plan_turn("chat-1", first, handoff_prompt="HANDOFF-完整交接一")
        self.assertFalse(plan.reused)
        result = asyncio.run(runtime.run(plan=plan))
        self.assertTrue(result.ok)
        self.assertFalse(result.session_reused)

        second = first + [
            _msg(RoleAssistant, "第一答"),
            _msg(RoleUser, "第二问 __ECHO_PROMPT__"),
        ]
        plan = runtime.plan_turn("chat-1", second, handoff_prompt="HANDOFF-完整交接二")
        self.assertTrue(plan.reused)
        self.assertEqual(plan.session_id, result.session_id)
        reused = asyncio.run(runtime.run(plan=plan))
        self.assertTrue(reused.ok)
        self.assertTrue(reused.session_reused)
        self.assertEqual(reused.text, plan.prompt)  # 内核只收到了增量
        self.assertNotIn("HANDOFF", reused.text)

    def test_affinity_broken_by_non_routed_turn(self) -> None:
        runtime = self.make(session_affinity=True)
        first = _messages("第一问")
        plan = runtime.plan_turn("chat-1", first, handoff_prompt="HANDOFF-一")
        asyncio.run(runtime.run(plan=plan))
        # 中间夹了一轮原生（历史长度 +1，不是 +2）→ 必须放弃复用。
        broken = first + [_msg(RoleAssistant, "原生回答")]
        plan = runtime.plan_turn("chat-1", broken, handoff_prompt="HANDOFF-二")
        self.assertFalse(plan.reused)
        self.assertEqual(plan.session_id, "")

    def test_affinity_disabled_never_reuses(self) -> None:
        runtime = self.make(session_affinity=False)
        first = _messages("第一问")
        asyncio.run(runtime.run(plan=runtime.plan_turn("chat-1", first, handoff_prompt="H1")))
        second = first + [_msg(RoleAssistant, "答"), _msg(RoleUser, "第二问")]
        plan = runtime.plan_turn("chat-1", second, handoff_prompt="H2")
        self.assertFalse(plan.reused)
        self.assertIsInstance(plan, DshTurnPlan)


if __name__ == "__main__":  # pragma: no cover - 手工调试入口
    unittest.main()
