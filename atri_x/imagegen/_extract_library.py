"""从上游机械抽取提示词库 ``prompts/library.json``。

对应 Go：``internal/imagegen/prompts/library.json``（``library.go`` 用
``//go:embed`` 内嵌同一文件）。上游那份 171KB 的 JSON 是数据文件，不是代码：
本脚本把它**逐字节**复制到 ``atri_x/imagegen/prompts/library.json``（相对路径
与上游一致），并提供 ``--check`` 做漂移比对，避免手工抄写。

用法::

    python -m atri_x.imagegen._extract_library                    # 抽取/更新
    python -m atri_x.imagegen._extract_library --check            # 只比对
    python -m atri_x.imagegen._extract_library --upstream <路径> --target <路径>

``--check`` 的退出码：``0`` 一致、``1`` 漂移、``2`` 上游不可用（未 checkout
上游时无法判定，不是"一致"）。

> 说明：按任务约定脚本落在包内（``scripts/`` 不在本次写权限范围内），若 Lead
> 决定统一收纳构建脚本，可整体移动到 ``scripts/extract_imagegen_library.py``，
> 本模块的 :func:`main` 不含包内相对导入，可直接搬。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "CHECK_DRIFT",
    "CHECK_OK",
    "CHECK_UNUSABLE",
    "LibraryCheck",
    "check",
    "default_paths",
    "extract",
    "main",
]

CHECK_OK = 0
CHECK_DRIFT = 1
CHECK_UNUSABLE = 2

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_PACKAGE_DIR))

#: 上游默认位置（与本仓库同级的 Go 仓库目录 ``ATRI-X``；可用 ``--upstream`` 覆盖）。
DEFAULT_UPSTREAM_REPO = os.path.join(os.path.dirname(_REPO_ROOT), "ATRI-X")
#: 库文件在包内的相对路径（与 Go 的 ``internal/imagegen/`` 内布局一致）。
LIBRARY_RELATIVE = os.path.join("prompts", "library.json")
#: 逐条比对的顶层集合（``keywords`` / ``keyword_terms`` 是映射，单独比键数）。
_LIST_KEYS = ("templates", "entries", "negatives", "presets", "tips", "vocabulary")
_MAP_KEYS = ("keywords", "keyword_terms")


@dataclass
class LibraryCheck:
    """``--check`` 的结果。对应"抽取是否漂移"这一个问题。"""

    target: str
    upstream: str
    upstream_available: bool = False
    identical: bool = False
    target_sha256: str = ""
    upstream_sha256: str = ""
    target_size: int = 0
    upstream_size: int = 0
    target_counts: dict[str, int] = field(default_factory=dict)
    upstream_counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.upstream_available and self.identical and not self.problems

    @property
    def exit_code(self) -> int:
        if not self.upstream_available:
            return CHECK_UNUSABLE
        return CHECK_OK if self.ok else CHECK_DRIFT

    def describe(self) -> str:
        lines = [
            f"target   : {self.target} ({self.target_size} bytes, "
            f"sha256={self.target_sha256[:16] or '-'})",
            f"upstream : {self.upstream} ({self.upstream_size} bytes, "
            f"sha256={self.upstream_sha256[:16] or '-'})",
        ]
        if not self.upstream_available:
            lines.append("判定：上游不可用，无法比对（不能算作「一致」）。")
            return "\n".join(lines)
        for name in _LIST_KEYS:
            lines.append(
                "%-12s target=%s upstream=%s"
                % (name, self.target_counts.get(name, 0), self.upstream_counts.get(name, 0))
            )
        for name in _MAP_KEYS:
            lines.append(
                "%-12s target=%s upstream=%s"
                % (name, self.target_counts.get(name, 0), self.upstream_counts.get(name, 0))
            )
        if self.problems:
            lines.append("漂移：")
            lines.extend("  - " + item for item in self.problems)
        else:
            lines.append("判定：与上游一致。")
        return "\n".join(lines)


def default_paths() -> tuple[str, str]:
    """返回 ``(upstream, target)`` 默认路径。"""
    upstream = os.path.join(DEFAULT_UPSTREAM_REPO, "internal", "imagegen", LIBRARY_RELATIVE)
    target = os.path.join(_PACKAGE_DIR, LIBRARY_RELATIVE)
    return upstream, target


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(payload: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(payload, dict):
        return out
    for name in _LIST_KEYS:
        value = payload.get(name)
        out[name] = len(value) if isinstance(value, list) else 0
    for name in _MAP_KEYS:
        value = payload.get(name)
        out[name] = len(value) if isinstance(value, dict) else 0
    return out


def extract(upstream: str | None = None, target: str | None = None) -> str:
    """把上游库文件逐字节复制到目标路径，返回目标路径。"""
    default_upstream, default_target = default_paths()
    upstream = upstream or default_upstream
    target = target or default_target
    if not os.path.isfile(upstream):
        raise FileNotFoundError(f"上游提示词库不存在：{upstream}")
    # 先解析一次：上游文件损坏时不要把它复制进来（embed 失败等价于启动即失败）。
    with open(upstream, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not _counts(payload).get("entries"):
        raise ValueError(f"上游提示词库 entries 为空：{upstream}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(upstream, target)
    return target


def check(upstream: str | None = None, target: str | None = None) -> LibraryCheck:
    """比对目标与上游（字节哈希 + 顶层条目数）。"""
    default_upstream, default_target = default_paths()
    upstream = upstream or default_upstream
    target = target or default_target
    report = LibraryCheck(target=target, upstream=upstream)
    if not os.path.isfile(target):
        report.problems.append(f"目标库文件缺失：{target}")
        return report
    report.target_size = os.path.getsize(target)
    report.target_sha256 = _sha256(target)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            report.target_counts = _counts(json.load(handle))
    except (OSError, ValueError) as exc:
        report.problems.append(f"目标库文件无法解析：{exc}")
        return report
    if not os.path.isfile(upstream):
        return report
    report.upstream_available = True
    report.upstream_size = os.path.getsize(upstream)
    report.upstream_sha256 = _sha256(upstream)
    with open(upstream, "r", encoding="utf-8") as handle:
        report.upstream_counts = _counts(json.load(handle))
    if report.target_sha256 != report.upstream_sha256:
        report.problems.append("字节内容与上游不一致（sha256 不同）")
    for name in _LIST_KEYS + _MAP_KEYS:
        if report.target_counts.get(name, 0) != report.upstream_counts.get(name, 0):
            report.problems.append(
                f"{name} 条目数不一致：target={report.target_counts.get(name, 0)} "
                f"upstream={report.upstream_counts.get(name, 0)}"
            )
    report.identical = not report.problems
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。返回进程退出码。"""
    default_upstream, default_target = default_paths()
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.imagegen._extract_library",
        description="从上游 ATRI-X 机械抽取 imagegen 提示词库（默认逐字节复制）。",
    )
    parser.add_argument("--upstream", default=default_upstream, help="上游 library.json 路径")
    parser.add_argument("--target", default=default_target, help="本仓库 library.json 路径")
    parser.add_argument("--check", action="store_true", help="只比对不写入，漂移时退出码为 1")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check:
        report = check(args.upstream, args.target)
        print(report.describe())
        return report.exit_code
    target = extract(args.upstream, args.target)
    print(f"已抽取：{args.upstream} -> {target} ({os.path.getsize(target)} bytes)")
    return CHECK_OK


if __name__ == "__main__":  # pragma: no cover - 手工调用
    sys.exit(main())
