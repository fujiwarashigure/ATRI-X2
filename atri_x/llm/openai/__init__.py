"""OpenAI-compatible 适配器包。

对应 Go：``internal/llm/openai``（``package openai``）。对外入口：

* :class:`atri_x.llm.openai.openai.OpenAIClient`（Go ``Adapter``）：Chat
  Completions，``{base_url}/chat/completions``；
* :class:`atri_x.llm.openai.responses.ResponsesAdapter`（Go ``ResponsesAdapter``）：
  Responses 协议，``{base_url}/responses``，内嵌前者以复用重试、``/models``、
  SSE 行扫描与 ``parse_error``。

包内拆分（移植约定的「单文件 ≤ 600 行」）：``wire`` 请求信封与解析 helper、
``errors`` 错误分类、``stream`` SSE 行扫描与超时——都对应 Go 同一个
``openai.go``/``responses.go`` 的不同小节。
"""

from __future__ import annotations

from atri_x.llm.openai.errors import (  # noqa: F401
    HTML_RESPONSE_HINT,
    classify,
    looks_like_html,
    looks_like_sse,
    parse_error,
    response_summary,
)
from atri_x.llm.openai.openai import Adapter, OpenAIClient  # noqa: F401
from atri_x.llm.openai.responses import (  # noqa: F401
    RESPONSES_RESERVED_REQUEST_FIELDS,
    OpenAIResponsesClient,
    ResponsesAdapter,
    to_responses_input,
    to_responses_tools,
)
from atri_x.llm.openai.stream import AwaitableStream, StreamLineReader  # noqa: F401
from atri_x.llm.openai.wire import (  # noqa: F401
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
    to_openai_messages,
    to_openai_tools,
    to_usage,
)

__all__ = [
    "Adapter",
    "AwaitableStream",
    "CHAT_RESERVED_REQUEST_FIELDS",
    "DATA_DONE",
    "DATA_PREFIX",
    "DEFAULT_FIRST_CHUNK_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_INITIAL_DELAY",
    "DEFAULT_STREAM_IDLE_TIMEOUT",
    "HTML_RESPONSE_HINT",
    "MAX_STREAM_LINE_BYTES",
    "OpenAIClient",
    "OpenAIResponsesClient",
    "RESPONSES_RESERVED_REQUEST_FIELDS",
    "RequestOptions",
    "ResponsesAdapter",
    "STREAM_PREFIX_BYTES",
    "StreamLineReader",
    "classify",
    "looks_like_html",
    "looks_like_sse",
    "parse_error",
    "response_summary",
    "to_openai_messages",
    "to_openai_tools",
    "to_responses_input",
    "to_responses_tools",
    "to_usage",
]
