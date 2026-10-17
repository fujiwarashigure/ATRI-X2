"""按平台名把运行时解析成"平台 API 调用器"。

对应 Go：``internal/app/platform_caller.go``。

Go 有两个同名方法、返回类型不同的解析器（``atriena.PlatformAPICaller`` 与
``rules.PlatformAPICaller``）。Python 侧两者都是鸭子类型（只要有
``call_platform`` 之类的方法即可），因此共用同一个实现，只按名字分派 ——
差异仅在于调用方按哪个协议使用返回值。

``platform_caller`` 返回 ``(caller, ok)``，与 Go 的 ``(x, bool)`` 一致。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PlatformCallerResolver",
    "platformCallerResolver",
    "HookPlatformCallerResolver",
    "hookPlatformCallerResolver",
    "resolve_platform_caller",
]


def resolve_platform_caller(runtimes: Any, name: str) -> tuple[Any, bool]:
    """在 ``runtimes`` 里按名字找平台 API 调用器。"""
    target = (name or "").strip()
    if not target:
        return None, False
    for runtime in runtimes or []:
        if runtime is None:
            continue
        runtime_name = getattr(runtime, "name", None)
        resolved = runtime_name() if callable(runtime_name) else runtime_name
        if resolved != target:
            continue
        return runtime, True
    return None, False


class PlatformCallerResolver:
    """给 ``atri_x.atriena`` 用的解析器。对应 Go ``platformCallerResolver``。"""

    def __init__(self, runtimes: Any = None) -> None:
        self.runtimes = runtimes

    def platform_caller(self, name: str) -> tuple[Any, bool]:
        """对应 Go ``PlatformCaller``。"""
        return resolve_platform_caller(self.runtimes, name)

    # Go 命名别名。
    PlatformCaller = platform_caller


class HookPlatformCallerResolver:
    """给 ``atri_x.hook.rules`` 用的解析器。对应 Go ``hookPlatformCallerResolver``。"""

    def __init__(self, runtimes: Any = None) -> None:
        self.runtimes = runtimes

    def platform_caller(self, name: str) -> tuple[Any, bool]:
        """对应 Go ``PlatformCaller``。"""
        return resolve_platform_caller(self.runtimes, name)

    # Go 命名别名。
    PlatformCaller = platform_caller


platformCallerResolver = PlatformCallerResolver
hookPlatformCallerResolver = HookPlatformCallerResolver
