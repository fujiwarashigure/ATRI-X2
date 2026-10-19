"""``atri_x.ops.executors`` 的单元测试：CPU / IO 线程池与两个 await 助手。

断言的是**契约**而不是实现：两个池是单例、容量符合定义、线程名带前缀、
``run_cpu`` / ``run_io`` 返回正确结果并原样传播异常、``shutdown_pools`` 幂等。
"""

from __future__ import annotations

import asyncio
import os
import threading
import unittest

from atri_x.ops.executors import (
    CPU_MAX_WORKERS,
    IO_MAX_WORKERS,
    cpu_pool,
    io_pool,
    named_pool,
    run_cpu,
    run_io,
    shutdown_pools,
)


def _expected_cpu_workers() -> int:
    """与 :mod:`atri_x.ops.executors` 的定义一致（兼容 3.11/3.12 无 process_cpu_count）。"""
    getter = getattr(os, "process_cpu_count", None)
    count = 0
    if getter is not None:
        try:
            count = int(getter() or 0)
        except OSError:  # pragma: no cover - 平台不支持
            count = 0
    return min(CPU_MAX_WORKERS, count or (os.cpu_count() or 1))


def _thread_name() -> str:
    return threading.current_thread().name


def _boom(message: str) -> None:
    raise ValueError(message)


class ExecutorsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 池是进程级单例：每个用例结束时收掉，避免用例之间互相影响。
        self.addCleanup(shutdown_pools)


class TestPoolShape(ExecutorsTestCase):
    def test_cpu_pool_is_a_singleton_with_the_defined_size(self) -> None:
        pool = cpu_pool()
        self.assertIs(pool, cpu_pool(), "cpu_pool 必须是模块级单例")
        self.assertEqual(pool._max_workers, _expected_cpu_workers())
        self.assertLessEqual(pool._max_workers, CPU_MAX_WORKERS)

    def test_io_pool_is_a_singleton_with_the_defined_size(self) -> None:
        pool = io_pool()
        self.assertIs(pool, io_pool(), "io_pool 必须是模块级单例")
        self.assertEqual(pool._max_workers, IO_MAX_WORKERS)
        self.assertEqual(IO_MAX_WORKERS, 32)

    def test_cpu_and_io_pools_are_distinct(self) -> None:
        self.assertIsNot(cpu_pool(), io_pool())

    def test_named_pool_is_a_singleton_keyed_by_prefix(self) -> None:
        first = named_pool("atrix-test-named", 3)
        self.assertIs(first, named_pool("atrix-test-named", 3))
        self.assertEqual(first._max_workers, 3)
        self.assertIsNot(first, named_pool("atrix-test-other", 3))
        # 容量下限为 1：传 0/负数不会造出空池（空池会永远不干活）。
        self.assertEqual(named_pool("atrix-test-zero", 0)._max_workers, 1)

    async def test_cpu_worker_threads_are_named(self) -> None:
        name = await run_cpu(_thread_name)
        self.assertTrue(name.startswith("atrix-cpu"), name)

    async def test_io_worker_threads_are_named(self) -> None:
        name = await run_io(_thread_name)
        self.assertTrue(name.startswith("atrix-io"), name)

    async def test_workers_actually_run_off_the_event_loop_thread(self) -> None:
        main = threading.current_thread().name
        self.assertNotEqual(await run_cpu(_thread_name), main)
        self.assertNotEqual(await run_io(_thread_name), main)


class TestRunHelpers(ExecutorsTestCase):
    async def test_run_cpu_returns_the_result_and_forwards_args_in_order(self) -> None:
        def combine(*args: object, **kwargs: object) -> tuple[object, ...]:
            self.assertEqual(kwargs, {})
            return args

        self.assertEqual(await run_cpu(combine, 1, "two", None), (1, "two", None))

    async def test_run_io_returns_the_result_and_forwards_args_in_order(self) -> None:
        self.assertEqual(await run_io(lambda *args: args, "a", 2), ("a", 2))

    async def test_run_cpu_propagates_exceptions(self) -> None:
        with self.assertRaises(ValueError) as captured:
            await run_cpu(_boom, "cpu boom")
        self.assertEqual(str(captured.exception), "cpu boom")

    async def test_run_io_propagates_exceptions(self) -> None:
        with self.assertRaises(ValueError) as captured:
            await run_io(_boom, "io boom")
        self.assertEqual(str(captured.exception), "io boom")

    async def test_run_helpers_are_awaitable_concurrently(self) -> None:
        # 两个池互不阻塞：一个卡住的 IO 任务不该挡住 CPU 任务。
        release = threading.Event()
        self.addCleanup(release.set)
        blocked = asyncio.ensure_future(run_io(release.wait, 5.0))
        try:
            self.assertEqual(await asyncio.wait_for(run_cpu(lambda: "cpu ok"), 5.0), "cpu ok")
        finally:
            release.set()
        self.assertTrue(await asyncio.wait_for(blocked, 5.0))


class TestShutdownPools(ExecutorsTestCase):
    def test_shutdown_is_idempotent(self) -> None:
        cpu_pool()
        io_pool()
        shutdown_pools()
        shutdown_pools()  # 第二次不能抛

    def test_shutdown_allows_recreation(self) -> None:
        first = cpu_pool()
        shutdown_pools()
        second = cpu_pool()
        self.assertIsNot(first, second, "关掉之后应该新建而不是复用已关闭的池")
        self.assertIs(second, cpu_pool())

    async def test_pools_work_again_after_shutdown(self) -> None:
        self.assertEqual(await run_cpu(lambda: 1), 1)
        shutdown_pools()
        self.assertEqual(await run_io(lambda: 2), 2)

    def test_shutdown_without_any_pool_is_safe(self) -> None:
        shutdown_pools()
        shutdown_pools()


if __name__ == "__main__":
    unittest.main()
