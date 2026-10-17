"""入站语音转写（ASR）。

对应 Go：``internal/agent/asr.go``。

职责：``AudioTranscriber`` / ``AudioTranscriptionRequest`` /
``AudioTranscriptionResult`` / ``transcribePlatformAudio`` / ``transcribeAudioSegment`` /
``isTranscribableAudioSegment`` / ``asrTranscriptionText`` 与三个上限
（``DefaultASRParallelism`` / ``DefaultASRMaxSegments`` / ``DefaultASRMaxAudioBytes``）。

与 Go 的差异（形式，不是简化）：

* ``goroutine`` 工作池 → :func:`asyncio.gather` + :class:`asyncio.Semaphore`（同一批
  最多 ``parallelism`` 个在飞）。结果按槽位下标写回，顺序语义与 Go 相同。
* 预留额度用 agent-core 的 ``reserve_budget_batch_requests``；调用 ID 与摘要的构造
  逐字照抄 Go（``<turn>:<media>:asr:<index>`` / ``provider\\0model\\0media``）。
* ``a.media.ReadLimited`` 是 async（媒体层要 I/O），因此整条链路是 async；
  ``AgentSeams.transcribe_platform_audio`` 已经按 async 调用。
"""

from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from atri_x.agent.group_policy import context_overflow_key, group_policy_for_scope, is_group_scope
from atri_x.agent.tools import service_host, short_hash
from atri_x.platform import (
    SegmentFile,
    SegmentText,
    message_context_from,
    with_message_context,
)

__all__ = [
    "DEFAULT_ASR_MAX_AUDIO_BYTES",
    "DEFAULT_ASR_MAX_SEGMENTS",
    "DEFAULT_ASR_PARALLELISM",
    "AudioTranscriber",
    "AudioTranscriptionRequest",
    "AudioTranscriptionResult",
    "AsrMixin",
    "asr_transcription_text",
    "is_transcribable_audio_segment",
    "transcribe_platform_audio",
]

#: 一条消息里最多同时转写几段录音。对应 Go ``DefaultASRParallelism``。
DEFAULT_ASR_PARALLELISM = 2
#: 一条消息最多转写几段录音。对应 Go ``DefaultASRMaxSegments``。
DEFAULT_ASR_MAX_SEGMENTS = 4
#: 单段录音的默认读取上限。对应 Go ``DefaultASRMaxAudioBytes``。
DEFAULT_ASR_MAX_AUDIO_BYTES = 20 * 1024 * 1024


@dataclass
class AudioTranscriptionRequest:
    """一段待上传的录音。对应 Go ``AudioTranscriptionRequest``。"""

    media_id: str = ""
    data: bytes = b""
    name: str = ""
    mime_type: str = ""
    language: str = ""


@dataclass
class AudioTranscriptionResult:
    """一次成功的转写。对应 Go ``AudioTranscriptionResult``。"""

    text: str = ""
    language: str = ""
    provider: str = ""
    model: str = ""


@runtime_checkable
class AudioTranscriber(Protocol):
    """把一段入站录音变成文本。对应 Go ``AudioTranscriber`` 接口。"""

    async def transcribe(
        self, ctx: Any, req: AudioTranscriptionRequest
    ) -> AudioTranscriptionResult: ...


@dataclass
class _Slot:
    index: int = 0
    segment: Any = None


async def transcribe_platform_audio(agent: Any, ctx: Any) -> Any:
    """把当前消息里的语音段换成文本转写段。对应 Go ``transcribePlatformAudio``。

    ASR 未装配、没有当前平台消息、或消息里没有录音时原样返回 ``ctx``。
    转写失败会保留原来的 ``[语音]`` 标记，消息永不丢失。
    """
    host = service_host(agent)
    transcriber = _transcriber(host)
    media = getattr(host, "media", None)
    if host is None or transcriber is None or media is None:
        return ctx
    msg, ok = message_context_from(ctx)
    if not ok or not msg.segments:
        return ctx
    if is_group_scope(host, ctx) and not group_policy_for_scope(
        host, _scope(host, ctx)
    ).is_asr_enabled():
        return ctx
    limit = _segment_limit(host)
    slots: list[_Slot] = []
    skipped = 0
    for index, segment in enumerate(msg.segments):
        if not is_transcribable_audio_segment(segment):
            continue
        if not str(getattr(segment, "media_id", "") or "").strip():
            continue
        if len(slots) >= limit:
            skipped += 1
            continue
        slots.append(_Slot(index=index, segment=segment))
    if not slots:
        return ctx
    if skipped > 0:
        _audit(
            host,
            "asr_skipped",
            reason="batch_limit",
            limit=limit,
            skipped=skipped,
            scope=context_overflow_key(_scope(host, ctx)),
            result="skipped",
        )
    selection = _selection(host)
    provider = str(getattr(selection, "provider", "") or "").strip()
    model = str(getattr(selection, "model", "") or "").strip()
    if provider and model:
        deny = _authorize_model(host, ctx, selection)
        if deny is not None:
            _audit(
                host,
                "model_denied",
                kind="asr",
                provider=provider,
                model=model,
                reason=str(deny),
                scope=context_overflow_key(_scope(host, ctx)),
                result="rejected",
            )
            return ctx
    requests = []
    for index, slot in enumerate(slots):
        media_id = str(getattr(slot.segment, "media_id", "") or "").strip()
        turn_id = _turn_request_id(ctx) or short_hash(media_id)
        requests.append(
            {
                "call_id": "%s:%s:asr:%d" % (turn_id, media_id, index),
                "digest": short_hash("%s\x00%s\x00%s" % (provider, model, media_id)),
            }
        )
    ok_reserve, reason = _reserve_batch(host, ctx, "asr", requests)
    if not ok_reserve:
        _audit(
            host,
            "budget_denied",
            kind="asr",
            reason=reason,
            scope=context_overflow_key(_scope(host, ctx)),
            result="rejected",
        )
        return ctx

    workers = min(_parallelism_limit(host), len(slots))
    semaphore = asyncio.Semaphore(max(1, workers))

    async def _run(slot: _Slot) -> tuple[str, BaseException | None]:
        async with semaphore:
            return await _transcribe_segment(host, ctx, slot.segment, transcriber)

    results = await asyncio.gather(*(_run(slot) for slot in slots))
    out = list(msg.segments)
    for index, slot in enumerate(slots):
        text, error = results[index]
        if error is not None:
            _audit(
                host,
                "asr_failed",
                media_id=getattr(slot.segment, "media_id", ""),
                error=str(error),
                scope=context_overflow_key(_scope(host, ctx)),
                result="failed",
            )
            continue
        label = asr_transcription_text(index + 1, text)
        if not label:
            continue
        out[slot.index] = _text_segment(label)
    msg.segments = out
    return with_message_context(ctx, msg)


async def _transcribe_segment(
    agent: Any, ctx: Any, segment: Any, transcriber: Any
) -> tuple[str, BaseException | None]:
    """读一段录音并转写。对应 Go ``transcribeAudioSegment``。"""
    media_id = str(getattr(segment, "media_id", "") or "").strip()
    try:
        data, meta = await agent.media.read_limited(ctx, media_id, _byte_limit(agent))
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：失败原样回传
        return "", exc
    mime_type = str(getattr(segment, "mime_type", "") or "").strip()
    if meta is not None and str(getattr(meta, "mime_type", "") or "").strip():
        mime_type = meta.mime_type
    try:
        result = transcriber.transcribe(
            ctx,
            AudioTranscriptionRequest(
                media_id=media_id,
                data=data,
                name=str(getattr(segment, "name", "") or ""),
                mime_type=mime_type,
            ),
        )
        if hasattr(result, "__await__"):
            result = await result
    except BaseException as exc:  # noqa: BLE001
        return "", exc
    return str(getattr(result, "text", "") or "").strip(), None


def is_transcribable_audio_segment(segment: Any) -> bool:
    """是否是"可转写的语音"。对应 Go ``isTranscribableAudioSegment``。

    只有语音编解码扩展名（``.amr`` / ``.silk`` / ``.spx``）才作为兜底：一个普通上传的
    ``.mp3``/``.wav`` 文件不会被转写，除非适配器标了 audio MIME 或语音标签。
    """
    if getattr(segment, "type", "") != SegmentFile:
        return False
    mime_type = str(getattr(segment, "mime_type", "") or "").strip().lower()
    if mime_type.startswith("audio/"):
        return True
    label = str(getattr(segment, "text", "") or "").strip().lower()
    if label in ("语音", "音频", "voice", "audio", "record"):
        return True
    name = str(getattr(segment, "name", "") or "").strip().replace("\\", "/")
    extension = posixpath.splitext(name)[1].lower()
    return extension in (".amr", ".silk", ".spx")


def asr_transcription_text(index: int, text: str) -> str:
    """给自动转写打标（命令惰性：以 ``[`` 开头）。对应 Go ``asrTranscriptionText``。"""
    body = str(text or "").strip()
    if not body:
        return ""
    return "[语音 %d 自动转写（可能有误）：%s]" % (index, body)


class AsrMixin:
    """Go ``*Agent`` 的入站语音方法组。"""

    async def transcribe_platform_audio(self, ctx: Any) -> Any:
        return await transcribe_platform_audio(self, ctx)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _transcriber(agent: Any) -> Any:
    value = getattr(agent, "asr", None)
    if value is None:
        value = getattr(agent, "audio_transcriber", None)
    return value


def _selection(agent: Any) -> Any:
    return getattr(agent, "asr_selection", None)


def _parallelism_limit(agent: Any) -> int:
    value = int(getattr(agent, "asr_parallelism", 0) or 0)
    return value if value > 0 else DEFAULT_ASR_PARALLELISM


def _segment_limit(agent: Any) -> int:
    value = int(getattr(agent, "asr_max_segments", 0) or 0)
    return value if value > 0 else DEFAULT_ASR_MAX_SEGMENTS


def _byte_limit(agent: Any) -> int:
    value = int(getattr(agent, "asr_max_audio_bytes", 0) or 0)
    return value if value > 0 else DEFAULT_ASR_MAX_AUDIO_BYTES


def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)


def _turn_request_id(ctx: Any) -> str:
    try:
        from atri_x.agent.request_context import turn_request_id_from_context

        return turn_request_id_from_context(ctx)
    except Exception:  # noqa: BLE001 - agent-core 尚未落地
        return ""


def _authorize_model(agent: Any, ctx: Any, selection: Any) -> BaseException | None:
    helper = getattr(agent, "authorize_execution_model_selection", None)
    if helper is None:
        return None
    try:
        helper(ctx, selection)
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


def _reserve_batch(
    agent: Any, ctx: Any, kind: str, requests: list[dict[str, str]]
) -> tuple[bool, str]:
    helper = getattr(agent, "reserve_budget_batch_requests", None)
    if helper is None:
        return True, ""
    try:
        from atri_x.agent.budget import BudgetReservationRequest
    except Exception:  # noqa: BLE001
        return helper(ctx, kind, requests)
    payload = [
        BudgetReservationRequest(call_id=item["call_id"], digest=item["digest"])
        for item in requests
    ]
    return helper(ctx, kind, payload)


def _text_segment(body: str) -> Any:
    from atri_x.platform import MessageSegment

    return MessageSegment(type=SegmentText, text=body)


def _audit(agent: Any, event: str, **attributes: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in attributes.items():
        args.append(key)
        args.append(value)
    audit(*args)
