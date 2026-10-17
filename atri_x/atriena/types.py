"""Atriena 事件协议的领域类型与版本/模式/状态常量。

对应 Go：``internal/atriena/types.go``。

``Atriena`` 是外部（HTTP 令牌、Hook、进程内调用）把一件事丢给 ATRI-X 的统一信封：
调用方声明"谁（atriwisp）因为什么（source/id）要求做什么（mode）"，ATRI-X 负责鉴权、
去重、目标解析与投递。

JSON 口径与 Go 的 struct tag **逐字一致**（``version`` / ``atriwisp`` / ``source`` /
``id`` / ``created_at`` / ``mode`` / ``title`` / ``format`` / ``content`` /
``model_slot`` / ``session_mode`` / ``tool_list_names`` / ``tools`` / ``segments`` /
``targets`` / ``calls`` / ``meta``），``omitempty`` 也逐字段复刻。Go 里切片的零值是
``nil``、空切片是 ``[]``，两者 marshal 结果不同（``null`` vs ``[]``）；本模块用
``None`` 表示 nil、``[]`` 表示空切片，:meth:`Request.to_json` 按同一规则输出，
因为 ``content_hash`` 是对这个 JSON 求 SHA-256（去重键要稳定）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atri_x.toolrun import AtriwispToolDeclaration

__all__ = [
    "Atriwisp",
    "Call",
    "CallKind",
    "CallKindCapability",
    "CallKindRaw",
    "CallResult",
    "Event",
    "ModeDirect",
    "ModeLLM",
    "ModeRecord",
    "Request",
    "Response",
    "Segment",
    "SegmentKind",
    "SegmentKindFile",
    "SegmentKindImage",
    "SegmentKindText",
    "StatusAccepted",
    "StatusCompleted",
    "StatusDelivering",
    "StatusDuplicate",
    "StatusFailed",
    "StatusQueued",
    "StatusResultReady",
    "StatusRunning",
    "StatusUnsupported",
    "Target",
    "VersionV2",
    "VersionV3",
]

#: 支持的协议版本。对应 Go ``VersionV2`` / ``VersionV3``。
VersionV2 = "atriena.v2"
VersionV3 = "atriena.v3"

#: 处理模式。对应 Go ``ModeRecord`` / ``ModeDirect`` / ``ModeLLM``。
ModeRecord = "record"
ModeDirect = "direct"
ModeLLM = "llm"

#: 事件状态机。对应 Go ``Status*``。
StatusAccepted = "accepted"
StatusQueued = "queued"
StatusRunning = "running"
StatusResultReady = "result_ready"
StatusDelivering = "delivering"
StatusCompleted = "completed"
StatusFailed = "failed"
StatusDuplicate = "duplicate"
StatusUnsupported = "unsupported"


class SegmentKind:
    """消息段种类。对应 Go 的 ``SegmentKind`` 字符串类型。"""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"


SegmentKindText = SegmentKind.TEXT
SegmentKindImage = SegmentKind.IMAGE
SegmentKindFile = SegmentKind.FILE


class CallKind:
    """平台调用种类。对应 Go 的 ``CallKind`` 字符串类型。"""

    CAPABILITY = "capability"
    RAW = "raw"


CallKindCapability = CallKind.CAPABILITY
CallKindRaw = CallKind.RAW


def _string_field(raw: dict[str, Any], key: str) -> str:
    """取一个字符串字段；缺失按 Go 的零值（空串）处理，类型不符即报错。"""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"json: cannot unmarshal non-string into {key}")
    return value


def _list_field(raw: dict[str, Any], key: str) -> list[Any] | None:
    """取一个数组字段；``null``/缺失返回 ``None``（Go 的 nil 切片）。"""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"json: cannot unmarshal non-array into {key}")
    return value


def _bool_field(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"json: cannot unmarshal non-bool into {key}")
    return value


@dataclass
class Atriwisp:
    """事件声明的"我是谁"。对应 Go ``Atriwisp``。

    ``name`` 与 ``tags`` 都**没有** ``omitempty``，所以 nil 的 tags 会 marshal 成
    ``null``；实际落库走 :func:`atri_x.atrinis.store.trim_strings`，永远是 ``[...]``。
    """

    name: str = ""
    tags: list[str] | None = None

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 struct tag 序列化。"""
        return {
            "name": self.name,
            "tags": None if self.tags is None else list(self.tags),
        }

    @classmethod
    def from_json(cls, raw: Any) -> "Atriwisp":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("json: cannot unmarshal non-object into atriwisp")
        tags = _list_field(raw, "tags")
        out = [str(item) for item in tags] if tags is not None else None
        return cls(name=_string_field(raw, "name"), tags=out)


@dataclass
class Segment:
    """一条入站内容段。对应 Go ``Segment``（``text`` 与媒体段互斥）。"""

    kind: str = SegmentKind.TEXT
    text: str = ""
    url: str = ""
    name: str = ""
    mime_type: str = ""

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 struct tag 序列化（``kind`` 必出，其余 omitempty）。"""
        out: dict[str, Any] = {"kind": self.kind}
        if self.text:
            out["text"] = self.text
        if self.url:
            out["url"] = self.url
        if self.name:
            out["name"] = self.name
        if self.mime_type:
            out["mime_type"] = self.mime_type
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "Segment":
        if not isinstance(raw, dict):
            raise ValueError("json: cannot unmarshal non-object into segment")
        return cls(
            kind=_string_field(raw, "kind") or SegmentKind.TEXT,
            text=_string_field(raw, "text"),
            url=_string_field(raw, "url"),
            name=_string_field(raw, "name"),
            mime_type=_string_field(raw, "mime_type"),
        )


@dataclass
class Target:
    """一条投递目标。对应 Go ``Target``。

    ``platform="all"`` 表示"所有已启用的平台"（此时 ``type``/``id`` 必须为空）。
    ``to_delivery_target`` 对应 Go 定义在 ``delivery.go`` 里的方法
    ``Target.ToDeliveryTarget``；为保持"类型定义唯一出处"放在这里。
    """

    platform: str = ""
    type: str = ""
    id: str = ""

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 struct tag 序列化（``platform`` 必出，其余 omitempty）。"""
        out: dict[str, Any] = {"platform": self.platform}
        if self.type:
            out["type"] = self.type
        if self.id:
            out["id"] = self.id
        return out

    def to_delivery_target(self) -> Any:
        """转成交付层目标。对应 Go ``Target.ToDeliveryTarget``。

        ``private`` → 私聊用户；``group`` → 群；其余（含 ``platform="all"`` 与
        "未指定 type"）→ 超级管理员，与 Go 的 ``default`` 分支一致。
        """
        from atri_x.delivery import Target as DeliveryTarget

        out = DeliveryTarget(platform=self.platform)
        if self.type == "private":
            out.private_user_id = self.id
        elif self.type == "group":
            out.group_id = self.id
        else:
            out.superadmins = True
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "Target":
        if not isinstance(raw, dict):
            raise ValueError("json: cannot unmarshal non-object into target")
        return cls(
            platform=_string_field(raw, "platform"),
            type=_string_field(raw, "type"),
            id=_string_field(raw, "id"),
        )


@dataclass
class Call:
    """一次平台 API 调用请求。对应 Go ``Call``（字段全部 omitempty）。"""

    kind: str = ""
    name: str = ""
    platform: str = ""
    target: Target | None = None
    api: str = ""
    params: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.kind:
            out["kind"] = self.kind
        if self.name:
            out["name"] = self.name
        if self.platform:
            out["platform"] = self.platform
        if self.target is not None:
            target = self.target.to_json()
            if target:
                out["target"] = target
        if self.api:
            out["api"] = self.api
        if self.params:
            out["params"] = dict(self.params)
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "Call":
        if not isinstance(raw, dict):
            raise ValueError("json: cannot unmarshal non-object into call")
        target = raw.get("target")
        params = raw.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError("json: cannot unmarshal non-object into call.params")
        return cls(
            kind=_string_field(raw, "kind"),
            name=_string_field(raw, "name"),
            platform=_string_field(raw, "platform"),
            target=Target.from_json(target) if target is not None else None,
            api=_string_field(raw, "api"),
            params=dict(params) if params is not None else None,
        )


@dataclass
class CallResult:
    """一次平台 API 调用的结果。对应 Go ``CallResult``。"""

    call: Call = field(default_factory=Call)
    response: str = ""
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"call": self.call.to_json()}
        if self.response:
            out["response"] = self.response
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Response:
    """一次分发的响应。对应 Go ``Response``。

    ``error`` 是唯一 omitempty 字段；其余（含 ``duplicate``）总是输出，
    与 Go 一致。
    """

    accepted: bool = False
    duplicate: bool = False
    event_key: str = ""
    mode: str = ""
    status: str = ""
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "event_key": self.event_key,
            "mode": self.mode,
            "status": self.status,
        }
        if self.error:
            out["error"] = self.error
        return out


def tool_declaration_to_json(declared: Any) -> dict[str, Any]:
    """把 :class:`~atri_x.toolrun.AtriwispToolDeclaration` 按 Go tag 序列化。

    对应 Go ``json.Marshal([]toolrun.AtriwispToolDeclaration)``。``atri_x.toolrun``
    只在 ``AtriwispInjection`` 内部使用该结构、没有自带 wire 形态，因此这里补齐
    （字段口径见 Go ``internal/toolrun/atriwisp.go`` 的 struct tag）。
    """
    out: dict[str, Any] = {"name": getattr(declared, "name", "") or ""}
    for key, name in (
        ("description", "description"),
        ("endpoint", "endpoint"),
    ):
        value = getattr(declared, name, "") or ""
        if value:
            out[key] = value
    schema = getattr(declared, "schema", None)
    if schema:
        out["schema"] = dict(schema)
    timeout = int(getattr(declared, "timeout_seconds", 0) or 0)
    if timeout:
        out["timeout_seconds"] = timeout
    if getattr(declared, "foreground_only", False):
        out["foreground_only"] = True
    return out


def tool_declaration_from_json(raw: Any) -> AtriwispToolDeclaration:
    """``tool_declaration_to_json`` 的逆操作（未知键忽略，类型不符报错）。"""
    if not isinstance(raw, dict):
        raise ValueError("json: cannot unmarshal non-object into tool declaration")
    schema = raw.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise ValueError("json: cannot unmarshal non-object into tool schema")
    timeout = raw.get("timeout_seconds")
    if timeout is not None and not isinstance(timeout, int):
        raise ValueError("json: cannot unmarshal non-int into timeout_seconds")
    return AtriwispToolDeclaration(
        name=_string_field(raw, "name"),
        description=_string_field(raw, "description"),
        schema=dict(schema) if schema else {},
        timeout_seconds=int(timeout or 0),
        endpoint=_string_field(raw, "endpoint"),
        foreground_only=_bool_field(raw, "foreground_only"),
    )


@dataclass
class Request:
    """一次 Atriena 请求。对应 Go ``Request``。

    列表字段的默认值是 ``None``（Go 的 nil 切片），marshal 成 ``null``。
    """

    version: str = ""
    atriwisp: Atriwisp = field(default_factory=Atriwisp)
    source: str = ""
    id: str = ""
    created_at: str = ""
    mode: str = ""
    title: str = ""
    format: str = ""
    content: str = ""
    model_slot: str = ""
    session_mode: str = ""
    tool_list_names: list[str] | None = None
    tools: list[AtriwispToolDeclaration] | None = None
    #: ``omitempty``：为空（含 ``None``）时整个键不出现。
    segments: list[Segment] | None = None
    targets: list[Target] | None = None
    #: ``omitempty``：同上。
    calls: list[Call] | None = None
    meta: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 struct tag 序列化（``segments`` / ``calls`` 为空时省略）。"""
        out: dict[str, Any] = {
            "version": self.version,
            "atriwisp": self.atriwisp.to_json(),
            "source": self.source,
            "id": self.id,
            "created_at": self.created_at,
            "mode": self.mode,
            "title": self.title,
            "format": self.format,
            "content": self.content,
            "model_slot": self.model_slot,
            "session_mode": self.session_mode,
            "tool_list_names": None if self.tool_list_names is None else list(self.tool_list_names),
            "tools": None if self.tools is None else [tool_declaration_to_json(t) for t in self.tools],
        }
        if self.segments:
            out["segments"] = [segment.to_json() for segment in self.segments]
        out["targets"] = None if self.targets is None else [t.to_json() for t in self.targets]
        if self.calls:
            out["calls"] = [call.to_json() for call in self.calls]
        out["meta"] = None if self.meta is None else dict(self.meta)
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "Request":
        """按 Go ``json.Unmarshal`` 的口径解析：未知键忽略，类型不符即报错。"""
        if not isinstance(raw, dict):
            raise ValueError("json: cannot unmarshal non-object into request")
        tool_list = _list_field(raw, "tool_list_names")
        tools = _list_field(raw, "tools")
        segments = _list_field(raw, "segments")
        targets = _list_field(raw, "targets")
        calls = _list_field(raw, "calls")
        meta = raw.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("json: cannot unmarshal non-object into meta")
        return cls(
            version=_string_field(raw, "version"),
            atriwisp=Atriwisp.from_json(raw.get("atriwisp")),
            source=_string_field(raw, "source"),
            id=_string_field(raw, "id"),
            created_at=_string_field(raw, "created_at"),
            mode=_string_field(raw, "mode"),
            title=_string_field(raw, "title"),
            format=_string_field(raw, "format"),
            content=_string_field(raw, "content"),
            model_slot=_string_field(raw, "model_slot"),
            session_mode=_string_field(raw, "session_mode"),
            tool_list_names=[str(item) for item in tool_list] if tool_list is not None else None,
            tools=[tool_declaration_from_json(item) for item in tools] if tools is not None else None,
            segments=[Segment.from_json(item) for item in segments] if segments is not None else None,
            targets=[Target.from_json(item) for item in targets] if targets is not None else None,
            calls=[Call.from_json(item) for item in calls] if calls is not None else None,
            meta=dict(meta) if meta is not None else None,
        )


@dataclass
class Event:
    """一次已校验、已预处理的事件。对应 Go ``Event``。

    Go 的这个结构没有 json tag，只在进程内流转（不落库、不上线），因此本模块
    不提供 ``to_json``。
    """

    request: Request = field(default_factory=Request)
    origin: Any = None
    event_key: str = ""
    content_hash: str = ""
    tool_declarations: str = ""
    tool_hash: str = ""
    tags_json: str = ""
    requested_targets: str = ""
    resolved_targets: str = ""
    segment_paths: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None
    received_at: datetime | None = None
