"""安装标记 ``install.json`` 的读写（安装器与 ``status`` 共用）。

为什么单独一层：标记读取要按「显式目标 → 默认安装目录 → **当前 exe 所在目录**」的顺序
找（Inno 安装到自定义目录、或解压后直接跑 ``status`` 时标记就在 exe 旁边），这段判定在
安装/卸载/状态和测试里都要用；把它从 :mod:`atri_x.dist.install` 拆出来，后者才不超
单文件行数上限。

标记内容：脚本安装由 :func:`atri_x.dist.install.install` 生成；Inno 安装包在
``ssPostInstall`` 里调 ``atri-x.exe -m atri_x.dist.install finalize`` 写成（标记含
``"installer":"inno"`` 与用户数据目录 ``"dataDir"``）。为什么不是 Inno 自己写：
Inno 的 ``[Code]`` 只能读写 ANSI 字符串，写不出带非 ASCII 路径的 UTF-8 JSON。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from atri_x import __version__
from atri_x.dist.paths import default_target, source_root

__all__ = [
    "DATA_FLAG_NAME",
    "INSTALL_MARKER",
    "install_state_payload",
    "read_marker",
    "write_install_state",
    "write_marker",
]

#: 安装标记文件名（相对安装目录）。
INSTALL_MARKER = "install.json"
#: 数据目录的纯文本副本（人读/排障用；卸载器读的是注册表，见 ``atri-x.iss``）。
DATA_FLAG_NAME = "data-dir.txt"


def write_marker(destination: str, payload: dict[str, Any]) -> None:
    """把标记写到 ``<destination>/install.json``（目录不存在就建）。"""
    os.makedirs(destination, exist_ok=True)
    path = os.path.join(destination, INSTALL_MARKER)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def install_state_payload(
    data_dir: str,
    *,
    migrated: bool = False,
    target: str = "",
) -> dict[str, Any]:
    """Inno 安装标记的内容（``finalize`` 子命令用）。"""
    destination = target or source_root()
    return {
        "installer": "inno",
        "app": "ATRI-X",
        "version": __version__,
        "target": destination,
        "uninstaller": os.path.join(destination, "unins000.exe"),
        "dataDir": data_dir,
        "dataMigrated": bool(migrated),
        "shortcuts": [
            os.path.join(
                (os.environ.get("APPDATA") or "").strip(),
                "Microsoft",
                "Windows",
                "Start Menu",
                "Programs",
                "ATRI-X",
                "ATRI-X.lnk",
            ),
            os.path.join(
                (os.environ.get("USERPROFILE") or "").strip(), "Desktop", "ATRI-X.lnk"
            ),
        ],
        "installedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_install_state(
    data_dir: str,
    *,
    migrated: bool = False,
    target: str = "",
) -> dict[str, Any]:
    """写 Inno 安装标记 + ``data-dir.txt``（安装器 ``ssPostInstall`` 调 finalize）。

    返回写出的标记内容；``data_dir`` 为空或不是绝对路径时抛 ``ValueError``
    （安装器不传相对路径，能到这一步说明参数坏了）。
    """
    directory = str(data_dir or "").strip()
    if not directory or not os.path.isabs(directory):
        raise ValueError(f"data-dir 必须是绝对路径：{data_dir!r}")
    destination = str(target or "").strip() or source_root()
    payload = install_state_payload(directory, migrated=migrated, target=destination)
    write_marker(destination, payload)
    with open(
        os.path.join(destination, DATA_FLAG_NAME), "w", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(directory + "\n")
    return payload


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
