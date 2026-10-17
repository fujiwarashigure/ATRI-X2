"""导入期的生成参数采集接缝。

对应 Go：``internal/media/generation.go``。

为什么需要这个接缝（``devdocs/code-map.md`` 原文）：采集点必须落在**压缩之前**
（压缩会把超限图片重编码成 JPEG，销毁 PNG 文本块与 EXIF/XMP），落盘点必须落在
**哈希之后**（媒体 ID 那时才存在）。所以 ``import_reader`` 在转码前留下原始字节、
拿到 ID 之后再交给 sink。

``GenerationSink`` 是可选的：``None`` 表示导入只存媒体本身。
``capture_generation`` 跑在导入路径上，**失败只记日志、绝不让导入失败** ——
丢一条参数记录不能连图一起丢。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from atri_x.media.types import raise_if_cancelled

__all__ = [
    "GenerationCapture",
    "GenerationSink",
    "RecordingGenerationSink",
    "capture_generation",
    "forget_generation",
]


@dataclass
class GenerationCapture:
    """一次导入在管线改写字节**之前**的样子。对应 Go ``GenerationCapture``。

    ``original`` 是收到的原始图片字节。超过导入压缩限制的图片会被重编码成 JPEG，
    那会丢掉承载生成参数的 PNG 文本块与 EXIF/XMP，所以 sink 必须在**同一次调用**
    里拿到这些字节。
    """

    #: 已存对象的 ID（不是原始文件的）。所有工具都用它寻址这份媒体。
    media_id: str = ""
    #: ``original`` 的 SHA-256（与 ID 同一种十六进制形态）。
    source_hash: str = ""
    #: 收到的文件形态，因此可以和已存对象的名称/类型/大小直接比较。
    source_name: str = ""
    source_mime: str = ""
    source_size: int = 0
    stored_mime: str = ""
    stored_size: int = 0
    #: 已存对象与原始字节不一致，因此无法再从它自己读回元数据。
    transcoded: bool = False
    #: 收到的原始字节（压缩前）。非图片导入时为 ``None``。
    original: bytes | None = None


@runtime_checkable
class GenerationSink(Protocol):
    """保存导入图片的生成参数。对应 Go ``GenerationSink``。

    实现方**可以**是同步方法也可以是协程：调用方按返回值是否 awaitable 决定要不要
    ``await``（Go 的实现是同步文件写入，Python 侧常见的实现也是同步的）。
    """

    def capture_generation(self, ctx: Any, capture: GenerationCapture) -> Any:
        """落一条记录；未知 ID 不适用（见 ``forget_generation``）。"""
        ...

    def forget_generation(self, ctx: Any, media_id: str) -> Any:
        """丢弃一个正在被删除的媒体对象的记录；未知 ID 必须视为成功。"""
        ...


class RecordingGenerationSink:
    """把采集/遗忘记进内存列表的 sink（对应 Go 测试里的 ``recordingGenerationSink``）。

    生产路径不使用它；提供它是为了让集成期的冒烟测试与 ``atri_x.app`` 的自检
    不必各自再写一遍。
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.captures: list[GenerationCapture] = []
        self.forgot: list[str] = []
        #: 非 ``None`` 时每次调用都抛它（用来验证"失败不影响导入"）。
        self.error = error

    def capture_generation(self, ctx: Any, capture: GenerationCapture) -> None:
        self.captures.append(capture)
        if self.error is not None:
            raise self.error

    def forget_generation(self, ctx: Any, media_id: str) -> None:
        self.forgot.append(media_id)
        if self.error is not None:
            raise self.error


async def capture_generation(manager: Any, ctx: Any, capture: GenerationCapture) -> None:
    """落一条记录，失败只告警。对应 Go ``Manager.captureGeneration``。"""
    sink = getattr(manager, "generation", None)
    if sink is None or capture.original is None:
        return
    raise_if_cancelled(ctx)
    try:
        await _maybe_await(sink.capture_generation(ctx, capture))
    except Exception as exc:  # noqa: BLE001 - 丢记录不能丢图
        _warn(manager, ctx, "store image generation parameters failed", capture.media_id, exc)


async def forget_generation(manager: Any, ctx: Any, media_id: str) -> None:
    """丢弃记录，失败只告警。对应 Go ``Manager.forgetGeneration``。"""
    sink = getattr(manager, "generation", None)
    if sink is None:
        return
    try:
        await _maybe_await(sink.forget_generation(ctx, media_id))
    except Exception as exc:  # noqa: BLE001
        _warn(manager, ctx, "forget image generation parameters failed", media_id, exc)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _warn(manager: Any, ctx: Any, message: str, media_id: str, exc: BaseException) -> None:
    logger = getattr(manager, "logger", None)
    if logger is None:
        return
    try:
        logger.warning("%s media_id=%s error=%s", message, media_id, exc)
    except Exception:  # noqa: BLE001 - 日志失败不能影响导入
        pass
