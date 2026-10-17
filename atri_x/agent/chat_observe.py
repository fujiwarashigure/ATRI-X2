"""模型调用的日志、审计与上下文助手。

对应 Go：``internal/agent/chat_llm.go`` 里的 ``logLLMOutput`` / ``auditUsage`` /
``elapsedMillis`` / ``userMessageSegments`` / ``platformSegmentsToLLM`` /
``fileSegmentText`` / ``isCLIContext`` / ``shouldShowCLIReasoning`` /
``sendCLIReasoning``。``chat_llm.py`` 在文件末尾再导出，导入路径不变。
"""

from __future__ import annotations

from typing import Any
from atri_x.logging import ResultFailed, ResultSucceeded
from atri_x.llm import Usage, text_segments
from atri_x.platform import message_context_from
from atri_x.agent.segments import platform_segments_to_llm
from atri_x.redact import error as redact_error
import time


def log_llm_output(
    agent: Any,
    ctx: Any,
    session_id: str,
    selection: Any,
    text: str,
    raw_text: str,
    tool_call_count: int,
    elapsed_ms: int,
) -> None:
    """记一条模型输出日志。对应 Go ``Agent.logLLMOutput``。"""
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    if not agent.history_enabled(ctx):
        logger.log(
            "info",
            "llm output",
            "event",
            "assistant_message",
            "session_id",
            session_id,
            "provider",
            selection.provider,
            "model",
            selection.model,
            "elapsed_ms",
            elapsed_ms,
            "tool_call_count",
            tool_call_count,
            "history",
            "off",
        )
        return
    from atri_x.agent.chat_tools import preview_log_text

    logger.log(
        "info",
        "llm output",
        "event",
        "assistant_message",
        "session_id",
        session_id,
        "provider",
        selection.provider,
        "model",
        selection.model,
        "elapsed_ms",
        elapsed_ms,
        "text",
        preview_log_text(text),
        "raw_text",
        preview_log_text(raw_text),
        "tool_call_count",
        tool_call_count,
    )


def audit_usage(
    agent: Any, session_id: str, selection: Any, usage: Usage | None, elapsed_ms: int
) -> None:
    """记一条用量审计。对应 Go ``Agent.auditUsage``。"""
    attrs: list[Any] = [
        "session_id",
        session_id,
        "provider",
        selection.provider,
        "model",
        selection.model,
        "elapsed_ms",
        elapsed_ms,
    ]
    if usage is not None:
        attrs.extend(
            [
                "prompt_tokens",
                usage.prompt_tokens,
                "completion_tokens",
                usage.completion_tokens,
                "total_tokens",
                usage.total_tokens,
                "cache_hit_tokens",
                usage.cache_hit_tokens,
            ]
        )
    attrs.extend(["result", ResultSucceeded])
    agent.audit("llm_usage", *attrs)


def _audit_llm_error(
    agent: Any, session_id: str, selection: Any, err: BaseException, started_at: float
) -> None:
    agent.audit(
        "llm_error",
        "session_id",
        session_id,
        "provider",
        selection.provider,
        "model",
        selection.model,
        "elapsed_ms",
        elapsed_millis(started_at),
        "error",
        redact_error(err),
        "result",
        ResultFailed,
    )


def elapsed_millis(started_at: float) -> int:
    """对应 Go ``elapsedMillis``（毫秒，基准是 ``time.monotonic``）。"""
    return int((time.monotonic() - started_at) * 1000)


def user_message_segments(ctx: Any, text: str) -> list[Any]:
    """入站消息的模型分段。对应 Go ``Agent.userMessageSegments``。"""
    message, ok = message_context_from(ctx)
    segments = getattr(message, "segments", None) if ok and message is not None else None
    if segments:
        return platform_segments_to_llm(list(segments), text)
    return text_segments(text)


def is_cli_context(agent: Any, ctx: Any) -> bool:
    """是否 CLI 上下文。对应 Go ``Agent.isCLIContext``。"""
    message, ok = message_context_from(ctx)
    if ok and message is not None:
        return getattr(message, "platform", "") == "cli"
    platform = getattr(agent, "platform", None)
    return platform is not None and platform.name() == "cli"


def should_show_cli_reasoning(agent: Any, ctx: Any) -> bool:
    """是否回显思维链（只在 CLI）。对应 Go ``Agent.shouldShowCLIReasoning``。"""
    return is_cli_context(agent, ctx)


def send_cli_reasoning(agent: Any, ctx: Any, text: str) -> None:
    """把思维链直接写给 CLI 发送器。对应 Go ``Agent.sendCLIReasoning``。"""
    if not text or not should_show_cli_reasoning(agent, ctx):
        return
    message, ok = message_context_from(ctx)
    sender = getattr(message, "sender", None) if ok and message is not None else None
    if sender is None:
        sender = getattr(agent, "platform", None)
    send = getattr(sender, "send_reasoning", None)
    if callable(send):
        send(ctx, text)
