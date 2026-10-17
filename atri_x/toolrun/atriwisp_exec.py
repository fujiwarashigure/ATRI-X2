"""Atriwisp 工具的 HTTP 执行。

对应 Go：``internal/toolrun/atriwisp_exec.go``。

协议（hook.v2 之外的 Atriwisp 工具 RPC，以 Go 为准）：

- 请求：``POST <endpoint>``，JSON ``{"tool": <name>, "event_key": <key>, "arguments": <raw>}``
- 响应：JSON 对象，按 ``content`` → ``text`` → ``result`` 顺序取第一个非空值；
  若解析不出 JSON，则**整个响应体**就是结果。
- ``error`` 字段非空时视为工具失败。
- 非 2xx 视为失败，错误里带上状态码与响应体。
- 超时取声明的 ``timeout_seconds``，缺失或非正数时默认 10 秒。
- 响应体读取上限 1 MiB（对应 Go 的 ``io.LimitReader(resp.Body, 1<<20)``）。
- 参数**不是合法 JSON** 时**本地失败**，不发出任何请求 —— 对应 Go 的
  ``json.Marshal`` 在嵌入 ``json.RawMessage`` 时用 ``appendCompact`` 校验字节，
  非法即返回 ``*json.MarshalerError``，``executionError`` 直接结束这次调用。
- 参数是合法 JSON 时**按原文嵌入**（只去字符串外的空白 + HTML 转义，不重排数字、
  不解码 ``\\uXXXX``），与 Go 的 ``appendCompact`` 写出的字节一致。
"""

from __future__ import annotations

import json
from typing import Any

from atri_x.llm.llm import LLMMessage, Role, ToolCallRequest, text_segments
from atri_x.net.httpclient import HTTPClient, HTTPError
from atri_x.tool.tool import Result

#: 默认超时（秒）。对应 Go 的 ``10 * time.Second``。
DEFAULT_ATRIWISP_TIMEOUT_SECONDS = 10.0

#: 响应体读取上限（字节）。对应 Go 的 ``1 << 20``。
ATRIWISP_MAX_RESPONSE_BYTES = 1 << 20

#: 对应 Go ``appendCompact`` 的 ``escapeHTML`` 转义表。
_HTML_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


def execute_atriwisp_tool(
    ctx: Any, call: ToolCallRequest, resolved: Any
) -> tuple[LLMMessage, Result | None, BaseException | None]:
    """执行一个 Atriwisp 工具。

    对应 Go ``executeAtriwispTool``。返回 ``(message, result, err)`` —— Go 返回
    ``ExecutionResult``，这里保持同样的"失败也带消息"语义。
    """
    message = LLMMessage(role=Role.TOOL, name=call.name, tool_call_id=call.id)
    cached = getattr(resolved, "cached", None)
    if cached is None:
        return _execution_error(call, message, ValueError("Atriwisp tool metadata is missing"))

    timeout = float(getattr(cached, "timeout_seconds", 0) or 0)
    if timeout <= 0:
        timeout = DEFAULT_ATRIWISP_TIMEOUT_SECONDS

    arguments = (call.arguments or "").strip() or "{}"
    # Go 的 ``json.Marshal(atriwispToolRequest{...})`` 会校验内嵌的 RawMessage：
    # 参数不是合法 JSON 时**在本地**就失败（``atriwisp_exec.go:47-50``），
    # 不会发出 HTTP 请求。顺序也很重要：校验发生在建请求之前。
    arguments_error = _raw_arguments_error(arguments)
    if arguments_error is not None:
        return _execution_error(call, message, arguments_error)

    body = _encode_tool_request(
        getattr(cached, "name", "") or "",
        getattr(cached, "event_key", "") or "",
        _compact_raw_arguments(arguments),
    )

    endpoint = getattr(cached, "endpoint", "") or ""
    if not endpoint:
        return _execution_error(call, message, ValueError("Atriwisp tool endpoint is missing"))

    return _run_http(call, message, endpoint, body, timeout)


def _run_http(
    call: ToolCallRequest,
    message: LLMMessage,
    endpoint: str,
    body: bytes,
    timeout: float,
) -> tuple[LLMMessage, Result | None, BaseException | None]:
    """同步执行（调用方在需要时用 ``asyncio.to_thread`` 包一层）。"""
    import http.client
    import urllib.parse

    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme not in ("http", "https"):
        return _execution_error(call, message, ValueError(f"unsupported endpoint: {endpoint}"))
    try:
        conn_cls = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), timeout=timeout)
        try:
            target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            conn.request(
                "POST",
                target,
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            raw = resp.read(ATRIWISP_MAX_RESPONSE_BYTES)
            status = resp.status
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as exc:
        return _execution_error(call, message, HTTPError(f"Atriwisp tool request failed: {exc}", cause=exc))

    text = raw.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        return _execution_error(
            call, message, ValueError(f"Atriwisp tool HTTP {status}: {text.strip()}")
        )

    content = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        error_text = str(parsed.get("error") or "").strip()
        if error_text:
            return _execution_error(
                call, message, ValueError(f"Atriwisp tool error: {error_text}")
            )
        content = first_atriwisp_result(
            str(parsed.get("content") or ""),
            str(parsed.get("text") or ""),
            str(parsed.get("result") or ""),
            content,
        )
    result = Result(content=content)
    message.segments = result.llm_segments()
    return message, result, None


def _raw_arguments_error(arguments: str) -> BaseException | None:
    """校验 ``arguments`` 是不是合法 JSON；非法时返回 Go 会返回的那个错误。

    对应 Go ``atriwisp_exec.go:43-50``：``json.Marshal`` 逐字段编码时，``json.RawMessage``
    走 ``marshalerEncoder`` → ``appendCompact(out, b, true)``，**字节非法即返回
    ``*json.MarshalerError``**（``"json: error calling MarshalJSON for type
    json.RawMessage: " + 语法错误``），``executeAtriwispTool`` 随即 ``executionError``
    结束调用 —— 没有 HTTP 请求，也没有远端响应。

    错误文本：外层与 Go 逐字一致；内层语法细节取自 Python 解码器（Go 用的是
    ``encoding/json`` scanner 的 ``SyntaxError`` 文本，措辞不同、语义相同）。

    Go 的 scanner 只认 RFC 7159 的字面量，**不接受** ``NaN`` / ``Infinity`` /
    ``-Infinity``（Python 的 ``json`` 默认接受），这里用 ``parse_constant`` 拒掉。
    """
    try:
        json.loads(arguments, parse_constant=_reject_json_constant)
    except ValueError as exc:
        return ValueError(f"json: error calling MarshalJSON for type json.RawMessage: {exc}")
    return None


def _reject_json_constant(name: str) -> Any:
    """对应 Go scanner 对 ``NaN`` / ``Infinity`` 的处理：报 "looking for beginning of value"。"""
    raise ValueError(f"invalid character {name[0]!r} looking for beginning of value")


def _encode_tool_request(tool: str, event_key: str, arguments: str) -> bytes:
    """拼出 Go ``json.Marshal(atriwispToolRequest{...})`` 写出的请求体。

    字段顺序与 Go 的结构体声明一致：``tool`` → ``event_key``（``omitempty``）→
    ``arguments``；``arguments`` 是 ``json.RawMessage``，**原样嵌入**。

    与 Go 的已知差异：``tool`` / ``event_key`` 这两个**键名与取值**不做 HTML 转义
    （Go 会把 ``<`` / ``>`` / ``&`` 写成 ``\\u003c`` 等），两者是等价的 JSON 文本。
    """
    parts = ['{"tool":', json.dumps(tool, ensure_ascii=False)]
    if event_key:
        parts.append(',"event_key":')
        parts.append(json.dumps(event_key, ensure_ascii=False))
    parts.append(',"arguments":')
    parts.append(arguments)
    parts.append("}")
    return "".join(parts).encode("utf-8")


def _compact_raw_arguments(text: str) -> str:
    """把**已校验**的 JSON 文本压成 Go ``appendCompact`` 写出的形态。

    对应 Go ``encoding/json.appendCompact``（``escapeHTML=true``）：

    - 去掉字符串字面量之外的空白；
    - ``<`` / ``>`` / ``&`` → ``\\u003c`` / ``\\u003e`` / ``\\u0026``；
    - 裸的 U+2028 / U+2029 → ``\\u2028`` / ``\\u2029``；
    - 字符串内部**原样保留**：``1e2`` 不会被改写成 ``100.0``、``\\u0041`` 不会被解成
      ``A``（这正是 Go 用 RawMessage 的意义）。

    调用方必须先用 :func:`_raw_arguments_error` 校验过，否则转义状态可能失配。
    """
    out: list[str] = []
    start = 0
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char in " \t\n\r":
            out.append(text[start:index])
            index += 1
            start = index
            continue
        index += 1
    out.append(text[start:])
    return "".join(_HTML_ESCAPES.get(char, char) for char in "".join(out))


def first_atriwisp_result(*values: str) -> str:
    """取第一个非空值。对应 Go ``firstAtriwispResult``。"""
    for value in values:
        if (value or "").strip():
            return value.strip()
    return ""


def _execution_error(
    call: ToolCallRequest, message: LLMMessage, err: BaseException
) -> tuple[LLMMessage, Result | None, BaseException | None]:
    """对应 Go ``executionError``。"""
    message.segments = text_segments(f"tool call {call.name} failed: {err}")
    return message, None, err


# Go 的导出名
ExecuteAtriwispTool = execute_atriwisp_tool
FirstAtriwispResult = first_atriwisp_result

__all__ = [
    "ATRIWISP_MAX_RESPONSE_BYTES",
    "DEFAULT_ATRIWISP_TIMEOUT_SECONDS",
    "ExecuteAtriwispTool",
    "FirstAtriwispResult",
    "execute_atriwisp_tool",
    "first_atriwisp_result",
]
