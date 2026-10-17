"""媒体图片工作进程（隔离压缩的执行器）。

对应 Go：``internal/media/worker.go`` + ``worker_process_unix.go`` +
``worker_process_windows.go``。

Go 的做法
---------
``compressImageIsolated`` 用 ``os.Executable()`` 重新执行**自己**并带上魔法参数
``__atrix_media_worker``（``worker.go`` 的 ``init()`` 会让进程直接变成工作进程），
把请求 JSON 从 stdin 灌进去、从 stdout 读回结果；60 秒超时后
``killWorkerProcessTree`` 按平台分支整棵杀掉（Windows 走
``taskkill /T /F /PID``，Unix 走 ``setpgid`` + ``kill(-pid)``）。
好处是病态图片（巨大画布、解码炸弹）最多毁掉子进程。

Python 侧的取舍（**有意偏离**，写在这里供集成期复核）
--------------------------------------------------
1. **用线程而不是子进程**：本机沙箱禁止子进程捕获管道（``stdio=pipe`` 会
   ``EPERM``），``multiprocessing`` 的默认传输也用管道；因此子进程方案在目标
   运行环境里不可靠。本仓改成 ``asyncio.to_thread`` 执行同一份纯 Python 压缩。
2. **代价**：线程与主进程共享堆，因此"内存隔离"这一层保护**没有**了；超时后
   线程也**无法**被强杀（Python 没有安全的线程 kill），只能放弃等待。
   为了不让显存/内存放大，闸门（``VISION_IMAGE_MAX_CONCURRENT_DECODES`` = 10）
   在 Python 侧从"vision 预处理专用"提升为**所有**隔离压缩共用 —— Go 靠子进程
   天然隔离且不限并发，本仓必须显式限流。
3. **保留 JSON 信封**：``run_image_worker`` / ``ImageWorkerRequest`` /
   ``ImageWorkerResponse`` 逐字保留 Go 的协议，可以直接用
   ``python -m atri_x.media.worker`` 起一个工作进程（便于诊断与将来换回子进程），
   但**当前 in-process 路径不经过它**（避免大图白搭一次 base64 往返）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import weakref
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Sequence

from atri_x.media.types import MediaError, raise_if_cancelled, wait_cancelled

__all__ = [
    "IMAGE_WORKER_ARG",
    "IMAGE_WORKER_TIMEOUT",
    "ImageWorkerRequest",
    "ImageWorkerResponse",
    "decode_worker_request",
    "decode_worker_response",
    "encode_worker_request",
    "encode_worker_response",
    "run_image_worker",
    "run_isolated",
    "truncate_worker_error",
]

#: 工作进程的魔法参数。对应 Go ``imageWorkerArg``。
IMAGE_WORKER_ARG = "__atrix_media_worker"
#: 隔离压缩的超时秒数。对应 Go ``compressImageIsolated`` 里的 60s。
IMAGE_WORKER_TIMEOUT = 60.0
#: 与 :data:`atri_x.media.image.VISION_IMAGE_MAX_CONCURRENT_DECODES` 同量级。
#: 刻意在这里重复字面量，避免 worker ← image 的循环 import。
_GATE_SLOTS = 10

#: 每个事件循环一把闸门：``asyncio.Semaphore`` 首次阻塞时会绑定 loop，
#: 而测试（``IsolatedAsyncioTestCase``）每个用例都是一个新 loop。
_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(_GATE_SLOTS)
        _gates[loop] = gate
    return gate


# ---------------------------------------------------------------------------
# 工作进程协议（与 Go 逐字一致）
# ---------------------------------------------------------------------------


@dataclass
class ImageWorkerRequest:
    """工作进程请求。对应 Go ``imageWorkerRequest``（JSON 键同名）。"""

    data: str = ""
    max_bytes: int = 0
    max_length: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"data": self.data, "max_bytes": self.max_bytes, "max_length": self.max_length}

    @classmethod
    def from_json(cls, raw: Any) -> "ImageWorkerRequest":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            data=str(raw.get("data") or ""),
            max_bytes=int(raw.get("max_bytes") or 0),
            max_length=int(raw.get("max_length") or 0),
        )


@dataclass
class ImageWorkerResponse:
    """工作进程响应。对应 Go ``imageWorkerResponse``（两个字段都 omitempty）。"""

    data: str = ""
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.data:
            out["data"] = self.data
        if self.error:
            out["error"] = self.error
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "ImageWorkerResponse":
        raw = raw if isinstance(raw, dict) else {}
        return cls(data=str(raw.get("data") or ""), error=str(raw.get("error") or ""))


def encode_worker_request(data: bytes, max_bytes: int, max_length: int) -> bytes:
    """构造 stdin 载荷。对应 Go 的 ``json.Marshal(imageWorkerRequest{...})``。"""
    request = ImageWorkerRequest(
        data=base64.b64encode(bytes(data)).decode("ascii"),
        max_bytes=int(max_bytes),
        max_length=int(max_length),
    )
    return json.dumps(request.to_json(), separators=(",", ":")).encode("utf-8")


def decode_worker_request(payload: bytes) -> ImageWorkerRequest:
    """解析 stdin 载荷。对应 Go ``runImageWorker`` 的 JSON 解码。"""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MediaError(f"decode image worker request: {exc}") from exc
    return ImageWorkerRequest.from_json(raw)


def encode_worker_response(response: ImageWorkerResponse) -> bytes:
    """构造 stdout 载荷。对应 Go 的 ``json.NewEncoder(stdout).Encode(response)``。"""
    return json.dumps(response.to_json(), separators=(",", ":")).encode("utf-8")


def decode_worker_response(payload: bytes) -> ImageWorkerResponse:
    """解析 stdout 载荷。对应 Go ``compressImageIsolated`` 的响应解码。"""
    try:
        raw = json.loads(payload.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise MediaError(f"decode image worker response: {exc}") from exc
    return ImageWorkerResponse.from_json(raw)


def truncate_worker_error(text: str, limit: int = 512) -> str:
    """截断子进程 stderr，避免把整段日志塞进错误消息。对应 Go ``truncateWorkerError``。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def execute_worker_request(request: ImageWorkerRequest) -> ImageWorkerResponse:
    """执行一次工作进程请求，返回响应（不涉及任何 IO）。

    对应 Go ``runImageWorker`` 里"解 base64 → compressImage → 组响应"的三步。
    """
    # 函数内 import：image.py 需要 worker.run_isolated，模块级 import 会成环。
    from atri_x.media.image import compress_image

    try:
        data = base64.b64decode(request.data, validate=True)
    except (ValueError, TypeError) as exc:
        raise MediaError(f"decode image worker data: {exc}") from exc
    try:
        compressed = compress_image(data, request.max_bytes, request.max_length)
    except Exception as exc:  # noqa: BLE001 - 与 Go 一致：失败写进响应体而不是崩掉进程
        return ImageWorkerResponse(error=str(exc))
    return ImageWorkerResponse(data=base64.b64encode(compressed).decode("ascii"))


def run_image_worker(stdin: BinaryIO, stdout: BinaryIO) -> None:
    """跑一次工作进程。对应 Go ``runImageWorker``。

    调用方（``python -m atri_x.media.worker``）负责把异常写到 stderr 并以非零码退出。
    """
    request = decode_worker_request(stdin.read())
    response = execute_worker_request(request)
    stdout.write(encode_worker_response(response))
    stdout.flush()


# ---------------------------------------------------------------------------
# 隔离执行
# ---------------------------------------------------------------------------


async def run_isolated(
    ctx: Any,
    task: Callable[..., Any],
    *args: Any,
    timeout: float | None = IMAGE_WORKER_TIMEOUT,
) -> Any:
    """在受闸门保护的线程里跑 ``task(*args)``。对应 Go ``compressImageIsolated`` 的骨架。

    * ``ctx`` 在进闸门前、进闸门后各查一次；``task`` 本身跑在线程里，**不**响应 ctx。
    * ``timeout`` 为正时超时抛 :class:`atri_x.media.types.MediaError`；线程仍在
      后台跑完（Python 无法安全终止线程，见模块 docstring）。
    """
    raise_if_cancelled(ctx)
    gate = _gate()
    await _acquire_gate(gate, ctx)
    try:
        raise_if_cancelled(ctx)
        future = asyncio.ensure_future(asyncio.to_thread(task, *args))
        if timeout is None:
            return await future
        try:
            return await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise MediaError(f"image worker timed out after {timeout:g}s") from exc
    finally:
        gate.release()


async def _acquire_gate(gate: asyncio.Semaphore, ctx: Any) -> None:
    """获取闸门槽位，等待期间可被 ctx 取消。"""
    waiter = None if ctx is None else wait_cancelled(ctx)
    if waiter is None:
        await gate.acquire()
        return
    acquire_task = asyncio.ensure_future(gate.acquire())
    cancel_task = asyncio.ensure_future(waiter)
    try:
        await asyncio.wait({acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (acquire_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
    if acquire_task.done() and not acquire_task.cancelled() and acquire_task.exception() is None:
        raise_if_cancelled(ctx)
        return
    raise_if_cancelled(ctx)
    raise asyncio.CancelledError("image worker gate acquisition aborted")


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m atri_x.media.worker`` 的入口。对应 Go ``init()`` 的魔法参数分支。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] != IMAGE_WORKER_ARG:
        sys.stderr.write(f"unknown media worker argument: {args[0]}\n")
        return 2
    try:
        run_image_worker(sys.stdin.buffer, sys.stdout.buffer)
    except Exception as exc:  # noqa: BLE001 - 与 Go 一致：写 stderr 并 exit 1
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
