"""用户数据根的统一解析（配置 / 会话库 / DSH 状态 / 日志 / 技能都跟着它走）。

**为什么需要这一层**：安装版默认把用户数据放在 ``%APPDATA%\\ATRI-X``（与
``%LOCALAPPDATA%\\ATRI-X\\app`` 分开）；但用户经常希望「整个 ATRI-X 就在我安装时选
的那个文件夹里」，方便备份、迁移、卸载与整包拷走。安装器因此会在安装时询问一个
**数据与配置目录**，并把选择写进安装目录的 ``install.json``（``"dataDir"`` 字段）。
运行期由本模块统一解析，其他模块不再各自判断 ``%APPDATA%``。

**解析顺序**（固定，先命中先用）：

1. 环境变量 ``ATRIX_HOME``（显式覆盖；便携/测试/高级用户用）；
2. 冻结包可执行文件旁边的 ``install.json`` 里的 ``dataDir``（安装器写入）；
3. 平台默认：Windows ``%APPDATA%\\ATRI-X``，其它平台 ``$XDG_CONFIG_HOME/atri-x``
   （缺省 ``~/.config/atri-x``）。

**目录布局**：数据根就是「配置目录」（``app.toml`` 所在目录），与默认布局完全一致；
数据根下的 ``data/`` 是数据目录（SQLite/日志等）。也就是说：

- ``user_home_dir()`` → 数据根（默认 ``%APPDATA%\\ATRI-X``）；
- ``user_data_dir()`` → ``<数据根>/data``（非 Windows 无覆盖时仍按 XDG 数据目录）；
- DSH 的 ``DSH_HOME``、研究/draw.io 数据、技能目录、GUI 状态都相对数据根。

只读与容错：安装标记损坏、``dataDir`` 不是绝对路径、环境变量指向相对路径时一律
**忽略并回退**到下一档，不抛异常（配置层在启动最早期就要用，不能因为一个坏标记
把程序挡死）；真正的可写性问题留给创建目录的那一步报错。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from atri_x.config.app import APP_DIR_NAME, XDG_APP_DIR_NAME

__all__ = [
    "ENV_HOME",
    "INSTALL_MARKER_NAME",
    "describe_home",
    "home_source",
    "installer_data_dir",
    "user_data_dir",
    "user_home_dir",
]

#: 显式覆盖用户数据根的环境变量（与 ``ATRIX_CONFIG_FILE`` 各管一件事）。
ENV_HOME = "ATRIX_HOME"

#: 安装标记文件名（与 :data:`atri_x.dist.marker.INSTALL_MARKER` 同名；这里不 import
#: ``dist``，避免配置层反向依赖发行层，也避免安装器最早期出现循环导入）。
INSTALL_MARKER_NAME = "install.json"

#: 安装标记里承载数据目录的键。
MARKER_DATA_DIR_KEY = "dataDir"


def installer_data_dir() -> str:
    """安装标记里写的用户数据目录；没有/不可用时返回空串。

    只在**冻结包**里读（源码运行时的 ``sys.executable`` 是 Python 解释器，旁边没有
    ``install.json``；这也避免把某个仓库根目录误当成安装目录）。
    """
    if not getattr(sys, "frozen", False):
        return ""
    executable = str(getattr(sys, "executable", "") or "")
    if not executable:
        return ""
    marker_path = os.path.join(os.path.dirname(os.path.abspath(executable)), INSTALL_MARKER_NAME)
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get(MARKER_DATA_DIR_KEY)
    if not isinstance(value, str):
        return ""
    return _absolute_dir(value)


def home_source() -> str:
    """当前数据根的来源：``env`` / ``installer`` / ``default``（取不到时 ``""``）。"""
    if _absolute_dir(os.environ.get(ENV_HOME) or ""):
        return "env"
    if installer_data_dir():
        return "installer"
    return "default" if _platform_home() else ""


def user_home_dir() -> str:
    """用户数据根（绝对路径）；取不到时返回空串。

    顺序见模块 docstring：``ATRIX_HOME`` → 安装标记 ``dataDir`` → 平台默认。
    """
    for candidate in (
        _absolute_dir(os.environ.get(ENV_HOME) or ""),
        installer_data_dir(),
        _platform_home(),
    ):
        if candidate:
            return candidate
    return ""


def user_data_dir() -> str:
    """数据目录（SQLite/日志等）；取不到时返回空串。

    有覆盖（环境变量或安装器选择）时是 ``<数据根>/data``（与 Windows 默认布局一致）；
    没有覆盖时保持平台默认：Windows ``%APPDATA%\\ATRI-X\\data``，其它平台
    ``$XDG_DATA_HOME/atri-x`` 或 ``~/.local/share/atri-x``。
    """
    home = user_home_dir()
    if home:
        return os.path.join(home, "data")
    return _platform_data_dir()


def describe_home() -> dict[str, Any]:
    """给 ``status`` / 诊断 / 文档用的快照（只含路径，不含任何密钥）。"""
    source = home_source()
    return {
        "homeDir": user_home_dir(),
        "dataDir": user_data_dir(),
        "source": source,
        "envVariable": ENV_HOME,
        "installerDataDir": installer_data_dir(),
    }


# ---------------------------------------------------------------------------
# 内部：平台默认目录 / 路径规整
# ---------------------------------------------------------------------------


def _absolute_dir(value: str) -> str:
    """把候选路径规整成绝对目录；相对路径/空串/非法值一律返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        expanded = os.path.expandvars(os.path.expanduser(text))
    except (ValueError, OSError):
        return ""
    if not os.path.isabs(expanded):
        return ""
    return os.path.normpath(expanded)


def _platform_home() -> str:
    """平台默认的用户数据根（``%APPDATA%\\ATRI-X`` / ``$XDG_CONFIG_HOME/atri-x``）。"""
    if os.name == "nt":
        base = (os.environ.get("AppData") or os.environ.get("APPDATA") or "").strip()
        if not base:
            return ""
        return _absolute_dir(os.path.join(base, APP_DIR_NAME))
    base = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if not base:
        home = (os.environ.get("HOME") or "").strip()
        if not home:
            return ""
        base = os.path.join(home, ".config")
    return _absolute_dir(os.path.join(base, XDG_APP_DIR_NAME))


def _platform_data_dir() -> str:
    """平台默认数据目录（无数据根覆盖时的口径）。"""
    if os.name == "nt":
        home = _platform_home()
        return os.path.join(home, "data") if home else ""
    base = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if base:
        return _absolute_dir(os.path.join(base, XDG_APP_DIR_NAME))
    home = (os.environ.get("HOME") or "").strip()
    if home:
        return _absolute_dir(os.path.join(home, ".local", "share", XDG_APP_DIR_NAME))
    return ""
