"""``hook.v2`` 的按行读取（带帧上限）。

对应 Go：``internal/hook/rules/exec.go`` 里的 ``readProtocolLine`` 与
``internal/hook/runtime/worker.go`` 里的 ``bufio.Scanner`` 配置。

Go 用 ``bufio.Reader.ReadSlice`` 反复拼接到 ``\\n``，或者用带
``scanner.Buffer(nil, MaxProtocolFrameBytes)`` 的 ``Scanner``；Python 的
``asyncio.StreamReader`` 默认行上限只有 64 KiB，所以这里显式实现一份：
按块读取、自己找换行、超限报错、EOF 时把残余数据当作最后一行返回
（与 ``bufio.Scanner`` 在 EOF 的行为一致）。
"""

from __future__ import annotations

import os
from typing import Any

from atri_x.errors import AtriXError
from atri_x.hook.event import MAX_PROTOCOL_FRAME_BYTES

__all__ = [
    "MAX_PROTOCOL_FRAME_BYTES",
    "ProtocolFrameError",
    "LineReader",
    "short_protocol_line",
    "await_cancelable",
    "detached_process_kwargs",
    "kill_process_group",
]

_READ_CHUNK = 64 * 1024


class ProtocolFrameError(AtriXError):
    """一帧超过上限或读取失败。对应 Go 里这一层的 ``fmt.Errorf``。"""


class LineReader:
    """从字节流里按行读取，单帧不超过 ``max_bytes``。"""

    def __init__(self, stream: Any, max_bytes: int = MAX_PROTOCOL_FRAME_BYTES) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._eof = False

    async def read_line(self) -> bytes | None:
        """返回下一行（含结尾换行）；流已结束时返回 ``None``。"""
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return line
            if self._eof:
                if not self._buffer:
                    return None
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            chunk = await self._stream.read(_READ_CHUNK)
            if not chunk:
                self._eof = True
                continue
            self._buffer.extend(chunk)
            if len(self._buffer) > self._max_bytes:
                raise ProtocolFrameError(
                    f"hook protocol stdout frame exceeds "
                    f"{self._max_bytes // (1024 * 1024)} MiB limit"
                )


def short_protocol_line(line: bytes | str) -> str:
    """把一行截断到 240 个 rune，用于错误信息。对应 Go ``shortProtocolLine``。"""
    if isinstance(line, (bytes, bytearray)):
        text = bytes(line).decode("utf-8", "replace")
    else:
        text = line
    text = text.strip()
    max_runes = 240
    if len(text) <= max_runes:
        return text
    return text[:max_runes] + "..."


async def await_cancelable(awaitable: Any, token: Any) -> Any:
    """等待 ``awaitable``；``token`` 取消时抛它自己的错误。

    对应 Go 里"``select`` 在 channel 与 ``ctx.Done()`` 之间二选一"的写法。
    ``token`` 为 ``None`` 或没有 ``add_callback`` 时退化为普通 ``await``。
    """
    if token is None or not hasattr(token, "add_callback"):
        return await awaitable
    import asyncio

    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()

    def _cancel() -> None:
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # pragma: no cover - 循环已关闭
            pass

    remove = token.add_callback(_cancel)
    try:
        return await task
    except asyncio.CancelledError:
        error = None
        checker = getattr(token, "is_cancelled", None)
        if callable(checker) and checker():
            getter = getattr(token, "error", None)
            if callable(getter):
                error = getter()
        if error is not None:
            raise error from None
        raise
    finally:
        remove()


def detached_process_kwargs(platform: str | None = None) -> dict[str, Any]:
    """``create_subprocess_exec`` 的「独立会话」参数：POSIX 上让子进程自成进程组。

    为什么必须有：POSIX 上插件超时/停止靠 ``killpg(os.getpgid(pid), SIGKILL)`` 杀整棵
    进程树；子进程若与宿主同组，这一枪打中的是**宿主自己所在的进程组**——Linux 上
    hook 插件一超时就把 ATRI-X 服务（或正在跑测试的进程）一起 SIGKILL 掉，表现是
    「CI job 卡死/服务整机掉线」。放进新会话后 killpg 只命中插件自己那棵树。

    Windows 不走这条路径（用 ``taskkill /T /PID``），返回空字典。
    """
    name = os.name if platform is None else platform
    return {"start_new_session": True} if name != "nt" else {}


def kill_process_group(pid: int) -> None:
    """只朝**子进程自己的**进程组发 SIGKILL；绝不误伤宿主所在的组。

    防御性兜底：万一某个调用方忘了用 :func:`detached_process_kwargs`（子进程与宿主同
    组），这里直接放弃 ``killpg``，交给调用方的 ``proc.kill()`` 只杀直接子进程——打死
    宿主自己永远比漏杀几个孙进程更糟。
    """
    import signal

    try:
        group = os.getpgid(int(pid))
    except (OSError, TypeError, ValueError):
        return
    if group == os.getpgrp():
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
