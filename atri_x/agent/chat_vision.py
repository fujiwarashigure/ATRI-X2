"""图片不受支持时的透明兜底。

对应 Go：``internal/agent/chat_llm.go`` 里的 ``shouldFallbackVision`` /
``visionFallbackTransparent`` / ``fallbackVisionMessages`` /
``notifyVisionFallbackOnce`` 与 ``visionFallbackBlockedNotice``。
``chat_llm.py`` 在文件末尾再导出，导入路径不变。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.chat_observe import should_show_cli_reasoning
from atri_x.delivery import ctx_value
from atri_x.llm import (
    LLMMessage,
    clone_messages,
    messages_have_image_segment,
    segments_content_text,
    text_segments,
)
from atri_x.llm.errors import ErrorCategoryVisionUnsupported, as_api_error


#: 模型已在输出中才报告图片不受支持时的提示。对应 Go ``visionFallbackBlockedNotice``。
VISION_FALLBACK_BLOCKED_NOTICE = (
    "模型在输出过程中报告了图片不受支持；为避免重复输出，本轮未自动改用图片描述，"
    "请重试或切换支持视觉的模型。"
)
#: 标记"这次请求已经为图片兜底重试过"。对应 Go ``visionFallbackAttemptKey{}``。
_VISION_ATTEMPT_KEY = object()


def should_fallback_vision(messages: list[LLMMessage], err: BaseException | None) -> bool:
    """图片请求是否因"模型不支持视觉"失败，可以改用文本描述重试。

    对应 Go ``shouldFallbackVision``：只看适配器给的 ``category`` 与 HTTP 状态码，
    绝不按人类可读的 message 分类。
    """
    if err is None or not messages_have_image_segment(messages):
        return False
    api_err = as_api_error(err)
    if api_err is None or api_err.category != ErrorCategoryVisionUnsupported:
        return False
    return int(getattr(api_err, "status_code", 0) or 0) in (400, 422, 404)


def vision_fallback_transparent(
    assistant_text: str, tool_call_count: int, reasoning_open: bool
) -> bool:
    """能否在不重放任何内容的前提下重试。对应 Go ``visionFallbackTransparent``。"""
    return assistant_text == "" and tool_call_count == 0 and not reasoning_open


def fallback_vision_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """把所有分段压成纯文本。对应 Go ``fallbackVisionMessages``。"""
    out = list(clone_messages(messages) or [])
    for message in out:
        if not message.segments:
            continue
        message.segments = text_segments(segments_content_text(message.segments))
    return out


def _vision_attempted(ctx: Any) -> bool:
    return bool(ctx_value(ctx, _VISION_ATTEMPT_KEY, False))


async def _notify_vision_fallback_once(agent: Any, ctx: Any, session_id: str, out: Any) -> None:
    """每个会话只提示一次"图片已按文本处理"。对应 Go ``notifyVisionFallbackOnce``。"""
    if not should_show_cli_reasoning(agent, ctx):
        return
    notified = getattr(agent, "vision_fallback_notified", None)
    if notified is None:  # pragma: no cover - 未装配时不提示
        return
    lock = getattr(agent, "vision_fallback_mu", None)
    if lock is not None:
        with lock:
            if notified.get(session_id):
                return
            notified[session_id] = True
    else:
        if notified.get(session_id):
            return
        notified[session_id] = True
    await out.send_assistant(ctx, "当前模型似乎不支持视觉，图片已按文本描述处理。")
