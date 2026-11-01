"""把「路由判定」接到对话链路上：凭据闸门、预算、并发、流式回传、失败回退。

挂点在 :func:`atri_x.agent.chat_llm.call_llm`：请求 hook 已经跑过、媒体已经解析，
**在真正打原生模型之前**问一次路由。命中时：

1. 先过**凭据协议闸门**（:mod:`atri_x.agent.dsh.credentials`）：选中的 provider 不是
   Anthropic Messages 端点就静默回原生（1.1.8 之前这里会把 OpenAI 兼容网关的
   ``base_url`` 注入内核，得到必然的 404）；
2. 用既有的 ``begin_chat_budget`` 做预算预占（硬限制生效时）；
3. 拼交接 prompt；命中会话亲和时只发增量（:mod:`atri_x.agent.dsh.affinity`）；
4. 通过 :class:`~atri_x.agent.dsh.scheduler.TurnScheduler` 拿并发槽，最多等
   ``busy_wait_seconds``；等不到就回退原生并审计 ``dsh_route_busy``（不算熔断失败）；
5. 把流式文本写进同一个 ``MessageStream``，返回普通的
   :class:`atri_x.agent.chat_llm.LLMCallResult` —— 上层落库/发送/状态发布全部沿用原生
   路径，不需要第二套收尾逻辑；
6. 用 ACP 回报的 ``usage`` 记账（``settle_chat_budget`` / ``record_chat_usage``）；
   内核**没回报 usage** 时不记 0，而是保留预占并标 uncertain。

失败语义（**不能破坏聊天**）：

- DSH 一个字都没输出就失败 → 回退原生链路，用户只会看到正常回答；连续失败到阈值
  时打开熔断并**只提示一次**；
- 已经流出了部分文本 → 保留部分内容收尾（不重放、不双写），提示一句"部分内容"。

Python 版新增（Go 上游没有）：见 ``devdocs/dsh-acp.md`` 与 ``docs/windows-app.md`` §8.1。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from atri_x.agent.chat_llm import LEVEL_WARN, LLMCallResult
from atri_x.agent.chat_observe import (
    audit_usage,
    elapsed_millis,
    log_llm_output,
    should_show_cli_reasoning,
)
from atri_x.agent.dsh.credentials import resolve_credential_env
from atri_x.agent.dsh.handoff import (
    build_handoff_prompt,
    latest_user_text,
    usage_from_acp,
)
from atri_x.agent.dsh.router import DshRouter, RouteDecision
from atri_x.agent.dsh.runtime import DshRuntime
from atri_x.agent.dsh.scheduler import DshBusyError
from atri_x.agent.dsh.service import (
    DshRouteService,
    close_route_service,
    describe_route,
    dsh_metrics,
    ensure_service,
    prewarm_route_service,
)
from atri_x.config import ModelSelection
from atri_x.errors import InvalidError
from atri_x.hook import Event, LLMPayload, PointLLMResponseReceived, SessionContext
from atri_x.llm import Usage
from atri_x.storage import new_id

__all__ = [
    "DshRouteService",
    "close_route_service",
    "describe_route",
    "dsh_metrics",
    "ensure_service",
    "maybe_route_to_dsh",
    "prewarm_route_service",
    "resolve_credential_env",
]

#: 回退/失败提示文案。
PARTIAL_NOTICE = "DSH 处理中断，以上是已生成的部分内容。"
BREAKER_NOTICE = "DSH 深度处理连续失败，已临时停用并继续使用本地模型。"

# ---------------------------------------------------------------------------
# 路由入口
# ---------------------------------------------------------------------------
async def maybe_route_to_dsh(
    agent: Any,
    ctx: Any,
    session_id: str,
    selection: Any,
    messages: Sequence[Any],
    stream: Any,
    out: Any,
    *,
    max_output: int = 0,
) -> LLMCallResult | None:
    """尝试把这一轮交给 DSH；返回 ``None`` 表示继续走原生链路。

    **保证不抛异常**（除了 ``asyncio.CancelledError``）：路由是增强能力，任何失败都
    必须能退回原生链路。
    """
    try:
        service = ensure_service(agent)
    except Exception:  # noqa: BLE001 - 连服务都建不起来就直接走原生
        return None
    router = service.router
    if not router.config.enabled:
        return None
    service.prepare(agent)
    text = latest_user_text(messages)
    if not text:
        return None
    estimated = _estimate_tokens(messages)
    decision = router.decide(text, estimated_tokens=estimated)
    _audit_decision(agent, session_id, decision, estimated)
    if not decision.use_dsh:
        return None
    try:
        runtime = service.ensure_runtime(agent)
    except Exception as exc:  # noqa: BLE001 - 运行时构造失败按不可用处理
        _audit(agent, "dsh_route_unavailable", "session_id", session_id, "error", str(exc))
        return None
    return await _run_routed(
        agent, ctx, session_id, selection, messages, stream, out,
        router=router, runtime=runtime, max_output=max_output,
    )


async def _run_routed(
    agent: Any,
    ctx: Any,
    session_id: str,
    selection: Any,
    messages: Sequence[Any],
    stream: Any,
    out: Any,
    *,
    router: DshRouter,
    runtime: DshRuntime,
    max_output: int,
) -> LLMCallResult | None:
    """真正执行一次 DSH 路由（判定已通过）；先过 provider 并发闸门。"""
    release_provider: Any = _noop
    provider = runtime.provider or _text_of(getattr(selection, "provider", ""))
    try:
        release_provider = await _acquire_provider_slot(agent, ctx, provider)
    except Exception as exc:  # noqa: BLE001 - provider 排队超时按忙处理
        _audit(
            agent, "dsh_route_busy", "session_id", session_id,
            "stage", "provider", "error", str(exc),
        )
        return None
    try:
        return await _run_governed(
            agent, ctx, session_id, selection, messages, stream, out,
            router=router, runtime=runtime, max_output=max_output,
        )
    finally:
        try:
            release_provider()
        except Exception:  # noqa: BLE001 - 释放失败不影响回答
            pass


async def _run_governed(
    agent: Any,
    ctx: Any,
    session_id: str,
    selection: Any,
    messages: Sequence[Any],
    stream: Any,
    out: Any,
    *,
    router: DshRouter,
    runtime: DshRuntime,
    max_output: int,
) -> LLMCallResult | None:
    """预算 → 交接计划 → 有界并发执行 → 结算/审计/失败回退。"""
    config = router.config
    billing = _billing_selection(selection, config.model)
    call_id = new_id()
    try:
        reservation = agent.begin_chat_budget(
            ctx, billing, list(messages), [], int(max_output or 0), call_id
        )
    except InvalidError as exc:
        _audit(
            agent, "dsh_route_budget_denied", "session_id", session_id, "error", str(exc)
        )
        return None
    except Exception:  # noqa: BLE001 - 预算服务异常时按"不路由"处理
        return None

    handoff = build_handoff_prompt(
        messages,
        max_history_messages=int(config.max_history_messages or 0),
        char_budget=int(config.prompt_char_budget or 0),
    )
    plan = runtime.plan_turn(session_id, messages, handoff_prompt=handoff)
    chunks: list[str] = []
    stopped = False
    token = object()

    async def on_chunk(text: str) -> None:
        nonlocal stopped
        if not agent.turn_output_allowed(ctx):
            if not stopped:
                stopped = True
                runtime.cancel(token)
            return
        chunks.append(text)
        if stream is not None:
            await stream.append(ctx, text)

    async def on_thought(text: str) -> None:
        if should_show_cli_reasoning(agent, ctx):
            await out.send_reasoning(ctx, text)

    started = time.monotonic()
    try:
        result = await runtime.run(
            plan=plan,
            on_chunk=on_chunk,
            on_thought=on_thought,
            cancel_token=token,
        )
    except DshBusyError as exc:
        # 并发槽忙：不算 DSH 失败（不动熔断），释放预占后回原生。
        _settle(agent, ctx, reservation, None, True)
        _audit(
            agent, "dsh_route_busy", "session_id", session_id,
            "stage", "prompt", "error", str(exc),
        )
        return None
    except asyncio.CancelledError:
        _settle(agent, ctx, reservation, None, True)
        raise
    except Exception as exc:  # noqa: BLE001 - 失败一律回退/收尾，不打断聊天
        return await _handle_failure(
            agent, ctx, session_id, messages, stream, out,
            router=router, reservation=reservation, chunks=chunks, error=exc,
        )

    elapsed = elapsed_millis(started)
    if stopped:
        # 本轮已不允许继续输出（取消/超时/被接管）：不记成功，也不回退原生重复调用。
        usage = usage_from_acp(result.usage) if result.ok and result.usage_reported else None
        _settle(agent, ctx, reservation, usage, False)
        _audit(
            agent, "dsh_route_stopped", "session_id", session_id,
            "elapsed_ms", elapsed, "warnings", list(result.warnings),
        )
        partial = "".join(chunks).strip()
        return LLMCallResult(
            text=partial,
            raw_text=partial,
            usage=usage,
            tool_calls=[],
            messages=list(messages),
            stream=stream,
        )
    text = (result.text or "").strip()
    if not text and chunks:
        text = "".join(chunks).strip()
    if not result.ok or not text:
        return await _handle_failure(
            agent, ctx, session_id, messages, stream, out,
            router=router, reservation=reservation, chunks=chunks,
            error=RuntimeError(
                f"DSH 未正常结束（stopReason={result.stop_reason or 'empty'}）"
            ),
        )

    reported = bool(result.usage_reported)
    usage = usage_from_acp(result.usage)
    text, usage = await _apply_response_hook(agent, ctx, session_id, router, text, usage, elapsed)
    router.record_success()
    # 内核没回报 usage（当前 DSH 的 session/prompt 只回 stopReason）时，把 None 交给结算：
    # 既有的「保留预占 + 标 uncertain + 审计 chat_budget_uncertain」逻辑就会生效，
    # 绝不能拿全 0 的占位值当真实用量（那会让硬额度永不消耗）。
    billable = usage if reported else None
    _settle(agent, ctx, reservation, billable, False)
    if reservation is None and billable is not None:
        try:
            agent.record_chat_usage(ctx, billing.model, billable, call_id)
        except Exception:  # noqa: BLE001 - 记账失败不影响回答
            pass
    try:
        audit_usage(agent, session_id, billing, billable, elapsed)
        log_llm_output(agent, ctx, session_id, billing, text, text, 0, elapsed)
    except Exception:  # noqa: BLE001 - 观察层失败不影响回答
        pass
    _audit(
        agent,
        "dsh_route_succeeded",
        "session_id",
        session_id,
        "route_session_id",
        result.session_id,
        "stop_reason",
        result.stop_reason,
        "elapsed_ms",
        elapsed,
        "prompt_chars",
        len(plan.prompt),
        "usage_reported",
        reported,
        "input_tokens",
        usage.prompt_tokens,
        "output_tokens",
        usage.completion_tokens,
        "tool_calls",
        len(result.tool_titles),
        "server",
        result.server,
        "warnings",
        list(result.warnings),
        "session_reused",
        bool(result.session_reused),
        "queue_wait_ms",
        int(result.queue_wait_seconds * 1000),
    )
    return LLMCallResult(
        text=text,
        raw_text=text,
        usage=usage,
        tool_calls=[],
        messages=list(messages),
        stream=stream,
    )


async def _handle_failure(
    agent: Any,
    ctx: Any,
    session_id: str,
    messages: Sequence[Any],
    stream: Any,
    out: Any,
    *,
    router: DshRouter,
    reservation: Any,
    chunks: list[str],
    error: BaseException,
) -> LLMCallResult | None:
    """失败处理：有部分输出就收尾，没有就回退原生；连续失败打开熔断。"""
    opened = router.record_failure()
    _audit(
        agent,
        "dsh_route_failed",
        "session_id",
        session_id,
        "error",
        f"{type(error).__name__}: {error}",
        "opened_breaker",
        opened,
        "partial_chars",
        sum(len(item) for item in chunks),
    )
    if chunks:
        text = "".join(chunks).strip()
        _settle(agent, ctx, reservation, None, False)
        try:
            await out.send_notice(ctx, LEVEL_WARN, PARTIAL_NOTICE)
        except Exception:  # noqa: BLE001 - 提示失败不影响收尾
            pass
        return LLMCallResult(
            text=text,
            raw_text=text,
            usage=None,
            tool_calls=[],
            messages=list(messages),
            stream=stream,
        )
    _settle(agent, ctx, reservation, None, True)
    if opened and router.note_notified():
        try:
            await out.send_notice(ctx, LEVEL_WARN, BREAKER_NOTICE)
        except Exception:  # noqa: BLE001 - 提示失败不影响回退
            pass
    return None


async def _apply_response_hook(
    agent: Any,
    ctx: Any,
    session_id: str,
    router: DshRouter,
    text: str,
    usage: Usage,
    elapsed: int,
) -> tuple[str, Usage]:
    """触发 ``PointLLMResponseReceived`` 钩子（与原生路径同语义）。"""
    run_hook = getattr(agent, "run_hook", None)
    if not callable(run_hook):
        return text, usage
    try:
        event = await run_hook(
            ctx,
            Event(
                point=PointLLMResponseReceived,
                session=SessionContext(id=session_id),
                llm=LLMPayload(
                    provider="dsh",
                    model=(router.config.model or "").strip(),
                    usage=usage,
                    source_text=text,
                    text=text,
                    elapsed_ms=elapsed,
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 钩子失败不推翻已经拿到的回答
        _audit(agent, "dsh_route_hook_failed", "error", str(exc))
        return text, usage
    llm = getattr(event, "llm", None)
    if llm is None:
        return text, usage
    new_text = str(getattr(llm, "text", "") or "").strip()
    new_usage = getattr(llm, "usage", None)
    return (new_text or text), (new_usage if isinstance(new_usage, Usage) else usage)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _noop() -> None:
    """provider 闸门未启用时的空释放函数。"""


async def _acquire_provider_slot(agent: Any, ctx: Any, provider: str) -> Any:
    """provider 级并发闸门（复用原生链路的 ``[ops]`` 治理；限流 0 时是 no-op）。"""
    from atri_x.agent.chat_stream import _acquire_provider_slot as acquire

    return await acquire(agent, ctx, provider)


def _billing_selection(selection: Any, model: str) -> ModelSelection:
    """记账用的模型选择：DSH 模型优先，否则沿用原生模型（价格表按 model 查）。"""
    provider = str(getattr(selection, "provider", "") or "") or "dsh"
    billing_model = (model or "").strip() or str(getattr(selection, "model", "") or "")
    return ModelSelection(provider=provider, model=billing_model or "dsh")


def _estimate_tokens(messages: Sequence[Any]) -> int:
    try:
        from atri_x.contextmgr import estimate_messages_tokens

        return int(estimate_messages_tokens(list(messages)) or 0)
    except Exception:  # noqa: BLE001 - 估算失败按 0 处理（只会少算成本压力）
        return 0


def _text_of(value: Any) -> str:
    return str(value or "").strip()


def _settle(agent: Any, ctx: Any, reservation: Any, usage: Usage | None, release: bool) -> None:
    if reservation is None:
        return
    try:
        agent.settle_chat_budget(ctx, reservation, usage, release)
    except Exception:  # noqa: BLE001 - 结算失败不影响回答
        pass


def _audit(agent: Any, event: str, *attrs: Any) -> None:
    audit = getattr(agent, "audit", None)
    if not callable(audit):
        return
    try:
        audit(event, *attrs)
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass


def _audit_decision(
    agent: Any, session_id: str, decision: RouteDecision, estimated: int
) -> None:
    audit_debug = getattr(agent, "audit_debug", None)
    if not callable(audit_debug):
        return
    try:
        audit_debug(
            "dsh_route_decision",
            "session_id",
            session_id,
            "use_dsh",
            decision.use_dsh,
            "reason",
            decision.reason,
            "score",
            round(decision.score, 4),
            "effective_score",
            round(decision.effective_score, 4),
            "cost_pressure",
            round(decision.cost_pressure, 4),
            "estimated_tokens",
            estimated,
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass
