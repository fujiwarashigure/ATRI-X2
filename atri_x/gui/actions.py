"""主窗口的「服务」动作：启动 / 重启 / 停止后台服务、打开目录/日志、关于。

这些动作原来写在 :class:`atri_x.gui.app.GuiApp` 里，抽出来是为了让主窗口只保留
"窗口骨架"（选项卡容器、状态栏、托盘、事件队列）。函数统一接收 ``app``，只通过
它的公开成员（``root`` / ``settings`` / ``client`` / ``post`` / ``reconnect``）和少量
界面状态字段工作，不反向 import ``app`` 模块（避免循环导入）。

线程模型：只有"重启后台服务"会阻塞到服务就绪，放在后台线程里跑，结果用
``app.post("restart", ...)`` 回到主线程，由 :func:`handle_restart` 展示。
"""

from __future__ import annotations

import os
import threading
from tkinter import messagebox
from typing import Any

from atri_x import __version__
from atri_x.gui.process import app_dir, open_path, service_log_path

__all__ = [
    "handle_restart",
    "open_config_dir",
    "open_service_log",
    "restart_backend",
    "show_about",
    "show_tray_help",
    "start_backend",
    "stop_backend",
]


def _auto_recover_hook(app: Any, action: str) -> None:
    """调用桌面端的断线自愈暂停/恢复钩子。

    ``--selftest``/单测里构造的是裸 :class:`GuiApp`（没有这两个方法），所以按名字取、
    取不到就跳过：服务菜单的动作不能因为自愈层缺失而报错。
    """
    hook = getattr(app, action, None)
    if hook is not None:
        hook()


def start_backend(app: Any) -> None:
    """确保后台服务在跑；成功后就重连。"""
    if app.settings is None:
        messagebox.showinfo("ATRI-X", "当前没有后台服务的配置信息。", parent=app.root)
        return
    from atri_x.gui.service import prepare_settings, spawn_service, wait_until_ready

    # 手动启动/等待期间暂停自愈：避免看护线程和这条路径同时拉起两个服务。
    _auto_recover_hook(app, "suspend_auto_recover")
    try:
        settings = prepare_settings(app.settings.config_path)
    except Exception as exc:  # noqa: BLE001 - 直接展示给用户
        _auto_recover_hook(app, "resume_auto_recover")
        messagebox.showerror("ATRI-X", f"准备配置失败：{exc}", parent=app.root)
        return
    ok, reason = wait_until_ready(settings, timeout=1.0)
    if not ok:
        try:
            spawn_service(settings)
        except Exception as exc:  # noqa: BLE001
            _auto_recover_hook(app, "resume_auto_recover")
            messagebox.showerror("ATRI-X", f"启动后台服务失败：{exc}", parent=app.root)
            return
        app.service_owned = True
        ok, reason = wait_until_ready(settings, timeout=25.0)
    if not ok:
        _auto_recover_hook(app, "resume_auto_recover")
        messagebox.showerror(
            "ATRI-X",
            f"后台服务没有就绪：{reason}\n\n日志：{service_log_path()}",
            parent=app.root,
        )
        return
    app.settings = settings
    app.service_owned = True
    _auto_recover_hook(app, "resume_auto_recover")
    app.reconnect()


def restart_backend(app: Any) -> None:
    """重启后台服务（配置改完后应用改动）。实际工作在后台线程里做。"""
    if app.settings is None:
        messagebox.showinfo("ATRI-X", "当前没有后台服务的配置信息。", parent=app.root)
        return
    if not messagebox.askyesno(
        "ATRI-X",
        "重启后台服务？\n\n会中断正在进行的任务，QQ 机器人会短暂掉线；"
        "GUI 会在服务就绪后自动重连。",
        parent=app.root,
    ):
        return
    # 重启本身会让连接断开 1–2 秒；期间不能让断线自愈再插一脚。
    _auto_recover_hook(app, "suspend_auto_recover")
    app._status_text = "● 正在重启后台服务…"
    threading.Thread(
        target=lambda: _restart_worker(app), name="atri-x-gui-restart", daemon=True
    ).start()


def _restart_worker(app: Any) -> None:
    from atri_x.gui.service import restart_service

    try:
        ok, detail = restart_service(app.settings, timeout=40.0)
    except Exception as exc:  # noqa: BLE001 - 结果要回到界面
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    app.post("restart", (ok, detail))


def handle_restart(app: Any, payload: Any) -> None:
    """主线程处理重启结果（``app._handle_event`` 收到 ``restart`` 事件时调用）。"""
    ok, detail = payload
    # 无论成败都恢复自愈：失败时服务可能是死的，交给看护线程按退避再试。
    _auto_recover_hook(app, "resume_auto_recover")
    if ok:
        app.service_owned = True
        app._append_notice("后台服务已重启，正在重新连接…")
        app.reconnect()
        return
    app._status_text = "● 未连接"
    messagebox.showerror(
        "ATRI-X",
        f"重启后台服务失败：{detail}\n\n日志：{service_log_path()}",
        parent=app.root,
    )


def stop_backend(app: Any) -> None:
    """强制结束后台服务（会中断任务、机器人掉线）。"""
    if not messagebox.askyesno(
        "ATRI-X",
        "强制结束后台服务？\n\n这会中断正在进行的任务，QQ 机器人也会掉线。",
        parent=app.root,
    ):
        return
    from atri_x.gui.service import stop_service

    ok, detail = stop_service()
    if ok:
        # 用户是**故意**停的：停止自愈，否则看护线程会立刻把服务拉起来。
        _auto_recover_hook(app, "suspend_auto_recover")
        app._status_text = "● 后台服务已停止"
    (messagebox.showinfo if ok else messagebox.showerror)("ATRI-X", detail, parent=app.root)


def open_config_dir(app: Any) -> None:
    """用系统文件管理器打开配置目录。"""
    directory = app.settings.config_dir if app.settings is not None else app_dir()
    if not open_path(directory):
        messagebox.showinfo("ATRI-X", f"打不开目录：{directory}", parent=app.root)


def open_service_log(app: Any) -> None:
    """用系统默认程序打开后台服务日志。"""
    path = service_log_path()
    if not os.path.exists(path):
        messagebox.showinfo("ATRI-X", f"还没有日志：{path}", parent=app.root)
        return
    if not open_path(path):
        messagebox.showinfo("ATRI-X", f"日志文件：{path}", parent=app.root)


def show_about(app: Any) -> None:
    """打开「关于 ATRI-X」窗口（版本 / 项目简述 / 仓库地址 / 推荐语 / 署名）。"""
    from atri_x.gui.about import show_about_window

    try:
        show_about_window(app)
    except Exception as exc:  # noqa: BLE001 - 窗口建不起来时退回消息框
        messagebox.showinfo(
            "关于 ATRI-X",
            f"ATRI-X {__version__}\n\n打开「关于」窗口失败：{exc}",
            parent=app.root,
        )


def show_tray_help(app: Any) -> None:
    tray = getattr(app, "_tray", None)
    tray_error = str(getattr(app, "_tray_error", "") or "")
    if tray is not None:
        status = "托盘图标：正常（关窗口 = 隐藏到托盘，后台服务不受影响）。"
    elif tray_error:
        status = f"托盘图标：不可用（{tray_error}）。关窗口会直接退出窗口，后台服务继续运行。"
    else:
        status = "托盘图标：未启用（--no-tray 或自检模式）。"
    messagebox.showinfo(
        "托盘与后台",
        f"{status}\n\n"
        "要彻底退出请用托盘菜单或「文件」菜单。\n"
        "配置保存后：state.toml 会热加载；platform/services/.env 需要「服务 → 重启后台服务」。",
        parent=app.root,
    )
