"""分级磁盘用量守卫（用于非必需媒体写入）。

对应 Go：``internal/ops/diskguard/guard.go``。

比例是"已用空间比例"（``0.85`` = 已用 85%）。检查结果按 ``cache_ttl`` 缓存，
避免 stat 风暴。 ``min_free_bytes`` 的判定优先于比例判定（与 Go 一致）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from atri_x.safecontext import CancelToken
from atri_x.sysinfo import disk_usage as _default_disk_usage

__all__ = ["Config", "Guard", "Level", "LevelCritical", "LevelOK", "LevelWarn", "new"]


class Level:
    """粗粒度磁盘健康等级。对应 Go ``diskguard.Level``。"""

    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


LevelOK = Level.OK
LevelWarn = Level.WARN
LevelCritical = Level.CRITICAL


@dataclass
class Config:
    """阈值配置。对应 Go ``diskguard.Config``。"""

    warn_ratio: float = 0.0
    critical_ratio: float = 0.0
    min_free_bytes: int = 0
    cache_ttl: float = 0.0


class Guard:
    """带缓存的磁盘守卫。对应 Go ``diskguard.Guard``。"""

    def __init__(
        self,
        root: str,
        cfg: Config | None = None,
        disk_usage: Callable[[str], tuple[int, int]] | None = None,
    ) -> None:
        cfg = cfg or Config()
        if cfg.warn_ratio <= 0 or cfg.warn_ratio >= 1:
            cfg.warn_ratio = 0.85
        if cfg.critical_ratio <= 0 or cfg.critical_ratio >= 1:
            cfg.critical_ratio = 0.95
        if cfg.cache_ttl <= 0:
            cfg.cache_ttl = 5.0
        self._cfg = cfg
        self._root = root
        self._disk_usage = disk_usage or _default_disk_usage
        self._level = LevelOK
        self._checked: float | None = None
        self._last_error: BaseException | None = None

    @property
    def config(self) -> Config:
        return self._cfg

    @property
    def last_error(self) -> BaseException | None:
        """上一次磁盘探测失败的原因（Go 里由 ``Check`` 的第三个返回值给出）。"""
        return self._last_error

    async def check(self, ctx: CancelToken | None = None) -> tuple[str, bool]:
        """返回 ``(level, critical)``；``critical`` 为真时应拒绝非必需写入。

        ``ctx`` 已取消时抛该 ctx 的错误（Go 返回 error）；磁盘探测失败按 Go
        的行为**降级**为 ``(ok, False)`` 并记录 :attr:`last_error`。
        """
        if ctx is not None:
            ctx.raise_if_cancelled()
        now = time.monotonic()
        if self._checked is not None and now - self._checked < self._cfg.cache_ttl:
            return self._level, self._level == LevelCritical
        try:
            total, free = self._disk_usage(self._root)
        except OSError as exc:
            self._level = LevelOK
            self._checked = now
            self._last_error = exc
            return self._level, False
        level = LevelOK
        if total > 0:
            used = 1 - float(free) / float(total)
            if self._cfg.min_free_bytes > 0 and free < self._cfg.min_free_bytes:
                level = LevelCritical
            elif used >= self._cfg.critical_ratio:
                level = LevelCritical
            elif used >= self._cfg.warn_ratio:
                level = LevelWarn
        self._level = level
        self._checked = now
        self._last_error = None
        return level, level == LevelCritical

    def error(self, level: str) -> Exception | None:
        """把 critical 等级格式化成给调用方的错误（对应 Go ``Guard.Error``）。"""
        if level != LevelCritical:
            return None
        return OSError("disk space is critically low; refusing non-essential media writes")


def new(
    root: str,
    cfg: Config | None = None,
    disk_usage: Callable[[str], tuple[int, int]] | None = None,
) -> Guard:
    """对应 Go ``diskguard.New``。"""
    return Guard(root, cfg, disk_usage)
