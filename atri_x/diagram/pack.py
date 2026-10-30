"""随包 draw.io 技能资源（``drawio_pack/``）与内置渲染器的定位。

``drawio_pack/`` 是仓库里的**数据目录**（不是 Python 包），布局固定：

.. code-block:: text

    drawio_pack/
      pack.json                     # 上游版本、文件清单、内容摘要
      tool.json                     # 唯一工具（drawio）的清单源：参数 schema 与参数→开关
      skills/drawio-skill/          # 上游 drawio-skill 3.4.2 的运行文件
        SKILL.md                    #   技能文档（ATRI-X 改写版：动作表替代 python 命令行）
        atrix_cli.py                #   ATRI-X 动作适配器（create/build/validate/export/docs/shapes）
        scripts/ references/ data/ styles/
      examples/                     # 上游回归用例（不进安装目录，只给一致性测试用）
      tests/                        # 上游 247 个回归测试（一致性门禁用）

冻结产物里它被 ``packaging/atri-x.spec`` 原样收进 ``_internal/drawio_pack``；
源码树里它就在仓库根。定位顺序：``sys._MEIPASS`` → exe 同目录 / ``_internal`` →
``atri_x/diagram`` 的上两级。**找不到时返回 ``None``**，由调用方决定降级方式——
图表能力不可用不应该让主程序起不来。

内置渲染器（draw.io 桌面版解包目录）**不进** ``drawio_pack``：它是可选构建材料，
运行时目录名固定 ``drawio_runtime``，源码树从 ``packaging/vendor/drawio`` 找。
查找顺序见 :func:`renderer_exe`。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "BUNDLED_DRAWIO_NAME",
    "PACK_DIR_NAME",
    "PACK_META_FILE",
    "PACK_SCHEMA",
    "RENDERER_DIR_NAME",
    "SKILL_DIR_NAME",
    "SKILL_INSTALL_NAME",
    "TOOL_FILE",
    "TOOL_SCHEMA",
    "is_frozen",
    "load_pack_meta",
    "load_tool_meta",
    "pack_root",
    "renderer_exe",
    "renderer_root",
    "repo_root",
    "skill_source_dir",
]

#: 随包数据目录名（仓库根与冻结包内同名）。
PACK_DIR_NAME = "drawio_pack"
#: 元数据文件名。
PACK_META_FILE = "pack.json"
#: 元数据 schema 标识。
PACK_SCHEMA = "atrix-drawio/pack/1"
#: 工具清单文件名（唯一工具的 schema / 参数 / 开关映射）。
TOOL_FILE = "tool.json"
#: 工具清单 schema 标识。
TOOL_SCHEMA = "atrix-drawio/tool/1"
#: 包内技能目录名（与上游布局一致：上游测试按 ``skills/drawio-skill`` 解析路径）。
SKILL_DIR_NAME = "drawio-skill"
#: 装进配置目录后的技能目录名 / 工具名（与 ``SKILL.md`` 的 ``name`` 一致）。
SKILL_INSTALL_NAME = "drawio_skill"
#: 内置 draw.io 桌面版目录名（冻结产物里在 ``_internal/drawio_runtime``）。
RENDERER_DIR_NAME = "drawio_runtime"
#: draw.io 桌面版可执行文件名（官方解包目录就是这个带点的名字）。
BUNDLED_DRAWIO_NAME = "draw.io.exe"

#: 可执行文件名候选（大小写/点号差异都认）。
_EXE_NAMES = (BUNDLED_DRAWIO_NAME, "drawio.exe", "draw.io", "drawio")


def is_frozen() -> bool:
    """当前进程是否跑在 PyInstaller 冻结产物里。"""
    return bool(getattr(sys, "frozen", False))


def repo_root() -> Path | None:
    """源码树根（``atri_x/diagram/pack.py`` 的上两级）；冻结时返回 ``None``。"""
    try:
        root = Path(__file__).resolve().parents[2]
    except (IndexError, OSError):  # pragma: no cover - 理论上不会发生
        return None
    return root if (root / "atri_x").is_dir() else None


def _candidates() -> list[Path]:
    """按优先级列出 ``drawio_pack`` 的候选位置（去重保序）。"""
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


def _load_json(path: Path, schema: str) -> dict[str, Any] | None:
    """读一个 JSON 元数据文件；缺失、损坏或 schema 不符都返回 ``None``（不抛异常）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != schema:
        return None
    return data


def load_pack_meta(root: Path | None = None) -> dict[str, Any] | None:
    """读取 ``pack.json``；文件缺失或损坏时返回 ``None``。"""
    base = root if root is not None else pack_root()
    if base is None:
        return None
    return _load_json(base / PACK_META_FILE, PACK_SCHEMA)


def skill_source_dir(root: Path | None = None) -> Path | None:
    """包内技能源目录（``drawio_pack/skills/drawio-skill``），不存在返回 ``None``。"""
    base = root if root is not None else pack_root()
    if base is None:
        return None
    candidate = base / "skills" / SKILL_DIR_NAME
    return candidate if (candidate / "SKILL.md").is_file() else None


def load_tool_meta(root: Path | None = None) -> dict[str, Any] | None:
    """读取工具清单 ``tool.json``；文件缺失或损坏时返回 ``None``。"""
    base = root if root is not None else pack_root()
    if base is None:
        return None
    return _load_json(base / TOOL_FILE, TOOL_SCHEMA)


def _renderer_dirs() -> list[Path]:
    """按优先级列出内置渲染器目录候选（去重保序）。"""
    raw: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "") or ""
    if meipass:
        raw.append(Path(meipass) / RENDERER_DIR_NAME)
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        raw.append(exe_dir / RENDERER_DIR_NAME)
        raw.append(exe_dir / "_internal" / RENDERER_DIR_NAME)
    root = repo_root()
    if root is not None:
        raw.append(root / "packaging" / "vendor" / "drawio")
    seen: set[str] = set()
    out: list[Path] = []
    for path in raw:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _exe_in(path: Path) -> str:
    """目录里找 draw.io 可执行文件；找不到返回空串。"""
    if path.is_file():
        return str(path)
    if not path.is_dir():
        return ""
    for name in _EXE_NAMES:
        candidate = path / name
        if candidate.is_file():
            return str(candidate)
    return ""


def _common_windows_dirs() -> list[Path]:
    """Windows 上 draw.io 的常见安装目录（便携版没有内置渲染器时的兜底）。"""
    if os.name != "nt":
        return []
    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
    ]
    out: list[Path] = []
    for base in roots:
        base = (base or "").strip()
        if not base:
            continue
        out.append(Path(base) / "Programs" / "draw.io")
        out.append(Path(base) / "draw.io")
    return out


def renderer_exe() -> str:
    """内置/本机 draw.io 桌面版可执行文件的绝对路径；找不到返回空串。

    查找顺序（先内置、后兜底）：

    1. 环境变量 ``ATRIX_DRAWIO``（可指向 exe 或解包目录；排障/源码树开发用）；
    2. 冻结产物里的 ``drawio_runtime/``（``_MEIPASS`` → exe 同目录 → ``_internal``）；
    3. 源码树的 ``packaging/vendor/drawio/``（构建材料，不入库）；
    4. ``PATH`` 上的 ``draw.io`` / ``drawio``（本机装过桌面版时）；
    5. Windows 常见安装目录。
    """
    override = (os.environ.get("ATRIX_DRAWIO") or "").strip()
    if override:
        found = _exe_in(Path(override))
        if found:
            return found
    for directory in _renderer_dirs():
        found = _exe_in(directory)
        if found:
            return found
    for name in ("draw.io", "drawio", "draw.io.exe", "drawio.exe"):
        found = shutil.which(name)
        if found:
            return found
    for directory in _common_windows_dirs():
        found = _exe_in(directory)
        if found:
            return found
    return ""


def renderer_root() -> str:
    """渲染器所在目录（没有则返回空串）。"""
    exe = renderer_exe()
    return str(Path(exe).resolve().parent) if exe else ""
