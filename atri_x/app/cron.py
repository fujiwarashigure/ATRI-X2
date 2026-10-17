"""Cron 启动编排：异步准备 cron 任务表。

对应 Go：``internal/app/cron.go``。

Go 用 ``go func(){...; close(done)}()`` + ``chan struct{}`` 表达"异步准备，完成时
通知"；Python 侧换成 :class:`asyncio.Task` + :class:`asyncio.Event`，
``start_cron_async`` 返回的 ``(task, done)`` 就是那对 ``(goroutine, chan)``。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_x.cron import PlatformTarget
from atri_x.maintenance import setup_cron as _maintenance_setup_cron

__all__ = [
    "startCronAsync",
    "start_cron_async",
    "setupCron",
    "setup_cron",
    "enabledCronPlatforms",
    "enabled_cron_platforms",
    "platformConfigEnabled",
    "platform_config_enabled",
]


def start_cron_async(
    ctx: Any,
    manager: Any,
    service: Any,
    cfg: Any,
    logger: Any = None,
    done: asyncio.Event | None = None,
) -> asyncio.Task:
    """对应 Go ``startCronAsync``：立刻返回，准备在后台跑。

    ``done`` 由调用方提供（``foundationLifecycle`` 持有），任务结束时置位；
    没传就内部新建一个，只是外部观测不到。
    """
    if done is None:
        done = asyncio.Event()

    async def runner() -> None:
        started = asyncio.get_running_loop().time()
        try:
            await setup_cron(ctx, manager, cfg)
        except asyncio.CancelledError:
            done.set()
            raise
        except Exception as exc:  # noqa: BLE001 - 启动失败只记日志，不炸应用
            elapsed = asyncio.get_running_loop().time() - started
            _warn(logger, "cron async startup failed", duration=_seconds(elapsed), error=str(exc))
            return
        finally:
            done.set()
        elapsed = asyncio.get_running_loop().time() - started
        _info(logger, "cron async startup completed", duration=_seconds(elapsed))

    return asyncio.ensure_future(runner())


async def setup_cron(ctx: Any, manager: Any, cfg: Any) -> None:
    """对应 Go ``setupCron``（转发到 maintenance）。"""
    await _maintenance_setup_cron(ctx, manager, cfg)


def enabled_cron_platforms(cfg: Any) -> list[PlatformTarget]:
    """对应 Go ``enabledCronPlatforms``。"""
    if cfg is None:
        return []
    out: list[PlatformTarget] = []
    platform = getattr(cfg, "platform", None) or {}
    security = getattr(cfg, "security", None)
    superadmins = getattr(security, "superadmins", None) or {}
    for name, raw in platform.items():
        if not platform_config_enabled(raw):
            continue
        out.append(
            PlatformTarget(name=name, superadmin_ids=list(superadmins.get(name) or []))
        )
    return out


def platform_config_enabled(raw: Any) -> bool:
    """对应 Go ``platformConfigEnabled``：只认真正的布尔 ``enabled``。"""
    if not isinstance(raw, dict):
        return False
    if "enabled" not in raw:
        return False
    return raw["enabled"] is True


def _warn(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "warn", None)
    if callable(emit):
        emit(message, **fields)


def _info(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "info", None)
    if callable(emit):
        emit(message, **fields)


def _seconds(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") + "s"


startCronAsync = start_cron_async
setupCron = setup_cron
enabledCronPlatforms = enabled_cron_platforms
platformConfigEnabled = platform_config_enabled
