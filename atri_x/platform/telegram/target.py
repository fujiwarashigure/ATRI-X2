"""发送落点与 ctx 传递（Python 侧拆出的最小模块）。

对应 Go：``internal/platform/telegram/adapter.go`` 里的 ``target`` 类型与
``targetKey`` / ``context.WithValue`` 用法。

拆出来是因为 ``adapter`` / ``stream`` / ``capabilities`` 都要读写同一个 ctx 键，
而 ``adapter.py`` 已接近单文件行数上限（PORTING §2）；Go 侧无此文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.delivery.delivery import ctx_value, ctx_with_value

#: ctx 里携带发送落点的键（对应 Go ``targetKey{}``）。
TARGET_KEY = object()


@dataclass
class Target:
    """一次发送的落点：chat id 与平台 scope。对应 Go ``target``。"""

    chat_id: int = 0
    scope_id: str = ""


def with_target(ctx: Any, target: Target) -> Any:
    """对应 Go ``context.WithValue(ctx, targetKey{}, target)``。"""
    return ctx_with_value(ctx, TARGET_KEY, target)


def target_from_context(ctx: Any) -> tuple[Target, bool]:
    """对应 Go ``ctx.Value(targetKey{}).(target)`` 的类型断言。"""
    value = ctx_value(ctx, TARGET_KEY)
    if isinstance(value, Target):
        return value, True
    return Target(), False


__all__ = [name for name in dir() if not name.startswith("_")]
