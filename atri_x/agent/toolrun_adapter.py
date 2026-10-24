"""工具运行适配层：把 ``Agent`` 接到 ``atri_x.toolrun`` 的 Runner 上。

对应 Go：``internal/agent/toolrun_adapter.go``。

职责：``agentToolRunDeps``（Go 的 ``RunnerDeps`` 实现）、``toolRunManager``、
``cachedToolsForSession``。真实的执行、确认与批量循环都在
:mod:`atri_x.toolrun.runner` 里，本模块**只**做宿主能力适配。

与 Go 的差异（形式，不是简化）：

* Go 方法 → :class:`AgentToolRunDeps` 的 ``async`` 方法；``RunnerDeps`` 协议里所有
  动作都是 async（见 ``atri_x/toolrun/runner.py``）。
* ``agent`` / ``output`` 一律鸭子类型：通过 ``getattr`` 取宿主能力；宿主尚未提供
  的钩子按 Go 的语义降级（``runHook`` 缺失 = 无 hook；``requests`` 缺失 = 不建请求）。
  ``tool.Executor.execute`` 仍然是"返回带 ``err`` 字段的结果对象、不抛异常"。
* ``turnRequestIDFromContext`` 等 helper 归 agent-core；此处用
  ``atri_x.tool.sandbox.session_id_from_context`` 与本地兜底实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.llm import (
    LLMMessage,
    Role,
    MessageSegment,
    SegmentType,
    text_segments,
)
from atri_x.toolrun import (
    CachedTool,
    ConfirmResult,
    Manager,
    SOURCE_KIND_NATIVE,
    normalize_cached_tools,
)
from atri_x.toolrun.toolrun import ResolvedTool
from atri_x.tool import RiskAssessment
# safety-guard: 工具结果审核 + 把 guard 挂到工具 ctx（image_generate / send_file 用）
from atri_x.safety import (
    TOOL_IMAGE_STRIPPED_TEXT,
    mode_from_host,
    scope_from_host,
    with_safety_guard,
    with_safety_scope,
)
from atri_x.agent.tools import (
    compact_arguments,
    is_background_session,
    preview_arguments,
    risk_reasons_text,
    tool_runtime_state,
)

__all__ = [
    "AgentToolRunDeps",
    "ConfirmResult",
    "cached_tools_for_session",
    "confirm_result_message",
    "risk_confirmation_detail_text",
    "tool_run_manager",
]


def tool_run_manager(agent: Any) -> Manager:
    """对应 Go ``Agent.toolRunManager``：惰性建立并挂上 media。"""
    state = tool_runtime_state(agent)
    if state.manager is None:
        state.manager = Manager(state.registry, getattr(agent, "security_policy", None))
    state.manager.media = getattr(agent, "media", None)
    return state.manager


def cached_tools_for_session(agent: Any, session: Any) -> list[CachedTool]:
    """对应 Go ``Agent.cachedToolsForSession``。

    后台 Session 必须丢掉 ``discover_tool``：否则缓存重建会把全量工具重新暴露给
    后台任务。重建缓存时保留 ``foreground_only`` 标记，前台专属工具因此进不了后台。
    """
    if session is None:
        return []
    from atri_x.agent.session_metadata import decode_session_metadata

    metadata = decode_session_metadata(getattr(session, "metadata", "") or "")
    background = bool(str(metadata.background_kind or "").strip())
    cached: list[CachedTool] = []
    for item in metadata.tool_cache:
        if background and item.name == "discover_tool":
            continue
        cached.append(item)
    state = tool_runtime_state(agent)
    for raw in metadata.discovered_tools:
        name = str(raw or "").strip()
        if not name:
            continue
        if background and name == "discover_tool":
            continue
        if state.registry is None:
            continue
        target, ok = state.registry.get(name)
        if not ok or target is None:
            continue
        info = target.info()
        cached.append(
            CachedTool(
                name=name,
                source=SOURCE_KIND_NATIVE,
                description=info.description,
                schema=target.schema(),
                foreground_only=bool(getattr(info, "foreground_only", False)),
            )
        )
    return normalize_cached_tools(cached)


@dataclass
class AgentToolRunDeps:
    """Go ``agentToolRunDeps``。实现 ``atri_x.toolrun.RunnerDeps``。"""

    agent: Any = None
    output: Any = None

    # -- hook 前后置 --------------------------------------------------------
    async def prepare_tool_call(self, ctx: Any, session: Any, call: Any) -> Any:
        """对应 Go ``PrepareToolCall``（``hook.PointToolCallPrepared``）。"""
        event = await self._run_tool_hook(
            ctx,
            session,
            "tool_call_prepared",
            call,
            {},
        )
        if event is None:
            return call
        tool_payload = getattr(event, "tool", None) or (event.get("tool") if isinstance(event, dict) else None)
        if tool_payload is None:
            return call
        arguments = _payload_field(tool_payload, "arguments")
        if arguments is not None:
            call.arguments = arguments
        return call

    async def complete_tool_call(
        self,
        ctx: Any,
        session: Any,
        call: Any,
        risk: str,
        segments: list[MessageSegment],
        call_err: BaseException | None,
    ) -> list[MessageSegment]:
        """对应 Go ``CompleteToolCall``（``hook.PointToolCallCompleted``）。"""
        original = [segment.clone() for segment in segments]
        result_text = _segments_content_text(original)
        event = await self._run_tool_hook(
            ctx,
            session,
            "tool_call_completed",
            call,
            {
                "role": Role.TOOL,
                "segments": [segment.clone() for segment in original],
                "risk": risk,
                "result": result_text,
                "error": call_err,
            },
        )
        if event is None:
            return await self._materialize(
                ctx, _safety_guard_segments(self.agent, ctx, original, call)
            )
        message_payload = _payload_field(event, "message")
        tool_payload = _payload_field(event, "tool")
        hook_segments = _payload_field(message_payload, "segments") if message_payload is not None else None
        if hook_segments is not None and not _segments_equal(hook_segments, original):
            return await self._materialize(
                ctx, _safety_guard_segments(self.agent, ctx, list(hook_segments), call)
            )
        hook_result = _payload_field(tool_payload, "result") if tool_payload is not None else None
        if hook_result is not None and hook_result != result_text:
            return await self._materialize(
                ctx,
                _safety_guard_segments(
                    self.agent, ctx, _set_segment_text(original, hook_result), call
                ),
            )
        return await self._materialize(
            ctx, _safety_guard_segments(self.agent, ctx, original, call)
        )

    # -- 请求/上下文 --------------------------------------------------------
    async def start_tool_request(
        self, ctx: Any, session_id: str, tool_name: str
    ) -> tuple[Any, Any, Any]:
        """对应 Go ``StartToolRequest``：登记一次 tool 类 request。

        返回值就是 :class:`atri_x.toolrun.RunnerDeps` 声明的**三元组**
        ``(ctx, started_at, done)``：``done`` 结束这次 request，由 ``runner.run()``
        保证恰好调用一次（成功/失败/取消/提前返回都释放）。宿主没有 request
        管理器时不建请求，返回的 ``done`` 是空操作 —— 这不是"失败"，
        所以仍返回三元组而不是抛异常；真正登记失败（``requests.start`` 抛错）
        才让异常传给 ``runner`` 当启动失败处理。
        """
        agent = self.agent
        requests = getattr(agent, "requests", None)
        if requests is None:
            return ctx, _now(), _noop
        start = getattr(requests, "start", None)
        if start is None:
            return ctx, _now(), _noop
        try:
            from atri_x.request import KindTool, StartRequest
        except Exception:  # noqa: BLE001 - 请求模块缺失时不建请求
            return ctx, _now(), _noop
        request = None
        tool_ctx = ctx
        done = _noop
        start_request = StartRequest(
            parent_id=_turn_request_id(ctx),
            session_id=session_id,
            kind=KindTool,
            label=tool_name,
            scope_key=_request_scope_key(agent, ctx),
            timeout=float(getattr(agent, "tool_timeout", 0) or 0),
        )
        outcome = start(ctx, start_request)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        if isinstance(outcome, tuple):
            request, tool_ctx = outcome[0], outcome[1]
            if len(outcome) > 2 and callable(outcome[2]):
                done = outcome[2]
        else:
            request = outcome
        started_at = getattr(request, "started_at", None) or _now()
        publish = getattr(self.output, "publish_runtime_status", None)
        if publish is not None:
            snapshot = _runtime_snapshot(session_id, request, tool_name, started_at)
            result = publish(ctx, snapshot)
            if hasattr(result, "__await__"):
                await result
        return tool_ctx, started_at, done

    async def prepare_tool_context(self, ctx: Any, session: Any, call: Any) -> Any:
        """对应 Go ``PrepareToolContext``。"""
        # safety-guard: 把 guard 与审计归属挂到工具 ctx 上（工具拿不到 agent，只能从 ctx 取）。
        # guard 绑定当前会话模式：chat / work 的「按模式覆盖」策略在工具内部也生效。
        guard = getattr(self.agent, "safety_guard", None)
        if guard is not None:
            guard = guard.for_mode(mode_from_host(self.agent, ctx))
        ctx = with_safety_guard(ctx, guard)
        ctx = with_safety_scope(ctx, scope_from_host(self.agent, ctx))
        if session is None:
            return ctx
        agent = self.agent
        try:
            from atri_x.platform import message_context_from, with_message_context
        except Exception:  # noqa: BLE001
            message_context_from = None  # type: ignore[assignment]
            with_message_context = None  # type: ignore[assignment]
        if message_context_from is not None:
            msg, ok = message_context_from(ctx)
            if ok:
                msg.session_id = str(getattr(session, "id", "") or "").strip()
                ctx = with_message_context(ctx, msg)
        from atri_x.tool.sandbox import with_session_id

        ctx = with_session_id(ctx, getattr(session, "id", "") or "")
        try:
            from atri_x.tool.detail import with_shown_rule_card_formats
        except Exception:  # noqa: BLE001
            with_shown_rule_card_formats = None  # type: ignore[assignment]
        if with_shown_rule_card_formats is not None:
            from atri_x.agent.session_metadata import decode_session_metadata

            metadata = decode_session_metadata(getattr(session, "metadata", "") or "")
            ctx = with_shown_rule_card_formats(
                ctx, metadata.shown_rule_card_formats
            )
        allowlist = _group_tool_allowlist(agent, ctx, session)
        if allowlist is not None:
            ctx = _with_discover_filter(ctx, allowlist)
        if is_background_session(session):
            return ctx
        from atri_x.tool.workspace import with_workspace_store

        return with_workspace_store(ctx, _SessionWorkspaceStore(agent=agent, session=session))

    # -- 预览 / 确认 --------------------------------------------------------
    async def should_send_preview(
        self, ctx: Any, session: Any, call: Any, assistant_text: str
    ) -> bool:
        """对应 Go ``ShouldSendPreview``。

        ``[tool_preview].enabled = false`` 时程序不主动发任何进度预览（模型自己
        说的中间正文仍会照常发送）；其余判断与旧版一致。
        """
        texts = getattr(self.agent, "tool_preview_texts", None)
        if texts is not None and not getattr(texts, "enabled", True):
            return False
        is_cli = getattr(self.agent, "is_cli_context", None)
        if is_cli is not None and is_cli(ctx):
            return True
        return not str(assistant_text or "").strip()

    async def confirm_tool_call(
        self,
        ctx: Any,
        session_id: str,
        call: Any,
        assessment: RiskAssessment,
        detail: str,
    ) -> ConfirmResult:
        """对应 Go ``ConfirmToolCall``：等待用户确认高风险调用。"""
        agent = self.agent
        if _auto_confirmed(agent, ctx, session_id, call.name):
            return ConfirmResult(allowed=True)
        # fail-closed：拿不到确认通道时不能"当作已确认"（旧实现返回 allowed=True，
        # 高危调用在 agent.turns 缺失时被静默放行）。
        if _confirmation_channel(agent) is None:
            _log_result(
                agent,
                session_id,
                call,
                assessment,
                "reject",
                "",
                "confirmation channel unavailable",
            )
            return _deny_without_channel(call)
        full_args = compact_arguments(call.arguments)
        preview_args = preview_arguments(full_args)
        timeout = _confirmation_timeout(agent, ctx)
        # 群聊 / 频道里参数不展示（提示与 /detail 都藏），也禁用 /confirmall。
        hide_arguments = _hides_arguments(agent, ctx)
        allow_confirm_all = _allows_confirm_all(agent, ctx)
        _log_wait(agent, session_id, call, assessment, timeout)
        await _send_chat(
            agent,
            ctx,
            "高风险工具调用等待确认\n工具：%s\n风险：%s\n%s%s\n%s。"
            % (
                call.name,
                assessment.level,
                "参数：已隐藏（群聊中不展示）"
                if hide_arguments
                else "参数：%s" % preview_args,
                risk_reasons_text(assessment.reasons),
                _confirmation_prompt_text(agent, timeout, allow_confirm_all),
            ),
        )
        response = await _await_confirmation(
            agent,
            ctx,
            session_id,
            call,
            assessment,
            full_args,
            preview_args,
            detail,
            timeout,
            actor_id=str(getattr(_actor(agent, ctx), "id", "") or ""),
            redacted=hide_arguments,
        )
        if response is None:
            _log_result(
                agent,
                session_id,
                call,
                assessment,
                "reject",
                "",
                "confirmation channel unavailable",
            )
            return _deny_without_channel(call)
        if getattr(response, "expired", False):
            _log_result(agent, session_id, call, assessment, "expire", getattr(response, "extra", ""), "confirmation wait expired")
            await _send_chat(agent, ctx, "高风险工具确认已过期，当前处理已停止。", cancel_safe=True)
            return ConfirmResult(
                allowed=False,
                extra=getattr(response, "extra", "") or "",
                message=_tool_message(
                    call, "tool call %s confirmation expired" % call.name
                ),
                stopped=True,
            )
        if getattr(response, "stopped", False):
            _log_result(agent, session_id, call, assessment, "stop", getattr(response, "extra", ""), "")
            return ConfirmResult(
                allowed=False,
                extra=getattr(response, "extra", "") or "",
                message=_tool_message(call, "tool call %s stopped by user" % call.name),
                stopped=True,
            )
        if getattr(response, "confirm_tool", False):
            _set_auto_confirmed(agent, ctx, session_id, call.name)
            await _send_chat(
                agent, ctx, "已为当前 Session 自动确认后续 %s 工具调用。" % call.name
            )
        if getattr(response, "confirm_all", False):
            _set_session_auto_confirmed(agent, ctx, session_id)
            await _send_chat(agent, ctx, "已为当前 Session 自动确认后续高风险工具调用。")
        if getattr(response, "rejected", False):
            reason = str(getattr(response, "reason", "") or "").strip() or "user rejected"
            _log_result(agent, session_id, call, assessment, "reject", getattr(response, "extra", ""), reason)
            return ConfirmResult(
                allowed=False,
                extra=getattr(response, "extra", "") or "",
                message=_tool_message(
                    call, "tool call %s rejected by user: %s" % (call.name, reason)
                ),
            )
        action = "confirm"
        if getattr(response, "confirm_tool", False):
            action = "confirmtool"
        if getattr(response, "confirm_all", False):
            action = "confirmall"
        _log_result(agent, session_id, call, assessment, action, getattr(response, "extra", ""), "")
        return ConfirmResult(allowed=True, extra=getattr(response, "extra", "") or "")

    async def confirm_background_tool(
        self,
        ctx: Any,
        session_id: str,
        call: Any,
        resolved: ResolvedTool,
        assessment: RiskAssessment,
    ) -> tuple[ConfirmResult, bool]:
        """对应 Go ``ConfirmBackgroundTool``：后台 shell 特例（不等待用户）。"""
        message = LLMMessage(role=Role.TOOL, name=call.name, tool_call_id=call.id)
        handler = getattr(self.agent, "confirm_background_sandbox_shell", None)
        if handler is None:
            return ConfirmResult(), False
        outcome = handler(ctx, session_id, call, assessment.level, message)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        if isinstance(outcome, tuple):
            allowed, handled = outcome[0], outcome[1]
        else:
            allowed, handled = getattr(outcome, "confirm", False), getattr(outcome, "handled", False)
        if not handled:
            return ConfirmResult(), False
        return ConfirmResult(allowed=allowed, message=message), True

    # -- 输出 / 记账 --------------------------------------------------------
    async def send_preview(self, ctx: Any, text: str) -> None:
        send = getattr(self.output, "send_preview", None)
        if send is None:
            return
        result = send(ctx, text)
        if hasattr(result, "__await__"):
            await result

    async def send_outputs(self, ctx: Any, outputs: list[Any]) -> None:
        send = getattr(self.output, "send_outputs", None)
        if send is None:
            return
        result = send(ctx, outputs)
        if hasattr(result, "__await__"):
            await result

    async def record_tool_call(
        self,
        ctx: Any,
        session_id: str,
        call: Any,
        risk: str,
        started_at: Any,
        result: str,
        call_err: BaseException | None,
    ) -> None:
        recorder = getattr(self.agent, "record_tool_call", None)
        if recorder is None:
            return
        outcome = recorder(ctx, session_id, call, risk, started_at, result, call_err)
        if hasattr(outcome, "__await__"):
            await outcome

    async def audit_tool_denied(
        self, ctx: Any, session_id: str, call: Any, risk: str, reason: str
    ) -> None:
        audit = getattr(self.agent, "audit", None)
        if audit is None:
            return
        actor = _actor(self.agent, ctx)
        audit(
            "permission_denied",
            "actor_id",
            getattr(actor, "id", ""),
            "session_id",
            session_id,
            "tool",
            call.name,
            "risk",
            risk,
            "reason",
            reason,
            "result",
            "rejected",
        )

    async def remember_discovery_result(self, ctx: Any, session: Any, result: Any) -> None:
        remember = getattr(self.agent, "remember_discovery_result", None)
        if remember is None:
            return
        outcome = remember(ctx, session, result)
        if hasattr(outcome, "__await__"):
            await outcome

    def add_tool_use(self, session_id: str, tool_name: str) -> None:
        turns = getattr(self.agent, "turns", None)
        if turns is None:
            return
        turns.add_tool_use(session_id, tool_name)

    def tool_result_message(self, session_id: str, message: LLMMessage) -> Any:
        from atri_x.agent.tool_transcript import tool_result_storage_message

        return tool_result_storage_message(session_id, message)

    def tool_call_message(
        self,
        session_id: str,
        content: str,
        raw_text: str,
        calls: list[Any],
        reasoning: str = "",
    ) -> Any:
        from atri_x.agent.tool_transcript import tool_call_storage_message

        return tool_call_storage_message(
            session_id, content, raw_text, calls, reasoning_content=reasoning
        )

    def persisted_tool_message(self, message: LLMMessage) -> LLMMessage:
        from atri_x.agent.tool_transcript import persisted_tool_message

        return persisted_tool_message(message)

    # -- 内部 ---------------------------------------------------------------
    async def _run_tool_hook(
        self, ctx: Any, session: Any, point: str, call: Any, extra: dict[str, Any]
    ) -> Any:
        runner = getattr(self.agent, "run_hook", None)
        if runner is None:
            return None
        try:  # hook 包由别的组负责 —— 延迟导入
            from atri_x.hook import Event, MessagePayload, PointToolCallCompleted, PointToolCallPrepared, ToolPayload
        except Exception:  # noqa: BLE001 - 没有 hook 层时不挂 hook
            return None
        mapping = {
            "tool_call_prepared": PointToolCallPrepared,
            "tool_call_completed": PointToolCallCompleted,
        }
        even_point = mapping.get(point)
        if even_point is None:
            return None
        payload = ToolPayload(
            id=call.id, name=call.name, arguments=call.arguments
        )
        message_payload = None
        if extra.get("segments") is not None:
            payload.risk = extra.get("risk", "")
            payload.result = extra.get("result", "")
            payload.error = extra.get("error")
            message_payload = MessagePayload(
                role=extra.get("role", Role.TOOL), segments=list(extra["segments"])
            )
        event = Event(
            point=even_point,
            session=_hook_session(self.agent, session),
            tool=payload,
        )
        if message_payload is not None:
            event.message = message_payload
        outcome = runner(ctx, event)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        if isinstance(outcome, tuple):
            outcome = outcome[0]
        return outcome

    async def _materialize(
        self, ctx: Any, segments: list[MessageSegment]
    ) -> list[MessageSegment]:
        materialize = getattr(self.agent, "materialize_media", None)
        if materialize is None:
            return segments
        outcome = materialize(ctx, segments)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        return list(outcome or segments)


@dataclass
class _SessionWorkspaceStore:
    """``sessionWorkspaceStore``：把 Session 绑定成 workspace 存储。"""

    agent: Any = None
    session: Any = None

    async def load(self, ctx: Any) -> Any:
        load = getattr(self.agent, "session_workspace", None)
        if load is None:
            return None
        outcome = load(ctx, self.session)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        return outcome

    async def save(self, ctx: Any, workspace: Any) -> None:
        save = getattr(self.agent, "persist_session_workspace", None)
        if save is None:
            return
        outcome = save(ctx, self.session, workspace)
        if hasattr(outcome, "__await__"):
            await outcome


# ---------------------------------------------------------------------------
# 公开 helper（同时供测试与别处复用）
# ---------------------------------------------------------------------------
def _safety_guard_segments(
    agent: Any, ctx: Any, segments: list[MessageSegment], call: Any
) -> list[MessageSegment]:
    """safety-guard: 工具结果审核。

    文本段逐段过 ``filter_text``（``enforce`` 下脱敏、命中词表类换成拦截文案）；
    ``should_strip_tool_images()`` 为真时把图片段替换成文本占位（模型与后续上下文
    看不到图）。``guard`` 未装配或 ``mode = off`` 时原样返回。
    """
    guard = getattr(agent, "safety_guard", None)
    if guard is None:
        return segments
    scope = scope_from_host(agent, ctx)
    mode = mode_from_host(agent, ctx)
    strip_images = guard.should_strip_tool_images(mode)
    tool_name = str(getattr(call, "name", "") or "")
    out: list[MessageSegment] = []
    for segment in segments:
        if segment.type == SegmentType.TEXT:
            filtered, _decision = guard.filter_text(
                segment.text,
                stage="tool",
                scope=scope,
                meta={"tool": tool_name},
                mode=mode,
            )
            if filtered != segment.text:
                clone = segment.clone()
                clone.text = filtered
                out.append(clone)
            else:
                out.append(segment)
            continue
        if segment.type == SegmentType.IMAGE and strip_images:
            out.append(
                MessageSegment(type=SegmentType.TEXT, text=TOOL_IMAGE_STRIPPED_TEXT)
            )
            continue
        out.append(segment)
    return out


def risk_confirmation_detail_text(confirmation: Any) -> str:
    """高风险调用详情文案。对应 Go ``riskConfirmationDetailText``。"""
    if getattr(confirmation, "redacted", False):
        from atri_x.agent.risk_confirmation import REDACTED_ARGUMENTS_TEXT

        return "高风险工具调用详情\n工具：%s\n风险：%s\n%s\n" % (
            getattr(confirmation, "tool_name", ""),
            getattr(confirmation, "risk", ""),
            REDACTED_ARGUMENTS_TEXT,
        )
    detail = str(getattr(confirmation, "detail", "") or "").strip()
    if not detail:
        from atri_x.agent.risk_confirmation import format_risk_confirmation_arguments

        detail = format_risk_confirmation_arguments(
            getattr(confirmation, "arguments", "") or ""
        )
    return "高风险工具调用详情\n工具：%s\n风险：%s\n参数：\n%s\n" % (
        getattr(confirmation, "tool_name", ""),
        getattr(confirmation, "risk", ""),
        detail,
    )


def confirm_result_message(call: Any, text: str) -> LLMMessage:
    """构造"拒绝/停止"用的 tool 消息。对应 RunnerDeps 的 message 语义。"""
    return _tool_message(call, text)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _tool_message(call: Any, text: str) -> LLMMessage:
    return LLMMessage(
        role=Role.TOOL, name=call.name, tool_call_id=call.id, segments=text_segments(text)
    )


def _now() -> Any:
    from atri_x.storage import now as storage_now

    return storage_now()


def _noop() -> None:
    return None


def _payload_field(payload: Any, field: str) -> Any:
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get(field)
    return getattr(payload, field, None)


def _segments_content_text(segments: list[MessageSegment]) -> str:
    from atri_x.llm import segments_content_text

    return segments_content_text(segments)


def _set_segment_text(segments: list[MessageSegment], text: str) -> list[MessageSegment]:
    from atri_x.llm import set_segment_text

    return set_segment_text(segments, text)


def _segments_equal(left: Any, right: Any) -> bool:
    left_list = list(left or ())
    right_list = list(right or ())
    if len(left_list) != len(right_list):
        return False
    for one, other in zip(left_list, right_list):
        if not isinstance(one, MessageSegment) or not isinstance(other, MessageSegment):
            return False
        if (one.type, one.text, one.url, one.media, one.mime_type, one.name) != (
            other.type,
            other.text,
            other.url,
            other.media,
            other.mime_type,
            other.name,
        ):
            return False
    return True


def _turn_request_id(ctx: Any) -> str:
    try:
        from atri_x.agent.request_context import turn_request_id_from_context

        return turn_request_id_from_context(ctx)
    except Exception:  # noqa: BLE001 - agent-core 尚未落地
        return ""


def _request_scope_key(agent: Any, ctx: Any) -> str:
    helper = getattr(agent, "request_scope_key", None)
    if helper is None:
        return ""
    return helper(ctx)


def _runtime_snapshot(session_id: str, request: Any, tool_name: str, started_at: Any) -> Any:
    try:
        from atri_x.runtime.status import PhaseTool, Snapshot
        from atri_x.request import KindTool
    except Exception:  # noqa: BLE001
        return {
            "session_id": session_id,
            "phase": "tool",
            "request_id": getattr(request, "id", ""),
            "label": tool_name,
            "tool_name": tool_name,
            "stage_started_at": started_at,
        }
    return Snapshot(
        session_id=session_id,
        phase=PhaseTool,
        request_id=getattr(request, "id", ""),
        kind=KindTool,
        label=tool_name,
        tool_name=tool_name,
        stage_started_at=started_at,
    )


def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is None:
        from atri_x.security import Actor

        return Actor()
    return helper(ctx)


def _auto_confirmed(agent: Any, ctx: Any, session_id: str, tool_name: str) -> bool:
    """这次调用是否已被授权登记表放行（命中即消耗一次使用）。

    没有 ``auto_confirm_allowed`` 的 Agent（老装配 / 测试替身）回退到旧的两参查询。
    """
    checker = getattr(agent, "auto_confirm_allowed", None)
    if checker is not None:
        return bool(checker(ctx, session_id, tool_name))
    if _call(agent, "is_session_auto_confirmed", session_id):
        return True
    return bool(_call(agent, "is_tool_auto_confirmed", session_id, tool_name))


def _call(agent: Any, name: str, *args: Any) -> Any:
    helper = getattr(agent, name, None)
    if helper is None:
        return None
    return helper(*args)


def _set_auto_confirmed(
    agent: Any, ctx: Any, session_id: str, tool_name: str
) -> None:
    granter = getattr(agent, "grant_tool_auto_confirm", None)
    if granter is not None:
        granter(ctx, session_id, tool_name)
        return
    _call(agent, "set_tool_auto_confirmed", session_id, tool_name)


def _set_session_auto_confirmed(agent: Any, ctx: Any, session_id: str) -> None:
    granter = getattr(agent, "grant_session_auto_confirm", None)
    if granter is not None:
        granter(ctx, session_id)
        return
    _call(agent, "set_session_auto_confirmed", session_id)


def _confirmation_timeout(agent: Any, ctx: Any) -> float:
    helper = getattr(agent, "confirmation_wait_timeout", None)
    if helper is None:
        return 0.0
    value = helper(ctx)
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value or 0)


def _confirmation_prompt_text(
    agent: Any, timeout: float, allow_confirm_all: bool = True
) -> str:
    from atri_x.agent.risk_confirmation import risk_confirmation_prompt_text

    prefix = ""
    helper = getattr(agent, "command_prefix", None)
    if helper is not None:
        prefix = helper() or ""
    return risk_confirmation_prompt_text(prefix, timeout, allow_confirm_all)


def _hides_arguments(agent: Any, ctx: Any) -> bool:
    """群聊 / 频道里确认提示与详情都不展示工具参数。"""
    from atri_x.agent.risk_confirmation import confirmation_hides_arguments

    return confirmation_hides_arguments(agent, ctx)


def _allows_confirm_all(agent: Any, ctx: Any) -> bool:
    """``/confirmall`` 只在单聊与 CLI 可用。"""
    from atri_x.agent.risk_confirmation import confirmation_allows_confirm_all

    return confirmation_allows_confirm_all(agent, ctx)


def _log_wait(agent: Any, session_id: str, call: Any, assessment: Any, timeout: float) -> None:
    helper = getattr(agent, "log_risk_confirmation_wait", None)
    if helper is None:
        return
    helper(session_id, call, assessment.level, assessment.reasons)


def _log_result(
    agent: Any,
    session_id: str,
    call: Any,
    assessment: Any,
    action: str,
    extra: str,
    reason: str,
) -> None:
    helper = getattr(agent, "log_risk_confirmation_result", None)
    if helper is None:
        return
    helper(session_id, call, assessment.level, action, extra, reason)


async def _send_chat(agent: Any, ctx: Any, text: str, cancel_safe: bool = False) -> None:
    sender = getattr(agent, "send_chat", None)
    if sender is None:
        return
    outcome = sender(ctx, text)
    if hasattr(outcome, "__await__"):
        await outcome


def _confirmation_channel(agent: Any) -> Any:
    """确认通道（``agent.turns.await_risk_confirmation_context``）；缺失时返回 None。"""
    turns = getattr(agent, "turns", None)
    if turns is None:
        return None
    channel = getattr(turns, "await_risk_confirmation_context", None)
    return channel if callable(channel) else None


def _deny_without_channel(call: Any) -> ConfirmResult:
    """没有确认通道时的统一拒绝：既要拦下调用，也要让审计记成 rejected。"""
    from atri_x.errors import ForbiddenError

    return ConfirmResult(
        allowed=False,
        message=_tool_message(
            call,
            "tool call %s requires user confirmation but no confirmation channel is available"
            % call.name,
        ),
        error=ForbiddenError("tool_confirmation_unavailable"),
    )


async def _await_confirmation(
    agent: Any,
    ctx: Any,
    session_id: str,
    call: Any,
    assessment: Any,
    full_args: str,
    preview_args: str,
    detail: str,
    timeout: float,
    actor_id: str = "",
    redacted: bool = False,
) -> Any:
    awai = _confirmation_channel(agent)
    if awai is None:
        return None
    from atri_x.turn import RiskConfirmation

    confirmation = RiskConfirmation(
        id=call.id,
        tool_name=call.name,
        arguments="" if redacted else full_args,
        risk=str(assessment.level),
        summary=call.name if redacted else "%s %s" % (call.name, preview_args),
        detail="" if redacted else detail,
        actor_id=actor_id,
        redacted=redacted,
    )
    outcome = awai(ctx, session_id, confirmation, timeout)
    if hasattr(outcome, "__await__"):
        outcome = await outcome
    if isinstance(outcome, tuple):
        return outcome[0] if outcome else None
    return outcome


def _group_tool_allowlist(
    agent: Any, ctx: Any, session: Any = None
) -> dict[str, bool] | None:
    helper = getattr(agent, "group_tool_allowlist", None)
    if helper is None:
        from atri_x.agent.tool_auth import group_tool_allowlist

        return group_tool_allowlist(agent, ctx, session)
    return helper(ctx, session)


def _with_discover_filter(ctx: Any, allowlist: dict[str, bool]) -> Any:
    """把群工具白名单挂到 ctx（Go ``tool.WithDiscoverFilter``）。

    ``atri_x.tool`` 尚未提供 ``with_discover_filter``（``internal/tool/discover.go``
    未落地）时静态返回原 ctx：白名单仍由 ``RunRequest.allowed_tools`` 与
    ``authorize_tool_call`` 强制执行，这里只是让 ``discover_tool`` 少列几个名字。
    """
    try:
        from atri_x.tool import with_discover_filter
    except Exception:  # noqa: BLE001
        return ctx
    return with_discover_filter(
        ctx, lambda _ctx, info: bool(allowlist.get(str(info.name).strip().lower()))
    )


def _hook_session(agent: Any, session: Any) -> Any:
    helper = getattr(agent, "hook_session", None)
    if helper is None:
        return session
    return helper(session)
