"""健康统计包装的 LLM 客户端。

对应 Go：``internal/app/health_llm.go``。

Python 的 ``atri_x.llm`` 协议与 Go 不同：``chat_stream(ctx, req)`` 是**同步函数**
返回 ``StreamChunk`` 的异步迭代器（建连/首包错误在首次迭代时抛），
``list_models(ctx)`` 是协程。因此本包装：

* ``chat_stream`` 保持同步签名并在错误时计数、原样重抛/透传；
* 生成器内部按 Go 的语义计数（``chunk.error`` 记失败；第一条成功 chunk 记一次成功，
  失败后重新允许再记一次）；
* ``set_retry_notifier`` / ``list_model_metadata`` 是**可选**能力，内层没有就
  按 Go 的注释语义退化（前者静默忽略，后者抛错让调用方回落到默认上下文窗口）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_x.errors import InvalidError

__all__ = ["HealthLLM", "healthLLM", "wrapHealthLLM", "wrap_health_llm"]


class HealthLLM:
    """对应 Go ``healthLLM``。"""

    def __init__(self, provider: str, inner: Any, state: Any) -> None:
        self.provider = provider
        self.inner = inner
        self.state = state

    # -- 流式 ---------------------------------------------------------------
    def chat_stream(self, ctx: Any, req: Any) -> Any:
        """对应 Go ``ChatStream``：包一层统计后返回异步迭代器。"""
        try:
            stream = self.inner.chat_stream(ctx, req)
        except BaseException as exc:
            self.record_error(exc)
            raise
        if stream is None:
            err = InvalidError(f'provider "{self.provider}" returned a nil stream')
            self.record_error(err)
            raise err
        return self._pipe(ctx, stream)

    async def _pipe(self, ctx: Any, stream: Any) -> Any:
        recorded_success = False
        try:
            iterator = stream.__aiter__()
            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    return
                if getattr(chunk, "error", None) is not None:
                    self.record_error(chunk.error)
                    recorded_success = False
                elif not recorded_success:
                    self.state.record_model_success(self.provider)
                    recorded_success = True
                yield chunk
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # 迭代期抛错（重试耗尽 / 非 200 / 非 SSE 首包）与 chunk.error 同义。
            self.record_error(exc)
            raise

    # -- 非流式 -------------------------------------------------------------
    async def list_models(self, ctx: Any) -> list[str]:
        """对应 Go ``ListModels``。"""
        try:
            models = self.inner.list_models(ctx)
            if hasattr(models, "__await__"):
                models = await models
        except BaseException as exc:
            self.record_error(exc)
            raise
        self.state.record_model_success(self.provider)
        return models

    # -- 可选能力 -----------------------------------------------------------
    def set_retry_notifier(self, fn: Any) -> None:
        """把可选的重试通知转发给内层。对应 Go ``SetRetryNotifier``。"""
        notifier = getattr(self.inner, "set_retry_notifier", None)
        if callable(notifier):
            notifier(fn)

    async def list_model_metadata(self, ctx: Any) -> list[Any]:
        """对应 Go ``ListModelMetadata``。

        不实现 ``ModelMetadataProvider`` 的内层直接抛错，让调用方回落到手写配置
        或默认上下文窗口。
        """
        provider = getattr(self.inner, "list_model_metadata", None)
        if not callable(provider):
            raise InvalidError(
                f'provider "{self.provider}" does not support model metadata'
            )
        result = provider(ctx)
        if hasattr(result, "__await__"):
            result = await result
        return result

    # -- 统计 ---------------------------------------------------------------
    def record_error(self, err: BaseException | None) -> None:
        """对应 Go ``recordError``：取消不算失败。"""
        if err is None or self.state is None:
            return
        if isinstance(err, asyncio.CancelledError):
            return
        self.state.record_model_error(self.provider, err)

    # Go 命名别名。
    ChatStream = chat_stream
    ListModels = list_models
    SetRetryNotifier = set_retry_notifier
    ListModelMetadata = list_model_metadata


def wrap_health_llm(provider: str, inner: Any, state: Any) -> Any:
    """对应 Go ``wrapHealthLLM``：``inner`` 或 ``state`` 缺一时原样返回。"""
    if inner is None or state is None:
        return inner
    return HealthLLM(provider, inner, state)


healthLLM = HealthLLM
wrapHealthLLM = wrap_health_llm
