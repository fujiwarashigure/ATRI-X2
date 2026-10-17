"""按键的滑动窗口限流器（进程内）。

对应 Go：``internal/ratelimit/ratelimit.go``。

``max <= 0`` 或 ``window <= 0`` 时全部放行，让调用方可以把限流当作可选项。
过期的 key 每个 window 最多清理一次，保证摊还成本恒定。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

__all__ = ["Window", "new", "new_with_clock"]


class Window:
    """每个 key 一个滑动窗口计数器。对应 Go ``ratelimit.Window``。"""

    def __init__(
        self,
        max_hits: int = 0,
        window: float = 0.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._max = max_hits
        self._window = window
        self._now = now or time.monotonic
        self._hits: dict[str, list[float]] = {}
        self._last_sweep: float | None = None
        self._lock = threading.Lock()

    @property
    def max_hits(self) -> int:
        return self._max

    @property
    def window(self) -> float:
        return self._window

    def allow(self, key: str) -> bool:
        """记录一次命中并回答该 key 是否仍在预算内。"""
        if self._max <= 0 or self._window <= 0:
            return True
        with self._lock:
            now = self._now()
            self._sweep_locked(now)
            cutoff = now - self._window
            kept = [at for at in self._hits.get(key, ()) if at > cutoff]
            if len(kept) >= self._max:
                self._hits[key] = kept
                return False
            kept.append(now)
            self._hits[key] = kept
            return True

    def refund(self, key: str) -> None:
        """退回一次此前记录的命中（预留但未落盘时用）。"""
        if self._max <= 0 or self._window <= 0:
            return
        with self._lock:
            entries = self._hits.get(key)
            if not entries:
                return
            entries = entries[:-1]
            if not entries:
                self._hits.pop(key, None)
                return
            self._hits[key] = entries

    def __len__(self) -> int:
        """当前持有命中的 key 数（对应 Go ``Window.Len``）。"""
        with self._lock:
            return len(self._hits)

    def _sweep_locked(self, now: float) -> None:
        if self._last_sweep is not None and now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        cutoff = now - self._window
        for key in list(self._hits):
            kept = [at for at in self._hits[key] if at > cutoff]
            if not kept:
                del self._hits[key]
                continue
            self._hits[key] = kept


def new(max_hits: int, window: float) -> Window:
    """对应 Go ``ratelimit.New``。"""
    return Window(max_hits, window)


def new_with_clock(max_hits: int, window: float, now: Callable[[], float]) -> Window:
    """对应 Go ``ratelimit.NewWithClock``（主要给测试用）。"""
    if max_hits <= 0 or window <= 0 or now is None:
        return Window()
    return Window(max_hits, window, now)
