"""OpenAI-compatible 线上格式：请求信封构造、响应字段解析与日志摘要。

对应 Go：``internal/llm/openai/openai.go`` 的 const 块、``RequestOptions``、
``toOpenAIMessages`` 一族与 ``chatRequestLogSummary`` 一族。从
:mod:`atri_x.llm.openai.openai` 拆出（单文件 ≤ 600 行的移植约定），
Responses 适配器复用其中的消息转换 helper。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from atri_x.llm.llm import (
    LLMMessage,
    MessageSegment,
    Role,
    SegmentType,
    ToolCallRequest,
    ToolSchema,
    Usage,
    image_reference_text,
    segments_content_text,
)

__all__ = [
    "CHAT_RESERVED_REQUEST_FIELDS",
    "DATA_DONE",
    "DATA_PREFIX",
    "DEFAULT_FIRST_CHUNK_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_INITIAL_DELAY",
    "DEFAULT_STREAM_IDLE_TIMEOUT",
    "MAX_STREAM_LINE_BYTES",
    "STREAM_PREFIX_BYTES",
    "RequestOptions",
    "first_json_int",
    "first_nested_json_int",
    "header_of",
    "is_retryable_status",
    "json_compact",
    "json_int",
    "merge_extra_fields",
    "openai_image_url",
    "openai_tool_content",
    "reasoning_of",
    "resolve_segments",
    "retry_delay",
    "text_of",
    "to_openai_content",
    "to_openai_messages",
    "to_openai_tool_calls",
    "to_openai_tools",
    "to_usage",
    "uses_reasoning_protocol",
    "with_image_references",
]

# 常量逐一对齐 Go 的 const 块。
DEFAULT_FIRST_CHUNK_TIMEOUT = 180.0
DEFAULT_STREAM_IDLE_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_INITIAL_DELAY = 2.0
STREAM_PREFIX_BYTES = 8 * 1024
MAX_STREAM_LINE_BYTES = 8 * 1024 * 1024
DATA_PREFIX = "data: "
DATA_DONE = "data: [DONE]"

#: 适配器独占的 chat-completions 字段（对应 Go ``chatReservedRequestFields``）。
#: 适配器永远在流式、信封必须描述调用方真正发来的 model/messages，因此 extra
#: payload 只能加厂商特有参数，不能替换这些：静默生效会让 hook 关掉 stream
#: （读端随后等一个永远不会来的 SSE body），或背着调用方换 model/messages。
CHAT_RESERVED_REQUEST_FIELDS = frozenset({"model", "messages", "stream", "stream_options"})


@dataclass
class RequestOptions:
    """对应 Go ``RequestOptions``。"""

    first_chunk_timeout: float = 0.0
    stream_idle_timeout: float = 0.0
    max_retries: int = 0
    retry_initial_delay: float = 0.0
    #: 对应 Go ``OnRetry func(context.Context, RetryEvent)``。
    on_retry: Callable[[Any, Any], Any] | None = None
    proxy: str = ""

    def with_defaults(self) -> "RequestOptions":
        """对应 Go ``withDefaults``：非正数一律回落到默认值。

        注意 ``max_retries <= 0`` 会变成 3，也就是说**无法**用 0 表示「不重试」，
        与 Go 一致。
        """
        if self.first_chunk_timeout <= 0:
            self.first_chunk_timeout = DEFAULT_FIRST_CHUNK_TIMEOUT
        if self.stream_idle_timeout <= 0:
            self.stream_idle_timeout = DEFAULT_STREAM_IDLE_TIMEOUT
        if self.max_retries <= 0:
            self.max_retries = DEFAULT_MAX_RETRIES
        if self.retry_initial_delay <= 0:
            self.retry_initial_delay = DEFAULT_RETRY_INITIAL_DELAY
        return self


# ---------------------------------------------------------------------------
# 小工具（对应 Go openai.go 的同名 helper）
# ---------------------------------------------------------------------------

def is_retryable_status(status: int) -> bool:
    """对应 Go ``isRetryableStatus``：408 / 409 / 425 / 429 / >=500。"""
    return status in (408, 409, 425, 429) or status >= 500


def retry_delay(initial: float, attempt: int) -> float:
    """对应 Go ``retryDelay``：``initial << attempt``（秒）。"""
    return float(initial) * (2 ** max(0, attempt))


def json_int(value: Any) -> int:
    """对应 Go ``jsonInt``：只认数字类型（bool 走 default → 0）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def first_json_int(raw: Mapping[str, Any], *names: str) -> int:
    """对应 Go ``firstJSONInt``：取第一个 > 0 的值。"""
    for name in names:
        value = json_int(raw.get(name))
        if value > 0:
            return value
    return 0


def first_nested_json_int(raw: Mapping[str, Any], *paths: Sequence[str]) -> int:
    """对应 Go ``firstNestedJSONInt``。"""
    for path in paths:
        current: Any = raw
        for key in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        value = json_int(current)
        if value > 0:
            return value
    return 0


def to_usage(raw: Mapping[str, Any]) -> Usage:
    """对应 Go ``openAIUsage.UnmarshalJSON`` + ``toUsage``（含缓存命中别名）。"""
    cache_hit = first_json_int(
        raw,
        "prompt_cache_hit_tokens",
        "cache_hit_tokens",
        "cached_tokens",
        "input_cache_hit_tokens",
        "prompt_cache_read_tokens",
        "cache_read_input_tokens",
    )
    if cache_hit == 0:
        cache_hit = first_nested_json_int(
            raw,
            ("prompt_tokens_details", "cached_tokens"),
            ("prompt_tokens_details", "cache_hit_tokens"),
            ("input_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cache_hit_tokens"),
        )
    return Usage(
        prompt_tokens=first_json_int(raw, "prompt_tokens", "input_tokens"),
        completion_tokens=first_json_int(raw, "completion_tokens", "output_tokens"),
        total_tokens=first_json_int(raw, "total_tokens"),
        cache_hit_tokens=cache_hit,
    )


def text_of(value: Any) -> str:
    """JSON 字符串字段的宽松取值（Go 侧类型不符会让整块解析失败）。"""
    return value if isinstance(value, str) else ""


def reasoning_of(delta: Mapping[str, Any]) -> str:
    """``reasoning_content`` 为主；部分网关只发 ``reasoning``，作为别名兜底。"""
    value = delta.get("reasoning_content")
    if not isinstance(value, str):
        value = delta.get("reasoning")
    return value if isinstance(value, str) else ""


def header_of(headers: Mapping[str, str] | None, name: str) -> str:
    """响应头大小写不敏感取值。"""
    target = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == target:
            return value
    return ""


def media_type_of(headers: Mapping[str, str] | None) -> str:
    """Content-Type 的 media type 部分（小写、去参数）。"""
    return header_of(headers, "content-type").split(";", 1)[0].strip().lower()


# ---------------------------------------------------------------------------
# 请求构造（对应 Go 的 request types 一节）
# ---------------------------------------------------------------------------

def resolve_segments(
    segments: Sequence[MessageSegment],
    resolve_image: Callable[[MessageSegment], str] | None = None,
) -> list[MessageSegment]:
    """把 url 为空的图片段交给注入的解析器补全 URL（返回新列表，不改入参）。

    Go 侧这一步由调用方完成（``agent.chat_llm`` 的 ``media.ResolveForLLM``），
    因此不注入解析器时的行为与 Go 完全一致：原样用 ``segment.url``。
    """
    if resolve_image is None:
        return list(segments)
    out: list[MessageSegment] = []
    for segment in segments:
        if segment.type != SegmentType.IMAGE or segment.url.strip():
            out.append(segment)
            continue
        resolved = resolve_image(segment) or ""
        if not resolved:
            out.append(segment)
            continue
        clone = segment.clone()
        clone.url = resolved
        out.append(clone)
    return out


def with_image_references(segments: Sequence[MessageSegment]) -> list[MessageSegment]:
    """对应 Go ``withOpenAIImageReferences``：每张图片前临时插入序号/URL 文本标签。"""
    out: list[MessageSegment] = []
    image_index = 0
    for segment in segments:
        if segment.type == SegmentType.IMAGE:
            reference = image_reference_text(segment, image_index + 1)
            if reference:
                image_index += 1
                out.append(MessageSegment(type=SegmentType.TEXT, text=reference))
        out.append(segment)
    return out


def openai_image_url(segment: MessageSegment) -> dict[str, Any]:
    """把图片段组装成 ``image_url`` 对象（非空 ``detail`` 才带上）。

    ``detail`` 是供应商侧的降采样档位。**不写**这个键时由上游按默认策略
    （通常是 ``auto``/``low``）处理；``view_image`` 与视觉服务因此显式写
    ``"original"``，避免原图被默认降采样成看不清文字与纹样的小图。
    """
    image_url: dict[str, Any] = {"url": segment.url}
    detail = str(getattr(segment, "detail", "") or "").strip()
    if detail:
        image_url["detail"] = detail
    return image_url


def to_openai_content(segments: Sequence[MessageSegment]) -> Any:
    """对应 Go ``toOpenAIContent``：纯文本发字符串，否则发 content parts。

    图片段经 :func:`openai_image_url` 组装，因此 ``detail`` 会原样带到上游。
    """
    if not segments:
        return ""
    if len(segments) == 1 and segments[0].type == SegmentType.TEXT:
        return segments[0].text
    parts: list[dict[str, Any]] = []
    for segment in segments:
        if segment.type == SegmentType.TEXT:
            if segment.text:
                parts.append({"type": "text", "text": segment.text})
        elif segment.type == SegmentType.IMAGE:
            if segment.url:
                parts.append({"type": "image_url", "image_url": openai_image_url(segment)})
        elif segment.type == SegmentType.FILE:
            # 与 Go 一致：厂商文件输入未实现，当前统一作为文本描述发送。
            text = segments_content_text([segment])
            if text:
                parts.append({"type": "text", "text": text})
    return parts or ""


def openai_tool_content(
    segments: Sequence[MessageSegment],
) -> tuple[str, list[MessageSegment]]:
    """对应 Go ``openAIToolContent``：拆出工具结果里的文本与图片。"""
    text_segments: list[MessageSegment] = []
    images: list[MessageSegment] = []
    for segment in segments:
        if segment.type == SegmentType.IMAGE and segment.url != "":
            images.append(segment)
            continue
        text_segments.append(segment)
    content = segments_content_text(text_segments)
    if content == "" and images:
        content = "工具返回了图片，见下一条多模态消息。"
    return content, images


def uses_reasoning_protocol(provider: str, model: str) -> bool:
    """provider/model 是否属于 DeepSeek 系思考模式协议族。

    ``reasoning_content`` 是 DeepSeek 的扩展字段；官方 provider 名是 ``deepseek``，
    第三方中转站通常沿用 ``deepseek-*`` 模型名。只有这类目标才写该字段（含空串），
    避免 OpenAI 等厂商拒收未知字段。
    """
    return "deepseek" in (str(provider or "") + " " + str(model or "")).lower()


def to_openai_tool_calls(calls: Sequence[ToolCallRequest]) -> list[dict[str, Any]]:
    """对应 Go ``toOpenAIToolCalls``。"""
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for call in calls
    ]


def to_openai_tools(tools: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    """对应 Go ``toOpenAITools``。"""
    return [
        {
            "type": tool.type or "function",
            "function": {
                "name": tool.function.name,
                "description": tool.function.description,
                "parameters": tool.function.parameters,
            },
        }
        for tool in tools
    ]


def openai_message(
    message: LLMMessage, content: Any, *, include_reasoning: bool = False
) -> dict[str, Any]:
    """对应 Go ``openAIMessage`` 的 struct tag（空 name/tool_call_id/tool_calls 省略）。

    ``reasoning_content`` 是 DeepSeek 思考模式的扩展字段：携带 ``tools`` 的请求必须
    把历史 assistant 的思维链原样回传，否则上游返回 400。

    * 消息本身带思维链时总是写入（能返回该字段的模型/网关通常也认它）；
    * ``include_reasoning=True``（DeepSeek 系 provider）时，带 ``tool_calls`` 的
      assistant 消息即使思维链为空也写 ``""``：思考模式的上游按「字段必须存在」
      校验，缺字段同样会 400。其它厂商不写，避免它们拒收未知字段。
    """
    out: dict[str, Any] = {"role": message.role, "content": content}
    if message.name:
        out["name"] = message.name
    if message.tool_call_id:
        out["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        out["tool_calls"] = to_openai_tool_calls(message.tool_calls)
    if message.role == Role.ASSISTANT:
        if message.reasoning_content:
            out["reasoning_content"] = message.reasoning_content
        elif include_reasoning and message.tool_calls:
            out["reasoning_content"] = ""
    return out


def to_openai_messages(
    messages: Sequence[LLMMessage],
    resolve_image: Callable[[MessageSegment], str] | None = None,
    *,
    include_reasoning: bool = False,
) -> list[dict[str, Any]]:
    """对应 Go ``toOpenAIMessages``。

    工具图片派生规则（源于架构文档）：同一批工具图片在**所有 tool message
    之后**派生为**一条** user 多模态消息，其中每张图片前插入带序号与可复用
    URL 的文本标签；这些派生内容不持久化。

    ``include_reasoning`` 见 :func:`openai_message`：DeepSeek 系 provider 传 True。
    """
    out: list[dict[str, Any]] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        segments = resolve_segments(message.segments, resolve_image)
        if message.role != Role.TOOL:
            out.append(
                openai_message(
                    message,
                    to_openai_content(with_image_references(segments)),
                    include_reasoning=include_reasoning,
                )
            )
            index += 1
            continue

        carrier: list[MessageSegment] = []
        while index < total and messages[index].role == Role.TOOL:
            tool_message = messages[index]
            content, images = openai_tool_content(
                resolve_segments(tool_message.segments, resolve_image)
            )
            out.append(
                openai_message(
                    tool_message, content, include_reasoning=include_reasoning
                )
            )
            if images:
                carrier.append(
                    MessageSegment(
                        type=SegmentType.TEXT,
                        text=f"以下图片来自工具 {tool_message.name}"
                        f"（tool_call_id: {tool_message.tool_call_id}）：",
                    )
                )
                carrier.extend(with_image_references(images))
            index += 1
        if carrier:
            out.append({"role": Role.USER, "content": to_openai_content(carrier)})
    return out


def merge_extra_fields(
    body: dict[str, Any], extra: Mapping[str, Any] | None, reserved_fields: Any
) -> list[str]:
    """对应 Go ``mergeExtraFields``：合并 extra，返回被拒绝的保留字段名。"""
    if not extra:
        return []
    dropped: list[str] = []
    for key, value in extra.items():
        if str(key).strip().lower() in reserved_fields:
            dropped.append(key)
            continue
        body[key] = value
    return dropped


# ---------------------------------------------------------------------------
# 日志摘要（对应 Go 的 logRequest / logFirstSystemMessage）
# ---------------------------------------------------------------------------

def first_system_text(messages: Sequence[LLMMessage]) -> str:
    for message in messages:
        if message.role == Role.SYSTEM:
            return segments_content_text(message.segments)
    return ""


def redact_data_url(value: str) -> str:
    """data: URL 只留前缀（请求本身仍发完整内容）。"""
    value = value.strip()
    comma = value.find(",")
    if comma >= 0:
        return value[:comma] + ",…"
    if len(value) > 128:
        return value[:128] + "…"
    return value


def log_safe_messages(
    messages: Sequence[LLMMessage], resolve_image: Callable[[MessageSegment], str] | None
) -> list[LLMMessage]:
    """克隆一批消息并把 data: URL 脱敏，供日志摘要使用。

    **必须克隆**：脱敏会原地改 ``segment.url``，若直接改调用方的段，真正发出去的
    请求体里 data: URL 就只剩前缀了（对应 Go 的 ``llm.CloneMessages``）。
    """
    out: list[LLMMessage] = []
    for message in messages:
        segments = [
            segment.clone()
            for segment in resolve_segments(message.segments, resolve_image)
        ]
        for segment in segments:
            if segment.url.strip().lower().startswith("data:"):
                segment.url = redact_data_url(segment.url)
        out.append(
            LLMMessage(
                role=message.role,
                segments=segments,
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=list(message.tool_calls),
            )
        )
    return out


def latest_message_json(
    messages: Sequence[LLMMessage],
    resolve_image: Callable[[MessageSegment], str] | None = None,
) -> str:
    """对应 Go ``latestMessageJSON``。"""
    if not messages:
        return ""
    converted = to_openai_messages(log_safe_messages(messages[-1:], resolve_image))
    return json_compact(converted[0]) if converted else ""


def first_system_message_json(
    messages: Sequence[LLMMessage],
    resolve_image: Callable[[MessageSegment], str] | None = None,
) -> str:
    """对应 Go ``firstSystemMessageJSON``。"""
    for message in messages:
        if message.role != Role.SYSTEM:
            continue
        converted = to_openai_messages(log_safe_messages([message], resolve_image))
        return json_compact(converted[0]) if converted else ""
    return ""


def json_compact(value: Any) -> str:
    """对应 Go ``marshalJSONNoEscape``：不转义 HTML/非 ASCII，紧凑输出。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def hash_json(value: Any) -> str:
    return hash_bytes(json_compact(value).encode("utf-8"))


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def hash_bytes(data: bytes) -> str:
    """对应 Go ``hashBytes``：``sha256`` 前 8 字节的十六进制。"""
    return hashlib.sha256(data).hexdigest()[:16]


class RequestLog:
    """请求日志摘要。对应 Go ``Adapter.logRequest`` / ``logFirstSystemMessage`` /
    ``logDroppedExtraFields``。

    只记字段名与摘要，绝不记凭据或完整 body：``Authorization``、api key 永远
    不进日志，``data:`` URL 只留前缀（``log_safe_messages`` 会先克隆再脱敏，
    因此真正发出的请求体仍是完整内容）。
    """

    def __init__(self, logger: Any = None, resolve_image: Any = None) -> None:
        self.logger = logger
        self.resolve_image = resolve_image
        self.logged_system: set[str] = set()

    def set_logger(self, logger: Any) -> None:
        self.logger = logger

    def log_dropped_extra_fields(self, fields: Sequence[str]) -> None:
        """只记被忽略的字段名，排序去重，绝不记值。"""
        if self.logger is None or not fields:
            return
        self.logger.warning(
            "openai extra payload tried to override reserved request fields; ignored fields=%s",
            ",".join(sorted(set(fields))),
        )

    def log_request(
        self, message: str, endpoint: str, req: Any, body: Mapping[str, Any]
    ) -> None:
        """对应 Go ``logRequest``：请求摘要 + 首个 system prompt（每会话一次）。"""
        if self.logger is None:
            return
        self.log_first_system_message(req)
        roles = [
            f"{item.role}:{item.name}" if item.name else str(item.role)
            for item in req.messages
        ]
        attrs = {
            "endpoint": endpoint,
            "model": req.model,
            "session_id": req.session_id,
            "latest_message_json": latest_message_json(req.messages, self.resolve_image),
            "message_count": len(req.messages),
            "message_roles": ",".join(roles),
            "latest_message": roles[-1] if roles else "",
            "tool_count": len(req.tools),
            "system_hash": hash_text(first_system_text(req.messages)),
            # Go 的 ``req.Tools`` 是可直接 json.Marshal 的结构体；Python 侧是
            # :class:`ToolSchema` 数据类，必须先转成线上 dict，否则 json.dumps 会抛
            # ``Object of type ToolSchema is not JSON serializable``。
            "tools_hash": hash_json(to_openai_tools(req.tools)),
            "body_hash": hash_bytes(json_compact(body).encode("utf-8")),
        }
        self.logger.debug("%s %s", message, " ".join(f"{k}={v}" for k, v in attrs.items()))

    def log_first_system_message(self, req: Any) -> None:
        if not req.session_id or first_system_text(req.messages) == "":
            return
        if req.session_id in self.logged_system:
            return
        self.logged_system.add(req.session_id)
        self.logger.info(
            "system prompt event=system_message session_id=%s model=%s first_system_message_json=%s",
            req.session_id,
            req.model,
            first_system_message_json(req.messages, self.resolve_image),
        )
