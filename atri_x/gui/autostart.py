"""开机自启：写用户注册表 ``Run`` 项（``HKCU\\…\\CurrentVersion\\Run``）。

只动当前用户的 ``Run`` 键（不需要管理员权限，也不碰 ``HKLM``）：

- :func:`enable` —— 写入/覆盖 ``ATRI-X`` 项，值为「启动托盘常驻 GUI」的命令行；
- :func:`disable` —— 删除该项（不存在也算成功，幂等）；
- :func:`status` —— 读回当前值，判断是否指向本程序。

冻结包的命令行指向 ``atri-x-gui.exe --minimized``；源码运行指向
``python -m atri_x.gui --minimized``——后者只用于开发验证。

> 用 ``--minimized`` 启动：界面不弹出来，直接进托盘，后台服务照常拉起。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

__all__ = ["RUN_KEY", "VALUE_NAME", "AutostartState", "disable", "enable", "status", "command_line"]

#: 注册表 Run 键路径（相对 ``HKEY_CURRENT_USER``）。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 值名。
VALUE_NAME = "ATRI-X"


@dataclass(frozen=True)
class AutostartState:
    """开机自启状态。"""

    supported: bool
    enabled: bool
    command: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """给 CLI/JSON 输出用。"""
        return {
            "supported": self.supported,
            "enabled": self.enabled,
            "command": self.command,
            "detail": self.detail,
        }


def command_line() -> str:
    """当前环境下的自启动命令行（带 ``--minimized``）。"""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        return f'"{exe}" --minimized'
    return f'"{os.path.abspath(sys.executable)}" -m atri_x.gui --minimized'


def enable(command: str = "") -> tuple[bool, str]:
    """启用开机自启；返回 ``(是否成功, 说明)``。"""
    if os.name != "nt":
        return False, "只有 Windows 支持注册表自启动"
    import winreg

    value = (command or "").strip() or command_line()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)
    except OSError as exc:
        return False, f"写注册表失败：{exc}"
    return True, f"已启用开机自启：{value}"


def disable() -> tuple[bool, str]:
    """禁用开机自启（幂等）。"""
    if os.name != "nt":
        return False, "只有 Windows 支持注册表自启动"
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, "开机自启本来就是关闭的"
    except OSError as exc:
        return False, f"写注册表失败：{exc}"
    return True, "已关闭开机自启"


def status() -> AutostartState:
    """读当前状态。"""
    if os.name != "nt":
        return AutostartState(supported=False, enabled=False, command="", detail="非 Windows 平台")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return AutostartState(supported=True, enabled=False, command="")
    except OSError as exc:
        return AutostartState(supported=True, enabled=False, command="", detail=f"读注册表失败：{exc}")
    text = str(value or "")
    return AutostartState(supported=True, enabled=bool(text), command=text)
