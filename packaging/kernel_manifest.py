"""内置运行时清单（``kernel.json``）的读写与几个小工具。

从 ``assemble_kernel.py`` 拆出来的一层：清单格式是**产物契约**（运行期
``atri_x.sidecar.registry`` 按它解析），单独放一个文件便于核对字段，也让组装脚本留在
600 行以内。这里只有纯函数，不碰 npm、不碰网络。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Mapping

__all__ = [
    "MANIFEST_FORMAT",
    "MANIFEST_NAME",
    "mb",
    "node_version",
    "now",
    "read_json",
    "tree_bytes",
    "write_json",
    "write_manifest",
]

#: 清单文件名（与 ``atri_x.sidecar.registry.RUNTIME_MANIFEST`` 一致）。
MANIFEST_NAME = "kernel.json"

#: 清单格式版本（与 ``atri_x.sidecar.registry.SUPPORTED_FORMAT`` 对应）。
MANIFEST_FORMAT = 1


def write_manifest(
    out: str,
    entry: str,
    dsh_version: str,
    copied: Mapping[str, int],
    prune: tuple[str, ...],
    *,
    profile: str,
    verified: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """写 ``out/kernel.json`` 并返回清单。``verified`` 为核验结果（None 表示跳过核验）。"""
    manifest: dict[str, Any] = {
        "id": "dsh-kernel",
        "kind": "dsh-kernel",
        "format": MANIFEST_FORMAT,
        "profile": profile,
        "dshVersion": dsh_version,
        "node": "node/node.exe",
        "entry": entry,
        "nodeModules": "node_modules",
        "packages": len(copied),
        "bytes": sum(copied.values()),
        "pruned": list(prune),
        "createdAt": now(),
        "nodeVersion": node_version(os.path.join(out, "node", "node.exe")),
    }
    if verified:
        manifest["verified"] = bool(verified.get("ok"))
        manifest["verifiedAt"] = now()
        manifest["verifiedWith"] = "acp-initialize"
        if verified.get("missing"):
            manifest["healed"] = list(verified.get("missing") or [])[:200]
    write_json(os.path.join(out, MANIFEST_NAME), manifest)
    return manifest


def node_version(node_exe: str) -> str:
    """跑 ``node --version``；失败返回空串（核验不因它中断）。"""
    try:
        completed = subprocess.run(
            [node_exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return (completed.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def read_json(path: str) -> dict[str, Any] | None:
    """读 JSON 对象；不存在/非法/不是对象都返回 ``None``。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: str, payload: Mapping[str, Any]) -> None:
    """写 JSON（UTF-8 无 BOM、LF、结尾换行）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def tree_bytes(path: str) -> int:
    """目录内所有文件字节数（读不到的跳过）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def now() -> str:
    """UTC 时间戳（RFC3339，秒级）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mb(value: Any) -> str:
    """字节数 → ``"X.Y MB"`` 文本（日志用）。"""
    try:
        return "%.1f MB" % (int(value) / 1048576.0)
    except (TypeError, ValueError):
        return "0 MB"
