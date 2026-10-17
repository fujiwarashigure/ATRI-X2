"""原子写文件。

对应 Go：``internal/utils/fileops/atomic_write.go`` +
``atomic_write_unix.go`` + ``atomic_write_windows.go``。

步骤与上游一致：同目录临时文件 → 写入 → chmod → **fsync** → close →
原子替换（POSIX ``rename`` + 目录 fsync；Windows ``MoveFileExW`` 带
``REPLACE_EXISTING | WRITE_THROUGH``）。符号链接目标直接原地写，避免把链接
替换成普通文件。
"""

from __future__ import annotations

import os
import tempfile

__all__ = ["atomic_write_file", "replace_file_atomic"]

_IS_WINDOWS = os.name == "nt"
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def atomic_write_file(path: str, data: bytes, mode: int = 0o644) -> None:
    """原子地把 ``data`` 写进 ``path``（``mode`` 为 POSIX 权限位）。"""
    if os.path.islink(path):
        # 符号链接：替换目录项会破坏链接，改为跟随链接写。
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            os.chmod(path, mode)
        except OSError:  # pragma: no cover - 权限不可改时与 Go 一样继续
            pass
        return
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    handle = None
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="." + base + ".tmp-", dir=directory)
        handle = os.fdopen(fd, "wb")
        handle.write(data)
        os.chmod(tmp_path, mode)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        replace_file_atomic(tmp_path, path)
        tmp_path = ""
    except OSError as exc:
        raise OSError(f"atomic write {path}: {exc}") from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:  # pragma: no cover
                pass


def replace_file_atomic(tmp_path: str, target_path: str) -> None:
    """原子替换目标文件。对应 Go ``replaceFileAtomic``。"""
    if _IS_WINDOWS:
        if _move_file_ex(tmp_path, target_path):
            return
        os.replace(tmp_path, target_path)
        return
    os.replace(tmp_path, target_path)
    _sync_directory(os.path.dirname(target_path) or ".")


def _move_file_ex(tmp_path: str, target_path: str) -> bool:
    """Windows 上用 ``MoveFileExW`` + ``WRITE_THROUGH``；不可用时返回 False。"""
    try:
        import ctypes
    except ImportError:  # pragma: no cover - 非 CPython 环境
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover
        return False
    move = kernel32.MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int
    if move(str(tmp_path), str(target_path), _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
        return True
    err = ctypes.get_last_error()
    raise OSError(err, f"replace file: MoveFileExW failed ({err})")


def _sync_directory(directory: str) -> None:
    """POSIX：把目录项落盘（Windows 不支持打开目录）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - 目录不可打开时与 Go 一样忽略
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)
