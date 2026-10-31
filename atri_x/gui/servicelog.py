"""后台服务的生命周期日志：启动头、启动/退出记录。

为什么单独一层：``gui/service.py`` 已经在单文件行数棘轮基线里（只允许变短），而
「服务悄无声息消失」的排查恰恰需要在这里多写两件事——启动时记一行、进程退出时记下
pid / 退出码 / 运行时长。把这块日志逻辑放到独立模块，``service.py`` 只留三个调用点。

日志写到 ``<配置目录>/gui/gui.log``（与界面操作同一份，512 KiB 轮转）；写失败一律
静默：日志不是关键路径，绝不能因为磁盘/权限问题影响服务管理。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # ``service.py`` 会 import 本模块，这里只做类型引用，避免循环 import
    from atri_x.gui.service import GuiSettings

__all__ = ["record", "watch_exit", "write_header"]


def _journal() -> Any:
    """延迟 import：``atri_x.gui.journal`` 很轻，但避免本模块与 service 互相 import。"""
    from atri_x.gui import journal

    return journal


def record(settings: GuiSettings, text: str) -> None:
    """把一条服务生命周期事件写进 ``gui.log``（失败静默）。"""
    try:
        _journal().record(settings.config_dir, text)
    except Exception:  # noqa: BLE001 - 记日志失败不能影响服务管理
        pass


def write_header(handle: Any, settings: GuiSettings) -> None:
    """往 ``service.out.log`` 的文件句柄写一段「本次启动」头（配置/地址/协议端）。"""
    moment = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    proto = settings.qqproto
    proto_line = (
        f"协议端：{proto.label} · {proto.ws_url}" if proto is not None else "协议端：（无）"
    )
    header = (
        f"\n=== {moment} 启动后台服务 ===\n"
        f"配置：{settings.config_path}\n"
        f"地址：{settings.url}\n"
        f"状态接口：{settings.health_addr or '（未配置）'}\n"
        f"{proto_line}\n"
    )
    handle.write(header.encode("utf-8"))


def watch_exit(process: Any, settings: GuiSettings) -> None:
    """等后台服务退出，在 ``gui.log`` 记下 pid / 退出码 / 运行时长。

    这是「服务悄无声息消失」唯一可靠的线索：进程被 ``taskkill``、崩溃、被系统回收都只会
    留下一个退出码（``taskkill /F`` 是 1，``STATUS_ACCESS_VIOLATION`` 是 ``0xC0000005``），
    ``service.out.log`` 里通常什么都没有。
    """
    started = time.monotonic()

    def _wait() -> None:
        try:
            code = process.wait()
        except Exception:  # noqa: BLE001 - 看护线程不能把 GUI 弄崩
            return
        elapsed = time.monotonic() - started
        record(
            settings,
            f"后台服务退出（pid {process.pid}，退出码 {code}，运行 {elapsed:.0f}s）",
        )

    threading.Thread(target=_wait, name="atri-x-gui-service-exit", daemon=True).start()
