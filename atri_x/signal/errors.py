"""signal 包的哨兵异常与 ``errors.Join`` 等价物。

对应 Go：``internal/signal/errors.go``（以及 ``queue.go`` / ``signal.go`` 里
的 ``ErrQueueFull`` / ``ErrClosed`` / ``ErrNoReceiver`` / ``ErrRecursive``）。

Go 的 ``errors.Join`` → :class:`JoinedError`；``filterErrors`` 逐行移植为
:func:`filter_errors`（只移除匹配的叶子，保留无关的 joined 错误）。
"""

from __future__ import annotations

from typing import Callable

from atri_x.errors import (
    AtriXError,
    ErrClosed,
    NoReceiverError,
    QueueFullError,
    RecursiveError,
)

__all__ = [
    "ErrClosed",
    "ErrNoReceiver",
    "ErrQueueFull",
    "ErrRecursive",
    "JoinedError",
    "SignalError",
    "filter_errors",
    "is_error",
    "join_errors",
]


class SignalError(AtriXError):
    """signal 包的通用配置/契约错误（对应 Go 里的 ``errors.New(...)`` 文本错误）。"""


class JoinedError(AtriXError):
    """对应 Go ``errors.Join``：承载多个并列错误。"""

    def __init__(self, errors: list[BaseException] | None = None) -> None:
        self.errors = list(errors or [])
        super().__init__("; ".join(str(err) for err in self.errors) or "no errors")


# signal 自己的队满哨兵（Go ``signal.ErrQueueFull``）；它与
# ``atri_x.errors.ErrQueueFull``（request 包的哨兵）是不同类型，但都继承
# ``QueueFullError``，所以按类型判定时两者都命中。
ErrQueueFull = QueueFullError("signal: queue full")
ErrNoReceiver = NoReceiverError("signal: no receiver")
ErrRecursive = RecursiveError("signal: recursive dependency")


def join_errors(errors: list[BaseException] | None) -> BaseException | None:
    """对应 Go ``errors.Join``：无错误返回 ``None``，单个错误原样返回。"""
    clean = [err for err in (errors or []) if err is not None]
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    return JoinedError(clean)


def is_error(err: BaseException | None, expected: BaseException | type) -> bool:
    """对应 Go 的 ``errors.Is``：递归检查聚合错误里的每个叶子。"""
    if err is None:
        return False
    if isinstance(expected, type):
        if isinstance(err, expected):
            return True
    elif err is expected:
        return True
    if _unwraps_to_many(err):
        return any(is_error(child, expected) for child in _children(err))
    cause = err.__cause__
    if cause is not None and is_error(cause, expected):
        return True
    return False


def _unwraps_to_many(err: BaseException) -> bool:
    """对应 Go 的 ``interface{ Unwrap() []error }``。"""
    return isinstance(err, (JoinedError, BaseExceptionGroup))


def _children(err: BaseException) -> list[BaseException]:
    if isinstance(err, JoinedError):
        return list(err.errors)
    if isinstance(err, BaseExceptionGroup):
        return list(err.exceptions)
    return []


def filter_errors(
    err: BaseException | None, expected: Callable[[BaseException], bool]
) -> tuple[BaseException | None, bool]:
    """只移除匹配的叶子，保留无关的 joined 错误。

    包装器未变化时原样保留；部分过滤后的树保留真正剩余的原因，而不是继续用
    一个仍在报告"已取消"的包装器。
    """
    if err is None:
        return None, False
    if _unwraps_to_many(err):
        kept: list[BaseException] = []
        changed = False
        for child in _children(err):
            remaining, removed = filter_errors(child, expected)
            changed = changed or removed
            if remaining is not None:
                kept.append(remaining)
        if changed:
            return join_errors(kept), True
        return err, False
    cause = err.__cause__
    if cause is not None:
        remaining, changed = filter_errors(cause, expected)
        if changed:
            return remaining, True
        return err, False
    if expected(err):
        return None, True
    return err, False
