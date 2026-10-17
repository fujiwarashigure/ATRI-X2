"""异步 Session 命名。对应 Go：``internal/session/naming.go``。

命名在后台线程里跑（Go 是 ``go s.generateTitle(...)``），因此**迟到**的写入必须
在事务内重新检查 ``title_renamed`` 标记：模型跑完时用户可能已经手动改过标题，手动
改名永远优先，此时放弃自动标题（向上抛 :data:`atri_x.errors.ErrTitleRenamed`，
由本模块吞掉并标记命名已完成）。

本模块提供 :class:`NamingMixin`，由 :class:`atri_x.session.service.Service` 组合；
mixin 依赖宿主类的 ``store`` / ``mu`` / ``naming_config`` / ``title_gen`` /
``notifier`` / ``naming_states`` 成员。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from dataclasses import dataclass
from typing import Any

from atri_x.errors import ErrTitleRenamed, TitleRenamedError
from atri_x.session.text import PLACEHOLDER_TITLE, default_title
from atri_x.session.types import (
    NamingCompletedEvent,
    NamingFailedEvent,
    NamingScheduledEvent,
)
from atri_x.storage.models import Message, RoleAssistant, RoleUser
from atri_x.storage.times import now

__all__ = [
    "MAX_NAMING_FAILURES",
    "NamingState",
    "NoopNamingNotifier",
    "NamingMixin",
    "fallback_title",
    "filter_conversation_messages",
    "invalid_title_reason",
    "is_placeholder_title",
    "llm_call_status",
    "normalize_title",
    "title_renamed_in_metadata",
]

#: 命名最大失败次数（达到后启用 fallback 标题并停止重试）。对应 Go ``maxNamingFailures``。
MAX_NAMING_FAILURES = 3


@dataclass
class NamingState:
    """单个会话的命名状态。对应 Go ``namingState``。"""

    in_flight: bool = False
    done: bool = False
    failures: int = 0


class NoopNamingNotifier:
    """默认通知者（什么都不做）。对应 Go ``noopNamingNotifier``。"""

    def notify_naming_scheduled(self, event: NamingScheduledEvent) -> None:
        ...

    def notify_naming_completed(self, event: NamingCompletedEvent) -> None:
        ...

    def notify_naming_failed(self, event: NamingFailedEvent) -> None:
        ...


async def _await_value(value: Any) -> Any:
    """把可等待对象包成协程，便于交给 :func:`asyncio.run`。"""
    return await value


def _call_title_generator(generator: Any, ctx: Any, messages: list[Message]) -> Any:
    """调用标题生成器并把协程跑完，返回 ``TitleResult``。

    对应 Go ``titleGenerator.GenerateTitle(ctx, messages)``。这里有两个 Python
    特有的过渡问题：

    * **异步实现**：真实实现是
      :meth:`atri_x.agent.title.TitleGenerator.generate_title`，签名
      ``async def generate_title(self, ctx, messages)``。命名跑在后台线程里
      （见 :meth:`NamingMixin.maybe_schedule_naming`），线程内没有事件循环，
      所以在本线程 ``asyncio.run`` 跑完它；万一当前线程已经有循环（非生产路径），
      改到一次性线程里跑，避免 ``asyncio.run`` 抛 ``RuntimeError``。
    * **同步替身**：测试替身可能写成只收 ``messages`` 的同步方法，按形参个数决定
      是否传 ``ctx``，避免把 ``ctx`` 硬塞给只认一个参数的调用方。

    历史上这里只传了 ``messages``，而真实实现要 ``(ctx, messages)`` 且是协程 ——
    结果是**自动命名永远失败**（每次都记成 ``llm_error``，模型标题从不落库），
    而同步假实现让测试全绿。改动这条调用约定时请同步跑
    ``tests/test_session_naming_contract.py``。
    """
    function = generator.generate_title
    try:
        positional = [
            parameter
            for parameter in inspect.signature(function).parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):  # 内建/扩展可调用对象
        positional = [None, None]
    result = function(ctx, messages) if len(positional) >= 2 else function(messages)
    if not inspect.isawaitable(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_value(result))

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(_await_value(result))
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传
            box["error"] = exc

    thread = threading.Thread(
        target=runner, name="session-naming-await", daemon=True
    )
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class NamingMixin:
    """``Service`` 的命名部分（Go 同文件的全部方法）。"""

    def maybe_schedule_naming(self, session_id: str) -> None:
        """按触发步数决定是否排期一次异步命名。对应 Go ``MaybeScheduleNaming``。"""
        if self.title_renamed(session_id) or self.title_gen is None:
            return
        try:
            messages = self.store.messages().list_by_session(session_id)
        except Exception as exc:  # noqa: BLE001 - 与 Go 一样转成命名失败事件
            self.notify_naming_failed(
                NamingFailedEvent(
                    session_id=session_id,
                    reason="load messages",
                    err=exc,
                    triggered_at=now(),
                )
            )
            return
        conversation = filter_conversation_messages(messages)
        if len(conversation) < self.naming_config.trigger_step:
            return
        naming_messages = list(conversation[: self.naming_config.trigger_step])
        if not self.mark_naming_in_flight(session_id):
            return
        self.notify_naming_scheduled(
            NamingScheduledEvent(
                session_id=session_id,
                triggered_at=now(),
                message_count=len(naming_messages),
                trigger_step=self.naming_config.trigger_step,
            )
        )
        worker = threading.Thread(
            target=self.generate_title,
            args=(session_id, naming_messages),
            name="session-naming",
            daemon=True,
        )
        worker.start()

    def title_renamed(self, session_id: str) -> bool:
        """该会话的标题是否已被手动改过。对应 Go ``titleRenamed``。"""
        try:
            session = self.store.sessions().get(session_id)
        except Exception:  # noqa: BLE001 - Go 里读失败按"没改过"处理
            return False
        return title_renamed_in_metadata(session.metadata)

    def generate_title(self, session_id: str, messages: list[Message]) -> None:
        """生成并落库标题（在后台线程里执行）。对应 Go ``generateTitle``。"""
        try:
            session = self.store.sessions().get(session_id)
        except Exception as exc:  # noqa: BLE001
            self.mark_naming_failed(session_id)
            self.notify_naming_failed(
                NamingFailedEvent(
                    session_id=session_id,
                    reason="load session",
                    err=exc,
                    triggered_at=now(),
                    message_count=len(messages),
                )
            )
            return

        try:
            result = _call_title_generator(
                self.title_gen, getattr(self, "title_ctx", None), messages
            )
        except Exception as exc:  # noqa: BLE001
            self.handle_naming_failure(
                session, messages, "generate title", exc, "llm_error", "", ""
            )
            return
        title = normalize_title(result.raw_title)
        if not title or is_placeholder_title(title):
            self.handle_naming_failure(
                session,
                messages,
                "invalid title",
                None,
                "invalid_response",
                result.raw_title,
                title,
            )
            return

        session.title = title
        session.updated_at = now()

        def update(current) -> None:
            # 手动改名的优先级高于迟到的自动标题，必须在事务里重新判定。
            if title_renamed_in_metadata(current.metadata):
                raise ErrTitleRenamed
            current.title = title
            current.updated_at = now()

        try:
            updated = self.store.sessions().mutate(session_id, update)
        except TitleRenamedError:
            self.mark_naming_done(session_id)
            return
        except Exception as exc:  # noqa: BLE001
            self.handle_naming_failure(
                session, messages, "update title", exc, "storage_update", result.raw_title, title
            )
            return
        session.title = updated.title
        session.updated_at = updated.updated_at
        self.mark_naming_done(session_id)
        self.notify_naming_completed(
            NamingCompletedEvent(
                session_id=session_id,
                title=title,
                triggered_at=now(),
                message_count=len(messages),
            )
        )

    def mark_naming_in_flight(self, session_id: str) -> bool:
        """占用命名名额；已在跑/已完成/失败超限时返回 ``False``。对应 Go ``markNamingInFlight``。"""
        with self.mu:
            state = self.naming_states.get(session_id) or NamingState()
            if state.in_flight or state.done or state.failures >= MAX_NAMING_FAILURES:
                return False
            state.in_flight = True
            self.naming_states[session_id] = state
            return True

    def mark_naming_done(self, session_id: str) -> None:
        """标记命名完成（不再重试）。对应 Go ``markNamingDone``。"""
        with self.mu:
            state = self.naming_states.get(session_id) or NamingState()
            state.in_flight = False
            state.done = True
            self.naming_states[session_id] = state

    def mark_naming_failed(self, session_id: str) -> int:
        """记一次命名失败并返回累计失败次数。对应 Go ``markNamingFailed``。"""
        with self.mu:
            state = self.naming_states.get(session_id) or NamingState()
            state.in_flight = False
            state.failures += 1
            self.naming_states[session_id] = state
            return state.failures

    def handle_naming_failure(
        self,
        session,
        messages: list[Message],
        reason: str,
        err: BaseException | None,
        stage: str,
        raw_title: str,
        normalized_title: str,
    ) -> None:
        """通知命名失败；失败到上限或标题仍是占位符时落 fallback 标题。

        对应 Go ``handleNamingFailure``。
        """
        failures = self.mark_naming_failed(session.id)
        event = NamingFailedEvent(
            session_id=session.id,
            title=session.title,
            stage=stage,
            llm_call=llm_call_status(err),
            generated_title_raw=raw_title,
            generated_title_normalized=normalized_title,
            invalid_reason=invalid_title_reason(normalized_title),
            reason=reason,
            err=err,
            triggered_at=now(),
            message_count=len(messages),
            failure_count=failures,
            max_failures=MAX_NAMING_FAILURES,
        )
        should_fallback = failures >= MAX_NAMING_FAILURES or is_placeholder_title(session.title)
        if should_fallback:
            event.fallback_title = fallback_title(messages)
            event.fallback_applied = event.fallback_title != ""
            self.mark_naming_done(session.id)
        self.notify_naming_failed(event)
        if not should_fallback:
            return
        if not event.fallback_title:
            return

        def update(current) -> None:
            if title_renamed_in_metadata(current.metadata):
                raise ErrTitleRenamed
            current.title = event.fallback_title
            current.updated_at = now()

        try:
            updated = self.store.sessions().mutate(session.id, update)
        except TitleRenamedError:
            return
        except Exception as update_err:  # noqa: BLE001
            self.notify_naming_failed(
                NamingFailedEvent(
                    session_id=session.id,
                    title=session.title,
                    reason="fallback title",
                    err=update_err,
                    triggered_at=now(),
                    message_count=len(messages),
                )
            )
            return
        session.title = updated.title
        session.updated_at = updated.updated_at

    def notify_naming_scheduled(self, event: NamingScheduledEvent) -> None:
        """转发"已排期"事件。对应 Go ``notifyNamingScheduled``。"""
        if self.notifier is not None:
            self.notifier.notify_naming_scheduled(event)

    def notify_naming_completed(self, event: NamingCompletedEvent) -> None:
        """转发"已完成"事件。对应 Go ``notifyNamingCompleted``。"""
        if self.notifier is not None:
            self.notifier.notify_naming_completed(event)

    def notify_naming_failed(self, event: NamingFailedEvent) -> None:
        """转发"已失败"事件。对应 Go ``notifyNamingFailed``。"""
        if self.notifier is not None:
            self.notifier.notify_naming_failed(event)


def title_renamed_in_metadata(raw: str) -> bool:
    """metadata 是否标记标题为手动命名。对应 Go ``titleRenamedInMetadata``。"""
    if not (raw or "").strip():
        return False
    try:
        metadata = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(metadata, dict):
        return False
    return metadata.get("title_renamed") is True


def filter_conversation_messages(messages: list[Message]) -> list[Message]:
    """只保留有正文的 user / assistant 消息。对应 Go ``filterConversationMessages``。"""
    kept: list[Message] = []
    for message in messages:
        if message.role not in (RoleUser, RoleAssistant):
            continue
        if not (message.content or "").strip():
            continue
        kept.append(message)
    return kept


def normalize_title(text: str) -> str:
    """规范化模型给出的标题。对应 Go ``normalizeTitle``。"""
    text = (text or "").replace("\n", " ").strip()
    text = text.strip("\"'` ")
    return default_title(text)


def is_placeholder_title(title: str) -> bool:
    """标题是否还是占位符。对应 Go ``isPlaceholderTitle``（``EqualFold`` → ``casefold``）。"""
    return (title or "").strip().casefold() == PLACEHOLDER_TITLE.casefold()


def llm_call_status(err: BaseException | None) -> str:
    """LLM 调用结果状态串。对应 Go ``llmCallStatus``。"""
    return "failed" if err is not None else "succeeded"


def invalid_title_reason(title: str) -> str:
    """标题不合法的原因（合法时为空串）。对应 Go ``invalidTitleReason``。"""
    if not (title or "").strip():
        return "empty title"
    if is_placeholder_title(title):
        return "placeholder title"
    return ""


def fallback_title(messages: list[Message]) -> str:
    """用第一条 user 消息兜底标题。对应 Go ``fallbackTitle``。"""
    for message in messages:
        if message.role == RoleUser:
            return default_title(message.content)
    return ""
