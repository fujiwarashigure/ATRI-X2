"""可移植的运行时资源快照。

对应 Go：``internal/sysinfo/sysinfo.go`` + ``disk_linux.go`` / ``disk_other.go``
/ ``rss_linux.go`` / ``rss_other.go``。

平台分支用 :data:`sys.platform` 判断，不留下无法 import 的模块：

- ``disk_usage``：Linux 用 ``os.statvfs``（与 Go 的 ``syscall.Statfs`` 等价）；
  其它平台走 ``shutil.disk_usage`` —— 这是相对 Go ``disk_other.go``（直接返回
  "不支持"错误）的**有意增强**，Python 标准库本来就能拿到这个数据。
- ``process_rss``：Linux 读 ``/proc/self/statm``；其它平台返回 0（与 Go 一致）。

Go 的 ``HeapAllocBytes`` / ``HeapSysBytes`` / ``Goroutines`` 是 Go runtime 的
概念，Python 侧映射为 tracemalloc 当前分配量 / 进程峰值 RSS / 线程数。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import tracemalloc
from dataclasses import dataclass

__all__ = [
    "Snapshot",
    "collect",
    "directory_size",
    "disk_usage",
    "format_bytes",
    "format_uint_bytes",
    "process_rss",
]

_IS_LINUX = sys.platform.startswith("linux")
_UNITS = ("KB", "MB", "GB", "TB", "PB")


@dataclass
class Snapshot:
    """一次资源快照。对应 Go ``sysinfo.Snapshot``。"""

    dir_bytes: int = 0
    disk_total_bytes: int = 0
    disk_free_bytes: int = 0
    disk_err: str = ""
    heap_alloc_bytes: int = 0
    heap_sys_bytes: int = 0
    rss_bytes: int = 0
    goroutines: int = 0


def collect(root: str) -> Snapshot:
    """采集目录大小、磁盘用量与进程内存。"""
    snapshot = Snapshot()
    snapshot.dir_bytes = directory_size(root)
    try:
        total, free = disk_usage(root)
        snapshot.disk_total_bytes = total
        snapshot.disk_free_bytes = free
    except OSError as exc:
        snapshot.disk_err = str(exc)
    if tracemalloc.is_tracing():
        snapshot.heap_alloc_bytes = tracemalloc.get_traced_memory()[0]
    snapshot.heap_sys_bytes = _peak_rss_bytes()
    # Go 的 runtime.NumGoroutine()：Python 侧最接近的并发规模指标是线程数。
    snapshot.goroutines = threading.active_count()
    snapshot.rss_bytes = process_rss()
    return snapshot


def directory_size(root: str) -> int:
    """累加 ``root`` 下普通文件的大小；不存在或无权访问的条目被跳过。"""
    root = (root or "").strip()
    if not root:
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _err: None):
        for name in filenames:
            try:
                info = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += info.st_size
    return total


def disk_usage(path: str) -> tuple[int, int]:
    """返回 ``(总字节, 可用字节)``。

    对应 Go ``sysinfo.DiskUsage``；失败时抛 ``OSError``（Go 返回 error）。
    """
    target = (path or "").strip() or "."
    if _IS_LINUX:
        stat = os.statvfs(target)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        return total, free
    usage = shutil.disk_usage(target)
    return usage.total, usage.free


def process_rss() -> int:
    """当前进程常驻内存字节数（非 Linux 返回 0，与 Go ``rss_other.go`` 一致）。"""
    if not _IS_LINUX:
        return 0
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            fields = handle.read().split()
    except OSError:
        return 0
    if len(fields) < 2:
        return 0
    try:
        pages = int(fields[1])
    except ValueError:
        return 0
    return pages * os.sysconf("SC_PAGE_SIZE")


def _peak_rss_bytes() -> int:
    """进程峰值 RSS（对应 Go 的 ``MemStats.Sys`` 量级，仅作展示）。"""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
    except Exception:  # pragma: no cover - 平台无 resource 模块
        return 0
    # Linux 的 ru_maxrss 单位是 KiB，macOS 是字节。
    if _IS_LINUX:
        return int(usage.ru_maxrss) * 1024
    return int(usage.ru_maxrss)


def format_bytes(value: int) -> str:
    """人类可读的字节数（与 Go ``FormatBytes`` 逐字一致）。"""
    unit = 1024
    if value < unit:
        return f"{value} B"
    size = float(value)
    index = -1
    while size >= unit and index < len(_UNITS) - 1:
        size /= unit
        index += 1
    if size >= 100:
        return f"{size:.0f} {_UNITS[index]}"
    return f"{size:.1f} {_UNITS[index]}"


def format_uint_bytes(value: int) -> str:
    """无符号字节数的可读渲染。"""
    return format_bytes(int(value))
