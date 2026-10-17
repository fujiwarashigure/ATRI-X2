"""平台重连用的指数退避。

对应 Go：``internal/platform/backoff.go``。

Go 的 ``time.Duration`` → Python 的秒（``float``）。
"""

from __future__ import annotations

DEFAULT_BACKOFF_BASE = 3.0
DEFAULT_BACKOFF_MAX = 10.0


class Backoff:
    """指数退避延迟。对应 Go ``Backoff``。

    连接失败时 :meth:`delay` 逐次翻倍，封顶 ``max``；连接成功后调用
    :meth:`reset` 回到 ``base``。日志降级：:meth:`should_warn` 仅在状态从正常
    转为失败时返回 True，连续失败期间返回 False。
    """

    def __init__(self, base: float = 0.0, max: float = 0.0) -> None:
        if base <= 0:
            base = DEFAULT_BACKOFF_BASE
        if max <= 0 or max < base:
            max = DEFAULT_BACKOFF_MAX
        self._base = base
        self._max = max
        self._cur = base
        self._failed = False

    def delay(self) -> float:
        """返回当前延迟并把下一次延迟翻倍（封顶 ``max``）。对应 Go ``Delay``。"""
        current = self._cur
        nxt = self._cur * 2
        if nxt > self._max or nxt < self._cur:
            nxt = self._max
        self._cur = nxt
        return current

    def reset(self) -> None:
        """连接成功后调用，延迟回到 ``base``。对应 Go ``Reset``。"""
        self._cur = self._base
        self._failed = False

    def should_warn(self) -> bool:
        """首次失败返回 True 并标记失败；连续失败返回 False。

        对应 Go ``ShouldWarn``；``reset`` 后再次失败重新返回 True。
        """
        if self._failed:
            return False
        self._failed = True
        return True


def new_backoff(base: float = 0.0, max: float = 0.0) -> Backoff:
    """对应 Go ``NewBackoff``。"""
    return Backoff(base=base, max=max)


__all__ = [name for name in dir() if not name.startswith("_")]
