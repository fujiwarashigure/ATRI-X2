"""LLM 抽象层类型。

对应 Go：``internal/llm/llm.go``。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence, runtime_checkable


class Role:
    """对应 Go ``MessageRole``。用字符串常量而非 Enum，便于直接落 JSON。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    ALL = (SYSTEM, USER, ASSISTANT, TOOL)


# 历史别名：Go 侧变量名为 RoleSystem / RoleUser / RoleAssistant / RoleTool
RoleSystem = Role.SYSTEM
RoleUser = Role.USER
RoleAssistant = Role.ASSISTANT
RoleTool = Role.TOOL


class SegmentType:
    """对应 Go ``MessageSegmentType``。"""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"

    ALL = (TEXT, IMAGE, FILE)


SegmentText = SegmentType.TEXT
SegmentImage = SegmentType.IMAGE
SegmentFile = SegmentType.FILE


@dataclass
class MessageSegment:
    """一条消息里的一段带类型内容。对应 Go ``MessageSegment``。

    JSON 键与 Go 的 struct tag 完全一致（``type`` / ``text`` / ``url`` /
    ``media`` / ``mime_type`` / ``name``），以便与 Go 版落库数据互通。

    ``detail`` 是 Python 侧新增字段（Go 版没有）：图片段的视觉细节档位，
    取值是供应商线上的 ``detail``（``"low"`` / ``"high"`` / ``"auto"`` /
    ``"original"``）。空串表示**不写这个键**，因此旧数据与 Go 落库数据保持
    逐字节兼容；``view_image`` 与视觉服务默认写 ``"original"``，让上游按原图
    尺寸理解画面，而不是默认被降采样成 ``low``。
    """

    type: str = SegmentType.TEXT
    text: str = ""
    url: str = ""
    media: str = ""
    mime_type: str = ""
    name: str = ""
    #: 图片段的视觉细节档位；空串表示不输出该键（与 Go 落库键兼容）。
    detail: str = ""

    # -- wire ---------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.text:
            out["text"] = self.text
        if self.url:
            out["url"] = self.url
        if self.media:
            out["media"] = self.media
        if self.mime_type:
            out["mime_type"] = self.mime_type
        if self.name:
            out["name"] = self.name
        # 空值不输出：旧库里的段没有这个键，写出去会多出一堆无意义字段。
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "MessageSegment":
        if raw is None:
            return cls()
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            return cls(type=SegmentType.TEXT, text=raw)
        if not isinstance(raw, dict):
            return cls(type=SegmentType.TEXT, text=str(raw))
        return cls(
            type=str(raw.get("type") or SegmentType.TEXT),
            text=str(raw.get("text") or ""),
            url=str(raw.get("url") or ""),
            media=str(raw.get("media") or ""),
            mime_type=str(raw.get("mime_type") or ""),
            name=str(raw.get("name") or ""),
            detail=str(raw.get("detail") or ""),
        )

    def clone(self) -> "MessageSegment":
        return dataclasses.replace(self)

    def __bool__(self) -> bool:  # 空的纯文本段视为假
        return bool(self.text or self.url or self.media or self.name or self.mime_type)


@dataclass
class ToolCallRequest:
    """模型想要发起的一次工具调用。对应 Go ``ToolCallRequest``。"""

    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class LLMMessage:
    """对话中的一条消息。对应 Go ``LLMMessage``。

    ``reasoning_content`` 是 Python 侧新增字段（Go 版没有）：思考型模型在
    ``assistant`` 消息里除了 ``content`` 还会返回思维链，**携带 tools 的请求必须
    在后续每一轮把历史 assistant 消息的 ``reasoning_content`` 原样回传**，否则
    上游会以 ``invalid_request_error``（HTTP 400）拒收。该字段不落用户可见文本，
    只参与请求序列化。
    """

    role: str = Role.USER
    segments: list[MessageSegment] = field(default_factory=list)
    name: str = ""
    tool_call_id: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    reasoning_content: str = ""

    def content_text(self) -> str:
        return segments_text_only(self.segments)


@dataclass
class ToolFunctionSchema:
    """对应 Go ``ToolFunctionSchema``。"""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSchema:
    """对应 Go ``ToolSchema``。"""

    type: str = "function"
    function: ToolFunctionSchema = field(default_factory=ToolFunctionSchema)


@dataclass
class ChatRequest:
    """一次 LLM 对话调用的输入。对应 Go ``ChatRequest``。"""

    model: str = ""
    session_id: str = ""
    #: Python 侧新增：本次调用实际使用的 provider 名。请求体构造需要它判断
    #: 是否写思考模式的 ``reasoning_content`` 扩展字段（DeepSeek 系要求，
    #: 其它厂商不认这个字段）。Go 版 ChatRequest 没有该字段。
    provider: str = ""
    messages: list[LLMMessage] = field(default_factory=list)
    tools: list[ToolSchema] = field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 0
    # 供应商特有字段；会覆盖 provider/model 的 extra_payload，但 model /
    # messages / stream / stream_options 这些协议保留字段由适配器独占，不可替换。
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallDelta:
    """流式响应里的一段工具调用增量。对应 Go ``ToolCallDelta``。"""

    index: int = 0
    id: str = ""
    name: str = ""
    args: str = ""


@dataclass
class Usage:
    """token 用量。对应 Go ``Usage``。

    ``cache_hit_tokens`` 的 0 有两种含义，靠 ``cache_hit_reported`` 区分：
    上游响应里根本没有缓存命中字段（``False``，此时 0 只是"没上报"，不等于"没命中"），
    或上游明确上报了 0（``True``）。默认 ``True`` 保持旧行为：手工构造的 usage
    一律按"0 就是 0"解释，只有线上解析器才会把它标成未上报。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    #: 上游是否上报过缓存命中字段（值可以是 0）。
    cache_hit_reported: bool = True


@dataclass
class StreamChunk:
    """流式响应的一块。对应 Go ``StreamChunk``。"""

    delta_content: str = ""
    delta_reasoning_content: str = ""
    tool_call_deltas: list[ToolCallDelta] = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage | None = None
    error: BaseException | None = None


@dataclass
class ChatResponse:
    """非流式对话调用的完整响应。对应 Go ``ChatResponse``。"""

    message: LLMMessage = field(default_factory=LLMMessage)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: Usage | None = None


@dataclass
class ModelMetadata:
    """对应 Go ``ModelMetadata``。"""

    id: str = ""
    context_window: int = 0


@dataclass
class RetryEvent:
    """对应 Go ``RetryEvent``。``delay`` 单位为秒（Go 为 ``time.Duration``）。"""

    attempt: int = 0
    max_retries: int = 0
    delay: float = 0.0
    err: BaseException | None = None


@runtime_checkable
class LLM(Protocol):
    """语言模型适配器接口。对应 Go ``LLM``。

    ``chat_stream`` 返回一个 ``StreamChunk`` 的异步迭代器。
    """

    def chat_stream(self, ctx: Any, req: ChatRequest) -> Any: ...

    async def list_models(self, ctx: Any) -> list[str]: ...


@runtime_checkable
class ModelMetadataProvider(Protocol):
    """对应 Go ``ModelMetadataProvider``。"""

    async def list_model_metadata(self, ctx: Any) -> list[ModelMetadata]: ...


@runtime_checkable
class RetryNotifier(Protocol):
    """对应 Go ``RetryNotifier``。"""

    def set_retry_notifier(self, fn: Callable[[Any, RetryEvent], Any] | None) -> None: ...


# ---------------------------------------------------------------------------
# 以下函数逐一对齐 internal/llm/segment.go
# ---------------------------------------------------------------------------

def clone_messages(messages: Sequence[LLMMessage] | None) -> list[LLMMessage] | None:
    """对应 Go ``CloneMessages``：深拷贝到 monolayer（segments / tool_calls 各复制一层）。"""
    if messages is None:
        return None
    return [
        LLMMessage(
            role=m.role,
            segments=[s.clone() for s in m.segments],
            name=m.name,
            tool_call_id=m.tool_call_id,
            tool_calls=[dataclasses.replace(tc) for tc in m.tool_calls],
            reasoning_content=m.reasoning_content,
        )
        for m in messages
    ]


def text_segments(text: str) -> list[MessageSegment]:
    """对应 Go ``TextSegments``。"""
    if not text:
        return []
    return [MessageSegment(type=SegmentType.TEXT, text=text)]


def set_segment_text(segments: Sequence[MessageSegment], text: str) -> list[MessageSegment]:
    """对应 Go ``SetSegmentText``：替换全部文本内容，保留非文本段。"""
    out: list[MessageSegment] = []
    inserted = False
    for segment in segments:
        if segment.type != SegmentType.TEXT:
            out.append(segment.clone())
            continue
        if not inserted and text:
            seg = segment.clone()
            seg.text = text
            out.append(seg)
            inserted = True
    if not inserted and text:
        out.insert(0, MessageSegment(type=SegmentType.TEXT, text=text))
    return out


def segments_text_only(segments: Sequence[MessageSegment]) -> str:
    """对应 Go ``SegmentsTextOnly``：只取文本段并 strip。"""
    return "".join(s.text for s in segments if s.type == SegmentType.TEXT).strip()


def segments_content_text(segments: Sequence[MessageSegment]) -> str:
    """对应 Go ``SegmentsContentText``：给存储/日志/摘要用的可读纯文本投影。"""
    parts: list[str] = []
    image_index = 0
    for segment in segments:
        if segment.type == SegmentType.TEXT:
            if segment.text:
                parts.append(segment.text)
        elif segment.type == SegmentType.IMAGE:
            if segment.url.strip() or segment.media:
                image_index += 1
                ref = image_reference_text(segment, image_index)
                if ref:
                    parts.append(ref)
            else:
                parts.append(
                    _segment_label("图片", "", segment.name, segment.text, segment.mime_type)
                )
        elif segment.type == SegmentType.FILE:
            source = _display_segment_url(segment.url)
            if segment.media:
                source = segment.media
            parts.append(
                _segment_label("文件", source, segment.name, segment.text, segment.mime_type)
            )
    return " ".join(p for p in parts if p).strip()


def image_reference_text(segment: MessageSegment, index: int) -> str:
    """对应 Go ``ImageReferenceText``：请求里与一张图片配对的文本标签。"""
    if segment.type != SegmentType.IMAGE or index < 1:
        return ""
    if not segment.url.strip() and not segment.media:
        return ""
    parts = [f"图片 {index}"]
    name = segment.name.strip()
    if name:
        parts.append("名称：" + name)
    if segment.media:
        parts.append("媒体 ID：" + segment.media)
    elif _reusable_image_url(segment.url)[1]:
        parts.append("引用 URL：" + _reusable_image_url(segment.url)[0])
    elif segment.url.strip().lower().startswith("data:"):
        parts.append("内嵌图片，无可复用 URL")
    else:
        parts.append("无可复用 URL")
    return "[" + "；".join(parts) + "]"


def _reusable_image_url(value: str) -> tuple[str, bool]:
    value = (value or "").strip()
    if not value:
        return "", False
    try:
        from urllib.parse import urlparse

        u = urlparse(value)
    except ValueError:
        return "", False
    if not u.hostname or u.scheme.lower() not in ("http", "https"):
        return "", False
    return value, True


def _display_segment_url(value: str) -> str:
    if (value or "").strip().lower().startswith("data:"):
        return ""
    return value or ""


def prepend_segment_text(segments: Sequence[MessageSegment], prefix: str) -> list[MessageSegment]:
    """对应 Go ``PrependSegmentText``（返回新列表，就地改第一个文本段）。"""
    if not prefix:
        return list(segments)
    out = [s.clone() for s in segments]
    for seg in out:
        if seg.type == SegmentType.TEXT:
            seg.text = prefix + seg.text
            return out
    out.insert(0, MessageSegment(type=SegmentType.TEXT, text=prefix))
    return out


def append_segment_text(segments: Sequence[MessageSegment], suffix: str) -> list[MessageSegment]:
    """对应 Go ``AppendSegmentText``。"""
    if not suffix:
        return list(segments)
    out = [s.clone() for s in segments]
    for seg in reversed(out):
        if seg.type == SegmentType.TEXT:
            seg.text += suffix
            return out
    out.append(MessageSegment(type=SegmentType.TEXT, text=suffix))
    return out


def replace_segment_text(
    segments: Sequence[MessageSegment],
    pattern: Any,
    replacement: str,
    all_: bool = False,
) -> list[MessageSegment]:
    """对应 Go ``ReplaceSegmentText``。``pattern`` 为 ``re.Pattern``。"""
    if pattern is None:
        return list(segments)
    out = [s.clone() for s in segments]
    for seg in out:
        if seg.type != SegmentType.TEXT:
            continue
        if all_:
            seg.text = pattern.sub(replacement, seg.text)
            continue
        match = pattern.search(seg.text)
        if match is None:
            continue
        seg.text = (
            seg.text[: match.start()]
            + pattern.sub(replacement, seg.text[match.start(): match.end()])
            + seg.text[match.end():]
        )
        return out
    return out


def image_segments(segments: Sequence[MessageSegment]) -> list[MessageSegment]:
    """对应 Go ``ImageSegments``。"""
    return [s for s in segments if s.type == SegmentType.IMAGE]


def file_segments(segments: Sequence[MessageSegment]) -> list[MessageSegment]:
    """对应 Go ``FileSegments``。"""
    return [s for s in segments if s.type == SegmentType.FILE]


def has_image_segment(segments: Sequence[MessageSegment]) -> bool:
    """对应 Go ``HasImageSegment``。"""
    return any(s.type == SegmentType.IMAGE for s in segments)


def messages_have_image_segment(messages: Sequence[LLMMessage]) -> bool:
    """对应 Go ``MessagesHaveImageSegment``。"""
    return any(has_image_segment(m.segments) for m in messages)


def latest_user_segments(messages: Sequence[LLMMessage]) -> list[MessageSegment]:
    """对应 Go ``LatestUserSegments``。"""
    for m in reversed(list(messages)):
        if m.role == Role.USER:
            return m.segments
    return []


def latest_user_segment_text_only(messages: Sequence[LLMMessage]) -> str:
    return segments_text_only(latest_user_segments(messages))


def latest_user_segment_content_text(messages: Sequence[LLMMessage]) -> str:
    return segments_content_text(latest_user_segments(messages))


def set_latest_user_segments(
    messages: Sequence[LLMMessage], segments: Sequence[MessageSegment]
) -> list[LLMMessage]:
    """对应 Go ``SetLatestUserSegments``。"""
    out = _shallow_copy_messages(messages)
    for m in reversed(out):
        if m.role == Role.USER:
            m.segments = [s.clone() for s in segments]
            return out
    return out


def prepend_latest_user_segment_text(messages: Sequence[LLMMessage], prefix: str) -> list[LLMMessage]:
    return _edit_latest_user_segments(
        messages, lambda segs: prepend_segment_text(segs, prefix)
    )


def append_latest_user_segment_text(messages: Sequence[LLMMessage], suffix: str) -> list[LLMMessage]:
    return _edit_latest_user_segments(
        messages, lambda segs: append_segment_text(segs, suffix)
    )


def replace_latest_user_segment_text(
    messages: Sequence[LLMMessage], pattern: Any, replacement: str, all_: bool = False
) -> list[LLMMessage]:
    return _edit_latest_user_segments(
        messages, lambda segs: replace_segment_text(segs, pattern, replacement, all_)
    )


def append_system_segment_text(messages: Sequence[LLMMessage], text: str) -> list[LLMMessage]:
    """对应 Go ``AppendSystemSegmentText``。"""
    text = (text or "").strip()
    if not text:
        return list(messages)
    out = _shallow_copy_messages(messages)
    for m in out:
        if m.role != Role.SYSTEM:
            continue
        if not segments_text_only(m.segments):
            m.segments = text_segments(text)
        else:
            m.segments = append_segment_text(m.segments, "\n\n" + text)
        return out
    out.insert(0, LLMMessage(role=Role.SYSTEM, segments=text_segments(text)))
    return out


def _edit_latest_user_segments(
    messages: Sequence[LLMMessage], edit: Callable[[list[MessageSegment]], list[MessageSegment]]
) -> list[LLMMessage]:
    out = _shallow_copy_messages(messages)
    for m in reversed(out):
        if m.role == Role.USER:
            m.segments = edit(m.segments)
            return out
    return out


def _shallow_copy_messages(messages: Sequence[LLMMessage]) -> list[LLMMessage]:
    """Go 侧 ``append([]LLMMessage(nil), messages...)`` 的等价物：只复制切片头。"""
    return list(messages)


def _segment_label(kind: str, source: str, *values: str) -> str:
    extra = _first_non_empty(*values)
    if extra:
        return f"[{kind}: {extra}]"
    if source:
        return f"[{kind}: {source}]"
    return f"[{kind}]"


def _first_non_empty(*values: str) -> str:
    for value in values:
        if (value or "").strip():
            return value.strip()
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
