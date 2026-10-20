"""进程与路径助手：程序目录、状态目录、后台服务的 pid 记录与终止、单实例。

从 :mod:`atri_x.gui.service` 拆出来的「与配置无关」的那一层：

- 路径：:func:`app_dir` / :func:`application_paths` / :func:`state_dir` /
  :func:`service_log_path` / :func:`open_path`；
- 进程：:func:`write_service_pid` / :func:`read_service_pid` /
  :func:`is_process_alive` / :func:`stop_service`；
- 单实例：:func:`single_instance_lock` / :func:`focus_existing_window`。

为什么单独一层：这些函数在测试里要脱离 Tk 单独跑，也方便将来换别的 GUI 工具包时复用。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Sequence

__all__ = [
    "app_dir",
    "application_paths",
    "cleanup_service_pid",
    "focus_existing_window",
    "is_process_alive",
    "load_window_state",
    "open_path",
    "read_service_pid",
    "record_startup_failure",
    "save_window_state",
    "service_log_path",
    "single_instance_lock",
    "startup_log_candidates",
    "startup_log_path",
    "state_dir",
    "stop_service",
    "window_state_path",
    "write_service_pid",
]

#: 后台服务的 stdout/stderr 文件名（放在状态目录下）。
SERVICE_LOG_NAME = "service.out.log"

#: 启动失败日志文件名。
#:
#: 桌面端最惨的失败是「配置目录都建不出来」（例如程序放在带 Low 完整性标签的目录里、
#: 或 %APPDATA% 被策略挡住），这时按配置目录写日志同样会失败，所以有兜底候选路径，
#: 见 :func:`startup_log_candidates`。
STARTUP_LOG_NAME = "gui-startup.log"

#: 后台服务 pid 文件名。
SERVICE_PID_NAME = "service.pid"

#: 窗口位置文件名。
WINDOW_STATE_NAME = "window.json"

#: Windows ``CreateMutexW`` 的 ``ERROR_ALREADY_EXISTS``。
_ERROR_ALREADY_EXISTS = 183


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def app_dir() -> str:
    """程序所在目录：冻结包是可执行文件目录，源码运行时是仓库根。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def application_paths() -> tuple[str, str]:
    """返回 ``(程序目录, 控制台版 exe 路径)``；源码运行时第二项为空串。"""
    directory = app_dir()
    if not getattr(sys, "frozen", False):
        return directory, ""
    for name in ("atri-x.exe", "ATRI-X.exe", "atri_x.exe"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return directory, candidate
    return directory, ""


def config_dir_hint() -> str:
    """尽量不触发「补齐默认配置」的配置目录（状态目录以它为基准）。"""
    env_path = (os.environ.get("ATRIX_CONFIG_FILE") or "").strip()
    if env_path:
        return os.path.dirname(os.path.normpath(env_path))
    appdata = (os.environ.get("AppData") or os.environ.get("APPDATA") or "").strip()
    if appdata:
        return os.path.join(appdata, "ATRI-X")
    try:
        from atri_x.config.loader import platform_default_config_path
    except Exception:  # noqa: BLE001 - 配置层不可用时给空串
        return ""
    path = platform_default_config_path() or ""
    return os.path.dirname(os.path.normpath(path)) if path else ""


def state_dir() -> str:
    """GUI 自己的状态目录：``<配置目录>/gui``（pid、日志、窗口位置）。"""
    base = config_dir_hint()
    directory = os.path.join(base, "gui") if base else os.path.join(app_dir(), "gui-state")
    os.makedirs(directory, exist_ok=True)
    return directory


def service_log_path() -> str:
    """后台服务的 stdout/stderr 落点。"""
    return os.path.join(state_dir(), SERVICE_LOG_NAME)


# ---------------------------------------------------------------------------
# 启动失败诊断日志
# ---------------------------------------------------------------------------


def startup_log_candidates() -> list[str]:
    """启动失败日志的候选落点（按优先级，已去重）。

    1. ``<配置目录>/gui/gui-startup.log`` —— 正常情况；
    2. ``%TEMP%/ATRI-X/gui-startup.log`` —— 配置目录本身就是坏的时候（最需要日志的场景）；
    3. ``<程序目录>/gui-startup.log`` —— 最后兜底（安装目录通常可写时）。
    """
    candidates: list[str] = []
    base = config_dir_hint()
    if base:
        candidates.append(os.path.join(base, "gui", STARTUP_LOG_NAME))
    try:
        temp = tempfile.gettempdir()
    except (OSError, ValueError):  # pragma: no cover - 极端环境
        temp = ""
    if temp:
        candidates.append(os.path.join(temp, "ATRI-X", STARTUP_LOG_NAME))
    candidates.append(os.path.join(app_dir(), STARTUP_LOG_NAME))
    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def startup_log_path() -> str:
    """启动失败日志「首选」落点（不落盘；给提示文案用）。"""
    candidates = startup_log_candidates()
    return candidates[0] if candidates else ""


def record_startup_failure(
    title: str, detail: str, *, candidates: Sequence[str] | None = None
) -> str:
    """把桌面端启动失败写进固定日志；返回实际写入路径，全部候选都失败时返回 ``""``。"""
    block = _startup_failure_block(title, detail)
    for path in (startup_log_candidates() if candidates is None else candidates):
        if not path:
            continue
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(block)
            return path
        except OSError:
            continue
    return ""


def _startup_failure_block(title: str, detail: str) -> str:
    """一条启动失败记录（含排障需要的版本/路径信息）。"""
    from atri_x import __version__  # 延迟导入：gui.process 可能被很早期地 import

    header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (detail or "").replace("\r\n", "\n").replace("\n", "\n        ")
    lines = [
        "=" * 72,
        f"{header}  atri-x {__version__}  frozen={bool(getattr(sys, 'frozen', False))}",
        f"title : {title or ''}",
        f"detail: {body}",
        f"exe   : {getattr(sys, 'executable', '') or ''}",
        f"config: {config_dir_hint() or '<unavailable>'}",
        "",
    ]
    return "\n".join(lines)


def open_path(path: str) -> bool:
    """用系统默认程序打开目录/文件（Windows ``os.startfile``）。"""
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - 有意调用系统关联
        return True
    except (AttributeError, OSError):
        return False


# ---------------------------------------------------------------------------
# pid 记录与服务终止
# ---------------------------------------------------------------------------


def _pid_path() -> str:
    return os.path.join(state_dir(), SERVICE_PID_NAME)


def write_service_pid(pid: int) -> None:
    """记下后台服务的 pid（供后续「停止服务」与排障用）。"""
    try:
        with open(_pid_path(), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{int(pid)}\n")
    except OSError:
        pass


def read_service_pid() -> int:
    """读记录的 pid；没有/非法返回 0。"""
    try:
        with open(_pid_path(), "r", encoding="utf-8") as handle:
            return int((handle.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def cleanup_service_pid() -> None:
    """删掉 pid 记录（服务已退出/已终止时用）。"""
    try:
        os.remove(_pid_path())
    except OSError:
        pass


def is_process_alive(pid: int) -> bool:
    """pid 是否仍在运行（Windows 用 ``GetExitCodeProcess`` 判断 ``STILL_ACTIVE``）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_service() -> tuple[bool, str]:
    """强制结束由 GUI 拉起的后台服务（需要用户确认，见界面文案）。

    Windows 上没有「给另一个控制台进程发 Ctrl+C」的可靠办法，因此这里用
    ``taskkill /T``：会中断当前任务，写库也可能来不及落盘。**只用于排障**。
    """
    pid = read_service_pid()
    if pid <= 0:
        return False, "没有记录到后台服务进程（可能不是由 GUI 拉起的）"
    if not is_process_alive(pid):
        cleanup_service_pid()
        return True, "后台服务已经不在运行"
    try:
        completed = subprocess.run(  # noqa: S603 - pid 来自本机 pid 文件
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"结束进程失败：{exc}"
    cleanup_service_pid()
    if completed.returncode == 0:
        return True, f"已结束后台服务（pid {pid}）"
    detail = (completed.stderr or completed.stdout or "").strip()
    return False, f"结束进程失败：{detail or completed.returncode}"


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------


def single_instance_lock(name: str = "ATRI-X-GUI") -> tuple[bool, object]:
    """创建单实例命名互斥量；返回 ``(是否首个实例, 句柄)``。

    句柄要一直持有到进程退出（被 GC 回收会释放锁）。非 Windows 上恒为
    ``(True, None)``——那些平台没有 ``CreateMutexW``。
    """
    if os.name != "nt":
        return True, None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.CreateMutexW(None, True, "Local\\" + name)
        if not handle:
            return True, None
        already = kernel32.GetLastError() == _ERROR_ALREADY_EXISTS
        return (not already), handle
    except (AttributeError, OSError):
        return True, None


def focus_existing_window(title: str = "ATRI-X") -> bool:
    """把已在运行的主窗口提到前台（单实例时用）。"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except (AttributeError, OSError):
        return False


# ---------------------------------------------------------------------------
# 窗口位置记忆
# ---------------------------------------------------------------------------


def window_state_path() -> str:
    """窗口位置文件路径（状态目录下的 ``window.json``）。"""
    return os.path.join(state_dir(), WINDOW_STATE_NAME)


def load_window_state() -> dict:
    """读窗口位置；没有/损坏返回空字典（界面用默认尺寸）。"""
    import json

    try:
        with open(window_state_path(), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_window_state(geometry: str) -> None:
    """把窗口几何写回（失败静默：位置记忆不是关键路径）。"""
    import json

    try:
        with open(window_state_path(), "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"geometry": geometry}, handle, ensure_ascii=False)
    except OSError:
        pass
