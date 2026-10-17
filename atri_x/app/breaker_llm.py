"""熔断 / 兜底包装的 LLM 客户端。

对应 Go：``internal/app/breaker_llm.go``。

与 Go 的对应关系：

* Go ``ChatStream(ctx, req) (<-chan StreamChunk, error)``；
  Python ``chat_stream(ctx, req)`` 是同步函数返回异步迭代器，**同步阶段**抛出的
  错误等价 Go 的第二返回值，**迭代阶段**抛出的错误等价 ``chunk.Error``。
* Go 的 ``context.WithTimeoutCause`` 总超时在这里用"迭代器内部按截止时间判定"复刻：
  Python 没有把 deadline 注入同步返回的异步迭代器的标准做法，硬造一个 Task 会
  改变取消语义，所以选择在每次取 chunk 前检查剩余时间，超时则抛
  :data:`atri_x.errors.ErrFallbackTotalTimeout`（并给下游留一次送出错误 chunk 的机会）。
* Go 的 ``sendStreamChunk`` 带 ``maxWait``，用于"接收方已经走了也要尽量把超时
  错误送达"。Python 侧异步生成器无法非阻塞投递，因此超时直接抛出
  :class:`atri_x.errors.FallbackTotalTimeoutError`，由调用方按错误 chunk 处理 ——
  这是**有意偏差**，语义（"总超时是一个可重试的失败"）保持不变。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from atri_x.errors import ErrFallbackTotalTimeout, FallbackTotalTimeoutError, OpenError
from atri_x.llm.breaker import Breaker, Config as BreakerConfig, ErrOpen

__all__ = [
    "BreakerLLM",
    "breakerLLM",
    "wrapBreakerLLM",
    "wrap_breaker_llm",
    "withFallbackOnError",
    "with_fallback_on_error",
    "withFallbackTotalTimeout",
    "with_fallback_total_timeout",
]


def with_fallback_on_error(enabled: bool) -> Any:
    """对应 Go ``withFallbackOnError``。"""
    return ("fallback_on_error", bool(enabled))


def with_fallback_total_timeout(timeout: float) -> Any:
    """对应 Go ``withFallbackTotalTimeout``。"""
    return ("total_timeout", float(timeout))


class BreakerLLM:
    """对应 Go ``breakerLLM``。"""

    def __init__(
        self,
        provider: str,
        inner: Any,
        fallback: Any = None,
        fallback_provider: str = "",
        fallback_model: str = "",
        breaker: Breaker | None = None,
        state: Any = None,
        fallback_on_error: bool = False,
        total_timeout: float = 0.0,
    ) -> None:
        self.provider = provider
        self.inner = inner
        self.fallback = fallback
        self.fallback_provider = fallback_provider
        self.fallback_model = fallback_model
        self.breaker = breaker if breaker is not None else Breaker(BreakerConfig())
        self.state = state
        self.fallback_on_error = fallback_on_error
        self.total_timeout = total_timeout

    # -- 流式 ---------------------------------------------------------------
    def chat_stream(self, ctx: Any, req: Any) -> Any:
        """对应 Go ``ChatStream``。"""
        deadline = (
            time.monotonic() + self.total_timeout if self.total_timeout > 0 else 0.0
        )
        use_fallback = not self.breaker.allow()
        self.record_circuit()
        if use_fallback and self.fallback is None:
            err = OpenError(f"{ErrOpen}: provider={self.provider}")
            self.record_failure(err)
            raise err

        try:
            stream, using_fallback = self.open_stream(ctx, req, use_fallback)
        except BaseException as exc:
            if not use_fallback:
                self.record_failure(exc)
                if self.fallback_on_error and self.fallback is not None:
                    try:
                        fallback_stream, _ = self.open_stream(ctx, req, True)
                    except BaseException as fallback_err:
                        raise _combined_error(
                            self.provider,
                            self.fallback_provider_name(),
                            exc,
                            fallback_err,
                        ) from fallback_err
                    return self._pipe(
                        ctx, fallback_stream, req, True, deadline
                    )
            raise
        return self._pipe(ctx, stream, req, using_fallback, deadline)

    def open_stream(self, ctx: Any, req: Any, fallback: bool) -> tuple[Any, bool]:
        """对应 Go ``openStream``，返回 ``(stream, using_fallback)``。"""
        client = self.inner
        provider = self.provider
        if fallback:
            client = self.fallback
            provider = self.fallback_provider_name()
            if self.fallback_model:
                req = _with_model(req, self.fallback_model)
        if client is None:
            from atri_x.errors import InvalidError

            raise InvalidError(f'provider "{provider}" is unavailable')
        stream = client.chat_stream(ctx, req)
        if stream is None:
            from atri_x.errors import InvalidError

            raise InvalidError(f'provider "{provider}" returned a nil stream')
        return stream, fallback

    async def _pipe(
        self,
        ctx: Any,
        first: Any,
        req: Any,
        using_fallback: bool,
        deadline: float,
    ) -> Any:
        """对应 Go ``pipe``：逐 chunk 转发，必要时在错误后切兜底。"""
        current = first
        active_fallback = using_fallback
        recorded_success = False
        delivered_content = False
        while current is not None:
            if deadline and time.monotonic() > deadline:
                self.record_failure(
                    FallbackTotalTimeoutError(f"{ErrFallbackTotalTimeout} after {self.total_timeout}s")
                )
                raise FallbackTotalTimeoutError(
                    f"{ErrFallbackTotalTimeout} after {self.total_timeout}s"
                )
            try:
                chunk = await current.__anext__()
            except StopAsyncIteration:
                current = None
                continue
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # 迭代期抛错等价 Go 的 chunk.Error。
                if not active_fallback:
                    self.record_failure(exc)
                    if (
                        self.fallback_on_error
                        and self.fallback is not None
                        and not delivered_content
                    ):
                        try:
                            next_stream, _ = self.open_stream(ctx, req, True)
                        except BaseException as fallback_err:
                            raise _combined_error(
                                self.provider,
                                self.fallback_provider_name(),
                                exc,
                                fallback_err,
                            ) from fallback_err
                        current = next_stream
                        active_fallback = True
                        recorded_success = False
                        continue
                raise
            error = getattr(chunk, "error", None)
            if error is not None:
                if not active_fallback:
                    self.record_failure(error)
                    if (
                        self.fallback_on_error
                        and self.fallback is not None
                        and not delivered_content
                    ):
                        try:
                            next_stream, _ = self.open_stream(ctx, req, True)
                        except BaseException as fallback_err:
                            error = _combined_error(
                                self.provider,
                                self.fallback_provider_name(),
                                error,
                                fallback_err,
                            )
                            chunk = _with_error(chunk, error)
                        else:
                            current = next_stream
                            active_fallback = True
                            recorded_success = False
                            continue
            else:
                if not active_fallback and not recorded_success:
                    self.breaker.success()
                    self.record_circuit()
                    recorded_success = True
                if (
                    getattr(chunk, "delta_content", "")
                    or getattr(chunk, "delta_reasoning_content", "")
                    or getattr(chunk, "tool_call_deltas", None)
                ):
                    delivered_content = True
            yield chunk

    # -- 非流式 -------------------------------------------------------------
    async def list_models(self, ctx: Any) -> list[str]:
        """对应 Go ``ListModels``。"""
        if self.breaker.allow():
            try:
                models = self.inner.list_models(ctx)
                if hasattr(models, "__await__"):
                    models = await models
            except BaseException as exc:
                self.record_failure(exc)
                if self.fallback_on_error and self.fallback is not None:
                    result = self.fallback.list_models(ctx)
                    if hasattr(result, "__await__"):
                        result = await result
                    return result
                raise
            self.breaker.success()
            self.record_circuit()
            return models
        return await self._list_fallback_models(ctx)

    async def _list_fallback_models(self, ctx: Any) -> list[str]:
        if self.fallback is not None:
            result = self.fallback.list_models(ctx)
            if hasattr(result, "__await__"):
                result = await result
            return result
        err = OpenError(f"{ErrOpen}: provider={self.provider}")
        self.record_failure(err)
        raise err

    # -- 可选能力 -----------------------------------------------------------
    def set_retry_notifier(self, fn: Any) -> None:
        """对应 Go ``SetRetryNotifier``：主/兜底两侧都转发。"""
        for client in (self.inner, self.fallback):
            notifier = getattr(client, "set_retry_notifier", None)
            if callable(notifier):
                notifier(fn)

    async def list_model_metadata(self, ctx: Any) -> list[Any]:
        """对应 Go ``ListModelMetadata``：只转发主客户端。"""
        from atri_x.errors import InvalidError

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
    def fallback_provider_name(self) -> str:
        """对应 Go ``fallbackProviderName``。"""
        return self.fallback_provider or "fallback"

    def record_failure(self, err: BaseException | None) -> None:
        """对应 Go ``recordFailure``：取消不记失败。"""
        if err is None or isinstance(err, asyncio.CancelledError):
            return
        self.breaker.failure()
        self.record_circuit()
        if self.state is not None:
            self.state.record_model_error(self.provider, err)

    def record_circuit(self) -> None:
        """对应 Go ``recordCircuit``：只在熔断器启用时上报状态。"""
        if self.state is None or self.breaker is None or not self.breaker.enabled():
            return
        self.state.set_model_circuit(self.provider, self.breaker.state())

    # Go 命名别名。
    ChatStream = chat_stream
    ListModels = list_models
    SetRetryNotifier = set_retry_notifier
    ListModelMetadata = list_model_metadata


def wrap_breaker_llm(
    provider: str,
    inner: Any,
    fallback: Any = None,
    fallback_provider: str = "",
    fallback_model: str = "",
    breaker: Breaker | None = None,
    state: Any = None,
    *opts: Any,
) -> Any:
    """对应 Go ``wrapBreakerLLM``。

    ``opts`` 接受 :func:`with_fallback_on_error` /
    :func:`with_fallback_total_timeout` 的返回值（``(字段名, 值)`` 二元组），
    与 Go 的 functional options 一一对应。
    """
    if inner is None:
        return inner
    if breaker is None:
        breaker = Breaker(BreakerConfig())
    client = BreakerLLM(
        provider=provider,
        inner=inner,
        fallback=fallback,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model,
        breaker=breaker,
        state=state,
    )
    for opt in opts:
        if not opt:
            continue
        name, value = opt
        setattr(client, name, value)
    # 关闭的熔断器是透明的，除非 provider 显式要求 on-error 兜底或总超时。
    if not breaker.enabled() and not client.fallback_on_error and client.total_timeout <= 0:
        return inner
    return client


def _combined_error(
    provider: str, fallback_provider: str, primary: BaseException, fallback: BaseException
) -> BaseException:
    """Go 的 ``fmt.Errorf("provider %s failed: %v; fallback provider %s failed: %w", ...)``。"""
    from atri_x.errors import InvalidError

    return InvalidError(
        f"provider {provider} failed: {primary}; "
        f"fallback provider {fallback_provider} failed: {fallback}"
    )


def _with_model(req: Any, model: str) -> Any:
    """返回 ``req`` 的副本并把 model 换成兜底模型（不改调用方对象）。"""
    import dataclasses

    try:
        return dataclasses.replace(req, model=model)
    except Exception:  # noqa: BLE001 - 非 dataclass 的请求对象就地改
        req.model = model
        return req


def _with_error(chunk: Any, error: BaseException) -> Any:
    import dataclasses

    try:
        return dataclasses.replace(chunk, error=error)
    except Exception:  # noqa: BLE001
        chunk.error = error
        return chunk


breakerLLM = BreakerLLM
wrapBreakerLLM = wrap_breaker_llm
withFallbackOnError = with_fallback_on_error
withFallbackTotalTimeout = with_fallback_total_timeout
