"""有状态 Hook 进程的协议负载（``system.init`` / ``event.handle``）。

对应 Go：``internal/hook/runtime/protocol.go``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atri_x.hook.event import Event
from atri_x.hook.match import MatchContext
from atri_x.hook.output import JSONDecodeError, MessageSegment, Segment, Target
from atri_x.hook.output.jsonstrict import (
    require_bool,
    require_list,
    require_string,
    strict_object,
)
from atri_x.hook.protocol import VERSION
from atri_x.hook.protocol.eventjson import (
    dumps,
    event_to_json,
    jsonable,
    match_context_to_json,
)
from atri_x.hook.protocol.frames import (
    EventResultBase,
    LLMResult,
    MessageResult,
)
from atri_x.llm import llm
from atri_x.storage.times import parse_time

#: 协议版本。对应 Go ``protocolVersion``。
PROTOCOL_VERSION = VERSION

__all__ = [
    "PROTOCOL_VERSION",
    "SystemHook",
    "SystemInit",
    "EventHandle",
    "EventResult",
    "EVENT_RESULT_BASE_KEYS",
    "must_json",
]

#: ``EventResultBase`` 的 JSON 键集合（Go 的内嵌 ``Group`` 会摊平到同级）。
EVENT_RESULT_BASE_KEYS = frozenset(
    {"status", "outputs", "target", "timing", "pass_through", "message", "llm"}
)


def must_json(value: Any) -> str:
    """把值编码成 JSON 文本（对应 Go ``mustJSON``）。"""
    return dumps(value)


@dataclass
class SystemHook:
    """``system.init`` 里的 hook 描述。对应 Go ``systemHook``。"""

    id: str = ""
    description: str = ""
    dir: str = ""
    cwd: str = ""
    shared_dir: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "plugin_dir": self.dir,
            "cwd": self.cwd,
            "shared_dir": self.shared_dir,
        }
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class SystemInit:
    """``system.init`` 的参数。对应 Go ``systemInit``。"""

    version: str = PROTOCOL_VERSION
    hook: SystemHook = field(default_factory=SystemHook)
    tools: list[llm.ToolSchema] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        from atri_x.hook.protocol.eventjson import tool_schema_to_json

        return {
            "version": self.version,
            "hook": self.hook.to_json(),
            "tools": [tool_schema_to_json(schema) for schema in self.tools],
        }


@dataclass
class EventHandle:
    """``event.handle`` 的参数。对应 Go ``eventHandle``。"""

    event: Event = field(default_factory=Event)
    match: MatchContext = field(default_factory=MatchContext)
    continuation: bool = False
    tool_context: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event": event_to_json(self.event),
            "match": match_context_to_json(self.match),
        }
        if self.continuation:
            out["continuation"] = True
        # Go 的 ``ToolContext string json:"tool_context"`` 没有 omitempty。
        out["tool_context"] = self.tool_context
        return out


@dataclass
class EventResult:
    """``event.handle`` 的结果（含等待态字段）。对应 Go ``eventResult``。"""

    status: str = ""
    outputs: list[Segment] = field(default_factory=list)
    target: Target = field(default_factory=Target)
    timing: str = ""
    pass_through: bool | None = None
    message: MessageResult | None = None
    llm: LLMResult | None = None
    conversation_id: str = ""
    expires_at: datetime | None = None

    def base(self) -> EventResultBase:
        """投影回 :class:`EventResultBase`（Go 的内嵌结构体）。"""
        return EventResultBase(
            status=self.status,
            outputs=list(self.outputs),
            target=self.target,
            timing=self.timing,
            pass_through=self.pass_through,
            message=self.message,
            llm=self.llm,
        )

    def group(self) -> Any:
        """内嵌 ``hookoutput.Group`` 的等价物。对应 Go 的 ``result.Group``。"""
        return self.base().group()

    def to_json(self) -> dict[str, Any]:
        out = self.base().to_json()
        if self.conversation_id:
            out["conversation_id"] = self.conversation_id
        if self.expires_at is not None:
            from atri_x.hook.protocol.eventjson import time_to_json

            out["expires_at"] = time_to_json(self.expires_at)
        return out

    @classmethod
    def from_object(cls, obj: Any, path: str = "result") -> "EventResult":
        """严格解码（Go 用 ``hookoutput.DecodeJSON``）。"""
        if not isinstance(obj, dict):
            raise JSONDecodeError(f"{path}: hook event result must be a JSON object")
        allowed = EVENT_RESULT_BASE_KEYS | {"conversation_id", "expires_at"}
        for key in obj:
            if key not in allowed:
                raise JSONDecodeError(f'{path}: json: unknown field "{key}"')
        base = EventResultBase.from_object(
            {key: value for key, value in obj.items() if key in EVENT_RESULT_BASE_KEYS},
            path,
        )
        extra = strict_object(
            {key: value for key, value in obj.items() if key not in EVENT_RESULT_BASE_KEYS},
            path,
            {
                "conversation_id": require_string,
                "expires_at": lambda value, where: _expires_at(value, where),
            },
        )
        return cls(
            status=base.status,
            outputs=base.outputs,
            target=base.target,
            timing=base.timing,
            pass_through=base.pass_through,
            message=base.message,
            llm=base.llm,
            conversation_id=extra.get("conversation_id") or "",
            expires_at=extra.get("expires_at"),
        )


def _expires_at(value: Any, where: str) -> datetime | None:
    text = require_string(value, where)
    if not text:
        return None
    try:
        return parse_time(text)
    except ValueError as exc:
        raise JSONDecodeError(f"{where}: {exc}") from exc


# 供交叉检索：Go 里 ``message.segments`` 用 hookoutput.MessageSegment。
_SEGMENT_TYPE = MessageSegment
_JSON = jsonable
_BOOL = require_bool
_LIST = require_list
