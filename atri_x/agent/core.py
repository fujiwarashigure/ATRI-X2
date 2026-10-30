"""Agent 核心：状态、构造与跨模块方法的薄转发。

对应 Go：``internal/agent/core.go``。

本文件只负责三件事：

1. 持有运行态字段（命名与 Go 的 struct 字段逐一对应，``PascalCase`` →
   ``snake_case``）；
2. ``new_options`` / ``new`` / ``new_with_prefixes`` 三个构造入口；
3. 把 Go 里散落在同一文件的方法转发到本包的对应模块
   （:mod:`atri_x.agent.output` / ``input`` / ``inbox`` / ``hooks`` …）。

主题实现放在同名的兄弟模块里；可选能力（模型目录、上下文压缩、群策略、提示词
构造、工具运行时）走 :class:`atri_x.agent.seams.AgentSeams` 的接缝，未装配时
降级而不是崩溃。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from atri_x.command import Router
from atri_x.completion import Service as CompletionService
from atri_x.delivery import Manager as DeliveryManager
from atri_x.delivery import new_manager as new_delivery_manager
from atri_x.hook import Event
from atri_x.hook.manager import NoopManager
from atri_x.platform import PlatformAdapter
from atri_x.request import (
    KindHook,
    KindTool,
    KindTurn,
    QueueConfig,
    new_manager_with_limits_and_queue,
)
from atri_x.security import Policy, default_policy
from atri_x.security import (
    DEFAULT_GRANT_TTL_SECONDS,
    GrantRegistry,
)
from atri_x.session import Config as SessionConfig
from atri_x.session import Service as SessionService
from atri_x.storage import Store, Session
from atri_x.turn import Manager as TurnManager

from atri_x.agent import (
    command_runtime,
    cron as cron_module,
    group_policy as group_policy_module,
    hooks as hooks_module,
    identity,
    inbox as inbox_module,
    input as input_module,
    logging as logging_module,
    ops as ops_module,
    output as output_module,
    prefix as prefix_module,
    state_watch,
    status as status_module,
    title as title_module,
    turn_gate,
)
from atri_x.agent.notice_queue import NoticeQueues
from atri_x.agent.options import (
    Options,
    set_cleanup_retention_days,
    set_sandbox_root,
    set_security_policy,
    set_session_idle_expiration,
    set_session_list_page_size,
    validate_options,
)
from atri_x.agent.platform_signals import (
    PlatformSubscriptions,
    close_platform_events,
    start_platform_events,
)
from atri_x.agent.core_forward import CoreForwardMixin
from atri_x.agent.seams import AgentSeams
from atri_x.agent.session_metadata import (
    SessionMetadata,
    decode_session_metadata,
    encode_session_metadata_into,
)

# ---------------------------------------------------------------------------
# 同僚组的 mixin（PORTING.md §3.1 的"组合方式"）
#
# ``Agent`` 由这些 mixin 组合而成，顺序无关紧要 —— 每个 mixin 自带 ``ensure_*`` 懒建，
# 宿主字段缺失不致命。唯一的硬约束是 :class:`AgentSeams` **放在基类列表最后**：
# 它是"没装配真实实现时的保守降级"，必须被真实的 mixin 覆盖。
#
# 方法安装点（``atri_x.agent.bootstrap.install_agent_services``）对这些名字是
# "缺什么装什么"，因此继承之后不会重复绑定。
# ---------------------------------------------------------------------------
from atri_x.agent.asr import AsrMixin  # noqa: E402
from atri_x.agent.budget_chat import BudgetMixin  # noqa: E402
from atri_x.agent.character_directive import CharacterDirectiveMixin  # noqa: E402
from atri_x.agent.context_compact import ContextCompactMixin  # noqa: E402
from atri_x.agent.context_policy import (  # noqa: E402
    ContextPolicyMixin,
    ensure_context_overflow_state,
)
from atri_x.agent.context_runtime import ContextRuntimeMixin  # noqa: E402
from atri_x.agent.context_seed import ContextSeedMixin  # noqa: E402
from atri_x.agent.context_usage import ContextUsageMixin  # noqa: E402
from atri_x.agent.cron import CronMixin  # noqa: E402
from atri_x.agent.group_knowledge import GroupKnowledgeMixin  # noqa: E402
from atri_x.agent.group_policy import GroupPolicyMixin  # noqa: E402
from atri_x.agent.group_runtime import (  # noqa: E402
    GroupRuntimeMixin,
    RuntimeSectionsMixin,
)
from atri_x.agent.group_services_signups import SignupServicesMixin  # noqa: E402
from atri_x.agent.inbound_media import InboundMediaMixin  # noqa: E402
from atri_x.agent.inbound_speaker import InboundSpeakerMixin  # noqa: E402
from atri_x.agent.media_output import MediaOutputMixin  # noqa: E402
from atri_x.agent.member_panel import MemberPanelMixin  # noqa: E402
from atri_x.agent.model_agent import ModelMixin  # noqa: E402
from atri_x.agent.model_list import ModelListMixin  # noqa: E402
from atri_x.agent.model_policy import ModelPolicyMixin  # noqa: E402
from atri_x.agent.model_snapshot import ModelSnapshotMixin  # noqa: E402
from atri_x.agent.model_state import RuntimeStateMixin  # noqa: E402
from atri_x.agent.ratelimit import RateLimitMixin  # noqa: E402
from atri_x.agent.tool_auth import ToolAuthMixin  # noqa: E402
from atri_x.agent.tool_cache import ToolCacheMixin  # noqa: E402
from atri_x.agent.tool_directive import ToolDirectiveMixin  # noqa: E402
from atri_x.agent.tool_directive_skill import SkillDirectiveMixin  # noqa: E402
from atri_x.agent.tool_tag_config import ToolTagConfigMixin  # noqa: E402
from atri_x.agent.tool_transcript import ToolTranscriptMixin  # noqa: E402
from atri_x.agent.tools import ToolRuntimeMixin  # noqa: E402
from atri_x.agent.turn_override import TurnOverrideMixin  # noqa: E402
from atri_x.agent.vision import VisionMixin  # noqa: E402

__all__ = [
    "Agent",
    "FileSoulProvider",
    "SoulProvider",
    "StaticSoulProvider",
    "new",
    "new_options",
    "new_with_prefixes",
]


class SoulProvider:
    """系统提示里"灵魂"文本的来源。对应 Go ``SoulProvider``。"""

    def system_prompt(self, ctx: Any, scope_id: str) -> str:  # pragma: no cover - 协议
        raise NotImplementedError


@dataclass
class StaticSoulProvider:
    """固定灵魂文本。对应 Go ``staticSoulProvider``。"""

    prompt: str = ""

    def system_prompt(self, ctx: Any, scope_id: str) -> str:
        return self.prompt


class Agent(
    CoreForwardMixin,
    ModelMixin,
    ModelListMixin,
    RuntimeStateMixin,
    ModelPolicyMixin,
    ModelSnapshotMixin,
    BudgetMixin,
    ContextRuntimeMixin,
    ContextPolicyMixin,
    ContextUsageMixin,
    ContextCompactMixin,
    ContextSeedMixin,
    RateLimitMixin,
    CharacterDirectiveMixin,
    ToolDirectiveMixin,
    SkillDirectiveMixin,
    TurnOverrideMixin,
    ToolTagConfigMixin,
    ToolAuthMixin,
    ToolCacheMixin,
    ToolTranscriptMixin,
    ToolRuntimeMixin,
    GroupPolicyMixin,
    GroupKnowledgeMixin,
    GroupRuntimeMixin,
    RuntimeSectionsMixin,
    SignupServicesMixin,
    MemberPanelMixin,
    CronMixin,
    AsrMixin,
    VisionMixin,
    InboundMediaMixin,
    InboundSpeakerMixin,
    MediaOutputMixin,
    AgentSeams,
):
    """最小可用的 Agent 核心。对应 Go ``Agent``。

    字段与 Go 同名（snake_case）；并发原语按 PORTING.md §1.6 选择（异步用
    ``asyncio.Lock``，只在同步短临界区用的状态用 ``threading.Lock``）。
    基类里的同僚 mixin 按 PORTING.md §3.1 组合；:class:`AgentSeams` **必须在最后**。
    """

    def __init__(self, opts: Options) -> None:
        validate_options(opts)
        self.platform: PlatformAdapter | None = opts.platform
        self.platform_senders: dict[str, Any] = {}
        self.clients: dict[str, Any] = dict(opts.clients or {})
        self.providers: dict[str, Any] = dict(opts.providers or {})
        self.mode_models: dict[str, Any] = dict(opts.mode_models or {})
        self.model_profiles: dict[str, Any] = dict(opts.model_profiles or {})
        self.model_aliases: dict[str, str] = dict(opts.model_aliases or {})
        self.tool_profiles: dict[str, list[str]] = dict(opts.tool_profiles or {})
        self.tool_aliases: dict[str, str] = dict(opts.tool_aliases or {})
        self.image_profiles: dict[str, bool] = dict(opts.image_profiles or {})
        self.image_aliases: dict[str, str] = dict(opts.image_aliases or {})
        self.actor_id = "cli:local"
        self.scope_id = "local"

        # -- 运行态 ---------------------------------------------------------
        self.services: dict[str, Any] = {}
        self.command_deps: Any = None
        self.model_runtime: Any = _new_model_runtime(self)
        self.store: Store | None = opts.store
        self.media = opts.media
        self.title_gen = title_module.TitleGenerator(
            primary=self.client_for_provider(_work_provider(self)),
            primary_model=_work_model(self),
            naming=_naming_client(self, opts),
            naming_model=_naming_field(opts, "model"),
            naming_source=_naming_field(opts, "provider"),
        )
        self.sessions = SessionService(
            opts.store,
            opts.session_config or SessionConfig(),
            self.title_gen,
            opts.naming_notifier,
        )
        self.turns = TurnManager()
        # ``[ops]`` 的并发上限与排队旋钮在这里真正接线（此前只被解析、没有消费者，
        # 并发因此无上限）。没有配置这些旋钮的宿主仍拿到空 limits + 无队列的旧行为。
        request_limits, request_queue = _request_settings_from_ops(opts.ops)
        self.requests = new_manager_with_limits_and_queue(
            0.0, request_limits, request_queue
        )
        self.inbox: inbox_module.SessionInbox | None = None
        self.inbox_mu = threading.Lock()
        self.commands = Router(opts.command_prefixes)
        self.command_executor = command_runtime.CommandExecutor()
        self.completion: CompletionService | None = None
        self.soul: SoulProvider = (
            StaticSoulProvider(prompt="You are a helpful assistant.")
            if not (opts.soul_path or "").strip()
            else _file_soul_provider(opts.soul_path)
        )
        self.security_policy: Policy = opts.security_policy or default_policy()
        self.hooks: Any = opts.hook_manager or NoopManager()
        self.hook_runtime: Any = opts.hook_runtime
        self.outputs: DeliveryManager = opts.output_manager or new_delivery_manager()
        self.notice_queues = NoticeQueues()
        self.platform_events = PlatformSubscriptions()
        self.prompt_builder: Any = None
        self.tool_runtime: Any = _ToolRuntime()
        self.tool_provider: Any = opts.tool_provider
        self.skills: Any = opts.skills
        self.characters = opts.character_store
        self.angel_memory = opts.angel_memory
        self.angel_memory_forget_on_recall = bool(
            getattr(opts.angel_memory_config, "forget_on_recall_enabled", lambda: False)()
        )
        self.self_learning = opts.self_learning
        self.resident_memory = opts.resident_memory_store
        self.file_backups = opts.file_backups
        self.doctor = opts.doctor
        self.logger: Any = None
        self.audit_logger: Any = None
        self.log_reader: Any = None
        self.logs: Any = opts.logs
        self.state_path: str = opts.state_path or ""
        self.state_mod_time: Any = None
        self.state_mu = threading.Lock()
        self.state_write_mu = threading.Lock()
        self.state_watch_mu = threading.Lock()
        self.state_watch_started = False
        self.state_watch_task: asyncio.Task | None = None
        self.status_mu = threading.Lock()
        self.runtime_status: dict[str, Any] = {}
        self.turn_mu = threading.Lock()
        self.turn_states: dict[str, bool] = {}
        self.message_work_mu = threading.Lock()
        self.message_work: dict[str, Any] = {}
        self.recall_mu = threading.Lock()
        self.recent_recalls: dict[str, Any] = {}
        self.vision_fallback_mu = threading.Lock()
        self.vision_fallback_notified: dict[str, bool] = {}
        # 授权登记表（评估 §B1）：``/confirmall`` / ``/confirmtool`` 的放行凭据。
        # 进程内、带 TTL（默认 10 分钟）、绑定 scope + actor；``/panic`` 清空。
        self.confirm_grant_ttl_seconds = float(
            getattr(opts, "confirm_grant_ttl_seconds", 0.0) or 0.0
        )
        self.grants = GrantRegistry(
            self.confirm_grant_ttl_seconds
            if self.confirm_grant_ttl_seconds > 0
            else DEFAULT_GRANT_TTL_SECONDS
        )
        self.discovered_tools: dict[str, dict[str, Any]] = {}
        self.group_policy: dict[str, Any] = dict(opts.group_policy or {})
        self.group_knowledge: dict[str, list[Any]] = {}
        self.session_commands: Any = None
        self.turn_directives = opts.turn_directives or _default_turn_directives()
        # Python 版新增：``[agent.dsh]`` 深度路由配置（状态在首次路由时才建）。
        self.dsh_config = opts.dsh_config
        # Python 版新增：``[tool_preview]`` / ``[reactions]``。
        # ``tool_preview_texts`` 是工具运行器认识的数据对象（见
        # ``atri_x.toolrun.preview``）；``reaction_service`` 是表情图库的进程内单例，
        # 配置关闭时仍会构造（方便 GUI/命令查看状态），但 ``decide`` 一律返回 disabled。
        self.tool_preview_config = opts.tool_preview_config
        self.tool_preview_texts = _tool_preview_texts(opts.tool_preview_config)
        self.reactions_config = opts.reactions_config
        self.reaction_service = _reaction_service(opts.reactions_config)
        # Python 版新增：``[quiet]`` 全局静默时段（评估 §E）。``None`` = 关闭。
        self.quiet_config = opts.quiet_config
        # Python 版新增：``[participation]`` 群聊参与状态机（评估 §D）。``None`` = 关闭。
        self.participation_config = opts.participation_config
        # Python 版新增：统一时间服务（``[time]``）。``None`` = 不做提示词时间注入，
        # 与 1.0.9 行为一致；``time_config.inject_context = false`` 时注入降级为"只写时区"。
        self.time_service = opts.time_service
        self.time_config = opts.time_config
        self.time_verifier = opts.time_verifier
        self.participation_states: dict[str, Any] = {}
        self.participation_mu = threading.Lock()
        self.tools_config = opts.tools_config or _default_tools_config()
        self.context_config = opts.context_config
        self.llm_request_config = opts.llm_request_config
        self.ops = opts.ops
        # 预算与定价是 BudgetMixin 的宿主字段：Go 里是 ``Options`` 的直接映射，
        # 缺了会让 ``begin_chat_budget`` 抛 AttributeError；未配置时给零限额默认值。
        self.budget_limits = opts.budget_limits or _default_budget_limits()
        self.pricing = opts.pricing
        self.model_metadata = opts.model_metadata
        self.compact_model = opts.compact_model
        self.idle_expiration = None
        self.sandbox_root = opts.sandbox_root
        self.media_retention_days = opts.media_retention_days
        self.response_timeout = _response_timeout(opts.llm_request_config)
        self.tool_timeout = _seconds(getattr(opts.ops, "tool_timeout_seconds", 0))
        self.hook_timeout = _seconds(getattr(opts.ops, "hook_timeout_seconds", 0))
        self.compress_timeout = _seconds(getattr(opts.ops, "compress_timeout_seconds", 0))
        self.user_confirmation_timeout = input_module.DEFAULT_USER_CONFIRMATION_TIMEOUT
        self.history_enabled_default = True
        self._compacting: set[str] = set()

        set_session_idle_expiration(self, opts.session_idle_expiration)
        set_sandbox_root(self, opts.sandbox_root)
        set_security_policy(self, opts.security_policy)
        _install_command_state(self, opts)
        _install_runtime_services(self, opts)
        # 旁路 LLM 用量统一入口：压缩/群分析/视觉/标题/图片重写都只调 record_side_usage。
        from atri_x.agent.chat_observe import audit_side_usage
        from atri_x.llm.usage_sink import set_usage_sink

        set_usage_sink(lambda payload: audit_side_usage(self, payload))
        self.command_executor = _build_command_executor(self)
        self.completion = _build_completion(self)
        self.prompt_builder = _lazy_prompt_builder(opts)
        _install_tool_runtime(self, opts)
        if opts.logs is not None:
            # 生产装配只把 logs 存进 opts。不在这里补挂通道的话 logger/audit_logger
            # 全程是 None：LLM 输出日志、llm_usage 审计（/usage）、异常 WARN 全静默丢。
            try:
                logging_module.set_log_manager(self, opts.logs)
            except Exception:  # noqa: BLE001 - 旧装配/测试桩的 logs 可能不完整
                pass

    # -- 构造后装配 ---------------------------------------------------------

    def set_logger(self, logger: Any) -> None:
        logging_module.set_logger(self, logger)

    def audit(self, event: str, *attrs: Any) -> None:
        """写一条 INFO 级审计记录。对应 Go ``Agent.audit``。"""
        logging_module.audit(self, event, *attrs)

    def audit_debug(self, event: str, *attrs: Any) -> None:
        """对应 Go ``Agent.auditDebug``。"""
        logging_module.audit_debug(self, event, *attrs)

    def audit_warn(self, event: str, *attrs: Any) -> None:
        """对应 Go ``Agent.auditWarn``。"""
        logging_module.audit_warn(self, event, *attrs)

    def audit_error(self, event: str, *attrs: Any) -> None:
        """对应 Go ``Agent.auditError``。"""
        logging_module.audit_error(self, event, *attrs)

    def set_log_manager(self, logs: Any) -> None:
        """对应 Go ``Agent.SetLogManager``。"""
        logging_module.set_log_manager(self, logs)

    def set_output_manager(self, manager: Any) -> None:
        output_module.set_output_manager(self, manager)

    def set_hook_manager(self, manager: Any) -> None:
        hooks_module.set_hook_manager(self, manager)

    def set_hook_runtime(self, router: Any) -> None:
        hooks_module.set_hook_runtime(self, router)

    def register_platform_sender(self, name: str, sender: Any) -> None:
        output_module.register_platform_sender(self, name, sender)

    def set_session_list_page_size(self, size: int) -> None:
        set_session_list_page_size(self, size)

    def set_cleanup_retention_days(self, days: int) -> None:
        set_cleanup_retention_days(self, days)

    def set_security_policy(self, policy: Any) -> None:
        set_security_policy(self, policy)

    def set_sandbox_root(self, root: str) -> None:
        set_sandbox_root(self, root)

    def set_session_idle_expiration(self, cfg: Any) -> None:
        set_session_idle_expiration(self, cfg)

    def query_logs(self, ctx: Any, query: Any) -> Any:
        return logging_module.query_logs(self, ctx, query)

    async def reload_runtime_state(self, ctx: Any) -> Any:
        return await state_watch.reload_runtime_state(self, ctx)

    def runtime_state_status(self) -> Any:
        return state_watch.runtime_state_status(self)

    def start_runtime_state_watch(self, ctx: Any = None) -> None:
        state_watch.start_runtime_state_watch(self, ctx)

    # -- 模型与客户端 -------------------------------------------------------

    def client_for_provider(self, provider_name: str) -> Any:
        """按 provider 取 LLM 客户端。对应 Go ``Agent.clientForProvider``。

        ``agent.clients`` 是**唯一出处**（构造期的 ``opts.clients``，装配期由 app 层
        继续登记）；:class:`~atri_x.agent.model.ModelRuntimeState` 里的客户端表只是
        构造时的一份快照，用它兜底。
        """
        live = getattr(self, "clients", None) or {}
        client = live.get(provider_name)
        if client is None:
            client = ModelMixin.client_for_provider(self, provider_name)
        if client is None and live:
            client = next(iter(live.values()))
        if client is None:
            raise RuntimeError(f"client not found for provider {provider_name!r}")
        return client

    # -- 身份与作用域 -------------------------------------------------------

    def scope(self, ctx: Any) -> Any:
        return identity.scope(self, ctx)

    def base_scope(self, ctx: Any) -> Any:
        return identity.base_scope(self, ctx)

    def actor(self, ctx: Any) -> Any:
        return identity.actor(self, ctx)

    def conversation_meta(self, ctx: Any, scope: Any) -> Any:
        return identity.conversation_meta(self, ctx, scope)

    # -- 命令前缀 -----------------------------------------------------------

    def command_prefix(self) -> str:
        return prefix_module.command_prefix(self)

    def command_prefixes(self) -> list[str]:
        return prefix_module.command_prefixes(self)

    def is_cancel_command(self, text: str) -> bool:
        return prefix_module.is_cancel_command(self, text)

    # -- 输出 ---------------------------------------------------------------

    async def send_chat(self, ctx: Any, text: str) -> None:
        await output_module.send_chat(self, ctx, text)

    async def send_chat_with_receipt(self, ctx: Any, text: str) -> Any:
        return await output_module.send_chat_with_receipt(self, ctx, text)

    async def send_notice(self, ctx: Any, notice: Any) -> Any:
        return await output_module.send_notice_public(self, ctx, notice)

    async def send_outputs(self, ctx: Any, outputs: list[Any] | None) -> None:
        await output_module.send_outputs(self, ctx, outputs)

    async def prepare_assistant_output(self, ctx: Any, point: str, text: str) -> str:
        return await output_module.prepare_assistant_output(self, ctx, point, text)

    def map_sent_assistant_message(
        self, ctx: Any, session_id: str, message_id: str, receipt: Any
    ) -> None:
        output_module.map_sent_assistant_message(self, ctx, session_id, message_id, receipt)

    # -- 运行状态 -----------------------------------------------------------

    def record_runtime_status(self, snapshot: Any) -> Any:
        return status_module.record_runtime_status(self, snapshot)

    def publish_runtime_status(self, ctx: Any, snapshot: Any) -> None:
        status_module.publish_runtime_status(self, ctx, snapshot)

    def runtime_status_for_session(self, session_id: str) -> Any:
        return status_module.runtime_status_for_session(self, session_id)

    # -- 回合闸门 -----------------------------------------------------------

    def mark_turn_canceled(self, request_id: str) -> None:
        turn_gate.mark_turn_canceled(self, request_id)

    def clear_turn_state(self, request_id: str) -> None:
        turn_gate.clear_turn_state(self, request_id)

    def turn_output_allowed(self, ctx: Any) -> bool:
        return turn_gate.turn_output_allowed(self, ctx)

    # -- Hook ---------------------------------------------------------------

    async def run_hook(self, ctx: Any, event: Event) -> Event:
        return await hooks_module.run_hook(self, ctx, event)

    async def notify_hook(self, ctx: Any, event: Event) -> None:
        await hooks_module.notify_hook(self, ctx, event)

    async def notify_hook_error(self, ctx: Any, source: Event, err: BaseException) -> None:
        await hooks_module.notify_hook_error(self, ctx, source, err)

    async def route_hook(self, ctx: Any, event: Event) -> tuple[Event, bool, Any]:
        return await hooks_module.route_hook(self, ctx, event)

    def cancel_hook_route(self, event: Event) -> bool:
        return hooks_module.cancel_hook_route(self, event)

    async def observe_hook_run(self, ctx: Any, event: Event, info: Any) -> tuple[Any, Any]:
        return await hooks_module.observe_hook_run(self, ctx, event, info)

    def fill_hook_context(self, ctx: Any, event: Event) -> Event:
        return hooks_module.fill_hook_context(self, ctx, event)

    def hook_session(self, session: Session | None) -> Any:
        return hooks_module.hook_session(session)

    def actor_context(self, actor: Any) -> Any:
        return hooks_module.actor_context(actor)

    async def handle_platform_connected(self, ctx: Any, platform_name: str) -> None:
        await hooks_module.handle_platform_connected(self, ctx, platform_name)

    # -- 平台事件与信号 -----------------------------------------------------

    async def handle_platform_event(self, ctx: Any, event: Any) -> None:
        from atri_x.agent import events as events_module

        events_module.handle_platform_event(self, ctx, event)

    async def start_platform_events(self, ctx: Any) -> None:
        await start_platform_events(self, ctx)

    async def close_platform_events(self, ctx: Any) -> None:
        await close_platform_events(self, ctx)

    async def close_notice_queues(self, ctx: Any) -> None:
        await self.notice_queues.close(ctx)

    # -- 收件箱 -------------------------------------------------------------

    async def use_inbound_inbox(self, ctx: Any) -> bool:
        return await inbox_module.use_inbound_inbox(self, ctx)

    async def submit_inbound(self, ctx: Any, session_id: str, text: str) -> None:
        await inbox_module.submit_inbound(self, ctx, session_id, text)

    def cancel_inbox_session(self, session_id: str) -> int:
        return inbox_module.cancel_inbox_session(self, session_id)

    def cancel_inbox_fair_key(self, fair_key: str) -> int:
        return inbox_module.cancel_inbox_fair_key(self, fair_key)

    def cancel_inbox_scope(self, scope_key: str) -> int:
        return inbox_module.cancel_inbox_scope(self, scope_key)

    def cancel_inbox_message(self, message_key: str) -> int:
        return inbox_module.cancel_inbox_message(self, message_key)

    def inbox_pending_for_fair_key(self, fair_key: str) -> int:
        return inbox_module.inbox_pending_for_fair_key(self, fair_key)

    async def inbox_session(self, ctx: Any, session_id: str) -> Session | None:
        from atri_x.agent.inbox import _inbox_session_impl

        return await _inbox_session_impl(self, ctx, session_id)

    async def wait_inbox_idle(self, ctx: Any, session_id: str) -> None:
        from atri_x.agent.inbox import _wait_inbox_idle_impl

        await _wait_inbox_idle_impl(self, ctx, session_id)

    # -- 输入 ---------------------------------------------------------------

    async def handle_input(self, ctx: Any, text: str) -> None:
        await input_module.handle_input(self, ctx, text)

    async def handle_session_input(self, ctx: Any, session: Session, text: str) -> None:
        await input_module.handle_session_input(self, ctx, session, text)

    async def continue_command_input(self, ctx: Any, continuation: Any) -> None:
        await input_module.continue_command_input(self, ctx, continuation)

    async def handle_append_confirmation_input(
        self, ctx: Any, session: Session, text: str
    ) -> None:
        await input_module.handle_append_confirmation_input(self, ctx, session, text)

    async def handle_risk_confirmation_input(
        self, ctx: Any, session_id: str, text: str
    ) -> None:
        await input_module.handle_risk_confirmation_input(self, ctx, session_id, text)

    async def expire_idle_current_session(self, ctx: Any) -> None:
        await input_module.expire_idle_current_session(self, ctx)

    def confirmation_wait_timeout(self, ctx: Any) -> float:
        return input_module.confirmation_wait_timeout(self, ctx)


    # -- 会话 metadata ------------------------------------------------------

    def session_metadata(self, session: Session | None) -> SessionMetadata:
        return decode_session_metadata(getattr(session, "metadata", "") or "")

    def store_session_metadata(self, session: Session, metadata: SessionMetadata) -> None:
        session.metadata = encode_session_metadata_into(
            getattr(session, "metadata", "") or "", metadata
        )

    def group_default_model_selection(self, ctx: Any) -> tuple[Any, bool]:
        """本群默认模型 ``(selection, has_group_default)``。对应 Go ``groupDefaultModelSelection``。

        Go 里这是 ``*Agent`` 的**直接实现**（``group_policy.go:865``：读群策略 → 解析模型
        目录 → 校验允许），从不经 ``Deps.GroupPolicy`` 二次分派。Python 侧若先查
        ``self._svc("group_policy")``，``bootstrap._wire_deps`` 按 Go 把 ``a`` 写进
        ``deps.group_policy`` 后就会拿到 ``Agent`` 自己 → 自递归。

        Go 的签名是 ``(config.ModelSelection, bool)``，而
        :mod:`atri_x.agent.group_policy` 的模块级实现只返回 ``ModelSelection | None``
        （``None`` 表示"没有群默认"）。这里做一次归一，保证调用点
        （``chat.model_selection_for_turn``）永远拿到二元组。
        """
        result = group_policy_module.group_default_model_selection(self, ctx)
        if isinstance(result, tuple):
            return result
        return (result, True) if result is not None else (None, False)

    def group_tool_allowlist(
        self, ctx: Any, session: Any = None
    ) -> dict[str, bool] | None:
        """群工具白名单（``None`` 表示不限制）。对应 Go ``Agent.groupToolAllowlist``。

        Go 里同样是直接实现（``tool_auth.go:53``），不查 ``Deps.GroupPolicy``；
        照抄 Go 的方法形状再叠加"优先用服务"的分派会自递归（见上）。

        本仓扩展：``session`` 用于选取当前会话模式的分模式白名单
        （``tool_allowlist_chat`` / ``tool_allowlist_work``）。
        """
        return ToolAuthMixin.group_tool_allowlist(self, ctx, session)

    def filter_group_feature_schemas(self, ctx: Any, schemas: list[Any]) -> list[Any]:
        """按群策略裁掉不可用功能的 schema。对应 Go ``Agent.filterGroupFeatureSchemas``。

        直接实现（``tool_auth.go:138``），理由同 :meth:`group_tool_allowlist`。
        """
        return ToolAuthMixin.filter_group_feature_schemas(self, ctx, schemas)

    def background_session_must_stop(self, ctx: Any, session_id: str) -> bool:
        """后台 turn 在安全点是否必须停下。对应 Go ``backgroundSessionMustStop``。

        已注册 ``services["foreground"]`` 时以它为准（前台接管服务），否则走
        :mod:`atri_x.agent.cron` 的接管标记检查。
        """
        service = self._svc("foreground")
        if service is not None and hasattr(service, "background_session_must_stop"):
            return bool(service.background_session_must_stop(ctx, session_id))
        return cron_module.background_session_must_stop(self, ctx, session_id)

    # -- 运行态分区（宿主扩展钩子） -----------------------------------------

    def apply_extra_runtime_sections(self, state: Any) -> None:
        """把 ``state.toml`` 的群策略 / 群知识 / 群服务 / 群运行态四段合并进内存。

        对应 Go ``applyRuntimeState`` 里属于 agent-tools 组的那几行。
        ``RuntimeStateMixin`` 只声明了空实现，这里显式接到
        :meth:`atri_x.agent.group_runtime.RuntimeSectionsMixin.apply_extra_runtime_sections`
        （不依赖 MRO 顺序，接口更稳）。
        """
        RuntimeSectionsMixin.apply_extra_runtime_sections(self, state)

    def publish_extra_runtime_sections(self, state: Any) -> None:
        """把内存里的四个分区写进 ``state``。对应 Go ``saveRuntimeState`` 的同段。"""
        RuntimeSectionsMixin.publish_extra_runtime_sections(self, state)

    # -- 运维 ---------------------------------------------------------------

    def active_requests(self) -> Any:
        return ops_module.active_requests(self)

    def runtime_status_snapshot(self, session_id: str) -> Any:
        return status_module.runtime_status_for_session(self, session_id)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        platform = self.platform.name() if self.platform is not None else ""
        return f"<Agent platform={platform!r} sessions={len(self.sessions.__dict__) > 0}>"


def _default_budget_limits() -> Any:
    """未配置预算时的默认限额（全 0 = 不设上限）。对应 Go ``config.BudgetLimitsConfig``。"""
    from atri_x.config import BudgetLimitsConfig

    return BudgetLimitsConfig()


def _tool_preview_texts(config: Any) -> Any:
    """``[tool_preview]`` 配置 → 工具运行器认识的 :class:`PreviewTexts`。"""
    from atri_x.toolrun.preview import PreviewTexts

    return PreviewTexts.from_config(config)


def _reaction_service(config: Any) -> Any:
    """``[reactions]`` 配置 → 进程内 :class:`ReactionService`（未配置返回 ``None``）。"""
    from atri_x.agent.reactions import build_reaction_service

    return build_reaction_service(config)


def _positive_int(source: Any, name: str) -> int:
    """读一个"正整数开关"字段；缺失 / 非法 / ``<= 0`` 一律当 0（= 不限）。"""
    try:
        value = int(getattr(source, name, 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _request_settings_from_ops(ops: Any) -> tuple[dict[str, int], QueueConfig | None]:
    """把 ``[ops]`` 的并发旋钮映射成 request manager 的 limits 与排队配置。

    对应 :func:`atri_x.request.new_manager_with_limits_and_queue` 的两个入参：

    * ``limits`` 只收 **> 0** 的 kind（0 / 缺失 = 不限，与
      ``Manager._can_run_locked`` 的 ``limit <= 0`` 语义一致）。只接
      turn / tool / hook：上下文压缩（``compress``）目前没有 ``[ops]`` 字段，
      所以它没有并发上限。
    * ``queue`` 只在**确有队列配置**（``queue_max_size`` /
      ``queue_max_per_user`` / ``queue_max_per_scope`` /
      ``queue_wait_timeout_seconds`` 任一项 > 0）时构造；否则返回 ``None``，
      让未配置这些旋钮的宿主保持"并发满即拒绝、不排队"的旧行为。
      单独配置 ``queue_wait_kinds`` 不算"有队列"：``Manager._acquire`` 还要
      ``max_queue > 0`` 才会真的入队。

    纯函数（不读全局状态、不构造 Manager），便于离线断言。
    """
    limits: dict[str, int] = {}
    for kind, field_name in (
        (KindTurn, "max_concurrent_turns"),
        (KindTool, "max_concurrent_tools"),
        (KindHook, "max_concurrent_hooks"),
    ):
        limit = _positive_int(ops, field_name)
        if limit > 0:
            limits[kind] = limit

    max_queue = _positive_int(ops, "queue_max_size")
    max_queue_per_fair_key = _positive_int(ops, "queue_max_per_user")
    max_queue_per_scope = _positive_int(ops, "queue_max_per_scope")
    wait_timeout = _positive_int(ops, "queue_wait_timeout_seconds")
    if not (max_queue or max_queue_per_fair_key or max_queue_per_scope or wait_timeout):
        return limits, None
    return limits, QueueConfig(
        max_queue=max_queue,
        max_queue_per_fair_key=max_queue_per_fair_key,
        max_queue_per_scope=max_queue_per_scope,
        wait_timeout=float(wait_timeout),
        wait_kinds=getattr(ops, "queue_wait_kinds", None) or frozenset(),
    )


def _install_command_state(agent: Agent, opts: Options) -> None:
    try:
        from atri_x.agent.commands.session_module import SessionCommandState

        agent.session_commands = SessionCommandState(
            opts.session_list_page_size or 10, opts.cleanup_retention_days or 30
        )
    except Exception:  # noqa: BLE001 - 命令模块可选
        agent.session_commands = None


def _install_runtime_services(agent: Agent, opts: Options) -> None:
    """把构造参数里能直接映射的服务登记进 ``services``。"""
    mapping = {
        "context_runtime": None,
        "group_policy": opts.group_policy,
        "group_knowledge": opts.group_knowledge,
        "group_services": opts.group_services,
        "inbound_media": opts.media,
        "asr": opts.audio_transcriber,
    }
    for key, value in mapping.items():
        if value is None:
            continue
        if key == "group_policy" and isinstance(value, dict):
            continue
        agent.services[key] = value
    # 构造期 seed 每作用域的溢出覆盖。对应 Go ``core.go:336``
    # ``contextOverflow: cloneContextOverflow(opts.ContextOverflow)``；不 seed 的话
    # 只有 ``state.toml`` 的热加载路径会填它，构造完到第一次热加载之间是空的。
    ensure_context_overflow_state(agent).replace(opts.context_overflow)
    _seed_group_services_config(agent, opts)
    try:
        agent.command_deps = _build_deps(agent, opts)
    except Exception:  # noqa: BLE001 - 命令依赖可选
        agent.command_deps = None


def _seed_group_services_config(agent: Agent, opts: Options) -> None:
    """构造期固定群服务配置。对应 Go ``core.go:343`` ``groupServicesCfg: opts.GroupServices.Normalized()``。

    只把 ``opts.group_services`` 塞进 ``services["group_services"]`` 是不够的：读侧
    :func:`atri_x.agent.group_services_state.group_services_config` 取的是主机的
    ``group_services_cfg`` 属性（或 ``services["group_services_config"]``），两条都不是
    ``services["group_services"]``。于是 ``[group_services] enabled = false`` 完全无效：
    提醒派发循环照起、各项上限退回默认值，而且不报任何错。
    """
    try:
        from atri_x.config.app_sections import GroupServicesConfig  # 延迟导入
    except Exception:  # noqa: BLE001 - 配置层缺件时保持旧行为
        return
    raw = opts.group_services
    config = raw if isinstance(raw, GroupServicesConfig) else GroupServicesConfig()
    agent.group_services_cfg = config.normalized()


def _install_tool_runtime(agent: Agent, opts: Options) -> None:
    """构造期的工具运行时接线。对应 Go ``core.go:428-434``。

    Go::

        if opts.ToolRegistry != nil || opts.Skills != nil {
            a.SetToolRuntime(opts.ToolRegistry, opts.Skills)
        } else if opts.ToolProvider != nil {
            a.SetToolProvider(opts.ToolProvider)
        }
        a.SetToolConfig(opts.ToolsConfig)
        a.SetToolTagConfig(opts.ToolTagsPath, opts.ToolTags)

    少了这一步，``Options.tool_registry`` 就没人消费：``tool_runtime.registry`` 恒为
    ``None`` → ``tools_for_session`` 走 ``Manager(None, ...)`` → 永远返回空 schema
    —— 模型一个工具都拿不到，而且不报任何错。

    必须在 ``self.prompt_builder`` 建好之后调用：``SetToolRuntime`` /
    ``SetToolProvider`` / ``SetToolTagConfig`` 都会 ``rebuildSystemPrompt()``。
    """
    if opts.tool_registry is not None or opts.skills is not None:
        agent.set_tool_runtime(opts.tool_registry, opts.skills)
    elif opts.tool_provider is not None:
        agent.set_tool_provider(opts.tool_provider)
    agent.set_tool_config(opts.tools_config or _default_tools_config())
    agent.set_tool_tag_config(opts.tool_tags_path, opts.tool_tags)


def _naming_field(opts: Options, name: str) -> str:
    """``opts.naming_selection`` 的字段（没有就取 Go 的零值空串）。"""
    return str(getattr(opts.naming_selection, name, "") or "")


def _naming_client(agent: Agent, opts: Options) -> Any:
    """命名专用模型的客户端。对应 Go ``clients[namingSelection.Provider]``。

    该 provider 没配客户端时返回 ``None``，``TitleGenerator`` 会自动回退主模型
    （与 Go 里 map 取到零值的行为一致）。
    """
    provider = _naming_field(opts, "provider")
    if not provider:
        return None
    return (agent.clients or {}).get(provider)


def _build_deps(agent: Agent, opts: Options) -> Any:
    from atri_x.agent.commands.deps import Deps, register_default_modules

    deps = Deps(
        router=agent.commands,
        sessions=agent.sessions,
        requests=agent.requests,
        turns=agent.turns,
        store=agent.store,
        scope=agent.scope,
        characters=agent.characters,
        angel_memory=agent.angel_memory,
        resident_memory=agent.resident_memory,
        self_learning=agent.self_learning,
        audit=lambda event, *attrs: logging_module.audit(agent, event, *attrs),
        session_state=agent.session_commands,
        runtime_state=agent,
        runtime_status=agent.runtime_status_for_session,
        cancel_session_inbox=agent.cancel_inbox_session,
        file_backups=agent.file_backups,
        doctor=agent.doctor,
        grants=agent.grants,
        # Python 版新增：``/time``（超管）用的时间服务与网络校验器。
        time_service=agent.time_service,
        time_verifier=getattr(agent, "time_verifier", None),
        tool_allowlist=lambda ctx: group_policy_module.group_tool_allowlist_text(
            agent, ctx
        ),
    )
    register_default_modules(agent.commands, deps)
    return deps


def _build_command_executor(agent: Agent) -> command_runtime.CommandExecutor:
    return command_runtime.CommandExecutor(
        router=agent.commands,
        sessions=agent.sessions,
        turns=agent.turns,
        scope=agent.scope,
        compact_active=agent.compact_active,
        send_chat=agent.send_chat,
        send_notice=_notice_sender(agent),
        audit=lambda event, *attrs: logging_module.audit(agent, event, *attrs),
        handle_append=agent.handle_append_confirmation_input,
        handle_risk=agent.handle_risk_confirmation_input,
        continue_input=agent.continue_command_input,
        group_admin_grant=_group_admin_grant(agent),
    )


def _notice_sender(agent: Agent) -> Any:
    from atri_x.delivery import Notice, text as text_output

    async def _send(ctx: Any, text: str) -> BaseException | None:
        try:
            await agent.send_notice(ctx, Notice(outputs=[text_output(text)]))
        except BaseException as exc:  # noqa: BLE001 - 命令回执失败原样上报
            return exc
        return None

    return _send


def _group_admin_grant(agent: Agent) -> Any:
    service = agent.services.get("group_admin")
    if service is not None and hasattr(service, "group_admin_command_grant"):
        return service.group_admin_command_grant
    return None


# ---------------------------------------------------------------------------
# 构造入口（对应 Go ``New`` / ``NewWithPrefixes`` / ``NewWithOptions``）
# ---------------------------------------------------------------------------

# 这些名字的实现搬到了 atri_x.agent.core_build，这里保持同样的导入路径。
from atri_x.agent.core_build import (  # noqa: E402,F401
    FileSoulProvider,
    _ToolRuntime,
    _build_completion,
    _default_tools_config,
    _default_turn_directives,
    _file_soul_provider,
    _lazy_prompt_builder,
    _new_model_runtime,
    _response_timeout,
    _seconds,
    _work_model,
    _work_provider,
    new,
    new_options,
    new_with_prefixes,
)
