"""signal 的选项与观察者占位类型。

对应 Go：``internal/signal/executor.go``。

选项属于**单个** signal，绝不共享全局池：``capacity`` 是等待中的回调上限
（不含正在运行的），``concurrency`` 是同时运行的回调上限。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from atri_x.signal.shutdown import CancelPending

__all__ = ["ConnectOptions", "Empty", "Options"]

T = TypeVar("T")


class Empty:
    """没有业务返回值的观察者结果。对应 Go ``signal.Empty``。"""

    __slots__ = ()


@dataclass
class Options(Generic[T]):
    """一个 signal 的选项。对应 Go ``signal.Options``。"""

    capacity: int = 0  # 等待中的回调上限（不含正在运行的）
    concurrency: int = 0  # 同时运行的回调上限；缺省 8（Go 侧为 10）
    timeout: float = 0.0  # 准入与执行预算（秒）；缺省 30
    required: bool = False  # 没有接收者时 emit 视为错误
    single: bool = False  # 最多一个接收者（请求/通知路由）
    key: Callable[[T], str] | None = None  # 相同非空 key 按准入顺序执行
    before: Any = None  # 所有者的执行期守卫，等位之后、接收者之前调用


@dataclass
class ConnectOptions:
    """一次 ``connect`` 的选项。对应 Go ``signal.ConnectOptions``。"""

    shutdown: int = CancelPending  # CancelPending 或 Drain
    once: bool = False  # 只认领一次（含被拒绝的准入）


# Go 的默认并发是 10（见 signal.New），Options 的文档注释里写作 8；
# 实现按 New 的实际取值对齐。
DEFAULT_CONCURRENCY = 10
DEFAULT_TIMEOUT = 30.0
