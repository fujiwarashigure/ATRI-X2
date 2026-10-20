"""``python -m atri_x.gui``：桌面端进程入口。

启动顺序（每一步失败都尽量给用户一个**能看懂**的弹窗，而不是静默退出）：

1. 解析参数；``--selftest`` / 自启动开关这类一次性命令直接处理；
2. 单实例检查：已有窗口就把它提到前台，然后退出；
3. 准备配置与 token（:func:`atri_x.gui.service.prepare_settings`）；
4. 探测后台服务；不通就后台拉起 ``atri-x service run``（除非 ``--no-service``）；
5. 建 Tk 窗口 + 托盘，进入事件循环。

``--minimized`` 用于开机自启：窗口直接收进托盘，后台服务照常运行。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import Sequence

from atri_x import __version__
from atri_x.gui import autostart
from atri_x.gui.app import WINDOW_TITLE, GuiApp
from atri_x.gui.client import RemoteClient
from atri_x.gui.process import record_startup_failure
from atri_x.gui.selftest import run_selftest
from atri_x.gui.service import (
    GuiConfigError,
    GuiSettings,
    focus_existing_window,
    prepare_settings,
    probe_server,
    single_instance_lock,
    spawn_service,
    wait_until_ready,
)

__all__ = ["main", "parse_args", "build_settings"]

#: 退出码：配置问题。
EXIT_CONFIG = 2

#: 退出码：Tk 不可用（没有图形环境/没装 tkinter）。
EXIT_NO_TK = 3


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.gui",
        description="ATRI-X 桌面端（Tk 前端 + 托盘 + 后台服务管理）",
    )
    parser.add_argument("--config", default="", help="app.toml 路径（默认用平台配置目录）")
    parser.add_argument("--url", default="", help="覆盖后台服务 WS 地址")
    parser.add_argument("--token", default="", help="覆盖客户端 token（一般不用）")
    parser.add_argument("--client-id", default="", help="覆盖客户端 id（默认取配置里的第一个）")
    parser.add_argument("--no-service", action="store_true", help="不拉起后台服务，只连接已有服务")
    parser.add_argument("--no-tray", action="store_true", help="不建托盘（调试用）")
    parser.add_argument("--minimized", action="store_true", help="启动后直接进托盘（开机自启用）")
    parser.add_argument("--selftest", action="store_true", help="只验证 Tk 界面能构建，不连服务")
    parser.add_argument("--enable-autostart", action="store_true", help="启用开机自启后退出")
    parser.add_argument("--disable-autostart", action="store_true", help="关闭开机自启后退出")
    parser.add_argument("--autostart-status", action="store_true", help="打印开机自启状态后退出")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """进程入口，返回退出码。"""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        _print(f"atri-x-gui {__version__}")
        return 0
    if args.selftest:
        return run_selftest()
    if args.enable_autostart or args.disable_autostart or args.autostart_status:
        return _run_autostart_command(args)

    first, _handle = single_instance_lock()
    if not first:
        if focus_existing_window(WINDOW_TITLE):
            return 0
        _show_message("ATRI-X 已经在运行", "同一个用户只能开一个桌面窗口；请从任务栏/托盘里找它。")
        return 0

    try:
        settings = build_settings(args)
    except GuiConfigError as exc:
        _show_message("ATRI-X 配置有问题", str(exc))
        return EXIT_CONFIG

    service_owned = False
    if not args.no_service:
        ok, _reason = probe_server(settings.url, settings.client_id, settings.token, timeout=1.0)
        if not ok:
            try:
                spawn_service(settings)
                service_owned = True
                _wait_ready_in_background(settings)
            except Exception as exc:  # noqa: BLE001 - 拉不起来也别崩，界面会显示未连接
                _show_message(
                    "ATRI-X 无法启动后台服务",
                    f"{exc}\n\n可以稍后从「服务 → 启动/确认后台服务」重试。",
                )

    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - 精简发行版可能没有 tkinter
        _print(f"GUI_NO_TK {exc}")
        return EXIT_NO_TK

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无图形环境
        _print(f"GUI_NO_DISPLAY {exc}")
        return EXIT_NO_TK

    client = RemoteClient(settings.url, settings.client_id, settings.token)
    app = GuiApp(
        root,
        settings,
        client=client,
        tray_enabled=not args.no_tray,
        minimized=args.minimized,
        service_owned=service_owned,
    )
    app.run()
    return 0


def build_settings(args: argparse.Namespace) -> GuiSettings:
    """准备配置，并叠加命令行覆盖项。"""
    settings = prepare_settings(args.config)
    if not args.url and not args.token and not args.client_id:
        return settings
    return GuiSettings(
        config_path=settings.config_path,
        config_dir=settings.config_dir,
        url=args.url or settings.url,
        client_id=args.client_id or settings.client_id,
        token=args.token or settings.token,
        token_env=settings.token_env,
        changed=settings.changed,
    )


def _wait_ready_in_background(settings: GuiSettings) -> None:
    """后台等服务就绪（GUI 侧靠 WS 重连自己接上，这里只为日志/排障）。"""

    def _wait() -> None:
        wait_until_ready(settings, timeout=60.0)

    threading.Thread(target=_wait, name="atri-x-gui-wait-service", daemon=True).start()


def _run_autostart_command(args: argparse.Namespace) -> int:
    if args.autostart_status:
        state = autostart.status()
        _print(
            f"AUTOSTART supported={state.supported} enabled={state.enabled} "
            f"command={state.command or '-'}"
        )
        return 0
    if args.enable_autostart:
        ok, detail = autostart.enable()
    else:
        ok, detail = autostart.disable()
    _print(detail)
    return 0 if ok else 1


def _show_message(title: str, text: str) -> None:
    """有 Tk 就弹窗，没有就打到 stdout/stderr；**同时写一份固定日志**。

    窗口版 exe 没有控制台，配置目录都建不出来时连 ``gui/`` 都写不进去；这种「最惨的
    启动失败」如果只弹一个窗、用户点掉就什么线索都没有。所以这里先落盘（候选路径见
    :func:`atri_x.gui.process.startup_log_candidates`），再把日志路径告诉用户。
    """
    log_path = record_startup_failure(title, text)
    if log_path:
        text = f"{text}\n\n（已记录到日志：{log_path}）"
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        try:
            messagebox.showerror(title, text, parent=root)
        finally:
            root.destroy()
        return
    except Exception:  # noqa: BLE001 - 没图形环境时退化成文本
        pass
    _print(f"{title}: {text}")


def _print(text: str) -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:  # windowed 版 exe 没有控制台
        return
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
