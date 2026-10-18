"""Windows 托盘图标（纯 ``ctypes``，零第三方依赖）。

为什么自己写：仓库的运行时纪律是「只用标准库」，而 Tk 没有托盘 API。这里直接调
``user32`` / ``shell32`` 的 ``Shell_NotifyIconW``，用一个**独立线程 + 隐藏窗口 + 自带
消息循环**承载图标，与 Tk 的主线程解耦：

- Tk 主线程只负责窗口；托盘回调在托盘线程里触发，**必须**用 ``root.after`` 转回主线程
  （见 :class:`atri_x.gui.app.GuiApp`）；
- 图标生命周期：:meth:`TrayIcon.start` 创建图标与消息循环，:meth:`TrayIcon.stop` 退出
  循环并删除图标（幂等）；
- 平台不对/API 调不通时 :meth:`TrayIcon.start` 返回 ``False``，界面据此把「关闭窗口」
  退化成「退出程序」，不会假装托盘可用。

菜单项用 ``(文本, 回调)`` 声明；``None`` 表示分隔线。左键单击/双击 = 激活主窗口。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Any, Callable, Sequence

__all__ = ["MENU_SEPARATOR", "TrayIcon", "available", "command_line"]

#: 菜单分隔线。
MENU_SEPARATOR = None

# --- Win32 常量 -------------------------------------------------------------
_WM_APP = 0x8000
_WM_TRAYICON = _WM_APP + 1
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B

_NIM_ADD = 0x00000000
_NIM_MODIFY = 0x00000001
_NIM_DELETE = 0x00000002

_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_NIF_INFO = 0x00000010
_NIF_SHOWTIP = 0x00000080

_MF_STRING = 0x00000000
_MF_SEPARATOR = 0x00000800
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100

_IDI_APPLICATION = 32512
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010

_NIIF_INFO = 0x00000001
_NIIF_USER = 0x00000004

#: 托盘窗口类名前缀（每个 TrayIcon 实例一个独立类名，避免重启后回调指向旧闭包）。
_CLASS_NAME = "AtriXGuiTrayWindow"

#: 消息循环线程等待窗口就绪的上限（秒）。
_START_TIMEOUT = 5.0

#: 是否已经设过函数原型（64 位下必须设 restype，否则句柄被截成 32 位）。
_APIS_BOUND = False


def _bind_apis() -> None:
    """设置用到的 Win32 函数原型（只做一次）。"""
    global _APIS_BOUND
    if _APIS_BOUND:
        return
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostQuitMessage.restype = None
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [wintypes.LPVOID]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        wintypes.LPVOID, wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [wintypes.LPVOID]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.DispatchMessageW.argtypes = [wintypes.LPVOID]
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, wintypes.LPVOID]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _APIS_BOUND = True


def available() -> bool:
    """当前平台是否支持托盘（Windows + ``ctypes.windll``）。"""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def command_line() -> str:
    """托盘/自启动用的命令行（冻结包指向 exe，源码指向 ``python -m atri_x.gui``）。"""
    import sys

    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    return f'"{os.path.abspath(sys.executable)}" -m atri_x.gui'


# ---------------------------------------------------------------------------
# Win32 结构体与函数原型
# ---------------------------------------------------------------------------


def _structures() -> dict[str, Any]:
    """按需构造结构体（非 Windows 上 import 本模块也不该失败）。"""
    from ctypes import wintypes

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", ctypes.c_wchar * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hWnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", wintypes.POINT),
        ]

    wnd_proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    return {
        "NOTIFYICONDATAW": NOTIFYICONDATAW,
        "WNDCLASSW": WNDCLASSW,
        "MSG": MSG,
        "WNDPROC": wnd_proc_type,
    }


# ---------------------------------------------------------------------------
# 托盘图标
# ---------------------------------------------------------------------------


class TrayIcon:
    """一个托盘图标 + 右键菜单。

    ``menu`` 是 ``[(文本, 回调), ...]``；``None`` 表示分隔线。回调在**托盘线程**里执行，
    必须自己转回 GUI 线程。
    """

    def __init__(
        self,
        tooltip: str = "ATRI-X",
        menu: Sequence[tuple[str, Callable[[], None]] | None] | None = None,
        *,
        on_activate: Callable[[], None] | None = None,
        icon_path: str = "",
    ) -> None:
        self.tooltip = tooltip
        self.menu = list(menu or [])
        self._on_activate = on_activate
        self._icon_path = icon_path
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._hwnd: int = 0
        self._thread_id: int = 0
        self._class_name: str = ""
        self._ok = False
        self._error = ""
        self._lock = threading.Lock()
        self._wndproc_ref: Any = None
        self._hinstance: int = 0

    # ---------------------------------------------------------------- 公开 API

    @property
    def started(self) -> bool:
        """托盘是否已经挂上。"""
        return self._ok

    @property
    def error(self) -> str:
        """最近一次失败原因（``start`` 返回 ``False`` 时有值）。"""
        return self._error

    def start(self, timeout: float = _START_TIMEOUT) -> bool:
        """启动托盘线程并等待图标创建完成；返回是否成功。"""
        if self._thread is not None:
            return self._ok
        if not available():
            self._error = "当前平台不支持托盘"
            return False
        try:
            _bind_apis()
        except (AttributeError, OSError) as exc:
            self._error = f"Win32 API 绑定失败：{exc}"
            return False
        self._thread = threading.Thread(target=self._run, name="atri-x-gui-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self._error = "托盘初始化超时"
            return False
        return self._ok

    def stop(self, timeout: float = 3.0) -> None:
        """移除图标并退出消息循环（幂等）。"""
        self._stopped.set()
        thread = self._thread
        if thread is None:
            return
        hwnd = self._hwnd
        if hwnd:
            try:
                ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass
        thread.join(timeout)
        self._thread = None
        self._ok = False

    def set_tooltip(self, text: str) -> None:
        """更新悬浮提示（连接状态/服务状态用）。"""
        self.tooltip = text
        self._modify(flags=_NIF_TIP)

    def notify(self, title: str, message: str) -> None:
        """弹一个气泡通知（Windows 10+ 表现为通知中心条目）。"""
        self._modify(flags=_NIF_INFO, title=title, message=message)

    # ---------------------------------------------------------------- 线程

    def _run(self) -> None:
        try:
            self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())  # type: ignore[attr-defined]
            self._create_window()
            self._add_icon()
            self._ok = True
            self._error = ""
        except Exception as exc:  # noqa: BLE001 - 托盘失败要降级，不能让进程挂掉
            self._ok = False
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        finally:
            self._ready.set()
        if self._stopped.is_set():
            self._remove_icon()
            return
        self._message_loop()

    def _create_window(self) -> None:
        shapes = _structures()
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._hinstance = int(kernel32.GetModuleHandleW(None))
        self._class_name = f"{_CLASS_NAME}-{id(self)}"

        def _wnd_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
            return self._handle_message(hwnd, message, wparam, lparam)

        self._wndproc_ref = shapes["WNDPROC"](_wnd_proc)  # 必须持有引用，否则回调被回收
        window_class = shapes["WNDCLASSW"]()
        window_class.style = 0
        window_class.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = self._class_name
        user32.RegisterClassW(ctypes.byref(window_class))

        hwnd = user32.CreateWindowExW(
            0, self._class_name, "ATRI-X", 0, 0, 0, 0, 0, None, None, self._hinstance, None
        )
        if not hwnd:
            raise OSError("CreateWindowExW 失败")
        self._hwnd = int(hwnd)

    def _add_icon(self) -> None:
        data = self._new_icon_data()
        data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP | _NIF_SHOWTIP
        data.uCallbackMessage = _WM_TRAYICON
        data.hIcon = self._load_icon()
        data.szTip = self.tooltip[:127]
        if not ctypes.windll.shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):  # type: ignore[attr-defined]
            raise OSError("Shell_NotifyIconW(NIM_ADD) 失败")

    def _remove_icon(self) -> None:
        if not self._hwnd:
            return
        data = self._new_icon_data()
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    def _new_icon_data(self) -> Any:
        shapes = _structures()
        data = shapes["NOTIFYICONDATAW"]()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = 1
        return data

    def _load_icon(self) -> int:
        if self._icon_path and os.path.isfile(self._icon_path):
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            handle = user32.LoadImageW(
                None, self._icon_path, _IMAGE_ICON, 0, 0, _LR_LOADFROMFILE
            )
            if handle:
                return int(handle)
        return int(
            ctypes.windll.user32.LoadIconW(None, ctypes.c_void_p(_IDI_APPLICATION))  # type: ignore[attr-defined]
        )

    def _message_loop(self) -> None:
        shapes = _structures()
        message = shapes["MSG"]()
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        while not self._stopped.is_set():
            got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if got in (0, -1):  # WM_QUIT / 错误
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._remove_icon()

    def _handle_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == _WM_TRAYICON:
            event = int(lparam) & 0xFFFF
            if event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                self._call(self._on_activate)
            elif event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                self._show_menu()
            return 0
        if message == _WM_CLOSE:
            ctypes.windll.user32.DestroyWindow(hwnd)  # type: ignore[attr-defined]
            return 0
        if message == _WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)  # type: ignore[attr-defined]
            return 0
        return int(ctypes.windll.user32.DefWindowProcW(hwnd, message, wparam, lparam))  # type: ignore[attr-defined]

    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        actions: dict[int, Callable[[], None]] = {}
        command_id = 1
        try:
            for item in self.menu:
                if item is None:
                    user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
                    continue
                label, callback = item
                user32.AppendMenuW(menu, _MF_STRING, command_id, label)
                actions[command_id] = callback
                command_id += 1
            point = self._cursor_pos()
            user32.SetForegroundWindow(self._hwnd)
            chosen = user32.TrackPopupMenu(
                menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD, point[0], point[1], 0, self._hwnd, None
            )
            user32.PostMessageW(self._hwnd, 0, 0, 0)  # 让菜单正常消失（Windows 的老规矩）
        finally:
            user32.DestroyMenu(menu)
        if chosen:
            self._call(actions.get(int(chosen)))

    @staticmethod
    def _cursor_pos() -> tuple[int, int]:
        from ctypes import wintypes

        point = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))  # type: ignore[attr-defined]
        return int(point.x), int(point.y)

    def _modify(self, *, flags: int, title: str = "", message: str = "") -> None:
        """NIM_MODIFY：更新提示或弹气泡（线程安全：只改图标记录，不碰回调）。"""
        if not self._hwnd:
            return
        with self._lock:
            data = self._new_icon_data()
            data.uFlags = flags
            if flags & _NIF_TIP:
                data.szTip = self.tooltip[:127]
            if flags & _NIF_INFO:
                data.szInfo = (message or "")[:255]
                data.szInfoTitle = (title or self.tooltip)[:63]
                data.dwInfoFlags = _NIIF_INFO | _NIIF_USER
                data.uTimeout = 5000
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass

    @staticmethod
    def _call(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - 托盘菜单回调失败不能弄死消息循环
            pass


def _selftest() -> int:
    """``python -m atri_x.gui.tray``：挂一个图标 2 秒再撤掉（人工验证用）。"""
    events: list[str] = []
    tray = TrayIcon(
        "ATRI-X 托盘自检",
        [("显示主窗口", lambda: events.append("show")), None, ("退出", lambda: events.append("quit"))],
        on_activate=lambda: events.append("activate"),
    )
    if not tray.start():
        print(f"TRAY_SELFTEST_FAIL {tray.error}")
        return 1
    try:
        for _ in range(10):
            time.sleep(0.2)
        tray.notify("ATRI-X", "托盘自检通过")
        time.sleep(1.0)
    finally:
        tray.stop()
    print("TRAY_SELFTEST_OK")
    return 0


if __name__ == "__main__":  # pragma: no cover - 人工自检入口
    raise SystemExit(_selftest())
