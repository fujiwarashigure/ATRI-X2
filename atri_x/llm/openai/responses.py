"""OpenAI Responses 协议适配器。

对应 Go：``internal/llm/openai/responses.go``（``ResponsesAdapter``）。

与 Go 的内嵌结构一致：:class:`ResponsesAdapter` 继承 :class:`OpenAIClient`，
复用它的重试、``/models``、SSE 行扫描（:class:`StreamLineReader`）与
``parse_error`` 错误分类，**只替换请求信封与流式事件翻译**：

* 请求用 ``input`` 而不是 ``messages``，system 段合并为 ``instructions``，
  固定 ``store=false``（ATRI-X 每轮重放完整历史、从不读回已存响应，服务端保留
  没有意义），工具定义扁平化到 ``type`` 旁边；
* 流式事件是命名事件（``response.output_text.delta`` 等），翻译成与 chat
  适配器同一套 ``StreamChunk``；
* ``ProtocolCapabilitiesFor`` 报告 ``responses`` 但**服务端能力全 False**：它是
  协议翻译层，不虚报 ``previous_response_id`` / ``additional_tools`` 那类能力。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from atri_x.llm.errors import APIError, classify, stringify_error_field
from atri_x.llm.llm import (
    ChatRequest,
    LLMMessage,
    MessageSegment,
    Role,
    SegmentType,
    StreamChunk,
    ToolCallDelta,
    ToolSchema,
    segments_content_text,
)
from atri_x.net.httpclient import HTTPError

from atri_x.llm.openai.errors import read_error, response_summary
from atri_x.llm.openai.openai import OpenAIClient
from atri_x.llm.openai.stream import StreamLineReader
from atri_x.llm.openai.wire import (
    DATA_DONE,
    DATA_PREFIX,
    json_int,
    merge_extra_fields,
    openai_tool_content,
    resolve_segments,
    text_of,
    to_usage,
    with_image_references,
)

__all__ = [
    "RESPONSES_RESERVED_REQUEST_FIELDS",
    "ResponsesAdapter",
    "OpenAIResponsesClient",
    "responses_content_message",
    "to_responses_content",
    "to_responses_input",
    "to_responses_tools",
]

#: 适配器独占的 Responses 字段（对应 Go ``responsesReservedRequestFields``），
#: 理由与 chat 侧相同：extra payload 可以加厂商参数，但不能关掉流式或背着调用方
#: 换 model/input。
#:
#: ``store`` **刻意不保留**：适配器自己发 ``store=false``（ATRI-X 每轮重放完整
#: 历史、从不读回已存响应，服务端保留没有意义），但需要别的值的网关仍可通过
#: extra_payload 覆盖它。
RESPONSES_RESERVED_REQUEST_FIELDS = frozenset(
    {"model", "input", "instructions", "stream", "tools"}
)


class ResponsesAdapter(OpenAIClient):
    """Responses 协议适配器。对应 Go ``ResponsesAdapter``（内嵌 ``Adapter``）。"""

    reserved_request_fields = RESPONSES_RESERVED_REQUEST_FIELDS
    request_log_message = "openai responses request"

    # -- 端点与能力 ---------------------------------------------------------
    def endpoint(self) -> str:
        """对应 Go ``ResponsesAdapter.endpoint``。"""
        return f"{self.base_url}/responses"

    def protocol_capabilities_for(self, model: str) -> Any:
        """报告本适配器**实际做的事**，而不是 Responses API 理论能力。

        对应 Go ``ResponsesAdapter.ProtocolCapabilitiesFor``：协议是
        ``responses``，但固定 ``store=false``、每轮重放完整历史、从不发
        ``previous_response_id`` / ``additional_tools``，因此四项服务端能力
        全为 False——虚报会让上层按不存在的服务端状态设计。
        """
        from atri_x.llm.protocol import Protocol, ProtocolCapabilities

        return ProtocolCapabilities(protocol=Protocol.RESPONSES)

    # -- 请求信封 -----------------------------------------------------------
    def build_body(self, req: ChatRequest, *, stream: bool = True) -> dict[str, Any]:
        """构造 Responses 请求信封（对应 Go ``ResponsesAdapter.ChatStream`` 的 body）。"""
        instructions, input_items = to_responses_input(req.messages, self.resolve_image)
        body: dict[str, Any] = {
            "model": req.model,
            "input": input_items,
            "stream": True,
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if req.temperature > 0:
            body["temperature"] = req.temperature
        if req.max_tokens > 0:
            body["max_output_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = to_responses_tools(req.tools)

        reserved = self.reserved_request_fields
        dropped = merge_extra_fields(body, self.extra_payload, reserved)
        dropped += merge_extra_fields(body, self.model_extra_payloads.get(req.model), reserved)
        dropped += merge_extra_fields(body, req.extra_body, reserved)
        self._log_dropped_extra_fields(dropped)
        return body

    async def chat(self, ctx: Any, req: ChatRequest) -> Any:
        """Responses 适配器没有非流式实现（Go 侧同样只有 ``ChatStream``）。"""
        raise NotImplementedError(
            "Responses 适配器只实现流式路径：用 chat_stream()，"
            "非流式请改用 OpenAIClient（chat_completions 协议）"
        )

    # -- 流式事件翻译 -------------------------------------------------------
    async def _consume_stream(self, reader: StreamLineReader) -> AsyncIterator[StreamChunk]:
        """把 Responses 命名事件翻译成 ``StreamChunk``（对应 Go ``readResponsesStream``）。"""
        calls: dict[int, dict[str, str]] = {}
        order: list[int] = []
        seen_terminal = False
        async for line in reader:
            if line == "" or line.startswith(":"):
                continue
            # Responses 流由 response.completed 结束，但前置网关有时仍会补一个
            # chat-completions 的 [DONE]。
            if line == DATA_DONE:
                return
            if not line.startswith(DATA_PREFIX):
                # 命名事件行（event: <type>）本身不带载荷。
                continue
            try:
                event = json.loads(line[len(DATA_PREFIX) :])
            except ValueError as exc:
                yield StreamChunk(error=HTTPError(f"parse stream chunk: {exc}", cause=exc))
                return
            if not isinstance(event, dict):
                yield StreamChunk(error=HTTPError("parse stream chunk: not a JSON object"))
                return

            event_type = text_of(event.get("type"))
            if event_type == "response.output_text.delta":
                delta = text_of(event.get("delta"))
                if delta:
                    yield StreamChunk(delta_content=delta)
            elif event_type in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            ):
                delta = text_of(event.get("delta"))
                if delta:
                    yield StreamChunk(delta_reasoning_content=delta)
            elif event_type in ("response.output_item.added", "response.output_item.done"):
                _accumulate_call(calls, order, event)
            elif event_type == "response.function_call_arguments.delta":
                _accumulate_arguments(
                    calls, order, json_int(event.get("output_index")), text_of(event.get("delta")), False
                )
            elif event_type == "response.function_call_arguments.done":
                # done 事件重复完整参数：替换而不是追加。
                _accumulate_arguments(
                    calls,
                    order,
                    json_int(event.get("output_index")),
                    text_of(event.get("arguments")),
                    True,
                )
            elif event_type in ("response.completed", "response.incomplete"):
                seen_terminal = True
                yield terminal_chunk(event, calls, order)
                return
            elif event_type == "response.failed":
                seen_terminal = True
                yield StreamChunk(error=failure_error(event))
                return
            elif event_type == "error":
                seen_terminal = True
                yield StreamChunk(error=stream_error(event))
                return
            # 其余（response.created / in_progress / output_text.done 等生命周期
            # 事件）没有 ATRI-X 增量消费的内容。
        if not seen_terminal:
            yield StreamChunk(error=read_error(EOFError("unexpected EOF")))


#: 交叉检索别名（任务约定的对外名字）。
OpenAIResponsesClient = ResponsesAdapter


# ---------------------------------------------------------------------------
# 请求类型（对应 Go responses.go 的 request types 一节）
# ---------------------------------------------------------------------------

def to_responses_input(
    messages: Sequence[LLMMessage],
    resolve_image: Callable[[MessageSegment], str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """把 ATRI-X 消息转成 ``instructions`` 字符串 + 有序 ``input`` items。

    对应 Go ``toResponsesInput``：system 段变成 instructions；assistant 的工具
    调用变成 ``function_call`` item、工具结果变成 ``function_call_output`` item，
    因为 Responses 协议把一轮工具调用重放成独立 item 而不是嵌套 tool_calls。
    工具返回的图片同样在所有 tool item 之后派生为一条 user 多模态消息。
    """
    input_items: list[dict[str, Any]] = []
    instructions: list[str] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        segments = resolve_segments(message.segments, resolve_image)
        if message.role == Role.SYSTEM:
            text = segments_content_text(segments)
            if text.strip():
                instructions.append(text)
            index += 1
            continue
        if message.role == Role.TOOL:
            carrier: list[MessageSegment] = []
            while index < total and messages[index].role == Role.TOOL:
                tool_message = messages[index]
                content, images = openai_tool_content(
                    resolve_segments(tool_message.segments, resolve_image)
                )
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_message.tool_call_id,
                        "output": content,
                    }
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
            # 图片没法塞进 function_call_output，只能像 chat 适配器那样跟在后面。
            if carrier:
                input_items.append(responses_content_message(Role.USER, carrier))
            continue
        if message.role == Role.ASSISTANT:
            parts = to_responses_content(with_image_references(segments), Role.ASSISTANT)
            if parts:
                input_items.append(
                    {"type": "message", "role": Role.ASSISTANT, "content": parts}
                )
            for call in message.tool_calls:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
            index += 1
            continue
        input_items.append(
            responses_content_message(message.role, with_image_references(segments))
        )
        index += 1
    return "\n\n".join(instructions), input_items


def responses_content_message(role: str, segments: Sequence[MessageSegment]) -> dict[str, Any]:
    """对应 Go ``responsesContentMessage``。"""
    parts = to_responses_content(segments, role)
    if not parts:
        # 空 content 数组会被上游拒绝；空字符串是官方「无内容消息」的写法。
        return {"type": "message", "role": role, "content": ""}
    return {"type": "message", "role": role, "content": parts}


def to_responses_content(
    segments: Sequence[MessageSegment], role: str
) -> list[dict[str, Any]]:
    """对应 Go ``toResponsesContent``。

    assistant 的历史用 ``output_text``，user/system 历史用 ``input_text``；
    图片只在 user 消息上合法，所以 assistant 的图片退化成文本引用。
    """
    text_type = "output_text" if role == Role.ASSISTANT else "input_text"
    parts: list[dict[str, Any]] = []
    for segment in segments:
        if segment.type == SegmentType.TEXT:
            if segment.text:
                parts.append({"type": text_type, "text": segment.text})
        elif segment.type == SegmentType.IMAGE:
            if segment.url == "" or role == Role.ASSISTANT:
                continue
            item: dict[str, Any] = {"type": "input_image", "image_url": segment.url}
            # ``detail`` 与 chat-completions 的 image_url 同义；空值不写，保持与
            # 旧请求体逐字节一致（上游按默认档位处理）。
            detail = str(getattr(segment, "detail", "") or "").strip()
            if detail:
                item["detail"] = detail
            parts.append(item)
        elif segment.type == SegmentType.FILE:
            # 与 chat 适配器一致：厂商文件输入未实现，统一作为文本描述发送。
            text = segments_content_text([segment])
            if text:
                parts.append({"type": text_type, "text": text})
    return parts


def to_responses_tools(tools: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    """对应 Go ``toResponsesTools``：name/description/parameters 提到 type 旁边。"""
    return [
        {
            "type": tool.type or "function",
            "name": tool.function.name,
            "description": tool.function.description,
            "parameters": tool.function.parameters,
        }
        for tool in tools
    ]


# ---------------------------------------------------------------------------
# 事件累积与错误（对应 Go readResponsesStream 的 helper）
# ---------------------------------------------------------------------------

def _accumulate_call(
    calls: dict[int, dict[str, str]], order: list[int], event: Mapping[str, Any]
) -> None:
    """对应 Go ``accumulateResponsesCall``。"""
    item = event.get("item")
    item = item if isinstance(item, dict) else {}
    if text_of(item.get("type")) != "function_call":
        return
    acc = _call_for(calls, order, json_int(event.get("output_index")))
    if item.get("call_id"):
        acc["call_id"] = str(item["call_id"])
    if item.get("name"):
        acc["name"] = str(item["name"])
    # output_item.done 重复完整参数，权威性高于 delta 累积出来的内容。
    if item.get("arguments"):
        acc["arguments"] = str(item["arguments"])


def _accumulate_arguments(
    calls: dict[int, dict[str, str]],
    order: list[int],
    index: int,
    arguments: str,
    replace: bool,
) -> None:
    """对应 Go ``accumulateResponsesArguments``。"""
    acc = _call_for(calls, order, index)
    if replace:
        acc["arguments"] = ""
    acc["arguments"] += arguments


def _call_for(
    calls: dict[int, dict[str, str]], order: list[int], index: int
) -> dict[str, str]:
    """对应 Go ``responsesCall``：按 output_index 建账并记住顺序。"""
    acc = calls.get(index)
    if acc is None:
        acc = {"call_id": "", "name": "", "arguments": ""}
        calls[index] = acc
        order.append(index)
    return acc


def terminal_chunk(
    event: Mapping[str, Any], calls: Mapping[int, dict[str, str]], order: Sequence[int]
) -> StreamChunk:
    """对应 Go ``responsesTerminalChunk``。

    工具调用**只在这里**吐一次，并带上管线要记的 finish reason；提前到每个
    ``output_item.done`` 吐会让 agent 重复执行同一次调用。
    """
    chunk = StreamChunk()
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    usage = response.get("usage")
    if isinstance(usage, dict):
        chunk.usage = to_usage(usage)
    for index in order:
        acc = calls.get(index)
        if acc is None:
            continue
        chunk.tool_call_deltas.append(
            ToolCallDelta(
                index=index,
                id=acc["call_id"],
                name=acc["name"],
                args=acc["arguments"],
            )
        )
    if text_of(event.get("type")) == "response.incomplete":
        chunk.finish_reason = incomplete_finish_reason(response)
    elif chunk.tool_call_deltas:
        chunk.finish_reason = "tool_calls"
    else:
        chunk.finish_reason = "stop"
    return chunk


def incomplete_finish_reason(response: Mapping[str, Any]) -> str:
    """对应 Go ``responsesIncompleteFinishReason``：截断要让日志看得见。"""
    details = response.get("incomplete_details")
    details = details if isinstance(details, dict) else {}
    if text_of(details.get("reason")).lower() == "max_output_tokens":
        return "length"
    return "incomplete"


def failure_error(event: Mapping[str, Any]) -> BaseException:
    """对应 Go ``responsesFailureError``。"""
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    nested = response.get("error")
    if not isinstance(nested, dict):
        nested = event.get("error")
    if isinstance(nested, dict):
        return _api_error_of(nested)
    return APIError("responses stream failed without an error payload")


def stream_error(event: Mapping[str, Any]) -> BaseException:
    """对应 Go ``responsesStreamError``：``error`` 事件把 code/message/param
    放在事件顶层（网关把它们嵌在 ``error`` 下的写法也接受）。

    顶层形态下 **不读** ``event["type"]``：它是事件名（``"error"``），不是错误
    类型；Go 也一样只取 code/message/param。
    """
    nested = event.get("error")
    if isinstance(nested, dict):
        return _api_error_of(nested)
    if text_of(event.get("message")).strip() or event.get("code") is not None:
        return _api_error_of(
            {"code": event.get("code"), "message": event.get("message"), "param": event.get("param")}
        )
    return APIError("responses stream reported an error without details")


def _api_error_of(raw: Mapping[str, Any]) -> APIError:
    """对应 Go ``responsesAPIErrorToLLM``。

    流内事件没有 HTTP 状态码，因此分类只依赖机器可读字段（``status=0``）。
    """
    code = stringify_error_field(raw.get("code"))
    type_name = str(raw.get("type") or "").strip()
    param = stringify_error_field(raw.get("param"))
    message = str(raw.get("message") or "").strip() or "responses request failed"
    return APIError(
        response_summary(message),
        code=code,
        type=type_name,
        param=param,
        category=classify(0, code, type_name, param, message),
    )
