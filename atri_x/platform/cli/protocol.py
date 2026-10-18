r"""远程 CLI 的线上消息类型与 JSON 编码。

对应 Go：``internal/platform/cli/remote_protocol.go``（消息类型常量与
``remoteMessage`` struct），并承载 ``remote_server.go`` / ``remote_client.go``
共用的两个纯函数：``remoteNoticeMessage`` 的级别文本化与 ``remoteNoticeLevel``。

Go 用 ``coder/websocket`` 的 ``wsjson``（内部就是 ``encoding/json``）收发，Python
侧用 :func:`json.dumps` / :func:`json.loads` + :meth:`RemoteMessage.to_json` /
:meth:`RemoteMessage.from_json` 显式映射。**键名与 ``omitempty`` 语义都逐字复刻
Go 的 struct tag**，其中三处 ``encoding/json`` 的坑要特别记住：

1. ``Type string \`json:"type"\`` **没有** ``omitempty``，因此空串也会出现在报文里。
2. ``Snapshot runtimestatus.Snapshot \`json:"snapshot,omitempty"\`` 看起来会省略，
   但 Go 的 ``omitempty`` **对 struct 无效**（只有指针/接口/map/slice/数组/字符串/
   数值/布尔才算 "empty"），所以 ``snapshot`` 永远出现在报文里；
   ``runtimestatus.Snapshot`` 与 ``completion.Item`` 都**没有 json tag**，
   线上键名就是 Go 字段名本体（PascalCase）。
3. ``time.Time`` **零值**会被序列化成 ``"0001-01-01T00:00:00Z"`` 而不是 ``null``；
   反向解析时该字符串还原成 Python 的 ``None``（Go 的零值时间）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from atri_x.completion.completion import Item
from atri_x.llm.llm import Usage
from atri_x.runtime.status import Snapshot

#: 协议消息类型。对应 Go 的 ``remoteMsg*`` 常量块。
REMOTE_MSG_HELLO = "hello"
REMOTE_MSG_HELLO_OK = "hello_ok"
REMOTE_MSG_INPUT = "input"
REMOTE_MSG_COMPLETE = "complete"
REMOTE_MSG_COMPLETE_RESULT = "complete_result"
REMOTE_MSG_CHAT = "chat"
REMOTE_MSG_NOTICE = "notice"
REMOTE_MSG_REASONING = "reasoning"
REMOTE_MSG_STATUS = "status"
REMOTE_MSG_STREAM_APPEND = "stream_append"
REMOTE_MSG_STREAM_REPLACE = "stream_replace"
REMOTE_MSG_STREAM_FINISH = "stream_finish"
REMOTE_MSG_ERROR = "error"

#: 会话浏览扩展（**Python 版新增**，Go 上游没有这两对帧）。
#:
#: 桌面端需要"列出会话 / 读某会话历史"，但 Go 的线上协议只有 ``input`` 与
#: ``complete`` 两种客户端请求。为了不改 ``input`` 的语义（它走完整的
#: ``handle_message``，会触发命令、落库、发送），这里加两对只读帧：
#:
#: * 客户端发 ``sessions``（``limit``/``text`` 可选）→ 服务端回 ``sessions_result``；
#: * 客户端发 ``history``（``session_id`` + ``limit``）→ 服务端回 ``history_result``。
#:
#: 两者都只读存储，不改变当前会话；切换会话仍走既有的 ``/resume <id>`` 命令，
#: 因此权限、审计、命名等语义完全复用命令链路，不在这里另起一套。
REMOTE_MSG_SESSIONS = "sessions"
REMOTE_MSG_SESSIONS_RESULT = "sessions_result"
REMOTE_MSG_HISTORY = "history"
REMOTE_MSG_HISTORY_RESULT = "history_result"

#: 单次写入的超时（秒）。对应 Go ``remoteWriteTimeout = 15 * time.Second``。
REMOTE_WRITE_TIMEOUT = 15.0

#: Go 零值时间的 RFC3339 文本。``time.Time{}`` 与 ``None`` 互相映射。
ZERO_TIME_TEXT = "0001-01-01T00:00:00Z"

#: 日志级别：复用 ``logging`` 的数值，与 ``atri_x.delivery.delivery.Notice.level``
#: 的类型口径一致（Go 的 ``slog.Level`` 在 Python 侧统一用 ``logging`` 级别整数）。
LEVEL_DEBUG = logging.DEBUG
LEVEL_INFO = logging.INFO
LEVEL_WARN = logging.WARNING
LEVEL_ERROR = logging.ERROR

_LEVEL_TEXTS = {
    LEVEL_DEBUG: "DEBUG",
    LEVEL_INFO: "INFO",
    LEVEL_WARN: "WARN",
    LEVEL_ERROR: "ERROR",
}

_LEVELS_BY_TEXT = {text: value for value, text in _LEVEL_TEXTS.items()}

__all__ = [
    "LEVEL_DEBUG",
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "REMOTE_MSG_CHAT",
    "REMOTE_MSG_COMPLETE",
    "REMOTE_MSG_COMPLETE_RESULT",
    "REMOTE_MSG_ERROR",
    "REMOTE_MSG_HELLO",
    "REMOTE_MSG_HELLO_OK",
    "REMOTE_MSG_HISTORY",
    "REMOTE_MSG_HISTORY_RESULT",
    "REMOTE_MSG_INPUT",
    "REMOTE_MSG_NOTICE",
    "REMOTE_MSG_REASONING",
    "REMOTE_MSG_SESSIONS",
    "REMOTE_MSG_SESSIONS_RESULT",
    "REMOTE_MSG_STATUS",
    "REMOTE_MSG_STREAM_APPEND",
    "REMOTE_MSG_STREAM_FINISH",
    "REMOTE_MSG_STREAM_REPLACE",
    "REMOTE_WRITE_TIMEOUT",
    "ZERO_TIME_TEXT",
    "HistoryEntry",
    "RemoteMessage",
    "SessionInfo",
    "format_rfc3339_nano",
    "history_entry_from_json",
    "history_entry_to_json",
    "item_from_json",
    "item_to_json",
    "level_from_text",
    "level_text",
    "output_message",
    "parse_rfc3339_nano",
    "remote_notice_level",
    "remote_notice_message",
    "session_info_from_json",
    "session_info_to_json",
    "set_from_strings",
    "snapshot_from_json",
    "snapshot_to_json",
    "usage_from_json",
    "usage_to_json",
]


@dataclass
class SessionInfo:
    """会话列表里的一条（**Python 版新增**，见 ``REMOTE_MSG_SESSIONS`` 的说明）。

    键名用 snake_case：这是扩展帧，不必复刻 Go 的 PascalCase 结构体字段。
    """

    id: str = ""
    title: str = ""
    mode: str = ""
    status: str = ""
    platform: str = ""
    updated_at: str = ""
    created_at: str = ""
    current: bool = False

    def to_json(self) -> dict[str, Any]:
        """序列化（空字段省略，``current`` 恒出现）。"""
        out: dict[str, Any] = {"id": self.id, "current": bool(self.current)}
        for key, value in (
            ("title", self.title),
            ("mode", self.mode),
            ("status", self.status),
            ("platform", self.platform),
            ("updated_at", self.updated_at),
            ("created_at", self.created_at),
        ):
            if value:
                out[key] = value
        return out


@dataclass
class HistoryEntry:
    """某会话历史里的一条消息（**Python 版新增**）。"""

    role: str = ""
    text: str = ""
    at: str = ""

    def to_json(self) -> dict[str, Any]:
        """序列化。"""
        out: dict[str, Any] = {"role": self.role, "text": self.text}
        if self.at:
            out["at"] = self.at
        return out


def session_info_to_json(info: SessionInfo) -> dict[str, Any]:
    """:class:`SessionInfo` → 线上字典。"""
    return info.to_json()


def session_info_from_json(raw: Any) -> SessionInfo:
    """线上字典 → :class:`SessionInfo`（非法值取零值）。"""
    if not isinstance(raw, Mapping):
        return SessionInfo()
    return SessionInfo(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        mode=str(raw.get("mode") or ""),
        status=str(raw.get("status") or ""),
        platform=str(raw.get("platform") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        created_at=str(raw.get("created_at") or ""),
        current=bool(raw.get("current")),
    )


def history_entry_to_json(entry: HistoryEntry) -> dict[str, Any]:
    """:class:`HistoryEntry` → 线上字典。"""
    return entry.to_json()


def history_entry_from_json(raw: Any) -> HistoryEntry:
    """线上字典 → :class:`HistoryEntry`。"""
    if not isinstance(raw, Mapping):
        return HistoryEntry()
    return HistoryEntry(
        role=str(raw.get("role") or ""),
        text=str(raw.get("text") or ""),
        at=str(raw.get("at") or ""),
    )


@dataclass
class RemoteMessage:
    """远程 CLI 的一帧。对应 Go ``remoteMessage``。

    字段的零值与 Go 一致：``cursor`` 是 ``int``，``items`` 是列表，
    ``snapshot`` 是 :class:`atri_x.runtime.status.Snapshot`。
    """

    type: str = ""
    id: str = ""
    client_id: str = ""
    token: str = ""
    text: str = ""
    level: str = ""
    cursor: int = 0
    items: list[Item] = field(default_factory=list)
    snapshot: Snapshot = field(default_factory=Snapshot)
    # -- 会话浏览扩展（Python 版新增；Go 的 remoteMessage 没有这些字段） ----
    session_id: str = ""
    limit: int = 0
    sessions: list[SessionInfo] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """按 Go struct tag 序列化（``omitempty`` 逐字段复刻）。"""
        out: dict[str, Any] = {"type": self.type}
        for key, value in (
            ("id", self.id),
            ("client_id", self.client_id),
            ("token", self.token),
            ("text", self.text),
            ("level", self.level),
            ("session_id", self.session_id),
        ):
            if value:
                out[key] = value
        if self.cursor:
            out["cursor"] = self.cursor
        if self.limit:
            out["limit"] = int(self.limit)
        if self.items:
            out["items"] = [item_to_json(item) for item in self.items]
        if self.sessions:
            out["sessions"] = [session_info_to_json(item) for item in self.sessions]
        if self.history:
            out["history"] = [history_entry_to_json(item) for item in self.history]
        # omitempty 对 struct 无效：snapshot 永远出现（见模块 docstring 第 2 条）。
        out["snapshot"] = snapshot_to_json(self.snapshot)
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "RemoteMessage":
        """按 Go ``encoding/json`` 反序列化（未知键忽略、缺失键取零值）。"""
        if not isinstance(raw, Mapping):
            return cls()
        cursor = raw.get("cursor")
        items = raw.get("items")
        sessions = raw.get("sessions")
        history = raw.get("history")
        limit = raw.get("limit")
        return cls(
            type=str(raw.get("type") or ""),
            id=str(raw.get("id") or ""),
            client_id=str(raw.get("client_id") or ""),
            token=str(raw.get("token") or ""),
            text=str(raw.get("text") or ""),
            level=str(raw.get("level") or ""),
            cursor=int(cursor) if isinstance(cursor, (int, float)) else 0,
            items=[item_from_json(value) for value in items] if isinstance(items, list) else [],
            snapshot=snapshot_from_json(raw.get("snapshot")),
            session_id=str(raw.get("session_id") or ""),
            limit=int(limit) if isinstance(limit, (int, float)) else 0,
            sessions=(
                [session_info_from_json(value) for value in sessions]
                if isinstance(sessions, list)
                else []
            ),
            history=(
                [history_entry_from_json(value) for value in history]
                if isinstance(history, list)
                else []
            ),
        )


# ---------------------------------------------------------------------------
# completion.Item（Go ``completion.Item`` 没有 json tag，键名即字段名）
# ---------------------------------------------------------------------------

def item_to_json(item: Item) -> dict[str, Any]:
    return {
        "Text": item.text,
        "Label": item.label,
        "Description": item.description,
        "Kind": item.kind,
        "ReplaceStart": item.replace_start,
        "ReplaceEnd": item.replace_end,
    }


def item_from_json(raw: Any) -> Item:
    if not isinstance(raw, Mapping):
        return Item()
    return Item(
        text=str(raw.get("Text") or ""),
        label=str(raw.get("Label") or ""),
        description=str(raw.get("Description") or ""),
        kind=str(raw.get("Kind") or ""),
        replace_start=_as_int(raw.get("ReplaceStart")),
        replace_end=_as_int(raw.get("ReplaceEnd")),
    )


# ---------------------------------------------------------------------------
# llm.Usage / runtime.Snapshot（同样没有 json tag）
# ---------------------------------------------------------------------------

def usage_to_json(usage: Usage | None) -> dict[str, Any] | None:
    """``*llm.Usage``：nil 指针序列化成 ``null``。"""
    if usage is None:
        return None
    return {
        "PromptTokens": usage.prompt_tokens,
        "CompletionTokens": usage.completion_tokens,
        "TotalTokens": usage.total_tokens,
        "CacheHitTokens": usage.cache_hit_tokens,
    }


def usage_from_json(raw: Any) -> Usage | None:
    if not isinstance(raw, Mapping):
        return None
    return Usage(
        prompt_tokens=_as_int(raw.get("PromptTokens")),
        completion_tokens=_as_int(raw.get("CompletionTokens")),
        total_tokens=_as_int(raw.get("TotalTokens")),
        cache_hit_tokens=_as_int(raw.get("CacheHitTokens")),
    )


def snapshot_to_json(snapshot: Snapshot) -> dict[str, Any]:
    """``runtimestatus.Snapshot`` → Go 字段名键的 JSON。

    ``omitempty`` 对 struct 无效，因此每个键都会出现；零值时间写成
    ``"0001-01-01T00:00:00Z"``，``usage`` 为空时写 ``null``。
    """
    return {
        "SessionID": snapshot.session_id,
        "Phase": snapshot.phase,
        "Provider": snapshot.provider,
        "Model": snapshot.model,
        "Mode": snapshot.mode,
        "RequestID": snapshot.request_id,
        "Kind": snapshot.kind,
        "Label": snapshot.label,
        "ToolName": snapshot.tool_name,
        "TurnStartedAt": format_rfc3339_nano(snapshot.turn_started_at),
        "StageStartedAt": format_rfc3339_nano(snapshot.stage_started_at),
        "FinishedAt": format_rfc3339_nano(snapshot.finished_at),
        "Usage": usage_to_json(snapshot.usage),
        "Error": snapshot.error,
    }


def snapshot_from_json(raw: Any) -> Snapshot:
    if not isinstance(raw, Mapping):
        return Snapshot()
    return Snapshot(
        session_id=str(raw.get("SessionID") or ""),
        phase=str(raw.get("Phase") or ""),
        provider=str(raw.get("Provider") or ""),
        model=str(raw.get("Model") or ""),
        mode=str(raw.get("Mode") or ""),
        request_id=str(raw.get("RequestID") or ""),
        kind=str(raw.get("Kind") or ""),
        label=str(raw.get("Label") or ""),
        tool_name=str(raw.get("ToolName") or ""),
        turn_started_at=parse_rfc3339_nano(raw.get("TurnStartedAt")),
        stage_started_at=parse_rfc3339_nano(raw.get("StageStartedAt")),
        finished_at=parse_rfc3339_nano(raw.get("FinishedAt")),
        usage=usage_from_json(raw.get("Usage")),
        error=str(raw.get("Error") or ""),
    )


def format_rfc3339_nano(value: datetime | None) -> str:
    """``datetime`` → ``time.RFC3339Nano`` 文本；``None``（Go 零值）→ 零值文本。

    Go 的 ``2006-01-02T15:04:05.999999999Z07:00`` 会**去掉小数尾部的零**，
    小数部分为零时连小数点一起省略。
    """
    if value is None:
        return ZERO_TIME_TEXT
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    nanos = value.microsecond * 1000
    if nanos:
        text += "." + f"{nanos:09d}".rstrip("0")
    return text + "Z"


def parse_rfc3339_nano(raw: Any) -> datetime | None:
    """``time.RFC3339Nano`` 文本 → UTC aware ``datetime``；零值文本/非法值 → ``None``。

    与 Go 的 ``time.Time.UnmarshalJSON`` 一致：接受 ``Z`` 与 ``±HH:MM`` 偏移、
    任意位数（≥1）的小数秒。非法文本按零值处理（Go 会报错，但线上报文由本模块
    自己产生，这里选择不因为一个时间字段丢掉整帧）。
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    if text.endswith(("Z", "z")):
        body, offset = text[:-1], timezone.utc
    else:
        body, offset = _split_offset(text)
        if offset is None:
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(body, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=offset).astimezone(timezone.utc)
    return None


def _split_offset(text: str) -> tuple[str, timezone | None]:
    """把 ``...+08:00`` / ``...-05:30`` 拆成 ``(主体, timezone)``。"""
    for index in range(len(text) - 1, 9, -1):
        char = text[index]
        if char not in "+-":
            continue
        offset_text = text[index:]
        sign = 1 if offset_text[0] == "+" else -1
        parts = offset_text[1:].split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return text, None
        delta = timezone(sign * timedelta(hours=int(parts[0]), minutes=int(parts[1])))
        return text[:index], delta
    return text, None


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


# ---------------------------------------------------------------------------
# 日志级别（Go ``slog.Level.String`` / ``UnmarshalText``）
# ---------------------------------------------------------------------------

def level_text(level: int) -> str:
    """对应 Go ``slog.Level.String()``：``DEBUG`` / ``INFO`` / ``WARN`` / ``ERROR``。

    区间外的级别（Go 的 ``INFO+2`` 这类）按''最近的下界''归类，与 Go 的分档一致。
    """
    if level <= LEVEL_DEBUG:
        return "DEBUG"
    if level < LEVEL_WARN:
        return "INFO"
    if level < LEVEL_ERROR:
        return "WARN"
    return "ERROR"


def level_from_text(text: str) -> int:
    """对应 Go ``slog.Level.UnmarshalText``；无法识别时退回 ``INFO``。

    Go 还接受 ``INFO+2`` / ``WARN-4`` 这类偏移写法，这里同样支持。
    """
    value = (text or "").strip().upper()
    if not value:
        return LEVEL_INFO
    base, separator, delta_text = value.partition("+")
    if not separator:
        base, separator, delta_text = value.partition("-")
        if separator:
            delta_text = "-" + delta_text
    base = base.strip()
    if base not in _LEVELS_BY_TEXT:
        return LEVEL_INFO
    level = _LEVELS_BY_TEXT[base]
    if separator:
        try:
            level += int(delta_text)
        except ValueError:
            return LEVEL_INFO
    return level


def remote_notice_level(text: str) -> int:
    """对应 Go ``remoteNoticeLevel``：空文本或非法文本都退回 ``INFO``。"""
    if not (text or "").strip():
        return LEVEL_INFO
    return level_from_text(text)


def remote_notice_message(notice: Any) -> RemoteMessage:
    """对应 Go ``remoteNoticeMessage``：通知 → ``notice`` 帧，级别写成文本。"""
    from atri_x.delivery.delivery import fallback_output

    message = output_message(REMOTE_MSG_NOTICE, fallback_output(notice.outputs))
    message.level = level_text(notice.level)
    return message


def output_message(kind: str, out: Any) -> RemoteMessage:
    """对应 Go ``outputMessage``：文本输出取 ``text``，其余走 :func:`fallback_text`。"""
    from atri_x.delivery.delivery import Kind, fallback_text

    text = out.text if out.kind == Kind.TEXT else fallback_text(out)
    return RemoteMessage(type=kind, text=text)


def set_from_strings(values: Sequence[str] | None) -> set[str]:
    """对应 Go ``setFromStrings``：剥掉 ``cli:`` 前缀、去空。"""
    out: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if text.startswith("cli:"):
            text = text[len("cli:"):].strip()
        if text:
            out.add(text)
    return out
