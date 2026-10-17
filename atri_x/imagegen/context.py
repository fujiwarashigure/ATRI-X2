"""每轮生图的 profile 传递（``@image:<name>``）。

对应 Go：``internal/imagegen/context.go``。

Go 用 ``context.WithValue`` 携带未导出的 key 类型；Python 侧 ctx 是鸭子类型：
自带 ``with_value``（:class:`atri_x.safecontext.CancelToken`）时用它，``Mapping``
时退化成一份不可变副本，其余情况下挂在本次调用新建的 dict 上。key 用模块级
``object()`` 单例，避免不同模块的 key 互相覆盖（对应 Go 的未导出结构体 key）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["profile_from", "with_profile"]

_PROFILE_KEY = object()


def _ctx_with_value(ctx: Any, key: Any, value: Any) -> Any:
    """对应 Go ``context.WithValue``（ctx 的鸭子类型写法）。"""
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(key, value)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[key] = value
    return out


def with_profile(ctx: Any, name: str) -> Any:
    """记录本轮 ``@image:<name>`` 选中的生图 profile。

    对应 Go ``WithProfile``：``name`` 为空时原样返回 ctx。
    """
    name = str(name or "").strip()
    if not name:
        return ctx
    return _ctx_with_value(ctx, _PROFILE_KEY, name)


def profile_from(ctx: Any) -> str:
    """取出本轮的生图 profile，没有则返回空串。对应 Go ``ProfileFrom``。"""
    if ctx is None:
        return ""
    getter = getattr(ctx, "value", None)
    if callable(getter):
        value = getter(_PROFILE_KEY, "")
    elif isinstance(ctx, Mapping):
        value = ctx.get(_PROFILE_KEY, "")
    else:
        value = ""
    return str(value or "").strip()
