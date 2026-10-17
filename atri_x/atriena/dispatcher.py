"""Atriena 分发总线。

对应 Go：``internal/atriena/dispatcher.go``。

进程内只有一个分派点：各个入口（HTTP 令牌、Hook、进程内调用）都拿到同一个
:class:`Bus`，由它转给真正的实现（``atri_x.atrinis.Service``）。这样"谁是
Atriena 的处理器"只在启动装配期决定一次，入口代码不需要持有具体实现。

与 Go 的差异：

* Go 的 ``Bus`` 用 ``sync.RWMutex`` 保护 dispatcher 字段；``SetDispatcher`` 由启动
  期的同步代码调用，因此本仓用 :class:`threading.Lock`（``asyncio.Lock`` 会把
  ``set_dispatcher`` 变成协程，进而改变调用点）。临界区只做一次属性读写，
  不会阻塞事件循环。
* Go 未配置 dispatcher 时返回 ``(Response{Accepted:false, Status:StatusFailed,
  Error:"atriena dispatcher is not configured"}, error)``；Python 的 ``(T, error)``
  一律改为抛异常，故这里抛 :class:`atri_x.errors.InvalidError`，调用方需要
  Response 时自行构造同样的值。
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable

from atri_x.atriena.origin import Origin
from atri_x.atriena.types import Request, Response
from atri_x.errors import InvalidError

__all__ = ["Bus", "Dispatcher", "DispatcherNotConfiguredMessage", "new_bus"]

#: 未配置 dispatcher 时的错误文案（与 Go 逐字一致）。
DispatcherNotConfiguredMessage = "atriena dispatcher is not configured"


@runtime_checkable
class Dispatcher(Protocol):
    """Atriena 分派实现。对应 Go ``Dispatcher``（由 ``atri_x.atrinis.Service`` 实现）。"""

    async def dispatch_atriena(
        self, ctx: Any, origin: Origin, req: Request
    ) -> Response:
        ...


class Bus:
    """可热插拔的分派总线。对应 Go ``Bus``。"""

    __slots__ = ("_lock", "_dispatcher")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dispatcher: Any = None

    def set_dispatcher(self, dispatcher: Any) -> None:
        """安装分派实现（``None`` 表示暂时没有）。对应 Go ``SetDispatcher``。"""
        with self._lock:
            self._dispatcher = dispatcher

    def dispatcher(self) -> Any:
        """当前分派实现（可能为 ``None``）。"""
        with self._lock:
            return self._dispatcher

    async def dispatch_atriena(self, ctx: Any, origin: Origin, req: Request) -> Response:
        """转给已安装的分派实现。对应 Go ``Bus.DispatchAtriena``。"""
        dispatcher = self.dispatcher()
        if dispatcher is None:
            raise InvalidError(DispatcherNotConfiguredMessage)
        return await dispatcher.dispatch_atriena(ctx, origin, req)


def new_bus() -> Bus:
    """构造一个空总线。对应 Go ``NewBus``。"""
    return Bus()
