"""``atri_x.media.worker`` 的槽位生命周期、有界排队与取消竞态测试。

对应任务 T3（评审 §四.1）。这里盯住 ``worker.py`` 的三条不变量：

1. **槽位只在底层工作真正结束后释放** —— 等待方超时/被取消时只放弃等待，
   既不释放槽位、也不取消内部 future（所以"最多 N 个槽位"确实等于
   "最多 N 个真实存活的工作"）。
2. **有界排队** —— 排队者数量上限与排队等待超时都以 ``MediaError`` 报出，
   消息分别是 ``queue full`` / ``queue timed out``。
3. **取消竞态不泄漏槽位** —— ``_acquire_gate`` 在"槽位已到手但 ctx 已取消"
   时先归还槽位再抛；反复踩这个窗口之后闸门仍能被完整占满再放空。

测试**不靠 sleep 猜时间**：线程 → 事件循环的同步走 ``call_soon_threadsafe``，
短超时/小上限都通过 patch 模块常量（``IMAGE_WORKER_QUEUE_LIMIT`` /
``IMAGE_WORKER_QUEUE_TIMEOUT`` / ``_GATE_SLOTS``）得到；``until`` 只用于等待
"某条件最终成立"（有上限，失败时打印统计快照）。每个用例都在新的
``IsolatedAsyncioTestCase`` 事件循环里跑，因此模块级闸门统计是干净的。
"""

from __future__ import annotations

import asyncio
import gc
import threading
import unittest
from typing import Any, Callable
from unittest import mock

from atri_x.media import (
    MediaError,
    compress_image_isolated,
    compress_vision_image,
    image_dimensions,
    worker,
)
from atri_x.media.worker import (
    IMAGE_WORKER_QUEUE_LIMIT,
    IMAGE_WORKER_QUEUE_TIMEOUT,
    run_isolated,
    worker_stats,
)
from atri_x.safecontext import CancelToken
from atri_x.safecontext import CancelledError as SafeCancelledError
from tests.test_media_manager import AsyncMediaTestCase, make_png

media_worker = worker


class _ThreadBridge:
    """线程 → 事件循环的确定性信号。

    工作跑在线程里，测试跑在事件循环里；用 ``call_soon_threadsafe`` 把
    "线程真的开始了" 变成事件循环上的一个 ``asyncio.Event``，就不用靠 sleep。
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._fired = threading.Event()

    def fire(self) -> None:
        self._fired.set()
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:  # pragma: no cover - loop 已关闭（收尾竞态）
            pass

    async def wait(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._event.wait(), timeout)

    @property
    def fired(self) -> bool:
        return self._fired.is_set()


class WorkerTestCase(AsyncMediaTestCase):
    """共享辅助：条件等待 + 闸门占满/放空。"""

    async def until(
        self, predicate: Callable[[], bool], label: str, timeout: float = 10.0
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() >= deadline:
                self.fail(f"condition never held: {label}; stats={worker_stats()}")
            await asyncio.sleep(0.005)

    def gate(self) -> Any:
        return media_worker._gate()

    async def fill_slots(self, release: threading.Event) -> list[asyncio.Task[Any]]:
        """用真实工作占满全部槽位（每个工作都卡在 ``release`` 上）。"""
        slots = self.gate().slots

        def held() -> bytes:
            release.wait(10.0)
            return b"held"

        tasks = [
            asyncio.ensure_future(run_isolated(None, held, timeout=30.0))
            for _ in range(slots)
        ]
        await self.until(lambda: worker_stats()["active"] == slots, "all slots held")
        return tasks

    async def drain(self, tasks: list[asyncio.Task[Any]]) -> None:
        results = await asyncio.wait_for(asyncio.gather(*tasks), 20.0)
        self.assertEqual(len(results), len(tasks))
        await self.until(lambda: worker_stats()["active"] == 0, "gate drained")


# ---------------------------------------------------------------------------
# (a) 超时后槽位仍被占用
# ---------------------------------------------------------------------------


class TestSlotLifecycle(WorkerTestCase):
    async def test_orphan_keeps_its_slot_until_the_thread_ends(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Event()

        def slow() -> bytes:
            started.set()
            release.wait(10.0)
            return b"slow"

        with self.assertRaises(MediaError) as captured:
            await run_isolated(None, slow, timeout=0.05)
        self.assertIn("timed out", str(captured.exception))
        self.assertTrue(started.wait(5.0), "工作线程应该真的起来了")
        stats = worker_stats()
        self.assertEqual(stats["active"], 1, "超时只放弃等待，槽位仍被占用")
        self.assertEqual(stats["orphaned"], 1)
        self.assertEqual(stats["timed_out"], 1)

        release.set()
        await self.until(lambda: worker_stats()["active"] == 0, "orphan slot released")
        stats = worker_stats()
        self.assertEqual(stats["orphaned"], 0)
        self.assertEqual(stats["completed"], 1)

    async def test_second_caller_waits_for_the_orphan_slot(self) -> None:
        """单槽位闸门：孤儿占着唯一槽位时，第二个调用者只能排队（并排队超时）。"""
        with mock.patch.object(media_worker, "_GATE_SLOTS", 1):
            release = threading.Event()
            self.addCleanup(release.set)
            started = threading.Event()

            def slow() -> bytes:
                started.set()
                release.wait(10.0)
                return b"slow"

            with self.assertRaises(MediaError):
                await run_isolated(None, slow, timeout=0.05)
            self.assertTrue(started.wait(5.0))
            self.assertEqual(worker_stats()["slots"], 1)
            self.assertEqual(worker_stats()["active"], 1)

            with mock.patch.object(media_worker, "IMAGE_WORKER_QUEUE_TIMEOUT", 0.05):
                with self.assertRaises(MediaError) as captured:
                    await run_isolated(None, lambda: b"second", timeout=5.0)
            self.assertIn("queue timed out", str(captured.exception))
            self.assertEqual(worker_stats()["active"], 1, "槽位没有被超时的等待方带走")

            release.set()
            await self.until(lambda: worker_stats()["active"] == 0, "orphan finished")

    async def test_gate_slots_grow_by_one_after_the_orphan_ends(self) -> None:
        """验收 (a)：一个超时孤儿 + ``_GATE_SLOTS`` 个任务，最后一个要等孤儿结束。"""
        release_slow = threading.Event()
        self.addCleanup(release_slow.set)
        slow_started = threading.Event()

        def slow() -> bytes:
            slow_started.set()
            release_slow.wait(10.0)
            return b"slow"

        with self.assertRaises(MediaError):
            await run_isolated(None, slow, timeout=0.05)
        self.assertTrue(slow_started.wait(5.0))

        slots = self.gate().slots
        self.assertEqual(slots, media_worker._GATE_SLOTS)
        release_holders = threading.Event()
        self.addCleanup(release_holders.set)
        bridges = [_ThreadBridge() for _ in range(slots)]

        def holder(index: int) -> Callable[[], bytes]:
            def held() -> bytes:
                bridges[index].fire()
                release_holders.wait(10.0)
                return b"held"

            return held

        tasks = [
            asyncio.ensure_future(run_isolated(None, holder(i), timeout=30.0))
            for i in range(slots)
        ]
        # 孤儿占 1 个槽位，剩下 slots-1 个立刻开工；第 slots 个只能排队。
        for bridge in bridges[:-1]:
            await bridge.wait()
        self.assertFalse(bridges[-1].fired, "最后一个必须等槽位真正空出来")
        self.assertEqual(worker_stats()["active"], slots)
        self.assertEqual(worker_stats()["waiting"], 1)

        release_slow.set()  # 孤儿结束 → 归还槽位 → 第 slots 个才开始
        await bridges[-1].wait()
        await self.until(lambda: worker_stats()["waiting"] == 0, "queue drained")

        release_holders.set()
        await self.drain(tasks)
        self.assertEqual(worker_stats()["orphaned"], 0)


# ---------------------------------------------------------------------------
# (b) 取消竞态不泄漏槽位
# ---------------------------------------------------------------------------


class TestGateCancellationRace(WorkerTestCase):
    async def test_slot_free_but_ctx_cancelled_returns_the_slot(self) -> None:
        gate = media_worker._Gate(1)
        for _ in range(25):
            token = CancelToken.with_cancel()
            token.cancel("race")
            with self.assertRaises(SafeCancelledError):
                await media_worker._acquire_gate(gate, token)
        self.assertEqual(gate.active, 0, "到手即还，不能漏槽位")
        self.assertEqual(gate.waiting, 0)

    async def test_queued_waiter_cancelled_by_ctx_leaves_the_queue(self) -> None:
        gate = media_worker._Gate(1)
        self.assertTrue(gate.try_acquire())
        token = CancelToken.with_cancel()
        queued = asyncio.ensure_future(media_worker._acquire_gate(gate, token))
        await self.until(lambda: gate.waiting == 1, "waiter queued")
        token.cancel("test")
        with self.assertRaises(SafeCancelledError):
            await asyncio.wait_for(queued, 5.0)
        self.assertEqual(gate.waiting, 0)
        self.assertEqual(gate.active, 1, "原有槽位不能被排队的取消带走")
        gate.release()
        self.assertEqual(gate.active, 0)

    async def test_slot_handed_off_then_cancelled_is_returned(self) -> None:
        """交接竞态：release 已经把槽位交给等待者，但等待者恢复前被取消。"""
        gate = media_worker._Gate(1)
        self.assertTrue(gate.try_acquire())
        handoff = asyncio.ensure_future(media_worker._acquire_gate(gate, None))
        await self.until(lambda: gate.waiting == 1, "waiter queued")
        gate.release()  # 交接：active 不变，槽位归等待者
        self.assertEqual(gate.active, 1)
        handoff.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handoff
        self.assertEqual(gate.active, 0, "交接后取消必须把槽位还回来")
        self.assertEqual(gate.waiting, 0)

    async def test_gate_can_still_be_filled_and_drained(self) -> None:
        """验收 (b)：反复踩三轮竞态窗口后，闸门仍能占满再放空。"""
        gate = media_worker._Gate(media_worker._GATE_SLOTS)
        slots = gate.slots
        for _ in range(20):
            # A) 槽位空闲 + ctx 已取消：try_acquire 成功后必须归还。
            dead = CancelToken.with_cancel()
            dead.cancel("race")
            with self.assertRaises(SafeCancelledError):
                await media_worker._acquire_gate(gate, dead)

            # B) 占满后排队，ctx 取消：离开队列，原有槽位不受影响。
            for _ in range(slots):
                self.assertTrue(gate.try_acquire())
            token = CancelToken.with_cancel()
            queued = asyncio.ensure_future(media_worker._acquire_gate(gate, token))
            await self.until(lambda: gate.waiting == 1, "waiter queued")
            token.cancel("race")
            with self.assertRaises(SafeCancelledError):
                await asyncio.wait_for(queued, 5.0)
            self.assertEqual(gate.waiting, 0)
            self.assertEqual(gate.active, slots)

            # C) 交接瞬间取消：槽位被还回来。
            handoff = asyncio.ensure_future(media_worker._acquire_gate(gate, None))
            await self.until(lambda: gate.waiting == 1, "waiter queued")
            gate.release()
            self.assertEqual(gate.active, slots, "交接不改变 active")
            handoff.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await handoff
            self.assertEqual(gate.active, slots - 1)
            self.assertEqual(gate.waiting, 0)

            for _ in range(slots - 1):
                gate.release()
            self.assertEqual(gate.active, 0, "每一轮都要回到空闸门")

        holders = [
            asyncio.ensure_future(media_worker._acquire_gate(gate, None))
            for _ in range(slots)
        ]
        await asyncio.wait_for(asyncio.gather(*holders), 5.0)
        self.assertEqual(gate.active, slots, "竞态之后仍能被完整占满")
        self.assertEqual(gate.waiting, 0)
        for _ in range(slots):
            gate.release()
        self.assertEqual(gate.active, 0, "占满之后仍能放空")
        self.assertFalse(gate._waiters)


# ---------------------------------------------------------------------------
# (c)(d) 有界排队
# ---------------------------------------------------------------------------


class TestBoundedQueue(WorkerTestCase):
    async def test_queue_limit_is_reported(self) -> None:
        self.assertEqual(IMAGE_WORKER_QUEUE_LIMIT, 64)
        self.assertEqual(IMAGE_WORKER_QUEUE_TIMEOUT, 60.0)
        release = threading.Event()
        self.addCleanup(release.set)
        holders = await self.fill_slots(release)
        waiter = asyncio.ensure_future(run_isolated(None, lambda: b"waiter", timeout=30.0))
        await self.until(lambda: worker_stats()["waiting"] == 1, "one waiter queued")

        with mock.patch.object(media_worker, "IMAGE_WORKER_QUEUE_LIMIT", 1):
            with self.assertRaises(MediaError) as captured:
                await run_isolated(None, lambda: b"never", timeout=5.0)
        self.assertIn("queue full", str(captured.exception))
        self.assertNotIn("queue timed out", str(captured.exception))
        self.assertEqual(worker_stats()["rejected"], 1)

        release.set()
        await self.drain(holders + [waiter])

    async def test_queue_timeout_is_reported(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        holders = await self.fill_slots(release)
        started = threading.Event()

        def never() -> bytes:
            started.set()
            return b"never"

        with mock.patch.object(media_worker, "IMAGE_WORKER_QUEUE_TIMEOUT", 0.05):
            with self.assertRaises(MediaError) as captured:
                await run_isolated(None, never, timeout=5.0)
        self.assertIn("queue timed out", str(captured.exception))
        self.assertNotIn("queue full", str(captured.exception))
        self.assertFalse(started.is_set(), "排队超时的调用方不能真的开始工作")
        self.assertEqual(worker_stats()["queue_timed_out"], 1)
        self.assertEqual(worker_stats()["active"], self.gate().slots)
        self.assertEqual(worker_stats()["waiting"], 0)

        release.set()
        await self.drain(holders)

    async def test_free_slot_is_not_reported_as_queue_full(self) -> None:
        """有槽位可拿时不该走排队：上限再小也不误伤（快速路径不计数）。"""
        with mock.patch.object(media_worker, "IMAGE_WORKER_QUEUE_LIMIT", 1):
            self.assertEqual(await run_isolated(None, lambda: b"one", timeout=5.0), b"one")
            self.assertEqual(await run_isolated(None, lambda: b"two", timeout=5.0), b"two")
        self.assertEqual(worker_stats()["rejected"], 0)
        await self.until(lambda: worker_stats()["active"] == 0, "slots released")


# ---------------------------------------------------------------------------
# (e) 统计与外部契约
# ---------------------------------------------------------------------------


class TestStatsAndContract(WorkerTestCase):
    async def test_orphan_stats_return_to_zero(self) -> None:
        """验收 (e)：超时孤儿跑完后统计归零。"""
        release = threading.Event()
        self.addCleanup(release.set)
        baseline = len(media_worker._live_futures)

        def slow() -> bytes:
            release.wait(10.0)
            return b"slow"

        with self.assertRaises(MediaError):
            await run_isolated(None, slow, timeout=0.05)
        stats = worker_stats()
        self.assertEqual(
            set(stats),
            {
                "slots",
                "active",
                "waiting",
                "orphaned",
                "timed_out",
                "rejected",
                "queue_timed_out",
                "completed",
            },
        )
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["orphaned"], 1)
        self.assertEqual(stats["waiting"], 0)
        self.assertEqual(len(media_worker._live_futures), baseline + 1, "孤儿 future 被强引用按住")

        release.set()
        await self.until(lambda: worker_stats()["active"] == 0, "orphan finished")
        self.assertEqual(
            len(media_worker._live_futures), baseline, "跑完要清掉强引用集合"
        )

    async def test_waiter_cancellation_keeps_the_work_alive(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        outcome: list[str] = []

        def work() -> bytes:
            release.wait(10.0)
            outcome.append("finished")
            return b"kept"

        task = asyncio.ensure_future(run_isolated(None, work, timeout=10.0))
        await self.until(lambda: worker_stats()["active"] == 1, "work started")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        stats = worker_stats()
        self.assertEqual(stats["active"], 1, "取消等待方不释放槽位")
        self.assertEqual(stats["orphaned"], 1)
        self.assertEqual(outcome, [], "工作仍在跑，不能被取消")

        release.set()
        await self.until(lambda: worker_stats()["active"] == 0, "work finished")
        self.assertEqual(outcome, ["finished"], "内部 future 没有被取消")

    async def test_orphan_exception_is_consumed(self) -> None:
        loop = asyncio.get_running_loop()
        reported: list[dict[str, Any]] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        self.addCleanup(loop.set_exception_handler, previous)

        release = threading.Event()
        self.addCleanup(release.set)

        def boom() -> bytes:
            release.wait(10.0)
            raise RuntimeError("orphan boom")

        with self.assertRaises(MediaError):
            await run_isolated(None, boom, timeout=0.05)
        release.set()
        await self.until(lambda: worker_stats()["active"] == 0, "orphan finished")
        gc.collect()
        never_retrieved = [
            item for item in reported if "never retrieved" in str(item.get("message", ""))
        ]
        self.assertEqual(never_retrieved, [], "done 回调必须消费 exception()")

    async def test_task_error_propagates_and_releases_the_slot(self) -> None:
        def boom() -> bytes:
            raise ValueError("worker boom")

        with self.assertRaises(ValueError):
            await run_isolated(None, boom, timeout=5.0)
        await self.until(lambda: worker_stats()["active"] == 0, "slot released after error")
        self.assertEqual(worker_stats()["orphaned"], 0)

    async def test_none_timeout_waits_for_the_work(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def slow() -> bytes:
            release.wait(10.0)
            return b"done"

        task = asyncio.ensure_future(run_isolated(None, slow, timeout=None))
        await self.until(lambda: worker_stats()["active"] == 1, "work started")
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "timeout=None 不设工作超时")
        self.assertEqual(worker_stats()["orphaned"], 0)
        release.set()
        self.assertEqual(await asyncio.wait_for(task, 10.0), b"done")
        await self.until(lambda: worker_stats()["active"] == 0, "work finished")

    async def test_isolated_compression_contract_is_unchanged(self) -> None:
        output = await self.guard(
            compress_image_isolated(None, make_png(200, 100), 64 * 1024, 32)
        )
        self.assertEqual(image_dimensions(output), (31, 15))
        await self.until(lambda: worker_stats()["active"] == 0, "slot released")

        vision = await self.guard(
            compress_vision_image(None, make_png(200, 100), 64 * 1024, 32)
        )
        self.assertEqual(image_dimensions(vision), (31, 15))
        await self.until(lambda: worker_stats()["active"] == 0, "vision slot released")

    async def test_cancelled_context_short_circuits(self) -> None:
        token = CancelToken.with_cancel()
        token.cancel("test")
        with self.assertRaises(SafeCancelledError):
            await run_isolated(token, lambda: b"never", timeout=5.0)
        self.assertEqual(worker_stats()["active"], 0)
        self.assertEqual(worker_stats()["waiting"], 0)

    def test_worker_stats_shape_outside_a_loop(self) -> None:
        """诊断接口在事件循环外也能调用，字段固定（不依赖某个 loop）。"""
        expected = {
            "slots",
            "active",
            "waiting",
            "orphaned",
            "timed_out",
            "rejected",
            "queue_timed_out",
            "completed",
        }
        outside = worker_stats()
        self.assertEqual(set(outside), expected)
        self.assertGreaterEqual(outside["slots"], 1)
        self.assertGreaterEqual(outside["active"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
