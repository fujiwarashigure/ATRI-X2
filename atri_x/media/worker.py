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

   正因如此，槽位的生命周期必须跟着**底层工作**而不是等待方走：超时/取消只让等待方
   放弃等待，线程仍在跑，槽位就仍然算被占用（:func:`run_isolated` 用
   :func:`asyncio.shield` + 内部 future 的 done 回调做到这一点）。否则"最多 10 个
   槽位"会退化成"最多 10 个等待方"，持续超时可以无限堆积真实存活的工作线程。
   等待方仍然有界：超过 :data:`IMAGE_WORKER_QUEUE_LIMIT` 个等待者直接拒绝，
   排队超过 :data:`IMAGE_WORKER_QUEUE_TIMEOUT` 秒直接放弃。
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
from collections import deque
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Sequence

from atri_x.media.types import MediaError, raise_if_cancelled, wait_cancelled

__all__ = [
    "IMAGE_WORKER_ARG",
    "IMAGE_WORKER_QUEUE_LIMIT",
    "IMAGE_WORKER_QUEUE_TIMEOUT",
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
    "worker_stats",
]

#: 工作进程的魔法参数。对应 Go ``imageWorkerArg``。
IMAGE_WORKER_ARG = "__atrix_media_worker"
#: 隔离压缩的超时秒数。对应 Go ``compressImageIsolated`` 里的 60s。
IMAGE_WORKER_TIMEOUT = 60.0
#: 与 :data:`atri_x.media.image.VISION_IMAGE_MAX_CONCURRENT_DECODES` 同量级。
#: 刻意在这里重复字面量，避免 worker ← image 的循环 import。
_GATE_SLOTS = 10
#: 排队等槽位的等待者数量上限。超出即立刻抛 :class:`MediaError`（消息含
#: ``queue full``），避免无限多的调用方堆在闸门前把内存耗光。
IMAGE_WORKER_QUEUE_LIMIT = 64
#: 排队等槽位的最长秒数；超时抛 :class:`MediaError`（消息含 ``queue timed out``）。
#: 它约束的是"拿到槽位**之前**"的等待，不是工作本身的执行时间
#: （那是 :data:`IMAGE_WORKER_TIMEOUT`）。
IMAGE_WORKER_QUEUE_TIMEOUT = 60.0

#: 每个事件循环一把闸门：asyncio 的同步原语会绑定 loop，而测试
#: （``IsolatedAsyncioTestCase``）每个用例都是一个新 loop。
_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Gate]" = (
    weakref.WeakKeyDictionary()
)

#: 正在跑的内部 future 的强引用集合。``asyncio`` 内部只保留弱引用，
#: 等待方超时/取消后就没人引用它们了 —— 如果不在这里按住，
#: 孤儿任务可能在跑完（并归还槽位）之前被 GC，还会留下
#: "exception was never retrieved"。done 回调里会把它移出去。
_live_futures: "set[asyncio.Future[Any]]" = set()


class _Gate:
    """每个事件循环一把的槽位闸门（显式记账）。

    语义：``active`` 是**已占用**的槽位数，它只在 :meth:`release` 时下降；
    槽位可以"直接交接"给队首等待者（``active`` 不变），交接与取消的竞态因此
    是可判定的：等待者恢复前被取消时，由 :func:`_wait_for_slot` 把槽位还回来。

    用 ``asyncio.Semaphore`` 也能限流，但它不暴露"谁在排队"，读不出
    :func:`worker_stats` 需要的诊断数据，也说不清"槽位是否已经交接给我"。
    """

    __slots__ = (
        "slots",
        "active",
        "waiting",
        "orphaned",
        "timed_out",
        "rejected",
        "queue_timed_out",
        "completed",
        "_waiters",
    )

    def __init__(self, slots: int) -> None:
        self.slots = max(1, int(slots))
        #: 已占用、且底层工作**尚未真正结束**的槽位数。
        self.active = 0
        #: 正在排队等槽位的等待者数（含刚交接、还没恢复的那一个）。
        self.waiting = 0
        #: 等待方已放弃（超时/取消）但底层工作仍在跑的数量（孤儿）。
        self.orphaned = 0
        #: 累计的工作超时次数。
        self.timed_out = 0
        #: 累计因排队上限被拒的次数。
        self.rejected = 0
        #: 累计排队超时次数。
        self.queue_timed_out = 0
        #: 累计真正跑完（无论成功/失败）的工作数。
        self.completed = 0
        self._waiters: "deque[asyncio.Future[None]]" = deque()

    # -- 槽位 ---------------------------------------------------------------

    def try_acquire(self) -> bool:
        """非阻塞抢槽位；成功即 ``active += 1``。"""
        if self.active >= self.slots:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        """归还一个槽位；有人排队时直接交接（该等待者的 ``active`` 不变）。"""
        if self.active <= 0:  # pragma: no cover - 记账错误的兜底
            raise RuntimeError("media worker gate released with no active slot")
        if self._hand_off():
            return
        self.active -= 1

    def _hand_off(self) -> bool:
        """把槽位给队首还活着的等待者。返回是否有人接走。"""
        while self._waiters:
            future = self._waiters.popleft()
            self.waiting -= 1
            if future.done():  # 等待者已放弃（取消/超时）→ 槽位继续往下传
                continue
            future.set_result(None)
            return True
        return False

    # -- 排队 ---------------------------------------------------------------

    def enqueue(self, future: "asyncio.Future[None]") -> None:
        self._waiters.append(future)
        self.waiting += 1

    def dequeue(self, future: "asyncio.Future[None]") -> None:
        try:
            self._waiters.remove(future)
        except ValueError:
            return
        self.waiting -= 1

    # -- 统计 ---------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        return {
            "slots": self.slots,
            "active": self.active,
            "waiting": self.waiting,
            "orphaned": self.orphaned,
            "timed_out": self.timed_out,
            "rejected": self.rejected,
            "queue_timed_out": self.queue_timed_out,
            "completed": self.completed,
        }


def _gate() -> _Gate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _Gate(_GATE_SLOTS)
        _gates[loop] = gate
    return gate


def _running_gate() -> _Gate | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return _gates.get(loop)


def _empty_stats() -> dict[str, int]:
    return {
        "slots": _GATE_SLOTS,
        "active": 0,
        "waiting": 0,
        "orphaned": 0,
        "timed_out": 0,
        "rejected": 0,
        "queue_timed_out": 0,
        "completed": 0,
    }


def worker_stats() -> dict[str, int]:
    """闸门的统计快照（诊断与测试用）。

    字段（与 :meth:`_Gate.stats` 一一对应）：

    * ``slots`` —— 槽位上限；
    * ``active`` —— 正在持有槽位、**尚未真正结束**的工作数（"槽位只在底层工作
      真正结束后释放"这条不变量的观测口径：超时后它不下降，工作结束才回 0）；
    * ``waiting`` —— 正在排队等槽位的调用方；
    * ``orphaned`` —— 等待方已放弃（超时/取消）但工作仍在跑的数量；
    * ``timed_out`` / ``rejected`` / ``queue_timed_out`` / ``completed`` —— 累计计数。

    在事件循环里调用时返回该循环那把闸门的快照；循环外调用时汇总所有已知闸门
    （``slots`` 取最大值）。
    """
    gate = _running_gate()
    if gate is not None:
        return gate.stats()
    merged = _empty_stats()
    for candidate in list(_gates.values()):
        for key, value in candidate.stats().items():
            if key == "slots":
                merged[key] = max(merged[key], value)
            else:
                merged[key] += value
    return merged


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

    * ``ctx`` 在进闸门前、槽位到手后各查一次；``task`` 本身跑在线程里，**不**响应 ctx。
    * **槽位生命周期跟底层工作走**：内部 future 由 :func:`asyncio.shield` 保护，
      等待方超时/被取消时只放弃等待（既不释放槽位、也不取消内部 future），
      槽位在内部 future 的 done 回调里归还。这样 ``_GATE_SLOTS`` 个槽位就等于
      "最多这么多个真实存活的工作"，而不是"最多这么多个等待方"。
    * 超时/取消留下的工作记进 :func:`worker_stats` 的 ``orphaned``，跑完才归零。
    * 排队有界：等待者达到 :data:`IMAGE_WORKER_QUEUE_LIMIT` 抛 MediaError
      （``queue full``）；排队超过 :data:`IMAGE_WORKER_QUEUE_TIMEOUT` 秒抛
      MediaError（``queue timed out``）。
    * ``timeout`` 为正时超时抛 :class:`atri_x.media.types.MediaError`；线程仍在
      后台跑完（Python 无法安全终止线程，见模块 docstring）。
    """
    raise_if_cancelled(ctx)
    gate = _gate()
    # 取消竞态（槽位到手但 ctx 已取消）由 _acquire_gate 自己归还，这里只管结果。
    await _acquire_gate(gate, ctx)
    try:
        future = asyncio.ensure_future(asyncio.to_thread(task, *args))
    except BaseException:  # pragma: no cover - 起任务失败也要还槽位
        gate.release()
        raise
    work = _Work(gate, future)
    _live_futures.add(future)
    future.add_done_callback(work.settled)
    try:
        if timeout is None:
            return await asyncio.shield(future)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            work.abandon(timed_out=True)
            raise MediaError(f"image worker timed out after {timeout:g}s") from exc
    except asyncio.CancelledError:
        # 等待方被取消：内部 future 不动，槽位等到线程真正跑完才释放。
        work.abandon(timed_out=False)
        raise


class _Work:
    """一次隔离工作的记账：从槽位到手到线程真正结束。"""

    __slots__ = ("_gate", "_future", "abandoned", "timed_out")

    def __init__(self, gate: _Gate, future: "asyncio.Future[Any]") -> None:
        self._gate = gate
        self._future = future
        #: 等待方是否已放弃等待（工作本身仍在跑）。
        self.abandoned = False
        self.timed_out = False

    def abandon(self, *, timed_out: bool) -> None:
        """等待方放弃等待：只记账，不碰槽位、不取消内部 future。"""
        if self.abandoned or self._future.done():
            return
        self.abandoned = True
        self.timed_out = bool(timed_out)
        self._gate.orphaned += 1
        if self.timed_out:
            self._gate.timed_out += 1

    def settled(self, future: "asyncio.Future[Any]") -> None:
        """内部 future 的 done 回调：消费异常、清强引用、**归还槽位**。"""
        _live_futures.discard(future)
        if not future.cancelled():
            # 读一次异常：done 回调之后没人再读它，不读就会留下
            # "exception was never retrieved"（等待方可能早已放弃等待）。
            future.exception()
        if self.abandoned:
            self._gate.orphaned = max(0, self._gate.orphaned - 1)
        self._gate.completed += 1
        self._gate.release()


async def _acquire_gate(gate: _Gate, ctx: Any) -> None:
    """获取一个槽位，等待期间可被 ctx 取消。

    取消竞态：槽位可能刚好在 ctx 取消的同一轮事件循环里交接给我们。这里用
    ``acquired`` 显式判定 —— 槽位到手后才发现 ctx 已取消时，**先归还槽位再抛**，
    绝不留下未归还的 acquire（漏一个槽位就永久少一个并发额度）。
    """
    acquired = False
    try:
        await _wait_for_slot(gate, ctx)
        acquired = True
        raise_if_cancelled(ctx)
    except BaseException:
        if acquired:
            gate.release()
        raise


async def _wait_for_slot(gate: _Gate, ctx: Any) -> None:
    """排队等一个槽位；正常返回即表示**已经持有**该槽位。"""
    if gate.try_acquire():
        return
    if gate.waiting >= IMAGE_WORKER_QUEUE_LIMIT:
        gate.rejected += 1
        raise MediaError(
            f"image worker queue full: {gate.waiting} waiters already queued "
            f"(limit {IMAGE_WORKER_QUEUE_LIMIT})"
        )
    slot: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    gate.enqueue(slot)
    try:
        await _await_slot(gate, slot, ctx)
    except BaseException:
        if slot.done() and not slot.cancelled():
            # 槽位已经交接给我们，但我们没能接受它（取消/超时竞态）→ 归还。
            gate.release()
        else:
            gate.dequeue(slot)
        raise


async def _await_slot(gate: _Gate, slot: "asyncio.Future[None]", ctx: Any) -> None:
    """等队首交接槽位；ctx 取消或排队超时都中止等待。

    返回时 ``slot`` 一定已经带着交接结果完成；否则抛异常，由
    :func:`_wait_for_slot` 把可能已经交接过来的槽位还回去。
    """
    timeout = IMAGE_WORKER_QUEUE_TIMEOUT
    watchers: "list[asyncio.Future[Any]]" = [slot]
    cancel_task: "asyncio.Task[Any] | None" = None
    if ctx is not None:
        waiter = wait_cancelled(ctx)
        if waiter is not None:
            cancel_task = asyncio.ensure_future(waiter)
            watchers.append(cancel_task)
    try:
        await asyncio.wait(
            watchers,
            timeout=timeout if timeout and timeout > 0 else None,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if cancel_task is not None:
            if not cancel_task.done():
                cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
    if cancel_task is not None:
        raise_if_cancelled(ctx)  # ctx 赢了竞争：槽位（若已交接）由上层归还
    if slot.done() and not slot.cancelled():
        return
    gate.queue_timed_out += 1
    if timeout and timeout > 0:
        raise MediaError(
            f"image worker queue timed out after {timeout:g}s waiting for a free slot"
        )
    raise MediaError("image worker queue timed out waiting for a free slot")


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
