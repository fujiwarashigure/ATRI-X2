"""应用入口：运行模式、启动选项与 ``Run``。

对应 Go：``internal/app/app.go``。

Go 的 ``Run(ctx, opts)`` 里 ``ctx`` 是生命周期上下文；Python 侧对应
``asyncio`` 任务取消 —— 调用方取消 ``run`` 协程即等价于 Go 里取消 ctx。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from atri_x.app.dependencies import Dependencies, Runner

__all__ = ["RunMode", "Options", "Run", "run", "new_runner", "run_sync"]


class RunMode(str):
    """Go ``RunMode``：``str`` 子类，便于直接当字符串比较/打印。"""

    __slots__ = ()

    def __new__(cls, value: str = "") -> "RunMode":
        return super().__new__(cls, value)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return f"RunMode({str(self)!r})"


#: 与 Go 常量逐字对应。
RUN_MODE_AUTO = RunMode("auto")
RUN_MODE_FULL = RunMode("full")
RUN_MODE_CLI_ONLY = RunMode("cli")
RUN_MODE_SERVICE = RunMode("service")

# PascalCase 别名（与 Go 导出名一致，便于交叉检索）。
RunModeAuto = RUN_MODE_AUTO
RunModeFull = RUN_MODE_FULL
RunModeCLIOnly = RUN_MODE_CLI_ONLY
RunModeService = RUN_MODE_SERVICE


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Options:
    """对应 Go ``Options``。"""

    config_path: str = ""
    version: str = ""
    #: Go 的 ``time.Time``；一律带时区。留空表示"现在"。
    started_at: datetime = field(default_factory=_now_utc)
    mode: RunMode = RUN_MODE_AUTO


def new_runner(deps: "Dependencies | None" = None) -> "Runner":
    """``NewRunner(DefaultDependencies())``。"""
    # 延迟导入：``dependencies`` 需要 ``Options``/``RunMode``，顶层互相 import 会成环。
    from atri_x.app.dependencies import DefaultDependencies, Runner as _Runner

    return _Runner(deps if deps is not None else DefaultDependencies())


async def run(ctx: Any = None, opts: Options | None = None) -> None:
    """用生产依赖集启动 ATRI-X。对应 Go ``Run``。

    ``ctx`` 只用于对齐 Go 签名：传 ``None`` 时使用"永不取消"的上下文并沿用当前
    事件循环；传入 :class:`asyncio.Event` 或任何带 ``is_set``/``wait`` 的对象时
    会在它被置位后请求关停。
    """
    runner = new_runner()
    return await runner.run(ctx, opts if opts is not None else Options())


#: Go 同名导出（``app.Run``）。
Run = run


def run_sync(ctx: Any = None, opts: Options | None = None) -> None:
    """同步包装，便于 CLI/脚本直接调用（对应 Go 的阻塞式 ``Run``）。"""
    asyncio.run(run(ctx, opts))
