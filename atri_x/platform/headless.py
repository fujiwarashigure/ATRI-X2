"""``internal/platform/headless`` 的 Python 移植。

service 模式下的“非交互主平台”：没有真实收发通道，:meth:`Adapter.run` 只是
阻塞到上下文结束，任何发送请求都直接失败（Go 侧用 ``fmt.Errorf`` 返回错误，
Python 侧按 PORTING.md 约定抛 :class:`atri_x.errors.AtriXError`）。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Sequence

from ..delivery import ctx_error
from ..errors import AtriXError
from .platform import Notice, Output, Receipt

__all__ = ["Adapter", "new", "raise_context_error"]


class Adapter:
    """service 模式的主平台适配器。对应 Go ``headless.Adapter``。"""

    def name(self) -> str:
        """平台名固定为 ``service``。对应 Go ``Adapter.Name``。"""
        return "service"

    async def run(self, ctx: Any, handler: Any) -> None:
        """阻塞到 ``ctx`` 结束，然后抛出结束原因。对应 Go ``Adapter.Run``。

        Go 是 ``<-ctx.Done(); return ctx.Err()``：取消即返回。这里等同一个
        “上下文结束”事件；若 ``ctx`` 不支持回调注册，退化为 50ms 轮询。
        """
        await _wait_context_done(ctx)
        raise_context_error(ctx)

    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """service 平台没有目标通道，直接失败。对应 Go ``Adapter.SendChat``。"""
        raise AtriXError(
            "service platform cannot send chat output without a target platform"
        )

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        """service 平台没有目标通道，直接失败。对应 Go ``Adapter.SendNotice``。"""
        raise AtriXError(
            "service platform cannot send notice without a target platform"
        )


def new() -> Adapter:
    """对应 Go ``headless.New``。"""
    return Adapter()


async def _wait_context_done(ctx: Any) -> None:
    """等到 ``ctx`` 结束。"""
    if ctx_error(ctx) is not None:
        return
    event = asyncio.Event()
    remover = None
    adder = getattr(ctx, "add_callback", None)
    if callable(adder):
        remover = adder(event.set)
    try:
        if remover is None:
            # 无回调注册能力：轮询，保持与 Go「阻塞到 Done」等价的可取消性。
            while ctx_error(ctx) is None:
                await asyncio.sleep(0.05)
            return
        await event.wait()
    finally:
        if remover is not None:
            with contextlib.suppress(Exception):
                remover()


def raise_context_error(ctx: Any) -> None:
    """抛出 ``ctx`` 的取消原因。对应 Go 的 ``return ctx.Err()``。"""
    err = ctx_error(ctx)
    if err is not None:
        raise err
    raise asyncio.CancelledError("headless adapter context cancelled")
