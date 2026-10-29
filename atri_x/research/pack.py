"""随包研究引擎资源（``research_pack/``）的定位与元数据读取。

``research_pack/`` 是仓库里的**数据目录**（不是 Python 包），布局固定：

.. code-block:: text

    research_pack/
      pack.json                     # 引擎版本、文件清单、内容摘要
      settings.json                 # 引擎默认设置（首次安装时落盘，之后尊重用户修改）
      engine/                       # 研究引擎源码（run.py + atrix_research/）
      skills/<skill>/SKILL.md       # 15 个技能文档（含引擎生成的工具契约附录）
      skills/<skill>/manifest.json  # 12 个硬工具的清单（bootstrap 据此渲染 ATRIX_SKILL.toml）

冻结产物里它被 ``packaging/atri-x.spec`` 原样收进 ``_internal/research_pack``；
源码树里它就在仓库根。定位顺序：``sys._MEIPASS`` → exe 同目录 / ``_internal`` →
``atri_x/research`` 的上两级。**找不到时返回 ``None``**，由调用方决定降级方式——
研究助理不可用不应该让主程序起不来。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "PACK_DIR_NAME",
    "PACK_META_FILE",
    "PACK_SCHEMA",
    "is_frozen",
    "load_pack_meta",
    "pack_root",
    "repo_root",
]

#: 随包数据目录名（仓库根与冻结包内同名）。
PACK_DIR_NAME = "research_pack"
#: 元数据文件名。
PACK_META_FILE = "pack.json"
#: 元数据 schema 标识。
PACK_SCHEMA = "atrix-research/pack/1"


def is_frozen() -> bool:
    """当前进程是否跑在 PyInstaller 冻结产物里。"""
    return bool(getattr(sys, "frozen", False))


def repo_root() -> Path | None:
    """源码树根（``atri_x/research/pack.py`` 的上两级）；冻结时返回 ``None``。"""
    try:
        root = Path(__file__).resolve().parents[2]
    except (IndexError, OSError):  # pragma: no cover - 理论上不会发生
        return None
    return root if (root / "atri_x").is_dir() else None


def _candidates() -> list[Path]:
    """按优先级列出 ``research_pack`` 的候选位置（去重保序）。"""
    raw: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "") or ""
    if meipass:
        raw.append(Path(meipass) / PACK_DIR_NAME)
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        raw.append(exe_dir / PACK_DIR_NAME)
        raw.append(exe_dir / "_internal" / PACK_DIR_NAME)
    root = repo_root()
    if root is not None:
        raw.append(root / PACK_DIR_NAME)
    seen: set[str] = set()
    out: list[Path] = []
    for path in raw:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def pack_root() -> Path | None:
    """随包资源目录（含 ``pack.json`` 才算数），找不到返回 ``None``。"""
    for path in _candidates():
        if (path / PACK_META_FILE).is_file():
            return path
    return None


def load_pack_meta(root: Path | None = None) -> dict[str, Any] | None:
    """读取 ``pack.json``；文件缺失或损坏时返回 ``None``（不抛异常）。"""
    base = root if root is not None else pack_root()
    if base is None:
        return None
    try:
        data = json.loads((base / PACK_META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != PACK_SCHEMA:
        return None
    return data
