"""ATRI-X 子进程 / 相关进程采集：把 DSH 内核、draw.io 渲染器等算进资源快照。

``/resources`` 此前只报 ATRI-X 自己那一个进程（RSS / CPU / 线程 / 句柄），于是
任务管理器里由软件拉起来的 ``node.exe``（DSH 内核）、``draw.io.exe``（图表渲染）、
``atri-x.exe``（研究/图表技能子进程）、协议端与 GUI 进程都不在统计里。本模块补上
这部分：一次采两份清单（进程树里的子孙 + 与 ATRI-X 同安装目录的其它进程），由
:mod:`atri_x.sysinfo` 求两次采样的差值算 CPU%。

两条分组口径
------------
``child``
    当前进程（后台服务）的**子孙进程**：DSH 内核、draw.io 渲染链、研究 runner、
    shell / MCP / hook 等由 agent 直接或间接拉起的进程都在这里。
``related``
    与 ATRI-X **同安装目录**的其它进程（GUI ``atri-x-gui.exe``、QQ 协议端
    ``NapCatWinBootMain.exe`` / ``QQ.exe``、上次渲染残留的 ``draw.io.exe`` 等）。
    只在**冻结产物**里启用（源码树跑测试时没有"安装目录"的概念，不扫同目录进程，
    免得把无关进程算进来）。

隐私与失败口径
--------------
渲染只出现**可执行文件名**（``node.exe`` / ``draw.io.exe``），不出现命令行、路径、
用户名；完整路径只在 :func:`install_root` / :class:`...` 内部用于"是否同安装目录"
判定，不进 :class:`ChildProcess` 的快照字段。任何一步失败都只返回"采到多少算多少"，
由调用方降级（``/resources`` 不因拿不到子进程而整条失败）。

平台实现：Windows 走 ``kernel32`` 的 Toolhelp 快照 + ``OpenProcess``（纯 ``ctypes``，
零第三方依赖）；Linux 读 ``/proc``；其它平台返回空清单。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "GROUP_CHILD",
    "GROUP_RELATED",
    "ChildProcess",
    "ChildProcessCollector",
    "LinuxChildProcessCollector",
    "WindowsChildProcessCollector",
    "default_child_process_collector",
    "EmptyChildProcessCollector",
    "install_root",
]

#: 后台服务自己的子孙进程（由 agent 直接/间接拉起）。
GROUP_CHILD = "child"

#: 与 ATRI-X 同安装目录的其它进程（GUI / 协议端 / 渲染残留）。
GROUP_RELATED = "related"

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

# ``WindowsChildProcessCollector`` 只在 Windows 分支定义；非 Windows 平台从导出表移除，
# 否则 ``check_imports --strict`` 会在 Linux 上把 Windows-only 导出判为缺失。
if not _IS_WINDOWS:  # pragma: no cover - 平台判定
    __all__ = [name for name in __all__ if name != "WindowsChildProcessCollector"]


@dataclass(frozen=True)
class ChildProcess:
    """一个非自身进程的采样读数（名字只保留可执行文件名，不带路径）。"""

    pid: int = 0
    name: str = ""
    group: str = GROUP_CHILD
    rss_bytes: int = 0
    private_bytes: int = 0
    cpu_seconds: float = 0.0
    start_unix_seconds: float = 0.0


class ChildProcessCollector(Protocol):
    """子进程采集器协议：``sample`` 返回**本次存活**的进程清单。"""

    def sample(self) -> list[ChildProcess]: ...


class EmptyChildProcessCollector:
    """不支持的平台：永远返回空清单（``/resources`` 显示"无活动子进程"）。"""

    def sample(self) -> list[ChildProcess]:
        return []


def install_root() -> str:
    """ATRI-X 的安装目录；源码运行（非冻结）返回空串。

    冻结产物里 ``sys.executable`` 就是安装目录下的 ``atri-x.exe`` /
    ``atri-x-gui.exe``，其所在目录就是整个安装根（``app\\``），协议端
    （``qqproto\\``）、内置运行时（``_internal\\runtime``）、渲染器
    （``_internal\\drawio_runtime``）都在它下面。
    """
    if not getattr(sys, "frozen", False):
        return ""
    executable = getattr(sys, "executable", "") or ""
    if not executable:
        return ""
    return os.path.dirname(os.path.abspath(executable))


def default_child_process_collector() -> ChildProcessCollector:
    """按平台选择子进程采集器。"""
    if _IS_WINDOWS:
        return WindowsChildProcessCollector()
    if _IS_LINUX:
        return LinuxChildProcessCollector()
    return EmptyChildProcessCollector()


# ---------------------------------------------------------------------------
# 公共辅助：父进程表 → 子孙集合
# ---------------------------------------------------------------------------
def _descendant_pids(parents: dict[int, int], root_pid: int) -> set[int]:
    """从 ``parent → (child pid)`` 映射里找出 ``root_pid`` 的全部子孙（不含自己）。"""
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        if child <= 0 or parent <= 0 or child == parent:
            continue
        children.setdefault(parent, []).append(child)
    found: set[int] = set()
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        if pid in found or pid == root_pid:
            continue
        found.add(pid)
        pending.extend(children.get(pid, ()))
    return found


def _is_under_install_root(path: str) -> bool:
    """进程可执行路径是否在 ATRI-X 安装目录下（不区分大小写）。"""
    if not path:
        return False
    root = install_root()
    if not root:
        return False
    try:
        prefix = os.path.normcase(os.path.abspath(root)) + os.sep
        return os.path.normcase(os.path.abspath(path)).startswith(prefix)
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        return False


def _collect(
    parents: dict[int, int],
    names: dict[int, str],
    read: Any,
    path_of: Any,
) -> list[ChildProcess]:
    """按"子孙优先、同安装目录次之"生成清单；读不到的进程静默跳过。"""
    own_pid = os.getpid()
    children = _descendant_pids(parents, own_pid)
    out: list[ChildProcess] = []
    for pid in sorted(children):
        info = read(pid)
        if info is None:
            continue
        user, kernel, rss, private = info
        out.append(
            ChildProcess(
                pid=pid,
                name=names.get(pid, ""),
                group=GROUP_CHILD,
                rss_bytes=rss,
                private_bytes=private,
                cpu_seconds=user + kernel,
            )
        )
    for pid in sorted(parents):
        if pid == own_pid or pid in children:
            continue
        if not _is_under_install_root(path_of(pid)):
            continue
        info = read(pid)
        if info is None:
            continue
        user, kernel, rss, private = info
        out.append(
            ChildProcess(
                pid=pid,
                name=names.get(pid, ""),
                group=GROUP_RELATED,
                rss_bytes=rss,
                private_bytes=private,
                cpu_seconds=user + kernel,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Windows：Toolhelp 快照 + OpenProcess（纯 ctypes）
# ---------------------------------------------------------------------------
if _IS_WINDOWS:  # pragma: no branch - 平台判定
    import ctypes
    import ctypes.wintypes as _wt

    from atri_x.sysinfo_platform import WindowsApi

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF
    _MAX_PROCESSES = 4096

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", _wt.DWORD),
            ("cntUsage", _wt.DWORD),
            ("th32ProcessID", _wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", _wt.DWORD),
            ("cntThreads", _wt.DWORD),
            ("th32ParentProcessID", _wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", _wt.DWORD),
            ("szExeFile", _wt.WCHAR * 260),
        ]

    class WindowsChildProcessApi:
        """Toolhelp 快照与单进程读取的最小封装（测试可整体替换）。"""

        def __init__(self, api: Any = None) -> None:
            self.api = api if api is not None else WindowsApi()
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            self.kernel32.CreateToolhelp32Snapshot.argtypes = [
                _wt.DWORD,
                _wt.DWORD,
            ]
            self.kernel32.Process32FirstW.restype = ctypes.c_int
            self.kernel32.Process32FirstW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PROCESSENTRY32W),
            ]
            self.kernel32.Process32NextW.restype = ctypes.c_int
            self.kernel32.Process32NextW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PROCESSENTRY32W),
            ]

        def entries(self) -> list[tuple[int, int, str]]:
            """整机进程快照：``[(pid, parent_pid, exe 文件名)]``。"""
            raw = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
            if not raw or int(raw) == _INVALID_HANDLE_VALUE:
                return []
            handle = ctypes.c_void_p(int(raw))
            rows: list[tuple[int, int, str]] = []
            try:
                entry = _PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                ok = self.kernel32.Process32FirstW(handle, ctypes.byref(entry))
                while ok and len(rows) < _MAX_PROCESSES:
                    rows.append(
                        (
                            int(entry.th32ProcessID),
                            int(entry.th32ParentProcessID),
                            str(entry.szExeFile),
                        )
                    )
                    ok = self.kernel32.Process32NextW(handle, ctypes.byref(entry))
            finally:
                self.api.close_handle(handle)
            return rows

        def read(self, pid: int) -> tuple[float, float, int, int] | None:
            return self.api.sample_other_process(pid)

        def path_of(self, pid: int) -> str:
            return self.api.process_image_path(pid)

    class WindowsChildProcessCollector:
        """Windows 子进程采集器：Toolhelp 枚举 + ``OpenProcess`` 读计数。"""

        def __init__(self, api: Any = None) -> None:
            self._api = api if api is not None else WindowsChildProcessApi()

        def sample(self) -> list[ChildProcess]:
            try:
                rows = self._api.entries()
            except Exception:  # noqa: BLE001 - 采集失败按"没有子进程"降级
                return []
            parents = {pid: parent for pid, parent, _name in rows if pid > 0}
            names = {pid: name for pid, _parent, name in rows if pid > 0}
            return _collect(parents, names, self._api.read, self._api.path_of)


# ---------------------------------------------------------------------------
# Linux：/proc
# ---------------------------------------------------------------------------
class LinuxChildProcessCollector:
    """Linux 子进程采集器：只读 ``/proc``（无第三方依赖）。"""

    def __init__(self, proc_root: str = "/proc") -> None:
        self.proc_root = proc_root

    def sample(self) -> list[ChildProcess]:
        parents: dict[int, int] = {}
        names: dict[int, str] = {}
        for pid in self._pids():
            parents[pid] = self._parent_of(pid)
            names[pid] = self._name_of(pid)
        return _collect(parents, names, self._read, self._path_of)

    def _pids(self) -> list[int]:
        try:
            entries = os.listdir(self.proc_root)
        except OSError:
            return []
        return [int(name) for name in entries if name.isdigit()]

    def _parent_of(self, pid: int) -> int:
        stat = self._stat(pid)
        if len(stat) > 1:
            try:
                return int(stat[1])
            except ValueError:
                return 0
        return 0

    def _name_of(self, pid: int) -> str:
        try:
            with open(
                os.path.join(self.proc_root, str(pid), "comm"),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def _stat(self, pid: int) -> list[str]:
        """读 ``/proc/<pid>/stat`` 并去掉 ``comm``（名字里可能有空格/括号）。"""
        try:
            with open(
                os.path.join(self.proc_root, str(pid), "stat"),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                text = handle.read()
        except OSError:
            return []
        return text.split(")")[-1].split()

    def _read(self, pid: int) -> tuple[float, float, int, int] | None:
        stat = self._stat(pid)
        if len(stat) <= 12:
            return None
        try:
            ticks = os.sysconf("SC_CLK_TCK")
            user = int(stat[11]) / ticks
            kernel = int(stat[12]) / ticks
        except (ValueError, OSError):
            return None
        rss, private = self._memory(pid)
        return user, kernel, rss, private

    def _memory(self, pid: int) -> tuple[int, int]:
        try:
            with open(
                os.path.join(self.proc_root, str(pid), "status"),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                text = handle.read()
        except OSError:
            return 0, 0
        values: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                values[key.strip()] = value.strip()
        return _kb(values.get("VmRSS")), _kb(values.get("VmData"))

    def _path_of(self, pid: int) -> str:
        try:
            return os.readlink(os.path.join(self.proc_root, str(pid), "exe"))
        except OSError:
            return ""


def _kb(value: str | None) -> int:
    try:
        return int(str(value or "").split()[0]) * 1024
    except (IndexError, ValueError):
        return 0
