"""``atri_x.signal`` 的单元测试。

对应 Go：``internal/signal/*_test.go``（queue / backpressure / shutdown /
isolation / forward / signal）。
"""

from __future__ import annotations

import asyncio
import io
import sys
import unittest

from atri_x.errors import ClosedError, QueueFullError
from atri_x.safecontext import CancelledError, DeadlineExceededError, with_cancel, with_timeout
from atri_x.signal import (
    ConnectOptions,
    Drain,
    Emission,
    ErrClosed,
    ErrNoReceiver,
    ErrQueueFull,
    ErrRecursive,
    JoinedError,
    Options,
    Queue,
    QueueOptions,
    Resolved,
    Result,
    ShutdownPolicy,
    Signal,
    SignalError,
    Task,
    diagnostics_written,
    dropped_diagnostics,
    filter_errors,
    is_error,
    map_results,
    report_failure,
    reset_diagnostics,
    flush_diagnostics,
)


def _ctx(timeout: float = 1.0):
    return with_timeout(None, timeout)


async def _close_signal(signal: Signal) -> None:
    await signal.close(_ctx())


class TestSignalResults(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._signals: list[Signal] = []

    async def asyncTearDown(self) -> None:
        for signal in self._signals:
            try:
                await _close_signal(signal)
            except BaseException:  # pragma: no cover - 清理失败不掩盖断言
                pass

    def make(self, name: str, options: Options | None = None) -> Signal:
        signal = Signal(name, options)
        self._signals.append(signal)
        return signal

    async def test_results_keep_registration_order(self) -> None:
        signal = self.make("results", Options())
        signal.connect(lambda ctx, n: n + 1)
        signal.connect(lambda ctx, n: n + 2)
        results = await signal.emit(None, 4)
        self.assertEqual([item.value for item in results], [5, 6])
        self.assertTrue(all(item.started for item in results))
        self.assertTrue(all(item.err is None for item in results))

    async def test_callback_failure_is_raised_with_started_flag(self) -> None:
        signal = self.make("failures", Options())
        failure = RuntimeError("failed")
        signal.connect(lambda ctx, n: n + 1)
        signal.connect(lambda ctx, n: (_ for _ in ()).throw(failure))
        emission = await signal.emit_async(None, 4)
        with self.assertRaises(RuntimeError):
            await emission.wait(None)
        self.assertEqual(emission.results[0].value, 5)
        self.assertIs(emission.results[1].err, failure)
        self.assertTrue(emission.results[0].started)
        self.assertTrue(emission.results[1].started)

    async def test_once_claims_a_single_emission(self) -> None:
        signal = self.make("once", Options())
        calls: list[int] = []

        async def handler(ctx, n):
            calls.append(n)

        signal.connect(handler, ConnectOptions(once=True))
        await asyncio.gather(*[signal.emit(None, index) for index in range(16)])
        self.assertEqual(len(calls), 1)

    async def test_single_rejects_second_receiver(self) -> None:
        signal = self.make("single", Options(single=True))
        signal.connect(lambda ctx, n: n)
        with self.assertRaises(SignalError):
            signal.connect(lambda ctx, n: n)

    async def test_invalid_shutdown_policy_rejected(self) -> None:
        signal = self.make("badpolicy", Options())
        with self.assertRaises(ValueError):
            signal.connect(lambda ctx, n: n, ConnectOptions(shutdown=7))
        self.assertEqual(ShutdownPolicy.CANCEL_PENDING, 0)

    async def test_no_receiver_and_recursive(self) -> None:
        signal = self.make("recursive", Options(required=True, single=True))
        with self.assertRaises(ValueError):
            signal.connect(None)  # type: ignore[arg-type]
        with self.assertRaises(Exception) as missing:
            await signal.emit(None, 0)
        self.assertTrue(is_error(missing.exception, ErrNoReceiver))

        async def handler(ctx, n):
            await signal.emit(ctx, n)
            return 0

        signal.connect(handler)
        with self.assertRaises(Exception) as recursive:
            await signal.emit(None, 0)
        self.assertTrue(is_error(recursive.exception, ErrRecursive))

    async def test_empty_signal_rejects_canceled_and_closed(self) -> None:
        signal = self.make("empty", Options())
        ctx = with_cancel(None)
        ctx.cancel("test")
        with self.assertRaises(CancelledError):
            await signal.emit(ctx, 0)
        signal.begin_close()
        with self.assertRaises(ClosedError):
            await signal.emit(None, 0)

    async def test_emit_without_receiver_returns_empty(self) -> None:
        signal = self.make("noreceiver", Options())
        self.assertEqual(await signal.emit(None, 0), [])


class TestSignalTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_retains_slot_and_other_signals_progress(self) -> None:
        slow = Signal("slow", Options(concurrency=1, capacity=1, timeout=0.03))
        fast = Signal("fast", Options())
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def handler(ctx, n):
            calls.append(n)
            started.set()
            await release.wait()
            return 9

        slow.connect(handler)
        fast.connect(lambda ctx, n: n)
        emission = await slow.emit_async(None, 0)
        await started.wait()
        with self.assertRaises(DeadlineExceededError):
            await emission.wait(None)
        self.assertTrue(emission.results[0].started)

        queued = await slow.emit_async(None, 1)
        overflow = await slow.emit_async(None, 2)
        self.assertEqual(len(overflow.admission_errors), 1)
        self.assertIs(overflow.admission_errors[0], ErrQueueFull)

        results = await fast.emit(None, 7)
        self.assertEqual(results[0].value, 7)

        with self.assertRaises(DeadlineExceededError):
            await queued.wait(None)
        self.assertFalse(queued.results[0].started)
        # 超时的回调仍然占着执行槽位，因此不会再被调用一次。
        self.assertEqual(calls, [0])
        self.assertEqual(emission.results[0].value, None)

        release.set()
        await slow.close(_ctx())
        await fast.close(_ctx())


class TestSignalKeyOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_key_ordering_does_not_occupy_workers(self) -> None:
        options = Options(
            concurrency=2, key=lambda n: "same" if n < 3 else "other"
        )
        signal = Signal("keys", options)
        started = asyncio.Event()
        release = asyncio.Event()
        order: list[int] = []

        async def handler(ctx, n):
            if n == 0:
                started.set()
                await release.wait()
            order.append(n)
            return n

        signal.connect(handler)
        first = await signal.emit_async(None, 0)
        await started.wait()
        second = await signal.emit_async(None, 1)
        third = await signal.emit_async(None, 2)
        other = await signal.emit(None, 3)
        self.assertEqual(other[0].value, 3)
        release.set()
        for emission in (first, second, third):
            await emission.wait(None)
        self.assertEqual(order, [3, 0, 1, 2])
        await signal.close(_ctx())


class TestSignalForwarding(unittest.IsolatedAsyncioTestCase):
    async def test_forward_resolved_preserves_single_result(self) -> None:
        failure = RuntimeError("partial delivery")
        for expected in (Result(), Result(err=ErrClosed), Result(value=42, err=failure, started=True)):
            signal = Signal("forwarded", Options(single=True))
            signal.connect_forward(lambda ctx, n, expected=expected: Resolved(expected))
            emission = await signal.emit_async(None, 0)
            if expected.err is None:
                results = await emission.wait(None)
            else:
                with self.assertRaises(BaseException):
                    await emission.wait(None)
                results = emission.results
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].value, expected.value)
            self.assertIs(results[0].err, expected.err)
            self.assertEqual(results[0].started, expected.started)
            await signal.close(_ctx())

    async def test_forward_nil_is_contract_error(self) -> None:
        signal = Signal("nilforward", Options(single=True))
        signal.connect_forward(lambda ctx, n: None)
        emission = await signal.emit_async(None, 0)
        with self.assertRaises(SignalError):
            await emission.wait(None)
        self.assertFalse(emission.results[0].started)
        await signal.close(_ctx())

    async def test_forward_rejected_emission_keeps_admission_error_once(self) -> None:
        down = Signal("down", Options(single=True, concurrency=1, capacity=1))
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(ctx, n):
            started.set()
            await release.wait()
            return 0

        down.connect(blocked)
        await down.emit_async(None, 0)
        await started.wait()
        await down.emit_async(None, 0)

        up = Signal("up", Options(single=True))

        async def forwarder(ctx, n):
            return await down.emit_async(ctx, n)

        up.connect_forward(forwarder)
        emission = await up.emit_async(None, 1)
        with self.assertRaises(QueueFullError):
            await emission.wait(None)
        self.assertEqual(len(emission.results), 1)
        self.assertIs(emission.results[0].err, ErrQueueFull)
        self.assertFalse(emission.results[0].started)
        release.set()
        await up.close(_ctx())
        await down.close(_ctx())

    async def test_forwarding_releases_upstream_worker_and_preserves_deadline(self) -> None:
        up = Signal("up", Options(concurrency=1, required=True, single=True, timeout=0.2))
        slow = Signal("slow", Options(single=True))
        fast = Signal("fast", Options(single=True))
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(ctx, n):
            started.set()
            await release.wait()
            return 1

        slow.connect(slow_handler)
        fast.connect(lambda ctx, n: n)

        async def forwarder(ctx, n):
            return await (slow if n == 0 else fast).emit_async(ctx, n)

        up.connect_forward(forwarder)
        first = await up.emit_async(None, 0)
        await started.wait()
        # 上游 worker 已经被释放：前一个 emission 还没完成，新的 emit 仍能跑。
        results = await up.emit(None, 5)
        self.assertEqual(results[0].value, 5)
        with self.assertRaises(DeadlineExceededError):
            await first.wait(None)
        self.assertTrue(first.results[0].started)
        release.set()
        await up.close(_ctx())
        await slow.close(_ctx())
        await fast.close(_ctx())


class TestSignalIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_signal_is_bounded_and_sheds_load(self) -> None:
        signal = Signal("blocked", Options(concurrency=8, capacity=64))
        gate = asyncio.Event()
        running: list[int] = []

        async def blocked(ctx, n):
            running.append(n)
            await gate.wait()

        signal.connect(blocked)
        for _ in range(8):
            await signal.emit_async(None, 0)
        for _ in range(200):
            if len(running) == 8:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(len(running), 8)

        queued = with_cancel(None)
        for _ in range(64):
            await signal.emit_async(queued, 0)
        self.assertEqual(signal.queue.pending, 64)
        self.assertEqual(signal.queue.workers, 8)
        self.assertEqual(len(signal.queue.active), 8)

        for _ in range(20):
            emission = await signal.emit_async(None, 0)
            self.assertEqual(len(emission.admission_errors), 1)
            self.assertIs(emission.admission_errors[0], ErrQueueFull)

        # 取消排队中的工作即可释放容量，即使所有 worker 都被阻塞。
        queued.cancel("test")
        for _ in range(500):
            if signal.queue.pending == 0:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(signal.queue.pending, 0)

        gate.set()
        await signal.close(_ctx())

    async def test_disconnect_retains_snapshot_and_close_waits_for_exit(self) -> None:
        signal = Signal("disconnect", Options(concurrency=1))
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(ctx, n):
            if n == 0:
                started.set()
                await release.wait()
            return n

        connection = signal.connect(handler)
        first = await signal.emit_async(None, 0)
        await started.wait()
        second = await signal.emit_async(None, 1)
        connection.disconnect()
        self.assertEqual(await signal.emit(None, 2), [])
        release.set()
        for emission in (first, second):
            results = await emission.wait(None)
            self.assertTrue(results[0].started)
        await connection.close(_ctx())
        self.assertTrue(connection.done().is_set())
        await signal.close(_ctx())

    async def test_connection_close_cancels_but_waits_for_running_callback(self) -> None:
        signal = Signal("connclose", Options())
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(ctx, n):
            started.set()
            await release.wait()
            return 1

        connection = signal.connect(handler)
        emission = await signal.emit_async(None, 0)
        await started.wait()
        with self.assertRaises(DeadlineExceededError):
            await connection.close(with_timeout(None, 0.01))
        with self.assertRaises(CancelledError):
            await emission.wait(None)
        self.assertTrue(emission.results[0].started)
        self.assertFalse(connection.done().is_set())
        release.set()
        await connection.close(_ctx())
        await signal.close(_ctx())

    async def test_closing_completes_pending_results(self) -> None:
        signal = Signal("closing", Options(concurrency=1))
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(ctx, n):
            started.set()
            await ctx.wait()
            await release.wait()
            raise ctx.error()

        signal.connect(handler)
        first = await signal.emit_async(None, 0)
        await started.wait()
        second = await signal.emit_async(None, 1)
        signal.begin_close()
        with self.assertRaises(BaseException):
            await second.wait(None)
        with self.assertRaises(CancelledError):
            await first.wait(None)
        self.assertFalse(signal.done().is_set())
        release.set()
        await signal.close(_ctx())

    async def test_partial_admission_keeps_every_subscriber_result(self) -> None:
        signal = Signal("partial", Options(concurrency=1, capacity=1))
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocker(ctx):
            started.set()
            await release.wait()

        await signal.queue.submit(None, Task(run=blocker, shutdown=Drain))
        await started.wait()
        signal.connect(lambda ctx, n: 1)
        signal.connect(lambda ctx, n: 2)
        emission = await signal.emit_async(None, 0)
        self.assertEqual(len(emission.admission_errors), 1)
        self.assertIs(emission.admission_errors[0], ErrQueueFull)
        release.set()
        with self.assertRaises(QueueFullError):
            await emission.wait(None)
        results = emission.results
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].value, 1)
        self.assertTrue(results[0].started)
        self.assertIs(results[1].err, ErrQueueFull)
        self.assertFalse(results[1].started)
        await signal.close(_ctx())


class TestQueue(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_fifo_and_error_continuation(self) -> None:
        queue = Queue(QueueOptions(name="fifo", capacity=2))
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def first(ctx):
            started.set()
            await release.wait()
            calls.append(0)
            raise RuntimeError("expected failure")

        await queue.submit(None, Task(run=first, shutdown=Drain))
        await started.wait()

        async def later(ctx, index):
            calls.append(index)

        for index in (1, 2):
            await queue.submit(None, Task(run=lambda ctx, i=index: later(ctx, i), shutdown=Drain))
        with self.assertRaises(QueueFullError):
            await queue.submit(None, Task(run=lambda ctx: None, shutdown=Drain))
        release.set()
        await queue.close(_ctx())
        self.assertEqual(calls, [0, 1, 2])
        with self.assertRaises(ClosedError):
            await queue.submit(None, Task(run=lambda ctx: None, shutdown=Drain))
        self.assertTrue(queue.done().is_set())

    async def test_queue_defaults_and_negative_options(self) -> None:
        with self.assertRaises(ValueError):
            Queue(QueueOptions(capacity=-1))
        with self.assertRaises(ValueError):
            Queue(QueueOptions(concurrency=-1))
        with self.assertRaises(ValueError):
            Queue(QueueOptions(admission_timeout=-1))
        default = Queue(QueueOptions())
        self.assertEqual(default.capacity, 256)
        self.assertEqual(default.concurrency, 1)
        await default.close(_ctx())

    async def test_backpressure_fifo(self) -> None:
        queue = Queue(QueueOptions(capacity=1, wait_for_capacity=True))
        started = asyncio.Event()
        release = asyncio.Event()
        got: list[str] = []

        async def first(ctx):
            started.set()
            await release.wait()
            got.append("A")

        await queue.submit(None, Task(run=first, shutdown=Drain))
        await started.wait()
        await queue.submit(None, Task(run=lambda ctx: got.append("B"), shutdown=Drain))
        waiter = asyncio.ensure_future(
            queue.submit(None, Task(run=lambda ctx: got.append("C"), shutdown=Drain))
        )
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done())
        release.set()
        await asyncio.wait_for(waiter, 1)
        await queue.close(_ctx())
        self.assertEqual(got, ["A", "B", "C"])

    async def test_backpressure_wake_on_cancel_and_close(self) -> None:
        for closing in (False, True):
            queue = Queue(QueueOptions(capacity=1, wait_for_capacity=True))
            started = asyncio.Event()
            release = asyncio.Event()

            async def first(ctx):
                started.set()
                await release.wait()

            await queue.submit(None, Task(run=first, shutdown=Drain))
            await started.wait()
            await queue.submit(None, Task(run=lambda ctx: None, shutdown=Drain))
            ctx = with_cancel(None)
            waiter = asyncio.ensure_future(
                queue.submit(ctx, Task(run=lambda ctx: self.fail("unaccepted"), shutdown=Drain))
            )
            await asyncio.sleep(0.02)
            self.assertFalse(waiter.done())
            if closing:
                queue.begin_close()
                expected = ClosedError
            else:
                ctx.cancel("test")
                expected = CancelledError
            with self.assertRaises(expected):
                await asyncio.wait_for(waiter, 1)
            self.assertFalse(queue.done().is_set())
            release.set()
            await queue.close(_ctx())

    async def test_mixed_shutdown_policies_keep_drain_order(self) -> None:
        queue = Queue(QueueOptions(capacity=4))
        started = asyncio.Event()
        stopping = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def blocking(ctx):
            started.set()
            while not ctx.is_cancelled():
                await asyncio.sleep(0.001)
            stopping.set()
            await release.wait()
            raise ctx.error()

        await queue.submit(None, Task(run=blocking))
        await started.wait()
        for index, policy in enumerate((Drain, 0, Drain, 0)):
            await queue.submit(
                None,
                Task(run=lambda ctx, i=index: calls.append(i), shutdown=policy),
            )
        closing = asyncio.ensure_future(queue.close(_ctx()))
        await stopping.wait()
        release.set()
        await closing
        self.assertEqual(calls, [0, 2])

    async def test_drain_failure_is_diagnosed(self) -> None:
        reset_diagnostics()
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            for policy, expected in ((0, 0), (Drain, 1)):
                queue = Queue(QueueOptions(name="draincheck", capacity=1))
                started = asyncio.Event()
                release = asyncio.Event()

                async def blocking(ctx):
                    started.set()
                    await release.wait()

                await queue.submit(None, Task(run=blocking, shutdown=policy))
                await started.wait()
                ctx = with_cancel(None)
                ctx.cancel("test")
                with self.assertRaises(CancelledError):
                    await queue.close(ctx)
                release.set()
                await queue.done().wait()
                await flush_diagnostics(_ctx())
                text = "\n".join(diagnostics_written())
                self.assertEqual(text.count("signal drain incomplete"), expected)
                reset_diagnostics()
        finally:
            sys.stderr = stderr


class TestSignalDiagnosticsAndHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_diagnostics_do_not_block_workers(self) -> None:
        await flush_diagnostics(_ctx())
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            for _ in range(1000):
                report_failure("x" * 512)
            # 队列有界：溢出的诊断只计数，不拖住 signal worker。
            self.assertGreater(dropped_diagnostics(), 0)
            signal = Signal("diag", Options())
            failure = RuntimeError("handler failed")
            signal.connect(lambda ctx, n: (_ for _ in ()).throw(failure))
            with self.assertRaises(RuntimeError):
                await signal.emit(_ctx(), 0)
            await signal.close(_ctx())
            await flush_diagnostics(_ctx())
            written = "\n".join(diagnostics_written())
            self.assertIn("signal callback failed", written)
            self.assertIn("signal diagnostics dropped", written)
        finally:
            sys.stderr = stderr

    async def test_map_results_and_join_errors(self) -> None:
        source: Emission = Emission(lambda: None, 1)
        mapped = map_results(source, lambda results: Result(value=results[0].value * 2))
        source.finish(0, Result(value=21))
        results = await mapped.wait(None)
        self.assertEqual(results[0].value, 42)

        failure = RuntimeError("boom")
        joined = JoinedError([failure, ErrClosed])
        self.assertTrue(is_error(joined, failure))
        self.assertTrue(is_error(joined, ErrClosed))
        remaining, removed = filter_errors(joined, lambda err: err is ErrClosed)
        self.assertTrue(removed)
        self.assertIs(remaining, failure)

    async def test_observe_ignores_business_value(self) -> None:
        signal = Signal("observe", Options())
        seen: list[int] = []

        async def observer(ctx, n):
            seen.append(n)

        signal.observe(observer)
        results = await signal.emit(None, 3)
        self.assertEqual(seen, [3])
        self.assertIsNone(results[0].value)
        await signal.close(_ctx())

    async def test_before_guard_blocks_receiver(self) -> None:
        calls: list[int] = []

        async def guard(ctx, event):
            raise RuntimeError("guard rejected")

        signal = Signal("guard", Options(before=guard))
        signal.connect(lambda ctx, n: calls.append(n))
        with self.assertRaises(RuntimeError):
            await signal.emit(None, 1)
        self.assertEqual(calls, [])
        await signal.close(_ctx())
