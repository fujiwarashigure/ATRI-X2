"""SSE 行扫描与首包/空闲超时。

对应 Go：``internal/llm/openai/openai.go`` 的 ``scanStreamLines`` /
``prepareStreamBody`` / ``readStream`` 里的 ``time.Timer`` 逻辑，以及
``ChatStream`` 同步返回 channel 的形状。从 :mod:`atri_x.llm.openai.openai`
拆出（单文件 ≤ 600 行的移植约定），两个适配器的流式循环共用。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from atri_x.llm.errors import APIError, ErrorCategory
from atri_x.net.httpclient import HTTPError

from atri_x.llm.openai.errors import HTML_RESPONSE_HINT, looks_like_html, looks_like_sse, read_error, response_summary
from atri_x.llm.openai.wire import (
    DATA_DONE,
    DATA_PREFIX,
    MAX_STREAM_LINE_BYTES,
    header_of,
    media_type_of,
)

__all__ = [
    "AwaitableStream",
    "StreamLineReader",
    "duration_text",
    "first_chunk_timeout_error",
    "idle_timeout_error",
    "prepare_stream_body",
    "timeout_error",
]


def duration_text(seconds: float) -> str:
    """Go 用 ``time.Duration.String()``；Python 侧统一按秒输出（如 ``180s``）。"""
    return f"{seconds:g}s"


def _raise_if_cancelled(cancel: Any) -> None:
    """对应 ``httpclient._raise_if_cancelled``：``ctx`` 被取消时立刻中断流。

    Go 的适配器在每个循环点检查 ``ctx.Err()``；这里对 ``CancelToken`` 或
    ``asyncio.Event`` 做同样的检查，抛 ``asyncio.CancelledError``（PORTING §6）。
    """
    if cancel is None:
        return
    checker = getattr(cancel, "is_cancelled", None)
    if callable(checker) and checker():
        raise asyncio.CancelledError("请求已取消")
    if isinstance(cancel, asyncio.Event) and cancel.is_set():
        raise asyncio.CancelledError("请求已取消")


def first_chunk_timeout_error(seconds: float) -> APIError:
    """首包超时错误（``category=timeout``，消息文本与 Go 同形）。"""
    message = f"LLM first stream chunk timeout after {duration_text(seconds)}"
    return APIError(message, category=ErrorCategory.TIMEOUT, cause=TimeoutError(message))


def idle_timeout_error(seconds: float) -> APIError:
    """流空闲超时错误（``category=timeout``）。"""
    message = f"LLM stream idle timeout after {duration_text(seconds)}"
    return APIError(message, category=ErrorCategory.TIMEOUT, cause=TimeoutError(message))


def timeout_error(
    seen_data: bool, first_chunk_timeout: float, stream_idle_timeout: float
) -> APIError:
    """按 Go ``readStream`` 的分支选首包或空闲超时。

    Go 抛的是裸 ``fmt.Errorf``；这里按要求带上 ``category=timeout``，让
    分类表的消费方（重试、降级、诊断）能按类别分支。
    """
    if seen_data:
        return idle_timeout_error(stream_idle_timeout)
    return first_chunk_timeout_error(first_chunk_timeout)


class StreamLineReader:
    """带首包/空闲超时的 SSE 行读取器。

    * 首行由 :func:`prepare_stream_body` 在 ``first_chunk_timeout`` 内读走
      （Go ``prepareStreamBody``），这里把它重放为第一行；
    * 之后每条 ``data: `` 行（``[DONE]`` 除外）把空闲计时重新起算；空白行、
      注释行、``event:`` 行都不重置——与 Go ``resetTimer`` 的位置一致；
    * 超时抛 :class:`APIError`（``category=timeout``）；底层读失败、行超长抛
      ``HTTPError``，且消息已带 Go 的 ``read stream:`` 前缀。
    """

    def __init__(
        self,
        iterator: AsyncIterator[str],
        prefix: str | None,
        first_chunk_timeout: float,
        stream_idle_timeout: float,
        cancel: Any = None,
    ) -> None:
        self._iterator = iterator
        self._pending = prefix
        self.first_chunk_timeout = first_chunk_timeout
        self.stream_idle_timeout = stream_idle_timeout
        #: 可选的取消令牌（对应 Go 的 ``context.Context``，见 PORTING §6：
        #: ``CancelToken`` 或 ``asyncio.Event``，鸭子类型判定 ``is_cancelled()``）。
        self._cancel = cancel

    def _timeout_error(self, seen_data: bool) -> APIError:
        return timeout_error(seen_data, self.first_chunk_timeout, self.stream_idle_timeout)

    def _check_line_size(self, line: str) -> None:
        # UTF-8 下单字符最多 4 字节，因此字符数先过 MAX/4 才需要真的编码计数；
        # 该下界保证「字节数超限」不会被漏掉。
        if len(line) > MAX_STREAM_LINE_BYTES // 4 and (
            len(line.encode("utf-8", "replace")) > MAX_STREAM_LINE_BYTES
        ):
            raise HTTPError(
                f"read stream: SSE line exceeds maximum size of {MAX_STREAM_LINE_BYTES} bytes"
            )

    async def __aiter__(self) -> AsyncIterator[str]:
        deadline = time.monotonic() + self.first_chunk_timeout
        seen_data = False
        while True:
            _raise_if_cancelled(self._cancel)
            if self._pending is not None:
                line = self._pending
                self._pending = None
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(seen_data)
                try:
                    line = await asyncio.wait_for(self._iterator.__anext__(), remaining)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    raise self._timeout_error(seen_data) from exc
                except HTTPError as exc:
                    raise HTTPError(
                        f"read stream: {exc}", cause=getattr(exc, "cause", None) or exc
                    ) from exc
            self._check_line_size(line)
            if line.startswith(DATA_PREFIX) and line != DATA_DONE:
                seen_data = True
                deadline = time.monotonic() + self.stream_idle_timeout
            yield line


async def prepare_stream_body(
    stream: Any, first_chunk_timeout: float
) -> tuple[AsyncIterator[str], str | None]:
    """对应 Go ``prepareStreamBody``：Content-Type/HTML/SSE 校验 + 首行读取。

    返回 ``(行迭代器, 首行)``；首行为 ``None`` 表示响应体一开始就 EOF。校验顺序
    与 Go 一致：先看 ``text/html`` 的 Content-Type（不等首行，避免网关页把首包
    超时耗满），再读首行看 HTML 特征，最后判定像不像 SSE。

    ``stream`` 只需提供 ``headers`` 与 ``lines()``（``httpclient.HTTPStream`` 与
    本包的 ``transport.LineStream`` 都满足）。
    """
    content_type = header_of(stream.headers, "content-type")
    media_type = media_type_of(stream.headers)
    if media_type == "text/html":
        raise HTTPError(HTML_RESPONSE_HINT)

    iterator = stream.lines().__aiter__()
    try:
        prefix: str | None = await asyncio.wait_for(iterator.__anext__(), first_chunk_timeout)
    except StopAsyncIteration:
        prefix = None
    except TimeoutError as exc:
        raise first_chunk_timeout_error(first_chunk_timeout) from exc
    except HTTPError as exc:
        raise read_error(exc) from exc

    text = prefix or ""
    if looks_like_html(text):
        raise HTTPError(HTML_RESPONSE_HINT)
    is_sse = looks_like_sse(text) or (not text.strip() and media_type == "text/event-stream")
    if not is_sse:
        raise HTTPError(
            f"上游返回非 SSE API 响应（Content-Type={content_type!r}）：{response_summary(text)}"
        )
    return iterator, prefix


class AwaitableStream:
    """``chat_stream`` 的返回值：既可 ``async for``，也可 ``await`` 后再 ``async for``。

    Go 的 ``ChatStream`` 同步返回 channel；Python 侧按冻结的 ``LLM`` 协议
    （``chat_stream`` 不是 ``async def``）返回异步迭代器。为了不让调用方多写
    一个 ``await`` 就炸掉，这里额外提供 ``__await__``（返回自身）。
    """

    __slots__ = ("_iterator",)

    def __init__(self, iterator: AsyncIterator[Any]) -> None:
        self._iterator = iterator

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterator

    async def __anext__(self) -> Any:
        return await self._iterator.__anext__()

    def __await__(self) -> Any:
        async def _self() -> "AwaitableStream":
            return self

        return _self().__await__()
