"""可移植的运行时资源快照（进程 / 系统 / 磁盘 / 硬件）。

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

1.1.4 修订（隐私边界）
----------------------
``/resources`` 曾把"配置目录所在卷"的**真实路径**（如 ``C:\\Users\\<用户名>\\...``）
渲染出来。现在磁盘段一律用**卷标签**（``系统盘`` / ``数据盘 N`` / ``指定卷 N``），
任何路径、用户名、主机名都不出现在快照与渲染里；采集失败的描述也只保留异常类型名
（异常原文可能带路径）。渲染因此拆到 :mod:`atri_x.sysinfo_format`，硬件型号与
显卡占用拆到 :mod:`atri_x.hardware` / :mod:`atri_x.gpu_usage`，本模块只负责采集与
数值换算。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from atri_x.hardware import HardwareInfo, collect_hardware
from atri_x.sysinfo_format import (
    format_bytes,
    format_resources,
    format_uint_bytes,
    format_uptime,
)

__all__ = [
    "DiskUsage",
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
    #: 进程启动的 Unix 时间戳（秒）；拿不到时为 0（渲染层就不报"已运行"）。
    start_unix_seconds: float = 0.0


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
    #: 进程已运行秒数（"软件进程状态"用）；拿不到启动时间时为 0。
    uptime_seconds: int = 0


@dataclass(frozen=True)
class DiskUsage:
    """一个卷的容量读数。

    ``label`` 是**展示名**（``系统盘`` / ``数据盘 N`` / ``指定卷 N``），刻意不是路径：
    快照与渲染都不得携带本机真实路径（1.1.4 隐私修订）。
    """

    label: str = ""
    total_bytes: int = 0
    free_bytes: int = 0


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

    ``disks`` 是 :class:`DiskUsage` 列表（按展示顺序）；采集失败的卷不出现在
    这里，原因写进 ``errors``（只写异常类型名，不写路径）。
    """

    process: ProcessResources = field(default_factory=ProcessResources)
    system: SystemResources = field(default_factory=SystemResources)
    disks: list[DiskUsage] = field(default_factory=list)
    #: 硬件身份与显卡占用（CPU/GPU 型号、系统版本、显存/3D 占用）。
    hardware: HardwareInfo = field(default_factory=HardwareInfo)
    dir_bytes: int = 0
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL
    errors: list[str] = field(default_factory=list)


class PlatformCollector(Protocol):
    """平台采集器协议：只提供"读一次原始计数器"，换算逻辑留在本模块。

    测试用假实现只需实现这两个方法，就能完全绕开真实 API 与真实计时。
    """

    def sample_process(self) -> ProcessSample: ...

    def sample_system(self) -> SystemSample: ...


#: 硬件采集最多等多久（秒）：``nvidia-smi`` 与 PDH 都可能慢，但命令不能被拖死。
HARDWARE_JOIN_TIMEOUT_SECONDS = 4.0


def collect_resources(
    paths: Any = None,
    *,
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
    include_dir_size: bool = False,
    platform_impl: PlatformCollector | None = None,
    hardware_provider: Callable[[], HardwareInfo] | None = None,
) -> ResourceSnapshot:
    """采集进程 / 系统 / 磁盘 / 硬件资源。

    参数
    ----
    paths:
        要统计磁盘用量的路径（``str`` 或可迭代对象）。空表示自动枚举本机固定磁盘
        （卷标签，不带路径）；给了路径也只会渲染成"指定卷 N"。
    sample_interval:
        CPU 占用率的采样窗口（秒），会被夹到
        ``[MIN_SAMPLE_INTERVAL, MAX_SAMPLE_INTERVAL]``。
    include_dir_size:
        是否递归统计 ``paths`` 的目录大小（``os.walk`` 很慢，默认关；自动枚举
        磁盘时忽略——不会去遍历整个卷）。
    platform_impl:
        注入的采集器；``None`` 时按当前平台自动选择。
    hardware_provider:
        注入的硬件采集器；``None`` 时用 :func:`atri_x.hardware.collect_hardware`。
        硬件采集与 CPU 采样窗口**并行**跑，多出来的等待时间被采样窗口吸收。

    **这是同步函数**：内部含 ``time.sleep``，调用方必须丢进线程
    （工具侧用 ``asyncio.to_thread``），否则会阻塞事件循环。
    """
    interval = _clamp_interval(sample_interval)
    snapshot = ResourceSnapshot(sample_interval=interval)
    collector = platform_impl if platform_impl is not None else _default_collector()
    targets = _normalize_paths(paths)
    explicit_targets = bool(targets)
    if not targets:
        targets = _local_volume_roots()

    hardware_box: list[HardwareInfo] = []
    hardware_errors: list[str] = []

    def _run_hardware() -> None:
        try:
            provider = hardware_provider if hardware_provider is not None else collect_hardware
            hardware_box.append(provider())
        except Exception as exc:  # noqa: BLE001 - 硬件采集失败只降级
            hardware_errors.append(f"硬件采集失败（{type(exc).__name__}）")

    # 硬件采集（注册表 / nvidia-smi / PDH）与下面的 CPU 采样窗口并行，避免叠加延迟。
    hardware_thread = threading.Thread(
        target=_run_hardware, name="atri-x-hardware", daemon=True
    )
    hardware_thread.start()

    first_process: ProcessSample | None = None
    first_system: SystemSample | None = None
    started = time.monotonic()
    try:
        first_process = collector.sample_process()
    except Exception as exc:  # noqa: BLE001 - 采集失败只降级，不炸调用方
        snapshot.errors.append(f"进程采样失败（{type(exc).__name__}）")
    try:
        first_system = collector.sample_system()
    except Exception as exc:  # noqa: BLE001
        snapshot.errors.append(f"系统采样失败（{type(exc).__name__}）")

    # 采样窗口：要拿到 CPU 占用率必须有两个时间点的计数器。
    time.sleep(interval)
    elapsed = max(time.monotonic() - started, MIN_SAMPLE_INTERVAL)

    second_process: ProcessSample | None = None
    second_system: SystemSample | None = None
    try:
        second_process = collector.sample_process()
    except Exception as exc:  # noqa: BLE001
        snapshot.errors.append(f"进程二次采样失败（{type(exc).__name__}）")
    # 硬件线程是采集器自己起的 Python 线程：线程数指标要把这一根减掉，
    # 否则"软件进程状态"会平白多 1 个线程。
    extra_threads = 1 if hardware_thread.is_alive() else 0
    try:
        second_system = collector.sample_system()
    except Exception as exc:  # noqa: BLE001
        snapshot.errors.append(f"系统二次采样失败（{type(exc).__name__}）")

    if first_process is not None and second_process is not None:
        snapshot.process = _build_process_resources(
            first_process, second_process, elapsed, extra_threads=extra_threads
        )
    elif second_process is not None:
        # 只能采到一次：内存类指标仍然有价值，CPU% 留 0。
        snapshot.process = _build_process_resources(
            None, second_process, 0.0, extra_threads=extra_threads
        )
    elif first_process is not None:
        snapshot.process = _build_process_resources(None, first_process, 0.0)

    if first_system is not None and second_system is not None:
        snapshot.system = _build_system_resources(first_system, second_system)
    elif second_system is not None:
        snapshot.system = _build_system_resources(None, second_system)
    elif first_system is not None:
        snapshot.system = _build_system_resources(None, first_system)

    disk_index = 0
    seen_volumes: set[str] = set()
    for target in targets:
        volume = _volume_key(target)
        if volume and volume in seen_volumes:
            continue
        if volume:
            seen_volumes.add(volume)
        try:
            total, free = disk_usage(target)
        except (OSError, ValueError) as exc:
            # 异常原文可能带真实路径：只保留类型名。
            snapshot.errors.append(f"磁盘用量采集失败（{type(exc).__name__}）")
            continue
        if _is_system_volume(target):
            label = "系统盘"
        else:
            disk_index += 1
            label = f"指定卷 {disk_index}" if explicit_targets else f"数据盘 {disk_index}"
        snapshot.disks.append(
            DiskUsage(label=label, total_bytes=total, free_bytes=free)
        )

    if include_dir_size and explicit_targets:
        for target in targets:
            try:
                snapshot.dir_bytes += directory_size(target)
            except Exception as exc:  # noqa: BLE001 - os.walk 的 onerror 已吞掉多数错误
                snapshot.errors.append(f"目录大小统计失败（{type(exc).__name__}）")

    hardware_thread.join(timeout=HARDWARE_JOIN_TIMEOUT_SECONDS)
    if hardware_thread.is_alive():
        snapshot.errors.append("硬件采集超时")
    snapshot.errors.extend(hardware_errors)
    if hardware_box:
        snapshot.hardware = hardware_box[0]
        snapshot.errors.extend(snapshot.hardware.errors)
    return snapshot


def resources_to_dict(snapshot: ResourceSnapshot) -> dict[str, Any]:
    """把快照渲染成 JSON 友好的 dict（工具结果的 ``data`` 字段用）。

    与文本渲染同一条隐私边界：磁盘只有 ``label``（``系统盘`` / ``数据盘 N``），
    **没有**任何本机路径。
    """
    hardware = snapshot.hardware
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
            "uptime_seconds": snapshot.process.uptime_seconds,
        },
        "system": {
            "cpu_percent": round(snapshot.system.cpu_percent, 2),
            "cpu_count": snapshot.system.cpu_count,
            "mem_total_bytes": snapshot.system.mem_total_bytes,
            "mem_avail_bytes": snapshot.system.mem_avail_bytes,
            "mem_percent": round(snapshot.system.mem_percent, 2),
            "uptime_seconds": snapshot.system.uptime_seconds,
        },
        "hardware": {
            "cpu_model": hardware.cpu_model,
            "cpu_threads": hardware.cpu_threads,
            "os": hardware.os_name,
            "arch": hardware.arch,
            "gpus": [
                {
                    "name": gpu.name,
                    "vendor": gpu.vendor,
                    "memory_total_bytes": gpu.memory_total_bytes,
                    "memory_used_bytes": gpu.memory_used_bytes,
                    "utilization_percent": gpu.utilization_percent,
                    "temperature_c": gpu.temperature_c,
                    "power_watts": gpu.power_watts,
                    "power_limit_watts": gpu.power_limit_watts,
                    "integrated": gpu.integrated,
                    "source": gpu.source,
                }
                for gpu in hardware.gpus
            ],
            "gpu_utilization_percent": hardware.gpu_utilization_percent,
            "gpu_memory_used_bytes": hardware.gpu_memory_used_bytes,
            "gpu_source": hardware.gpu_source,
        },
        "disks": [
            {
                "label": disk.label,
                "total_bytes": disk.total_bytes,
                "free_bytes": disk.free_bytes,
            }
            for disk in snapshot.disks
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


def _system_volume_root() -> str:
    """系统盘根（``C:\\`` / ``/``）；只用于判断"哪个卷是系统盘"，不参与渲染。"""
    if not _IS_WINDOWS:
        return "/"
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, 260):
            return ""
        drive = os.path.splitdrive(buffer.value)[0]
        return drive + "\\" if drive else ""
    except Exception:  # noqa: BLE001 - 拿不到就退化成"没有系统盘标签"
        return ""


def _local_volume_roots() -> list[str]:
    """本机**固定磁盘**的根（Windows）或 ``/``（其它平台）。

    只统计 ``DRIVE_FIXED``：网络盘、可移动盘、光驱不碰 —— 它们既不属于本机，
    容量读数还容易卡住。
    """
    if not _IS_WINDOWS:
        return ["/"]
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:  # noqa: BLE001
        return []
    roots: list[str] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        try:
            drive_type = int(
                ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            )
        except Exception:  # noqa: BLE001
            continue
        if drive_type == 3:  # DRIVE_FIXED
            roots.append(root)
    return roots


def _volume_key(target: str) -> str:
    """卷级去重键：同一个卷上的多个路径只报一次。"""
    try:
        absolute = os.path.abspath(target)
    except (OSError, ValueError):
        return ""
    drive = os.path.splitdrive(absolute)[0]
    return (drive or absolute).casefold()


def _is_system_volume(target: str) -> bool:
    system_root = _system_volume_root()
    if not system_root:
        return False
    try:
        absolute = os.path.abspath(target)
    except (OSError, ValueError):
        return False
    drive = os.path.splitdrive(absolute)[0]
    if not drive:
        return False
    return drive.casefold() == os.path.splitdrive(system_root)[0].casefold()


def _build_process_resources(
    first: ProcessSample | None,
    second: ProcessSample,
    elapsed: float,
    *,
    extra_threads: int = 0,
) -> ProcessResources:
    process = ProcessResources(
        rss_bytes=second.rss_bytes,
        peak_rss_bytes=second.peak_rss_bytes or second.rss_bytes,
        private_bytes=second.private_bytes,
        threads=max(second.threads - max(extra_threads, 0), 0),
        handles=second.handles,
        io_read_bytes=second.io_read_bytes,
        io_write_bytes=second.io_write_bytes,
    )
    if second.start_unix_seconds > 0:
        process.uptime_seconds = int(max(time.time() - second.start_unix_seconds, 0.0))
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
    """按平台选择采集器；实现已拆到 :mod:`atri_x.sysinfo_platform`（1.1.4 拆分）。"""
    from atri_x.sysinfo_platform import default_collector as select_collector

    return select_collector()



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
