"""安装器的注册表后端（只写 ``HKCU``，不需要管理员权限）。

三件事：

- 在「应用和功能」里登记卸载项（``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\ATRI-X``）；
- 卸载时删掉它（不存在也算成功）；
- 可选地把安装目录加进/移出**用户** ``PATH``（``HKCU\\Environment\\Path``）。

``install.py`` 通过依赖注入使用本模块（测试里换成假后端），因此这里只放真实实现，
不放安装流程。
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["UNINSTALL_KEY", "WindowsRegistry", "update_user_path"]

#: 「应用和功能」里的卸载项（``HKCU``）。
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ATRI-X"

#: 用户环境变量键。
_ENVIRONMENT_KEY = "Environment"


class WindowsRegistry:
    """真实注册表后端（只写 ``HKCU``）。"""

    def write_uninstall(self, payload: dict[str, Any]) -> None:
        """登记卸载项；字段与「应用和功能」面板约定一致。"""
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, payload["display_name"])
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, payload["display_version"])
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, payload["publisher"])
            winreg.SetValueEx(
                key, "InstallLocation", 0, winreg.REG_SZ, payload["install_location"]
            )
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, payload["display_icon"])
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, payload["uninstall"])
            winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ, payload["uninstall"])
            winreg.SetValueEx(
                key, "EstimatedSize", 0, winreg.REG_DWORD, int(payload["estimated_size_kb"])
            )
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)

    def delete_uninstall(self) -> None:
        """删除卸载项（幂等）。"""
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except FileNotFoundError:
            pass

    def add_to_user_path(self, directory: str) -> None:
        """把目录加进用户 ``PATH``（已存在则不重复）。"""
        update_user_path(add=directory)

    def remove_from_user_path(self, directory: str) -> None:
        """把目录从用户 ``PATH`` 移除（不存在也算成功）。"""
        update_user_path(remove=directory)


def update_user_path(*, add: str = "", remove: str = "") -> None:
    """读改写 ``HKCU\\Environment\\Path``（``REG_EXPAND_SZ``；不存在时新建）。"""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENVIRONMENT_KEY, 0, winreg.KEY_READ) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
    parts = [part for part in str(current or "").split(os.pathsep) if part]
    if add and add not in parts:
        parts.append(add)
    if remove:
        parts = [part for part in parts if os.path.normcase(part) != os.path.normcase(remove)]
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, _ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "Path", 0, kind or winreg.REG_EXPAND_SZ, os.pathsep.join(parts))
