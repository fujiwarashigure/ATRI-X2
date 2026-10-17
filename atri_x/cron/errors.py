"""cron 包内部的错误聚合 helper（Python 侧新增件）。

对应 Go：``internal/cron`` 各文件里的 ``errors.Join`` 调用点（``execution.go`` 的
``errors.Join(prepareErr, deliverErr)``、``delivery.go`` 的 ``errors.Join(buildErr, err)``、
``recovery.go`` 的 ``errors.Join(prepareErr, deliverErr)``）。

Go 的 ``errors.Join(nil, err) == err``、全 nil 时为 ``nil``；本仓的等价物是
:func:`join_errors`：无错误返回 ``None``，单个错误原样返回，多个错误装进标准库的
``BaseExceptionGroup``（``atri_x.signal.errors.is_error`` 认识这种聚合形态）。
这样内部 helper 可以像 Go 一样"把错误当值返回"，只在公开入口抛异常。
"""

from __future__ import annotations

__all__ = ["join_errors"]


def join_errors(*errors: BaseException | None) -> BaseException | None:
    """对应 Go ``errors.Join``：无错误 ``None``，单个错误原样返回，多个装进异常组。"""
    present = [err for err in errors if err is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return BaseExceptionGroup("cron", present)
