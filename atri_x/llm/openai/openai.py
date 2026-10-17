"""OpenAI-compatible 适配器（Chat Completions）。

对应 Go：``internal/llm/openai/openai.go`` 的 ``Adapter`` / ``New*`` /
``ChatStream`` / ``ListModels`` / ``ListModelMetadata`` / 重试与日志。

职责划分（按「单文件 ≤ 600 行」的移植约定拆到包内多个模块，对外入口仍是
:class:`OpenAIClient`）：

* :mod:`atri_x.llm.openai.wire`：请求信封/消息转换/usage 解析（Go 的 request
  types 与 helper 一节）；
* :mod:`atri_x.llm.openai.errors`：``parse_error`` 与响应摘要（Go 的 error
  handling 一节）；
* :mod:`atri_x.llm.openai.stream`：SSE 行扫描与首包/空闲超时（Go 的 stream
  reading 一节）；
* :mod:`atri_x.llm.openai.responses`：Responses 协议，复用本模块的重试、
  ``/models``、SSE 行扫描与 ``parse_error``——与 Go 侧 ``ResponsesAdapter``
  内嵌 ``Adapter`` 的结构一致。

本模块自己负责：端点与请求头、``extra_payload``/``model_extra_payloads``/
``extra_body`` 的合并顺序与协议保留字段、可重试状态码与退避、
``RetryNotifier``、非流式 ``chat()``、模型列表与日志摘要。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from atri_x.llm.errors import APIError
from atri_x.llm.llm import (
    ChatRequest,
    ChatResponse,
    LLMMessage,
    MessageSegment,
    ModelMetadata,
    RetryEvent,
    Role,
    SegmentType,
    StreamChunk,
    ToolCallDelta,
    ToolCallRequest,
)
from atri_x.net.httpclient import HTTPClient, HTTPError, HTTPStream

# 错误分类/摘要与请求构造拆到同包模块；这里再导出一次，保持「对外入口仍是
# OpenAIClient（且 parse_error 等仍可从 openai 模块取到）」。
from atri_x.llm.openai.errors import (  # noqa: F401
    classify,
    looks_like_html,
    looks_like_sse,
    parse_error,
    read_error,
    response_summary,
)
from atri_x.llm.openai.stream import (
    AwaitableStream,
    StreamLineReader,
    prepare_stream_body,
)
from atri_x.llm.openai.transport import BufferedResponse, LineStream, buffered_request
from atri_x.llm.openai.wire import (
    CHAT_RESERVED_REQUEST_FIELDS,
    DATA_DONE,
    DATA_PREFIX,
    DEFAULT_FIRST_CHUNK_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_INITIAL_DELAY,
    DEFAULT_STREAM_IDLE_TIMEOUT,
    MAX_STREAM_LINE_BYTES,
    STREAM_PREFIX_BYTES,
    RequestOptions,
    RequestLog,
    first_system_message_json,
    first_system_text,
    hash_bytes,
    hash_json,
    hash_text,
    is_retryable_status,
    json_compact,
    json_int,
    latest_message_json,
    merge_extra_fields,
    reasoning_of,
    retry_delay,
    text_of,
    to_openai_messages,
    to_openai_tools,
    to_usage,
)

__all__ = [
    "Adapter",
    "CHAT_RESERVED_REQUEST_FIELDS",
    "DEFAULT_FIRST_CHUNK_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_INITIAL_DELAY",
    "DEFAULT_STREAM_IDLE_TIMEOUT",
    "MAX_STREAM_LINE_BYTES",
    "OpenAIClient",
    "RequestOptions",
    "STREAM_PREFIX_BYTES",
    "StreamLineReader",
    "classify",
    "looks_like_html",
    "looks_like_sse",
    "parse_error",
    "response_summary",
    "to_openai_messages",
]


class OpenAIClient:
    """OpenAI-compatible 适配器。对应 Go ``Adapter``。

    Go 的 ``New`` / ``NewWithModelExtraPayloads`` / ``NewWithOptions`` 在这里合并
    为一个构造器：省略 ``model_extra_payloads`` / ``options`` 即前两者的默认值。

    ``resolve_image`` 是 Python 侧新增的注入点：Go 由调用方
    （``agent.chat_llm`` 的 ``media.ResolveForLLM``）提前把图片 URL 填好，这里默认
    行为完全一致（原样用 ``segment.url``），只有 url 为空且注入了解析器时才会去问
    它——因此不注入时与 Go 逐字等价。
    """

    #: 本适配器独占的协议保留字段（Responses 适配器覆盖它）。
    reserved_request_fields = CHAT_RESERVED_REQUEST_FIELDS
    #: 请求日志的 message 前缀（Go 的 ``logChatRequest`` / ``logResponsesRequest``）。
    request_log_message = "openai chat request"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        extra_payload: Mapping[str, Any] | None = None,
        model_extra_payloads: Mapping[str, Mapping[str, Any]] | None = None,
        options: RequestOptions | None = None,
        *,
        resolve_image: Callable[[MessageSegment], str] | None = None,
        http: HTTPClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.extra_payload: dict[str, Any] = dict(extra_payload or {})
        self.model_extra_payloads: dict[str, dict[str, Any]] = {
            model: dict(payload) for model, payload in (model_extra_payloads or {}).items()
        }
        self.options = (options or RequestOptions()).with_defaults()
        self.first_chunk_timeout = self.options.first_chunk_timeout
        self.stream_idle_timeout = self.options.stream_idle_timeout
        self.max_retries = self.options.max_retries
        self.retry_initial_delay = self.options.retry_initial_delay
        self.on_retry: Callable[[Any, RetryEvent], Any] | None = self.options.on_retry
        self.resolve_image = resolve_image
        self._request_log = RequestLog(logger=None, resolve_image=resolve_image)

        if self.options.proxy:
            # 对应 Go ``url.Parse`` 失败即报错：Python 的 urlsplit 很宽容，这里
            # 显式要求 http/https scheme 与 host。
            parsed = urllib.parse.urlsplit(self.options.proxy)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"invalid proxy URL {self.options.proxy!r}")
        self._http = http or HTTPClient(base_url=self.base_url, timeout=self.first_chunk_timeout)
        # 与 Go 一致（TestNewWithOptionsWithoutProxyDisablesEnvironmentProxy）：
        # 未配置 provider proxy 时不继承环境代理。
        self._http.proxy = self.options.proxy or ""

    # -- 配置 ---------------------------------------------------------------
    def set_retry_notifier(self, fn: Callable[[Any, RetryEvent], Any] | None) -> None:
        """对应 Go ``SetRetryNotifier``。"""
        self.on_retry = fn

    def set_logger(self, logger: logging.Logger | None) -> None:
        """对应 Go ``SetLogger``。"""
        self._request_log.set_logger(logger)

    def validate_base_url(self) -> None:
        """对应 Go ``validateBaseURL``。"""
        if not self.base_url.strip():
            raise ValueError("openai base_url is required")

    def protocol_capabilities_for(self, model: str) -> Any:
        """对应 Go ``Adapter.ProtocolCapabilitiesFor``：报告最弱协议。

        Chat Completions 没有服务端续链、原生压缩、增量工具与服务端存储。
        """
        from atri_x.llm.protocol import Protocol, ProtocolCapabilities

        return ProtocolCapabilities(protocol=Protocol.CHAT_COMPLETIONS)

    # -- 端点与请求头 -------------------------------------------------------
    def endpoint(self) -> str:
        """对应 Go ``Adapter.endpoint``。"""
        return f"{self.base_url}/chat/completions"

    def models_endpoint(self) -> str:
        return f"{self.base_url}/models"

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _transport_headers(self, *, json_body: bool = False) -> dict[str, str]:
        """请求头 = ``HTTPClient`` 默认头（含 User-Agent）+ 适配器头。"""
        headers = dict(self._http.headers)
        headers.update(self._headers(json_body=json_body))
        return headers

    def build_body(self, req: ChatRequest, *, stream: bool = True) -> dict[str, Any]:
        """构造请求信封并合并 extra payload（对应 Go ``ChatStream`` 的 body 部分）。

        合并顺序：provider ``extra_payload`` → ``model_extra_payloads[model]`` →
        ``ChatRequest.extra_body``，后者胜；协议保留字段由适配器独占。
        """
        body: dict[str, Any] = {
            "model": req.model,
            "messages": to_openai_messages(req.messages, self.resolve_image),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if not stream:
            body.pop("stream")
            body.pop("stream_options")
        if req.temperature > 0:
            body["temperature"] = req.temperature
        if req.max_tokens > 0:
            # Go 的 chat 适配器漏发了 max_tokens（ChatRequest 有这个字段，但
            # openai.go 只把它用在 responses 的 max_output_tokens），属于上游
            # 契约缺口；这里按 Python 契约补上，详见移植报告。
            body["max_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = to_openai_tools(req.tools)

        reserved = self.reserved_request_fields
        dropped = merge_extra_fields(body, self.extra_payload, reserved)
        dropped += merge_extra_fields(body, self.model_extra_payloads.get(req.model), reserved)
        dropped += merge_extra_fields(body, req.extra_body, reserved)
        self._log_dropped_extra_fields(dropped)
        return body

    # -- 流式 ---------------------------------------------------------------
    def chat_stream(self, ctx: Any, req: ChatRequest) -> Any:
        """发送一次流式请求，返回 ``StreamChunk`` 的异步迭代器。

        对应 Go ``ChatStream``。Go 把「建连/首包阶段」的错误同步返回；Python 的
        异步迭代器只能在下一次迭代时抛出，因此 base_url 与代理校验仍然立刻抛，
        其余（重试耗尽、非 200、HTML/非 SSE 首包）在首次迭代时抛出。
        """
        self.validate_base_url()
        body = self.build_body(req)
        self._log_request(self.request_log_message, self.endpoint(), req, body)
        return AwaitableStream(self._stream(ctx, body, self._consume_stream))

    async def _stream(
        self,
        ctx: Any,
        body: dict[str, Any],
        consume: Callable[[StreamLineReader], AsyncIterator[StreamChunk]],
    ) -> AsyncIterator[StreamChunk]:
        """流式骨架：重试 → 状态码 → 首包校验 → 逐行翻译。"""
        stream = await self._do_with_retry(ctx, lambda: self.open_stream_request(ctx, body))
        if stream.status != 200:
            try:
                raw = await stream.read_all()
            except HTTPError as exc:
                raise APIError(
                    f"failed to read body: {exc}", status_code=stream.status, cause=exc
                ) from exc
            finally:
                await stream.close()
            raise parse_error(stream.status, raw, stream.headers)

        try:
            iterator, prefix = await prepare_stream_body(stream, self.first_chunk_timeout)
        except BaseException:
            await stream.close()
            raise
        reader = StreamLineReader(
            iterator,
            prefix,
            self.first_chunk_timeout,
            self.stream_idle_timeout,
            cancel=ctx,
        )
        try:
            async for chunk in consume(reader):
                yield chunk
        except (APIError, HTTPError) as exc:
            yield StreamChunk(error=exc)
        finally:
            await stream.close()

    async def open_stream_request(self, ctx: Any, body: dict[str, Any]) -> LineStream:
        """打开 SSE 连接（对应 Go 里 ``doWithRetry`` 包住的 ``client.Do``）。

        TODO(httpclient): 这里本应直接用 ``self._http.open_stream(...)``；当前
        ``httpclient.HTTPStream._pump`` 在工作线程里调用 ``asyncio.get_running_loop()``
        必然抛 ``RuntimeError``，导致 ``open_stream`` 永久挂起，所以走同包的
        :class:`~atri_x.llm.openai.transport.LineStream`（同样的对外契约，
        连接/代理/TLS 仍复用 httpclient 的 ``_open_connection``/``_send``）。
        httpclient 修好后把下面这段换成 ``self._http.open_stream`` 即可。

        另外 ``read_timeout=0`` 的语义：关掉 socket 级读超时，首包/空闲判定完全由
        StreamLineReader 负责，与 Go 只用 timer（没有读截止时间）的语义一致。
        """
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        stream = LineStream(
            url=self.endpoint(),
            method="POST",
            headers={**self._transport_headers(json_body=True), "Accept": "text/event-stream"},
            payload=payload,
            proxy=self._http.proxy,
            verify_tls=self._http.verify_tls,
            timeout=self.first_chunk_timeout,
        )
        await stream.open()
        return stream

    async def _buffered_request(
        self, ctx: Any, method: str, url: str, body: Any = None
    ) -> BufferedResponse:
        """一次性请求（``/models``、非流式 chat）。

        TODO(httpclient): 这里本应直接用 ``self._http.request(...)``；当前
        ``HTTPClient._blocking_request`` 把 ``_send()`` 的 ``None`` 当成状态码，
        ``Response.status`` 恒为 ``None``，``request()`` 里的重试策略比较会抛
        ``TypeError``。因此走同包的 ``transport.buffered_request``（同样的对外
        契约，连接/代理/TLS 仍复用 httpclient）。httpclient 修好后即可换回。
        """
        payload = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        headers = self._transport_headers(json_body=body is not None)
        return await self._do_with_retry(
            ctx,
            lambda: buffered_request(
                url=url,
                method=method,
                headers=headers,
                payload=payload,
                proxy=self._http.proxy,
                verify_tls=self._http.verify_tls,
                timeout=self.first_chunk_timeout,
            ),
        )

    async def _consume_stream(self, reader: StreamLineReader) -> AsyncIterator[StreamChunk]:
        """把 chat-completions SSE 翻译成 ``StreamChunk``（对应 Go ``readStream``）。"""
        accums: dict[int, dict[str, str]] = {}
        seen_done = False
        async for line in reader:
            if line == "" or line.startswith(":"):
                continue
            if line == DATA_DONE:
                seen_done = True
                break
            if not line.startswith(DATA_PREFIX):
                continue
            try:
                raw = json.loads(line[len(DATA_PREFIX) :])
            except ValueError as exc:
                yield StreamChunk(error=HTTPError(f"parse stream chunk: {exc}", cause=exc))
                return
            if not isinstance(raw, dict):
                yield StreamChunk(error=HTTPError("parse stream chunk: not a JSON object"))
                return

            usage = raw.get("usage")
            choices = raw.get("choices") or []
            if isinstance(usage, dict) and not choices:
                yield StreamChunk(usage=to_usage(usage))
                continue
            if not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta")
            delta = delta if isinstance(delta, dict) else {}

            chunk = StreamChunk(
                delta_content=text_of(delta.get("content")),
                delta_reasoning_content=reasoning_of(delta),
            )
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str):
                chunk.finish_reason = finish_reason
            if isinstance(usage, dict):
                chunk.usage = to_usage(usage)

            for tool_call in delta.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                index = json_int(tool_call.get("index"))
                acc = accums.setdefault(index, {"id": "", "name": "", "args": ""})
                # Go 是「非空即覆盖」：后续分片带空 id/name 时保留首次值。
                if tool_call.get("id"):
                    acc["id"] = str(tool_call["id"])
                function = tool_call.get("function")
                function = function if isinstance(function, dict) else {}
                if function.get("name"):
                    acc["name"] = str(function["name"])
                acc["args"] += text_of(function.get("arguments"))

            if chunk.finish_reason and accums:
                # Go 遍历 map（顺序随机）；这里按 index 首次出现顺序输出，可复现。
                for index, acc in accums.items():
                    chunk.tool_call_deltas.append(
                        ToolCallDelta(index=index, id=acc["id"], name=acc["name"], args=acc["args"])
                    )
            yield chunk
        if not seen_done:
            yield StreamChunk(error=read_error(EOFError("unexpected EOF")))

    # -- 非流式 -------------------------------------------------------------
    async def chat(self, ctx: Any, req: ChatRequest) -> ChatResponse:
        """非流式对话调用。

        Go 侧只定义了 ``llm.ChatResponse`` 而没有实现（适配器只有流式路径）；
        这里用同一封信封去掉 ``stream`` / ``stream_options`` 实现，字段解析与
        流式保持同一套语义。
        """
        self.validate_base_url()
        body = self.build_body(req, stream=False)
        response = await self._buffered_request(ctx, "POST", self.endpoint(), body)
        if response.status != 200:
            raise parse_error(response.status, response.body, response.headers)
        try:
            data = json.loads(response.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise HTTPError(f"decode chat response: {exc}", cause=exc) from exc

        message: dict[str, Any] = {}
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            candidate = choices[0].get("message")
            message = candidate if isinstance(candidate, dict) else {}
        tool_calls = [
            ToolCallRequest(
                id=str(call.get("id") or ""),
                name=str(_function_of(call).get("name") or ""),
                arguments=str(_function_of(call).get("arguments") or ""),
            )
            for call in (message.get("tool_calls") or [])
            if isinstance(call, dict)
        ]
        content = text_of(message.get("content"))
        usage_raw = data.get("usage") if isinstance(data, dict) else None
        return ChatResponse(
            message=LLMMessage(
                role=Role.ASSISTANT,
                segments=[MessageSegment(type=SegmentType.TEXT, text=content)] if content else [],
                tool_calls=tool_calls,
            ),
            tool_calls=tool_calls,
            usage=to_usage(usage_raw) if isinstance(usage_raw, dict) else None,
        )

    # -- 模型列表 -----------------------------------------------------------
    async def list_models(self, ctx: Any) -> list[str]:
        """对应 Go ``ListModels``：``{base_url}/models`` 的 ``data[].id``。

        Go 保持上游顺序且不去重，这里逐字对齐（不排序、不去重）。
        """
        result = await self._fetch_models(ctx)
        return [
            str(model.get("id") or "")
            for model in (result.get("data") or [])
            if isinstance(model, dict)
        ]

    async def list_model_metadata(self, ctx: Any) -> list[ModelMetadata]:
        """对应 Go ``ListModelMetadata``。"""
        result = await self._fetch_models(ctx)
        out: list[ModelMetadata] = []
        for model in result.get("data") or []:
            if not isinstance(model, dict):
                continue
            window = 0
            for name in ("context_window", "max_context_length", "max_input_tokens", "max_tokens"):
                window = json_int(model.get(name))
                if window > 0:
                    break
            out.append(ModelMetadata(id=str(model.get("id") or ""), context_window=window))
        return out

    async def _fetch_models(self, ctx: Any) -> dict[str, Any]:
        """对应 Go ``fetchModels``。"""
        self.validate_base_url()
        response = await self._buffered_request(ctx, "GET", self.models_endpoint())
        if response.status != 200:
            raise parse_error(response.status, response.body, response.headers)
        try:
            result = json.loads(response.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise HTTPError(f"decode models response: {exc}", cause=exc) from exc
        if not isinstance(result, dict):
            raise HTTPError("decode models response: not a JSON object")
        return result

    # -- 重试 ---------------------------------------------------------------
    async def _do_with_retry(self, ctx: Any, open_fn: Callable[[], Any]) -> Any:
        """对应 Go ``doWithRetry``：可重试状态码与传输错误都重试。

        ``max_retries`` 语义与 Go 一致：总尝试次数为 ``max_retries + 1``，每次
        重试前回调 ``RetryNotifier``（``RetryEvent.attempt`` 从 1 开始）。
        """
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await open_fn()
            except HTTPError as exc:
                last_error = HTTPError(f"http request: {exc}", cause=exc)
            else:
                if not is_retryable_status(response.status):
                    return response
                last_error = await self._retryable_status_error(response)
            if attempt == self.max_retries:
                raise last_error
            delay = retry_delay(self.retry_initial_delay, attempt)
            if self.on_retry is not None:
                self.on_retry(
                    ctx,
                    RetryEvent(
                        attempt=attempt + 1,
                        max_retries=self.max_retries,
                        delay=delay,
                        err=last_error,
                    ),
                )
            await asyncio.sleep(delay)
        raise last_error  # pragma: no cover - 循环内必定 return/raise

    async def _retryable_status_error(self, response: Any) -> BaseException:
        """对应 Go ``retryableStatusError``：读完错误体再关闭并结构化。"""
        if isinstance(response, (LineStream, HTTPStream)):
            try:
                raw = await response.read_all()
            finally:
                await response.close()
            return parse_error(response.status, raw, response.headers)
        return parse_error(response.status, response.body, response.headers)

    # -- 日志（对应 Go 的 logRequest / logFirstSystemMessage） ---------------
    @property
    def logger(self) -> Any:
        return self._request_log.logger

    def _log_dropped_extra_fields(self, fields: Sequence[str]) -> None:
        self._request_log.log_dropped_extra_fields(fields)

    def _log_request(
        self, message: str, endpoint: str, req: ChatRequest, body: Mapping[str, Any]
    ) -> None:
        self._request_log.log_request(message, endpoint, req, body)


#: 交叉检索别名：Go 的类型名是 ``Adapter``。
Adapter = OpenAIClient


def _function_of(call: Mapping[str, Any]) -> Mapping[str, Any]:
    function = call.get("function")
    return function if isinstance(function, dict) else {}
