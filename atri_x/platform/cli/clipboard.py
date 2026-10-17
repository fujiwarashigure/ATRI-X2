"""TUI 剪贴板读写：系统剪贴板 + OSC52 转义序列。

对应 Go：``internal/platform/cli/clipboard.go``。

Go 依赖 ``atotto/clipboard``（OS 剪贴板）与 ``aymanbagabas/go-osc52/v2``（终端转义）。
Python 标准库没有剪贴板 API，因此：

* 系统剪贴板用**标准库能力**实现 —— Windows 走 ``ctypes`` 调 ``user32``/``kernel32``
  的 ``CF_UNICODETEXT``；macOS 走 ``pbcopy``/``pbpaste``；其余平台依次尝试
  ``wl-copy`` / ``xclip`` / ``xsel``。任何一步失败都抛 :class:`ClipboardError`
  （对应 Go 的 ``error`` 返回），由 :class:`AutoClipboardWriter` 决定是否退到 OSC52。
* OSC52 的转义序列按 go-osc52 v2 的默认形态生成：``ESC ] 52 ; c ; <base64> BEL``，
  上限 :data:`OSC52_CLIPBOARD_MAX_BYTES` 按 **UTF-8 字节数**计（与 Go 的
  ``len([]byte(text))`` 一致）。

命名差异：Go 的接口方法 ``WriteAll`` → :meth:`ClipboardWriter.write_all`（PORTING §1.4）。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

#: OSC52 载荷上限（字节）。对应 Go ``osc52ClipboardMaxBytes``。
OSC52_CLIPBOARD_MAX_BYTES = 100 * 1024

#: OSC52 序列的起止符。go-osc52 v2 默认用 BEL 结束。
OSC52_PREFIX = "\x1b]52;c;"
OSC52_SUFFIX = "\x07"

#: 判定"远程会话"的环境变量。对应 Go ``autoClipboardWriter.remoteSession``。
REMOTE_SESSION_ENV = ("SSH_CONNECTION", "SSH_TTY", "TMUX", "STY")

__all__ = [
    "OSC52_CLIPBOARD_MAX_BYTES",
    "OSC52_PREFIX",
    "OSC52_SUFFIX",
    "REMOTE_SESSION_ENV",
    "AutoClipboardWriter",
    "ClipboardError",
    "ClipboardWriter",
    "Osc52ClipboardWriter",
    "SystemClipboardWriter",
    "TuiClipboardMsg",
    "new_clipboard_writer",
    "osc52_sequence",
    "read_system_clipboard",
    "read_tui_clipboard",
    "write_system_clipboard",
]


class ClipboardError(Exception):
    """剪贴板读写失败。对应 Go 里被 ``fmt.Errorf`` 包出来的 error。"""


@runtime_checkable
class ClipboardWriter(Protocol):
    """把一段文本写进某个剪贴板通道的写入器。对应 Go ``clipboardWriter``。"""

    def write_all(self, text: str) -> None: ...


@dataclass
class TuiClipboardMsg:
    """一次异步剪贴板读的结果。对应 Go ``tuiClipboardMsg``。

    ``err`` 非空时 ``text`` 无意义（Go 侧同样只保证二者之一有效）。
    """

    text: str = ""
    err: BaseException | None = None


# ---------------------------------------------------------------------------
# OSC52
# ---------------------------------------------------------------------------

def osc52_sequence(text: str) -> str:
    """生成 OSC52 序列（base64 载荷，UTF-8 编码）。"""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return OSC52_PREFIX + encoded + OSC52_SUFFIX


@dataclass
class Osc52ClipboardWriter:
    """把文本写成 OSC52 转义序列发给终端。对应 Go ``osc52ClipboardWriter``。"""

    out: object | None = None
    max_bytes: int = 0

    def write_all(self, text: str) -> None:
        if self.out is None:
            raise ClipboardError("osc52 output is nil")
        limit = self.max_bytes if self.max_bytes > 0 else OSC52_CLIPBOARD_MAX_BYTES
        size = len(text.encode("utf-8"))
        if size > limit:
            raise ClipboardError(
                f"copy text too large for OSC52: {size} bytes > {limit} bytes"
            )
        payload = osc52_sequence(text)
        write = getattr(self.out, "write", None)
        if not callable(write):  # pragma: no cover - 防御性
            raise ClipboardError("osc52 output is not writable")
        try:
            write(payload)
        except UnicodeEncodeError:
            # 控制台码页（本机 GBK）容不下转义序列时按 ascii 回退，见 PORTING §7.4。
            write(payload.encode("ascii", "backslashreplace").decode("ascii"))
        flush = getattr(self.out, "flush", None)
        if callable(flush):
            flush()


# ---------------------------------------------------------------------------
# 系统剪贴板
# ---------------------------------------------------------------------------

def write_system_clipboard(text: str) -> None:
    """写系统剪贴板；失败抛 :class:`ClipboardError`。"""
    if sys.platform.startswith("win"):
        _write_windows_clipboard(text)
        return
    if sys.platform == "darwin":
        _run_clipboard_tool(["pbcopy"], text)
        return
    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(command[0]):
            _run_clipboard_tool(command, text)
            return
    raise ClipboardError("no system clipboard tool found")


def read_system_clipboard() -> str:
    """读系统剪贴板；失败抛 :class:`ClipboardError`。"""
    if sys.platform.startswith("win"):
        return _read_windows_clipboard()
    if sys.platform == "darwin":
        return _run_clipboard_tool(["pbpaste"], None)
    for command in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
        if shutil.which(command[0]):
            return _run_clipboard_tool(command, None)
    raise ClipboardError("no system clipboard tool found")


def _run_clipboard_tool(command: list[str], text: str | None) -> str:
    """跑一个剪贴板命令行工具；``text`` 为 ``None`` 时读、否则写。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 命令来自固定白名单
            command,
            input=text.encode("utf-8") if text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ClipboardError(f"{command[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ClipboardError(f"{command[0]} exited {completed.returncode}: {detail}")
    return completed.stdout.decode("utf-8", "replace")


def _write_windows_clipboard(text: str) -> None:
    """Windows：``CF_UNICODETEXT`` + ``GlobalAlloc``/``SetClipboardData``。"""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    gmemptr_moveable, cf_unicode_text = 0x0042, 13
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(gmemptr_moveable, len(data))
    if not handle:
        raise ClipboardError("GlobalAlloc failed")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise ClipboardError("GlobalLock failed")
    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise ClipboardError("OpenClipboard failed")
    try:
        if not user32.EmptyClipboard():
            raise ClipboardError("EmptyClipboard failed")
        if not user32.SetClipboardData(cf_unicode_text, handle):
            raise ClipboardError("SetClipboardData failed")
        # 所有权已转移给系统，不能再 GlobalFree。
    finally:
        user32.CloseClipboard()


def _read_windows_clipboard() -> str:
    """Windows：``CF_UNICODETEXT`` + ``GetClipboardData``。"""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    cf_unicode_text = 13
    if not user32.IsClipboardFormatAvailable(cf_unicode_text):
        return ""
    if not user32.OpenClipboard(None):
        raise ClipboardError("OpenClipboard failed")
    try:
        handle = user32.GetClipboardData(cf_unicode_text)
        if not handle:
            raise ClipboardError("GetClipboardData failed")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardError("GlobalLock failed")
        try:
            text = ctypes.c_wchar_p(pointer).value
        finally:
            kernel32.GlobalUnlock(handle)
        return text or ""
    finally:
        user32.CloseClipboard()


@dataclass
class SystemClipboardWriter:
    """系统剪贴板写入器。对应 Go ``systemClipboardWriter``。"""

    def write_all(self, text: str) -> None:
        write_system_clipboard(text)


# ---------------------------------------------------------------------------
# 自动选择
# ---------------------------------------------------------------------------

@dataclass
class AutoClipboardWriter:
    """按会话环境在系统剪贴板与 OSC52 之间选择。对应 Go ``autoClipboardWriter``。

    远程会话（SSH / tmux / screen）优先 OSC52，失败再退回系统剪贴板；
    本地会话反过来。两边都失败时抛最后一个错误（与 Go 的返回值一致）。
    """

    system: ClipboardWriter | None = None
    osc52: ClipboardWriter | None = None
    env: Callable[[str], str] | None = None

    def write_all(self, text: str) -> None:
        if self.remote_session():
            try:
                self._write_osc52(text)
                return
            except ClipboardError:
                self._write_system(text)
                return
        try:
            self._write_system(text)
            return
        except ClipboardError:
            self._write_osc52(text)

    def _write_system(self, text: str) -> None:
        if self.system is None:
            raise ClipboardError("system clipboard is nil")
        self.system.write_all(text)

    def _write_osc52(self, text: str) -> None:
        if self.osc52 is None:
            raise ClipboardError("osc52 clipboard is nil")
        self.osc52.write_all(text)

    def remote_session(self) -> bool:
        """对应 Go ``autoClipboardWriter.remoteSession``。"""
        env = self.env if self.env is not None else os.environ.get
        return any(env(name) for name in REMOTE_SESSION_ENV)


def new_clipboard_writer() -> AutoClipboardWriter:
    """Go ``newClipboardWriter`` 的等价物。"""
    return AutoClipboardWriter(
        system=SystemClipboardWriter(),
        osc52=Osc52ClipboardWriter(out=sys.stdout),
        env=os.environ.get,
    )


def read_tui_clipboard() -> Callable[[], TuiClipboardMsg]:
    """对应 Go ``readTUIClipboard``：返回一个"读到结果就产出消息"的可调用对象。

    Go 返回 ``tea.Cmd``；Python 里 ``tea.Cmd`` 没有对应物，因此返回**零参同步函数**，
    调用它即得到 :class:`TuiClipboardMsg`（由调用方决定放到哪个队列）。
    """

    def command() -> TuiClipboardMsg:
        try:
            return TuiClipboardMsg(text=read_system_clipboard())
        except BaseException as exc:  # noqa: BLE001 - 与 Go 一致地把错误带给 UI
            return TuiClipboardMsg(err=exc)

    return command
