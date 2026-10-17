"""``tests/test_vision.py`` 用的 fake：上游 LLM 与图像预处理。

放在独立模块里，避免测试文件被 fake 的样板代码撑长；同时让「I/O 适配器用
fake」这一条测试约定显式可见（PORTING.md §5）。

* :class:`FakeLLM` —— 返回异步迭代器的流式适配器。``gate`` 未释放时不会产出
  任何块，用来观察并发合并与队列行为；``error`` 用来模拟建连期失败。
* :func:`patch_prepare` —— 替换
  :func:`atri_x.media.prepare_vision_image_context`，让视觉服务不必真的解码
  位图（位图编解码由 ``atri_x.media`` 自己的测试覆盖）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator, Sequence
from unittest import mock

from atri_x.llm.llm import ChatRequest, StreamChunk

__all__ = ["FakeLLM", "patch_prepare"]


class FakeLLM:
    """最小流式 LLM 适配器。对应 Go 测试里的 ``fakeLLM``。"""

    def __init__(
        self,
        chunks: Sequence[StreamChunk] | None = None,
        *,
        error: BaseException | None = None,
        gate: Any = None,
    ) -> None:
        self.chunks = list(chunks or [])
        self.error = error
        self.gate = gate
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat_stream(self, ctx: Any, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        # Go 的 ChatStream 在建连阶段同步返回错误；Python 适配器同样如此。
        self.calls += 1
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return self._stream(ctx)

    async def _stream(self, ctx: Any) -> AsyncIterator[StreamChunk]:
        if self.gate is not None:
            await self.gate.wait()
        for chunk in self.chunks:
            yield chunk

    async def list_models(self, ctx: Any) -> list[str]:
        return []


@contextmanager
def patch_prepare() -> Iterator[mock.MagicMock]:
    """把视觉预处理替换成「原样返回 + 声明 MIME」的假实现。"""

    def _fake(ctx: Any, data: bytes, mime_type: str, max_edge: int, max_bytes: int):
        del ctx, max_edge, max_bytes
        return data, mime_type or "image/png"

    with mock.patch(
        "atri_x.vision.service.prepare_vision_image_context", side_effect=_fake
    ) as patched:
        yield patched
