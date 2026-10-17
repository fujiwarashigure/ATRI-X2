"""TUI 的消息类型与小状态对象。

对应 Go：``tui.go`` 顶部的 ``tui*Msg`` 类型、``completionState``、``tuiPasteBurst``、
``tuiNotice``，以及 ``clipboard.go`` 的 ``tuiClipboardMsg``（在
:mod:`atri_x.platform.cli.clipboard` 里定义，此处再导出）。

Go 里这些类型是 ``tea.Msg``（空接口），投进 ``chan tea.Msg``；Python 侧用
dataclass，靠 ``isinstance`` 分派（对应 Go 的 ``switch msg := msg.(type)``）。
警告级别统一是 ``logging`` 级别整数（``atri_x.delivery.delivery.Notice.level`` 同口径），
对应 Go 的 ``slog.Level``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.completion.completion import Item
from atri_x.platform.cli.clipboard import TuiClipboardMsg
from atri_x.runtime.status import Snapshot

#: 粘贴突发检测的时间常量。对应 Go 的 ``pasteBurst*`` 常量。
PASTE_BURST_CHAR_INTERVAL = timedelta(milliseconds=12)
PASTE_BURST_IDLE_TIMEOUT = timedelta(milliseconds=80)
PASTE_BURST_MIN_RUNES = 3

__all__ = [
    "PASTE_BURST_CHAR_INTERVAL",
    "PASTE_BURST_IDLE_TIMEOUT",
    "PASTE_BURST_MIN_RUNES",
    "CompletionState",
    "TuiClipboardMsg",
    "TuiFinishAssistantMsg",
    "TuiNotice",
    "TuiNoticeMsg",
    "TuiOutputMsg",
    "TuiPasteBurst",
    "TuiReasoningMsg",
    "TuiReplaceAssistantMsg",
    "TuiStatusMsg",
    "TuiStatusTickMsg",
]


@dataclass
class TuiOutputMsg:
    """追加一段助手正文。对应 Go ``tuiOutputMsg``。"""

    text: str = ""

    def __str__(self) -> str:  # pragma: no cover - 便于调试
        return self.text


@dataclass
class TuiReplaceAssistantMsg:
    """整体替换当前助手消息。对应 Go ``tuiReplaceAssistantMsg``。"""

    text: str = ""


@dataclass
class TuiFinishAssistantMsg:
    """结束当前助手消息。对应 Go ``tuiFinishAssistantMsg``。"""


@dataclass
class TuiReasoningMsg:
    """追加一段思维链文本。对应 Go ``tuiReasoningMsg``。"""

    text: str = ""


@dataclass
class TuiStatusMsg:
    """替换运行状态快照。对应 Go ``tuiStatusMsg``。"""

    snapshot: Snapshot = field(default_factory=Snapshot)


@dataclass
class TuiStatusTickMsg:
    """状态栏时钟跳动。对应 Go ``tuiStatusTickMsg``（载荷是 ``time.Time``）。"""

    now: datetime | None = None


@dataclass
class TuiNoticeMsg:
    """追加一条通知。对应 Go ``tuiNoticeMsg``。``level`` 是 ``logging`` 级别整数。"""

    text: str = ""
    level: int = logging.INFO


@dataclass
class TuiNotice:
    """通知面板里的一条。对应 Go ``tuiNotice``。"""

    text: str = ""
    level: int = logging.INFO


@dataclass
class CompletionState:
    """补全弹窗状态。对应 Go ``completionState``。"""

    base: str = ""
    items: list[Item] = field(default_factory=list)
    index: int = 0

    def visible(self) -> bool:
        """超过 1 个候选时才弹窗。对应 Go ``completionState.visible``。"""
        return len(self.items) > 1

    def current_text(self) -> str:
        """当前选中候选的插入文本。对应 Go ``completionState.currentText``。"""
        if not self.items or self.index < 0 or self.index >= len(self.items):
            return ""
        return self.items[self.index].text


@dataclass
class TuiPasteBurst:
    """Windows 控制台粘贴突发的检测器。对应 Go ``tuiPasteBurst``。

    Windows Console 把粘贴文本当成普通按键事件送出，因此粘贴内容里的换行会被
    当成 Enter。检测"短时间内连续到达若干字符"来判断当前处于粘贴流中，
    从而把 Enter 当成正文换行。
    """

    last_input: datetime | None = None
    runes: int = 0
    active: bool = False

    def observe_text(self, now: datetime, count: int) -> None:
        """记录一次文本输入。对应 Go ``(*tuiPasteBurst).observeText``。"""
        if count <= 0:
            return
        if self.last_input is None:
            gap: timedelta | None = None
        else:
            gap = now - self.last_input
        if gap is None or gap < timedelta(0) or gap > PASTE_BURST_IDLE_TIMEOUT:
            self.runes = count
            self.active = False
        elif self.active or gap <= PASTE_BURST_CHAR_INTERVAL:
            self.runes += count
        else:
            self.runes = count
        if self.runes >= PASTE_BURST_MIN_RUNES:
            self.active = True
        self.last_input = now

    def should_insert_enter(self, now: datetime) -> bool:
        """这次 Enter 是否应该当成换行。对应 Go ``shouldInsertEnter``。"""
        if (
            not self.active
            or self.last_input is None
            or now - self.last_input > PASTE_BURST_IDLE_TIMEOUT
        ):
            self.reset()
            return False
        self.last_input = now
        return True

    def reset(self) -> None:
        """复位。对应 Go ``(*tuiPasteBurst).reset``。"""
        self.last_input = None
        self.runes = 0
        self.active = False


def utc_now() -> datetime:
    """当前 UTC 时间（TUI 里所有时间的口径）。"""
    return datetime.now(timezone.utc)


def normalize_tui_input_paste(text: str) -> str:
    """把 CRLF / CR 归一成 LF。对应 Go ``normalizeTUIInputPaste``。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def as_msg(value: Any) -> Any:
    """Go 里 ``chan tea.Msg`` 收的是空接口；Python 侧保持原样即可。"""
    return value
