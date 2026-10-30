"""硬件身份与显卡型号的只读采集（Windows / Linux，零第三方依赖）。

对应 Python 侧新增（1.1.4 修订）：``/resources`` 与 ``runtime_resources`` 要报
「CPU / 内存 / 磁盘 / 显卡占用 + 型号」，但**不能回显本机真实路径与敏感信息**。
本模块负责**身份**：CPU 型号、逻辑核数、操作系统版本、架构、显卡型号与显存容量；
显存/GPU 占用读数见 :mod:`atri_x.gpu_usage`，文本渲染见 :mod:`atri_x.sysinfo_format`。

隐私约束（硬约束，不是建议）
---------------------------
* 只输出**型号名与数值**；绝不输出盘符、用户目录、安装目录、用户名、主机名。
* 显卡枚举只读注册表的 ``DriverDesc`` / ``qwMemorySize``；同键下还有
  ``DalDCELogFilePath`` 之类带绝对路径的驱动值，**不读**。
* 采集失败只记「哪一步失败 + 异常类型名」；异常原文可能带路径，**不进入渲染**。

数据来源
--------
============  ============================================================
平台          型号
============  ============================================================
Windows       ``HKLM\\SYSTEM\\...\\Class\\{4d36e968...}\\NNNN``
              （``DriverDesc`` / ``qwMemorySize``）
Linux         ``/sys/class/drm/card*/device``（``vendor`` / ``device``）
其它          —
============  ============================================================

显卡**占用**（``nvidia-smi`` / PDH / AMD 的 ``gpu_busy_percent``）在
:mod:`atri_x.gpu_usage`；本模块只负责"这是块什么卡"，并把占用读数合并进
:class:`HardwareInfo`。
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from atri_x.gpu_usage import (  # noqa: F401 - GpuInfo 等在此再导出，保持旧导入路径可用
    GpuInfo,
    generic_gpu_usage,
    nvidia_smi_gpus,
    nvidia_smi_path,
    parse_nvidia_smi_csv,
    query_nvidia_smi,
)

__all__ = [
    "GpuInfo",
    "HardwareInfo",
    "arch_name",
    "collect_hardware",
    "cpu_model",
    "gpu_vendor",
    "is_integrated_gpu",
    "is_virtual_gpu",
    "nvidia_smi_path",
    "os_description",
    "parse_nvidia_smi_csv",
    "query_nvidia_smi",
]

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

#: 虚拟/镜像显示驱动的名字特征：它们不是真实 GPU，报出来只会误导。
_VIRTUAL_GPU_MARKERS = (
    "idd",
    "indirect display",
    "virtual",
    "remote display",
    "basic display",
    "mirror driver",
    "parsec",
    "spacedesk",
    "usb display",
)

#: 集成显卡的名字特征：显存是共享内存，不能按独立显存展示。
_INTEGRATED_GPU_MARKERS = (
    "uhd graphics",
    "hd graphics",
    "iris",
    "vega ",
    "radeon graphics",
    "integrated",
)


@dataclass
class HardwareInfo:
    """一次硬件快照（只有型号与数值，没有任何路径）。"""

    cpu_model: str = ""
    cpu_threads: int = 0
    os_name: str = ""
    arch: str = ""
    gpus: list[GpuInfo] = field(default_factory=list)
    #: 无法归到单卡时的整机 GPU 3D 引擎占用（0–100）。
    gpu_utilization_percent: float | None = None
    #: 整机显卡显存占用（PDH 合计）；``0`` 表示不可得。
    gpu_memory_used_bytes: int = 0
    #: 占用数据的来源说明（``nvidia-smi`` / ``perf-counters`` / ``""``）。
    gpu_source: str = ""
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def collect_hardware() -> HardwareInfo:
    """采集一次硬件快照；任何一步失败都降级进 ``errors``，绝不抛异常。"""
    info = HardwareInfo(cpu_threads=os.cpu_count() or 0)
    for step, attribute, reader in (
        ("CPU 型号", "cpu_model", cpu_model),
        ("系统版本", "os_name", os_description),
        ("架构", "arch", arch_name),
    ):
        try:
            setattr(info, attribute, reader())
        except Exception as exc:  # noqa: BLE001 - 采集失败只降级
            info.errors.append(_failure(step, exc))

    try:
        gpus = list_gpus()
    except Exception as exc:  # noqa: BLE001
        info.errors.append(_failure("显卡型号", exc))
        gpus = []
    nvidia, nvidia_error = nvidia_smi_gpus()
    if nvidia_error:
        info.errors.append(nvidia_error)

    if nvidia:
        # nvidia-smi 是 NVIDIA 卡的权威来源：注册表里的 NVIDIA 条目会让同一块卡报两遍。
        info.gpus = list(nvidia) + [gpu for gpu in gpus if gpu.vendor != "NVIDIA"]
        info.gpu_source = "nvidia-smi"
    else:
        info.gpus = gpus

    if not any(gpu.utilization_percent is not None for gpu in info.gpus):
        usage = generic_gpu_usage()
        if usage is not None:
            percent, memory_used = usage
            info.gpu_utilization_percent = percent
            info.gpu_memory_used_bytes = memory_used
            info.gpu_source = "perf-counters"
            if len(info.gpus) == 1:
                info.gpus[0] = replace(
                    info.gpus[0],
                    utilization_percent=percent,
                    memory_used_bytes=memory_used or info.gpus[0].memory_used_bytes,
                )
    return info


def cpu_model() -> str:
    """CPU 型号（人类可读）。取不到时返回空串。"""
    if _IS_WINDOWS:
        model = _windows_cpu_model()
        if model:
            return model
    elif _IS_LINUX:
        model = _linux_cpu_model()
        if model:
            return model
    return platform.processor() or platform.machine() or ""


def os_description() -> str:
    """操作系统版本描述（不含主机名 / 用户名）。"""
    if _IS_WINDOWS:
        return _windows_version()
    if _IS_LINUX:
        name = _linux_pretty_name()
        if name:
            return name
    return platform.system() or platform.platform()


def arch_name() -> str:
    """架构的短名：``x64`` / ``arm64`` / ``x86`` / 原始 ``machine``。"""
    machine = (platform.machine() or "").lower()
    if machine in {"amd64", "x86_64", "x64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86", "i386", "i686"}:
        return "x86"
    return machine


def gpu_vendor(name: str) -> str:
    """按型号名判断厂商（注册表里没有稳定的 vendor 值）。"""
    lowered = (name or "").lower()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered or "tesla" in lowered:
        return "NVIDIA"
    if "amd" in lowered or "radeon" in lowered or "ati " in lowered:
        return "AMD"
    if "intel" in lowered:
        return "Intel"
    return "其它"


def is_virtual_gpu(name: str) -> bool:
    """名字像虚拟 / 镜像显示驱动（远程桌面、串流、间接显示驱动）时返回 True。"""
    lowered = (name or "").lower()
    return any(marker in lowered for marker in _VIRTUAL_GPU_MARKERS)


def is_integrated_gpu(name: str) -> bool:
    """名字像核显时返回 True（显存是共享内存，不当作独立显存展示）。"""
    lowered = (name or "").lower()
    return any(marker in lowered for marker in _INTEGRATED_GPU_MARKERS)


# ---------------------------------------------------------------------------
# 显卡枚举
# ---------------------------------------------------------------------------


def list_gpus() -> list[GpuInfo]:
    """枚举显卡型号（不依赖占用数据）。失败由 :func:`collect_hardware` 降级。"""
    if _IS_WINDOWS:
        return _dedupe_gpus(_windows_registry_gpus())
    if _IS_LINUX:
        return _dedupe_gpus(_linux_sysfs_gpus())
    return []


def _dedupe_gpus(gpus: Iterable[GpuInfo]) -> list[GpuInfo]:
    """按型号去重；虚拟显示驱动直接丢弃。顺序保持枚举顺序。"""
    out: list[GpuInfo] = []
    seen: set[str] = set()
    for gpu in gpus:
        name = (gpu.name or "").strip()
        if not name or is_virtual_gpu(name):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(gpu)
    return out


def _windows_registry_gpus() -> list[GpuInfo]:
    """读显示适配器类键（``{4d36e968-...}``）下的 ``DriverDesc`` 与显存容量。

    只取两个值：型号名与显存字节数。**不读**同键下那些带绝对路径的驱动值。
    """
    import winreg  # noqa: PLC0415 - 仅 Windows 需要

    root = (
        r"SYSTEM\CurrentControlSet\Control\Class"
        r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    gpus: list[GpuInfo] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
    except OSError:
        return gpus
    try:
        index = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            if not subkey_name.isdigit():
                continue
            gpu = _windows_registry_gpu(winreg, root, subkey_name)
            if gpu is not None:
                gpus.append(gpu)
    finally:
        key.Close()
    return gpus


def _windows_registry_gpu(winreg: Any, root: str, subkey_name: str) -> GpuInfo | None:
    try:
        subkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + "\\" + subkey_name)
    except OSError:
        return None
    try:
        name = _winreg_string(winreg, subkey, "DriverDesc")
        if not name:
            return None
        integrated = is_integrated_gpu(name)
        return GpuInfo(
            name=name,
            vendor=gpu_vendor(name),
            memory_total_bytes=0 if integrated else _winreg_memory(winreg, subkey),
            integrated=integrated,
            source="registry",
        )
    finally:
        subkey.Close()


def _winreg_string(winreg: Any, key: Any, value_name: str) -> str:
    try:
        value = winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return ""
    return str(value).strip() if value else ""


def _winreg_memory(winreg: Any, key: Any) -> int:
    """显存容量：优先 ``qwMemorySize``（QWORD），退到 ``MemorySize``（32 位 DWORD）。"""
    for value_name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
        try:
            raw = winreg.QueryValueEx(key, value_name)[0]
        except OSError:
            continue
        size = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
        if size > 0:
            return size
    return 0


def _linux_sysfs_gpus() -> list[GpuInfo]:
    """``/sys/class/drm/card*``：AMD 能直接拿到占用与显存，其它厂商只有型号/厂商。"""
    root = "/sys/class/drm"
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    gpus: list[GpuInfo] = []
    for entry in entries:
        if not entry.startswith("card") or not entry[4:].isdigit():
            continue
        device = os.path.join(root, entry, "device")
        vendor_id = _read_text(os.path.join(device, "vendor"))
        if not vendor_id:
            continue
        vendor = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}.get(
            vendor_id.lower(), "其它"
        )
        name = _linux_gpu_name(device, vendor)
        busy = _read_int(os.path.join(device, "gpu_busy_percent"))
        total = _read_int(os.path.join(device, "mem_info_vram_total"))
        used = _read_int(os.path.join(device, "mem_info_vram_used"))
        gpus.append(
            GpuInfo(
                name=name,
                vendor=vendor,
                memory_total_bytes=total or 0,
                memory_used_bytes=used or 0,
                utilization_percent=float(busy) if busy is not None else None,
                integrated=is_integrated_gpu(name),
                source="sysfs",
            )
        )
    return gpus


def _linux_gpu_name(device: str, vendor: str) -> str:
    """内核没有统一的型号字符串：能读 ``product_name`` 就用，否则用厂商占位名。"""
    for candidate in ("product_name", "label"):
        name = _read_text(os.path.join(device, candidate))
        if name:
            return name
    return f"{vendor} GPU"


# ---------------------------------------------------------------------------
# Windows 注册表 / 系统版本（winreg 只在函数内 import，避免非 Windows 报错）
# ---------------------------------------------------------------------------


def _windows_cpu_model() -> str:
    import winreg  # noqa: PLC0415

    path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return ""
    try:
        return _winreg_string(winreg, key, "ProcessorNameString")
    finally:
        key.Close()


def _windows_version() -> str:
    """``Windows 11（build 26100）`` 之类的描述；不含主机名。"""
    version = sys.getwindowsversion()
    name = "Windows"
    if version.major == 10:
        name = "Windows 11" if version.build >= 22000 else "Windows 10"
    elif version.major == 6:
        name = {1: "Windows 7", 2: "Windows 8", 3: "Windows 8.1"}.get(
            version.minor, "Windows"
        )
    return f"{name}（build {version.build}）"


# ---------------------------------------------------------------------------
# Linux：/proc、os-release、sysfs
# ---------------------------------------------------------------------------


def _linux_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, sep, value = line.partition(":")
                if sep and key.strip() in {"model name", "Model", "cpu model"}:
                    return value.strip()
    except OSError:
        return ""
    return ""


def _linux_pretty_name() -> str:
    try:
        release = platform.freedesktop_os_release()
    except (OSError, AttributeError):
        return ""
    return str(release.get("PRETTY_NAME") or release.get("NAME") or "")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _read_int(path: str) -> int | None:
    text = _read_text(path)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _failure(step: str, exc: BaseException) -> str:
    """失败的**安全**描述：只有步骤名与异常类型名，绝不带异常原文（可能含路径）。"""
    return f"{step}采集失败（{type(exc).__name__}）"
