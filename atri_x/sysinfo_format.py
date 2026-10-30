"""资源快照的文本渲染：``/resources`` 命令与 ``runtime_resources`` 工具的唯一出处。

从 :mod:`atri_x.sysinfo` 拆出来（1.1.4 修订）有两个原因：

1. ``sysinfo.py`` 已经超线（只降不升），渲染逻辑独立后采集侧才装得下硬件/进程时长；
2. 隐私边界要在一个地方收口 —— **输出里不出现任何本机路径**：
   磁盘只说"系统盘 / 数据盘 N / 指定卷 N"，不说盘符与目录；显卡/CPU 只说型号；
   用户名、主机名、安装目录一概不出现。任何降级信息也只写"哪一步失败"。

分段：``【硬件】``（CPU/内存/GPU 型号与系统版本）、``【系统】``（整机 CPU/内存/开机时长）、
``【显卡】``（每卡占用/显存/温度/功耗，或整机 3D 占用）、``【磁盘】``（卷级已用率）、
``【软件】``（ATRI-X 版本、进程时长、内存/CPU/线程/句柄/读写）、``【降级】``、``【说明】``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from atri_x import __version__

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查用，避免循环 import
    from atri_x.sysinfo import DiskUsage, ResourceSnapshot

__all__ = ["format_bytes", "format_resources", "format_uint_bytes", "format_uptime"]

_UNITS = ("KB", "MB", "GB", "TB", "PB")


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


def format_resources(snapshot: "ResourceSnapshot") -> str:
    """把 :class:`atri_x.sysinfo.ResourceSnapshot` 渲染成给模型 / 操作者看的紧凑中文。

    命令与工具共用这一处实现（单一出处），避免两套格式在后续改动里漂移。
    """
    interval_ms = int(round(snapshot.sample_interval * 1000))
    hardware = getattr(snapshot, "hardware", None)
    lines = [f"ATRI-X 资源占用（采样 {interval_ms}ms）"]
    lines.append(_hardware_line(hardware, snapshot.system))
    lines.append(_system_line(snapshot.system))
    lines.extend(_gpu_lines(hardware))
    for disk in snapshot.disks:
        lines.append(_disk_line(disk))
    lines.append(_software_line(snapshot.process))
    if snapshot.dir_bytes:
        lines.append(f"【目录】合计 {format_bytes(snapshot.dir_bytes)}（含子目录）")
    if snapshot.errors:
        lines.append("【降级】" + "；".join(snapshot.errors))
    lines.append(
        f"【说明】进程 CPU 为单核基准，忙时可超过 100%；采样窗口 {interval_ms}ms；"
        "只采集型号与占用，不读取本机路径、用户名或主机名。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 各段渲染
# ---------------------------------------------------------------------------


def _hardware_line(hardware: Any, system: Any) -> str:
    parts: list[str] = []
    cpu_model = str(getattr(hardware, "cpu_model", "") or "").strip()
    threads = int(getattr(hardware, "cpu_threads", 0) or system.cpu_count or 0)
    if cpu_model:
        parts.append(f"CPU {cpu_model}（{threads} 线程）" if threads else f"CPU {cpu_model}")
    elif threads:
        parts.append(f"CPU {threads} 线程")
    if system.mem_total_bytes > 0:
        parts.append(f"内存 {format_bytes(system.mem_total_bytes)}")
    models = _gpu_models_text(hardware)
    if models:
        parts.append(f"GPU {models}")
    os_name = str(getattr(hardware, "os_name", "") or "").strip()
    arch = str(getattr(hardware, "arch", "") or "").strip()
    system_text = " ".join(part for part in (os_name, arch) if part)
    if system_text:
        parts.append(f"系统 {system_text}")
    return "【硬件】" + "｜".join(parts) if parts else "【硬件】暂无可用信息"


def _gpu_models_text(hardware: Any) -> str:
    gpus = list(getattr(hardware, "gpus", None) or [])
    names: list[str] = []
    for gpu in gpus:
        name = str(getattr(gpu, "name", "") or "").strip()
        if not name:
            continue
        if getattr(gpu, "integrated", False):
            names.append(f"{name}（共享显存）")
        elif getattr(gpu, "memory_total_bytes", 0):
            names.append(f"{name}（{format_bytes(gpu.memory_total_bytes)}）")
        else:
            names.append(name)
    return "、".join(names)


def _system_line(system: Any) -> str:
    parts = [f"CPU {system.cpu_percent:.1f}%"]
    if system.mem_total_bytes > 0:
        parts.append(
            f"内存 已用 {system.mem_percent:.1f}%"
            f"（{format_bytes(system.mem_avail_bytes)} 可用 / "
            f"{format_bytes(system.mem_total_bytes)}）"
        )
    if system.cpu_count:
        parts.append(f"逻辑核 {system.cpu_count}")
    if system.uptime_seconds:
        parts.append(f"开机 {format_uptime(system.uptime_seconds)}")
    return "【系统】" + "｜".join(parts)


def _gpu_lines(hardware: Any) -> list[str]:
    gpus = list(getattr(hardware, "gpus", None) or [])
    aggregate = getattr(hardware, "gpu_utilization_percent", None)
    measured = [gpu for gpu in gpus if getattr(gpu, "utilization_percent", None) is not None]
    if not gpus and aggregate is None:
        return ["【显卡】未识别到可用显卡"]
    if measured:
        return [_per_gpu_line(gpu) for gpu in gpus]
    if aggregate is not None:
        parts = [f"整机 GPU 3D 占用 {aggregate:.1f}%"]
        memory_used = int(getattr(hardware, "gpu_memory_used_bytes", 0) or 0)
        if memory_used > 0:
            parts.append(f"显存占用 {format_bytes(memory_used)}")
        return ["【显卡】" + "｜".join(parts)]
    names = [
        str(getattr(gpu, "name", "") or "").strip()
        for gpu in gpus
        if str(getattr(gpu, "name", "") or "").strip()
    ]
    if names:
        return ["【显卡】" + "、".join(names) + "｜占用不可得"]
    return []


def _per_gpu_line(gpu: Any) -> str:
    parts = [str(getattr(gpu, "name", "") or "显卡")]
    utilization = getattr(gpu, "utilization_percent", None)
    if utilization is None:
        parts.append("占用不可得")
    else:
        parts.append(f"GPU {utilization:.1f}%")
    total = int(getattr(gpu, "memory_total_bytes", 0) or 0)
    used = int(getattr(gpu, "memory_used_bytes", 0) or 0)
    if total > 0:
        if used > 0:
            percent = used / total * 100.0
            parts.append(
                f"显存 {format_bytes(used)} / {format_bytes(total)}（{percent:.1f}%）"
            )
        else:
            parts.append(f"显存 {format_bytes(total)}")
    temperature = getattr(gpu, "temperature_c", None)
    if temperature is not None:
        parts.append(f"温度 {temperature:.0f}°C")
    power = getattr(gpu, "power_watts", None)
    if power is not None:
        limit = getattr(gpu, "power_limit_watts", None)
        if limit:
            parts.append(f"功耗 {power:.0f} W / {limit:.0f} W")
        else:
            parts.append(f"功耗 {power:.0f} W")
    return "【显卡】" + "｜".join(parts)


def _disk_line(disk: "DiskUsage") -> str:
    label = str(getattr(disk, "label", "") or "磁盘")
    total = int(getattr(disk, "total_bytes", 0) or 0)
    free = int(getattr(disk, "free_bytes", 0) or 0)
    if total <= 0:
        return f"【磁盘】{label} 容量不可得"
    used_percent = (total - free) / total * 100.0
    return (
        f"【磁盘】{label} 已用 {used_percent:.1f}%"
        f"（{format_bytes(free)} 可用 / {format_bytes(total)}）"
    )


def _software_line(process: Any) -> str:
    parts = [f"ATRI-X {__version__} 运行中"]
    uptime = int(getattr(process, "uptime_seconds", 0) or 0)
    if uptime > 0:
        parts.append(f"已运行 {format_uptime(uptime)}")
    parts.append(
        f"内存 {format_bytes(process.rss_bytes)}"
        f"（峰值 {format_bytes(process.peak_rss_bytes)}"
        f"，私有提交 {format_bytes(process.private_bytes)}）"
    )
    parts.append(f"CPU {process.cpu_percent:.1f}%（单核基准）")
    parts.append(f"线程 {process.threads}")
    if process.handles:
        parts.append(f"句柄 {process.handles}")
    if process.io_read_bytes or process.io_write_bytes:
        parts.append(
            f"读写 {format_bytes(process.io_read_bytes)} / "
            f"{format_bytes(process.io_write_bytes)}"
        )
    return "【软件】" + "｜".join(parts)
