"""用户数据迁移：把旧位置的 ``ATRI-X`` 数据搬到安装时选择的数据目录。

**使用场景**：安装器新增了「数据与配置目录」选项（默认 ``<安装目录>\\data``）。用户
从旧版本升级、或把位置从 ``%APPDATA%\\ATRI-X`` 改到安装目录时，配置、会话库、成员
档案、DSH 状态、技能与日志都需要跟着走；这一步做成显式的命令，安装器在
``ssPostInstall`` 里调用它，用户也可以手动跑::

    atri-x -m atri_x.dist.install migrate --from %APPDATA%\\ATRI-X --to <安装目录>\\data --move

**安全边界**：

- 默认**复制**（``--copy``），只有显式 ``--move`` 才在复制并逐文件核对通过后删除源；
- 源/目标必须是绝对路径且互不包含（避免把安装目录搬进自己的子目录造成递归复制）；
- 目标非空时拒绝（除非 ``--overwrite``），不静默覆盖用户已有数据；
- 复制后逐文件核对**存在性与大小**，任一不符就报错且**不删源**；
- ``--dry-run`` 只统计与校验参数，不落盘。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MigrationError", "MigrationReport", "migrate", "tree_stats"]


class MigrationError(RuntimeError):
    """可预期的迁移失败（消息直接展示给用户/写进安装日志）。"""


@dataclass
class MigrationReport:
    """迁移结果（``--json`` 直接序列化它）。"""

    ok: bool
    source: str
    target: str
    mode: str = "copy"
    files: int = 0
    bytes: int = 0
    dry_run: bool = False
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转 dict（``--json`` 输出用）。"""
        return {
            "ok": self.ok,
            "source": self.source,
            "target": self.target,
            "mode": self.mode,
            "files": self.files,
            "bytes": self.bytes,
            "dryRun": self.dry_run,
            "detail": self.detail,
            "warnings": list(self.warnings),
        }


def tree_stats(directory: str) -> tuple[int, int]:
    """统计目录下 ``(文件数, 总字节数)``；目录不存在返回 ``(0, 0)``。"""
    files = 0
    total = 0
    if not directory or not os.path.isdir(directory):
        return 0, 0
    for root, _dirs, names in os.walk(directory):
        for name in names:
            path = os.path.join(root, name)
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
            files += 1
    return files, total


def migrate(
    source: str,
    target: str,
    *,
    move: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> MigrationReport:
    """把 ``source`` 的数据迁移到 ``target``；返回 :class:`MigrationReport`。"""
    origin = _absolute(source, "源目录")
    destination = _absolute(target, "目标目录")
    mode = "move" if move else "copy"
    report = MigrationReport(ok=False, source=origin, target=destination, mode=mode, dry_run=dry_run)
    _reject_overlap(origin, destination)
    if not os.path.isdir(origin):
        raise MigrationError(f"源目录不存在：{origin}")
    files, total = tree_stats(origin)
    if files == 0:
        report.ok = True
        report.detail = "源目录为空，无需迁移"
        return report
    if os.path.isdir(destination) and os.listdir(destination) and not overwrite:
        raise MigrationError(
            f"目标目录已存在且非空：{destination}（确认要覆盖请加 --overwrite）"
        )
    report.files = files
    report.bytes = total
    if dry_run:
        report.ok = True
        report.detail = f"计划{'移动' if move else '复制'} {files} 个文件（{total} 字节）"
        return report

    try:
        os.makedirs(destination, exist_ok=True)
        shutil.copytree(origin, destination, dirs_exist_ok=True, copy_function=shutil.copy2)
    except OSError as exc:
        raise MigrationError(f"复制失败：{origin} → {destination}：{exc}") from exc

    _verify_copy(origin, destination)
    report.ok = True
    report.detail = f"已{'移动' if move else '复制'} {files} 个文件（{total} 字节）"
    if move:
        try:
            shutil.rmtree(origin)
        except OSError as exc:
            report.warnings.append(f"已复制但删除旧目录失败：{origin}：{exc}")
    return report


# ---------------------------------------------------------------------------
# 内部：路径校验 / 复制核对
# ---------------------------------------------------------------------------


def _absolute(value: str, label: str) -> str:
    """规整成绝对路径；空值/相对路径直接报错（安装器只传绝对路径）。"""
    text = str(value or "").strip()
    if not text:
        raise MigrationError(f"{label}为空")
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        raise MigrationError(f"{label}必须是绝对路径：{text}")
    return os.path.normpath(expanded)


def _reject_overlap(source: str, target: str) -> None:
    """源与目标相同、或互相包含时拒绝（复制会递归、移动会把自己删掉）。"""
    left = os.path.normcase(source)
    right = os.path.normcase(target)
    if left == right:
        raise MigrationError(f"源与目标相同：{source}")
    prefix_left = left.rstrip("\\/") + os.sep
    prefix_right = right.rstrip("\\/") + os.sep
    if prefix_left.startswith(prefix_right) or prefix_right.startswith(prefix_left):
        raise MigrationError(f"源目录与目标目录互相包含，拒绝迁移：{source} ⇄ {target}")


def _verify_copy(source: str, target: str) -> None:
    """逐文件核对：目标存在且大小一致；不一致时抛错（调用方据此不删源）。"""
    missing: list[str] = []
    mismatched: list[str] = []
    for root, _dirs, names in os.walk(source):
        relative = os.path.relpath(root, source)
        for name in names:
            source_path = os.path.join(root, name)
            target_path = os.path.join(target, relative, name) if relative != "." else os.path.join(
                target, name
            )
            try:
                source_size = os.path.getsize(source_path)
            except OSError:
                continue
            try:
                target_size = os.path.getsize(target_path)
            except OSError:
                missing.append(target_path)
                continue
            if source_size != target_size:
                mismatched.append(target_path)
    if not missing and not mismatched:
        return
    problems = (missing + mismatched)[:5]
    raise MigrationError(
        f"复制核对失败（缺失 {len(missing)}、大小不符 {len(mismatched)}）："
        + "；".join(problems)
    )
