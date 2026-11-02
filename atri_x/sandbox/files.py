"""租户文件空间：路径约束、导入导出与清理。

修改清单 §5 的原则是「**宿主秘密从未进入盒子**，而不是进入以后靠命令黑名单保护」，
因此这里只做两件事：

1. **路径约束**：文件类工具拿到的每个路径都必须落在租户工作区内；拒绝绝对路径、
   ``~``、盘符、UNC、``..`` 逃逸、符号链接 / Windows reparse point、FIFO / socket /
   设备等特殊文件。检查与打开之间不做「先 stat 后 open」的赌博：POSIX 上用
   ``O_NOFOLLOW``，Windows 上用 ``lstat`` 的 reparse 标志 + 只读/常规文件检查。
2. **受控导入导出**：宿主侧只允许「复制进来」「复制出去」两种方向，且都有字节上限；
   产物导出后由调用方决定发回哪个会话（默认发回发起任务的用户）。

宿主文件访问（``allow_host_files=True``）是**独立能力**，默认关闭；打开后也只允许
绝对路径的常规文件/目录，不做通配。
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import Any

from atri_x.sandbox.errors import SandboxDeniedError

__all__ = [
    "check_regular_file",
    "disk_usage_bytes",
    "ensure_tenant_dirs",
    "export_file",
    "import_file",
    "prune_tree",
    "resolve_workspace_path",
    "within",
]

_MIB = 1024 * 1024
_WINDOWS_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def within(root: str, path: str) -> bool:
    """``path`` 是否在 ``root`` 内（用 ``realpath`` 比较，跨盘符返回 False）。"""
    if not root or not path:
        return False
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
        return os.path.commonpath([real_root, real_path]) == real_root
    except (OSError, ValueError):
        return False


def resolve_workspace_path(
    workspace: str,
    raw_path: str,
    *,
    allow_create: bool = False,
    allow_directory: bool = False,
    allow_host_files: bool = False,
) -> str:
    """把工具传入的路径解析成租户工作区内的绝对路径。

    越界、特殊文件、指向工作区外的符号链接都抛
    :class:`atri_x.sandbox.errors.SandboxDeniedError`（不回灌宿主路径）。
    """
    raw = (raw_path or "").strip()
    if raw == "":
        raise SandboxDeniedError("path is required")
    workspace = os.path.abspath(os.path.normpath(workspace or ""))
    if not workspace:
        raise SandboxDeniedError("沙箱工作区未初始化")
    if _absolute_like(raw):
        if not allow_host_files:
            raise SandboxDeniedError(
                "沙箱文件空间只接受相对路径（基于工作区）；访问宿主文件需要单独授权能力",
                detail="[sandbox].allow_host_files=false",
            )
        target = os.path.abspath(os.path.normpath(raw))
        _assert_safe_path(target, allow_create=allow_create, allow_directory=allow_directory)
        return target
    clean = _normalize_relative(raw)
    target = os.path.abspath(os.path.normpath(os.path.join(workspace, clean)))
    if not within(workspace, target):
        raise SandboxDeniedError("路径越界：不允许离开沙箱工作区")
    _assert_no_reparse(workspace, target)
    _assert_safe_path(target, allow_create=allow_create, allow_directory=allow_directory)
    return target


def check_regular_file(path: str, *, max_bytes: int = 0) -> os.stat_result:
    """确认是一个可读的常规文件（拒绝特殊文件/符号链接），可选检查大小上限。"""
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SandboxDeniedError(f"路径不可用：{exc}") from exc
    if _is_link_or_reparse(info):
        raise SandboxDeniedError("沙箱不跟随符号链接 / reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise SandboxDeniedError("只允许常规文件（拒绝目录、FIFO、socket、设备）")
    if max_bytes and info.st_size > max_bytes:
        raise SandboxDeniedError(
            f"文件超过大小上限：{info.st_size} > {max_bytes}",
            detail=f"{max_bytes / _MIB:.0f} MiB",
        )
    return info


def ensure_tenant_dirs(paths: Any) -> None:
    """创建租户工作区/私有 home/临时目录（存在则跳过）。"""
    for name in ("root", "workspace", "home", "tmp", "proxy_state", "logs"):
        directory = str(getattr(paths, name, "") or "")
        if directory:
            os.makedirs(directory, exist_ok=True)


def import_file(
    source: str,
    destination: str,
    *,
    max_bytes: int,
    workspace: str = "",
) -> int:
    """把一个宿主文件**复制**进租户空间（不改动源文件），返回复制字节数。

    ``source`` 只能来自控制面（媒体缓存等）；如果给了 ``workspace``，``destination``
    必须落在其中，否则拒绝。
    """
    src = os.path.abspath(os.path.normpath(source))
    dst = os.path.abspath(os.path.normpath(destination))
    info = check_regular_file(src, max_bytes=max_bytes)
    if workspace and not within(workspace, dst):
        raise SandboxDeniedError("导入目标越界：必须位于租户工作区内")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.exists(dst):
        check_regular_file(dst)
    copied = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        target_fd = os.open(dst, flags, 0o600)
        try:
            while True:
                chunk = os.read(source_fd, 65536)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise SandboxDeniedError("导入文件超过大小上限")
                os.write(target_fd, chunk)
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)
    if copied > info.st_size:  # pragma: no cover - 并发截断的防御
        raise SandboxDeniedError("导入文件在复制过程中发生变化")
    return copied


def export_file(path: str, *, max_bytes: int) -> str:
    """校验产物可导出（常规文件且不超上限），返回绝对路径。"""
    target = os.path.abspath(os.path.normpath(path))
    check_regular_file(target, max_bytes=max_bytes)
    return target


def disk_usage_bytes(path: str, *, limit_entries: int = 20000) -> int:
    """递归统计目录占用（字节）；超过 ``limit_entries`` 停止，避免拖慢主循环。"""
    total = 0
    seen = 0
    stack = [path]
    while stack and seen < limit_entries:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                seen += 1
                if seen > limit_entries:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_link_or_reparse(info):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


def prune_tree(path: str) -> int:
    """删除一棵租户目录（重置/清理用），返回删除的文件/目录数量。

    Windows 只读文件会让 ``shutil.rmtree`` 失败，``onerror`` 里补一次 chmod；
    符号链接/reparse point 只删链接本身，不递归进去。
    """
    target = os.path.abspath(os.path.normpath(path or ""))
    if not target or not os.path.exists(target):
        return 0
    if os.path.islink(target):
        os.unlink(target)
        return 1
    removed = 0

    def _on_error(func: Any, failed: str, _info: Any) -> None:
        try:
            os.chmod(failed, 0o700)
            func(failed)
        except OSError:
            raise

    shutil.rmtree(target, onerror=_on_error)
    removed += 1
    return removed


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _absolute_like(value: str) -> bool:
    text = value.replace("\\", "/")
    if text.startswith("~") or text.startswith("$HOME"):
        return True
    if text.startswith("/"):
        return True
    if len(text) >= 2 and text[1] == ":":
        return True
    if text.startswith("//"):  # UNC
        return True
    return os.path.isabs(value)


def _normalize_relative(value: str) -> str:
    text = value.replace("\\", "/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise SandboxDeniedError("路径越界：不允许 .. 逃逸沙箱工作区")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise SandboxDeniedError("路径不能为空或仅指向工作区根")
    return os.path.join(*parts)


def _assert_safe_path(path: str, *, allow_create: bool, allow_directory: bool) -> None:
    """检查路径本身：存在则必须是常规文件（或允许的目录），不存在时必须允许创建。"""
    if not os.path.exists(path):
        if not allow_create:
            raise SandboxDeniedError(f"路径不存在：{path}")
        # 允许创建时向上找到最近的已存在祖先：它必须是目录且不是链接/reparse。
        ancestor = os.path.dirname(path) or "."
        while ancestor and not os.path.exists(ancestor):
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent
        if not ancestor or not os.path.isdir(ancestor):
            raise SandboxDeniedError("父目录不存在")
        info = os.lstat(ancestor)
        if _is_link_or_reparse(info):
            raise SandboxDeniedError("沙箱不跟随符号链接 / reparse point")
        return
    info = os.lstat(path)
    if _is_link_or_reparse(info):
        raise SandboxDeniedError("沙箱不跟随符号链接 / reparse point")
    if stat.S_ISDIR(info.st_mode):
        if not allow_directory:
            raise SandboxDeniedError("path is a directory")
        return
    if not stat.S_ISREG(info.st_mode):
        raise SandboxDeniedError("只允许常规文件（拒绝 FIFO、socket、设备）")


def _assert_no_reparse(root: str, path: str) -> None:
    """从 ``root`` 到 ``path`` 逐段检查，任何一段是链接/reparse point 都拒绝。"""
    root = os.path.abspath(os.path.normpath(root))
    current = root
    rel = os.path.relpath(os.path.abspath(path), root)
    for part in rel.split(os.sep):
        if part in ("", "."):
            continue
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            break
        if _is_link_or_reparse(info):
            raise SandboxDeniedError("沙箱不跟随符号链接 / reparse point")


def _is_link_or_reparse(info: os.stat_result) -> bool:
    """``lstat`` 结果是否符号链接 / Windows reparse point。"""
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE)
