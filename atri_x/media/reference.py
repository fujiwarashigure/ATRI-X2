"""引用持有与工具参数引用。

对应 Go：``internal/media/reference.go``。

* :func:`retain_session_tool_arguments` —— ToolRun 在"已确认并即将执行"时把最终
  参数 JSON 里**完整且有效**的媒体 ID 以 ``session_tool`` owner 原子关联到当前
  Session。同一 Session/媒体幂等（主键去重），失败执行仍保留，执行前拒绝/跳过不关联。
* :func:`hold` —— 给"正在读取/发送"的消费者一个独立于持久化 owner 的临时引用
  （``owner_type="request"``、``purpose="temporary"``）。
* :class:`ReferencedReader` —— 把流的关闭与引用释放绑在一起，``close()`` 幂等。

同步 vs 异步（**有意偏离**）
--------------------------
Go 里这三个入口都收 ``ctx`` 并返回 ``error``。它们底下是**同步 SQLite**调用
（``media_references`` 的插入/删除），因此本仓把它们做成同步方法：``hold`` 的
释放函数必须能在同步清理路径（``ResolveForOutput`` 的 cleanup、``finally``）
里直接调用，做成协程会强迫每个调用点都去 await 一个纯 DB 操作。
Go ``Hold`` 的释放用 5 秒超时 ctx；本仓同步调用不需要它。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from atri_x.media.types import MediaError, valid_id
from atri_x.storage.ids import new_id
from atri_x.storage.models import MediaReference

__all__ = [
    "ReferencedReader",
    "argument_media_ids",
    "hold",
    "retain_session_tool_arguments",
]

#: ToolRun 关联工具参数媒体时用的 owner 类型。对应 Go 字面量 ``"session_tool"``。
SESSION_TOOL_OWNER = "session_tool"
#: 工具参数媒体的 purpose。对应 Go 字面量 ``"input"``。
TOOL_INPUT_PURPOSE = "input"
#: 临时持有的 owner 类型 / purpose。对应 Go ``Hold`` 里的字面量。
REQUEST_OWNER = "request"
TEMPORARY_PURPOSE = "temporary"


def retain_session_tool_arguments(manager: Any, session_id: str, arguments: str) -> None:
    """把参数里的媒体 ID 关联到 Session。对应 Go ``RetainSessionToolArguments``。

    只认**精确的 JSON 值**（不扫自由文本、不做前缀匹配）：``{"prompt": "prefix media:…"}``
    里那个嵌在字符串中的 ID 不会被关联。整批通过 ``add_all`` 单事务写入，
    因此出现不存在的媒体 ID 时整批回滚（Go 的测试断言"部分引用"为空）。
    """
    if not session_id:
        raise MediaError("session ID is required for tool media references")
    ids = argument_media_ids(arguments)
    if not ids:
        return
    created_at = manager.now()
    references = [
        MediaReference(
            media_id=id,
            owner_type=SESSION_TOOL_OWNER,
            owner_id=session_id,
            purpose=TOOL_INPUT_PURPOSE,
            session_id=session_id,
            created_at=created_at,
        )
        for id in ids
    ]
    manager.store.media_references().add_all(references)


def argument_media_ids(arguments: str) -> list[str]:
    """递归取出参数 JSON 里的完整媒体 ID（去重后升序）。对应 Go ``argumentMediaIDs``。"""
    try:
        value = json.loads(arguments)
    except (ValueError, TypeError):
        return []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            if valid_id(node):
                seen.add(node)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item)
            return
        if isinstance(node, dict):
            for item in node.values():
                visit(item)

    visit(value)
    return sorted(seen)


def hold(manager: Any, id: str) -> Callable[[], None]:
    """持有临时引用，返回幂等的 ``release``。对应 Go ``Hold``。"""
    reference = MediaReference(
        media_id=id,
        owner_type=REQUEST_OWNER,
        owner_id=new_id(),
        purpose=TEMPORARY_PURPOSE,
    )
    manager.add_reference(reference)
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        manager.remove_reference(reference)

    return release


class ReferencedReader:
    """把流的关闭与引用释放绑在一次。对应 Go ``referencedReader``。"""

    def __init__(self, stream: Any, release: Callable[[], None]) -> None:
        self.stream = stream
        self._release = release
        self._closed = False

    # -- io.ReadCloser 表面 -------------------------------------------------
    def read(self, size: int = -1) -> bytes:
        """按文件对象语义读取（``size < 0`` = 读到底）。"""
        if size is None or size < 0:
            return self.stream.read()
        return self.stream.read(size)

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        """关闭底层流并释放引用；幂等，失败时抛出合并后的异常。

        Go ``referencedReader.Close`` 用 ``errors.Join(Close(), release())``；
        本仓用 :class:`ExceptionGroup`（只有一个失败时直接抛它）。
        """
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        try:
            self.stream.close()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        try:
            self._release()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        if not failures:
            return
        if len(failures) == 1:
            raise failures[0]
        raise BaseExceptionGroup("close media reader failed", failures)  # noqa: B904

    # -- 上下文管理器 -------------------------------------------------------
    def __enter__(self) -> "ReferencedReader":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            self.close()
        except BaseException:  # noqa: BLE001 - 关闭失败不能掩盖体内异常
            if exc is None:
                raise
        return False

    @property
    def closed(self) -> bool:
        return self._closed
