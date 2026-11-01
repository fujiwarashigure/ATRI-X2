"""模型流式调用的叶子助手（打开流、并发槽、记账结算、媒体补全）。

对应 Go：``internal/agent/chat_llm.go`` 里 ``callLLM`` 下半段使用的小工具函数。
函数本体没有任何本包模块级依赖，所以 ``chat_llm.py`` 在文件末尾再导出，
``from atri_x.agent.chat_llm import _settle`` 这类导入路径保持不变。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_x.agent.chat_vision import (
    _vision_attempted,
    should_fallback_vision,
    vision_fallback_transparent,
)
from atri_x.errors import QueueFullError, QueueTimeoutError
from atri_x.llm import ChatRequest, LLMMessage, Usage
from atri_x.logging import ResultFailed
from atri_x.ops.concurrency import Config as LimitConfig
from atri_x.ops.concurrency import Limiter
from atri_x.safecontext import CancelToken


def _settle(agent: Any, ctx: Any, reservation: Any, usage: Usage | None, release: bool) -> None:
    if reservation is None:
        return
    agent.settle_chat_budget(ctx, reservation, usage, release)


def _segments_at(messages: list[LLMMessage], index: int) -> list[Any]:
    if 0 <= index < len(messages):
        return list(messages[index].segments)
    return []


async def _persist_pending_lenient(agent: Any, ctx: Any, pending: Any) -> None:
    """尽力落库待发用户消息（原错误优先，落库失败不覆盖它）。"""
    if pending is None:
        return
    try:
        await agent.persist_turn_message(ctx, pending.message, "append_pending_user_message")
    except BaseException:  # noqa: BLE001 - 与 Go 的 errors.Join 相比只保留原错误
        return


async def _resolve_media_for_llm(
    media: Any, ctx: Any, base_messages: list[LLMMessage], releases: list[Any]
) -> tuple[list[LLMMessage], Any]:
    """Hold 住媒体引用再解析成模型可读形态。对应 Go 的 ``Hold`` / ``ResolveForLLM``。"""
    seen: set[str] = set()
    for message in base_messages:
        for segment in message.segments:
            media_id = getattr(segment, "media", "") or ""
            if not media_id or media_id in seen:
                continue
            seen.add(media_id)
            release = media.hold(media_id)
            if release is not None:
                releases.append(release)
    resolved = media.resolve_for_llm(ctx, base_messages)
    if hasattr(resolved, "__await__"):
        resolved = await resolved
    messages, cleanup = resolved
    return list(messages), cleanup


def _release_all(releases: list[Any], cleanup: Any) -> None:
    if callable(cleanup):
        try:
            cleanup()
        except BaseException:  # noqa: BLE001 - 清理失败不影响主流程
            pass
    for release in releases:
        if callable(release):
            try:
                release()
            except BaseException:  # noqa: BLE001
                pass


async def _open_stream(agent: Any, ctx: Any, selection: Any, req: ChatRequest) -> Any:
    """取流式响应。``chat_stream`` 可能是协程，也可能直接是异步迭代器。"""
    client = agent.client_for_provider(selection.provider)
    agent.attach_llm_retry_notifier(client, selection.provider)
    iterator = client.chat_stream(ctx, req)
    if hasattr(iterator, "__await__"):
        iterator = await iterator
    return iterator


async def _acquire_provider_slot(agent: Any, ctx: Any, provider: str) -> Any:
    """provider 级并发闸门。对应 Go ``Agent.acquireProviderSlot``。

    Go 只有 ``concurrency.Limiter`` 一条路；Python 侧分两条，按
    ``provider_queue_max_size`` 选：

    * ``> 0``：用 :class:`atri_x.ops.concurrency.Limiter`，最多排队这么多等待者，
      队满**立即**拒绝。1.1.8 起把 1.1.7 里"只解析、没有消费者"的
      ``provider_queue_max_size`` / ``provider_wait_timeout_seconds`` 真正接上。
    * ``<= 0``：保持旧语义（``asyncio.Semaphore``，最多等到
      ``provider_wait_timeout_seconds`` 再拒绝），默认配置走这条，行为不变。
    """
    ops = getattr(agent, "ops", None)
    limit = int(getattr(ops, "provider_max_concurrent", 0) or 0)
    if limit <= 0:
        return _noop
    queue_size = int(getattr(ops, "provider_queue_max_size", 0) or 0)
    timeout = float(getattr(ops, "provider_wait_timeout_seconds", 0) or 0)
    if queue_size > 0:
        return await _acquire_provider_limiter(
            agent, ctx, provider, limit, queue_size, timeout
        )
    return await _acquire_provider_semaphore(agent, ctx, provider, limit, timeout)


async def _acquire_provider_semaphore(
    agent: Any, ctx: Any, provider: str, limit: int, timeout: float
) -> Any:
    """旧路径：每个 provider 一个 :class:`asyncio.Semaphore`（无限等待者）。"""
    semaphores = getattr(agent, "_provider_semaphores", None)
    if semaphores is None:
        semaphores = {}
        agent._provider_semaphores = semaphores
    semaphore = semaphores.get(provider)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        semaphores[provider] = semaphore
    try:
        if timeout > 0:
            await asyncio.wait_for(semaphore.acquire(), timeout)
        else:
            await semaphore.acquire()
    except asyncio.TimeoutError as exc:
        _audit_provider_rejected(agent, provider, str(exc))
        raise RuntimeError(f"provider {provider} 并发排队超时") from exc

    released = False

    def _release() -> None:
        nonlocal released
        if released:
            return
        released = True
        semaphore.release()

    return _release


async def _acquire_provider_limiter(
    agent: Any, ctx: Any, provider: str, limit: int, queue_size: int, timeout: float
) -> Any:
    """有界队列路径：队满立即拒绝，而不是让等待者无界堆积。"""
    limiters = getattr(agent, "_provider_limiters", None)
    if limiters is None:
        limiters = {}
        agent._provider_limiters = limiters
    limiter = limiters.get(provider)
    if limiter is None:
        limiter = Limiter(
            LimitConfig(max=limit, queue_size=queue_size, wait_timeout=timeout)
        )
        limiters[provider] = limiter
    token = ctx if isinstance(ctx, CancelToken) else None
    try:
        return await limiter.acquire(token)
    except (QueueFullError, QueueTimeoutError) as exc:
        _audit_provider_rejected(agent, provider, str(exc))
        raise RuntimeError(f"provider {provider} 并发排队失败：{exc}") from exc


def _audit_provider_rejected(agent: Any, provider: str, error: str) -> None:
    """记一条审计（审计不可用也不该盖住真正的并发错误）。"""
    audit = getattr(agent, "audit", None)
    if not callable(audit):
        return
    audit(
        "provider_concurrency_rejected",
        "provider",
        provider,
        "error",
        error,
        "result",
        ResultFailed,
    )


def _noop() -> None:
    return None


def _can_retry_without_vision(
    messages: list[LLMMessage],
    err: BaseException | None,
    assistant_text: str,
    tool_call_count: int,
    reasoning_open: bool,
    ctx: Any,
) -> bool:
    return (
        should_fallback_vision(messages, err)
        and vision_fallback_transparent(assistant_text, tool_call_count, reasoning_open)
        and not _vision_attempted(ctx)
    )
