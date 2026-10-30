"""显卡占用与型号读数（Windows PDH + nvidia-smi；零第三方依赖）。

``/resources`` 要报「显卡占用」。只有 NVIDIA 有随驱动分发的 ``nvidia-smi``；AMD / Intel
（以及未装 NVIDIA 工具时的 NVIDIA）在 Windows 上统一走 **PDH**：``pdh.dll`` 的
``\\GPU Engine(*)\\Utilization Percentage`` 与 ``\\GPU Adapter Memory(*)\\Dedicated Usage``
—— 与任务管理器同源，覆盖三家厂商，不需要用户另外装 AMD 驱动工具。

设计约束：

* **纯标准库**：PDH 用 ``ctypes`` 调 ``pdh.dll``；``nvidia-smi`` 走 ``subprocess``；
* **纯函数解析**：:func:`parse_gpu_engine_usage` / :func:`parse_gpu_memory_usage` /
  :func:`parse_nvidia_smi_csv` 只吃文本或 ``(实例名, 值)``，可在任意平台单测；
* **失败静默降级**：PDH 不可用（老系统 / 权限受限 / 计数器被禁用）或没有
  ``nvidia-smi`` 时返回 ``None`` / 空列表，由调用方把"占用不可得"如实展示出来，
  不编造 0。

``nvidia-smi`` 是 NVIDIA 驱动的标准组件（通常随驱动装在
``%SystemRoot%\\System32``），本模块按「环境变量 → 随包目录 → 系统目录 → ``PATH``」
的顺序找它；打包方也可以把副本放进 ``runtime/tools/nvidia-smi.exe``，或用
``ATRIX_NVIDIA_SMI`` 指定。**不下载、不生成**任何二进制。

口径：PDH 实例名形如 ``pid_1234_luid_0x..._phys_0_eng_0_engtype_3D``，3D 引擎的
百分比按实例求和并夹到 100 —— 这就是"整机 3D 引擎占用"，多卡时是合计值
（PDH 没有把 LUID 映射回具体显卡的稳定接口，所以不假装能分卡）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

_IS_WINDOWS = sys.platform == "win32"
_MIB = 1024 * 1024

#: 两次 ``PdhCollectQueryData`` 之间的间隔：速率型计数器需要两个时间点。
PDH_SAMPLE_INTERVAL_SECONDS = 0.15

__all__ = [
    "GpuInfo",
    "NVIDIA_SMI_TIMEOUT_SECONDS",
    "PDH_SAMPLE_INTERVAL_SECONDS",
    "generic_gpu_usage",
    "nvidia_smi_gpus",
    "nvidia_smi_path",
    "parse_gpu_engine_usage",
    "parse_gpu_memory_usage",
    "parse_nvidia_smi_csv",
    "query_nvidia_smi",
]

#: ``nvidia-smi`` 的单次采集上限（秒）。它只读，慢一点不该拖垮命令。
NVIDIA_SMI_TIMEOUT_SECONDS = 3.0

#: ``nvidia-smi --query-gpu`` 的字段顺序（解析按位置对齐，改这里必须同步解析）。
_NVIDIA_SMI_FIELDS = (
    "name",
    "utilization.gpu",
    "memory.total",
    "memory.used",
    "temperature.gpu",
    "power.draw",
    "power.limit",
)

@dataclass(frozen=True)
class GpuInfo:
    """一块显卡的型号与（可选的）占用读数。"""

    name: str = ""
    vendor: str = ""
    #: 独立显存总容量；集成显卡为 0（共享内存，不展示成独立显存）。
    memory_total_bytes: int = 0
    memory_used_bytes: int = 0
    utilization_percent: float | None = None
    temperature_c: float | None = None
    power_watts: float | None = None
    power_limit_watts: float | None = None
    integrated: bool = False
    #: 数据来源：``nvidia-smi`` / ``registry`` / ``sysfs`` / ``perf-counters``。
    source: str = ""


# ---------------------------------------------------------------------------
# nvidia-smi
# ---------------------------------------------------------------------------


def nvidia_smi_path() -> str:
    """定位 ``nvidia-smi``；找不到返回空串。顺序：环境变量 → 随包 → 系统 → PATH。"""
    explicit = (os.environ.get("ATRIX_NVIDIA_SMI") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    names = ("nvidia-smi.exe", "nvidia-smi")
    for base in _bundled_bases():
        for name in names:
            candidate = os.path.join(base, "tools", name)
            if os.path.isfile(candidate):
                return candidate
    for directory in _system_nvidia_dirs():
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return shutil.which("nvidia-smi") or ""


def _bundled_bases() -> list[str]:
    """随包目录：冻结版 ``_internal``、exe 同级、源码树仓库根。"""
    bases: list[str] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bases.append(str(meipass))
        # onedir 冻结版：<包>/_internal 的上一级也可能是运行时根。
        bases.append(os.path.dirname(str(meipass)))
    executable = getattr(sys, "executable", "")
    if executable:
        bases.append(os.path.dirname(os.path.abspath(executable)))
    bases.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out: list[str] = []
    for base in bases:
        if base and base not in out:
            out.append(base)
    return out


def _system_nvidia_dirs() -> list[str]:
    if not _IS_WINDOWS:
        return ["/usr/bin", "/usr/local/bin", "/opt/nvidia"]
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return [
        os.path.join(system_root, "System32"),
        os.path.join(program_files, "NVIDIA Corporation", "NVSMI"),
    ]


def query_nvidia_smi() -> list[GpuInfo]:
    """查询 ``nvidia-smi``；未安装 / 超时 / 解析失败都返回空列表（不抛）。"""
    gpus, _error = nvidia_smi_gpus()
    return gpus


def nvidia_smi_gpus() -> tuple[list[GpuInfo], str]:
    executable = nvidia_smi_path()
    if not executable:
        return [], ""
    command = [
        executable,
        "--query-gpu=" + ",".join(_NVIDIA_SMI_FIELDS),
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            creationflags=_no_window_flags(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"nvidia-smi 采集失败（{type(exc).__name__}）"
    if completed.returncode != 0:
        return [], "nvidia-smi 查询失败（退出码非 0）"
    gpus = parse_nvidia_smi_csv(completed.stdout or "")
    if not gpus:
        return [], "nvidia-smi 无可用读数"
    return gpus, ""


def parse_nvidia_smi_csv(text: str) -> list[GpuInfo]:
    """解析 ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits`` 输出。

    字段顺序见 :data:`_NVIDIA_SMI_FIELDS`；``[N/A]`` / 空字段按"不可得"处理。
    """
    gpus: list[GpuInfo] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        fields = [item.strip() for item in stripped.split(",")]
        if len(fields) < len(_NVIDIA_SMI_FIELDS):
            fields.extend([""] * (len(_NVIDIA_SMI_FIELDS) - len(fields)))
        name = fields[0]
        if not name:
            continue
        total_mib = _to_float(fields[2])
        used_mib = _to_float(fields[3])
        gpus.append(
            GpuInfo(
                name=name,
                vendor="NVIDIA",
                memory_total_bytes=int(total_mib * _MIB) if total_mib else 0,
                memory_used_bytes=int(used_mib * _MIB) if used_mib else 0,
                utilization_percent=_to_float(fields[1]),
                temperature_c=_to_float(fields[4]),
                power_watts=_to_float(fields[5]),
                power_limit_watts=_to_float(fields[6]),
                source="nvidia-smi",
            )
        )
    return gpus


def _to_float(value: str) -> float | None:
    text = (value or "").strip()
    if not text or text.upper() in {"N/A", "[N/A]", "NA", "[NOT SUPPORTED]"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _no_window_flags() -> int:
    """Windows 上不弹黑框；其它平台无此参数。"""
    if not _IS_WINDOWS:
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def parse_gpu_engine_usage(items: Iterable[tuple[str, float]]) -> float | None:
    """把 GPU Engine 计数器实例合计成"3D 引擎占用 %"（0–100）。

    ``items`` 是 ``(实例名, 值)``。一个能认出的 3D 引擎都没有时返回 ``None``
    （表示"不可得"，而不是 0）。
    """
    total = 0.0
    seen = False
    for name, value in items:
        lowered = (name or "").lower()
        if "_engtype_" not in lowered:
            continue
        if lowered.rsplit("_engtype_", 1)[-1] != "3d":
            continue
        seen = True
        total += max(float(value), 0.0)
    if not seen:
        return None
    return round(min(total, 100.0), 1)


def parse_gpu_memory_usage(items: Iterable[tuple[str, float]]) -> int:
    """把 ``\\GPU Adapter Memory(*)\\Dedicated Usage`` 实例合计成字节数。"""
    total = 0.0
    for _name, value in items:
        total += max(float(value), 0.0)
    return int(total)


def generic_gpu_usage() -> tuple[float | None, int] | None:
    """整机 GPU 占用 ``(3D 引擎百分比, 显存占用字节)``；当前只有 Windows 有实现。

    返回 ``None`` 表示该平台/该机器上拿不到（渲染层会写"占用不可得"）。
    """
    if _IS_WINDOWS:
        return _windows_pdh_usage()
    return None


def _windows_pdh_usage() -> tuple[float | None, int] | None:
    """用 PDH 读 GPU Engine / GPU Adapter Memory 计数器；任何失败都返回 ``None``。"""
    reader = _new_pdh_reader()
    if reader is None:
        return None
    try:
        percent = parse_gpu_engine_usage(
            (name, value)
            for name, status, value in reader.read(r"\GPU Engine(*)\Utilization Percentage")
            if status == 0
        )
        memory_used = parse_gpu_memory_usage(
            (name, value)
            for name, status, value in reader.read(
                r"\GPU Adapter Memory(*)\Dedicated Usage"
            )
            if status == 0
        )
    except Exception:  # noqa: BLE001 - PDH 不可用时静默降级
        return None
    if percent is None and memory_used <= 0:
        return None
    return percent, memory_used


if _IS_WINDOWS:  # pragma: no branch - 平台判定
    import ctypes
    import ctypes.wintypes as _wt

    _PDH_FMT_DOUBLE = 0x00000200
    _PDH_MORE_DATA = 0x800007D2
    _ERROR_SUCCESS = 0

    class _PdhCounterValue(ctypes.Structure):
        _fields_ = [("status", _wt.DWORD), ("value", ctypes.c_double)]

    class _PdhCounterValueItem(ctypes.Structure):
        _fields_ = [("name", _wt.LPWSTR), ("value", _PdhCounterValue)]

    class _PdhReader:
        """``pdh.dll`` 的最小封装：只做"加计数器 + 取格式化数组"。"""

        def __init__(self) -> None:
            self.api = ctypes.WinDLL("pdh", use_last_error=True)
            self.api.PdhOpenQueryW.restype = ctypes.c_long
            self.api.PdhOpenQueryW.argtypes = [
                _wt.LPCWSTR,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self.api.PdhAddEnglishCounterW.restype = ctypes.c_long
            self.api.PdhAddEnglishCounterW.argtypes = [
                ctypes.c_void_p,
                _wt.LPCWSTR,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self.api.PdhCollectQueryData.restype = ctypes.c_long
            self.api.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self.api.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
            self.api.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p,
                _wt.DWORD,
                ctypes.POINTER(_wt.DWORD),
                ctypes.POINTER(_wt.DWORD),
                ctypes.POINTER(_PdhCounterValueItem),
            ]
            self.api.PdhCloseQuery.restype = ctypes.c_long
            self.api.PdhCloseQuery.argtypes = [ctypes.c_void_p]

        def read(self, counter_path: str) -> list[tuple[str, int, float]]:
            """返回 ``[(实例名, CStatus, 值)]``。"""
            query = ctypes.c_void_p()
            if self.api.PdhOpenQueryW(None, 0, ctypes.byref(query)) != _ERROR_SUCCESS:
                raise OSError("PdhOpenQueryW failed")
            try:
                counter = ctypes.c_void_p()
                if (
                    self.api.PdhAddEnglishCounterW(
                        query, counter_path, 0, ctypes.byref(counter)
                    )
                    != _ERROR_SUCCESS
                ):
                    raise OSError("PdhAddEnglishCounterW failed")
                if self.api.PdhCollectQueryData(query) != _ERROR_SUCCESS:
                    raise OSError("PdhCollectQueryData failed")
                time.sleep(PDH_SAMPLE_INTERVAL_SECONDS)
                if self.api.PdhCollectQueryData(query) != _ERROR_SUCCESS:
                    raise OSError("PdhCollectQueryData (second) failed")
                return self._formatted_items(counter)
            finally:
                self.api.PdhCloseQuery(query)

        def _formatted_items(self, counter: Any) -> list[tuple[str, int, float]]:
            size = _wt.DWORD(0)
            count = _wt.DWORD(0)
            status = self.api.PdhGetFormattedCounterArrayW(
                counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
            )
            if (status & 0xFFFFFFFF) not in (_ERROR_SUCCESS, _PDH_MORE_DATA):
                raise OSError("PdhGetFormattedCounterArrayW (size) failed")
            if size.value == 0 or count.value == 0:
                return []
            buffer = (ctypes.c_byte * size.value)()
            items = ctypes.cast(buffer, ctypes.POINTER(_PdhCounterValueItem))
            if (
                self.api.PdhGetFormattedCounterArrayW(
                    counter,
                    _PDH_FMT_DOUBLE,
                    ctypes.byref(size),
                    ctypes.byref(count),
                    items,
                )
                != _ERROR_SUCCESS
            ):
                raise OSError("PdhGetFormattedCounterArrayW (data) failed")
            out: list[tuple[str, int, float]] = []
            for index in range(count.value):
                item = items[index]
                out.append(
                    (
                        str(item.name or ""),
                        int(item.value.status),
                        float(item.value.value),
                    )
                )
            return out

    def _new_pdh_reader() -> Any:
        """构造 PDH 读取器；系统缺 ``pdh.dll`` 时返回 ``None``。"""
        try:
            return _PdhReader()
        except Exception:  # noqa: BLE001 - 老系统 / 受限环境
            return None

else:  # pragma: no cover - 非 Windows 平台没有 PDH

    def _new_pdh_reader() -> Any:
        return None
