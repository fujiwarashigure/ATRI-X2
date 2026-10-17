"""服务实例标记：防止同一台机器上跑起第二个 ``atri-x`` service。

对应 Go：``internal/app/service_marker_common.go`` +
``service_marker_windows.go`` + ``service_marker_unix.go``。

Go 用两种内核原语实现"进程死了锁自动释放"：

* Windows：命名互斥体 ``Local\\atrix_service_marker``；
* POSIX：``flock`` 一个 pid 文件。

Python 标准库没有命名互斥体（那要走 ``ctypes`` 调 ``kernel32``，风险与收益不匹配），
因此**统一用文件锁**：

* Windows：``msvcrt.locking``（``LK_NBLCK``）；
* POSIX：``fcntl.flock``（``LOCK_EX | LOCK_NB``）。

两者都由内核在进程退出时释放，语义与 Go 的 POSIX 分支一致 —— 也就是说
Windows 侧从"命名互斥体"改成"文件锁"，这是与 Go 的**明确偏差**，但保留了
"陈旧标记不会挡住新进程"这一关键性质（Go 的注释里也是这么描述 POSIX 分支的）。

标记路径与 Go ``serviceMarkerPath`` 一致：``$XDG_RUNTIME_DIR/atri-x/atri-x.pid``；
没有 ``XDG_RUNTIME_DIR`` 时退到 ``<temp>/atri-x-<uid>/atri-x/atri-x.pid``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from atri_x.errors import AtriXError

__all__ = [
    "ServiceMarkerError",
    "ServiceMarker",
    "service_marker_path",
    "read_service_marker",
    "service_marker_running",
    "claim_service_marker",
]

#: Windows 侧 Go 用的互斥体名，保留成常量以便运维脚本检索/对照。
SERVICE_MARKER_MUTEX_NAME = r"Local\atrix_service_marker"

_MARKER_DIR_NAME = "atri-x"
_MARKER_FILE_NAME = "atri-x.pid"

#: 锁的字节偏移。**不能是 0**：Windows 的 ``msvcrt.locking`` 是强制字节区间锁，
#: 被锁的那一个字节连同一个进程的另一个句柄都读不到，而 pid 文本就写在文件开头，
#: 锁 0 会让 :func:`read_service_marker` 读不出 pid（POSIX 的 ``flock`` 无此问题）。
#: 锁在 64 字节处：既在 pid 文本（≤20 字节）之后，又允许锁到 EOF 之外。
_LOCK_OFFSET = 64


def _lock(fileobj: Any) -> bool:
    """尝试对已打开的文件对象加排他非阻塞锁；成功返回 True。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            fileobj.seek(_LOCK_OFFSET)
            msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fileobj: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            fileobj.seek(_LOCK_OFFSET)
            msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class ServiceMarkerError(AtriXError):
    """服务标记相关错误（含"已在运行"）。对应 Go 里这一层的 ``fmt.Errorf``。"""


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        try:
            return int(getuid())
        except Exception:  # noqa: BLE001
            return 0
    return 0


def service_marker_path() -> str:
    """对应 Go ``serviceMarkerPath``。"""
    base = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if not base:
        import tempfile

        base = os.path.join(tempfile.gettempdir(), f"atri-x-{_current_uid()}")
    return os.path.join(base, _MARKER_DIR_NAME, _MARKER_FILE_NAME)


def read_service_marker(path: str) -> int:
    """读出标记里的 pid；失败返回 0（对应 Go 的 ``(0, err)``）。"""
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    try:
        pid = int(data.strip())
    except ValueError:
        return 0
    return pid if pid > 0 else 0


class ServiceMarker:
    """持锁的标记句柄。``close()`` 放锁。对应 Go ``*serviceMarker``。"""

    def __init__(self, path: str, fileobj: Any) -> None:
        self.path = path
        self._file = fileobj

    @property
    def closed(self) -> bool:
        return self._file is None

    def close(self) -> None:
        """对应 Go ``Close``；幂等。"""
        if self._file is None:
            return
        fileobj, self._file = self._file, None
        try:
            _unlock(fileobj)
        finally:
            try:
                fileobj.close()
            except OSError:
                pass

    # Go 命名别名。
    Close = close


def service_marker_running() -> bool:
    """探测是否已有实例持有标记。对应 Go ``serviceMarkerRunning``。

    只读探测：无论成功与否都不留下自己的锁。
    """
    path = service_marker_path()
    try:
        fileobj = open(path, "a+b")  # noqa: SIM115 - 句柄要么给 ServiceMarker，要么立刻关
    except OSError:
        return False
    try:
        if not _lock(fileobj):
            return True
        _unlock(fileobj)
        return False
    finally:
        try:
            fileobj.close()
        except OSError:
            pass


def claim_service_marker() -> ServiceMarker:
    """抢占服务标记。对应 Go ``claimServiceMarker``。

    已被占用时抛 :class:`ServiceMarkerError`，错误文案里带上对方 pid（若可读）。
    """
    path = service_marker_path()
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ServiceMarkerError(f"create service marker directory: {exc}") from exc
    try:
        fileobj = open(path, "a+b")
    except OSError as exc:
        raise ServiceMarkerError(f"open service marker: {exc}") from exc
    if not _lock(fileobj):
        try:
            fileobj.close()
        except OSError:
            pass
        pid = read_service_marker(path)
        if pid:
            raise ServiceMarkerError(
                f"atri-x service already appears to be running with pid {pid}"
            )
        raise ServiceMarkerError("atri-x service already appears to be running")
    try:
        fileobj.seek(0)
        fileobj.truncate(0)
        fileobj.write(f"{os.getpid()}\n".encode("ascii"))
        fileobj.flush()
    except OSError as exc:
        _unlock(fileobj)
        try:
            fileobj.close()
        except OSError:
            pass
        raise ServiceMarkerError(f"write service marker: {exc}") from exc
    return ServiceMarker(path, fileobj)


# Go 命名别名。
serviceMarkerPath = service_marker_path
readServiceMarker = read_service_marker
serviceMarkerRunning = service_marker_running
claimServiceMarker = claim_service_marker
