"""``hook.Event`` 与其负载的 JSON 投影。

对应 Go：``internal/hook/event.go`` 等结构体上的 struct tag。

Go 侧有 ``encoding/json`` + struct tag，序列化是自动的；Python 的数据类没有
这条通路，而 ``atri_x/hook/event.py`` 是冻结契约（不允许改），因此投影集中写在
这里。**键名逐字照抄 Go 的 struct tag**，未打 tag 的结构体按 Go 的默认规则
（导出字段名原样）输出，``omitempty`` 逐字段复刻。
"""

from __future__ import annotations

import base64 as _base64
import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from atri_x.delivery import delivery
from atri_x.hook.event import (
    ActorContext,
    Control,
    ErrorPayload,
    Event,
    LLMPayload,
    MessagePayload,
    MessageReplyPayload,
    PlatformContext,
    RequestContext,
    SessionContext,
    ToolPayload,
)
from atri_x.hook.match import MatchContext, RegexMatch
from atri_x.llm import llm

__all__ = [
    "ZERO_TIME_JSON",
    "RawJSON",
    "time_to_json",
    "jsonable",
    "dumps",
    "event_to_json",
    "event_from_json",
    "match_context_to_json",
    "match_context_from_json",
    "regex_match_to_json",
    "regex_match_from_json",
    "output_to_json",
    "source_to_json",
    "target_to_json",
    "llm_message_to_json",
    "llm_message_from_json",
    "usage_to_json",
    "tool_schema_to_json",
]

#: Go 零值 ``time.Time`` 的 JSON 形态（``time.Time.MarshalJSON``）。
ZERO_TIME_JSON = "0001-01-01T00:00:00Z"


def time_to_json(value: datetime | None) -> str:
    """``time.Time`` → RFC3339 文本（零值输出 Go 的零值形态，不是空串）。"""
    if value is None:
        return ZERO_TIME_JSON
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        # 契约要求带时区；缺失时按 UTC 解释，绝不静默用本地时间。
        value = value.replace(tzinfo=timezone.utc)
    moment = value.astimezone(timezone.utc)
    if moment.year == 1 and moment.month == 1 and moment.day == 1:
        return ZERO_TIME_JSON
    out = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        out += "." + f"{moment.microsecond:06d}".rstrip("0")
    return out + "Z"


def jsonable(value: Any) -> Any:
    """把任意值收敛成 ``json.dumps`` 能处理的对象。

    ``event.metadata`` 是 ``map[string]any``，里面可能有 :class:`MatchContext`
    这类 Go 结构体（Go 会自动序列化），这里逐个识别。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return _base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime):
        return time_to_json(value)
    if isinstance(value, MatchContext):
        return match_context_to_json(value)
    if isinstance(value, RegexMatch):
        return regex_match_to_json(value)
    if isinstance(value, Event):
        return event_to_json(value)
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [jsonable(item) for item in items]
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return jsonable(to_json())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    return str(value)


# ---------------------------------------------------------------------------
# 匹配上下文（metadata["match"]）
# ---------------------------------------------------------------------------
def regex_match_to_json(value: RegexMatch) -> dict[str, Any]:
    """对应 Go ``RegexMatch`` 的 json tag。"""
    out: dict[str, Any] = {
        "field": value.field,
        "value": value.value,
        "text": value.text,
        "groups": list(value.groups),
    }
    if value.named:
        out["named"] = dict(value.named)
    out["start"] = value.start
    out["end"] = value.end
    return out


def regex_match_from_json(raw: Any) -> RegexMatch:
    if not isinstance(raw, dict):
        return RegexMatch()
    groups = raw.get("groups")
    named = raw.get("named")
    return RegexMatch(
        field=str(raw.get("field") or ""),
        value=str(raw.get("value") or ""),
        text=str(raw.get("text") or ""),
        groups=[str(item) for item in groups] if isinstance(groups, list) else [],
        named=(
            {str(key): str(item) for key, item in named.items()}
            if isinstance(named, dict)
            else None
        ),
        start=int(raw.get("start") or 0),
        end=int(raw.get("end") or 0),
    )


def match_context_to_json(value: MatchContext | None) -> dict[str, Any]:
    """对应 Go ``MatchContext``（``regex`` 是 ``omitempty``）。"""
    out: dict[str, Any] = {}
    if value is not None and value.regex:
        out["regex"] = [regex_match_to_json(item) for item in value.regex]
    return out


def match_context_from_json(raw: Any) -> MatchContext:
    if not isinstance(raw, dict) or not isinstance(raw.get("regex"), list):
        return MatchContext()
    return MatchContext(regex=[regex_match_from_json(item) for item in raw["regex"]])


# ---------------------------------------------------------------------------
# 交付输出（``outputs``）
# ---------------------------------------------------------------------------
def target_to_json(target: delivery.Target) -> dict[str, Any]:
    """``delivery.Target`` **没有** json tag，Go 输出全部字段。"""
    return {
        "Platform": target.platform,
        "ScopeID": target.scope_id,
        "PrivateUserID": target.private_user_id,
        "GroupID": target.group_id,
        "Superadmins": target.superadmins,
    }


def source_to_json(source: delivery.Source) -> dict[str, Any]:
    """``delivery.Source``：只有 ``MediaID`` 带 ``json:"media,omitempty"``。"""
    out: dict[str, Any] = {"URL": source.url, "Path": source.path}
    if source.media_id:
        out["media"] = source.media_id
    out["MIMEType"] = source.mime_type
    # Go 的 ``[]byte``：nil → null，空切片 → ""。
    out["Data"] = (
        _base64.b64encode(source.data).decode("ascii") if source.data else None
    )
    return out


def output_to_json(out: delivery.Output) -> dict[str, Any]:
    """``delivery.Output`` **没有** json tag（字段名原样、无 omitempty）。"""
    return {
        "Kind": out.kind,
        "Text": out.text,
        "Name": out.name,
        "EmoticonID": out.emoticon_id,
        "AltText": out.alt_text,
        "ReplyToPlatformMessageID": out.reply_to_platform_message_id,
        "Source": source_to_json(out.source),
        "Target": target_to_json(out.target),
        "Meta": jsonable(out.meta) if out.meta else None,
    }


# ---------------------------------------------------------------------------
# LLM 负载
# ---------------------------------------------------------------------------
def usage_to_json(usage: llm.Usage) -> dict[str, Any]:
    """``llm.Usage`` 没有 json tag。"""
    return {
        "PromptTokens": usage.prompt_tokens,
        "CompletionTokens": usage.completion_tokens,
        "TotalTokens": usage.total_tokens,
        "CacheHitTokens": usage.cache_hit_tokens,
    }


def usage_from_json(raw: Any) -> llm.Usage | None:
    if not isinstance(raw, dict):
        return None
    return llm.Usage(
        prompt_tokens=int(raw.get("PromptTokens") or 0),
        completion_tokens=int(raw.get("CompletionTokens") or 0),
        total_tokens=int(raw.get("TotalTokens") or 0),
        cache_hit_tokens=int(raw.get("CacheHitTokens") or 0),
    )


def tool_schema_to_json(schema: llm.ToolSchema) -> dict[str, Any]:
    """对应 Go ``ToolSchema`` / ``ToolFunctionSchema`` 的 json tag。"""
    return {
        "type": schema.type,
        "function": {
            "name": schema.function.name,
            "description": schema.function.description,
            "parameters": jsonable(schema.function.parameters),
        },
    }


def llm_message_to_json(message: llm.LLMMessage) -> dict[str, Any]:
    """``llm.LLMMessage`` 没有 json tag；消息段沿用 ``MessageSegment`` 的 tag。"""
    return {
        "Role": message.role,
        "Segments": [segment.to_json() for segment in message.segments],
        "Name": message.name,
        "ToolCallID": message.tool_call_id,
        "ToolCalls": [
            {"ID": call.id, "Name": call.name, "Arguments": call.arguments}
            for call in message.tool_calls
        ],
    }


def llm_message_from_json(raw: Any) -> llm.LLMMessage:
    if not isinstance(raw, dict):
        return llm.LLMMessage()
    segments = raw.get("Segments")
    calls = raw.get("ToolCalls")
    return llm.LLMMessage(
        role=str(raw.get("Role") or llm.Role.USER),
        segments=[
            llm.MessageSegment.from_json(item)
            for item in (segments if isinstance(segments, list) else [])
        ],
        name=str(raw.get("Name") or ""),
        tool_call_id=str(raw.get("ToolCallID") or ""),
        tool_calls=[
            llm.ToolCallRequest(
                id=str(item.get("ID") or ""),
                name=str(item.get("Name") or ""),
                arguments=str(item.get("Arguments") or ""),
            )
            for item in (calls if isinstance(calls, list) else [])
            if isinstance(item, dict)
        ],
    )


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------
def _platform_to_json(value: PlatformContext) -> dict[str, Any]:
    return {
        "name": value.name,
        "scope_id": value.scope_id,
        "user_id": value.user_id,
        "conversation_id": value.conversation_id,
        "message_id": value.message_id,
        "reply_to_message_id": value.reply_to_message_id,
    }


def _actor_to_json(value: ActorContext) -> dict[str, Any]:
    return {
        "id": value.id,
        "role": value.role,
        "group_role": value.group_role,
        "user_id": value.user_id,
        "nickname": value.nickname,
        "group_card": value.group_card,
        "display_name": value.display_name,
    }


def _session_to_json(value: SessionContext) -> dict[str, Any]:
    return {
        "id": value.id,
        "mode": value.mode,
        "title": value.title,
        "status": value.status,
    }


def _request_to_json(value: RequestContext) -> dict[str, Any]:
    return {
        "id": value.id,
        "kind": value.kind,
        "session_id": value.session_id,
        "phase": value.phase,
    }


def _control_to_json(value: Control) -> dict[str, Any]:
    return {
        "consume": value.consume,
        "stop_propagation": value.stop_propagation,
    }


def _reply_to_json(value: MessageReplyPayload) -> dict[str, Any]:
    out: dict[str, Any] = {"message_id": value.message_id}
    if value.sender_id:
        out["sender_id"] = value.sender_id
    if value.text:
        out["text"] = value.text
    if value.display_text:
        out["display_text"] = value.display_text
    if value.segments:
        out["segments"] = [segment.to_json() for segment in value.segments]
    return out


def _message_to_json(value: MessagePayload) -> dict[str, Any]:
    out: dict[str, Any] = {"id": value.id, "role": value.role}
    if value.platform_text:
        out["platform_text"] = value.platform_text
    if value.platform_message is not None:
        out["platform_message"] = jsonable(value.platform_message)
    if value.intent_text:
        out["intent_text"] = value.intent_text
    if value.reply is not None:
        out["reply"] = _reply_to_json(value.reply)
    if value.segments:
        out["segments"] = [segment.to_json() for segment in value.segments]
    if value.messages:
        out["messages"] = [llm_message_to_json(item) for item in value.messages]
    return out


def _llm_to_json(value: LLMPayload) -> dict[str, Any]:
    out: dict[str, Any] = {"provider": value.provider, "model": value.model}
    if value.messages:
        out["messages"] = [llm_message_to_json(item) for item in value.messages]
    if value.tools:
        out["tools"] = [tool_schema_to_json(item) for item in value.tools]
    if value.usage is not None:
        out["usage"] = usage_to_json(value.usage)
    if value.source_text:
        out["source_text"] = value.source_text
    if value.text:
        out["text"] = value.text
    if value.tool_calls:
        out["tool_calls"] = [
            {"ID": call.id, "Name": call.name, "Arguments": call.arguments}
            for call in value.tool_calls
        ]
    if value.elapsed_ms:
        out["elapsed_ms"] = value.elapsed_ms
    if value.system_append:
        out["system_append"] = list(value.system_append)
    if value.temperature is not None:
        out["temperature"] = value.temperature
    if value.max_tokens is not None:
        out["max_tokens"] = value.max_tokens
    if value.extra_body:
        out["extra_body"] = jsonable(value.extra_body)
    return out


def _tool_to_json(value: ToolPayload) -> dict[str, Any]:
    out: dict[str, Any] = {"id": value.id, "name": value.name}
    if value.arguments:
        out["arguments"] = value.arguments
    if value.risk:
        out["risk"] = value.risk
    if value.result:
        out["result"] = value.result
    if value.error is not None:
        # Go 的 ``error`` 是接口，非 nil 时 ``json.Marshal`` 输出 ``{}``
        # （具体错误类型的字段都不可导出）。这里照抄这个形状，插件靠
        # ``error_info`` 拿真正的文本。
        out["error"] = {}
    return out


def event_to_json(event: Event) -> dict[str, Any]:
    """``hook.Event`` → JSON 对象（键名与 Go struct tag 逐字一致）。"""
    out: dict[str, Any] = {
        "id": event.id,
        "point": event.point,
        "time": time_to_json(event.time),
    }
    if event.metadata:
        out["metadata"] = jsonable(event.metadata)
    out["control"] = _control_to_json(event.control)
    out["platform"] = _platform_to_json(event.platform)
    out["actor"] = _actor_to_json(event.actor)
    out["session"] = _session_to_json(event.session)
    out["request"] = _request_to_json(event.request)
    out["message"] = _message_to_json(event.message)
    out["llm"] = _llm_to_json(event.llm)
    out["tool"] = _tool_to_json(event.tool)
    if event.outputs:
        out["outputs"] = [output_to_json(item) for item in event.outputs]
    if event.error_info is not None:
        out["error"] = {"message": event.error_info.message}
    return out


def _segments_from_json(raw: Any) -> list[llm.MessageSegment]:
    if not isinstance(raw, list):
        return []
    return [llm.MessageSegment.from_json(item) for item in raw]


def event_from_json(raw: Any) -> Event:
    """JSON 对象 → ``hook.Event``（用于测试与插件回环；Go 靠 Unmarshal）。"""
    if not isinstance(raw, dict):
        raise ValueError("hook event must be a JSON object")
    metadata = raw.get("metadata")
    control = raw.get("control")
    reply_raw = None
    message_raw = raw.get("message")
    if isinstance(message_raw, dict) and isinstance(message_raw.get("reply"), dict):
        reply_raw = message_raw["reply"]
    event = Event(
        id=str(raw.get("id") or ""),
        point=str(raw.get("point") or ""),
        time=_time_from_json(raw.get("time")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        control=Control(
            consume=bool((control or {}).get("consume")),
            stop_propagation=bool((control or {}).get("stop_propagation")),
        ),
    )
    platform = raw.get("platform") or {}
    actor = raw.get("actor") or {}
    session = raw.get("session") or {}
    request = raw.get("request") or {}
    if isinstance(platform, dict):
        event.platform = PlatformContext(
            name=str(platform.get("name") or ""),
            scope_id=str(platform.get("scope_id") or ""),
            user_id=str(platform.get("user_id") or ""),
            conversation_id=str(platform.get("conversation_id") or ""),
            message_id=str(platform.get("message_id") or ""),
            reply_to_message_id=str(platform.get("reply_to_message_id") or ""),
        )
    if isinstance(actor, dict):
        event.actor = ActorContext(
            id=str(actor.get("id") or ""),
            role=str(actor.get("role") or ""),
            group_role=str(actor.get("group_role") or ""),
            user_id=str(actor.get("user_id") or ""),
            nickname=str(actor.get("nickname") or ""),
            group_card=str(actor.get("group_card") or ""),
            display_name=str(actor.get("display_name") or ""),
        )
    if isinstance(session, dict):
        event.session = SessionContext(
            id=str(session.get("id") or ""),
            mode=str(session.get("mode") or ""),
            title=str(session.get("title") or ""),
            status=str(session.get("status") or ""),
        )
    if isinstance(request, dict):
        event.request = RequestContext(
            id=str(request.get("id") or ""),
            kind=str(request.get("kind") or ""),
            session_id=str(request.get("session_id") or ""),
            phase=str(request.get("phase") or ""),
        )
    if isinstance(message_raw, dict):
        event.message = MessagePayload(
            id=str(message_raw.get("id") or ""),
            role=str(message_raw.get("role") or ""),
            platform_text=str(message_raw.get("platform_text") or ""),
            platform_message=message_raw.get("platform_message"),
            intent_text=str(message_raw.get("intent_text") or ""),
            reply=(
                MessageReplyPayload(
                    message_id=str(reply_raw.get("message_id") or ""),
                    sender_id=str(reply_raw.get("sender_id") or ""),
                    text=str(reply_raw.get("text") or ""),
                    display_text=str(reply_raw.get("display_text") or ""),
                    segments=_segments_from_json(reply_raw.get("segments")),
                )
                if isinstance(reply_raw, dict)
                else None
            ),
            segments=_segments_from_json(message_raw.get("segments")),
            messages=[
                llm_message_from_json(item)
                for item in (message_raw.get("messages") or [])
            ],
        )
    llm_raw = raw.get("llm")
    if isinstance(llm_raw, dict):
        event.llm = LLMPayload(
            provider=str(llm_raw.get("provider") or ""),
            model=str(llm_raw.get("model") or ""),
            messages=[llm_message_from_json(item) for item in (llm_raw.get("messages") or [])],
            usage=usage_from_json(llm_raw.get("usage")),
            source_text=str(llm_raw.get("source_text") or ""),
            text=str(llm_raw.get("text") or ""),
            tool_calls=[
                llm.ToolCallRequest(
                    id=str(item.get("ID") or ""),
                    name=str(item.get("Name") or ""),
                    arguments=str(item.get("Arguments") or ""),
                )
                for item in (llm_raw.get("tool_calls") or [])
                if isinstance(item, dict)
            ],
            elapsed_ms=int(llm_raw.get("elapsed_ms") or 0),
            system_append=[str(item) for item in (llm_raw.get("system_append") or [])],
            temperature=llm_raw.get("temperature"),
            max_tokens=llm_raw.get("max_tokens"),
            extra_body=dict(llm_raw.get("extra_body") or {}),
        )
    tool_raw = raw.get("tool")
    if isinstance(tool_raw, dict):
        event.tool = ToolPayload(
            id=str(tool_raw.get("id") or ""),
            name=str(tool_raw.get("name") or ""),
            arguments=str(tool_raw.get("arguments") or ""),
            risk=str(tool_raw.get("risk") or ""),
            result=str(tool_raw.get("result") or ""),
        )
    error_raw = raw.get("error")
    if isinstance(error_raw, dict):
        event.error_info = ErrorPayload(message=str(error_raw.get("message") or ""))
    return event


def _time_from_json(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        from atri_x.storage.times import parse_time

        return parse_time(raw)
    except ValueError:
        return None


class RawJSON(str):
    """已编码的 JSON 文本（对应 Go ``json.RawMessage``）：序列化时原样嵌入。

    用于 ``shared.get`` 的 ``value``、帧的 ``params`` / ``result`` 这类
    "值本身已经是 JSON" 的位置 —— 若按普通字符串编码，会被多加一层引号。
    """


def _encode(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
        return
    if value is True:
        out.append("true")
        return
    if value is False:
        out.append("false")
        return
    if isinstance(value, RawJSON):
        text = str(value)
        out.append(text if text else "null")
        return
    if isinstance(value, int):
        out.append(str(value))
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            # Go 的 json.Marshal 对 NaN/Inf 直接报错；这里退化为 null，不产出非法 JSON。
            out.append("null")
            return
        out.append(repr(value))
        return
    if isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
        return
    if isinstance(value, (bytes, bytearray)):
        out.append(json.dumps(_base64.b64encode(bytes(value)).decode("ascii")))
        return
    if isinstance(value, datetime):
        out.append(json.dumps(time_to_json(value)))
        return
    if isinstance(value, MatchContext):
        _encode(match_context_to_json(value), out)
        return
    if isinstance(value, RegexMatch):
        _encode(regex_match_to_json(value), out)
        return
    if isinstance(value, Event):
        _encode(event_to_json(value), out)
        return
    if isinstance(value, BaseException):
        out.append(json.dumps(str(value), ensure_ascii=False))
        return
    if isinstance(value, Mapping):
        out.append("{")
        first = True
        for key, item in value.items():
            if not first:
                out.append(",")
            first = False
            out.append(json.dumps(str(key), ensure_ascii=False))
            out.append(":")
            _encode(item, out)
        out.append("}")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        out.append("[")
        for index, item in enumerate(items):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
        return
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        _encode(to_json(), out)
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        _encode(dataclasses.asdict(value), out)
        return
    out.append(json.dumps(str(value), ensure_ascii=False))


def dumps(value: Any) -> str:
    """紧凑 JSON（对应 Go ``json.Marshal``：无多余空白、原样嵌入 RawMessage）。

    与 Go 的差异：不做 HTML 转义（Go 默认把 ``<`` / ``>`` / ``&`` 转成
    ``\\u003c`` 之类）。两者是等价的 JSON 文本。
    """
    out: list[str] = []
    _encode(value, out)
    return "".join(out)