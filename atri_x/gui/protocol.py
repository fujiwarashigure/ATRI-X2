"""GUI 侧的协议解析与对话模型（纯逻辑，不依赖 tkinter，可离线测试）。

前端协议见 ``docs/frontend-api.md``；线上一帧由
:class:`atri_x.platform.cli.protocol.RemoteMessage` 解析（**唯一出处**，不在这里另抄
一份字段映射）。本模块只做「把一帧变成界面状态」这部分纯函数与状态机：

- :class:`ChatLine` / :class:`Transcript` —— 聊天记录与流式累积；
- :func:`describe_status` —— 状态栏文本（复用 :mod:`atri_x.runtime.status`）；
- :func:`apply_completion` —— 把补全候选按**字节偏移**替换进输入框文本；
- :func:`status_phase_label` —— 阶段的中文标签。

设计原则：GUI 只读这一层，不碰网络、不碰 Tk；因此 ``tests/test_gui_protocol.py``
可以把「服务端发什么 → 界面显示什么」当纯函数断言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from atri_x.completion.completion import Item
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_CHAT,
    REMOTE_MSG_COMPLETE_RESULT,
    REMOTE_MSG_ERROR,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_HISTORY_RESULT,
    REMOTE_MSG_NOTICE,
    REMOTE_MSG_REASONING,
    REMOTE_MSG_SESSIONS_RESULT,
    REMOTE_MSG_STATUS,
    REMOTE_MSG_STREAM_APPEND,
    REMOTE_MSG_STREAM_FINISH,
    REMOTE_MSG_STREAM_REPLACE,
    HistoryEntry,
    RemoteMessage,
    SessionInfo,
    parse_rfc3339_nano,
)
from atri_x.runtime.status import Snapshot, format_compact

__all__ = [
    "FRAME_HANDLERS",
    "FRAME_SPECIAL",
    "ChatLine",
    "HistoryItem",
    "ROLE_ASSISTANT",
    "ROLE_ERROR",
    "ROLE_NOTICE",
    "ROLE_REASONING",
    "ROLE_USER",
    "SessionItem",
    "Transcript",
    "apply_completion",
    "apply_frame",
    "decode_history",
    "decode_sessions",
    "describe_status",
    "format_clock",
    "format_notice",
    "iter_messages",
    "phase_label",
    "status_from_frame",
]

#: 聊天记录的行角色（Tk 侧按它选 tag/颜色）。
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_REASONING = "reasoning"
ROLE_NOTICE = "notice"
ROLE_ERROR = "error"

#: 阶段 → 中文短标签。缺的阶段直接用英文原值（协议侧保证是这几个值）。
_PHASE_LABELS = {
    "idle": "空闲",
    "preparing": "准备中",
    "llm": "生成中",
    "tool": "工具",
    "sending": "发送中",
    "done": "完成",
    "error": "出错",
}


@dataclass
class ChatLine:
    """聊天记录里的一行。"""

    role: str
    text: str = ""
    level: str = ""
    at: str = ""
    #: 流式行（还在追加中）为 ``True``；定稿后置 ``False``。
    streaming: bool = False

    def to_dict(self) -> dict[str, Any]:
        """给测试/日志用的普通字典。"""
        return {
            "role": self.role,
            "text": self.text,
            "level": self.level,
            "at": self.at,
            "streaming": self.streaming,
        }


@dataclass
class Transcript:
    """聊天记录 + 流式累积状态机。

    ``RemoteClient`` 收到服务端帧后调用对应方法，界面按 :attr:`lines` 重绘即可。
    流式三段（``stream_append`` / ``stream_replace`` / ``stream_finish``）合并成**一行**
    助手消息；``chat`` 到达时用它替换流式文本（Hook 可能改写输出，两处文本可能不同）。
    """

    lines: list[ChatLine] = field(default_factory=list)
    #: 当前流式行在 ``lines`` 里的下标；没有流式行时为 ``-1``。
    stream_index: int = -1

    # ---------------------------------------------------------------- 查询

    @property
    def streaming(self) -> bool:
        """是否有正在生成的助手消息。"""
        return self.stream_index >= 0 and 0 <= self.stream_index < len(self.lines)

    def text(self) -> str:
        """整段记录拼成纯文本（复制全部/落日志用）。"""
        out: list[str] = []
        for line in self.lines:
            prefix = {
                ROLE_USER: "你",
                ROLE_ASSISTANT: "ATRI",
                ROLE_REASONING: "思考",
                ROLE_NOTICE: "通知",
                ROLE_ERROR: "错误",
            }.get(line.role, line.role)
            out.append(f"[{line.at}] {prefix}: {line.text}" if line.at else f"{prefix}: {line.text}")
        return "\n".join(out)

    # ---------------------------------------------------------------- 写入

    def add_user(self, text: str, at: str = "") -> ChatLine:
        """用户消息；发出去的同时把流式行收尾（上一轮可能还没收到 stream_finish）。"""
        self.stream_finish()
        return self._append(ChatLine(role=ROLE_USER, text=text, at=_now(at)))

    def add_notice(self, text: str, level: str = "", at: str = "") -> ChatLine:
        """通知（Cron 结果、Hook 输出等），单独成行，不参与流式。"""
        return self._append(ChatLine(role=ROLE_NOTICE, text=text, level=level, at=_now(at)))

    def add_error(self, text: str, at: str = "") -> ChatLine:
        """错误行。"""
        return self._append(ChatLine(role=ROLE_ERROR, text=text, at=_now(at)))

    def add_reasoning(self, text: str, at: str = "") -> ChatLine:
        """推理片段：追加到上一条推理行，避免一行一个字地把界面刷满。"""
        if self.lines:
            last = self.lines[-1]
            if last.role == ROLE_REASONING and not last.streaming:
                last.text += text
                return last
        return self._append(ChatLine(role=ROLE_REASONING, text=text, at=_now(at)))

    def add_chat(self, text: str, at: str = "") -> ChatLine:
        """完整回复。若正在流式生成，则用它定稿那一行（文本以服务端最终值为准）。"""
        if self.streaming:
            line = self.lines[self.stream_index]
            line.text = text
            line.streaming = False
            line.at = _now(at)
            self.stream_index = -1
            return line
        return self._append(ChatLine(role=ROLE_ASSISTANT, text=text, at=_now(at)))

    def stream_append(self, text: str) -> ChatLine:
        """流式增量：没有在流就开一行。"""
        line = self._ensure_stream()
        line.text += text
        return line

    def stream_replace(self, text: str) -> ChatLine:
        """流式定稿文本替换（可能和增量拼接结果不同）。"""
        line = self._ensure_stream()
        line.text = text
        return line

    def stream_finish(self) -> ChatLine | None:
        """结束流式行；没有在流时返回 ``None``。"""
        if not self.streaming:
            return None
        line = self.lines[self.stream_index]
        line.streaming = False
        self.stream_index = -1
        if not line.text:
            self.lines.pop()
        return line

    def clear(self) -> None:
        """清空记录（新建会话）。"""
        self.lines.clear()
        self.stream_index = -1

    # ---------------------------------------------------------------- 内部

    def _append(self, line: ChatLine) -> ChatLine:
        self.lines.append(line)
        return line

    def _ensure_stream(self) -> ChatLine:
        if self.streaming:
            return self.lines[self.stream_index]
        self.stream_index = len(self.lines)
        self.lines.append(ChatLine(role=ROLE_ASSISTANT, at=_now(), streaming=True))
        return self.lines[self.stream_index]


# ---------------------------------------------------------------------------
# 会话列表 / 历史（Python 版新增帧的界面形态）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionItem:
    """侧边栏里的一个会话条目。"""

    id: str
    title: str = ""
    mode: str = ""
    status: str = ""
    updated_at: str = ""
    current: bool = False

    @property
    def label_text(self) -> str:
        """列表行文本：``● 标题 · 07-01 12:00``（当前会话带圆点）。"""
        title = (self.title or "").strip() or "(未命名会话)"
        moment = format_clock(self.updated_at)
        marker = "● " if self.current else "  "
        return f"{marker}{title}" + (f" · {moment}" if moment else "")


@dataclass(frozen=True)
class HistoryItem:
    """历史消息（只含用户/助手两类角色）。"""

    role: str = ROLE_USER
    text: str = ""
    at: str = ""


def decode_sessions(message: RemoteMessage) -> list[SessionItem]:
    """``sessions_result`` 帧 → 会话条目列表（其它帧返回空）。"""
    if (message.type or "").strip() != REMOTE_MSG_SESSIONS_RESULT:
        return []
    return [
        SessionItem(
            id=item.id,
            title=item.title,
            mode=item.mode,
            status=item.status,
            updated_at=item.updated_at,
            current=item.current,
        )
        for item in message.sessions
        if isinstance(item, SessionInfo)
    ]


def decode_history(message: RemoteMessage) -> list[HistoryItem]:
    """``history_result`` 帧 → 历史消息列表（其它帧返回空）。"""
    if (message.type or "").strip() != REMOTE_MSG_HISTORY_RESULT:
        return []
    out: list[HistoryItem] = []
    for item in message.history:
        if not isinstance(item, HistoryEntry):
            continue
        if item.role not in (ROLE_USER, ROLE_ASSISTANT):
            continue
        out.append(HistoryItem(role=item.role, text=item.text, at=item.at))
    return out


def format_clock(value: str) -> str:
    """RFC3339 文本 → ``MM-DD HH:MM``（本地时区；解析失败返回空串）。"""
    moment = parse_rfc3339_nano(value)
    if moment is None:
        return ""
    return moment.astimezone().strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 帧 → 界面状态
# ---------------------------------------------------------------------------


def phase_label(phase: str) -> str:
    """阶段的中文标签（空值按空闲处理）。"""
    key = (phase or "").strip()
    if not key:
        return _PHASE_LABELS["idle"]
    return _PHASE_LABELS.get(key, key)


def describe_status(snapshot: Snapshot | None) -> str:
    """状态栏文本：``生成中 · 00:03 · deepseek/deepseek-v4 · tokens 1,234``。

    复用 :func:`atri_x.runtime.status.format_compact` 保证与 TUI 口径一致；
    ``idle`` 且无模型信息时返回空串（界面显示「空闲」）。
    """
    if snapshot is None:
        return ""
    text = format_compact(snapshot)
    if text.startswith("idle"):
        return ""
    return text


def format_notice(text: str, level: str = "") -> str:
    """通知行文本：``[WARN] xxx``；级别为空时不加前缀。"""
    prefix = f"[{(level or '').strip().upper()}] " if (level or "").strip() else ""
    return prefix + text


def apply_completion(text: str, item: Item | Mapping[str, Any] | None, cursor: int | None = None) -> str:
    """把补全候选应用到输入文本，返回新文本。

    协议里的 ``replace_start`` / ``replace_end`` 是**字节**偏移（见
    ``docs/frontend-api.md``）；这里按 UTF-8 字节切片，再用选中的 ``item.text``
    替换该区间。区间非法或没有候选时只把候选插到 ``cursor`` 处。
    """
    insertion = _item_text(item)
    if not insertion:
        return text
    body = text.encode("utf-8")
    start = _item_int(item, "replace_start")
    end = _item_int(item, "replace_end")
    if start < 0 or end < start or end > len(body):
        start = len(body) if cursor is None else _clamp_byte(text, cursor)
        end = start
    merged = body[:start] + insertion.encode("utf-8") + body[end:]
    return merged.decode("utf-8", "replace")


def _item_text(item: Item | Mapping[str, Any] | None) -> str:
    if item is None:
        return ""
    if isinstance(item, Mapping):
        return str(item.get("Text") or item.get("text") or "")
    return str(getattr(item, "text", "") or "")


def _item_int(item: Item | Mapping[str, Any] | None, key: str) -> int:
    """取候选里的整数字段；``Result``/``dataclass`` 两种命名（``ReplaceStart``/``replace_start``）都认。"""
    if item is None:
        return -1
    if isinstance(item, Mapping):
        value = item.get(key)
        if value is None:
            value = item.get(_snake(key))
        if value is None:
            value = item.get(_pascal(key))
    else:
        value = getattr(item, _snake(key), None)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


def _clamp_byte(text: str, cursor: int) -> int:
    """字符下标 → UTF-8 字节下标（越界时夹到边界）。"""
    return len(text[: max(0, min(len(text), cursor))].encode("utf-8"))


def _snake(key: str) -> str:
    """``ReplaceStart`` → ``replace_start``（Item 是 dataclass，键名两种都见）。"""
    out: list[str] = []
    for index, char in enumerate(key):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _pascal(key: str) -> str:
    """``replace_start`` → ``ReplaceStart``（JSON 里的 Item 键名）。"""
    return "".join(part[:1].upper() + part[1:] for part in key.split("_") if part)


def _now(at: str = "") -> str:
    """``HH:MM:SS``；调用方给了就原样用（测试可注入固定时间）。"""
    return at or datetime.now().strftime("%H:%M:%S")


#: 帧类型 → ``Transcript`` 方法名的映射（GUI 与测试共用，避免两处 switch）。
FRAME_HANDLERS: dict[str, str] = {
    REMOTE_MSG_CHAT: "add_chat",
    REMOTE_MSG_NOTICE: "add_notice",
    REMOTE_MSG_REASONING: "add_reasoning",
    REMOTE_MSG_STREAM_APPEND: "stream_append",
    REMOTE_MSG_STREAM_REPLACE: "stream_replace",
    REMOTE_MSG_STREAM_FINISH: "stream_finish",
    REMOTE_MSG_ERROR: "add_error",
}

#: 需要特殊参数（不是单纯 text）的帧类型。
FRAME_SPECIAL = frozenset(
    {
        REMOTE_MSG_NOTICE,
        REMOTE_MSG_STATUS,
        REMOTE_MSG_HELLO_OK,
        REMOTE_MSG_COMPLETE_RESULT,
        REMOTE_MSG_SESSIONS_RESULT,
        REMOTE_MSG_HISTORY_RESULT,
    }
)


def apply_frame(transcript: Transcript, message: RemoteMessage) -> bool:
    """把一帧写进 :class:`Transcript`；返回是否真的改动了记录。

    状态帧、握手帧、补全结果帧不动记录（分别由状态栏/连接层/输入框处理）。
    """
    kind = (message.type or "").strip()
    if kind == REMOTE_MSG_STREAM_FINISH:
        return transcript.stream_finish() is not None
    handler = FRAME_HANDLERS.get(kind)
    if handler is None:
        return False
    method = getattr(transcript, handler)
    if kind == REMOTE_MSG_NOTICE:
        method(format_notice(message.text, message.level), message.level)
    else:
        method(message.text)
    return True


def status_from_frame(message: RemoteMessage) -> Snapshot | None:
    """``status`` 帧 → 快照；不是状态帧时返回 ``None``。"""
    if (message.type or "").strip() != REMOTE_MSG_STATUS:
        return None
    snapshot = message.snapshot
    return snapshot if isinstance(snapshot, Snapshot) else None


def iter_messages(raw_frames: Iterable[Any]) -> Iterable[RemoteMessage]:
    """把 JSON 帧（dict）批量转成 :class:`RemoteMessage`（测试与调试用）。"""
    for frame in raw_frames:
        if isinstance(frame, RemoteMessage):
            yield frame
        elif isinstance(frame, Mapping):
            yield RemoteMessage.from_json(frame)
