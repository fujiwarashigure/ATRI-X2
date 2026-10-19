"""把「路由判定」接到对话链路上：预算、执行、流式回传、熔断与失败回退。

挂点在 :func:`atri_x.agent.chat_llm.call_llm`：请求 hook 已经跑过、媒体已经解析，
**在真正打原生模型之前**问一次路由。命中时：

1. 用既有的 ``begin_chat_budget`` 做预算预占（硬限制生效时）；
2. 拼交接 prompt、把最近历史交给 DSH 跑一轮（:mod:`atri_x.agent.dsh.handoff` /
   :mod:`atri_x.agent.dsh.runtime`）；
3. 把流式文本写进同一个 ``MessageStream``，返回一个普通的
   :class:`atri_x.agent.chat_llm.LLMCallResult` —— 因此上层的落库、发送、状态发布、
   ``replace_and_finish_stream`` 全部沿用原生路径，不需要第二套收尾逻辑；
4. 用 ACP 回报的 ``usage`` 记账（``settle_chat_budget`` / ``record_chat_usage``）；
   内核**没回报 usage** 时不记 0，而是保留预占并标 uncertain（见
   :func:`atri_x.agent.budget_ledger.BudgetLedger.settle_chat_budget`），软账本跳过。

失败语义（**不能破坏聊天**）：

- DSH 一个字都没输出就失败 → 回退原生链路，用户只会看到正常回答；连续失败到阈值
  时打开熔断并**只提示一次**；
- 已经流出了部分文本 → 保留部分内容收尾（不重放、不双写），提示一句"部分内容"。

Python 版新增（Go 上游没有）：见 ``docs/windows-app.md`` 与 ``devdocs/dsh-acp.md``。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from atri_x.agent.chat_llm import LEVEL_WARN, LLMCallResult
from atri_x.agent.chat_observe import (
    audit_usage,
    elapsed_millis,
    log_llm_output,
    should_show_cli_reasoning,
)
from atri_x.agent.dsh.handoff import (
    build_handoff_prompt,
    latest_user_text,
    usage_from_acp,
)
from atri_x.agent.dsh.router import (
    DEFAULT_CREDENTIAL_BASE_URL_ENV,
    DEFAULT_CREDENTIAL_ENV,
    DshRouteConfig,
    DshRouter,
    RouteDecision,
)
from atri_x.agent.dsh.runtime import DshRuntime, resolve_paths
from atri_x.config import ModelSelection
from atri_x.errors import InvalidError
from atri_x.hook import Event, LLMPayload, PointLLMResponseReceived, SessionContext
from atri_x.llm import Usage
from atri_x.storage import SessionModeWork, new_id

__all__ = [
    "DshRouteService",
    "close_route_service",
    "describe_route",
    "ensure_service",
    "maybe_route_to_dsh",
    "resolve_credential_env",
]

#: 服务键（``agent.services['dsh_route']``）。
SERVICE_KEY = "dsh_route"
#: 回退/失败提示文案。
PARTIAL_NOTICE = "DSH 处理中断，以上是已生成的部分内容。"
BREAKER_NOTICE = "DSH 深度处理连续失败，已临时停用并继续使用本地模型。"


@dataclass
class DshRouteService:
    """Agent 生命周期内的路由服务：路由器 + 懒创建的运行时。"""

    router: DshRouter
    runtime: DshRuntime | None = None
    _paths_agent: Any = field(default=None, repr=False)

    def ensure_runtime(self, agent: Any) -> DshRuntime:
        """懒创建运行时（第一次真正要路由时才解析命令与目录）。"""
        if self.runtime is None:
            config = self.router.config
            self.runtime = DshRuntime(
                config,
                command=self.router.command() if self.router.available() else None,
                paths=resolve_paths(agent, config),
                extra_env=resolve_credential_env(agent, config),
            )
            self._paths_agent = agent
        return self.runtime

    async def aclose(self) -> None:
        """关掉 DSH 子进程（应用关停时调用）。"""
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            await runtime.aclose()

    def describe(self) -> dict[str, Any]:
        """状态快照（doctor / 测试）。"""
        out = self.router.describe()
        if self.runtime is not None:
            out["runtime"] = self.runtime.describe()
        return out


def ensure_service(agent: Any) -> DshRouteService:
    """取/建路由服务并挂在 Agent 上（幂等）。"""
    services = getattr(agent, "services", None)
    if isinstance(services, dict):
        existing = services.get(SERVICE_KEY)
        if isinstance(existing, DshRouteService):
            return existing
    existing = getattr(agent, "dsh_route_service", None)
    if isinstance(existing, DshRouteService):
        return existing
    config = DshRouteConfig.from_section(getattr(agent, "dsh_config", None))
    service = DshRouteService(router=DshRouter(config))
    if isinstance(services, dict):
        services[SERVICE_KEY] = service
    try:
        agent.dsh_route_service = service
    except Exception:  # noqa: BLE001 - 只读/冻结的 Agent 替身不挂属性
        pass
    return service


async def close_route_service(agent: Any) -> None:
    """关停路由服务（没有创建过则什么都不做）。"""
    if agent is None:
        return
    services = getattr(agent, "services", None)
    service = services.get(SERVICE_KEY) if isinstance(services, dict) else None
    if service is None:
        service = getattr(agent, "dsh_route_service", None)
    aclose = getattr(service, "aclose", None)
    if callable(aclose):
        await aclose()


def describe_route(agent: Any) -> dict[str, Any]:
    """路由状态快照（没有创建过服务时返回 ``enabled`` 与可用性）。"""
    services = getattr(agent, "services", None)
    service = services.get(SERVICE_KEY) if isinstance(services, dict) else None
    if service is None:
        service = getattr(agent, "dsh_route_service", None)
    if isinstance(service, DshRouteService):
        return service.describe()
    config = DshRouteConfig.from_section(getattr(agent, "dsh_config", None))
    router = DshRouter(config)
    return router.describe()


#: 官方 DeepSeek 主机；``base_url`` 指向它时不必额外下发覆盖变量。
_OFFICIAL_BASE_URL_HOSTS = ("api.deepseek.com",)


def _text_of(value: Any) -> str:
    return str(value or "").strip()


def _provider_api_key(provider: Any) -> str:
    """provider 的可用密钥：``api_key`` 优先，其次 ``api_key_env`` 指向的宿主环境变量。"""
    key = _text_of(getattr(provider, "api_key", ""))
    if key:
        return key
    key_env = _text_of(getattr(provider, "api_key_env", ""))
    return _text_of(os.environ.get(key_env, "")) if key_env else ""


def _pick_credential_provider(agent: Any, named: str) -> Any:
    """挑一个能提供密钥的 provider（显式指定 > 名字含 deepseek 的 > work provider）。"""
    providers = getattr(agent, "providers", None)
    table = providers if isinstance(providers, Mapping) else {}
    candidates: list[Any] = []
    if named and named in table:
        candidates.append(table[named])
    for key, value in table.items():
        if "deepseek" in _text_of(key).lower():
            candidates.append(value)
    selection = (getattr(agent, "mode_models", None) or {}).get(SessionModeWork)
    work_provider = _text_of(getattr(selection, "provider", ""))
    if work_provider and table.get(work_provider) is not None:
        candidates.append(table[work_provider])
    seen: set[int] = set()
    for provider in candidates:
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        if _provider_api_key(provider):
            return provider
    return None


def _is_official_base_url(base_url: str) -> bool:
    """``base_url`` 是否就是官方 DeepSeek 端点（含 ``/v1`` 之类的路径）。"""
    text = _text_of(base_url)
    if not text:
        return True
    host = _text_of(urlsplit(text).hostname)
    return any(
        host == official or host.endswith("." + official)
        for official in _OFFICIAL_BASE_URL_HOSTS
    )


def resolve_credential_env(agent: Any, config: DshRouteConfig) -> dict[str, str]:
    """解析要注入 DSH 子进程的环境变量（凭据 + 可选的非官方 base_url）。

    DSH 侧 ``dsh-credentials-local`` 的优先级是「继承环境变量 > ``DSH_HOME`` 下的
    ``.credentials.yaml`` > cwd 的 ``.env`` > ``DSH_HOME`` 的 ``.env``」，而 ATRI-X 读
    ``.env`` 时**从不回写** ``os.environ``（Windows 图形端把密钥写进
    ``%APPDATA%\\ATRI-X\\.env``），所以子进程继承环境是最可靠的注入点：这里从
    ``agent.providers`` 取 ATRI-X 已经解析好的 ``api_key``，交给
    :func:`atri_x.agent.dsh.runtime.child_environment` 放回过滤后的环境。

    ``api_key`` 为空时**不注入空值**：退回继承值（``.credentials.yaml`` / 宿主环境仍然可用）。
    正常加载路径上 loader 已经把 ``api_key_env`` 解析进 ``api_key``；这里也接受只声明了
    ``api_key_env`` 的 provider（读宿主同名环境变量）。
    """
    env_name = _text_of(getattr(config, "credential_env", "")) or DEFAULT_CREDENTIAL_ENV
    base_url_env = (
        _text_of(getattr(config, "credential_base_url_env", ""))
        or DEFAULT_CREDENTIAL_BASE_URL_ENV
    )
    provider = _pick_credential_provider(
        agent, _text_of(getattr(config, "credential_provider", ""))
    )
    api_key = _provider_api_key(provider)
    base_url = _text_of(getattr(provider, "base_url", ""))
    out: dict[str, str] = {}
    if api_key:
        out[env_name] = api_key
        if base_url and not _is_official_base_url(base_url):
            out[base_url_env] = base_url
        return out
    inherited = _text_of(os.environ.get(env_name, ""))
    if inherited:
        out[env_name] = inherited
    return out


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
    """真正执行一次 DSH 路由（判定已通过）。"""
    config = router.config
    billing = _billing_selection(selection, config.model)
    call_id = new_id()
    reservation = None
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

    prompt = build_handoff_prompt(
        messages,
        max_history_messages=int(config.max_history_messages or 0),
        char_budget=int(config.prompt_char_budget or 0),
    )
    chunks: list[str] = []
    stopped = False

    async def on_chunk(text: str) -> None:
        nonlocal stopped
        if not agent.turn_output_allowed(ctx):
            if not stopped:
                stopped = True
                runtime.cancel_active()
            return
        chunks.append(text)
        if stream is not None:
            await stream.append(ctx, text)

    async def on_thought(text: str) -> None:
        if should_show_cli_reasoning(agent, ctx):
            await out.send_reasoning(ctx, text)

    started = time.monotonic()
    try:
        result = await runtime.run(prompt, on_chunk=on_chunk, on_thought=on_thought)
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
            agent,
            "dsh_route_stopped",
            "session_id",
            session_id,
            "elapsed_ms",
            elapsed,
            "warnings",
            list(result.warnings),
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
        len(prompt),
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
