"""关闭策略与任务描述。

对应 Go：``internal/signal/shutdown.go``。

Go 的 ``Task.cancel`` 是 signal 自己的 ``context.CancelFunc``：队列在关闭时
调用它，``Finish`` 负责正常释放。Python 侧它就是一个
:class:`atri_x.safecontext.CancelToken`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atri_x.safecontext import CancelToken

__all__ = [
    "CancelPending",
    "Drain",
    "SHUTDOWN_POLICIES",
    "ShutdownPolicy",
    "Task",
]


class ShutdownPolicy:
    """队列关闭时对排队/运行中任务的处置。对应 Go ``signal.ShutdownPolicy``。"""

    CANCEL_PENDING = 0
    DRAIN = 1


CancelPending = ShutdownPolicy.CANCEL_PENDING
Drain = ShutdownPolicy.DRAIN

_POLICIES = (CancelPending, Drain)
#: 合法的关闭策略集合（供队列校验）。
SHUTDOWN_POLICIES = _POLICIES


@dataclass
class Task:
    """一个有界工作单元。对应 Go ``signal.Task``。

    - ``run``：``Callable[[CancelToken], Any]``，同步或异步皆可。
    - ``key``：非空且相等的 key 不会并发执行（按准入顺序）。
    - ``finish``：只会被调用一次（含执行前被取消）；不得阻塞。
    - ``cancel``：signal 持有的 token，队列在关闭时取消它；正常释放由
      ``finish`` 负责。
    """

    run: Callable[[CancelToken], Any] | None = None
    shutdown: int = CancelPending
    key: str = ""
    finish: Callable[[BaseException | None], None] | None = None
    cancel: CancelToken | None = None
