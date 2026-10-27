"""``atri_x.llm.openai.responses``（Responses 协议）端到端测试。

对应 Go：``internal/llm/openai/responses_test.go``。复用 ``test_openai_adapter``
里的假服务端（``ThreadingHTTPServer``），事件逐条断言，不 mock HTTP 层。
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping, Sequence

from atri_x.llm.errors import APIError, ErrorCategory, as_api_error
from atri_x.llm.llm import (
    LLMMessage,
    MessageSegment,
    Role,
    SegmentType,
    ToolCallRequest,
    ToolFunctionSchema,
    ToolSchema,
    text_segments,
)
from atri_x.llm.openai.responses import (
    RESPONSES_RESERVED_REQUEST_FIELDS,
    ResponsesAdapter,
)
from atri_x.llm.openai.wire import RequestOptions
from atri_x.net.httpclient import HTTPStream
from tests.test_openai_adapter import (
    FakeServer,
    _StreamCase,
    raw,
    write_body,
    write_headers_only,
    write_sse,
)


class _ResponsesCase(_StreamCase):
    def make_responses(self, *args: Any, **kwargs: Any) -> ResponsesAdapter:
        options = kwargs.pop(
            "options",
            RequestOptions(
                first_chunk_timeout=2.0,
                stream_idle_timeout=2.0,
                max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        return ResponsesAdapter(*args, options=options, **kwargs)

    def responses_server(
        self, events: Sequence[str], capture: bool = True
    ) -> FakeServer:
        chunks = [event.encode("utf-8") + b"\n\n" for event in events]
        return self.start_server(lambda handler, record: write_sse(handler, chunks))


class TestResponsesStreaming(_ResponsesCase):
    async def test_text_usage_and_request_shape(self) -> None:
        server = self.responses_server(
            [
                'data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}',
                'data: {"type":"response.output_text.delta","delta":"Hello"}',
                'data: {"type":"response.output_text.delta","delta":" world"}',
                'data: {"type":"response.completed","response":{"id":"resp_1",'
                '"status":"completed","usage":{"input_tokens":5,"output_tokens":2,'
                '"total_tokens":7,"input_tokens_details":{"cached_tokens":3}}}}',
            ]
        )
        client = self.make_responses(server.url + "/v1", "test-key")
        content, _, finish, _, usage, error = await self.collect(
            client.chat_stream(
                None,
                self.request(
                    model="gpt-5.1",
                    messages=[
                        LLMMessage(role=Role.SYSTEM, segments=text_segments("be brief")),
                        LLMMessage(role=Role.USER, segments=text_segments("Hi")),
                    ],
                    tools=[
                        ToolSchema(
                            type="function",
                            function=ToolFunctionSchema(
                                name="shell",
                                description="run a command",
                                parameters={"type": "object"},
                            ),
                        )
                    ],
                ),
            )
        )
        self.assertIsNone(error)
        self.assertEqual(content, "Hello world")
        self.assertEqual(finish, "stop")
        assert usage is not None
        self.assertEqual(
            (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, usage.cache_hit_tokens),
            (5, 2, 7, 3),
        )

        self.assertEqual(server.records[0]["path"], "/v1/responses")
        body = server.last_body()
        self.assertEqual(body["model"], "gpt-5.1")
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["store"], False)
        self.assertEqual(body["instructions"], "be brief")
        self.assertNotIn("stream_options", body)
        self.assertNotIn("messages", body)
        input_items = body["input"]
        self.assertEqual(len(input_items), 1)
        self.assertEqual(input_items[0]["type"], "message")
        self.assertEqual(input_items[0]["role"], "user")
        self.assertEqual(input_items[0]["content"],
                         [{"type": "input_text", "text": "Hi"}])
        tool = body["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["name"], "shell")
        self.assertEqual(tool["description"], "run a command")
        self.assertNotIn("function", tool)

    async def test_tool_call_emitted_once_with_complete_arguments(self) -> None:
        server = self.responses_server(
            [
                'data: {"type":"response.output_item.added","output_index":0,"item":'
                '{"type":"function_call","id":"fc_1","call_id":"call_1","name":"shell","arguments":""}}',
                'data: {"type":"response.function_call_arguments.delta","output_index":0,'
                '"item_id":"fc_1","delta":"{\\"cmd\\""}',
                'data: {"type":"response.function_call_arguments.delta","output_index":0,'
                '"item_id":"fc_1","delta":":\\"ls\\"}"}',
                'data: {"type":"response.function_call_arguments.done","output_index":0,'
                '"item_id":"fc_1","arguments":"{\\"cmd\\":\\"ls\\"}"}',
                'data: {"type":"response.output_item.done","output_index":0,"item":'
                '{"type":"function_call","id":"fc_1","call_id":"call_1","name":"shell",'
                '"arguments":"{\\"cmd\\":\\"ls\\"}"}}',
                'data: {"type":"response.completed","response":{"id":"resp_2",'
                '"status":"completed","usage":{"input_tokens":10,"output_tokens":4,'
                '"total_tokens":14}}}',
            ]
        )
        client = self.make_responses(server.url, "test-key")
        _, _, finish, tool_calls, usage, error = await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[LLMMessage(role=Role.USER, segments=text_segments("list files"))]
                ),
            )
        )
        self.assertIsNone(error)
        self.assertEqual(len(tool_calls), 1, "工具调用只能吐一次，否则 agent 会重复执行")
        call = tool_calls[0]
        self.assertEqual((call.index, call.id, call.name, call.args),
                         (0, "call_1", "shell", '{"cmd":"ls"}'))
        self.assertEqual(finish, "tool_calls")
        assert usage is not None
        self.assertEqual(usage.total_tokens, 14)

    async def test_tool_turn_request_shape(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"id":"resp_3","status":"completed"}}']
        )
        client = self.make_responses(server.url + "/v1", "test-key")
        _, _, _, _, _, error = await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[
                        LLMMessage(role=Role.SYSTEM, segments=text_segments("be brief")),
                        LLMMessage(role=Role.USER, segments=text_segments("list files")),
                        LLMMessage(
                            role=Role.ASSISTANT,
                            segments=text_segments("on it"),
                            tool_calls=[
                                ToolCallRequest(id="call_1", name="shell", arguments='{"cmd":"ls"}')
                            ],
                        ),
                        LLMMessage(
                            role=Role.TOOL,
                            name="shell",
                            tool_call_id="call_1",
                            segments=text_segments("a.txt"),
                        ),
                    ]
                ),
            )
        )
        self.assertIsNone(error)
        input_items = server.last_body()["input"]
        self.assertEqual(len(input_items), 4)
        self.assertEqual(input_items[0]["type"], "message")
        self.assertEqual(input_items[0]["role"], "user")
        assistant = input_items[1]
        self.assertEqual((assistant["type"], assistant["role"]), ("message", "assistant"))
        self.assertEqual(assistant["content"], [{"type": "output_text", "text": "on it"}])
        self.assertEqual(
            input_items[2],
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": '{"cmd":"ls"}'},
        )
        self.assertEqual(
            input_items[3],
            {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
        )

    async def test_image_becomes_input_image_with_reference(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(server.url, "test-key")
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[
                        LLMMessage(
                            role=Role.USER,
                            segments=[
                                MessageSegment(type=SegmentType.TEXT, text="what is this"),
                                MessageSegment(
                                    type=SegmentType.IMAGE, url="https://example.invalid/cat.png"
                                ),
                            ],
                        )
                    ]
                ),
            )
        )
        content = server.last_body()["input"][0]["content"]
        self.assertEqual(len(content), 3)
        self.assertEqual(content[0], {"type": "input_text", "text": "what is this"})
        self.assertEqual(
            content[1],
            {"type": "input_text", "text": "[图片 1；引用 URL：https://example.invalid/cat.png]"},
        )
        self.assertEqual(
            content[2], {"type": "input_image", "image_url": "https://example.invalid/cat.png"}
        )

    async def test_assistant_image_is_downgraded_to_text(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(server.url, "test-key")
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[
                        LLMMessage(
                            role=Role.ASSISTANT,
                            segments=[MessageSegment(type=SegmentType.IMAGE, url="https://x/y.png")],
                        )
                    ]
                ),
            )
        )
        content = server.last_body()["input"][0]["content"]
        self.assertTrue(all(part["type"] == "output_text" for part in content))
        self.assertEqual(len(content), 1)

    async def test_reasoning_delta(self) -> None:
        server = self.responses_server(
            [
                'data: {"type":"response.reasoning_summary_text.delta","delta":"thinking"}',
                'data: {"type":"response.reasoning_text.delta","delta":" more"}',
                'data: {"type":"response.output_text.delta","delta":"answer"}',
                'data: {"type":"response.completed","response":{"status":"completed"}}',
            ]
        )
        client = self.make_responses(server.url, "test-key")
        content, reasoning, _, _, _, error = await self.collect(
            client.chat_stream(None, self.request())
        )
        self.assertIsNone(error)
        self.assertEqual(reasoning, "thinking more")
        self.assertEqual(content, "answer")

    async def test_incomplete_event_maps_truncation_to_length(self) -> None:
        server = self.responses_server(
            [
                'data: {"type":"response.output_text.delta","delta":"cut"}',
                'data: {"type":"response.incomplete","response":{"status":"incomplete",'
                '"incomplete_details":{"reason":"max_output_tokens"}}}',
            ]
        )
        client = self.make_responses(server.url, "test-key")
        content, _, finish, _, _, error = await self.collect(
            client.chat_stream(None, self.request())
        )
        self.assertIsNone(error)
        self.assertEqual(content, "cut")
        self.assertEqual(finish, "length")

    async def test_done_sentinel_ends_stream(self) -> None:
        server = self.responses_server(
            [
                'data: {"type":"response.output_text.delta","delta":"Hello"}',
                "data: [DONE]",
            ]
        )
        client = self.make_responses(server.url, "test-key")
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "Hello")

    async def test_missing_terminal_event_is_an_unexpected_eof(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.output_text.delta","delta":"partial"}']
        )
        client = self.make_responses(server.url, "test-key")
        content, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertEqual(content, "partial")
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("unexpected EOF", str(error))

    async def test_malformed_event_is_reported(self) -> None:
        server = self.start_server(lambda handler, record: write_sse(handler, raw("data: {oops}\n\n")))
        client = self.make_responses(server.url, "test-key")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("parse stream chunk", str(error))

    async def test_named_event_lines_are_ignored(self) -> None:
        server = self.start_server(
            lambda handler, record: write_sse(
                handler,
                raw(
                    b"event: response.output_text.delta\n",
                    b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                    b"event: response.completed\n",
                    b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
                ),
            )
        )
        client = self.make_responses(server.url, "test-key")
        content, _, finish, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNone(error)
        self.assertEqual(content, "ok")
        self.assertEqual(finish, "stop")


class TestResponsesErrors(_ResponsesCase):
    async def test_failed_event_carries_upstream_error(self) -> None:
        server = self.responses_server(
            [
                'data: {"type":"response.failed","response":{"id":"resp_4","status":"failed",'
                '"error":{"code":"model_not_found","type":"invalid_request_error",'
                '"message":"model gpt-x does not exist"}}}'
            ]
        )
        client = self.make_responses(server.url, "test-key")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request(model="gpt-x")))
        self.assertIsNotNone(error)
        api_error = as_api_error(error)
        self.assertIsNotNone(api_error)
        assert api_error is not None
        self.assertEqual(api_error.code, "model_not_found")
        self.assertEqual(api_error.category, ErrorCategory.MODEL_NOT_FOUND)

    async def test_error_event_carries_message_and_code(self) -> None:
        server = self.responses_server(
            ['data: {"type":"error","code":"server_error","message":"upstream exploded",'
             '"param":null,"sequence_number":2}']
        )
        client = self.make_responses(server.url, "test-key")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("upstream exploded", str(error))
        api_error = as_api_error(error)
        self.assertIsNotNone(api_error)
        assert api_error is not None
        self.assertEqual(api_error.code, "server_error")
        # 流内事件没有 HTTP 状态码，因此没有基于状态码的类别（与 Go 一致：
        # classifyAPIError(0, "server_error", ...) 也是空类别）。
        self.assertEqual(api_error.category, "")

    async def test_nested_error_event_is_accepted(self) -> None:
        server = self.responses_server(
            ['data: {"type":"error","error":{"type":"server_error","message":"nested failure"}}']
        )
        client = self.make_responses(server.url, "test-key")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("nested failure", str(error))

    async def test_failed_without_payload_is_reported(self) -> None:
        server = self.responses_server(['data: {"type":"response.failed","response":{"status":"failed"}}'])
        client = self.make_responses(server.url, "test-key")
        _, _, _, _, _, error = await self.collect(client.chat_stream(None, self.request()))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("without an error payload", str(error))

    async def test_http_error_keeps_vision_category(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(
                handler,
                '{"error":{"message":"Invalid content type for image input",'
                '"type":"invalid_request_error","param":"input[0].content[1]"}}',
                status=400,
            )
        )
        client = self.make_responses(server.url, "test-key")
        with self.assertRaises(APIError) as caught:
            await self.collect(
                client.chat_stream(
                    None,
                    self.request(
                        messages=[
                            LLMMessage(
                                role=Role.USER,
                                segments=[
                                    MessageSegment(
                                        type=SegmentType.IMAGE,
                                        url="https://example.invalid/cat.png",
                                    )
                                ],
                            )
                        ]
                    ),
                )
            )
        self.assertEqual(caught.exception.category, ErrorCategory.VISION_UNSUPPORTED)

    async def test_first_chunk_timeout(self) -> None:
        server = self.start_server(lambda handler, record: write_headers_only(handler))
        client = self.make_responses(
            server.url,
            "test-key",
            options=RequestOptions(
                first_chunk_timeout=0.15, stream_idle_timeout=0.15, max_retries=1,
                retry_initial_delay=0.01,
            ),
        )
        with self.assertRaises(APIError) as caught:
            await self.collect(client.chat_stream(None, self.request()))
        self.assertIn("first stream chunk timeout", str(caught.exception))
        self.assertEqual(caught.exception.category, ErrorCategory.TIMEOUT)


class TestResponsesRequestEnvelope(_ResponsesCase):
    async def test_extra_payload_cannot_replace_reserved_fields(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(
            server.url,
            "test-key",
            {
                "stream": False,
                "input": [],
                "instructions": "hijacked",
                "reasoning": {"effort": "low"},
                "store": True,
                "max_output_tokens": 512,
            },
            {"test": {"stream": False}},
        )
        await self.collect(
            client.chat_stream(
                None,
                self.request(
                    messages=[
                        LLMMessage(role=Role.SYSTEM, segments=text_segments("be brief")),
                        LLMMessage(role=Role.USER, segments=text_segments("hi")),
                    ]
                ),
            )
        )
        body = server.last_body()
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["instructions"], "be brief")
        self.assertEqual(len(body["input"]), 1)
        # store 刻意不保留：网关需要别的值时仍可通过 extra_payload 覆盖。
        self.assertEqual(body["store"], True)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["max_output_tokens"], 512)

    def test_reserved_request_fields(self) -> None:
        for field in ("model", "input", "instructions", "stream", "tools"):
            self.assertIn(field, RESPONSES_RESERVED_REQUEST_FIELDS)
        self.assertNotIn("store", RESPONSES_RESERVED_REQUEST_FIELDS)

    async def test_max_tokens_maps_to_max_output_tokens(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(server.url, "test-key")
        await self.collect(client.chat_stream(None, self.request(max_tokens=256, temperature=0.4)))
        body = server.last_body()
        self.assertEqual(body["max_output_tokens"], 256)
        self.assertEqual(body["temperature"], 0.4)

    async def test_extended_extra_body_still_passes_through(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(server.url, "test-key")
        await self.collect(
            client.chat_stream(None, self.request(extra_body={"custom_field": "kept"}))
        )
        self.assertEqual(server.last_body()["custom_field"], "kept")

    async def test_protocol_capabilities_report_responses_without_server_side_state(self) -> None:
        client = ResponsesAdapter("https://example.invalid/v1", "k")
        caps = client.protocol_capabilities_for("m")
        self.assertEqual(caps.protocol, "responses")
        # 协议翻译层：固定 store=false、每轮重放历史 → 不许虚报服务端能力。
        self.assertFalse(caps.server_side_conversation)
        self.assertFalse(caps.native_compaction)
        self.assertFalse(caps.incremental_tools)
        self.assertFalse(caps.server_side_store)

    async def test_non_streaming_chat_is_not_implemented(self) -> None:
        client = ResponsesAdapter("https://example.invalid/v1", "k")
        with self.assertRaises(NotImplementedError):
            await client.chat(None, self.request())

    async def test_endpoint_matches_go(self) -> None:
        client = ResponsesAdapter("https://example.invalid/v1/", "k")
        self.assertEqual(client.endpoint(), "https://example.invalid/v1/responses")

    async def test_models_endpoint_is_shared_with_chat_adapter(self) -> None:
        server = self.start_server(
            lambda handler, record: write_body(handler, '{"data":[{"id":"m1","context_window":8}]}')
        )
        client = self.make_responses(server.url, "test-key")
        self.assertEqual(await client.list_models(None), ["m1"])
        self.assertEqual(server.records[0]["path"], "/models")


class TestResponsesTransportWiring(_ResponsesCase):
    """Responses 适配器继承同一套传输：流式也必须走 ``HTTPClient.open_stream``。"""

    async def test_stream_goes_through_httpclient_open_stream(self) -> None:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(server.url + "/v1", "test-key")
        ctx = object()
        seen = self.spy_open_stream(client)

        _, _, finish, _, _, error = await self.collect(client.chat_stream(ctx, self.request()))

        self.assertIsNone(error)
        self.assertEqual(finish, "stop")
        self.assertEqual(server.records[0]["path"], "/v1/responses")
        self.assertEqual(len(seen["calls"]), 1)
        args, kwargs = seen["calls"][0]
        self.assertEqual(args, ("POST", client.endpoint()))
        self.assertEqual(kwargs["read_timeout"], 0)
        self.assertIs(kwargs["follow_redirects"], True)
        self.assertIs(kwargs["cancel"], ctx)
        self.assertIsInstance(seen["streams"][0], HTTPStream)

    async def test_models_goes_through_httpclient_request(self) -> None:
        """``/models`` 是父类实现的非流式路径：也必须走 HTTPClient.request。"""
        server = self.start_server(lambda handler, record: write_body(handler, '{"data":[]}'))
        client = self.make_responses(server.url, "test-key")
        seen = self.spy_request(client)

        self.assertEqual(await client.list_models(None), [])
        args, kwargs = seen["calls"][0]
        self.assertEqual(args, ("GET", client.models_endpoint()))
        self.assertIsNone(kwargs.get("retry"))


class TestResponsesReasoningEffort(_ResponsesCase):
    """chat 风格的顶层 ``reasoning_effort`` 必须折进 Responses 的 ``reasoning.effort``。

    回归：严格校验的网关（实测 opencode.ai）对 ``/responses`` 里的顶层
    ``reasoning_effort`` 直接回 400——``reasoning_effort: requires a native
    Responses target``；OpenAI 官方的 Responses 协议同样只认 ``reasoning.effort``。
    """

    async def _body(
        self,
        extra_payload: Mapping[str, Any] | None = None,
        model_extra: Mapping[str, Mapping[str, Any]] | None = None,
        **request_kwargs: Any,
    ) -> tuple[dict[str, Any], ResponsesAdapter]:
        server = self.responses_server(
            ['data: {"type":"response.completed","response":{"status":"completed"}}']
        )
        client = self.make_responses(server.url, "test-key", extra_payload, model_extra)
        await self.collect(client.chat_stream(None, self.request(**request_kwargs)))
        return server.last_body(), client

    async def test_provider_level_effort_becomes_reasoning_effort_object(self) -> None:
        body, _ = await self._body({"reasoning_effort": "high"})
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["reasoning"], {"effort": "high"})

    async def test_model_level_effort_overrides_provider_level(self) -> None:
        provider_low = {"reasoning_effort": "low"}
        model_high = {"test": {"reasoning_effort": "high"}}
        body, _ = await self._body(provider_low, model_high)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        # 没有模型级覆盖的模型仍用 Provider 级档位。
        other, _ = await self._body(provider_low, model_high, model="other")
        self.assertEqual(other["reasoning"], {"effort": "low"})

    async def test_explicit_reasoning_object_wins_over_dedicated_field(self) -> None:
        body, _ = await self._body(
            {"reasoning": {"effort": "low", "summary": "auto"}, "reasoning_effort": "high"}
        )
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["reasoning"], {"effort": "low", "summary": "auto"})

    async def test_effort_is_added_when_explicit_reasoning_lacks_it(self) -> None:
        body, _ = await self._body(
            {"reasoning": {"summary": "auto"}, "reasoning_effort": "high"}
        )
        self.assertEqual(body["reasoning"], {"summary": "auto", "effort": "high"})

    async def test_non_object_reasoning_keeps_explicit_value(self) -> None:
        body, _ = await self._body({"reasoning": "auto", "reasoning_effort": "high"})
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["reasoning"], "auto")

    async def test_extra_payload_is_not_mutated(self) -> None:
        """折叠只改本次请求体，不能把 effort 写回可复用的 provider 配置。"""
        payload = {"reasoning": {"summary": "auto"}, "reasoning_effort": "high"}
        _, client = await self._body(payload)
        self.assertEqual(client.extra_payload, {"reasoning": {"summary": "auto"}, "reasoning_effort": "high"})

    async def test_empty_effort_is_not_sent_at_all(self) -> None:
        body, _ = await self._body({"reasoning_effort": ""})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("reasoning", body)

    async def test_request_extra_body_effort_is_folded_too(self) -> None:
        body, _ = await self._body(
            None, None, extra_body={"reasoning_effort": "minimal"}
        )
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["reasoning"], {"effort": "minimal"})

    def test_fold_reasoning_effort_is_idempotent(self) -> None:
        client = ResponsesAdapter("https://example.invalid/v1", "k")
        body: dict[str, Any] = {}
        self.assertEqual(client.fold_reasoning_effort(body), [])
        body = {"reasoning_effort": "high"}
        self.assertEqual(client.fold_reasoning_effort(body), [])
        self.assertEqual(body, {"reasoning": {"effort": "high"}})
        self.assertEqual(client.fold_reasoning_effort(body), [])


if __name__ == "__main__":
    unittest.main()
