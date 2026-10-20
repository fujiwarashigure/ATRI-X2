"""一个 assistant 回合里整批工具调用的编排。

对应 Go：``internal/toolrun/runner.go``。

顺序是**契约**，不要重排（每一步都有理由）：

1. ``chat`` 模式一律不跑工具（哪怕 Hook 或模型塞了调用）。
2. 每个调用先过 ``prepare_tool_call``（Hook 可改写参数）→ 群白名单 →
   ``authorize_tool``（服务端权威检查）。
3. 评估风险 → 扣「工具使用次数」→ ``preflight_confirmation``。
4. 批量预览只发一次；单条调用按需发。
5. 确认：不可用 → 超管门槛 → 风险门槛 → 后台确认 → 是否需要确认。
6. **取到队列槽位后再授权一次**：请求排队期间策略可能被撤销，所以
   ``authorize_tool`` 在执行前会被再调一次。
7. ``before_execute``（日配额账本在这里预占）。
8. 媒体参数保留 → 执行 → 输出投递 → ``complete_tool_call`` 钩子 →
   记录 → 发现结果记忆 → 失败时补一条失败预览。

``RunResult.stopped`` 表示"被用户中断"，调用方应停止本回合的后续处理。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from atri_x.delivery.delivery import Output
from atri_x.errors import ForbiddenError, InvalidError
from atri_x.llm.llm import (
    LLMMessage,
    MessageSegment,
    Role,
    ToolCallRequest,
    segments_content_text,
    text_segments,
)
from atri_x.security import Actor, RiskHigh
from atri_x.storage.models import Session, SessionModeChat
from atri_x.storage.times import now as storage_now
from atri_x.tool.tool import DiscoveryResult, Result, RiskAssessment
from atri_x.toolrun.cache import CachedTool
from atri_x.toolrun.toolrun import Manager, ResolvedTool

#: 预览参数的最大 rune 数。对应 Go ``maxPreviewRunes``。
MAX_PREVIEW_RUNES = 160


@dataclass
class ConfirmResult:
    """确认结果。对应 Go ``ConfirmResult``。"""

    allowed: bool = False
    extra: str = ""
    message: LLMMessage = field(default_factory=LLMMessage)
    stopped: bool = False
    #: 拒绝原因。对应 Go ``confirm`` 的**第二个返回值**（``error``）：Go 在拒绝时返回
    #: 非 nil error，而 Python 只能返回一个值，所以挂在结果上。
    #:
    #: ⚠️ 被拒绝的调用**必须**带上它：``record_tool_call`` 只看 ``error is None``
    #: 判定成功，漏了就写进 ``tool_call_records.success=1``，``SuccessfulIDs`` 随后会
    #: 让模型以为「这个工具刚才真的执行过」。历史上这里只填了 ``message``，
    #: 超管门槛/风险门槛被拒的调用全被记成成功。
    error: BaseException | None = None


@dataclass
class RunRequest:
    """一次批量工具运行的输入。对应 Go ``RunRequest``。"""

    session: Session | None = None
    calls: list[ToolCallRequest] = field(default_factory=list)
    assistant_text: str = ""
    assistant_raw_text: str = ""
    cached_tools: list[CachedTool] = field(default_factory=list)
    actor: Actor = field(default_factory=Actor)
    #: 非 None 时是服务端的工具名白名单，在确认/执行**之前**生效。
    allowed_tools: dict[str, bool] | None = None
    #: 服务端权威工具检查：别名解析后调一次，队列等待后执行前**再**调一次。
    authorize_tool: Callable[[Any, ToolCallRequest, ResolvedTool], Any] | None = None
    #: 在确认之后、队列槽位到手之后、执行之前运行；抛错即拒绝执行（日配额在此预占）。
    before_execute: Callable[[Any, ToolCallRequest, ResolvedTool], Any] | None = None

    async def authorize(
        self, ctx: Any, call: ToolCallRequest, resolved: ResolvedTool
    ) -> tuple[bool, str]:
        # fail-closed：没有装配权威授权检查时不放行。生产路径（``chat_tools``）一定
        # 会传入 ``atri_x.agent.tool_auth.authorize_tool_call``；``None`` 只应出现在
        # 脱离 Agent 的裸用场景，此时"默认放行"等于让群白名单/超管门槛静默失效。
        if self.authorize_tool is None:
            return False, "tool authorization is not configured"
        result = self.authorize_tool(ctx, call, resolved)
        if hasattr(result, "__await__"):
            result = await result
        return result


@dataclass
class RunResult:
    """一次批量工具运行的输出。对应 Go ``RunResult``。"""

    messages: list[LLMMessage] = field(default_factory=list)
    prepared_calls: list[ToolCallRequest] = field(default_factory=list)
    confirmation_extra: str = ""
    transcript: list[Any] = field(default_factory=list)
    stopped: bool = False


@runtime_checkable
class RunnerDeps(Protocol):
    """运行一次工具批次所需的宿主能力。对应 Go ``RunnerDeps``。

    全部是 async（Python 侧这些动作都涉及 I/O）；实现见 ``atri_x.agent``。
    """

    async def prepare_tool_call(
        self, ctx: Any, session: Any, call: ToolCallRequest
    ) -> ToolCallRequest: ...

    async def should_send_preview(
        self, ctx: Any, session: Any, call: ToolCallRequest, assistant_text: str
    ) -> bool: ...

    async def confirm_tool_call(
        self,
        ctx: Any,
        session_id: str,
        call: ToolCallRequest,
        assessment: RiskAssessment,
        detail: str,
    ) -> ConfirmResult: ...

    async def confirm_background_tool(
        self,
        ctx: Any,
        session_id: str,
        call: ToolCallRequest,
        resolved: ResolvedTool,
        assessment: RiskAssessment,
    ) -> tuple[ConfirmResult, bool]: ...

    async def start_tool_request(
        self, ctx: Any, session_id: str, tool_name: str
    ) -> tuple[Any, datetime, Callable[[], None]]:
        """登记一次 tool 类 request，返回 ``(ctx, started_at, done)``。

        这是**三方共用的契约**（协议声明 / 实现 / ``run()`` 的解包），元数必须一致：

        * 登记成功 → 返回三元组。第 3 项 ``done`` 负责结束这次 request，
          ``run()`` 保证在离开该次调用时**恰好调用一次** —— 工具执行成功、执行失败、
          执行前被二次授权拒绝、被取消、上层提前返回，都会走同一条释放路径。
        * 登记失败 → 抛异常。``run()`` 在异常分支生成工具失败结果，不再执行工具，
          也不会去调 ``done``（没有登记成功的 request 可释放）。
        * 宿主不提供 request 管理器时（``requests``/``start`` 缺失）**不算失败**：
          实现返回 ``(ctx, now, noop)`` 明确表示"本次没有登记请求"。

        ⚠️ 2026-10 修复：``run()`` 曾经按 4 项解包（``ctx, _, done, err``），
        而这里的协议与 :class:`atri_x.agent.toolrun_adapter.AgentToolRunDeps` 都返回
        3 项 —— 真实适配器接上后每次调用都在解包处抛 ``ValueError``，被宽泛的
        ``except BaseException`` 记成"工具启动失败"，工具**永远执行不到**，
        且已登记的 request 因为 ``done`` 丢失而永不结束。测试里的假实现当时返回
        4 项，所以单测全绿也发现不了。
        """

    async def prepare_tool_context(
        self, ctx: Any, session: Any, call: ToolCallRequest
    ) -> Any: ...

    async def complete_tool_call(
        self,
        ctx: Any,
        session: Any,
        call: ToolCallRequest,
        risk: str,
        segments: list[MessageSegment],
        call_err: BaseException | None,
    ) -> list[MessageSegment]: ...

    async def send_preview(self, ctx: Any, text: str) -> None: ...

    async def send_outputs(self, ctx: Any, outputs: list[Output]) -> None: ...

    async def record_tool_call(
        self,
        ctx: Any,
        session_id: str,
        call: ToolCallRequest,
        risk: str,
        started_at: datetime,
        result: str,
        call_err: BaseException | None,
    ) -> None: ...

    async def audit_tool_denied(
        self, ctx: Any, session_id: str, call: ToolCallRequest, risk: str, reason: str
    ) -> None: ...

    async def remember_discovery_result(
        self, ctx: Any, session: Any, result: Result
    ) -> None: ...

    def add_tool_use(self, session_id: str, tool_name: str) -> None: ...

    def tool_result_message(self, session_id: str, message: LLMMessage) -> Any: ...

    def tool_call_message(
        self,
        session_id: str,
        content: str,
        raw_text: str,
        calls: list[ToolCallRequest],
    ) -> Any: ...

    def persisted_tool_message(self, message: LLMMessage) -> LLMMessage: ...


async def run(manager: Manager, ctx: Any, deps: RunnerDeps, req: RunRequest) -> RunResult:
    """执行一整批工具调用。对应 Go ``Manager.Run``。"""
    if req.session is None or deps is None:
        return RunResult()
    # Chat 模式永远不跑工具
    if req.session.mode == SessionModeChat:
        return RunResult()

    session_id = req.session.id
    messages: list[LLMMessage] = []
    prepared_calls: list[ToolCallRequest] = []
    transcript: list[Any] = []
    confirmation_extra = ""
    batch_preview_sent = await _send_batch_tool_preview(deps, ctx, req)

    for original in req.calls:
        started_at = storage_now()
        try:
            call = await deps.prepare_tool_call(ctx, req.session, original)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - Hook 失败要回灌给模型
            prepared_calls.append(original)
            message = _tool_message(
                original.name,
                original.id,
                f"tool call {original.name} failed: hook: {exc}",
            )
            messages.append(message)
            transcript.append(deps.tool_result_message(session_id, message))
            continue

        prepared_calls.append(call)

        if not _tool_allowed(req.allowed_tools, call.name):
            await deps.audit_tool_denied(
                ctx, session_id, call, RiskHigh, "tool_not_in_group_allowlist"
            )
            message = _tool_message(
                call.name,
                call.id,
                f"tool call {call.name} denied: tool is not in this group's allowlist",
            )
            content = segments_content_text(message.segments)
            await deps.record_tool_call(
                ctx,
                session_id,
                call,
                "denied",
                started_at,
                content,
                ValueError("tool not in group allowlist"),
            )
            messages.append(message)
            transcript.append(deps.tool_result_message(session_id, message))
            continue

        resolved = manager.resolve(ctx, call.name, req.cached_tools)
        allowed, reason = await req.authorize(ctx, call, resolved)
        if not allowed:
            await deps.audit_tool_denied(ctx, session_id, call, RiskHigh, reason)
            message = _tool_message(
                call.name, call.id, f"tool call {call.name} denied: {reason}"
            )
            content = segments_content_text(message.segments)
            await deps.record_tool_call(
                ctx, session_id, call, "denied", started_at, content, ValueError(reason)
            )
            messages.append(message)
            transcript.append(deps.tool_result_message(session_id, message))
            continue

        tool_ctx = await deps.prepare_tool_context(ctx, req.session, call)
        assessment, risk_text = await _assess_for_run(manager, tool_ctx, resolved, call)
        deps.add_tool_use(session_id, call.name)

        try:
            await manager.preflight_confirmation(tool_ctx, resolved, call)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            message = _tool_message(
                call.name, call.id, f"tool call {call.name} failed: {exc}"
            )
            content = segments_content_text(message.segments)
            await deps.record_tool_call(
                ctx, session_id, call, risk_text, started_at, content, exc
            )
            messages.append(message)
            transcript.append(deps.tool_result_message(session_id, message))
            continue

        if not batch_preview_sent and await deps.should_send_preview(
            ctx, req.session, call, req.assistant_text
        ):
            await deps.send_preview(
                ctx, f"正在调用 {call.name}：{_preview_arguments(call.arguments)}"
            )

        error: BaseException | None = None
        try:
            confirm = await _confirm(manager, deps, ctx, req.actor, session_id, call, resolved, assessment)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：confirm 返回 err 一起记
            confirm = ConfirmResult(allowed=False)
            error = exc
        else:
            error = confirm.error
        if not confirm.allowed:
            if confirm.stopped:
                return RunResult(
                    messages=messages,
                    prepared_calls=prepared_calls,
                    confirmation_extra=confirmation_extra,
                    transcript=transcript,
                    stopped=True,
                )
            if error is None:
                # 兜底：Go 的 confirm 在拒绝时一定返回非 nil error。少了这一条，
                # 没带 error 的拒绝路径（例如 deps.confirm_background_tool 的拒绝）
                # 会被 record_tool_call 记成 success=1 —— 见 ConfirmResult.error 的注释。
                error = ForbiddenError(
                    f"tool call {call.name} denied: confirmation was not granted"
                )
            message = confirm.message
            if not message.segments:
                message = _tool_message(
                    call.name, call.id, f"tool call {call.name} failed: {error}"
                )
            message_text = segments_content_text(message.segments)
            await deps.record_tool_call(
                ctx, session_id, call, risk_text, started_at, message_text, error
            )
            messages.append(message)
            transcript.append(deps.tool_result_message(session_id, message))
            confirmation_extra = _join_assistant_text(confirmation_extra, confirm.extra)
            continue

        confirmation_extra = _join_assistant_text(confirmation_extra, confirm.extra)

        # 契约：``deps.start_tool_request`` 返回三元组 ``(ctx, started_at, done)``，
        # 登记失败**抛异常**（协议见 :class:`RunnerDeps.start_tool_request`）。
        # 这里曾经按 4 项解包，而真实适配器返回 3 项 → 每次调用都在解包处抛错、
        # 被下面的宽泛 except 记成"启动失败"，工具永远执行不到。
        start_err: BaseException | None = None
        try:
            run_ctx, _, done = await deps.start_tool_request(ctx, session_id, call.name)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：启动失败即 err
            start_err, run_ctx, done = exc, ctx, _noop
        if start_err is not None:
            content = f"tool call {call.name} failed: {start_err}"
            message = _tool_message(call.name, call.id, content)
            await deps.record_tool_call(
                ctx, session_id, call, risk_text, started_at, content, start_err
            )
            messages.append(message)
            transcript.append(deps.tool_result_message(session_id, message))
            continue

        # 登记成功后，``done()`` 必须**恰好执行一次**：执行成功、执行失败、
        # 排队后被二次授权拒绝、before_execute 拒绝、媒体参数保留失败、被取消、
        # 以及上面的提前 return 都走同一条释放路径 —— 漏掉任何一条都会把
        # request 永久留在"进行中"。
        try:
            run_ctx = await deps.prepare_tool_context(run_ctx, req.session, call)
            # 排队期间策略可能被撤销 —— 执行前再授权一次
            allowed, reason = await req.authorize(run_ctx, call, resolved)
            if not allowed:
                await deps.audit_tool_denied(ctx, session_id, call, RiskHigh, reason)
                message = _tool_message(
                    call.name, call.id, f"tool call {call.name} denied: {reason}"
                )
                content = segments_content_text(message.segments)
                await deps.record_tool_call(
                    ctx, session_id, call, "denied", started_at, content, ValueError(reason)
                )
                messages.append(message)
                transcript.append(deps.tool_result_message(session_id, message))
                continue

            if req.before_execute is not None:
                before = req.before_execute(run_ctx, call, resolved)
                if hasattr(before, "__await__"):
                    before = await before
                if isinstance(before, BaseException):
                    reason = str(before)
                    await deps.audit_tool_denied(ctx, session_id, call, RiskHigh, reason)
                    message = _tool_message(
                        call.name, call.id, f"tool call {call.name} denied: {reason}"
                    )
                    content = segments_content_text(message.segments)
                    await deps.record_tool_call(
                        ctx, session_id, call, risk_text, started_at, content, before
                    )
                    messages.append(message)
                    transcript.append(deps.tool_result_message(session_id, message))
                    continue

            if manager.media is not None:
                retain = getattr(manager.media, "retain_session_tool_arguments", None)
                if callable(retain):
                    try:
                        # 本仓的留存入口是**同步两参**方法：Go 版的 ctx 只服务于
                        # 同步 SQLite 调用，移植时有意去掉（见
                        # ``atri_x.media.reference`` 的「同步 vs 异步」说明）。
                        # 曾经照 Go 版传 ctx，真实接入后每次都 TypeError、被下面
                        # 的宽泛捕获记成 "retain media arguments"，任何工具都
                        # 执行不到。见 MediaRetainContractTests。
                        outcome = retain(session_id, call.arguments)
                        if hasattr(outcome, "__await__"):
                            await outcome
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:  # noqa: BLE001
                        message = _tool_message(
                            call.name,
                            call.id,
                            f"tool call {call.name} failed: retain media arguments: {exc}",
                        )
                        content = segments_content_text(message.segments)
                        await deps.record_tool_call(
                            ctx, session_id, call, risk_text, started_at, content, exc
                        )
                        messages.append(message)
                        transcript.append(deps.tool_result_message(session_id, message))
                        continue

            result = await manager.execute(run_ctx, call, resolved, req.actor)
            tool_err = _ctx_error(run_ctx)
            if tool_err is not None:
                if _ctx_error(ctx) is not None:
                    return RunResult(
                        messages=messages,
                        prepared_calls=prepared_calls,
                        confirmation_extra=confirmation_extra,
                        transcript=transcript,
                        stopped=True,
                    )
                result.err = tool_err
                result.message = _tool_message(
                    call.name, call.id, f"tool call {call.name} canceled by user"
                )

            if result.result is not None and getattr(result.result, "outputs", None):
                try:
                    await deps.send_outputs(ctx, list(result.result.outputs))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    result.err = exc
                    result.message.segments = text_segments(
                        f"tool call {call.name} failed: output: {exc}"
                    )

            try:
                completed = await deps.complete_tool_call(
                    ctx, req.session, call, risk_text, result.message.segments, result.err
                )
                result.message.segments = completed
            except asyncio.CancelledError:
                raise
            except BaseException as hook_err:  # noqa: BLE001
                result.err = hook_err
                result.message.segments = text_segments(
                    f"tool call {call.name} failed: hook: {hook_err}"
                )

            result_text = segments_content_text(result.message.segments)
            await deps.record_tool_call(
                ctx, session_id, call, risk_text, started_at, result_text, result.err
            )
            messages.append(result.message)
            if call.name == "discover_tool" and result.result is not None:
                await deps.remember_discovery_result(ctx, req.session, result.result)
            transcript.append(
                deps.tool_result_message(session_id, deps.persisted_tool_message(result.message))
            )
            if result.err is not None:
                await deps.send_preview(ctx, f"{call.name} 调用失败：{result.err}")
        finally:
            _release_request(done)

    transcript.insert(
        0,
        deps.tool_call_message(
            session_id, req.assistant_text, req.assistant_raw_text, prepared_calls
        ),
    )
    return RunResult(
        messages=messages,
        prepared_calls=prepared_calls,
        confirmation_extra=confirmation_extra,
        transcript=transcript,
    )


Run = run


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
async def _confirm(
    manager: Manager,
    deps: RunnerDeps,
    ctx: Any,
    actor: Actor,
    session_id: str,
    call: ToolCallRequest,
    resolved: ResolvedTool,
    assessment: RiskAssessment,
) -> ConfirmResult:
    """对应 Go ``Manager.confirm``；拒绝时返回的 ``ConfirmResult.message`` 已带文本。"""
    message = LLMMessage(role=Role.TOOL, name=call.name, tool_call_id=call.id)
    if not resolved.available:
        message.segments = text_segments(
            f"tool call {call.name} failed: {resolved.reason}"
        )
        return ConfirmResult(
            allowed=False,
            message=message,
            error=InvalidError(f"tool call {call.name} failed: {resolved.reason}"),
        )
    if resolved.source == "atriwisp":
        return ConfirmResult(allowed=True)
    policy = _policy_for(manager, ctx)
    if resolved.native is None:
        message.segments = text_segments(
            f"tool call {call.name} failed: native tool unavailable"
        )
        return ConfirmResult(
            allowed=False,
            message=message,
            error=InvalidError("native tool unavailable"),
        )

    info = resolved.native.info()
    from atri_x.security import RoleSuperadmin

    if getattr(info, "superadmin_only", False) and getattr(actor, "role", "") != RoleSuperadmin:
        await deps.audit_tool_denied(
            ctx, session_id, call, assessment.level, "tool_requires_superadmin"
        )
        message.segments = text_segments(
            f"tool call {call.name} denied: requires superadmin role; ask an "
            "administrator to add you to security.superadmins"
        )
        return ConfirmResult(
            allowed=False,
            message=message,
            error=ForbiddenError("tool_requires_superadmin"),
        )

    if not policy.can_use_tool(actor, assessment.level, bool(getattr(info, "owner_scoped", False))):
        await deps.audit_tool_denied(
            ctx, session_id, call, assessment.level, "tool_risk_above_allowed_level"
        )
        message.segments = text_segments(
            f"tool call {call.name} denied: risk {assessment.level} is above your "
            "allowed tool level; ask an administrator to raise "
            "security.user_max_tool_risk or add you to security.superadmins"
        )
        return ConfirmResult(
            allowed=False,
            message=message,
            error=ForbiddenError("tool_risk_above_allowed_level"),
        )

    confirm, handled = await deps.confirm_background_tool(
        ctx, session_id, call, resolved, assessment
    )
    if handled:
        return confirm
    if not policy.needs_tool_confirmation(actor, assessment.level):
        return ConfirmResult(allowed=True)
    detail = await manager.risk_detail(ctx, resolved, call)
    result = deps.confirm_tool_call(ctx, session_id, call, assessment, detail)
    if hasattr(result, "__await__"):
        result = await result
    return result


def _policy_for(manager: Manager, ctx: Any) -> Any:
    from atri_x.toolrun.toolrun import policy_for_manager

    return policy_for_manager(ctx, manager.policy)


async def _assess_for_run(
    manager: Manager, ctx: Any, resolved: ResolvedTool, call: ToolCallRequest
) -> tuple[RiskAssessment, str]:
    """对应 Go ``assessForRun``：评估失败按 ``high``（fail-closed）而不是 ``low``。

    Go 上游在评估出错时返回 ``RiskLow``（``internal/toolrun/runner.go:288``），
    后果是"评估器抛异常 → 高危工具直接执行、不需要确认"。这里与
    :mod:`atri_x.tool.builtin.shell_risk`（解析失败即 High）统一为 fail-closed：
    评估不出来就必须走确认，确认通道也没有时由确认层拒绝。
    """
    try:
        assessment = await manager.assess_risk(ctx, resolved, call.arguments)
    except asyncio.CancelledError:
        # 取消不是"评估失败"：降成 high 会让调用继续走确认流程，等于吞掉取消信号。
        raise
    except BaseException:  # noqa: BLE001
        assessment = None
    if assessment is None or not assessment.level:
        return (
            RiskAssessment(
                level="high",
                reasons=["风险评估失败，按高风险处理（fail-closed），必须经用户确认"],
            ),
            "unknown",
        )
    return assessment, str(assessment.level)


async def _send_batch_tool_preview(deps: RunnerDeps, ctx: Any, req: RunRequest) -> bool:
    """对应 Go ``sendBatchToolPreview``：多条调用只发一条合并预览。"""
    from atri_x.llm.llm import ToolCallRequest as _TCR

    if len(req.calls) <= 1:
        return False
    if not await deps.should_send_preview(ctx, req.session, _TCR(), req.assistant_text):
        return False
    lines = [
        f"正在调用 {call.name}：{_preview_arguments(call.arguments)}" for call in req.calls
    ]
    await deps.send_preview(ctx, "\n".join(lines))
    return True


def _tool_message(name: str, ident: str, content: str) -> LLMMessage:
    """对应 Go ``toolMessage``。"""
    return LLMMessage(
        role=Role.TOOL, name=name, tool_call_id=ident, segments=text_segments(content)
    )


def _tool_allowed(allowlist: dict[str, bool] | None, name: str) -> bool:
    """对应 Go ``toolAllowed``：白名单为 None 表示不限制。"""
    if allowlist is None:
        return True
    return bool(allowlist.get((name or "").strip().lower()))


def _preview_arguments(args: str) -> str:
    """对应 Go ``previewArguments``（先紧凑化再按 160 rune 截断）。"""
    text = _compact_arguments(args)
    if len(text) <= MAX_PREVIEW_RUNES:
        return text
    return text[:MAX_PREVIEW_RUNES] + "..."


def _compact_arguments(args: str) -> str:
    """对应 Go ``compactArguments``：能解析就压成最紧凑 JSON，否则原样返回。"""
    args = (args or "").strip()
    if not args:
        return "{}"
    try:
        parsed = json.loads(args)
    except (ValueError, TypeError):
        return args
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _join_assistant_text(first: str, second: str) -> str:
    """对应 Go ``joinAssistantText``。"""
    first = (first or "").strip()
    second = (second or "").strip()
    if not first:
        return second
    if not second:
        return first
    return first + "\n\n" + second


def _ctx_error(ctx: Any) -> BaseException | None:
    if ctx is None:
        return None
    for name in ("error", "err"):
        getter = getattr(ctx, name, None)
        if callable(getter):
            value = getter()
            if value is not None:
                return value
            return None
    return None


def _release_request(done: Callable[[], None]) -> None:
    """结束一次已登记的 tool request（``start_tool_request`` 的 ``done``）。

    释放动作**不能**覆盖工具本身的结果：``done`` 抛普通异常时吞掉继续；
    ``asyncio.CancelledError`` 属于取消传播，照常抛出（此时调用方本来就在取消中）。
    """
    try:
        done()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 释放失败不影响工具结果与后续调用
        return


def _noop() -> None:
    return None


__all__ = [
    "MAX_PREVIEW_RUNES",
    "ConfirmResult",
    "Run",
    "RunRequest",
    "RunResult",
    "RunnerDeps",
    "run",
]
