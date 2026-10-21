"""工具调用在会话记录里的落库形态。对应 Go：``internal/agent/tool_transcript.go``。

职责：``assistantMetadata``、``toolCallStorageMessage``、``toolResultStorageMessage``、
``storedMessageSegments``、``persistedToolMessage``、``rememberDiscoveryResult`` /
``rememberActivatedTools`` 以及 ``persistTurnMessage(s)``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``*Agent`` 方法 → 本模块的模块级函数（首个参数 ``agent``，鸭子类型）；
  :class:`ToolTranscriptMixin` 只是把这些函数挂成 ``Agent`` 上的同名方法。
* ``storage.Message`` 的字段名与 Go 一致（``segments`` 以 JSON 字符串落库，
  与 Go 的 ``Segments string`` 相同），因此落库格式可互通。
* 出现 ``MediaID`` 的段落落库时清空 ``URL`` —— 与 Go 相同，避免会话记录里
  留下会过期的直链。

本模块的 ``compact_arguments`` / ``preview_arguments`` / ``first_non_empty`` 等来自
``tools.py``（该处注明各自 Go 出处），不重复定义。
"""

from __future__ import annotations

import json
from typing import Any

from atri_x.llm import LLMMessage, MessageSegment, segments_content_text
from atri_x.storage import Message
from atri_x.storage.models import RoleAssistant, RoleTool

__all__ = [
    "AssistantMetadata",
    "ToolTranscriptMixin",
    "assistant_raw_text_metadata",
    "metadata_tool_names",
    "persisted_tool_message",
    "remember_activated_tools",
    "remember_discovery_result",
    "segments_text_only",
    "stored_message_segments",
    "tool_call_storage_message",
    "tool_name_metadata",
    "tool_result_storage_message",
]


class AssistantMetadata:
    """助手消息 metadata。对应 Go ``assistantMetadata``。

    json tag：``tool_calls,omitempty`` / ``raw_text,omitempty`` / ``reasoning_content,omitempty``。

    ``reasoning_content`` 是 Python 侧追加字段（Go 版没有）：思考模式的模型在
    assistant 消息里会带思维链，携带 tools 的请求必须把历史思维链原样回传，否则
    上游 400。它只落 metadata，不进用户可见文本。
    """

    def __init__(
        self,
        tool_calls: list[Any] | None = None,
        raw_text: str = "",
        reasoning_content: str = "",
    ) -> None:
        self.tool_calls = list(tool_calls or [])
        self.raw_text = raw_text or ""
        self.reasoning_content = reasoning_content or ""

    def to_json(self) -> str:
        data: dict[str, Any] = {}
        if self.tool_calls:
            data["tool_calls"] = [
                _tool_call_json(call) for call in self.tool_calls
            ]
        if self.raw_text:
            data["raw_text"] = self.raw_text
        if self.reasoning_content:
            data["reasoning_content"] = self.reasoning_content
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "AssistantMetadata":
        if not str(raw or "").strip():
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        status = cls(
            raw_text=str(data.get("raw_text") or ""),
            reasoning_content=str(data.get("reasoning_content") or ""),
        )
        calls = data.get("tool_calls")
        if isinstance(calls, list):
            status.tool_calls = [_tool_call_from_json(item) for item in calls]
        return status


def _tool_call_json(call: Any) -> dict[str, Any]:
    to_json = getattr(call, "to_json", None)
    if callable(to_json):
        return to_json()
    data: dict[str, Any] = {}
    for key, attr in (("id", "id"), ("name", "name"), ("arguments", "arguments")):
        value = getattr(call, attr, "")
        if value:
            data[key] = value
    return data


def _tool_call_from_json(raw: Any) -> Any:
    from atri_x.llm import ToolCallRequest

    if isinstance(raw, ToolCallRequest):
        return raw
    if not isinstance(raw, dict):
        return ToolCallRequest()
    return ToolCallRequest(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        arguments=str(raw.get("arguments") or ""),
    )


def tool_call_storage_message(
    session_id: str,
    content: str,
    raw_text: str,
    calls: list[Any],
    reasoning_content: str = "",
) -> Message:
    """助手侧的"我发起了这些工具调用"记录。对应 Go ``toolCallStorageMessage``。"""
    metadata = AssistantMetadata(
        tool_calls=list(calls or []), reasoning_content=reasoning_content
    )
    if raw_text and raw_text != content:
        metadata.raw_text = raw_text
    return Message(
        session_id=session_id,
        role=RoleAssistant,
        content=content,
        metadata=metadata.to_json(),
    )


def stored_message_segments(segments: list[MessageSegment] | None) -> str:
    """落库用的 segments JSON：纯文本段落返回空串。对应 Go ``storedMessageSegments``。"""
    if not segments or segments_text_only(segments):
        return ""
    copied = [segment.clone() for segment in segments]
    for segment in copied:
        if segment.media:
            segment.url = ""
    return json.dumps(
        [segment.to_json() for segment in copied], ensure_ascii=False, separators=(",", ":")
    )


def segments_text_only(segments: list[MessageSegment] | None) -> bool:
    """所有段落都是文本。对应 Go ``segmentsTextOnly``。"""
    for segment in segments or ():
        if segment.type != "text":
            return False
    return True


def assistant_raw_text_metadata(
    content: str, raw_text: str, reasoning_content: str = ""
) -> str:
    """助手 metadata：保留原始文本（与正文不同时）与思维链。

    对应 Go ``assistantRawTextMetadata``；``reasoning_content`` 是 Python 侧追加：
    思考模式的历史思维链必须能在下一轮请求里原样回传，因此与 raw_text 一起落
    metadata（不进用户可见正文）。
    """
    keep_raw = bool(raw_text) and raw_text != content
    if not keep_raw and not reasoning_content:
        return ""
    return AssistantMetadata(
        raw_text=raw_text if keep_raw else "",
        reasoning_content=reasoning_content,
    ).to_json()


def tool_result_storage_message(session_id: str, message: LLMMessage) -> Message:
    """工具结果落库。对应 Go ``toolResultStorageMessage``。"""
    return Message(
        session_id=session_id,
        role=RoleTool,
        content=segments_content_text(message.segments),
        tool_call_id=message.tool_call_id,
        segments=stored_message_segments(message.segments),
        metadata=tool_name_metadata(message.name),
    )


def tool_name_metadata(name: str) -> str:
    """``{"name": ...}``。对应 Go ``toolNameMetadata``。"""
    if not name:
        return ""
    return json.dumps({"name": name}, ensure_ascii=False, separators=(",", ":"))


def persisted_tool_message(message: LLMMessage) -> LLMMessage:
    """落库前的精简：``discover_tool`` 只留"发现了哪些工具"。对应 Go 同名函数。"""
    content = segments_content_text(message.segments)
    if message.name != "discover_tool" or not content:
        return message
    try:
        result = json.loads(content)
    except ValueError:
        return message
    names: list[str] = []
    tools = result.get("tools") if isinstance(result, dict) else None
    for discovered in tools or ():
        info = discovered.get("info") if isinstance(discovered, dict) else None
        name = str((info or {}).get("name") or "")
        if name:
            names.append(name)
    from atri_x.llm import text_segments

    message.segments = text_segments("discover_tool found tools: %s" % join_names(names))
    return message


def join_names(names: list[str] | None) -> str:
    """对应 Go ``joinNames``。"""
    values = list(names or [])
    if not values:
        return "none"
    return ", ".join(values)


async def remember_discovery_result(agent: Any, ctx: Any, session: Any, result: Any) -> None:
    """记住发现结果与激活工具。对应 Go ``rememberDiscoveryResult``。"""
    if result is None:
        return
    data = getattr(result, "data", None)
    if data:
        try:
            raw = json.loads(data)
        except ValueError:
            raw = None
        if isinstance(raw, dict):
            await _maybe_await(
                _remember_discovered_tools(agent, ctx, session, _discovery_from_json(raw))
            )
    metadata = getattr(result, "metadata", None) or {}
    await remember_activated_tools(agent, ctx, session, metadata)
    from atri_x.tool.tool import METADATA_SHOWN_RULE_CARD_FORMATS

    formats = metadata_tool_names(metadata.get(METADATA_SHOWN_RULE_CARD_FORMATS))
    if formats:
        helper = getattr(agent, "persist_shown_rule_card_formats", None)
        if helper is None:
            from atri_x.agent.tool_cache import persist_shown_rule_card_formats

            helper = persist_shown_rule_card_formats
            await _maybe_await(helper(agent, ctx, session, formats))
        else:
            await _maybe_await(helper(ctx, session, formats))


async def remember_activated_tools(
    agent: Any, ctx: Any, session: Any, metadata: dict[str, Any] | None
) -> None:
    """把工具结果 metadata 里声明的工具补进发现缓存。对应 Go ``rememberActivatedTools``。"""
    if not metadata or session is None:
        return
    from atri_x.tool import (
        METADATA_ACTIVATE_TOOLS,
        DiscoveryResult,
        DiscoveredTool,
        PublicInfo,
        info_available_in_context,
    )

    state = _tool_runtime_state(agent)
    if state is None or state.registry is None:
        return
    names = metadata_tool_names(metadata.get(METADATA_ACTIVATE_TOOLS))
    if not names:
        return
    policy = getattr(agent, "security_policy", None)
    if policy is None:
        from atri_x.security import DefaultPolicy

        policy = DefaultPolicy()
    actor = _actor(agent, ctx)
    discovery = DiscoveryResult()
    for name in names:
        target, ok = state.registry.get(name)
        if not ok or target is None:
            continue
        info = target.info()
        risk = info.risk or "high"
        if not info_available_in_context(ctx, info) or not policy.can_use_tool(
            actor, risk, bool(getattr(info, "owner_scoped", False))
        ):
            continue
        discovery.tools.append(
            DiscoveredTool(
                info=PublicInfo(
                    name=name,
                    description=info.description,
                    source=str(info.source),
                    foreground_only=bool(getattr(info, "foreground_only", False)),
                ),
                schema=target.schema(),
            )
        )
    await _remember_discovered_tools(agent, ctx, session, discovery)


def metadata_tool_names(value: Any) -> list[str]:
    """宽松解析 metadata 里的字符串列表。对应 Go ``metadataToolNames``。"""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, str) and item]
    return []


async def persist_turn_message(agent: Any, ctx: Any, message: Message, operation: str) -> None:
    """落库一条会话消息。对应 Go ``persistTurnMessage``。"""
    history = getattr(agent, "history_enabled", None)
    if history is not None and not history(ctx):
        # history=off 只表示"不再写新的持久记录"，不删已有行。
        return
    media = getattr(agent, "media", None)
    if media is not None and getattr(message, "segments", ""):
        materialize = getattr(agent, "materialize_media", None)
        if materialize is not None:
            from atri_x.storage import parse_segments

            segments = await _maybe_await(
                materialize(ctx, parse_segments(message.segments))
            )
            message.segments = stored_message_segments(list(segments or []))
            message.content = segments_content_text(list(segments or []))
    store = getattr(agent, "store", None)
    if store is None:
        return
    try:
        await _maybe_await(store.messages().append(message))
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：记账后把错误交回调用方
        audit = getattr(agent, "audit", None)
        if audit is not None:
            audit(
                "persistence_error",
                "session_id",
                message.session_id,
                "operation",
                operation,
                "error",
                str(exc),
                "result",
                "failed",
            )
        raise


async def persist_turn_messages(
    agent: Any, ctx: Any, session_id: str, operation: str, messages: list[Message]
) -> None:
    """批量落库。对应 Go ``persistTurnMessages``。"""
    for message in messages:
        message.session_id = session_id
        await persist_turn_message(agent, ctx, message, operation)


class ToolTranscriptMixin:
    """Go ``*Agent`` 的会话记录方法组。"""

    async def remember_discovery_result(self, ctx: Any, session: Any, result: Any) -> None:
        await remember_discovery_result(self, ctx, session, result)

    async def remember_activated_tools(
        self, ctx: Any, session: Any, metadata: dict[str, Any] | None
    ) -> None:
        await remember_activated_tools(self, ctx, session, metadata)

    async def persist_turn_message(self, ctx: Any, message: Message, operation: str) -> None:
        await persist_turn_message(self, ctx, message, operation)

    async def persist_turn_messages(
        self, ctx: Any, session_id: str, operation: str, messages: list[Message]
    ) -> None:
        await persist_turn_messages(self, ctx, session_id, operation, messages)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _tool_runtime_state(agent: Any) -> Any:
    from atri_x.agent.tools import tool_runtime_state

    return tool_runtime_state(agent)


def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is None:
        from atri_x.security import Actor

        return Actor()
    return helper(ctx)


async def _remember_discovered_tools(
    agent: Any, ctx: Any, session: Any, discovery: Any
) -> None:
    helper = getattr(agent, "remember_discovered_tools", None)
    if helper is not None:
        await _maybe_await(helper(ctx, session, discovery))
        return
    from atri_x.agent.tool_cache import remember_discovered_tools

    await _maybe_await(remember_discovered_tools(agent, ctx, session, discovery))


def _discovery_from_json(raw: dict[str, Any]) -> Any:
    """把 ``tool.Result.Data`` 里的发现结果解析成 ``DiscoveryResult``。"""
    from atri_x.llm import ToolSchema, ToolFunctionSchema
    from atri_x.tool.tool import (
        DiscoveredTool,
        DiscoveryError,
        DiscoveryResult,
        PublicInfo,
    )

    result = DiscoveryResult()
    for item in raw.get("tools") or ():
        if not isinstance(item, dict):
            continue
        info_raw = item.get("info") if isinstance(item.get("info"), dict) else {}
        schema = None
        schema_raw = item.get("schema")
        if isinstance(schema_raw, dict):
            function_raw = (
                schema_raw.get("function") if isinstance(schema_raw.get("function"), dict) else {}
            )
            schema = ToolSchema(
                type=str(schema_raw.get("type") or "function"),
                function=ToolFunctionSchema(
                    name=str(function_raw.get("name") or ""),
                    description=str(function_raw.get("description") or ""),
                    parameters=function_raw.get("parameters") or {},
                ),
            )
        result.tools.append(
            DiscoveredTool(
                info=PublicInfo(
                    name=str(info_raw.get("name") or ""),
                    description=str(info_raw.get("description") or ""),
                    source=str(info_raw.get("source") or ""),
                    tags=[str(tag) for tag in (info_raw.get("tags") or ())],
                    foreground_only=bool(info_raw.get("foreground_only")),
                ),
                schema=schema,
                detail=str(item.get("detail") or ""),
            )
        )
    for item in raw.get("errors") or ():
        if not isinstance(item, dict):
            continue
        result.errors.append(
            DiscoveryError(
                name=str(item.get("name") or ""), reason=str(item.get("reason") or "")
            )
        )
    return result
