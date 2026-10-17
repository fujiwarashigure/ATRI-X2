"""``atri_x.platform.headless`` 的单元测试（对应 Go ``internal/platform/headless``）。"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.errors import AtriXError
from atri_x.platform.headless import Adapter, new, raise_context_error


class _Ctx:
    """最小上下文替身：支持 ``error()`` 与 ``add_callback``。"""

    def __init__(self) -> None:
        self._error: BaseException | None = None
        self._callbacks: list = []

    def error(self):
        return self._error

    def add_callback(self, fn):
        self._callbacks.append(fn)

        def remove():
            if fn in self._callbacks:
                self._callbacks.remove(fn)

        return remove

    def cancel(self, exc: BaseException) -> None:
        self._error = exc
        for fn in list(self._callbacks):
            fn()


class _PollOnlyCtx:
    """不支持回调注册的上下文替身（走轮询分支）。"""

    def __init__(self) -> None:
        self._error: BaseException | None = None
        self.cancelled = False

    def cancel(self, exc: BaseException) -> None:
        self._error = exc
        self.cancelled = True


class TestAdapterBasics(unittest.TestCase):
    def test_name(self):
        self.assertEqual(Adapter().name(), "service")

    def test_new_returns_adapter(self):
        self.assertIsInstance(new(), Adapter)


class TestSend(unittest.IsolatedAsyncioTestCase):
    async def test_send_chat_raises(self):
        with self.assertRaises(AtriXError) as ctx:
            await Adapter().send_chat(None, [])
        self.assertIn("cannot send chat output", str(ctx.exception))

    async def test_send_notice_raises(self):
        with self.assertRaises(AtriXError) as ctx:
            await Adapter().send_notice(None, None)
        self.assertIn("cannot send notice", str(ctx.exception))


class TestRun(unittest.IsolatedAsyncioTestCase):
    async def test_run_raises_immediately_when_already_cancelled(self):
        ctx = _Ctx()
        boom = AtriXError("done")
        ctx.cancel(boom)
        with self.assertRaises(AtriXError) as caught:
            await Adapter().run(ctx, None)
        self.assertIs(caught.exception, boom)

    async def test_run_blocks_until_cancel_with_callback(self):
        ctx = _Ctx()
        task = asyncio.ensure_future(Adapter().run(ctx, None))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        boom = AtriXError("stop")
        ctx.cancel(boom)
        with self.assertRaises(AtriXError) as caught:
            await asyncio.wait_for(task, 2.0)
        self.assertIs(caught.exception, boom)
        self.assertEqual(ctx._callbacks, [])

    async def test_run_polls_when_no_callback_support(self):
        ctx = _PollOnlyCtx()
        task = asyncio.ensure_future(Adapter().run(ctx, None))
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        ctx.cancel(AtriXError("poll stop"))
        with self.assertRaises(AtriXError):
            await asyncio.wait_for(task, 2.0)


class TestRaiseContextError(unittest.TestCase):
    def test_raises_context_error(self):
        ctx = _Ctx()
        boom = AtriXError("x")
        ctx.cancel(boom)
        with self.assertRaises(AtriXError) as caught:
            raise_context_error(ctx)
        self.assertIs(caught.exception, boom)

    def test_without_error_raises_cancelled(self):
        with self.assertRaises(asyncio.CancelledError):
            raise_context_error(_Ctx())


if __name__ == "__main__":
    unittest.main()
