"""Shell 解释器探测、子进程配置与进程树清理。

对应 Go：``internal/tool/builtin/shell.go`` 的解释器探测部分（``resolveWindowsShell``
/ ``resolveUnixShell`` / ``detectWindowsShell`` / ``isPowerShellEnv`` /
``isPowerShellShell`` / ``powershellUTF8Command`` / ``currentShellDesc``），加上
``shell_process_windows.go`` 与 ``shell_process_unix.go``。

与 Go 的差异：Go 用 ``sync.Once`` 缓存探测结果，Python 用模块级 ``_cache``；
``configureShellProcess`` 在 Unix 上设置 ``start_new_session=True``（等价
``Setpgid``），Windows 上不做额外配置。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

POWERSHELL_UTF8_PRELUDE = (
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "try { [Console]::OutputEncoding = $OutputEncoding } catch {}; "
)


@dataclass
class WindowsShell:
    """对应 Go ``windowsShell``。"""

    name: str = ""
    args: list[str] = field(default_factory=list)


_windows_shell: WindowsShell | None = None
_unix_shell: tuple[str, list[str]] | None = None


def resolve_windows_shell() -> tuple[str, list[str]]:
    """对应 Go ``resolveWindowsShell``（结果缓存）。"""
    global _windows_shell
    if _windows_shell is None:
        _windows_shell = detect_windows_shell()
    return _windows_shell.name, list(_windows_shell.args)


def detect_windows_shell() -> WindowsShell:
    """对应 Go ``detectWindowsShell``：pwsh → bash → powershell.exe。"""
    if shutil.which("pwsh"):
        return WindowsShell("pwsh", ["-NoProfile", "-Command"])
    if shutil.which("bash"):
        return WindowsShell("bash", ["--noprofile", "--norc", "-c"])
    return WindowsShell("powershell.exe", ["-NoProfile", "-Command"])


def resolve_unix_shell() -> tuple[str, list[str]]:
    """对应 Go ``resolveUnixShell``（结果缓存）。"""
    global _unix_shell
    if _unix_shell is None:
        if shutil.which("bash"):
            _unix_shell = ("bash", ["--noprofile", "--norc", "-c"])
        else:
            _unix_shell = ("sh", ["-c"])
    return _unix_shell[0], list(_unix_shell[1])


def current_shell_desc() -> str:
    """对应 Go ``currentShellDesc``：给模型看的解释器描述。"""
    if sys.platform != "win32":
        name, _ = resolve_unix_shell()
        return "bash" if name == "bash" else "sh (POSIX shell)"
    name, _ = resolve_windows_shell()
    if name == "bash":
        return "bash"
    if name == "pwsh":
        return "PowerShell (pwsh)"
    return "Windows PowerShell"


def is_power_shell_shell(shell_name: str) -> bool:
    """对应 Go ``isPowerShellShell``。"""
    base = os.path.basename((shell_name or "").replace("\\", "/")).lower()
    return base in ("pwsh", "pwsh.exe", "powershell", "powershell.exe")


def is_power_shell_env() -> bool:
    """对应 Go ``isPowerShellEnv``：当前 Windows 解释器是否为 PowerShell。"""
    if sys.platform != "win32":
        return False
    name, _ = resolve_windows_shell()
    return name in ("pwsh", "powershell.exe")


def powershell_utf8_command(shell_name: str, cmd_text: str) -> str:
    """对应 Go ``powershellUTF8Command``：给 PowerShell 命令加 UTF-8 前缀。"""
    if not is_power_shell_shell(shell_name):
        return cmd_text
    return POWERSHELL_UTF8_PRELUDE + cmd_text


def configure_shell_process(kwargs: dict) -> dict:
    """对应 Go ``configureShellProcess``：Unix 上新建进程组，Windows 无操作。"""
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return kwargs


def kill_shell_process_tree(process: object) -> None:
    """对应 Go ``killShellProcessTree``。

    Windows 用 ``taskkill /T /F /PID``（失败则退回 ``kill``）；Unix 杀整个进程组。
    """
    pid = getattr(process, "pid", None)
    if not pid:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()  # type: ignore[attr-defined]
    except (OSError, ProcessLookupError, AttributeError):
        pass


__all__ = [name for name in dir() if not name.startswith("_")]
