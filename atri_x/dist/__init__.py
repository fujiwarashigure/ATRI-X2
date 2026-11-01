"""``atri_x.dist``：Windows 分发件（安装器 + 快捷方式）。

发行包（PyInstaller onedir）里的两个可执行文件共用一套 ``_internal``：

- ``atri-x.exe``：控制台版，跑 ``service run`` / ``config check`` / ``doctor`` 等；
- ``atri-x-gui.exe``：窗口版，桌面端（Tk 前端 + 托盘）。

本包负责“把它装进用户环境”的那部分：复制到 ``%LOCALAPPDATA%``、建开始菜单/桌面
快捷方式、写「应用和功能」卸载项、以及安装状态查询。

用户数据默认在 ``%APPDATA%\\ATRI-X``，但安装器允许把它放到安装目录（自包含）或任意
目录：选择写进安装标记的 ``dataDir``，运行期由 :mod:`atri_x.config.home` 统一解析。

| 模块 | 职责 |
|---|---|
| :mod:`atri_x.dist.shell_link` | ``ctypes`` 调 COM 创建/读取 ``.lnk`` |
| :mod:`atri_x.dist.install` | ``install`` / ``uninstall`` / ``status``（``python -m atri_x.dist.install``） |
| :mod:`atri_x.dist.migrate` | 旧数据目录 → 新数据目录（复制/移动 + 逐文件核对） |
| :mod:`atri_x.dist.cli` | ``python -m atri_x.dist.install`` 的命令行解析与文本输出 |

入口::

    python -m atri_x.dist.install status
    python -m atri_x.dist.install install --autostart
    python -m atri_x.dist.install migrate --from "%APPDATA%\\ATRI-X" --to D:\\ATRI-X\\data --move
    python -m atri_x.dist.install uninstall --yes

> 导出名故意叫 ``install_app`` / ``uninstall_app``：函数名 ``install`` 会和子模块
> ``atri_x.dist.install`` 撞车。这里用模块级 ``__getattr__`` **延迟导入**，这样
> ``python -m atri_x.dist.install`` 不会先导入子模块再执行它（否则 runpy 会警告
> "found in sys.modules after import of package"）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "APP_NAME",
    "INSTALL_MARKER",
    "UNINSTALL_KEY",
    "InstallError",
    "InstallReport",
    "default_target",
    "install_app",
    "source_root",
    "status",
    "uninstall_app",
]

#: 导出名 → (子模块, 属性名)。
_EXPORTS = {
    "APP_NAME": ("atri_x.dist.install", "APP_NAME"),
    "INSTALL_MARKER": ("atri_x.dist.install", "INSTALL_MARKER"),
    "UNINSTALL_KEY": ("atri_x.dist.install", "UNINSTALL_KEY"),
    "InstallError": ("atri_x.dist.install", "InstallError"),
    "InstallReport": ("atri_x.dist.install", "InstallReport"),
    "default_target": ("atri_x.dist.install", "default_target"),
    "install_app": ("atri_x.dist.install", "install"),
    "source_root": ("atri_x.dist.install", "source_root"),
    "status": ("atri_x.dist.install", "status"),
    "uninstall_app": ("atri_x.dist.install", "uninstall"),
}


def __getattr__(name: str) -> Any:
    """按需导入 ``atri_x.dist.install`` 里的导出名（见模块 docstring 末段）。"""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value
