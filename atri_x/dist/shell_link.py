"""创建/读取 Windows 快捷方式（``.lnk``），纯 ``ctypes`` 调 COM。

安装器需要把「开始菜单」和「桌面」快捷方式指向程序，而 Python 标准库没有这层
API。这里直接实现 ``IShellLinkW`` + ``IPersistFile`` 的最小调用面：

- :func:`create_shortcut` —— 写一个 ``.lnk``（目标、参数、工作目录、描述、图标）；
- :func:`read_shortcut_target` —— 读回目标路径（安装器 ``status`` 与测试用）。

调用约定：进程内首次调用时 ``CoInitialize``（幂等，不 ``CoUninitialize``——COM 在
线程上初始化一次即可，安装器是短命进程）。非 Windows 平台抛 :class:`ShortcutError`。
"""

from __future__ import annotations

import ctypes
import os
import uuid

try:  # ``ctypes.wintypes`` 只在 Windows 存在；非 Windows 上模块仍要能导入
    from ctypes import wintypes
except (ImportError, ValueError):  # pragma: no cover - 非 Windows
    wintypes = None  # type: ignore[assignment]

__all__ = ["ShortcutError", "create_shortcut", "read_shortcut_target", "shortcut_available"]

#: COM 常量。
_CLSCTX_INPROC_SERVER = 0x1
_S_OK = 0

#: ``IShellLinkW`` 的 CLSID / IID（与 Windows SDK 一致）。
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"

#: vtable 槽位。
_VT_SET_DESCRIPTION = 7
_VT_SET_WORKING_DIR = 9
_VT_SET_ARGUMENTS = 11
_VT_SET_ICON_LOCATION = 17
_VT_SET_PATH = 20
_VT_GET_PATH = 3
_VT_QUERY_INTERFACE = 0
_VT_RELEASE = 2
_VT_PERSIST_LOAD = 5
_VT_PERSIST_SAVE = 6

#: ``CoInitialize`` 是否已在本进程调用过。
_COM_READY = False


class ShortcutError(Exception):
    """快捷方式创建/读取失败。"""


def shortcut_available() -> bool:
    """当前平台是否能创建快捷方式。"""
    return os.name == "nt" and hasattr(ctypes, "windll")


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        value = uuid.UUID(text)
        guid = cls()
        guid.Data1 = value.time_low
        guid.Data2 = value.time_mid
        guid.Data3 = value.time_hi_version
        body = value.bytes[8:]
        for index, byte in enumerate(body):
            guid.Data4[index] = byte
        return guid


def _alloc_guid(text: str) -> ctypes.POINTER[_GUID]:  # pragma: no cover - 仅调试/测试辅助
    """把 GUID 文本转成指针（调用方负责让源对象在调用期间存活）。"""
    guid = _GUID.parse(text)
    return ctypes.cast(ctypes.byref(guid), ctypes.POINTER(_GUID))


def _ensure_com() -> None:
    global _COM_READY
    if _COM_READY:
        return
    hr = ctypes.windll.ole32.CoInitialize(None)  # type: ignore[attr-defined]
    if hr not in (0, 1):  # S_OK / S_FALSE
        raise ShortcutError(f"CoInitialize 失败：0x{hr & 0xFFFFFFFF:08X}")
    _COM_READY = True


def _vtable(pointer: int, index: int, restype: object, argtypes: list[object]) -> object:
    """取 COM 接口第 ``index`` 个槽位的函数指针。"""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    entry = table.contents[index]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(entry)


def _co_create(clsid: str, iid: str) -> int:
    """``CoCreateInstance``，返回接口指针。"""
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    clsid_value = _GUID.parse(clsid)
    iid_value = _GUID.parse(iid)
    pointer = ctypes.c_void_p()
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    hr = ole32.CoCreateInstance(
        ctypes.byref(clsid_value),
        None,
        wintypes.DWORD(_CLSCTX_INPROC_SERVER),
        ctypes.byref(iid_value),
        ctypes.byref(pointer),
    )
    if hr != _S_OK or not pointer.value:
        raise ShortcutError(f"CoCreateInstance 失败：0x{hr & 0xFFFFFFFF:08X}")
    return int(pointer.value)


def _release(pointer: int) -> None:
    try:
        function = _vtable(pointer, _VT_RELEASE, ctypes.c_ulong, [])
        function(pointer)  # type: ignore[operator]
    except (OSError, ValueError, AttributeError):
        pass


def _set(pointer: int, slot: int, value: str) -> None:
    function = _vtable(pointer, slot, ctypes.c_long, [wintypes.LPCWSTR])
    hr = function(pointer, value)  # type: ignore[operator]
    if hr != _S_OK:
        raise ShortcutError(f"IShellLinkW 调用失败（slot {slot}）：0x{hr & 0xFFFFFFFF:08X}")


def _query_persist_file(link: int) -> int:
    iid = _GUID.parse(_IID_IPERSISTFILE)
    pointer = ctypes.c_void_p()
    function = _vtable(
        link, _VT_QUERY_INTERFACE, ctypes.c_long, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    )
    hr = function(link, ctypes.byref(iid), ctypes.byref(pointer))  # type: ignore[operator]
    if hr != _S_OK or not pointer.value:
        raise ShortcutError(f"QueryInterface(IPersistFile) 失败：0x{hr & 0xFFFFFFFF:08X}")
    return int(pointer.value)


def create_shortcut(
    path: str,
    target: str,
    *,
    arguments: str = "",
    working_dir: str = "",
    description: str = "",
    icon_path: str = "",
) -> str:
    """创建 ``.lnk``；返回快捷方式绝对路径。失败抛 :class:`ShortcutError`。"""
    if not shortcut_available():
        raise ShortcutError("当前平台不支持创建快捷方式")
    destination = os.path.abspath(path)
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    _ensure_com()
    link = _co_create(_CLSID_SHELL_LINK, _IID_ISHELLLINKW)
    try:
        _set(link, _VT_SET_PATH, os.path.abspath(target))
        if arguments:
            _set(link, _VT_SET_ARGUMENTS, arguments)
        if working_dir:
            _set(link, _VT_SET_WORKING_DIR, os.path.abspath(working_dir))
        if description:
            _set(link, _VT_SET_DESCRIPTION, description)
        if icon_path:
            set_icon = _vtable(
                link, _VT_SET_ICON_LOCATION, ctypes.c_long, [wintypes.LPCWSTR, ctypes.c_int]
            )
            set_icon(link, os.path.abspath(icon_path), 0)  # type: ignore[operator]
        persist = _query_persist_file(link)
        try:
            save = _vtable(
                persist, _VT_PERSIST_SAVE, ctypes.c_long, [wintypes.LPCWSTR, wintypes.BOOL]
            )
            hr = save(persist, destination, True)  # type: ignore[operator]
            if hr != _S_OK:
                raise ShortcutError(f"IPersistFile::Save 失败：0x{hr & 0xFFFFFFFF:08X}")
        finally:
            _release(persist)
    finally:
        _release(link)
    return destination


def read_shortcut_target(path: str) -> str:
    """读 ``.lnk`` 的目标路径；失败返回空串（不抛异常，供状态展示用）。"""
    if not shortcut_available() or not os.path.isfile(path):
        return ""
    try:
        _ensure_com()
        link = _co_create(_CLSID_SHELL_LINK, _IID_ISHELLLINKW)
    except ShortcutError:
        return ""
    try:
        persist = _query_persist_file(link)
        try:
            load = _vtable(
                persist, _VT_PERSIST_LOAD, ctypes.c_long, [wintypes.LPCWSTR, wintypes.DWORD]
            )
            if load(persist, os.path.abspath(path), 0) != _S_OK:  # type: ignore[operator]
                return ""
            get_path = _vtable(
                link, _VT_GET_PATH, ctypes.c_long, [wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            )
            buffer = ctypes.create_unicode_buffer(1024)
            if get_path(link, buffer, 1024, None, 0) != _S_OK:  # type: ignore[operator]
                return ""
            return buffer.value
        finally:
            _release(persist)
    finally:
        _release(link)
