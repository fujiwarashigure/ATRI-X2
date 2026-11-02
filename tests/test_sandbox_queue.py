"""准入队列单元测试：容量、公平轮转、后台防饿死、超时与取消。"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.sandbox.errors import SandboxQueueTimeoutError, SandboxQuotaError
from atri_x.sandbox.queue import AdmissionController


class TestAdmissionQueue(unittest.TestCase):
    def test_acquire_release_and_stats(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(capacity=1, queue_max_size=4, per_tenant_limit=2)
            first = await queue.acquire("t1")
            self.assertEqual(queue.snapshot().running, 1)
            with self.assertRaises(SandboxQueueTimeoutError):
                await queue.acquire("t1", timeout=0.2)
            first.release()
            stats = queue.snapshot()
            self.assertEqual(stats.running, 0)
            self.assertEqual(stats.timed_out_total, 1)

        asyncio.run(scenario())

    def test_queue_bounds_reject_instead_of_hanging(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(
                capacity=1, queue_max_size=1, per_tenant_limit=2, wait_timeout=5
            )
            lease = await queue.acquire("t1")
            waiter = asyncio.ensure_future(queue.acquire("t2"))
            await asyncio.sleep(0.05)
            with self.assertRaises(SandboxQuotaError):
                await queue.acquire("t3", timeout=0.2)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            lease.release()

        asyncio.run(scenario())

    def test_per_tenant_limit(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(capacity=2, per_tenant_limit=1)
            lease = await queue.acquire("t1")
            with self.assertRaises(SandboxQuotaError):
                await queue.acquire("t1", timeout=0.2)
            lease.release()

        asyncio.run(scenario())

    def test_fair_round_robin_between_tenants(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(capacity=1, per_tenant_limit=2)
            held = await queue.acquire("t1")
            waiters = [
                asyncio.ensure_future(queue.acquire("t2")),
                asyncio.ensure_future(queue.acquire("t3")),
                asyncio.ensure_future(queue.acquire("t2")),
            ]
            await asyncio.sleep(0.05)
            held.release()
            await asyncio.sleep(0.05)
            granted_first = [w for w in waiters if w.done()][0]
            self.assertEqual(granted_first.result().tenant, "t2")
            granted_first.result().release()
            await asyncio.sleep(0.05)
            granted_second = [w for w in waiters if w.done() and w is not granted_first][0]
            self.assertEqual(granted_second.result().tenant, "t3")
            granted_second.result().release()
            await asyncio.sleep(0.05)
            remaining = [w for w in waiters if not w.done()]
            for waiter in remaining:
                waiter.result().release()
            await asyncio.gather(*waiters, return_exceptions=True)

        asyncio.run(scenario())

    def test_background_starvation_promotion(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(
                capacity=1, per_tenant_limit=4, starvation_rounds=1
            )
            held = await queue.acquire("interactive")
            background = asyncio.ensure_future(
                queue.acquire("job", kind="background")
            )
            interactive = asyncio.ensure_future(queue.acquire("chat"))
            await asyncio.sleep(0.05)
            held.release()
            await asyncio.sleep(0.05)
            # 后台任务被跳过一轮后，下一轮应被提升（防饿死）。
            first = [w for w in (background, interactive) if w.done()][0]
            first.result().release()
            await asyncio.sleep(0.05)
            done = [w for w in (background, interactive) if w.done()]
            self.assertTrue(done)
            for waiter in done:
                waiter.result().release()
            await asyncio.gather(background, interactive, return_exceptions=True)

        asyncio.run(scenario())

    def test_cancel_all_on_panic(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(capacity=1, per_tenant_limit=4)
            held = await queue.acquire("t1")
            waiter = asyncio.ensure_future(queue.acquire("t2"))
            await asyncio.sleep(0.05)
            cancelled = queue.cancel_all()
            self.assertEqual(cancelled, 1)
            with self.assertRaises(Exception):
                await waiter
            held.release()

        asyncio.run(scenario())

    def test_lease_release_is_idempotent(self) -> None:
        async def scenario() -> None:
            queue = AdmissionController(capacity=1)
            lease = await queue.acquire("t1")
            lease.release()
            lease.release()
            self.assertEqual(queue.snapshot().running, 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
