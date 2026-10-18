"""运行时目录发现与清单解析。

「一个运行时」= 一个目录 + 一份 ``<id>.json`` 清单（DSH 内核用 ``kernel.json``）。
清单只保留发行包需要的字段，内容由打包脚本生成，不在运行期修改::

    {
      "id": "dsh-kernel",
      "kind": "dsh-kernel",
      "format": 1,
      "entry": "node_modules/@deepseek-ai/dsh/lib/bin.js",
      "node": "node/node.exe",
      "dshVersion": "0.2.0-rc.2",
      "nodeVersion": "v24.19.0",
      "packages": 384,
      "bytes": 198295728,
      "verified": true
    }

查找顺序：

1. ``ATRIX_RUNTIME_DIR``（显式根目录，本地调试/替换运行时用）；
2. ``<bundle>/runtime``：冻结包里是 PyInstaller 的 ``_MEIPASS``，源码运行时是仓库根；
3. ``<配置目录>/runtime``：用户级覆盖，改运行时不用重新打包。

每个根目录下的每个子目录都会被扫到；带清单的子目录才算「已安装」。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "RUNTIME_DIR_ENV",
    "RUNTIME_MANIFEST",
    "Runtime",
    "bundle_root",
    "config_runtime_dir",
    "discover",
    "find",
    "is_frozen",
    "load_manifest",
    "load_runtime",
    "runtime_dirs",
]

#: 覆盖运行时根目录的环境变量。
RUNTIME_DIR_ENV = "ATRIX_RUNTIME_DIR"

#: 默认清单文件名（DSH 内核用这个名字；其他运行时也复用）。
RUNTIME_MANIFEST = "kernel.json"

#: 清单格式版本；不认识的高版本会被忽略（避免读到未来格式）。
SUPPORTED_FORMAT = 1


@dataclass(frozen=True)
class Runtime:
    """一个已安装的运行时。"""

    id: str
    kind: str
    root: str
    manifest: Mapping[str, Any]

    @property
    def version(self) -> str:
        """运行时版本（DSH 内核是 ``dshVersion``）。"""
        for key in ("version", "dshVersion", "nodeVersion"):
            value = str(self.manifest.get(key) or "").strip()
            if value:
                return value
        return ""

    @property
    def source(self) -> str:
        """这份运行时是从哪个根目录找到的（排障用）。"""
        return os.path.dirname(self.root)

    def path(self, key: str) -> str:
        """把清单里的相对路径变成绝对路径；``key`` 缺失时返回空串。"""
        relative = str(self.manifest.get(key) or "").strip()
        if not relative:
            return ""
        return os.path.normpath(os.path.join(self.root, *relative.replace("\\", "/").split("/")))


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 冻结包里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> str:
    """发行包根目录：冻结后是 ``_MEIPASS``，源码运行时是仓库根。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return os.path.abspath(str(meipass))
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def config_runtime_dir() -> str:
    """用户级运行时目录（``<配置目录>/runtime``）；配置目录拿不到时返回空串。"""
    try:
        from atri_x.config.loader import platform_default_config_path
    except Exception:  # noqa: BLE001 - 配置层不可用时只是少一个搜索位置
        return ""
    try:
        config_path = platform_default_config_path()
    except Exception:  # noqa: BLE001
        return ""
    if not config_path:
        return ""
    return os.path.join(os.path.dirname(os.path.normpath(config_path)), "runtime")


def runtime_dirs() -> list[str]:
    """按优先级返回运行时根目录（只返回真实存在的目录，去重保序）。"""
    candidates: list[str] = []
    explicit = (os.environ.get(RUNTIME_DIR_ENV) or "").strip()
    if explicit:
        candidates.append(os.path.abspath(os.path.expanduser(explicit)))
    candidates.append(os.path.join(bundle_root(), "runtime"))
    user_dir = config_runtime_dir()
    if user_dir:
        candidates.append(user_dir)
    out: list[str] = []
    for candidate in candidates:
        if not os.path.isdir(candidate):
            continue
        if candidate not in out:
            out.append(candidate)
    return out


def discover() -> list[Runtime]:
    """扫描所有运行时根目录，返回带回的清单（同名 id 先到先得）。"""
    found: dict[str, Runtime] = {}
    for base in runtime_dirs():
        for entry in sorted(_safe_listdir(base)):
            directory = os.path.join(base, entry)
            if not os.path.isdir(directory):
                continue
            runtime = load_runtime(directory)
            if runtime is not None and runtime.id not in found:
                found[runtime.id] = runtime
    return list(found.values())


def find(runtime_id: str) -> Runtime | None:
    """按 id 找运行时。"""
    wanted = (runtime_id or "").strip()
    if not wanted:
        return None
    for runtime in discover():
        if runtime.id == wanted:
            return runtime
    return None


def load_manifest(directory: str) -> Mapping[str, Any] | None:
    """读一个运行时目录的清单；不存在/非法/版本不支持时返回 ``None``。"""
    path = os.path.join(directory, RUNTIME_MANIFEST)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        version = int(payload.get("format") or 0)
    except (TypeError, ValueError):
        version = 0
    if version > SUPPORTED_FORMAT:
        return None
    if not str(payload.get("id") or "").strip():
        return None
    return payload


def load_runtime(directory: str) -> Runtime | None:
    """读目录里的运行时；没有合法清单时返回 ``None``。"""
    manifest = load_manifest(directory)
    if manifest is None:
        return None
    runtime_id = str(manifest.get("id") or "").strip()
    kind = str(manifest.get("kind") or runtime_id).strip()
    return Runtime(id=runtime_id, kind=kind, root=directory, manifest=manifest)


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []
