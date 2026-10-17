"""上下文运行态：窗口/预算解析、压缩后端选择、加载器与最近用量。

对应 Go：``internal/agent/context_runtime.go``，外加 ``context_policy.go`` /
``context_usage.go`` / ``context_compact.go`` 里定义在 ``contextRuntimeState`` 上的
成员（``promptBudget`` / ``summarizeText`` / ``overflowMode`` /
``userOriginalMaxRunes`` / ``recordUsage`` / ``usage`` / ``status`` /
``reachedCompactThreshold`` / ``compactActive``）。

**异步化说明**：Go 的 ``Resolve`` / ``SummarizeText`` 是同步阻塞调用，Python 侧窗口
解析（要问 provider 的 ``list_model_metadata``）与摘要都是 ``await``，所以
``prompt_budget`` / ``summarize_text`` / ``status`` / ``reached_compact_threshold``
都是 ``async``。调用点本来就在异步的 chat 路径上。

压缩**编排**（创建新会话、写 session metadata、turns/requests 生命周期）在
:mod:`atri_x.agent.context_compact`；本模块只提供它需要的状态访问器。
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import types
from typing import Any

from atri_x.config import (
    DEFAULT_CONTEXT_WINDOW,
    ContextConfig,
    ModelMetadataConfig,
    ModelSelection,
)
from atri_x.agent.model import AgentService, host_of, install_methods  # noqa: F401
from atri_x.contextmgr import (
    ClientProvider,
    Compactor,
    CompactionChoice,
    Compressor,
    LoadedContext,
    Loader,
    PromptBudget,
    SummarizeRequest,
    WindowResolver,
    new_prompt_budget,
    new_window_resolver,
)
from atri_x.llm.llm import Usage
from atri_x.llm.protocol import protocol_capabilities_of
from atri_x.request.manager import KindCompress
from atri_x.storage.models import SessionModeWork
from atri_x.storage.repositories import Store
from atri_x.turn.manager import PhaseCompact

__all__ = [
    "CONTEXT_METHODS",
    "ContextRuntimeMixin",
    "ContextRuntimeService",
    "ContextRuntimeState",
    "compact_selection_for_session",
    "ensure_context_runtime",
    "install_context_services",
    "new_context_runtime_state",
]


def new_context_runtime_state(
    store: Store | None = None,
    sessions: Any = None,
    requests: Any = None,
    turns: Any = None,
) -> "ContextRuntimeState":
    """构造运行态（默认配置 + 空用量表）。对应 Go ``newContextRuntimeState``。"""
    return ContextRuntimeState(store, sessions, requests, turns)


class ContextRuntimeState:
    """上下文运行态。对应 Go ``contextRuntimeState``。"""

    def __init__(
        self,
        store: Store | None = None,
        sessions: Any = None,
        requests: Any = None,
        turns: Any = None,
        *,
        config: ContextConfig | None = None,
        model_metadata: ModelMetadataConfig | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.requests = requests
        self.turns = turns
        self.loader = Loader(store=store)
        self.window_resolver: WindowResolver | None = None
        self.compactor: Compactor | None = None
        self.client_for: ClientProvider | None = None
        self.mu = threading.RLock()
        self.compress_timeout: float = 0.0
        self.config = config if config is not None else _default_context_config()
        self.model_metadata = (
            model_metadata if model_metadata is not None else ModelMetadataConfig()
        )
        self.compact_model = ModelSelection()
        self.last_usage: dict[str, Usage] = {}

    # -- 配置 ---------------------------------------------------------------
    def configure(
        self,
        ctx_cfg: ContextConfig,
        metadata: ModelMetadataConfig,
        providers: Any,
        compact_model: ModelSelection,
        client_for: ClientProvider | None,
    ) -> None:
        """装配窗口解析器、压缩后端与加载器。对应 Go ``configure``。"""
        ctx_cfg = ctx_cfg.normalized()
        with self.mu:
            self.config = ctx_cfg
            self.model_metadata = metadata
            self.compact_model = compact_model
            self.loader = Loader(store=self.store)
            self.window_resolver = new_window_resolver(metadata, providers, client_for)
            self.compactor = Compressor(client_for=client_for)
            self.client_for = client_for

    def set_compress_timeout(self, seconds: float) -> None:
        """压缩超时（秒）。对应 Go ``r.compressTimeout`` 的赋值点。"""
        with self.mu:
            self.compress_timeout = float(seconds or 0.0)

    # -- 压缩后端 -----------------------------------------------------------
    def compactor_for(
        self, provider_name: str, model: str
    ) -> tuple[Compactor | None, CompactionChoice]:
        """按协议能力选择压缩后端并说明是否回退。对应 Go ``compactorFor``。

        目前只有客户端后端：即使适配器声称支持服务端原生压缩，也必须回退到客户端压缩
        并让调用方记一条审计 —— "协议支持但实现没跟上" 不能变成压缩静默失效。
        """
        with self.mu:
            compactor = self.compactor
            client_for = self.client_for
        if compactor is None:
            return None, CompactionChoice()
        choice = CompactionChoice(backend=compactor.name())
        if client_for is None:
            return compactor, choice
        caps = protocol_capabilities_of(client_for(provider_name), model)
        if caps.native_compaction:
            choice.fallback_reason = (
                "protocol_advertises_native_compaction_without_backend"
            )
        return compactor, choice

    # -- 加载 ---------------------------------------------------------------
    async def load(self, ctx: Any, session_id: str) -> LoadedContext:
        """对应 Go ``load``。"""
        with self.mu:
            loader = self.loader
        return await loader.load(ctx, session_id)

    async def load_raw_messages(self, ctx: Any, session_id: str) -> list[Any]:
        """对应 Go ``loadRawMessages``。"""
        with self.mu:
            loader = self.loader
        return await loader.load_raw_messages(ctx, session_id)

    # -- 压缩模型 -----------------------------------------------------------
    def compact_selection(self, fallback: ModelSelection) -> ModelSelection:
        """配置了压缩模型就用它，否则用 ``fallback``。对应 Go ``compactSelection``。"""
        with self.mu:
            selected = self.compact_model
        if selected.provider and selected.model:
            return selected
        return fallback

    def configured_compact_model(self) -> ModelSelection:
        """对应 Go ``configuredCompactModel``。"""
        with self.mu:
            return self.compact_model.copy()

    def set_compact_model(self, selection: ModelSelection) -> None:
        """对应 Go ``setCompactModel``。"""
        with self.mu:
            self.compact_model = selection.copy()

    # -- 长消息策略 ---------------------------------------------------------
    def overflow_mode(self) -> str:
        """全局长消息保护策略。对应 Go ``overflowMode``。"""
        with self.mu:
            return self.config.overflow_mode

    def user_original_max_runes(self) -> int:
        """压缩时每条用户原话保留的最大字符数。对应 Go ``userOriginalMaxRunes``。"""
        with self.mu:
            return self.config.user_original_max_runes

    # -- 预算 ---------------------------------------------------------------
    async def window_for(self, ctx: Any, selection: ModelSelection) -> int:
        """解析上下文窗口（带三级兜底）。对应 Go ``promptBudget`` 的前半段。"""
        with self.mu:
            resolver = self.window_resolver
            metadata = self.model_metadata
        window = 0
        if resolver is not None:
            window = await resolver.resolve(ctx, selection.provider, selection.model)
        return self._window_fallback(window, metadata)

    def window_for_sync(self, selection: ModelSelection) -> int:
        """同步版窗口解析（只用缓存/手工配置/默认值）。对应 Go ``promptBudget``。

        异步的 :meth:`window_for` 会问 provider 的 ``list_model_metadata``（并写缓存），
        同步版读同一份缓存 —— 因此在请求路径上解析过一次之后两者结果一致。同步版存在
        的原因是 ``should_compact`` / ``begin_chat_budget`` 这些调用点在 ``AgentSeams``
        里是同步返回值。
        """
        with self.mu:
            resolver = self.window_resolver
            metadata = self.model_metadata
        window = 0
        if resolver is not None:
            window = resolver.resolve_sync(selection.provider, selection.model)
        return self._window_fallback(window, metadata)

    @staticmethod
    def _window_fallback(window: int, metadata: Any) -> int:
        if window <= 0:
            window = int(getattr(metadata, "default_context_window", 0) or 0)
        if window <= 0:
            window = DEFAULT_CONTEXT_WINDOW
        return window

    async def prompt_budget(self, ctx: Any, selection: ModelSelection) -> PromptBudget:
        """本次请求的估算输入预算。对应 Go ``promptBudget``。"""
        window = await self.window_for(ctx, selection)
        return self._budget_for(window)

    def prompt_budget_sync(self, selection: ModelSelection) -> PromptBudget:
        """同步版预算（见 :meth:`window_for_sync`）。"""
        return self._budget_for(self.window_for_sync(selection))

    def _budget_for(self, window: int) -> PromptBudget:
        with self.mu:
            cfg = self.config
        return new_prompt_budget(
            window,
            cfg.max_prompt_ratio,
            cfg.single_message_max_ratio,
            cfg.reserve_output_tokens,
        )

    # -- 摘要 ---------------------------------------------------------------
    async def summarize_text(
        self, ctx: Any, selection: ModelSelection, text: str, max_tokens: int
    ) -> str:
        """把长用户消息压成摘要。对应 Go ``summarizeText``。"""
        with self.mu:
            compressor = self.compactor
            timeout = self.compress_timeout
        if compressor is None:
            raise ValueError("压缩后端未配置")

        chunk_limit = max_tokens
        budget = await self.prompt_budget(ctx, selection)
        if budget.input_limit > 0:
            chunk_limit = budget.input_limit
        request = SummarizeRequest(
            provider=selection.provider,
            model=selection.model,
            text=text,
            max_input_tokens=max_tokens,
            chunk_input_tokens=chunk_limit,
        )
        if timeout > 0:
            result = await asyncio.wait_for(
                compressor.summarize_text(ctx, request), timeout
            )
        else:
            result = await compressor.summarize_text(ctx, request)
        return (result.summary or "").strip()

    # -- 状态展示 -----------------------------------------------------------
    def status(
        self,
        ctx: Any,
        session_id: str,
        usage: Usage | None,
        selection: ModelSelection,
    ) -> str:
        """``/context`` 的状态文本。对应 Go ``contextRuntimeState.status``。

        同步（``ContextStatusService`` 协议要求同步返回值）；窗口走
        :meth:`window_for_sync`。
        """
        from atri_x.agent.context_usage import render_context_status

        with self.mu:
            ctx_cfg = self.config
        window = 0
        if selection.provider or selection.model:
            window = self.window_for_sync(selection)
        return render_context_status(
            usage,
            window,
            compact_enabled=ctx_cfg.compact_enabled,
            compact_trigger_ratio=ctx_cfg.compact_trigger_ratio,
        )

    # -- 用量 ---------------------------------------------------------------
    def record_usage(self, session_id: str, usage: Usage | None) -> None:
        """记住某会话最近一次用量。对应 Go ``recordUsage``。"""
        if usage is None:
            return
        with self.mu:
            self.last_usage[session_id] = usage

    def usage(self, session_id: str) -> Usage | None:
        """对应 Go ``usage``。"""
        with self.mu:
            return self.last_usage.get(session_id)

    async def reached_compact_threshold_async(
        self, ctx: Any, usage: Usage | None, selection: ModelSelection
    ) -> bool:
        """异步版阈值判定（窗口解析会问 provider 元数据）。"""
        from atri_x.contextmgr import UsageState

        with self.mu:
            ctx_cfg = self.config
            resolver = self.window_resolver
        if not self._threshold_precondition(ctx_cfg, usage, resolver):
            return False
        window = await resolver.resolve(ctx, selection.provider, selection.model)
        return UsageState(
            usage=usage,
            context_window=window,
            trigger_ratio=ctx_cfg.compact_trigger_ratio,
        ).reached_threshold()

    def reached_compact_threshold(
        self, ctx: Any, usage: Usage | None, selection: ModelSelection
    ) -> bool:
        """是否已达压缩阈值。对应 Go ``reachedCompactThreshold``。

        同步：``AgentSeams.should_compact`` 直接对返回值取 ``bool``，返回协程会被
        当成"永远为真"。窗口走 :meth:`window_for_sync`。
        """
        from atri_x.contextmgr import UsageState

        with self.mu:
            ctx_cfg = self.config
            resolver = self.window_resolver
        if not self._threshold_precondition(ctx_cfg, usage, resolver):
            return False
        window = self.window_for_sync(selection)
        return UsageState(
            usage=usage,
            context_window=window,
            trigger_ratio=ctx_cfg.compact_trigger_ratio,
        ).reached_threshold()

    @staticmethod
    def _threshold_precondition(cfg: Any, usage: Usage | None, resolver: Any) -> bool:
        return bool(
            cfg.compact_enabled
            and usage is not None
            and int(usage.total_tokens or 0) > 0
            and resolver is not None
        )

    # -- 压缩占用 -----------------------------------------------------------
    def compact_active(self, session_id: str) -> bool:
        """该会话是否正在压缩。对应 Go ``contextRuntimeState.compactActive``。"""
        turns = self.turns
        if turns is not None:
            snapshot = turns.snapshot(session_id)
            if getattr(snapshot, "phase", "") == PhaseCompact:
                return True
        requests = self.requests
        if requests is not None:
            for active in requests.list_by_session(session_id):
                if getattr(active, "kind", "") == KindCompress:
                    return True
        return False


def _default_context_config() -> ContextConfig:
    """``config.Default().Context`` 的等价物（代码默认值，不读资产）。"""
    from atri_x.config import default as default_config

    cfg = default_config()
    context = getattr(cfg, "context", None)
    return context if isinstance(context, ContextConfig) else ContextConfig()


def ensure_context_runtime(host: Any) -> ContextRuntimeState:
    """取宿主的上下文运行态；缺失时按宿主的 store/sessions/requests/turns 补建。"""
    agent = host_of(host)
    state = getattr(agent, "context_runtime", None)
    if isinstance(state, ContextRuntimeState):
        return state
    state = ContextRuntimeState(
        getattr(agent, "store", None),
        getattr(agent, "sessions", None),
        getattr(agent, "requests", None),
        getattr(agent, "turns", None),
        config=getattr(agent, "context_config", None),
    )
    agent.context_runtime = state
    timeout = getattr(agent, "compress_timeout", 0.0)
    if timeout:
        state.set_compress_timeout(timeout)
    return state


def compact_selection_for_session(host: Any, session: Any) -> ModelSelection:
    """按会话模式取压缩模型。对应 Go ``compactSelectionForSession``。"""
    mode = SessionModeWork
    if session is not None and getattr(session, "mode", ""):
        mode = session.mode
    return ensure_context_runtime(host).compact_selection(host.model_for_mode(mode))


class ContextRuntimeMixin:
    """``internal/agent/context_runtime.go`` 里挂在 ``*Agent`` 上的方法。"""

    def set_context_options(
        self,
        ctx_cfg: ContextConfig,
        metadata: ModelMetadataConfig,
        providers: Any,
        compact_model: ModelSelection,
    ) -> None:
        """装配上下文运行态。对应 Go ``SetContextOptions``。"""
        ensure_context_runtime(self).configure(
            ctx_cfg, metadata, providers, compact_model, self.client_for_provider
        )

    def compact_selection_for_session(self, session: Any) -> ModelSelection:
        """对应 Go ``compactSelectionForSession``。"""
        return compact_selection_for_session(self, session)

    def set_compress_timeout(self, seconds: float) -> None:
        """压缩超时（Go 里由装配代码写 ``r.compressTimeout``）。"""
        ensure_context_runtime(self).set_compress_timeout(seconds)


#: 可以装到 Agent 实例上的上下文方法（跨本组四个 context_* 模块）。
CONTEXT_METHODS: tuple[str, ...] = (
    "set_context_options",
    "compact_selection_for_session",
    "set_compress_timeout",
    "context_status",
    "record_usage",
    "usage_for_session",
    "persist_usage",
    "record_llm_origin",
    "should_compact",
    "compact_current",
    "compact_session",
    "compact_active",
    "detect_prompt_overflow",
    "handle_initial_prompt_overflow",
    "_reject_overflow",
    "guard_prompt_before_call",
    "load_session",
    "overflow_alert_text",
    "context_policy_status",
    "set_context_policy",
    "reset_context_policy",
    "context_overflow_mode",
    "context_overflow_snapshot",
    "set_context_overflow_snapshot",
    "context_overflow_for_scope",
    "set_context_overflow_for_scope",
    "consume_context_compact_seed",
)


class ContextRuntimeService(AgentService):
    """上下文服务门面（``agent.services['context_runtime']``）。

    同时满足 ``Deps.compact`` / ``Deps.context_status`` / ``Deps.context_policy``
    三个协议；方法由装到 Agent 上的同名单方法提供。

    **为什么把方法显式写出来而不是全靠 ``AgentService.__getattr__`` 转发**：
    ``typing.Protocol`` 的 ``runtime_checkable`` 判定走 ``type(obj)`` 上的属性，
    ``__getattr__`` 只在实例上兜底，纯转发型门面会被判成 ``False``。
    显式声明一遍既让 ``isinstance`` 成立，也让门面的协议面可静态检索。
    """

    # -- Deps.ContextStatusService -----------------------------------------
    def context_status(self, ctx: Any, session: Any) -> str:
        """对应 Go ``ContextStatusService.ContextStatus``。"""
        return self.agent.context_status(ctx, session)

    # -- Deps.CompactService ------------------------------------------------
    async def compact_current(self, ctx: Any, trigger_reason: str) -> str:
        """对应 Go ``CompactService.CompactCurrent``。"""
        return await self.agent.compact_current(ctx, trigger_reason)

    # -- Deps.ContextPolicyService -----------------------------------------
    def context_policy_status(self, ctx: Any) -> str:
        """对应 Go ``ContextPolicyService.ContextPolicyStatus``。"""
        return self.agent.context_policy_status(ctx)

    async def set_context_policy(self, ctx: Any, target: str, value: str) -> str:
        """对应 Go ``ContextPolicyService.SetContextPolicy``。"""
        return await self.agent.set_context_policy(ctx, target, value)

    async def reset_context_policy(self, ctx: Any, target: str) -> str:
        """对应 Go ``ContextPolicyService.ResetContextPolicy``。"""
        return await self.agent.reset_context_policy(ctx, target)


def install_context_services(agent: Any) -> ContextRuntimeService:
    """装配上下文服务：创建运行态、把方法装到 Agent、注册门面（幂等）。"""
    ensure_context_runtime(agent)
    install_methods_from_modules(agent, CONTEXT_METHODS)
    services = getattr(agent, "services", None)
    existing = services.get("context_runtime") if isinstance(services, dict) else None
    if isinstance(existing, ContextRuntimeService):
        return existing
    service = ContextRuntimeService(agent)
    if isinstance(services, dict):
        services["context_runtime"] = service
    return service


def install_methods_from_modules(agent: Any, names: tuple[str, ...]) -> list[str]:
    """按名字在 ``context_runtime`` / ``context_usage`` / ``context_compact`` /
    ``context_policy`` / ``context_seed`` 里查找方法并装到 Agent 上。"""
    owners = []
    for module_name in (
        "atri_x.agent.context_runtime",
        "atri_x.agent.context_usage",
        "atri_x.agent.context_compact",
        "atri_x.agent.context_policy",
        "atri_x.agent.context_seed",
    ):
        owners.append(importlib.import_module(module_name))
    installed: list[str] = []
    for name in names:
        if getattr(agent, name, None) is not None:
            continue
        for module in owners:
            for value in vars(module).values():
                if not isinstance(value, type):
                    continue
                fn = value.__dict__.get(name)
                if fn is None:
                    continue
                setattr(agent, name, types.MethodType(fn, agent))
                installed.append(name)
                break
            if name in installed:
                break
    return installed
