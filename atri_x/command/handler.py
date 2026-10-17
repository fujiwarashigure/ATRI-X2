"""函数式命令处理器。

对应 Go：``internal/command/handler.go``。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from atri_x.command.types import Info, Request, Result

# FuncHandler 的处理函数签名：``async (ctx, req) -> Result | None``。
Func = Callable[[Any, Request], Awaitable[Result | None]]


class FuncHandler:
    """把一个 ``Info`` 加一个处理函数包成命令。对应 Go ``FuncHandler``。"""

    def __init__(self, info: Info, fn: Func | None = None) -> None:
        self._info = info
        self._fn = fn

    def info(self) -> Info:
        """对应 Go ``FuncHandler.Info``。"""
        return self._info

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        """对应 Go ``FuncHandler.Handle``。"""
        if self._fn is None:
            return None
        return await self._fn(ctx, req)


def new_func(info: Info, fn: Func | None = None) -> FuncHandler:
    """对应 Go ``NewFunc``。"""
    return FuncHandler(info, fn)


__all__ = [name for name in dir() if not name.startswith("_")]
