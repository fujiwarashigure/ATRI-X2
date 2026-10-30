"""平台采集器实现：Windows（kernel32 / psapi）、Linux（/proc）与 POSIX 兜底。

从 :mod:`atri_x.sysinfo` 拆出来（1.1.4 修订）：``sysinfo.py`` 已经超线且只降不升，
把"读一次原始计数器"的平台代码独立成模块后，采集/换算/渲染各自有地方放。
数值换算与降级语义仍留在 ``sysinfo``（:func:`atri_x.sysinfo.collect_resources`），
本模块只负责按平台挑一个采集器并读原始计数器。

测试注入沿用 ``sysinfo.PlatformCollector`` 协议：本模块的实现只是它的三个具体版本，
不引入新的契约。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

from atri_x.sysinfo import PlatformCollector, ProcessSample, SystemSample

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

__all__ = [
    "FallbackCollector",
    "LinuxCollector",
    "WindowsApi",
    "WindowsCollector",
    "default_collector",
]


def default_collector() -> PlatformCollector:
    """按当前平台选择采集器（对应 ``sysinfo._default_collector``）。"""
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

    #: FILETIME 起点（1601-01-01）到 Unix 纪元（1970-01-01）之间的秒数。
    _FILETIME_UNIX_EPOCH_OFFSET = 11644473600.0

    def _filetime_unix(value: Any) -> float:
        return _filetime_seconds(value) - _FILETIME_UNIX_EPOCH_OFFSET

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

        def process_times(self) -> tuple[float, float, float]:
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
            return (
                _filetime_seconds(user),
                _filetime_seconds(kernel),
                _filetime_unix(creation),
            )

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
        user, kernel, started = self.api.process_times()
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
            start_unix_seconds=started,
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
            start_unix_seconds=_proc_start_unix(self.proc_root, stat, ticks),
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


def _proc_start_unix(proc_root: str, stat: list[str], ticks: int) -> float:
    """进程启动的 Unix 时间：``btime + starttime / HZ``；拿不到返回 0。"""
    if len(stat) <= 19 or ticks <= 0:
        return 0.0
    boot_time = _proc_boot_time(proc_root)
    if boot_time <= 0:
        return 0.0
    try:
        start_ticks = int(stat[19])
    except ValueError:
        return 0.0
    return boot_time + start_ticks / ticks


def _proc_boot_time(proc_root: str) -> float:
    """``/proc/stat`` 的 ``btime``（Unix 秒）；缺失/不可读返回 0。"""
    for line in _read_text(os.path.join(proc_root, "stat")).splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


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
