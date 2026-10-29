"""可移植的运行时资源快照。

对应 Go：``internal/sysinfo/sysinfo.go`` + ``disk_linux.go`` / ``disk_other.go``
/ ``rss_linux.go`` / ``rss_other.go``。

平台分支用 :data:`sys.platform` 判断，不留下无法 import 的模块：

- ``disk_usage``：Linux 用 ``os.statvfs``（与 Go 的 ``syscall.Statfs`` 等价）；
  其它平台走 ``shutil.disk_usage`` —— 这是相对 Go ``disk_other.go``（直接返回
  "不支持"错误）的**有意增强**，Python 标准库本来就能拿到这个数据。
- ``process_rss``：Linux 读 ``/proc/self/statm``；Windows 走 kernel32 的
  ``K32GetProcessMemoryInfo``（1.1.4 补齐，此前恒返回 0）；其它平台返回 0。

Go 的 ``HeapAllocBytes`` / ``HeapSysBytes`` / ``Goroutines`` 是 Go runtime 的
概念，Python 侧映射为 tracemalloc 当前分配量 / 进程峰值 RSS / 线程数。

1.1.4 新增的 :func:`collect_resources` 是**并行**的一条采集路径（``collect`` 原样
保留，日报 / ``/metrics`` / diskguard 的语义不受影响）：它同时采进程级与系统级
指标，并带一个可调采样窗口用于计算 CPU 占用率。设计约束见
``docs/dev/adr`` 与 ``devdocs/code-map.md``：

- **零第三方依赖**：Windows 侧只用 ``ctypes`` 调 kernel32 / psapi，Linux 侧只读
  ``/proc``，不引 psutil；
- **可注入**：``platform_impl`` 允许测试塞入假采集器，避免用例依赖真实 Windows
  API 或真实计时；
- **失败降级**：任何一项采集失败只写进 ``errors``，不抛异常、不整条失败。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ProcessResources",
    "ProcessSample",
    "ResourceSnapshot",
    "Snapshot",
    "SystemResources",
    "SystemSample",
    "collect",
    "collect_resources",
    "directory_size",
    "disk_usage",
    "format_bytes",
    "format_resources",
    "format_uint_bytes",
    "format_uptime",
    "process_rss",
    "resources_to_dict",
]

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")
_UNITS = ("KB", "MB", "GB", "TB", "PB")

#: CPU 采样窗口的默认值（秒）与允许区间（秒）。下限避免除以极小的 Δt 放大噪声。
DEFAULT_SAMPLE_INTERVAL = 0.5
MIN_SAMPLE_INTERVAL = 0.05
MAX_SAMPLE_INTERVAL = 2.0


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


# --- 1.1.4 资源采集（进程 + 系统 + 磁盘） ---------------------------------


@dataclass
class ProcessSample:
    """进程级**原始计数器**的一次读数。

    计数器是单调的：``cpu_*_seconds`` 是进程启动以来的累计 CPU 时间，
    ``io_*_bytes`` 是累计 I/O 字节，因此 CPU 占用率必须由两次采样求差得出。
    """

    rss_bytes: int = 0
    peak_rss_bytes: int = 0
    private_bytes: int = 0
    cpu_user_seconds: float = 0.0
    cpu_sys_seconds: float = 0.0
    threads: int = 0
    handles: int = 0
    io_read_bytes: int = 0
    io_write_bytes: int = 0


@dataclass
class SystemSample:
    """系统级**原始计数器**的一次读数。"""

    #: 所有 CPU 时间之和（忙 + idle），单位秒。
    cpu_total_seconds: float = 0.0
    #: 其中 idle 的时间，单位秒。
    cpu_idle_seconds: float = 0.0
    mem_total_bytes: int = 0
    mem_avail_bytes: int = 0
    uptime_seconds: int = 0


@dataclass
class ProcessResources:
    """一次采集得到的进程级资源（已换算成可读数值）。"""

    rss_bytes: int = 0
    peak_rss_bytes: int = 0
    private_bytes: int = 0
    #: 单核基准：多线程忙时可超过 100%。
    cpu_percent: float = 0.0
    cpu_user_seconds: float = 0.0
    cpu_sys_seconds: float = 0.0
    threads: int = 0
    handles: int = 0
    io_read_bytes: int = 0
    io_write_bytes: int = 0


@dataclass
class SystemResources:
    """一次采集得到的系统级资源。"""

    cpu_percent: float = 0.0
    cpu_count: int = 0
    mem_total_bytes: int = 0
    mem_avail_bytes: int = 0
    mem_percent: float = 0.0
    uptime_seconds: int = 0


@dataclass
class ResourceSnapshot:
    """:func:`collect_resources` 的结果。

    ``disks`` 是 ``(路径, 总字节, 可用字节)`` 三元组列表，顺序与入参一致；
    采集失败的路径不出现在这里，原因写进 ``errors``。
    """

    process: ProcessResources = field(default_factory=ProcessResources)
    system: SystemResources = field(default_factory=SystemResources)
    disks: list[tuple[str, int, int]] = field(default_factory=list)
    dir_bytes: int = 0
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL
    errors: list[str] = field(default_factory=list)


class PlatformCollector(Protocol):
    """平台采集器协议：只提供"读一次原始计数器"，换算逻辑留在本模块。

    测试用假实现只需实现这两个方法，就能完全绕开真实 API 与真实计时。
    """

    def sample_process(self) -> ProcessSample: ...

    def sample_system(self) -> SystemSample: ...


def collect_resources(
    paths: Any = None,
    *,
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
    include_dir_size: bool = False,
    platform_impl: PlatformCollector | None = None,
) -> ResourceSnapshot:
    """采集进程 / 系统 / 磁盘资源。

    参数
    ----
    paths:
        要统计磁盘用量的路径（``str`` 或可迭代对象）。空表示不统计磁盘。
    sample_interval:
        CPU 占用率的采样窗口（秒），会被夹到
        ``[MIN_SAMPLE_INTERVAL, MAX_SAMPLE_INTERVAL]``。
    include_dir_size:
        是否递归统计 ``paths`` 的目录大小（``os.walk`` 很慢，默认关）。
    platform_impl:
        注入的采集器；``None`` 时按当前平台自动选择。

    **这是同步函数**：内部含 ``time.sleep``，调用方必须丢进线程
    （工具侧用 ``asyncio.to_thread``），否则会阻塞事件循环。
    """
    interval = _clamp_interval(sample_interval)
    snapshot = ResourceSnapshot(sample_interval=interval)
    collector = platform_impl if platform_impl is not None else _default_collector()
    targets = _normalize_paths(paths)

    first_process: ProcessSample | None = None
    first_system: SystemSample | None = None
    started = time.monotonic()
    try:
        first_process = collector.sample_process()
    except Exception as exc:  # noqa: BLE001 - 采集失败只降级，不炸调用方
        snapshot.errors.append(f"进程采样失败：{exc}")
    try:
        first_system = collector.sample_system()
    except Exception as exc:  # noqa: BLE001
        snapshot.errors.append(f"系统采样失败：{exc}")

    # 采样窗口：要拿到 CPU 占用率必须有两个时间点的计数器。
    time.sleep(interval)
    elapsed = max(time.monotonic() - started, MIN_SAMPLE_INTERVAL)

    second_process: ProcessSample | None = None
    second_system: SystemSample | None = None
    try:
        second_process = collector.sample_process()
    except Exception as exc:  # noqa: BLE001
        snapshot.errors.append(f"进程二次采样失败：{exc}")
    try:
        second_system = collector.sample_system()
    except Exception as exc:  # noqa: BLE001
        snapshot.errors.append(f"系统二次采样失败：{exc}")

    if first_process is not None and second_process is not None:
        snapshot.process = _build_process_resources(
            first_process, second_process, elapsed
        )
    elif second_process is not None:
        # 只能采到一次：内存类指标仍然有价值，CPU% 留 0。
        snapshot.process = _build_process_resources(None, second_process, 0.0)
    elif first_process is not None:
        snapshot.process = _build_process_resources(None, first_process, 0.0)

    if first_system is not None and second_system is not None:
        snapshot.system = _build_system_resources(first_system, second_system)
    elif second_system is not None:
        snapshot.system = _build_system_resources(None, second_system)
    elif first_system is not None:
        snapshot.system = _build_system_resources(None, first_system)

    for target in targets:
        try:
            total, free = disk_usage(target)
        except (OSError, ValueError) as exc:
            snapshot.errors.append(f"磁盘用量采集失败（{target}）：{exc}")
            continue
        snapshot.disks.append((target, total, free))

    if include_dir_size:
        for target in targets:
            try:
                snapshot.dir_bytes += directory_size(target)
            except Exception as exc:  # noqa: BLE001 - os.walk 的 onerror 已吞掉多数错误
                snapshot.errors.append(f"目录大小统计失败（{target}）：{exc}")

    return snapshot


def resources_to_dict(snapshot: ResourceSnapshot) -> dict[str, Any]:
    """把快照渲染成 JSON 友好的 dict（工具结果的 ``data`` 字段用）。"""
    return {
        "sample_interval_seconds": round(snapshot.sample_interval, 3),
        "process": {
            "rss_bytes": snapshot.process.rss_bytes,
            "peak_rss_bytes": snapshot.process.peak_rss_bytes,
            "private_bytes": snapshot.process.private_bytes,
            "cpu_percent": round(snapshot.process.cpu_percent, 2),
            "cpu_user_seconds": round(snapshot.process.cpu_user_seconds, 3),
            "cpu_sys_seconds": round(snapshot.process.cpu_sys_seconds, 3),
            "threads": snapshot.process.threads,
            "handles": snapshot.process.handles,
            "io_read_bytes": snapshot.process.io_read_bytes,
            "io_write_bytes": snapshot.process.io_write_bytes,
        },
        "system": {
            "cpu_percent": round(snapshot.system.cpu_percent, 2),
            "cpu_count": snapshot.system.cpu_count,
            "mem_total_bytes": snapshot.system.mem_total_bytes,
            "mem_avail_bytes": snapshot.system.mem_avail_bytes,
            "mem_percent": round(snapshot.system.mem_percent, 2),
            "uptime_seconds": snapshot.system.uptime_seconds,
        },
        "disks": [
            {"path": path, "total_bytes": total, "free_bytes": free}
            for path, total, free in snapshot.disks
        ],
        "dir_bytes": snapshot.dir_bytes,
        "errors": list(snapshot.errors),
    }


def _clamp_interval(value: float) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_INTERVAL
    if interval != interval:  # NaN
        return DEFAULT_SAMPLE_INTERVAL
    return min(max(interval, MIN_SAMPLE_INTERVAL), MAX_SAMPLE_INTERVAL)


def _normalize_paths(paths: Any) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, (str, bytes, os.PathLike)):
        candidates: list[Any] = [paths]
    else:
        candidates = list(paths)
    out: list[str] = []
    for candidate in candidates:
        text = os.fspath(candidate) if isinstance(candidate, os.PathLike) else str(candidate)
        text = text.strip()
        if text and text not in out:
            out.append(text)
    return out


def _build_process_resources(
    first: ProcessSample | None, second: ProcessSample, elapsed: float
) -> ProcessResources:
    process = ProcessResources(
        rss_bytes=second.rss_bytes,
        peak_rss_bytes=second.peak_rss_bytes or second.rss_bytes,
        private_bytes=second.private_bytes,
        threads=second.threads,
        handles=second.handles,
        io_read_bytes=second.io_read_bytes,
        io_write_bytes=second.io_write_bytes,
    )
    if first is None:
        process.cpu_user_seconds = second.cpu_user_seconds
        process.cpu_sys_seconds = second.cpu_sys_seconds
        return process
    delta_user = max(second.cpu_user_seconds - first.cpu_user_seconds, 0.0)
    delta_sys = max(second.cpu_sys_seconds - first.cpu_sys_seconds, 0.0)
    process.cpu_user_seconds = round(delta_user, 3)
    process.cpu_sys_seconds = round(delta_sys, 3)
    # 单核基准：多线程忙时可以超过 100%，因此这里不夹到 100。
    process.cpu_percent = _ratio_percent(delta_user + delta_sys, elapsed)
    return process


def _build_system_resources(
    first: SystemSample | None, second: SystemSample
) -> SystemResources:
    resources = SystemResources(
        cpu_count=os.cpu_count() or 0,
        mem_total_bytes=second.mem_total_bytes,
        mem_avail_bytes=second.mem_avail_bytes,
        uptime_seconds=second.uptime_seconds,
    )
    if resources.mem_total_bytes > 0:
        used = resources.mem_total_bytes - resources.mem_avail_bytes
        resources.mem_percent = _ratio_percent(used, resources.mem_total_bytes)
    if first is None:
        return resources
    delta_total = max(second.cpu_total_seconds - first.cpu_total_seconds, 0.0)
    delta_idle = max(second.cpu_idle_seconds - first.cpu_idle_seconds, 0.0)
    resources.cpu_percent = _ratio_percent(delta_total - delta_idle, delta_total)
    return resources


def _ratio_percent(part: float, whole: float) -> float:
    """``part / whole`` 的百分数；``whole <= 0`` 时返回 0。

    不做 0–100 夹取：进程 CPU 是单核基准（可以 >100），由调用方说明口径。
    """
    if whole <= 0:
        return 0.0
    return round(max(part / whole * 100.0, 0.0), 2)


def _default_collector() -> PlatformCollector:
    if _IS_WINDOWS:
        return WindowsCollector()
    if _IS_LINUX:
        return LinuxCollector()
    return FallbackCollector()


# --- Windows：ctypes 调 kernel32 / psapi -----------------------------------

if _IS_WINDOWS:  # pragma: no branch - 平台判定
    import ctypes
    import ctypes.wintypes as _wt

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", _wt.DWORD),
            ("dwHighDateTime", _wt.DWORD),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", _wt.DWORD),
            ("PageFaultCount", _wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", _wt.DWORD),
            ("dwMemoryLoad", _wt.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    def _filetime_seconds(value: Any) -> float:
        ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
        return ticks / 10_000_000.0

    class WindowsApi:
        """kernel32 / psapi 的最小封装：只有本模块用到的 7 个调用。

        单独成类是为了让测试能把整个对象换成假实现，而不必 monkeypatch
        ``ctypes`` 本身。
        """

        def __init__(self) -> None:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # 句柄是 64 位：restype 不设 c_void_p 会被截断成 int32；argtypes 不声明
            # HANDLE 为 c_void_p 时，回传的伪句柄（-1 → 0xFFFFFFFFFFFFFFFF）会触发
            # "int too long to convert"。两处都必须显式声明。
            self.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            self.kernel32.GetCurrentProcess.argtypes = []
            # Win7+ 走 K32GetProcessMemoryInfo；老系统回退 psapi 的同名导出。
            get_memory_info = getattr(self.kernel32, "K32GetProcessMemoryInfo", None)
            if get_memory_info is None:  # pragma: no cover - 古董系统
                psapi = ctypes.WinDLL("psapi", use_last_error=True)
                get_memory_info = psapi.GetProcessMemoryInfo
            get_memory_info.restype = ctypes.c_int
            get_memory_info.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
                _wt.DWORD,
            ]
            self._get_process_memory_info = get_memory_info
            self.kernel32.GetProcessTimes.restype = ctypes.c_int
            self.kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]
            self.kernel32.GetProcessHandleCount.restype = ctypes.c_int
            self.kernel32.GetProcessHandleCount.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_wt.DWORD),
            ]
            self.kernel32.GetProcessIoCounters.restype = ctypes.c_int
            self.kernel32.GetProcessIoCounters.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_IO_COUNTERS),
            ]
            self.kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
            self.kernel32.GlobalMemoryStatusEx.argtypes = [
                ctypes.POINTER(_MEMORYSTATUSEX)
            ]
            self.kernel32.GetSystemTimes.restype = ctypes.c_int
            self.kernel32.GetSystemTimes.argtypes = [
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]
            try:
                self.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
                self.kernel32.GetTickCount64.argtypes = []
            except AttributeError:  # pragma: no cover - 古董系统
                self.kernel32.GetTickCount64 = None

        def current_process(self) -> Any:
            # 再包一层 c_void_p：即使调用方拿到的 restype 退化成 int 也仍然安全。
            return ctypes.c_void_p(self.kernel32.GetCurrentProcess())

        def process_memory(self) -> _PROCESS_MEMORY_COUNTERS:
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            handle = self.current_process()
            ok = self._get_process_memory_info(
                handle, ctypes.byref(counters), counters.cb
            )
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return counters

        def process_times(self) -> tuple[float, float]:
            handle = self.current_process()
            creation = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            ok = self.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return _filetime_seconds(user), _filetime_seconds(kernel)

        def process_handle_count(self) -> int:
            handle = self.current_process()
            count = _wt.DWORD(0)
            ok = self.kernel32.GetProcessHandleCount(handle, ctypes.byref(count))
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return int(count.value)

        def process_io(self) -> tuple[int, int]:
            handle = self.current_process()
            counters = _IO_COUNTERS()
            ok = self.kernel32.GetProcessIoCounters(handle, ctypes.byref(counters))
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return int(counters.ReadTransferCount), int(counters.WriteTransferCount)

        def memory_status(self) -> _MEMORYSTATUSEX:
            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ok = self.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return status

        def system_times(self) -> tuple[float, float, float]:
            idle = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            ok = self.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return (
                _filetime_seconds(idle),
                _filetime_seconds(kernel),
                _filetime_seconds(user),
            )

        def tick_count_seconds(self) -> int:
            getter = getattr(self.kernel32, "GetTickCount64", None)
            if getter is None:  # pragma: no cover - 古董系统
                return 0
            return int(getter() / 1000)


class WindowsCollector:
    """Windows 采集器：kernel32 / psapi，纯标准库。"""

    def __init__(self, api: Any = None) -> None:
        self.api = api if api is not None else WindowsApi()

    def sample_process(self) -> ProcessSample:
        memory = self.api.process_memory()
        user, kernel = self.api.process_times()
        try:
            handles = self.api.process_handle_count()
        except OSError:
            handles = 0
        try:
            io_read, io_write = self.api.process_io()
        except OSError:
            io_read = io_write = 0
        return ProcessSample(
            rss_bytes=int(memory.WorkingSetSize),
            peak_rss_bytes=int(memory.PeakWorkingSetSize),
            private_bytes=int(memory.PagefileUsage),
            cpu_user_seconds=user,
            cpu_sys_seconds=kernel,
            threads=threading.active_count(),
            handles=handles,
            io_read_bytes=io_read,
            io_write_bytes=io_write,
        )

    def sample_system(self) -> SystemSample:
        idle, kernel, user = self.api.system_times()
        # GetSystemTimes 的 kernel 时间**含 idle**，所以总时间 = kernel + user。
        status = self.api.memory_status()
        return SystemSample(
            cpu_total_seconds=kernel + user,
            cpu_idle_seconds=idle,
            mem_total_bytes=int(status.ullTotalPhys),
            mem_avail_bytes=int(status.ullAvailPhys),
            uptime_seconds=self.api.tick_count_seconds(),
        )


# --- Linux：/proc ----------------------------------------------------------


class LinuxCollector:
    """Linux 采集器：只读 ``/proc``。"""

    def __init__(self, proc_root: str = "/proc") -> None:
        self.proc_root = proc_root

    def sample_process(self) -> ProcessSample:
        statm = _read_text(os.path.join(self.proc_root, "self", "statm")).split()
        page_size = os.sysconf("SC_PAGE_SIZE")
        rss = int(statm[1]) * page_size if len(statm) > 1 else 0
        status = _parse_key_values(
            _read_text(os.path.join(self.proc_root, "self", "status"))
        )
        stat = _read_text(os.path.join(self.proc_root, "self", "stat")).split(")")[-1].split()
        # /proc/self/stat 去掉 comm 字段后，utime/stime 落在下标 11/12。
        ticks = os.sysconf("SC_CLK_TCK")
        user = int(stat[11]) / ticks if len(stat) > 12 else 0.0
        system = int(stat[12]) / ticks if len(stat) > 12 else 0.0
        io_read, io_write = self._read_io()
        return ProcessSample(
            rss_bytes=rss,
            peak_rss_bytes=_kb(status.get("VmHWM", "0 kB")),
            private_bytes=_kb(status.get("VmData", "0 kB")),
            cpu_user_seconds=user,
            cpu_sys_seconds=system,
            threads=int(status.get("Threads", "0") or 0),
            handles=_count_dir_entries(os.path.join(self.proc_root, "self", "fd")),
            io_read_bytes=io_read,
            io_write_bytes=io_write,
        )

    def _read_io(self) -> tuple[int, int]:
        try:
            io = _parse_key_values(_read_text(os.path.join(self.proc_root, "self", "io")))
        except OSError:
            return 0, 0
        return int(io.get("read_bytes", "0") or 0), int(io.get("write_bytes", "0") or 0)

    def sample_system(self) -> SystemSample:
        cpu_line = _read_text(os.path.join(self.proc_root, "stat")).splitlines()[0].split()
        values = [float(item) for item in cpu_line[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0.0)
        meminfo = _parse_key_values(_read_text(os.path.join(self.proc_root, "meminfo")))
        try:
            uptime = int(float(_read_text(os.path.join(self.proc_root, "uptime")).split()[0]))
        except (OSError, IndexError, ValueError):
            uptime = 0
        return SystemSample(
            cpu_total_seconds=total / os.sysconf("SC_CLK_TCK"),
            cpu_idle_seconds=idle / os.sysconf("SC_CLK_TCK"),
            mem_total_bytes=_kb(meminfo.get("MemTotal", "0 kB")),
            mem_avail_bytes=_kb(
                meminfo.get("MemAvailable", meminfo.get("MemFree", "0 kB"))
            ),
            uptime_seconds=uptime,
        )


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _parse_key_values(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _kb(value: str) -> int:
    try:
        return int(str(value).split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def _count_dir_entries(path: str) -> int:
    try:
        return len(os.listdir(path))
    except OSError:
        return 0


# --- 其它平台：尽力而为 -----------------------------------------------------


class FallbackCollector:
    """POSIX 兜底采集器：进程内存走 ``resource``，系统指标留 0。

    不是 Linux、也不是 Windows 的平台（macOS / BSD）不抛异常，系统级指标为 0
    并在结果里显式表明不支持 —— 与 Go ``rss_other.go`` 的"返回 0"同义。
    """

    def sample_process(self) -> ProcessSample:
        usage = None
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
        except Exception:  # pragma: no cover - 平台无 resource 模块
            usage = None
        peak = int(getattr(usage, "ru_maxrss", 0) or 0)
        # ru_maxrss 在 macOS 是字节，在其余 POSIX 是 KiB。
        if sys.platform != "darwin":  # pragma: no cover - 其它 POSIX
            peak *= 1024
        return ProcessSample(
            rss_bytes=peak,
            peak_rss_bytes=peak,
            threads=threading.active_count(),
        )

    def sample_system(self) -> SystemSample:
        return SystemSample(cpu_total_seconds=0.0, cpu_idle_seconds=0.0)


# --- 旧接口（兼容保留） ----------------------------------------------------


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
    """当前进程常驻内存字节数。

    Linux 读 ``/proc/self/statm``；Windows 走 ``K32GetProcessMemoryInfo``
    （1.1.4 补齐：此前恒返回 0，导致 ``/metrics`` 与日报在 Windows 上永远显示 0）；
    其它平台返回 0（与 Go ``rss_other.go`` 一致）。
    """
    process = _current_process_sample()
    return process.rss_bytes if process is not None else 0


def _peak_rss_bytes() -> int:
    """进程峰值 RSS（对应 Go 的 ``MemStats.Sys`` 量级，仅作展示）。"""
    process = _current_process_sample()
    if process is not None:
        return process.peak_rss_bytes
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
    except Exception:  # pragma: no cover - 平台无 resource 模块
        return 0
    # Linux 的 ru_maxrss 单位是 KiB，macOS 是字节。
    if _IS_LINUX:
        return int(usage.ru_maxrss) * 1024
    return int(usage.ru_maxrss)


def _current_process_sample() -> ProcessSample | None:
    """给旧接口用的一次进程采样；平台不支持或失败时返回 ``None``。"""
    collector = _default_collector()
    try:
        return collector.sample_process()
    except Exception:  # noqa: BLE001 - 旧接口必须保持"失败返回 0"的语义
        return None


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


def format_uptime(seconds: int) -> str:
    """把秒数渲染成 ``2天3小时`` / ``3小时12分`` / ``12分34秒``。"""
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def format_resources(snapshot: ResourceSnapshot) -> str:
    """把 :class:`ResourceSnapshot` 渲染成给模型 / 操作者看的紧凑中文。

    工具 ``runtime_resources`` 与 ``/resources`` 命令共用这一处实现
    （单一出处），避免两套格式在后续改动里漂移。
    """
    interval_ms = int(round(snapshot.sample_interval * 1000))
    process = snapshot.process
    system = snapshot.system
    lines = [f"ATRI-X 资源占用（采样 {interval_ms}ms）"]

    process_parts = [
        f"内存 {format_bytes(process.rss_bytes)}"
        f"（峰值 {format_bytes(process.peak_rss_bytes)}"
        f"，私有提交 {format_bytes(process.private_bytes)}）",
        f"CPU {process.cpu_percent:.1f}%（单核基准，{system.cpu_count} 核）",
        f"线程 {process.threads}",
    ]
    if process.handles:
        process_parts.append(f"句柄 {process.handles}")
    if process.io_read_bytes or process.io_write_bytes:
        process_parts.append(
            f"读写 {format_bytes(process.io_read_bytes)} / "
            f"{format_bytes(process.io_write_bytes)}"
        )
    lines.append("【进程】" + "｜".join(process_parts))

    system_parts = [
        f"内存 {format_bytes(system.mem_avail_bytes)} 可用 / "
        f"{format_bytes(system.mem_total_bytes)}"
        f"（已用 {system.mem_percent:.1f}%）",
        f"CPU {system.cpu_percent:.1f}%",
        f"逻辑核 {system.cpu_count}",
    ]
    if system.uptime_seconds:
        system_parts.append(f"运行 {format_uptime(system.uptime_seconds)}")
    lines.append("【系统】" + "｜".join(system_parts))

    for path, total, free in snapshot.disks:
        used_percent = 0.0 if total <= 0 else (total - free) / total * 100.0
        lines.append(
            f"【磁盘】{path} 可用 {format_bytes(free)} / {format_bytes(total)}"
            f"（已用 {used_percent:.1f}%）"
        )
    if snapshot.dir_bytes:
        lines.append(f"【目录】{format_bytes(snapshot.dir_bytes)}（含子目录）")
    if snapshot.errors:
        lines.append("【降级】" + "；".join(snapshot.errors))
    lines.append(
        f"【说明】进程 CPU 为单核基准，忙时可超过 100%；采样窗口 {interval_ms}ms。"
    )
    return "\n".join(lines)

