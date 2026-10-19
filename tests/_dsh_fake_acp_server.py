#!/usr/bin/env python3
"""离线 ACP 服务端桩：给 ``tests/test_dsh_acp_client.py`` 提供可脚本化的 NDJSON 对端。

只实现测试用到的 ACP v1 子集：``initialize`` / ``authenticate`` / ``session/new`` /
``session/list`` / ``session/resume`` / ``session/set_config_option`` /
``session/prompt`` / ``session/close``。

``session/prompt`` 的文本里带以下标记会触发不同行为：

- ``__PERMISSION__``：先发 ``session/request_permission`` 请求，把客户端应答回显在文本里；
- ``__UNKNOWN__``：先发一个客户端不认识的请求（``fs/read_text_file``），回显其错误码；
- ``__HANG__``：不回本轮响应，等 ``session/cancel`` 到达后再以 ``cancelled`` 收尾；
- ``__CRASH__``：``os._exit(3)`` 模拟服务端崩溃；
- ``__CRASH_AFTER_CHUNK__``：先流出一段正文再 ``os._exit(3)``（模拟"有部分输出的崩溃"）；
- ``__NO_USAGE__``：以 ``end_turn`` 收尾但**不带** ``usage`` 字段（DSH 内核的真实行为，
  用于验证"内核不回 usage → 记账按不确定处理"）。

其余情况正常流式返回两段文本 + ``tool_call`` + ``usage_update`` + ``usage``。

``session/set_config_option`` 默认接受任何 ``model`` 值（老测试依赖这个宽松口径）；
设 ``FAKE_ACP_STRICT_MODEL=1`` 后只接受公布过的 ``model`` 取值，其余回
``-32602 unknown model option`` —— 复现"用户把 ``[agent.dsh].model`` 填成裸模型 id"
时内核的拒绝。

``FAKE_ACP_STDERR_SECRET`` 非空时，像真实内核那样在 startup 往 stderr 打一行带凭据的
日志（``api_key=<值>``），用于验证报错信息带出去之前先脱敏。
"""

from __future__ import annotations

import json
import os
import sys

_MODEL = "fake-model"


def _send(payload: dict) -> None:
    """写一帧 NDJSON。"""
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _read() -> dict | None:
    """读一帧；EOF 返回 ``None``。"""
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    return json.loads(line.decode("utf-8"))


def _notify(session_id: str, update: dict) -> None:
    """发一条 ``session/update`` 通知。"""
    _send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": update}})


def _text_chunk(session_id: str, text: str) -> None:
    """发一段 assistant 正文。"""
    _notify(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


def _config_options(current: str) -> list[dict]:
    """公布的可配置项：与 DSH 一样只有 model / reasoning_effort 两个下拉。"""
    return [
        {
            "id": "model",
            "name": "Model",
            "type": "select",
            "currentValue": current,
            "options": [{"value": _MODEL, "name": "Fake Model"}],
        },
        {
            "id": "reasoning_effort",
            "name": "Reasoning effort",
            "type": "select",
            "currentValue": "low",
            "options": [{"value": "low", "name": "Low"}, {"value": "high", "name": "High"}],
        },
    ]


def _option_values(config_id: str) -> set[str]:
    """某项公布过的取值（严格模式下只接受这些）。"""
    item = next(
        (entry for entry in _config_options(_MODEL) if entry.get("id") == config_id),
        None,
    )
    options = item.get("options") if isinstance(item, dict) else None
    return {str(option.get("value")) for option in (options or [])}


def _response(result: dict, request_id: object) -> None:
    """回一个成功结果。"""
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(message: str, request_id: object, code: int = -32601) -> None:
    """回一个错误。"""
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _await_result(request_id: int) -> dict:
    """读帧直到等到 ``request_id`` 的响应；把结果原样返回。"""
    while True:
        message = _read()
        if message is None:
            return {"error": {"code": -32000, "message": "client closed stdin"}}
        if message.get("id") == request_id and "method" not in message:
            return message


def _finish_prompt(session_id: str, request_id: object, usage: bool = True) -> None:
    """以 ``end_turn`` 收尾一轮，并按需带 usage。"""
    result: dict = {"stopReason": "end_turn"}
    if usage:
        result["usage"] = {
            "totalTokens": 123,
            "inputTokens": 100,
            "outputTokens": 23,
            "thoughtTokens": 5,
            "cachedReadTokens": 50,
            "cachedWriteTokens": 10,
        }
    _response(result, request_id)


def main() -> int:
    """服务端主循环。"""
    # 模拟内核 startup 日志（真实 dsh 会打印 "may contain configuration or credential values"）。
    leaked = os.environ.get("FAKE_ACP_STDERR_SECRET")
    if leaked:
        sys.stderr.write(f"[fake-dsh] startup: api_key={leaked}\n")
        sys.stderr.flush()
    auth_methods: list[dict] = []
    if os.environ.get("FAKE_ACP_AUTH") == "1":
        auth_methods = [{"id": "none", "name": "No authentication"}]
    sessions: dict[str, dict] = {}
    config_value = _MODEL
    pending_prompts: dict[str, object] = {}
    request_counter = 9000
    session_counter = 0
    while True:
        message = _read()
        if message is None:
            return 0
        if not message:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method is None:
            continue
        if method == "initialize":
            protocol = int(os.environ.get("FAKE_ACP_PROTOCOL", "1"))
            _response(
                {
                    "protocolVersion": protocol,
                    "agentCapabilities": {},
                    "authMethods": auth_methods,
                    "agentInfo": {"name": "fake-dsh-acp", "version": "0.0.1"},
                },
                request_id,
            )
        elif method == "authenticate":
            _response({}, request_id)
        elif method == "session/new":
            session_counter += 1
            session_id = f"sess-{session_counter}"
            sessions[session_id] = {"cwd": params.get("cwd"), "mcpServers": params.get("mcpServers")}
            _response({"sessionId": session_id, "configOptions": _config_options(config_value)}, request_id)
        elif method == "session/resume":
            session_id = str(params.get("sessionId") or "sess-1")
            sessions[session_id] = {"cwd": params.get("cwd"), "mcpServers": params.get("mcpServers")}
            _response({"sessionId": session_id, "configOptions": _config_options(config_value)}, request_id)
        elif method == "session/list":
            _response(
                {"sessions": [{"sessionId": key, "cwd": value.get("cwd")} for key, value in sessions.items()]},
                request_id,
            )
        elif method == "session/set_config_option":
            config_id = str(params.get("configId") or "")
            value = str(params.get("value"))
            if os.environ.get("FAKE_ACP_STRICT_MODEL") == "1" and value not in _option_values(config_id):
                _error(f"unknown {config_id} option: {value}", request_id, code=-32602)
                continue
            if config_id == "model":
                config_value = value
            _response({"configOptions": _config_options(config_value)}, request_id)
        elif method == "session/close":
            sessions.pop(str(params.get("sessionId")), None)
            _response({}, request_id)
        elif method == "session/prompt":
            session_id = str(params.get("sessionId"))
            blocks = params.get("prompt") or []
            text = "".join(
                str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
            )
            if "__CRASH__" in text:
                sys.stdout.buffer.flush()
                os._exit(3)
            if "__CRASH_AFTER_CHUNK__" in text:
                _text_chunk(session_id, "部分输出后崩溃")
                sys.stdout.buffer.flush()
                os._exit(3)
            if "__HANG__" in text:
                pending_prompts[session_id] = request_id
                continue
            if "__NO_USAGE__" in text:
                _text_chunk(session_id, "没有 usage 的回答")
                _finish_prompt(session_id, request_id, usage=False)
                continue
            if "__PERMISSION__" in text:
                request_counter += 1
                permission_id = request_counter
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": permission_id,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": session_id,
                            "toolCall": {"toolCallId": "t1", "title": "write file"},
                            "options": [
                                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                            ],
                        },
                    }
                )
                answer = _await_result(permission_id)
                _text_chunk(session_id, "permission:" + json.dumps(answer.get("result"), ensure_ascii=False))
                _finish_prompt(session_id, request_id)
                continue
            if "__UNKNOWN__" in text:
                request_counter += 1
                unknown_id = request_counter
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": unknown_id,
                        "method": "fs/read_text_file",
                        "params": {"sessionId": session_id, "path": "/tmp/x"},
                    }
                )
                answer = _await_result(unknown_id)
                _text_chunk(session_id, "unknown:" + json.dumps(answer.get("error"), ensure_ascii=False))
                _finish_prompt(session_id, request_id)
                continue
            _text_chunk(session_id, "你好，")
            _notify(session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "想一想"}})
            _notify(
                session_id,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "t1",
                    "title": "bash",
                    "kind": "execute",
                    "status": "in_progress",
                },
            )
            _notify(session_id, {"sessionUpdate": "usage_update", "used": 123, "size": 1000})
            _text_chunk(session_id, "世界")
            _finish_prompt(session_id, request_id)
        elif method == "session/cancel":
            session_id = str(params.get("sessionId"))
            pending = pending_prompts.pop(session_id, None)
            if pending is not None:
                _response({"stopReason": "cancelled"}, pending)
        elif request_id is not None:
            _error(f"unknown method {method}", request_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
