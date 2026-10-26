"""请求级时间上下文：入站消息时间基准 + 系统提示里的时间片段。

对应 Go：Go 侧没有这一层（提示词里只有 ``session_created_at``）；本模块是 Python 侧
「模型必须看到当前时间」的落地点。

分工：

* **入站时间基准**：平台适配器把「消息什么时候到」写进 ctx（:func:`with_inbound_times`），
  ``chat.py`` 再把它写进 ``Message.metadata``；相对时间（"明天八点"）以
  ``message_received_at`` 解释，而不是以「请求准备时刻」解释。
* **提示词片段**：:func:`inject_time_block` 在 :func:`atri_x.agent.chat_llm.call_llm`
  里、``system_append`` 之后调用；片段带稳定标记 :data:`TIME_BLOCK_MARKER`，
  每次注入前**先剥旧段再追加**，保证一次请求最多一份（工具续轮 / 视觉重试 /
  DSH 路由 / 后台任务都不会累积）。

片段内容**不得**声称"已校准"：``source=system_clock`` 只表示读到了系统时钟，
网络观测在首版 ``verify_only`` 下只写诊断。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from atri_x.delivery import ctx_value, ctx_with_value
from atri_x.errors import InvalidError
from atri_x.llm.llm import LLMMessage, Role, segments_text_only, text_segments

__all__ = [
    "TIME_BLOCK_MARKER",
    "build_time_block",
    "inbound_times",
    "inject_time_block",
    "message_time_metadata",
    "merge_message_metadata",
    "platform_sent_at_from_context",
    "received_at_from_context",
    "strip_time_block",
    "with_inbound_times",
    "with_platform_sent_at",
]

#: Go 的 ``inboundReceivedAtKey{}`` / ``inboundPlatformSentAtKey{}`` 等价物。
InboundReceivedAt = object()
InboundPlatformSentAt = object()

#: 时间片段首行标记。改版本号即可让旧片段被识别成"待替换"。
TIME_BLOCK_MARKER = "[atx-time:v1]"

#: 接收时刻与处理时刻相差超过它时，提示模型不要直接按"今天/明天"推断。
AMBIGUITY_SECONDS = 300.0

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _ensure_aware(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise InvalidError(f"{label} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidError(f"{label} must be timezone-aware: {value!r}")
    return value


# ---------------------------------------------------------------------------
# 入站时间基准（ctx 值）
# ---------------------------------------------------------------------------


def with_inbound_times(
    ctx: Any,
    *,
    received_at: dt.datetime | None,
    platform_sent_at: dt.datetime | None = None,
) -> Any:
    """把入站时间写进 ctx（``received_at`` 为空时原样返回 ctx）。"""
    if received_at is None:
        return ctx
    moment = _ensure_aware(received_at, "message_received_at")
    ctx = ctx_with_value(ctx, InboundReceivedAt, moment)
    if platform_sent_at is not None:
        ctx = ctx_with_value(
            ctx, InboundPlatformSentAt, _ensure_aware(platform_sent_at, "platform_sent_at")
        )
    return ctx


def received_at_from_context(ctx: Any) -> dt.datetime | None:
    value = ctx_value(ctx, InboundReceivedAt)
    return value if isinstance(value, dt.datetime) else None


def with_platform_sent_at(ctx: Any, moment: dt.datetime | None) -> Any:
    """写入平台侧的「消息发送时间」（平台已知时才有；``None`` 原样返回）。"""
    if moment is None:
        return ctx
    return ctx_with_value(
        ctx, InboundPlatformSentAt, _ensure_aware(moment, "platform_sent_at")
    )


def platform_sent_at_from_context(ctx: Any) -> dt.datetime | None:
    value = ctx_value(ctx, InboundPlatformSentAt)
    return value if isinstance(value, dt.datetime) else None


def inbound_times(ctx: Any) -> tuple[dt.datetime | None, dt.datetime | None]:
    """返回 ``(message_received_at, platform_sent_at)``。"""
    return received_at_from_context(ctx), platform_sent_at_from_context(ctx)


def message_time_metadata(ctx: Any) -> dict[str, str]:
    """``Message.metadata`` 里要落库的时间字段（RFC3339 文本）。

    **不改** ``Message.created_at`` 的语义（它仍由存储层补齐），只在 metadata 里
    记录「平台接收时刻 / 平台发送时刻」，旧数据没有这两个键也完全兼容。
    """
    received_at, platform_sent_at = inbound_times(ctx)
    meta: dict[str, str] = {}
    if received_at is not None:
        meta["received_at"] = received_at.astimezone(dt.timezone.utc).isoformat()
    if platform_sent_at is not None:
        meta["platform_sent_at"] = platform_sent_at.astimezone(dt.timezone.utc).isoformat()
    return meta


def merge_message_metadata(base: str, ctx: Any) -> str:
    """把入站时间合并进已有 metadata JSON（``base`` 不是 JSON 对象时原样返回）。

    ``Message.metadata`` 是 JSON blob；发言人信息已经占用了它，所以时间字段只能
    **合并**而不能整份覆盖。
    """
    extra = message_time_metadata(ctx)
    if not extra:
        return base
    if not (base or "").strip():
        data: dict[str, Any] = {}
    else:
        try:
            loaded = json.loads(base)
        except (TypeError, ValueError):
            return base
        if not isinstance(loaded, dict):
            return base
        data = loaded
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 提示词片段
# ---------------------------------------------------------------------------

#: 各校验状态下给模型的说明。**任何一条都不允许说"已校准/准确"。**
_VERIFICATION_NOTES = {
    "disabled": "网络时间校验未启用；系统时间未经证实，不得声称已校准。",
    "checking": "正在与网络时间来源比对；结果未知，不得声称已校准。",
    "consistent": "网络来源与本地一致（仅观测）；不得声称已校准或已同步。",
    "insufficient_evidence": "网络证据不足（独立来源不够）；系统时间未经证实，不得声称已校准。",
    "conflict": "本地时间与网络来源冲突，当前时间可能不准确；先与用户确认再安排时间。",
    "stale": "最近一次网络时间样本已过期；系统时间是否准确未知。",
    "unavailable": "本轮没有可用的网络时间来源；系统时间未经证实，不得声称已校准。",
}


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(round(max(0.0, seconds)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    if total < 86400:
        return f"{total // 3600}h{(total % 3600) // 60:02d}m"
    return f"{total // 86400}d{(total % 86400) // 3600:02d}h"


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def build_time_block(
    service: Any,
    ctx: Any = None,
    *,
    timezone: str | None = None,
    inject_instant: bool = True,
) -> str:
    """构造时间片段文本（稳定行在前、动态值在后）。

    :param service: :class:`atri_x.time_service.TimeService`
    :param ctx: 请求 ctx（读 ``message_received_at`` / ``platform_sent_at``）
    :param inject_instant: ``False`` 时只写时区说明、不写具体时刻（``inject_context``
        的降级形态）。
    """
    snapshot = service.snapshot(timezone)
    offset = snapshot.utc_offset
    sign = "+" if offset >= dt.timedelta(0) else "-"
    seconds = int(abs(offset).total_seconds())
    offset_text = f"UTC{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"
    lines = [TIME_BLOCK_MARKER, f"timezone={snapshot.timezone} ({offset_text})"]
    if inject_instant:
        lines.append(
            f"current={_iso(snapshot.current_time)} "
            f"({_WEEKDAYS[snapshot.weekday]}, unix={snapshot.unix_timestamp})"
        )
    else:
        lines.append("current=unknown (time injection is limited to the timezone)")
    lines.append(
        f"source={snapshot.source} verification={snapshot.verification_state}"
    )
    received_at, platform_sent_at = inbound_times(ctx)
    if received_at is not None:
        local_received = received_at.astimezone(service.timezone())
        age = service.age_seconds(received_at)
        lines.append(
            f"message_received_at={_iso(local_received)} (age={_format_age(age)})"
        )
    if platform_sent_at is not None:
        local_sent = platform_sent_at.astimezone(service.timezone())
        lines.append(f"platform_sent_at={_iso(local_sent)}")
    note = _VERIFICATION_NOTES.get(
        snapshot.verification_state,
        "系统时间未经证实，不得声称已校准。",
    )
    lines.append(f"note={note}")
    if received_at is not None:
        age = service.age_seconds(received_at)
        if age is not None and age > AMBIGUITY_SECONDS:
            lines.append(
                "note=消息接收已过去 "
                f"{_format_age(age)}；相对时间（今天/明天/几点）先向用户确认。"
            )
    return "\n".join(lines)


def _strip_marker(text: str) -> str:
    """删掉文本里已存在的整个时间片段（从标记行到片段末尾）。

    片段由「标记行 + 若干 ``key=value`` 行」组成，因此从标记行往后一直吃到
    第一条**不像片段行**的内容为止；重复片段会被逐个剥掉。
    """
    current = text
    for _ in range(8):  # 防御性上限：正常最多一份
        if "[atx-time:" not in current:
            break
        candidate = _strip_one_block(current)
        if candidate == current:
            break
        current = candidate
    return current


def _strip_one_block(text: str) -> str:
    index = text.find("[atx-time:")
    if index == -1:
        return text
    head = text[:index]
    lines = text[index:].split("\n")
    keep_from = 1  # 跳过标记行
    while keep_from < len(lines):
        stripped = lines[keep_from].strip()
        if stripped and not _looks_like_block_line(stripped):
            break
        keep_from += 1
    rest = "\n".join(lines[keep_from:]).lstrip()
    if not rest:
        return head.strip()
    if not head.strip():
        return rest
    return head.rstrip() + "\n\n" + rest


def _looks_like_block_line(line: str) -> bool:
    if line.startswith("[atx-time:"):
        return True
    key = line.split("=", 1)[0]
    return key in {
        "timezone",
        "current",
        "source",
        "message_received_at",
        "platform_sent_at",
        "note",
    }


def strip_time_block(messages: list[LLMMessage]) -> list[LLMMessage]:
    """返回**新的**消息列表，其中系统消息里的旧时间片段已被剔除。

    不修改传入的 :class:`LLMMessage` 对象：调用方的 ``base_messages`` 必须保持原样
    （``call_llm`` 会把它作为 ``result.messages`` 交给下一轮）。
    """
    out: list[LLMMessage] = []
    for message in messages:
        if message.role != Role.SYSTEM:
            out.append(message)
            continue
        text = segments_text_only(message.segments)
        if TIME_BLOCK_MARKER not in text and "[atx-time:" not in text:
            out.append(message)
            continue
        cleaned = _strip_marker(text).strip()
        clone = LLMMessage(
            role=message.role,
            segments=text_segments(cleaned) if cleaned else [],
            name=message.name,
            tool_call_id=message.tool_call_id,
            tool_calls=list(message.tool_calls),
            reasoning_content=message.reasoning_content,
        )
        out.append(clone)
    return out


def inject_time_block(
    messages: list[LLMMessage],
    service: Any,
    ctx: Any = None,
    *,
    enabled: bool = True,
    inject_instant: bool = True,
) -> list[LLMMessage]:
    """把时间片段注入系统消息（幂等：先剥旧段再追加）。

    ``enabled=False`` 或 ``service=None`` 时**不注入**（保持 1.0.9 行为）；此时仍会
    剥掉已有片段，避免历史消息里的旧时间被当成现在。
    """
    if service is None:
        return list(messages)
    stripped = strip_time_block(messages)
    if not enabled:
        return stripped
    block = build_time_block(service, ctx, inject_instant=inject_instant)
    out: list[LLMMessage] = []
    injected = False
    for message in stripped:
        if injected or message.role != Role.SYSTEM:
            out.append(message)
            continue
        text = segments_text_only(message.segments)
        combined = f"{text}\n\n{block}" if text.strip() else block
        out.append(
            LLMMessage(
                role=message.role,
                segments=text_segments(combined),
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=list(message.tool_calls),
                reasoning_content=message.reasoning_content,
            )
        )
        injected = True
    if not injected:
        out.insert(0, LLMMessage(role=Role.SYSTEM, segments=text_segments(block)))
    return out
