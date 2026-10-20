"""Agent 的可选能力接缝（seam）。

对应 Go：散布在 ``model.go`` / ``group_*.py`` / ``context_*.py`` / ``prompt.go`` /
``tools.go`` 等文件里的 Agent 方法，但 Python 侧这些主题由同僚模块实现。

本模块**只定义默认行为**：没有装配真实实现时，每个接缝都退化成"这一项不存在"
的保守答案（不唤醒、不放行、不压缩、不注入），从而保证 :class:`Agent` 在只有
平台 + 存储 + LLM 的情况下依然可构造、可测试、可跑通一条完整回合。

接缝的装配有两种方式，二者等价：

1. 直接给 ``agent.services`` 字典塞对象（键名见各方法里的 ``self._svc("...")``）；
2. 给 ``agent.command_deps``（``atri_x.agent.commands.deps.Deps``）的对应字段赋值。

方法名与 Go 的 Agent 方法逐一对应，便于交叉检索。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.llm import MessageSegment
from atri_x.storage import Message, SessionModeWork
from atri_x.turn import Input, Speaker

from atri_x.agent.segments import inbound_turn_input

__all__ = ["AgentSeams", "DirectiveOutcome"]


@dataclass
class DirectiveOutcome:
    """指令处理的中性结果，字段是同僚实现可能填的超集。

    ``text`` 是（可能被剥掉指令后的）正文；``err`` 非 ``None`` 时调用方应抛出它。
    """

    text: str = ""
    applied: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    injected_wrappers: list[str] = field(default_factory=list)
    existing_wrappers: list[str] = field(default_factory=list)
    mode_blocked: bool = False
    err: BaseException | None = None


class AgentSeams:
    """可选能力的默认实现。对应 Go 里那些"由别的文件提供"的 Agent 方法。"""

    # -- 服务查找 -----------------------------------------------------------

    def _svc(self, key: str) -> Any:
        """按名字取装配好的服务（``agent.services[key]``）。

        **宿主自己不算服务**：Go 的 ``Deps.GroupPolicy`` 等槽位直接接 ``a``
        （``core.go:454``），Python 侧 ``bootstrap._wire_deps`` 照抄了这一点。若
        ``_svc`` 把 ``Agent`` 自己当成"已装配的服务"，``hasattr`` 恒为真，
        ``service.foo(...)`` 就会原地调用 ``self.foo(...)`` → ``RecursionError``。
        """
        services = getattr(self, "services", None)
        if isinstance(services, dict):
            service = services.get(key)
            if service is not None and service is not self:
                return service
        deps = getattr(self, "command_deps", None)
        if deps is not None and hasattr(deps, key):
            service = getattr(deps, key)
            if service is not None and service is not self:
                return service
        return None

    # -- 群策略与运行态 -----------------------------------------------------

    def raw_group_policy(self, scope: Any) -> Any:
        """当前作用域的原始群策略。对应 Go ``Agent.rawGroupPolicy``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "raw_group_policy"):
            result = service.raw_group_policy(scope)
            if result is not None:
                return result
        return self._default_group_policy(scope)

    def _default_group_policy(self, scope: Any) -> Any:
        from atri_x.config import GroupPolicyConfig

        policies = getattr(self, "group_policy", None)
        if isinstance(policies, dict):
            key = getattr(scope, "platform_scope_id", "") or ""
            found = policies.get(key)
            if found is not None:
                return found
        return GroupPolicyConfig()

    def group_policy_for_scope(self, scope: Any) -> Any:
        """唤醒判定与提示文案用的群策略。对应 Go ``Agent.groupPolicyForScope``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "group_policy_for_scope"):
            result = service.group_policy_for_scope(scope)
            if result is not None:
                return result
        return self.raw_group_policy(scope)

    def group_merge_window(self, ctx: Any) -> float:
        """群消息合并窗口（秒，0 表示不合并）。对应 Go ``Agent.groupMergeWindow``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "group_merge_window"):
            return float(service.group_merge_window(ctx) or 0.0)
        policy = self.group_policy_for_scope(self.scope(ctx))
        millis = int(getattr(policy, "merge_window_ms", 0) or 0)
        return millis / 1000.0 if millis > 0 else 0.0

    def group_default_session_mode(self, ctx: Any) -> str:
        """群默认会话模式。对应 Go ``Agent.groupDefaultSessionMode``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "group_default_session_mode"):
            mode = service.group_default_session_mode(ctx)
            if mode:
                return mode
        policy = self.group_policy_for_scope(self.scope(ctx))
        mode = (getattr(policy, "default_mode", "") or "").strip()
        return mode or SessionModeWork

    def group_quiet_hours_active(self, ctx: Any) -> bool:
        """群是否处于免打扰时段。对应 Go ``Agent.groupQuietHoursActive``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "group_quiet_hours_active"):
            return bool(service.group_quiet_hours_active(ctx))
        return False

    def group_runtime_blocked(self, ctx: Any) -> bool:
        """群运行态是否禁止一切输出。对应 Go ``Agent.groupRuntimeBlocked``。"""
        service = self._svc("group_runtime")
        if service is not None and hasattr(service, "group_runtime_blocked"):
            return bool(service.group_runtime_blocked(ctx))
        return False

    def group_runtime_notice_bypass(self, ctx: Any) -> bool:
        """通知是否绕过群运行态闸门。对应 Go ``groupRuntimeNoticeBypass``。"""
        service = self._svc("group_runtime")
        if service is not None and hasattr(service, "group_runtime_notice_bypass"):
            return bool(service.group_runtime_notice_bypass(ctx))
        return False

    def notice_target_blocked(self, target: Any) -> bool:
        """该通知目标是否被群运行态屏蔽。对应 Go ``Agent.noticeTargetBlocked``。"""
        service = self._svc("group_runtime")
        if service is not None and hasattr(service, "notice_target_blocked"):
            return bool(service.notice_target_blocked(target))
        return False

    def update_group_runtime_from_event(self, event: Any) -> bool:
        """消费机器人自身的禁言/退群事实。对应 Go ``updateGroupRuntimeFromEvent``。"""
        service = self._svc("group_runtime")
        if service is not None and hasattr(service, "update_group_runtime_from_event"):
            return bool(service.update_group_runtime_from_event(event))
        return False

    def group_tool_allowlist(self, ctx: Any) -> dict[str, bool] | None:
        """群工具白名单（``None`` 表示不限制）。对应 Go ``Agent.groupToolAllowlist``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "group_tool_allowlist"):
            return service.group_tool_allowlist(ctx)
        return None

    def filter_group_feature_schemas(self, ctx: Any, schemas: list[Any]) -> list[Any]:
        """按群策略裁掉不可用功能的 schema。对应 Go ``filterGroupFeatureSchemas``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "filter_group_feature_schemas"):
            return list(service.filter_group_feature_schemas(ctx, schemas))
        return list(schemas)

    def group_default_model_selection(self, ctx: Any) -> tuple[Any, bool]:
        """群默认模型选择。对应 Go ``Agent.groupDefaultModelSelection``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "group_default_model_selection"):
            result = service.group_default_model_selection(ctx)
            if isinstance(result, tuple):
                return result
            if result is not None:
                return result, True
        return None, False

    def try_group_knowledge_answer(self, ctx: Any, text: str) -> bool:
        """群知识直答（命中就不进模型）。对应 Go ``Agent.tryGroupKnowledgeAnswer``。"""
        service = self._svc("group_knowledge")
        if service is not None and hasattr(service, "try_group_knowledge_answer"):
            return bool(service.try_group_knowledge_answer(ctx, text))
        return False

    # -- 限流与请求分桶 -----------------------------------------------------

    def allow_inbound(self, ctx: Any) -> tuple[bool, float]:
        """入站限流。对应 Go ``Agent.allowInbound``（返回 ``(是否放行, 稍后重试秒数)``）。"""
        service = self._svc("ratelimit")
        if service is not None and hasattr(service, "allow_inbound"):
            result = service.allow_inbound(ctx)
            if isinstance(result, tuple):
                return bool(result[0]), float(result[1] or 0.0)
        return True, 0.0

    def rate_limit_retry_text(self, retry_after: float) -> str:
        """限流提示。对应 Go ``rateLimitRetryText``。"""
        service = self._svc("ratelimit")
        if service is not None and hasattr(service, "rate_limit_retry_text"):
            text = service.rate_limit_retry_text(retry_after)
            if text:
                return text
        if retry_after and retry_after > 0:
            return f"发送太频繁，请等待约 {retry_after:g} 秒后再试。"
        return "发送太频繁，请稍后再试。"

    def request_fair_key(self, ctx: Any) -> str:
        """请求公平键（含发言人）。对应 Go ``Agent.requestFairKey``。"""
        service = self._svc("request_keys")
        if service is not None and hasattr(service, "request_fair_key"):
            return service.request_fair_key(ctx)
        current = self.actor(ctx)
        scope = self.scope(ctx)
        return "\x00".join([scope.platform, scope.platform_scope_id, current.id])

    def request_scope_key(self, ctx: Any) -> str:
        """请求作用域桶（不含发言人）。对应 Go ``Agent.requestScopeKey``。"""
        service = self._svc("request_keys")
        if service is not None and hasattr(service, "request_scope_key"):
            return service.request_scope_key(ctx)
        scope = self.scope(ctx)
        return "\x00".join([scope.platform, scope.platform_scope_id])

    # -- 模型 ---------------------------------------------------------------

    def model_for_mode(self, mode: str) -> Any:
        """某个会话模式当前的模型选择。对应 Go ``Agent.modelForMode``。"""
        service = self._svc("models")
        if service is not None and hasattr(service, "model_for_mode"):
            result = service.model_for_mode(mode)
            if result is not None:
                return result
        models = getattr(self, "mode_models", None) or {}
        selection = models.get(mode)
        if selection is None:
            selection = models.get(SessionModeWork)
        if selection is None:
            from atri_x.config import ModelSelection

            selection = ModelSelection()
        return selection

    def authorize_execution_model_selection(self, ctx: Any, selection: Any) -> None:
        """执行前授权模型选择（群模型目录）。对应 Go ``authorizeExecutionModelSelection``。"""
        service = self._svc("models")
        if service is not None and hasattr(service, "authorize_execution_model_selection"):
            service.authorize_execution_model_selection(ctx, selection)

    def client_for_provider(self, provider: str) -> Any:
        """按 provider 名取 LLM 客户端。对应 Go ``Agent.clientForProvider``。"""
        clients = getattr(self, "clients", None) or {}
        client = clients.get(provider)
        if client is None and clients:
            client = next(iter(clients.values()))
        if client is None:
            raise RuntimeError(f"client not found for provider {provider!r}")
        return client

    def attach_llm_retry_notifier(self, client: Any, name: str) -> None:
        """给客户端挂重试通知。对应 Go ``Agent.attachLLMRetryNotifier``。"""
        service = self._svc("ratelimit")
        if service is not None and hasattr(service, "attach_llm_retry_notifier"):
            service.attach_llm_retry_notifier(client, name)

    def history_enabled(self, ctx: Any) -> bool:
        """是否允许把消息正文写进历史/日志。对应 Go ``Agent.historyEnabled``。"""
        service = self._svc("group_policy")
        if service is not None and hasattr(service, "history_enabled"):
            return bool(service.history_enabled(ctx))
        return True

    def learning_enabled_for_scope(self, scope: Any) -> bool:
        """该作用域是否允许自我学习。对应 Go ``Agent.learningEnabledForScope``。"""
        service = self._svc("self_learning")
        if service is not None and hasattr(service, "learning_enabled_for_scope"):
            return bool(service.learning_enabled_for_scope(scope))
        return False

    def turn_input_for_message(self, ctx: Any, text: str) -> Input:
        """当前消息的回合输入。对应 Go ``Agent.turnInputForMessage``。"""
        input_value = inbound_turn_input(ctx, text)
        current = self.actor(ctx)
        input_value.speaker = Speaker(
            actor_id=current.id,
            user_id=current.platform_user_id,
            name=current.display_name or current.nickname,
            role=str(current.role),
        )
        return input_value

    # -- 指令 ---------------------------------------------------------------

    def apply_tool_directives(self, ctx: Any, session: Any, text: str) -> DirectiveOutcome:
        """``@tool:`` 工具指令。对应 Go ``Agent.applyToolDirectives``。"""
        service = self._svc("tool_directive")
        if service is not None and hasattr(service, "apply_tool_directives"):
            return service.apply_tool_directives(ctx, session, text)
        return DirectiveOutcome(text=text)

    async def notify_tool_directive_result(self, ctx: Any, result: Any) -> None:
        """对应 Go ``Agent.notifyToolDirectiveResult``。"""
        service = self._svc("tool_directive")
        if service is not None and hasattr(service, "notify_tool_directive_result"):
            await service.notify_tool_directive_result(ctx, result)

    def apply_skill_directives(self, ctx: Any, session: Any, text: str) -> DirectiveOutcome:
        """``@skill:`` 技能指令。对应 Go ``Agent.applySkillDirectives``。"""
        service = self._svc("skill_directive")
        if service is not None and hasattr(service, "apply_skill_directives"):
            return service.apply_skill_directives(ctx, session, text)
        return DirectiveOutcome(text=text)

    async def notify_skill_directive_result(self, ctx: Any, result: Any) -> None:
        """对应 Go ``Agent.notifySkillDirectiveResult``。"""
        service = self._svc("skill_directive")
        if service is not None and hasattr(service, "notify_skill_directive_result"):
            await service.notify_skill_directive_result(ctx, result)

    # -- 媒体与语音 ---------------------------------------------------------

    async def materialize_platform_media(self, ctx: Any) -> Any:
        """把入站平台媒体本地化。对应 Go ``Agent.materializePlatformMedia``。"""
        service = self._svc("inbound_media")
        if service is not None and hasattr(service, "materialize_platform_media"):
            return await _maybe_await(service.materialize_platform_media(ctx))
        return ctx

    async def materialize_media(self, ctx: Any, segments: list[Any]) -> list[Any]:
        """把 LLM 段里的远端媒体本地化。对应 Go ``Agent.materializeMedia``。

        Go 里这条链路是同步的（``media.Materialize`` 直接返回切片），Python 版要下
        远端媒体所以是异步。``toolrun_adapter`` / ``tool_transcript`` 通过
        ``getattr(agent, "materialize_media")`` 调用并自行处理 awaitable，这里
        把它挂到 Agent 上补齐 Go 的方法面。
        """
        from atri_x.agent.segments import materialize_media as _materialize_media

        return await _materialize_media(self, ctx, list(segments or []))

    async def transcribe_platform_audio(self, ctx: Any) -> Any:
        """入站语音转写。对应 Go ``Agent.transcribePlatformAudio``。"""
        service = self._svc("inbound_speaker") or self._svc("asr")
        if service is not None and hasattr(service, "transcribe_platform_audio"):
            return await _maybe_await(service.transcribe_platform_audio(ctx))
        return ctx

    async def send_prepared_media(self, ctx: Any, outputs: list[Any], send: Any) -> Any:
        """把媒体输出解析成可发送形态再发。对应 Go ``Agent.sendPreparedMedia``。"""
        service = self._svc("media_output")
        if service is not None and hasattr(service, "send_prepared_media"):
            return await _maybe_await(service.send_prepared_media(ctx, outputs, send))
        return await send(outputs)

    async def send_cli_reasoning(self, ctx: Any, text: str) -> None:
        """把思维链回显到 CLI。对应 Go ``Agent.sendCLIReasoning``。"""
        service = self._svc("cli_reasoning")
        if service is not None and hasattr(service, "send_cli_reasoning"):
            await _maybe_await(service.send_cli_reasoning(ctx, text))

    # -- 上下文与提示词 -----------------------------------------------------

    def should_compact(self, ctx: Any, session: Any, selection: Any) -> bool:
        """这一轮是否该先压缩上下文。对应 Go ``Agent.shouldCompact``。"""
        service = self._svc("context_runtime")
        if service is not None and hasattr(service, "should_compact"):
            return bool(service.should_compact(ctx, session, selection))
        return False

    def compact_active(self, session_id: str) -> bool:
        """该会话是否正在压缩。对应 Go ``Agent.compactActive``。"""
        service = self._svc("context_runtime")
        if service is not None and hasattr(service, "compact_active"):
            return bool(service.compact_active(session_id))
        returns = _compacting_sessions(self)
        return (session_id or "") in returns

    async def compact_session(
        self, ctx: Any, session: Any, trigger: str, selection: Any
    ) -> tuple[Any, str]:
        """压缩会话。对应 Go ``Agent.compactSession``。"""
        service = self._svc("context_runtime")
        if service is not None and hasattr(service, "compact_session"):
            result = await _maybe_await(service.compact_session(ctx, session, trigger, selection))
            if isinstance(result, tuple):
                return result
        return session, ""

    async def handle_initial_prompt_overflow(
        self,
        ctx: Any,
        session: Any,
        selection: Any,
        llm_messages: list[Any],
        tools: list[Any],
        out: Any,
    ) -> tuple[bool, bool]:
        """首轮 prompt 溢出的降级。对应 Go ``Agent.handleInitialPromptOverflow``。"""
        service = self._svc("context_runtime")
        if service is not None and hasattr(service, "handle_initial_prompt_overflow"):
            result = await _maybe_await(
                service.handle_initial_prompt_overflow(
                    ctx, session, selection, llm_messages, tools, out
                )
            )
            if isinstance(result, tuple):
                return bool(result[0]), bool(result[1])
        return True, False

    async def guard_prompt_before_call(
        self,
        ctx: Any,
        session_id: str,
        selection: Any,
        messages: list[Any],
        tools: list[Any],
        out: Any,
    ) -> None:
        """调用前的 prompt 预算闸门。对应 Go ``Agent.guardPromptBeforeCall``。"""
        service = self._svc("context_runtime")
        if service is not None and hasattr(service, "guard_prompt_before_call"):
            await _maybe_await(
                service.guard_prompt_before_call(ctx, session_id, selection, messages, tools, out)
            )

    async def build_prompt(self, ctx: Any, request: Any) -> list[Any]:
        """构造发给模型的完整消息列表。对应 Go ``PromptBuilder.Build``。"""
        builder = getattr(self, "prompt_builder", None)
        if builder is not None:
            result = builder.build(ctx, request)
            return list(await _maybe_await(result) or [])
        return _fallback_prompt_build(self, request)

    # -- 记账与预算 ---------------------------------------------------------

    def record_usage(self, session_id: str, usage: Any) -> None:
        """记录最后一次用量。对应 Go ``Agent.recordUsage``。"""
        service = self._svc("usage")
        if service is not None and hasattr(service, "record_usage"):
            service.record_usage(session_id, usage)

    def record_llm_origin(self, ctx: Any, session: Any, selection: Any) -> None:
        """记录会话的协议来源。对应 Go ``Agent.recordLLMOrigin``。"""
        service = self._svc("usage")
        if service is not None and hasattr(service, "record_llm_origin"):
            service.record_llm_origin(ctx, session, selection)

    def begin_chat_budget(
        self,
        ctx: Any,
        selection: Any,
        messages: list[Any],
        tools: list[Any],
        max_output: int,
        usage_call_id: str,
    ) -> Any:
        """预占一次对话预算。对应 Go ``Agent.beginChatBudget``。"""
        service = self._svc("budget")
        if service is not None and hasattr(service, "begin_chat_budget"):
            return service.begin_chat_budget(
                ctx, selection, messages, tools, max_output, usage_call_id
            )
        return None

    def settle_chat_budget(
        self, ctx: Any, reservation: Any, usage: Any, release: bool
    ) -> None:
        """结算/释放对话预算。对应 Go ``Agent.settleChatBudget``。"""
        service = self._svc("budget")
        if service is not None and hasattr(service, "settle_chat_budget"):
            service.settle_chat_budget(ctx, reservation, usage, release)

    def record_chat_usage(self, ctx: Any, model: str, usage: Any, usage_call_id: str) -> None:
        """没有预算预占时直接记账。对应 Go ``Agent.recordChatUsage``。"""
        service = self._svc("budget")
        if service is not None and hasattr(service, "record_chat_usage"):
            service.record_chat_usage(ctx, model, usage, usage_call_id)

    # -- 工具 ---------------------------------------------------------------

    def cached_tools_for_session(self, session: Any) -> list[Any]:
        """会话缓存的发现工具。对应 Go ``Agent.cachedToolsForSession``。"""
        service = self._svc("tools")
        if service is not None and hasattr(service, "cached_tools_for_session"):
            return list(service.cached_tools_for_session(session))
        from atri_x.agent.session_metadata import decode_session_metadata

        metadata = decode_session_metadata(getattr(session, "metadata", "") or "")
        return list(metadata.tool_cache)

    def tool_run_manager(self) -> Any:
        """工具运行管理器。对应 Go ``Agent.toolRunManager``。"""
        service = self._svc("tools")
        if service is not None and hasattr(service, "tool_run_manager"):
            return service.tool_run_manager()
        runtime = getattr(self, "tool_runtime", None)
        return getattr(runtime, "manager", None)

    # -- 持久化 -------------------------------------------------------------

    async def persist_turn_message(self, ctx: Any, message: Message, operation: str) -> None:
        """落库一条回合消息。对应 Go ``Agent.persistTurnMessage``。"""
        service = self._svc("persistence")
        if service is not None and hasattr(service, "persist_turn_message"):
            await _maybe_await(service.persist_turn_message(ctx, message, operation))
            return
        store = getattr(self, "store", None)
        if store is None or store.messages() is None:
            return
        try:
            store.messages().create(message)
        except Exception as exc:  # noqa: BLE001 - 落库失败要冒泡给回合
            from atri_x.agent.errors import new_user_error_details

            details = new_user_error_details("保存消息失败", exc)
            raise RuntimeError(details.text) from exc

    async def persist_turn_messages(
        self, ctx: Any, session_id: str, operation: str, messages: list[Message]
    ) -> None:
        """落库一批回合消息。对应 Go ``Agent.persistTurnMessages``。"""
        for message in messages:
            await self.persist_turn_message(ctx, message, operation)

    def background_session_must_stop(self, ctx: Any, session_id: str) -> bool:
        """后台回合是否必须停下（前台接管）。对应 Go ``backgroundSessionMustStop``。"""
        service = self._svc("foreground")
        if service is not None and hasattr(service, "background_session_must_stop"):
            return bool(service.background_session_must_stop(ctx, session_id))
        return False


def _compacting_sessions(agent: Any) -> set[str]:
    sessions = getattr(agent, "_compacting", None)
    if sessions is None:
        sessions = set()
        agent._compacting = sessions
    return sessions


def _fallback_prompt_build(agent: Any, request: Any) -> list[Any]:
    """没有 PromptBuilder 时的最小 prompt：系统提示 + 历史 + 本轮消息。"""
    from atri_x.llm import LLMMessage, RoleSystem, text_segments

    messages: list[Any] = []
    soul = getattr(agent, "soul", None)
    prompt = ""
    if soul is not None:
        try:
            prompt = soul.system_prompt(None, "") or ""
        except Exception:  # noqa: BLE001 - 灵魂读取失败不该阻断对话
            prompt = ""
    if prompt.strip():
        messages.append(LLMMessage(role=RoleSystem, segments=text_segments(prompt)))
    for message in getattr(request, "messages", []) or []:
        segments = _segments_from_stored(message)
        messages.append(LLMMessage(role=message.role, segments=segments))
    return messages


def _segments_from_stored(message: Any) -> list[MessageSegment]:
    from atri_x.storage import parse_segments

    raw = getattr(message, "segments", "") or ""
    if raw:
        try:
            parsed = parse_segments(raw)
            if parsed:
                return list(parsed)
        except Exception:  # noqa: BLE001 - 解析失败退回纯文本
            pass
    content = getattr(message, "content", "") or ""
    from atri_x.llm import text_segments

    return text_segments(content)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value
