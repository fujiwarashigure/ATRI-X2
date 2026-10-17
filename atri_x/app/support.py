"""装配层的叶子级共享助手。

放在这里而不是 :mod:`atri_x.app.runner`，是为了避免 ``runner`` ↔ ``platforms``
之间的循环导入：两边都需要 :func:`maybe_await`，而它本身不依赖任何 app 模块。

对应 Go：``internal/app`` 里没有这个文件 —— Go 的工厂签名是静态的（同步或返回
``error``），不需要"同步或协程"的统一收口。Python 侧因为并行移植期同僚可能先给
同步桩、后换协程，才需要这个判定点。它只做一次 ``inspect.isawaitable``，
不引入抽象层。
"""

from __future__ import annotations

import inspect
from typing import Any

__all__ = ["maybe_await", "maybe_close", "log_info", "log_warn", "log_error", "call_bool"]


async def maybe_await(value: Any) -> Any:
    """``value`` 是 awaitable 就 await，否则原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def maybe_close(obj: Any, ctx: Any = None) -> Any:
    """调用 ``obj.close(ctx)``；没有 ``close`` 时静默跳过。

    ``close`` 只接受零参数时回落到 ``close()``，兼容两种签名。
    """
    close = getattr(obj, "close", None)
    if not callable(close):
        return None
    try:
        return await maybe_await(close(ctx))
    except TypeError:
        return await maybe_await(close())


def log_info(logger: Any, message: str, **fields: Any) -> None:
    """``logger.info(message, **fields)``；logger 缺该方法时静默。"""
    emit = getattr(logger, "info", None)
    if callable(emit):
        emit(message, **fields)


def log_warn(logger: Any, message: str, **fields: Any) -> None:
    """``logger.warn(message, **fields)``；logger 缺该方法时静默。"""
    emit = getattr(logger, "warn", None)
    if callable(emit):
        emit(message, **fields)


def log_error(logger: Any, message: str, **fields: Any) -> None:
    """``logger.error(message, **fields)``；logger 缺该方法时静默。"""
    emit = getattr(logger, "error", None)
    if callable(emit):
        emit(message, **fields)


def call_bool(section: Any, name: str) -> bool:
    """调用 ``section.<name>()`` 并把结果当布尔用；方法缺失时返回 ``False``。

    对应 Go 里 ``section.IsReportEnabled()`` 这类取值形状。
    **唯一出处**：``runtime.py`` 与 ``runtime_support.py`` 都从这里导入 ——
    真实事故：``runtime_support`` 里裸用了一个只定义在同层 ``runtime`` 模块的私有名字，
    启动到那一行就 ``NameError``（import 门禁查不出函数体里的未定义名）。
    """
    method = getattr(section, name, None)
    return bool(method()) if callable(method) else False
