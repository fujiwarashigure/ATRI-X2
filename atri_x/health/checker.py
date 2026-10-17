"""外部 readiness 检查。

对应 Go：``internal/health/checker.go``。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class Checker(Protocol):
    """由 HTTP 服务端求值的外部 readiness 检查。对应 Go ``Checker``。"""

    def name(self) -> str: ...

    def check(self, ctx: Any = None) -> None: ...


@dataclass
class CheckerFunc:
    """把一个函数适配成 :class:`Checker`。对应 Go ``CheckerFunc``。"""

    fn: Callable[[Any], Any] | None = None
    check_name: str = ""

    def name(self) -> str:
        return (self.check_name or "").strip() or "check"

    def check(self, ctx: Any = None) -> None:
        if self.fn is None:
            return None
        _raise_if_cancelled(ctx)
        return self.fn(ctx)


@dataclass
class DirWritable:
    """验证一个目录能创建并删除临时文件。对应 Go ``DirWritable``。"""

    dir: str = ""
    check_name: str = ""

    def name(self) -> str:
        return (self.check_name or "").strip() or "dir_writable"

    def check(self, ctx: Any = None) -> None:
        """可写则返回 None，否则抛异常（对应 Go 返回 error）。"""
        _raise_if_cancelled(ctx)
        directory = (self.dir or "").strip()
        if not directory:
            raise ValueError("directory is empty")
        # Go 用 os.CreateTemp(dir, ".atri-x-health-*")：在**目标目录**里建文件，
        # 这样才能真正验证该目录可写（而不是验证临时目录可写）。
        fd, path = tempfile.mkstemp(prefix=".atri-x-health-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("ok\n")
        except BaseException:
            _silent_remove(path)
            raise
        if not os.path.exists(path):  # pragma: no cover - 防御
            raise OSError(f"temp file disappeared: {path}")
        os.remove(path)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _raise_if_cancelled(ctx: Any) -> None:
    """尽力而为地识别取消（对应 Go 的 ``ctx.Err()``）。"""
    if ctx is None:
        return
    cancelled = getattr(ctx, "is_cancelled", None)
    if callable(cancelled) and cancelled():
        raise _cancelled_error(ctx)
    error = getattr(ctx, "error", None)
    if callable(error):
        value = error()
        if value is not None:
            raise value


def _cancelled_error(ctx: Any) -> BaseException:
    import asyncio

    error = getattr(ctx, "error", None)
    if callable(error):
        value = error()
        if value is not None:
            return value
    return asyncio.CancelledError("context cancelled")


__all__ = ["Checker", "CheckerFunc", "DirWritable"]
