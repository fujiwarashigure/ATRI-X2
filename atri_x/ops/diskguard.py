"""分级磁盘用量守卫（用于非必需媒体写入）。

对应 Go：``internal/ops/diskguard/guard.go``。

比例是"已用空间比例"（``0.85`` = 已用 85%）。检查结果按 ``cache_ttl`` 缓存，
避免 stat 风暴。 ``min_free_bytes`` 的判定优先于比例判定（与 Go 一致）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from atri_x.safecontext import CancelToken, CancelledError
from atri_x.sysinfo import disk_usage as _default_disk_usage

__all__ = [
    "Config",
    "Guard",
    "Level",
    "LevelCritical",
    "LevelOK",
    "LevelWarn",
    "new",
    "raise_if_cancelled",
]


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

        取消检查必须鸭子类型（见 :func:`raise_if_cancelled`）：本仓的 ctx 除了
        :class:`~atri_x.safecontext.CancelToken` 还有"只承载值的普通 Mapping"
        （入站平台路径经 ``ctx_with_value`` 落成的形状），后者没有取消语义。
        """
        if ctx is not None:
            raise_if_cancelled(ctx)
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


def raise_if_cancelled(ctx: object) -> None:
    """按 Go ``ctx.Err()`` 的语义检查取消，兼容本仓两种 ctx 形状。

    为什么不能直接 ``ctx.raise_if_cancelled()``：Go 只有一个 ``context.Context``，
    而本仓的 ctx 同时存在

    * :class:`~atri_x.safecontext.CancelToken` —— 带 ``raise_if_cancelled``；
    * 只承载值的普通 ``Mapping``（以及 ``_RunFlag`` 这类不带 ``with_value`` 的对象
      经 :func:`atri_x.delivery.ctx_with_value` 之后落成的"值包"）—— **没有取消语义**。

    入站平台消息的 ctx 就是后一种，于是旧实现在磁盘守卫这一步抛
    ``AttributeError: 'dict' object has no attribute 'raise_if_cancelled'``；这个异常又
    被媒体导入的"失败降级"吞成 ``[媒体不可用；<名字>]``，表现为**群聊里每张图都读不到**
    （tools/`get_media` 走的是回合 ctx，有 CancelToken，所以同一张图后来又能取到）。
    回归用例：``tests/test_ops.py::TestDiskGuard.test_value_only_context_*`` 与
    ``tests/test_media_manager.py::TestImportWithDiskGuard``。
    """
    if ctx is None:
        return
    raiser = getattr(ctx, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
        return
    is_cancelled = getattr(ctx, "is_cancelled", None)
    if is_cancelled is None:
        is_cancelled = getattr(ctx, "cancelled", None)
    try:
        cancelled = bool(is_cancelled() if callable(is_cancelled) else is_cancelled)
    except Exception:  # noqa: BLE001 - 判定失败按"已取消"保守处理
        cancelled = True
    if not cancelled:
        return
    error = getattr(ctx, "error", None)
    if not callable(error) and not isinstance(error, BaseException):
        error = getattr(ctx, "err", None)
    if callable(error):
        error = error()
    if isinstance(error, BaseException):
        raise error
    raise CancelledError("disk guard context cancelled")


def new(
    root: str,
    cfg: Config | None = None,
    disk_usage: Callable[[str], tuple[int, int]] | None = None,
) -> Guard:
    """对应 Go ``diskguard.New``。"""
    return Guard(root, cfg, disk_usage)
