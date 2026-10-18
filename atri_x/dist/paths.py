"""安装器的路径判定（程序目录、安装目录、数据目录、快捷方式目录）。

单独一层的原因：这些函数既被安装/卸载流程用，也被 ``status`` 与测试用；而且它们
只依赖环境变量，不依赖注册表与 COM，单独测最省事。

约定：

- **安装目录**在 ``%LOCALAPPDATA%\\ATRI-X\\app``（用户级，不需要管理员）；
- **数据目录**在 ``%APPDATA%\\ATRI-X``（与运行期一致，卸载默认保留）；
- **开始菜单/桌面**都按当前用户的 ``%APPDATA%`` / ``%USERPROFILE%`` 推。
"""

from __future__ import annotations

import os
import sys

from atri_x.errors import AtriXError

__all__ = [
    "APP_NAME",
    "DATA_DIR_NAME",
    "InstallError",
    "data_dir",
    "default_target",
    "desktop_dir",
    "is_frozen",
    "source_root",
    "start_menu_dir",
]

#: 程序显示名。
APP_NAME = "ATRI-X"

#: 数据目录名（``%APPDATA%\\ATRI-X``，与运行期一致）。
DATA_DIR_NAME = "ATRI-X"


class InstallError(AtriXError):
    """可预期的安装/卸载失败（消息直接展示给用户）。"""


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 冻结包里。"""
    return bool(getattr(sys, "frozen", False))


def source_root() -> str:
    """要安装的「源」目录：冻结包是可执行文件目录，否则是仓库根。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _appdata() -> str:
    return (os.environ.get("APPDATA") or os.environ.get("AppData") or "").strip()


def default_target() -> str:
    """默认安装目录：``%LOCALAPPDATA%\\ATRI-X\\app``（拿不到时退回 ``%APPDATA%``）。"""
    base = (
        (os.environ.get("LOCALAPPDATA") or os.environ.get("LocalAppData") or "").strip()
        or _appdata()
    )
    if not base:
        raise InstallError("拿不到 LOCALAPPDATA/APPDATA，无法确定安装目录")
    return os.path.join(base, DATA_DIR_NAME, "app")


def data_dir() -> str:
    """用户数据目录（卸载默认保留）。"""
    base = _appdata()
    if not base:
        return ""
    return os.path.join(base, DATA_DIR_NAME)


def start_menu_dir() -> str:
    """开始菜单「程序」目录。"""
    base = _appdata()
    if not base:
        return ""
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def desktop_dir() -> str:
    """桌面目录（优先 ``USERPROFILE\\Desktop``）。"""
    home = (os.environ.get("USERPROFILE") or os.environ.get("HOME") or "").strip()
    if not home:
        return ""
    return os.path.join(home, "Desktop")
