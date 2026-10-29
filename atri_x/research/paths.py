"""研究数据目录的解析规则（命令层 / GUI / 引擎子进程共用唯一出处）。

规则只有三条，顺序固定：

1. 环境变量 ``ATRIX_RESEARCH_HOME`` 显式指定 → 直接用它（安装器与测试用这个）；
2. 否则 ``<ATRI-X 配置目录>/research``；
3. 配置目录本身按平台默认：Windows ``%APPDATA%/ATRI-X``，其它平台
   ``$XDG_CONFIG_HOME/atri-x``（没有 XDG 时退回 ``~/.config/atri-x``）。

为什么单独一个模块：``/research`` 命令、桌面「研究」页、技能子进程三处必须看到
**同一个目录**；三份各写一遍时，任何一处改了平台判断都会让 GUI 显示"没有运行"
而命令能列出运行记录——这类问题无法靠单测发现，只能靠唯一出处。
"""

from __future__ import annotations

import os

__all__ = ["APP_DIR_NAME", "ENV_HOME", "XDG_APP_DIR_NAME", "default_config_dir", "research_home"]

#: Windows 下配置目录名（与 ``atri_x.config.loader`` 的 ``%APPDATA%\\ATRI-X`` 一致）。
APP_DIR_NAME = "ATRI-X"
#: 其它平台配置目录名。
XDG_APP_DIR_NAME = "atri-x"
#: 研究数据目录的环境变量覆盖名。
ENV_HOME = "ATRIX_RESEARCH_HOME"


def default_config_dir() -> str:
    """平台默认的 ATRI-X 配置目录（``%APPDATA%\\ATRI-X`` 或 ``~/.config/atri-x``）。

    环境变量都拿不到时返回空串，由调用方决定退到哪里（通常是当前目录下的
    ``research``，与引擎的兜底一致）。
    """
    if os.name == "nt":
        base = (os.environ.get("APPDATA") or os.environ.get("AppData") or "").strip()
        name = APP_DIR_NAME
    else:
        base = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        if not base:
            user_home = (os.environ.get("HOME") or "").strip()
            base = os.path.join(user_home, ".config") if user_home else ""
        name = XDG_APP_DIR_NAME
    return os.path.join(base, name) if base else ""


def research_home(config_dir: str = "") -> str:
    """研究数据目录（绝对路径）。``config_dir`` 为 ATRI-X 配置目录，可显式给出。"""
    override = (os.environ.get(ENV_HOME) or "").strip()
    if override:
        return os.path.abspath(override)
    base = (config_dir or "").strip() or default_config_dir()
    if not base:
        return os.path.abspath("research")
    return os.path.join(os.path.abspath(base), "research")
