"""provider 级并发闸门：``provider_queue_max_size`` 从"死配置"接成有界队列。

回归背景（1.1.8）：``[ops].provider_queue_max_size`` / ``provider_wait_timeout_seconds``
在 1.1.7 只被解析、没有消费者——并发满时所有调用方都堆在 ``asyncio.Semaphore`` 后面
等到超时，队列上限完全无效。这里钉住两条路径：

* ``provider_queue_max_size <= 0``：保持旧语义（信号量等待，超时拒绝）；
* ``provider_queue_max_size > 0``：有界队列，**队满立即拒绝**，有人释放后队首拿到槽位。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from atri_x.agent.chat_stream import _acquire_provider_slot


class _FakeAgent:
    """只带 ``ops`` 与 ``audit`` 的最小宿主。"""

    def __init__(self, **ops: object) -> None:
        defaults = {
            "provider_max_concurrent": 1,
            "provider_queue_max_size": 0,
            "provider_wait_timeout_seconds": 5.0,
        }
        defaults.update(ops)
        self.ops = SimpleNamespace(**defaults)
        self.audits: list[tuple[str, tuple[object, ...]]] = []

    def audit(self, event: str, *args: object) -> None:
        self.audits.append((event, args))


class ProviderSlotTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_returns_noop(self) -> None:
        agent = _FakeAgent(provider_max_concurrent=0)
        release = await _acquire_provider_slot(agent, None, "deepseek")
        release()  # 幂等、可重复调用
        release()

    async def test_queue_full_is_rejected_immediately(self) -> None:
        agent = _FakeAgent(
            provider_max_concurrent=1,
            provider_queue_max_size=1,
            provider_wait_timeout_seconds=5.0,
        )
        first = await _acquire_provider_slot(agent, None, "deepseek")
        queued = asyncio.ensure_future(_acquire_provider_slot(agent, None, "deepseek"))
        await asyncio.sleep(0.05)
        self.assertFalse(queued.done())
        with self.assertRaises(RuntimeError):
            await _acquire_provider_slot(agent, None, "deepseek")
        self.assertTrue(any(event == "provider_concurrency_rejected" for event, _ in agent.audits))
        first()
        second = await asyncio.wait_for(queued, 2.0)
        second()

    async def test_queued_call_gets_slot_after_release(self) -> None:
        agent = _FakeAgent(
            provider_max_concurrent=1,
            provider_queue_max_size=4,
            provider_wait_timeout_seconds=5.0,
        )
        first = await _acquire_provider_slot(agent, None, "p")
        queued = asyncio.ensure_future(_acquire_provider_slot(agent, None, "p"))
        await asyncio.sleep(0.02)
        first()
        second = await asyncio.wait_for(queued, 2.0)
        second()
        self.assertEqual(agent.audits, [])

    async def test_semaphore_path_keeps_old_semantics(self) -> None:
        agent = _FakeAgent(
            provider_max_concurrent=1,
            provider_queue_max_size=0,
            provider_wait_timeout_seconds=0.05,
        )
        first = await _acquire_provider_slot(agent, None, "p")
        with self.assertRaises(RuntimeError):
            await _acquire_provider_slot(agent, None, "p")
        self.assertTrue(any(event == "provider_concurrency_rejected" for event, _ in agent.audits))
        first()

    async def test_per_provider_limits_are_independent(self) -> None:
        agent = _FakeAgent(
            provider_max_concurrent=1,
            provider_queue_max_size=1,
            provider_wait_timeout_seconds=1.0,
        )
        release_a = await _acquire_provider_slot(agent, None, "a")
        release_b = await _acquire_provider_slot(agent, None, "b")
        release_a()
        release_b()


if __name__ == "__main__":  # pragma: no cover - 手动运行
    unittest.main()
