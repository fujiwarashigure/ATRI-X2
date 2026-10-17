"""``hook.v2`` JSON Lines 协议帧。

对应 Go：``internal/hook/protocol/protocol.go``。

一帧就是一行 JSON（``\\n`` 结尾）。``params`` / ``result`` 在 Go 里是
``json.RawMessage``（原始 JSON 文本，不做解释），这里同样用"已编码的 JSON 文本"
表示，避免二次序列化时改变内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.hook.event import Event
from atri_x.hook.match import MatchContext
from atri_x.hook.output import Group, MessageSegment, Segment, Target
from atri_x.hook.output.jsonstrict import (
    require_bool,
    require_int,
    require_list,
    require_number,
    require_object,
    require_string,
    require_string_list,
    strict_object,
)
from atri_x.hook.protocol.eventjson import (
    dumps,
    event_to_json,
    jsonable,
    match_context_to_json,
)

#: 协议版本。对应 Go ``protocol.Version``。
VERSION = "hook.v2"
Version = VERSION

__all__ = [
    "VERSION",
    "Version",
    "Frame",
    "EventResultBase",
    "MessageResult",
    "LLMResult",
    "EventHandleParams",
    "new_request",
    "decode_frame",
    "encode_frame",
    "validate_id",
    "apply_llm_result",
]


def _embedded(raw: Any) -> Any:
    """把 ``json.RawMessage`` 形态的值还原成可序列化对象。"""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _raw(value: Any) -> str | None:
    """把任意值编码成 ``json.RawMessage`` 文本（``None`` 表示缺省）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return dumps(value)


@dataclass
class Frame:
    """一帧 hook.v2 消息。对应 Go ``Frame``。"""

    type: str = ""
    id: str = ""
    method: str = ""
    #: ``json.RawMessage``：已编码的 JSON 文本。
    params: str | None = None
    ok: bool | None = None
    #: ``json.RawMessage``：已编码的 JSON 文本。
    result: str | None = None
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        """对齐 Go 的 json tag（除 ``type`` 外全部 ``omitempty``）。"""
        out: dict[str, Any] = {"type": self.type}
        if self.id:
            out["id"] = self.id
        if self.method:
            out["method"] = self.method
        if self.params:
            out["params"] = _embedded(self.params)
        if self.ok is not None:
            out["ok"] = self.ok
        if self.result:
            out["result"] = _embedded(self.result)
        if self.error:
            out["error"] = self.error
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "Frame":
        """解码一帧；与 Go 的 ``json.Unmarshal`` 一样**忽略**未知字段。"""
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f"parse hook.v2 frame: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("hook.v2 frame must be a JSON object")
        ok = raw.get("ok")
        return cls(
            type=str(raw.get("type") or ""),
            id=str(raw.get("id") or ""),
            method=str(raw.get("method") or ""),
            params=_raw(raw.get("params")),
            ok=bool(ok) if isinstance(ok, bool) else None,
            result=_raw(raw.get("result")),
            error=str(raw.get("error") or ""),
        )


@dataclass
class MessageResult:
    """插件替换消息内容的结果。对应 Go ``MessageResult``。"""

    text: str | None = None
    segments: list[MessageSegment] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.text is not None:
            out["text"] = self.text
        if self.segments is not None:
            out["segments"] = [item.to_json() for item in self.segments]
        return out

    @classmethod
    def from_object(cls, obj: Any, path: str = "message") -> "MessageResult":
        """严格解码（Go 用 ``DisallowUnknownFields``）。"""
        values = strict_object(
            obj,
            path,
            {
                "text": lambda value, where: (
                    None if value is None else require_string(value, where)
                ),
                "segments": lambda value, where: [
                    MessageSegment.from_object(item, f"{where}[{index}]")
                    for index, item in enumerate(require_list(value, where))
                ],
            },
        )
        return cls(**values)


@dataclass
class LLMResult:
    """请求作用域的临时 LLM 改动（永不写入会话历史）。对应 Go ``LLMResult``。"""

    system_append: list[str] = field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.system_append:
            out["system_append"] = list(self.system_append)
        if self.temperature is not None:
            out["temperature"] = self.temperature
        if self.max_tokens is not None:
            out["max_tokens"] = self.max_tokens
        if self.extra_body:
            out["extra_body"] = jsonable(self.extra_body)
        return out

    @classmethod
    def from_object(cls, obj: Any, path: str = "llm") -> "LLMResult | None":
        if obj is None:
            return None
        values = strict_object(
            obj,
            path,
            {
                "system_append": require_string_list,
                "temperature": lambda value, where: (
                    None if value is None else require_number(value, where)
                ),
                "max_tokens": lambda value, where: (
                    None if value is None else require_int(value, where)
                ),
                "extra_body": require_object,
            },
        )
        return cls(**values)


@dataclass
class EventResultBase:
    """``event.handle`` 的返回基座。对应 Go ``EventResultBase``。

    Go 侧内嵌了 ``hookoutput.Group``（内联展开，所以 ``outputs`` / ``target`` /
    ``timing`` 与 ``status`` 同级）；Python 侧把三个字段摊平，再提供
    :meth:`group` 还原成 :class:`atri_x.hook.output.Group`。
    """

    status: str = ""
    outputs: list[Segment] = field(default_factory=list)
    target: Target = field(default_factory=Target)
    timing: str = ""
    pass_through: bool | None = None
    message: MessageResult | None = None
    llm: LLMResult | None = None

    def group(self) -> Group:
        return Group(outputs=list(self.outputs), target=self.target, timing=self.timing)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            # Go 的 ``Group.Outputs`` 没有 omitempty：nil 会输出 null。
            "outputs": [item.to_json() for item in self.outputs] if self.outputs else None,
            # ``target`` 虽然带 omitempty，但结构体值永远不为空，所以一定输出。
            "target": self.target.to_json(),
        }
        if self.timing:
            out["timing"] = self.timing
        if self.pass_through is not None:
            out["pass_through"] = self.pass_through
        if self.message is not None:
            out["message"] = self.message.to_json()
        if self.llm is not None:
            out["llm"] = self.llm.to_json()
        return out

    @classmethod
    def from_object(cls, obj: Any, path: str = "result") -> "EventResultBase":
        """从已解析的 JSON 对象严格取值（Go 侧由 ``DecodeJSON`` 完成）。"""
        if not isinstance(obj, dict):
            raise ValueError("hook event result must be a JSON object")
        values = strict_object(
            obj,
            path,
            {
                "status": require_string,
                "outputs": lambda value, where: [
                    Segment.from_object(item, f"{where}[{index}]")
                    for index, item in enumerate(require_list(value, where))
                ],
                "target": Target.from_object,
                "timing": require_string,
                "pass_through": lambda value, where: (
                    None if value is None else require_bool(value, where)
                ),
                "message": MessageResult.from_object,
                "llm": LLMResult.from_object,
            },
        )
        return cls(**values)


@dataclass
class EventHandleParams:
    """``event.handle`` 的参数。对应 Go ``EventHandleParams``。"""

    event: Event = field(default_factory=Event)
    match: MatchContext = field(default_factory=MatchContext)

    def to_json(self) -> dict[str, Any]:
        # ``match`` 带 omitempty 但它是结构体，Go 一定输出它。
        return {
            "event": event_to_json(self.event),
            "match": match_context_to_json(self.match),
        }


def new_request(id: str, method: str, params: Any) -> Frame:
    """构造一个 ``request`` 帧。对应 Go ``NewRequest``。"""
    return Frame(type="request", id=id, method=method, params=_raw(params))


def decode_frame(data: str | bytes) -> Frame:
    """解析一行帧。对应 Go ``DecodeFrame``（失败抛 :class:`ValueError`）。"""
    return Frame.from_json(data)


def encode_frame(frame: Any) -> bytes:
    """编码一帧（含结尾换行由调用方负责）。对应 Go ``EncodeFrame``。"""
    if isinstance(frame, Frame):
        return dumps(frame.to_json()).encode("utf-8")
    return dumps(frame).encode("utf-8")


def validate_id(id: str, prefix: str) -> None:
    """帧 ID 必须使用指定前缀。对应 Go ``ValidateID``；不合法抛 :class:`ValueError`。"""
    if not (id or "").startswith(prefix):
        raise ValueError(f"frame id {id!r} must use {prefix} prefix")


def apply_llm_result(event: Event, result: LLMResult | None) -> Event:
    """把进程 Hook 的 LLM 改动合并进事件。对应 Go ``ApplyLLMResult``。"""
    if result is None:
        return event
    for part in result.system_append:
        if part and part.strip():
            event.llm.system_append.append(part)
    if result.temperature is not None:
        event.llm.temperature = result.temperature
    if result.max_tokens is not None:
        event.llm.max_tokens = result.max_tokens
    if result.extra_body:
        if event.llm.extra_body is None:
            event.llm.extra_body = {}
        for key, value in result.extra_body.items():
            event.llm.extra_body[key] = value
    return event
