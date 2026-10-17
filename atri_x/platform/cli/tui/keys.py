"""TUI 的按键与鼠标事件：``bubbletea`` 的 ``KeyMsg`` / ``MouseMsg`` 等价物。

对应 Go：``tea.KeyMsg`` / ``tea.MouseMsg``（``tui.go`` / ``tui_mouse.go`` 消费它们）。

Python 没有 bubbletea，这里用两个 dataclass 承载同样的信息，并复刻 ``KeyMsg.String()``
这个**规范化键名**（``"ctrl+c"`` / ``"alt+h"`` / ``"shift+tab"`` / ``"up"`` …），
因为 :mod:`atri_x.platform.cli.tui.model` 的分支判断完全按它来，逐字对齐才能保持行为一致。

按键来源（真实终端）在 :mod:`atri_x.platform.cli.tui.program` 里；本模块只描述数据。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 键类型常量。对应 bubbletea 的 ``KeyType`` 里本 TUI 用到的子集。
KEY_RUNES = "runes"
KEY_ENTER = "enter"
KEY_ESC = "esc"
KEY_TAB = "tab"
KEY_BACKSPACE = "backspace"
KEY_DELETE = "delete"
KEY_UP = "up"
KEY_DOWN = "down"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_PGUP = "pgup"
KEY_PGDOWN = "pgdown"
KEY_HOME = "home"
KEY_END = "end"
KEY_CTRL_C = "ctrl+c"
KEY_CTRL_D = "ctrl+d"
KEY_CTRL_H = "ctrl+h"
KEY_CTRL_I = "ctrl+i"
KEY_CTRL_J = "ctrl+j"
KEY_CTRL_K = "ctrl+k"
KEY_CTRL_U = "ctrl+u"
KEY_CTRL_V = "ctrl+v"
KEY_SHIFT_TAB = "shift+tab"
KEY_BACKTAB = "backtab"
KEY_UNKNOWN = "unknown"

#: 鼠标动作常量。对应 bubbletea ``MouseAction``。
MOUSE_ACTION_PRESS = "press"
MOUSE_ACTION_RELEASE = "release"
MOUSE_ACTION_MOTION = "motion"

#: 鼠标按键常量。对应 bubbletea ``MouseButton``。
MOUSE_BUTTON_LEFT = "left"
MOUSE_BUTTON_MIDDLE = "middle"
MOUSE_BUTTON_RIGHT = "right"
MOUSE_BUTTON_WHEEL_UP = "wheelup"
MOUSE_BUTTON_WHEEL_DOWN = "wheeldown"
MOUSE_BUTTON_WHEEL_LEFT = "wheelleft"
MOUSE_BUTTON_WHEEL_RIGHT = "wheelright"
MOUSE_BUTTON_NONE = "none"

#: 鼠标事件类型常量。对应 bubbletea ``MouseWheelUp`` 等。
MOUSE_WHEEL_UP = "mouse_wheel_up"
MOUSE_WHEEL_DOWN = "mouse_wheel_down"

__all__ = [
    "KEY_BACKSPACE",
    "KEY_BACKTAB",
    "KEY_CTRL_C",
    "KEY_CTRL_D",
    "KEY_CTRL_H",
    "KEY_CTRL_I",
    "KEY_CTRL_J",
    "KEY_CTRL_K",
    "KEY_CTRL_U",
    "KEY_CTRL_V",
    "KEY_DELETE",
    "KEY_DOWN",
    "KEY_END",
    "KEY_ENTER",
    "KEY_ESC",
    "KEY_HOME",
    "KEY_LEFT",
    "KEY_PGDOWN",
    "KEY_PGUP",
    "KEY_RIGHT",
    "KEY_RUNES",
    "KEY_SHIFT_TAB",
    "KEY_TAB",
    "KEY_UNKNOWN",
    "KEY_UP",
    "MOUSE_ACTION_MOTION",
    "MOUSE_ACTION_PRESS",
    "MOUSE_ACTION_RELEASE",
    "MOUSE_BUTTON_LEFT",
    "MOUSE_BUTTON_MIDDLE",
    "MOUSE_BUTTON_NONE",
    "MOUSE_BUTTON_RIGHT",
    "MOUSE_BUTTON_WHEEL_DOWN",
    "MOUSE_BUTTON_WHEEL_LEFT",
    "MOUSE_BUTTON_WHEEL_RIGHT",
    "MOUSE_BUTTON_WHEEL_UP",
    "MOUSE_WHEEL_DOWN",
    "MOUSE_WHEEL_UP",
    "KeyMsg",
    "MouseMsg",
]


@dataclass
class KeyMsg:
    """一次按键。对应 ``tea.KeyMsg``。

    ``runes`` 是本次输入的字符（``type == KEY_RUNES`` 时有效）；
    ``paste`` 表示这是终端粘贴事件；``alt`` 表示按住了 Alt。
    """

    type: str = KEY_RUNES
    runes: str = ""
    alt: bool = False
    paste: bool = False

    def string(self) -> str:
        """规范化键名。对应 ``tea.KeyMsg.String()``。

        ``alt`` 参与时返回 ``alt+<底层键名>``（例如 ``alt+h`` / ``alt+backspace``）。
        """
        name = self.name()
        if self.alt:
            return "alt+" + name
        return name

    def name(self) -> str:
        """不含修饰符的键名。"""
        if self.type == KEY_RUNES:
            return self.runes
        return self.type

    def rune_count(self) -> int:
        """本次输入的字符数。对应 Go ``len(keyMsg.Runes)``。"""
        return len(self.runes)

    @classmethod
    def runes_key(cls, value: str, *, paste: bool = False, alt: bool = False) -> "KeyMsg":
        return cls(type=KEY_RUNES, runes=value, alt=alt, paste=paste)

    @classmethod
    def special(cls, name: str, *, alt: bool = False) -> "KeyMsg":
        return cls(type=name, alt=alt)


@dataclass
class MouseMsg:
    """一次鼠标事件。对应 ``tea.MouseMsg`` / ``tea.MouseEvent``。"""

    x: int = 0
    y: int = 0
    action: str = MOUSE_ACTION_PRESS
    button: str = MOUSE_BUTTON_NONE
    type: str = ""
    alt: bool = False
    ctrl: bool = False
    shift: bool = False

    def event(self) -> "MouseMsg":
        """对应 Go ``tea.MouseEvent(msg)``（本类即事件本身）。"""
        return self

    def is_wheel(self) -> bool:
        """对应 ``tea.MouseEvent.IsWheel()``。"""
        return self.button in (
            MOUSE_BUTTON_WHEEL_UP,
            MOUSE_BUTTON_WHEEL_DOWN,
            MOUSE_BUTTON_WHEEL_LEFT,
            MOUSE_BUTTON_WHEEL_RIGHT,
        )
