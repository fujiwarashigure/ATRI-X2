"""安装标记 ``install.json`` 的读写（安装器与 ``status`` 共用）。

为什么单独一层：标记读取要按「显式目标 → 默认安装目录 → **当前 exe 所在目录**」的顺序
找（Inno 安装到自定义目录、或解压后直接跑 ``status`` 时标记就在 exe 旁边），这段判定在
安装/卸载/状态和测试里都要用；把它从 :mod:`atri_x.dist.install` 拆出来，后者才不超
单文件行数上限。

标记内容由 :func:`atri_x.dist.install._install_payload` 生成（脚本安装）或 Inno 安装包
在 ``CurStepChanged(ssPostInstall)`` 里写入（``"installer":"inno"``）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from atri_x.dist.paths import default_target, source_root

__all__ = ["INSTALL_MARKER", "read_marker", "write_marker"]

#: 安装标记文件名（相对安装目录）。
INSTALL_MARKER = "install.json"


def write_marker(destination: str, payload: dict[str, Any]) -> None:
    """把标记写到 ``<destination>/install.json``（目录不存在就建）。"""
    os.makedirs(destination, exist_ok=True)
    path = os.path.join(destination, INSTALL_MARKER)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_marker(target: str = "") -> dict[str, Any]:
    """读安装标记；找不到返回空字典。

    查找顺序：

    1. 显式 ``target`` 目录；
    2. :func:`atri_x.dist.paths.default_target`（脚本安装的默认位置、Inno 默认
       ``%LOCALAPPDATA%\\ATRI-X\\app``）；
    3. :func:`atri_x.dist.paths.source_root`（当前 exe 所在目录）——覆盖 Inno 装到
       自定义 ``/DIR``、或把发行包解压到任意目录后直接跑 ``status`` 的情况。
    """
    candidates = [os.path.join(target, INSTALL_MARKER)] if target else []
    seen: set[str] = set()
    for directory in _marker_dirs(target):
        path = os.path.join(directory, INSTALL_MARKER)
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _marker_dirs(target: str) -> list[str]:
    """候选目录（显式优先，随后默认安装目录与当前 exe 目录）。"""
    directories: list[str] = []
    if target:
        directories.append(target)
    try:
        directories.append(default_target())
    except Exception:  # noqa: BLE001 - 拿不到环境变量时跳过
        pass
    try:
        directories.append(source_root())
    except Exception:  # noqa: BLE001 - 拿不到源目录时跳过
        pass
    return directories
