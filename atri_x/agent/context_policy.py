"""长消息保护（上下文溢出策略）：判定、截断/摘要/拒绝与群级覆盖。

对应 Go：``internal/agent/context_policy.go``。

策略取值 ``reject | truncate | summarize``，按「群级覆盖 → 全局默认 → reject」的顺序
解析，且分别对 ``chat`` / ``work`` 两个模式生效。

**只有单条消息超限才允许改写它**：整段 prompt 超限（``total_prompt``）时即使配置了
``truncate`` / ``summarize`` 也退回 ``reject`` —— 截断最后一条消息救不了已经装满的
上下文，装作处理了只会让模型看到残缺历史。

改写走 ``llm.set_segment_text``（保留图片/文件 segment），拒绝时不把原文写进会话。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from atri_x.agent.model import host_of
from atri_x.config import ContextOverflowConfig, ModelSelection
from atri_x.contextmgr import (
    PromptBudget,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    estimate_tools_tokens,
    truncate_text_to_tokens,
)
from atri_x.contextmgr.budget import MESSAGE_TOKEN_OVERHEAD
from atri_x.errors import ErrPromptOverflow, InvalidError, PromptOverflowError
from atri_x.llm.llm import LLMMessage, MessageSegment, set_segment_text
from atri_x.llm.segment import segments_content_text
from atri_x.session.types import Scope
from atri_x.storage.models import Session, SessionModeChat, SessionModeWork

#: 截断后追加的标记。对应 Go ``handleInitialPromptOverflow`` 里的字面量。
TRUNCATION_MARKER = "\n\n...[原始消息过长，已自动截断；请分段发送以完整处理]"

#: 合法的策略值。对应 Go ``validOverflowMode``。
OVERFLOW_MODES: tuple[str, ...] = ("reject", "truncate", "summarize")

#: 通知级别（对应 Go 的 ``slog.LevelWarn``）。
WARN_LEVEL = "warning"

__all__ = [
    "OVERFLOW_MODES",
    "ContextOverflowState",
    "ContextPolicyMixin",
    "ErrPromptOverflow",
    "PromptOverflowInfo",
    "TRUNCATION_MARKER",
    "context_overflow_key",
    "ensure_context_overflow_state",
    "normalize_overflow_target",
    "valid_overflow_mode",
]


def context_overflow_key(scope: Scope | None) -> str:
    """群级覆盖的键（``platform:scope``）。对应 Go ``contextOverflowKey``。"""
    platform = str(getattr(scope, "platform", "") or "").strip()
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip()
    if not platform:
        platform = "unknown"
    if not scope_id:
        return platform
    return platform + ":" + scope_id


def clone_context_overflow(
    input: Mapping[str, ContextOverflowConfig] | None,
) -> dict[str, ContextOverflowConfig]:
    """拷贝群级覆盖表。对应 Go ``cloneContextOverflow``。"""
    if not input:
        return {}
    out: dict[str, ContextOverflowConfig] = {}
    for key, value in input.items():
        out[key] = value.copy() if hasattr(value, "copy") else value
    return out


def valid_overflow_mode(value: str) -> bool:
    """策略值是否合法（去空白、大小写不敏感）。对应 Go ``validOverflowMode``。"""
    return (value or "").strip().lower() in OVERFLOW_MODES


def normalize_overflow_target(value: str) -> str:
    """把目标归一化成 ``chat`` / ``work`` / ``all``。对应 Go ``normalizeOverflowTarget``。"""
    text = (value or "").strip().lower()
    if text == "chat":
        return "chat"
    if text == "work":
        return "work"
    return "all"


@dataclass
class PromptOverflowInfo:
    """一次溢出判定的结果。对应 Go ``promptOverflowInfo``。"""

    budget: PromptBudget = field(default_factory=PromptBudget)
    estimated_tokens: int = 0
    current_tokens: int = 0
    reason: str = ""

    def total_exceeded(self) -> bool:
        """整段输入超限。对应 Go ``TotalExceeded``。"""
        return self.budget.input_limit > 0 and self.estimated_tokens > self.budget.input_limit

    def single_message_exceeded(self) -> bool:
        """单条消息超限。对应 Go ``SingleMessageExceeded``。"""
        return (
            self.budget.single_message_limit > 0
            and self.current_tokens > self.budget.single_message_limit
        )

    def exceeded(self) -> bool:
        """对应 Go ``Exceeded``。"""
        return self.total_exceeded() or self.single_message_exceeded()


class ContextOverflowState:
    """群级长消息保护覆盖表。对应 Go ``Agent.contextOverflow`` + ``contextOverflowMu``。"""

    def __init__(self, values: Mapping[str, ContextOverflowConfig] | None = None) -> None:
        self.mu = threading.RLock()
        self.values: dict[str, ContextOverflowConfig] = clone_context_overflow(values)

    def snapshot(self) -> dict[str, ContextOverflowConfig]:
        """对应 Go ``contextOverflowSnapshot``。"""
        with self.mu:
            return clone_context_overflow(self.values)

    def replace(self, values: Mapping[str, ContextOverflowConfig] | None) -> None:
        """对应 Go ``setContextOverflowSnapshot``。"""
        with self.mu:
            self.values = clone_context_overflow(values)

    def get(self, key: str) -> ContextOverflowConfig | None:
        """对应 Go ``contextOverflowForScope``（未配置返回 ``None``）。"""
        with self.mu:
            value = self.values.get(key)
            return None if value is None else value.copy()

    def set(self, key: str, value: ContextOverflowConfig) -> None:
        """对应 Go ``setContextOverflowForScope``（两个模式都为空则删除该键）。"""
        with self.mu:
            if not (value.chat or "").strip() and not (value.work or "").strip():
                self.values.pop(key, None)
                return
            self.values[key] = value.copy()


def ensure_context_overflow_state(host: Any) -> ContextOverflowState:
    """取宿主的群级覆盖表；缺失时补建（补建在真正的宿主上）。"""
    agent = host_of(host)
    state = getattr(agent, "context_overflow_state", None)
    if isinstance(state, ContextOverflowState):
        return state
    state = ContextOverflowState()
    agent.context_overflow_state = state
    return state


class ContextPolicyMixin:
    """``internal/agent/context_policy.go`` 里挂在 ``*Agent`` 上的方法。

    宿主成员：``self.context_overflow_state``、``self.context_runtime``、
    ``self.scope(ctx)``、``self.current_mode(ctx)``、``self.command_prefix()``、
    ``self.compact_selection_for_session(session)``、``self.save_runtime_state()``、
    ``self.load_session(ctx, id)``、``self.store``。
    """

    # -- 覆盖表 -------------------------------------------------------------
    def set_context_overflow_snapshot(
        self, snapshot: Mapping[str, ContextOverflowConfig] | None
    ) -> None:
        """对应 Go ``setContextOverflowSnapshot``。"""
        ensure_context_overflow_state(self).replace(snapshot)

    def context_overflow_snapshot(self) -> dict[str, ContextOverflowConfig]:
        """对应 Go ``contextOverflowSnapshot``。"""
        return ensure_context_overflow_state(self).snapshot()

    def context_overflow_for_scope(
        self, scope: Scope | None
    ) -> tuple[ContextOverflowConfig, bool]:
        """对应 Go ``contextOverflowForScope``（第二个值是是否存在）。"""
        value = ensure_context_overflow_state(self).get(context_overflow_key(scope))
        if value is None:
            return ContextOverflowConfig(), False
        return value, True

    def set_context_overflow_for_scope(
        self, scope: Scope | None, value: ContextOverflowConfig
    ) -> None:
        """对应 Go ``setContextOverflowForScope``。"""
        ensure_context_overflow_state(self).set(context_overflow_key(scope), value)

    # -- 模式解析 -----------------------------------------------------------
    def context_overflow_mode(self, ctx: Any, session: Session | None) -> str:
        """当前模式生效的策略。对应 Go ``contextOverflowMode``。"""
        mode = SessionModeWork
        if session is not None and getattr(session, "mode", ""):
            mode = session.mode
        override, ok = self.context_overflow_for_scope(self.scope(ctx))
        if ok:
            value = override.work
            if mode == SessionModeChat:
                value = override.chat
            if valid_overflow_mode(value):
                return value.strip().lower()
        from atri_x.agent.context_runtime import ensure_context_runtime

        value = ensure_context_runtime(self).overflow_mode()
        if valid_overflow_mode(value):
            return value.strip().lower()
        return "reject"

    # -- 判定 ---------------------------------------------------------------
    async def detect_prompt_overflow(
        self,
        ctx: Any,
        session: Session | None,
        selection: ModelSelection,
        messages: Sequence[LLMMessage],
        tools: Sequence[Any],
    ) -> tuple[PromptOverflowInfo, bool]:
        """对应 Go ``detectPromptOverflow``。"""
        from atri_x.agent.context_runtime import ensure_context_runtime

        budget = await ensure_context_runtime(self).prompt_budget(ctx, selection)
        if budget.window <= 0:
            return PromptOverflowInfo(budget=budget), False
        info = PromptOverflowInfo(
            budget=budget,
            estimated_tokens=estimate_messages_tokens(messages)
            + estimate_tools_tokens(tools),
        )
        if messages:
            info.current_tokens = estimate_message_tokens(messages[-1])
        if info.single_message_exceeded():
            info.reason = "single_message"
            return info, True
        if info.total_exceeded():
            info.reason = "total_prompt"
            return info, True
        return info, False

    # -- 处理 ---------------------------------------------------------------
    async def handle_initial_prompt_overflow(
        self,
        ctx: Any,
        session: Session | None,
        selection: ModelSelection,
        messages: list[LLMMessage],
        tools: Sequence[Any],
        out: Any,
    ) -> tuple[bool, bool]:
        """处理**首轮**输入溢出。对应 Go ``handleInitialPromptOverflow``。

        返回 ``(继续发送, 是否改写过输入)``；输入列表**就地**修改（对应 Go 的
        ``*[]llm.LLMMessage`` 指针）。
        """
        if not messages:
            return True, False
        info, exceeded = await self.detect_prompt_overflow(
            ctx, session, selection, messages, tools
        )
        if not exceeded:
            return True, False

        mode = self.context_overflow_mode(ctx, session)
        latest_index = len(messages) - 1
        old_segments = list(messages[latest_index].segments)
        text = segments_content_text(old_segments)

        other_tokens = info.estimated_tokens - info.current_tokens
        available = info.budget.input_limit - other_tokens
        if available < 0:
            available = 0
        current_limit = available
        if 0 < info.budget.single_message_limit < current_limit:
            current_limit = info.budget.single_message_limit
        if current_limit > MESSAGE_TOKEN_OVERHEAD:
            current_limit -= MESSAGE_TOKEN_OVERHEAD
        else:
            current_limit = 0
        # 整段超限时不允许改写最后一条消息（改写救不了已经装满的上下文）。
        if not info.single_message_exceeded():
            mode = "reject"

        if mode == "truncate":
            budget = current_limit - estimate_text_tokens(TRUNCATION_MARKER)
            if budget <= 0:
                return self._reject_overflow(ctx, session, selection, info, out)
            truncated = truncate_text_to_tokens(text, budget)
            if not truncated.strip():
                return self._reject_overflow(ctx, session, selection, info, out)
            messages[latest_index].segments = set_segment_text(
                old_segments, truncated + TRUNCATION_MARKER
            )
            _send_notice(
                out,
                ctx,
                WARN_LEVEL,
                self.overflow_alert_text(
                    session, selection, info, mode, "已自动截断后继续处理"
                ),
            )
            return True, True

        if mode == "summarize":
            if current_limit <= 0:
                return self._reject_overflow(ctx, session, selection, info, out)
            compact_selection = self.compact_selection_for_session(session)
            if not compact_selection.provider or not compact_selection.model:
                compact_selection = selection
            from atri_x.agent.context_runtime import ensure_context_runtime

            try:
                summary = await ensure_context_runtime(self).summarize_text(
                    ctx, compact_selection, text, current_limit
                )
            except Exception:  # noqa: BLE001 - 摘要失败按拒绝处理（与 Go 一致）
                summary = ""
            if not (summary or "").strip():
                _send_notice(
                    out,
                    ctx,
                    WARN_LEVEL,
                    self.overflow_alert_text(
                        session, selection, info, "reject", "自动摘要失败，已拒绝发送"
                    ),
                )
                return False, False
            messages[latest_index].segments = set_segment_text(old_segments, summary)
            _send_notice(
                out,
                ctx,
                WARN_LEVEL,
                self.overflow_alert_text(
                    session, selection, info, mode, "已自动摘要后继续处理"
                ),
            )
            return True, True

        return self._reject_overflow(ctx, session, selection, info, out)

    def _reject_overflow(
        self,
        ctx: Any,
        session: Session | None,
        selection: ModelSelection,
        info: PromptOverflowInfo,
        out: Any,
    ) -> tuple[bool, bool]:
        _send_notice(
            out,
            ctx,
            WARN_LEVEL,
            self.overflow_alert_text(
                session, selection, info, "reject", "未发送给模型，原文未写入会话"
            ),
        )
        return False, False

    async def guard_prompt_before_call(
        self,
        ctx: Any,
        session_id: str,
        selection: ModelSelection,
        messages: Sequence[LLMMessage],
        tools: Sequence[Any],
        out: Any,
    ) -> None:
        """工具阶段调用前的兜底：超限即停止本轮。对应 Go ``guardPromptBeforeCall``。

        超限时抛 :class:`atri_x.errors.PromptOverflowError`（哨兵实例是
        :data:`atri_x.errors.ErrPromptOverflow`，与 Go 的 ``errors.Is`` 对应关系是
        ``except PromptOverflowError``）。
        """
        session = await self.load_session(ctx, session_id)
        info, exceeded = await self.detect_prompt_overflow(
            ctx, session, selection, messages, tools
        )
        if not exceeded:
            return
        _send_notice(
            out,
            ctx,
            WARN_LEVEL,
            self.overflow_alert_text(
                session, selection, info, "reject", "工具阶段上下文超限，已停止本轮请求"
            ),
        )
        raise PromptOverflowError(
            f"prompt overflow: estimated={info.estimated_tokens} "
            f"limit={info.budget.input_limit}"
        )

    async def load_session(self, ctx: Any, session_id: str) -> Session | None:
        """按 ID 取会话（取不到返回 ``None``）。对应 Go ``loadSession``。"""
        store = getattr(self, "store", None)
        if store is None or getattr(store, "sessions", None) is None:
            return None
        try:
            return store.sessions().get(session_id)
        except Exception:  # noqa: BLE001 - 会话不存在不是错误
            return None

    # -- 展示与设置 ---------------------------------------------------------
    def overflow_alert_text(
        self,
        session: Session | None,
        selection: ModelSelection,
        info: PromptOverflowInfo,
        mode: str,
        action: str,
    ) -> str:
        """长消息保护的用户可见提示。对应 Go ``overflowAlertText``（文本逐字一致）。"""
        mode_name = SessionModeWork
        if session is not None and getattr(session, "mode", ""):
            mode_name = session.mode
        lines = [
            "⚠️ 长消息保护已触发",
            f"模式：{mode_name} / 策略：{mode}",
            f"模型：{selection.provider}/{selection.model}",
            f"模型窗口：{info.budget.window} tokens；可用输入：{info.budget.input_limit} tokens",
            f"估算输入：{info.estimated_tokens} tokens；本条消息：{info.current_tokens} tokens",
        ]
        if info.budget.single_message_limit > 0:
            lines.append(f"单条消息上限：{info.budget.single_message_limit} tokens")
        lines.append("处理：" + action)
        if mode == "reject":
            prefix = self.command_prefix()
            lines.append(
                f"建议：拆分为多条发送；管理员可发送 {prefix}overflow --chat summarize "
                f"或 {prefix}overflow --work summarize 启用自动摘要；"
                f"也可先发送 {prefix}compact 压缩历史。"
            )
        return "\n".join(lines) + "\n"

    async def context_policy_status(self, ctx: Any) -> str:
        """``/overflow`` 的状态文本。对应 Go ``ContextPolicyStatus``。"""
        scope = self.scope(ctx)
        key = context_overflow_key(scope)
        current_mode = self.current_mode(ctx)
        override, _ = self.context_overflow_for_scope(scope)
        from atri_x.agent.context_runtime import ensure_context_runtime

        global_mode = ensure_context_runtime(self).overflow_mode()
        if not valid_overflow_mode(global_mode):
            global_mode = "reject"
        chat = global_mode
        work = global_mode
        if (override.chat or "").strip():
            chat = override.chat.strip().lower()
        if (override.work or "").strip():
            work = override.work.strip().lower()
        chat_source = "当前群覆盖" if (override.chat or "").strip() else "全局默认"
        work_source = "当前群覆盖" if (override.work or "").strip() else "全局默认"
        prefix = self.command_prefix()
        return "\n".join(
            [
                "长消息保护策略",
                f"作用域：{key}",
                f"当前模式：{current_mode}",
                f"chat：{chat}（{chat_source}）",
                f"work：{work}（{work_source}）",
                f"全局默认：{global_mode}",
                "可选值：reject | truncate | summarize",
                f"修改：{prefix}overflow [--chat|--work|--all] <值>；"
                f"重置：{prefix}overflow reset [--chat|--work|--all]",
            ]
        )

    async def set_context_policy(self, ctx: Any, target: str, value: str) -> str:
        """设置群级覆盖。对应 Go ``SetContextPolicy``（协议要求 ``async``）。"""
        target = normalize_overflow_target(target)
        value = (value or "").strip().lower()
        if not valid_overflow_mode(value):
            raise InvalidError(
                f'无效的长消息保护策略 "{value}"，可选：reject、truncate、summarize'
            )
        scope = self.scope(ctx)
        previous = self.context_overflow_snapshot()
        override, _ = self.context_overflow_for_scope(scope)
        if target in ("chat", "all"):
            override.chat = value
        if target in ("work", "all"):
            override.work = value
        self.set_context_overflow_for_scope(scope, override)
        try:
            self.save_runtime_state()
        except Exception:
            self.set_context_overflow_snapshot(previous)
            raise
        return f"长消息保护策略已更新：{target} = {value}"

    async def reset_context_policy(self, ctx: Any, target: str) -> str:
        """重置群级覆盖。对应 Go ``ResetContextPolicy``（协议要求 ``async``）。"""
        target = normalize_overflow_target(target)
        scope = self.scope(ctx)
        previous = self.context_overflow_snapshot()
        override, _ = self.context_overflow_for_scope(scope)
        if target == "all":
            override = ContextOverflowConfig()
        elif target == "chat":
            override.chat = ""
        elif target == "work":
            override.work = ""
        self.set_context_overflow_for_scope(scope, override)
        try:
            self.save_runtime_state()
        except Exception:
            self.set_context_overflow_snapshot(previous)
            raise
        return f"长消息保护策略已重置：{target} 使用全局默认"


def _send_notice(out: Any, ctx: Any, level: str, text: str) -> None:
    """经 ``turnOutput`` 发通知（``out`` 为 agent-core 的 turn_output，缺失即跳过）。"""
    sender = getattr(out, "send_notice", None)
    if not callable(sender):
        return
    try:
        sender(ctx, level, text)
    except Exception:  # noqa: BLE001 - 通知失败不能改变裁决结果
        pass
