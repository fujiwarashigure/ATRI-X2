"""多行输入框：``bubbles/textarea`` 的最小等价物。

对应 Go：``tui.go`` / ``tui_copy.go`` 里 ``textarea.Model`` 的用法。

只实现本 TUI 真正消费的那部分语义：``value``（整段文本）、光标、``char_limit``、
``width`` / ``height``、以及按键更新。行为对齐点：

* ``CharLimit`` 是**码点数**上限（Go 的 textarea 按 rune 计数），插入时截断而不是拒绝。
* ``Line()`` 是光标所在行号；``LineCount()`` 是总行数；``LineInfo()`` 给出
  ``StartColumn`` / ``ColumnOffset``，:mod:`~atri_x.platform.cli.tui.model` 用它们
  把光标换算成"整段文本里的码点偏移"。
* 光标在整段文本里的位置是本实现的**唯一真源**（``cursor``）；``line()`` /
  ``line_info()`` 由它派生。Go 里以"行 + 列"为真源，两者在等价的编辑序列下结果一致。
* ``Blur()`` / ``Focus()`` 只影响 ``focused``（copy mode 进入时失焦、退出时回焦）；
  本实现不在失焦时改变渲染内容（Go 的 textarea 也只是换样式）。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.platform.cli.tui.keys import (
    KEY_BACKSPACE,
    KEY_CTRL_D,
    KEY_CTRL_H,
    KEY_DELETE,
    KEY_DOWN,
    KEY_END,
    KEY_ENTER,
    KEY_HOME,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_RUNES,
    KEY_UP,
    KeyMsg,
)

#: 默认字符上限。对应 Go ``tuiInputCharLimit``。
DEFAULT_CHAR_LIMIT = 4096

__all__ = ["DEFAULT_CHAR_LIMIT", "LineInfo", "TextArea"]


@dataclass
class LineInfo:
    """光标所在行的信息。对应 ``textarea.LineInfo``。"""

    #: 光标行行首在整段文本里的码点偏移。
    start_column: int = 0
    #: 行内列偏移（本实现恒为 0，保留字段以便与 Go 对齐）。
    column_offset: int = 0
    #: 光标行在文本里的列号（码点）。
    column: int = 0
    #: 光标行的高度（按显示宽度折行后的行数）。
    height: int = 1
    #: 光标行的字符数。
    char_offset: int = 0


@dataclass
class TextArea:
    """多行输入框。对应 ``textarea.Model``。"""

    char_limit: int = DEFAULT_CHAR_LIMIT
    width: int = 0
    height: int = 1
    focused: bool = True
    _value: str = ""
    _cursor: int = 0
    _desired_column: int = -1

    # -- 取值 ---------------------------------------------------------------
    def value(self) -> str:
        """整段文本。对应 ``Value()``。"""
        return self._value

    def cursor(self) -> int:
        """光标在整段文本里的码点偏移。"""
        return self._cursor

    def set_value(self, value: str) -> None:
        """整体替换并把光标放到末尾。对应 ``SetValue``。"""
        self._value = self._clamp(value)
        self._cursor = len(self._value)
        self._desired_column = -1

    def reset(self) -> None:
        """清空。对应 ``Reset()``。"""
        self._value = ""
        self._cursor = 0
        self._desired_column = -1

    def insert_string(self, value: str) -> None:
        """在光标处插入文本（超 ``char_limit`` 的部分被截断）。对应 ``InsertString``。"""
        self._insert(value)

    def set_height(self, value: int) -> None:
        self.height = value

    def set_width(self, value: int) -> None:
        self.width = value

    def focus(self) -> None:
        self.focused = True

    def blur(self) -> None:
        self.focused = False

    # -- 光标位置 -----------------------------------------------------------
    def lines(self) -> list[str]:
        """按 ``\\n`` 拆行。"""
        return self._value.split("\n")

    def line(self) -> int:
        """光标所在行号。对应 ``Line()``。"""
        return self._value.count("\n", 0, self._cursor)

    def line_count(self) -> int:
        """总行数。对应 ``LineCount()``。"""
        return self._value.count("\n") + 1

    def line_info(self) -> LineInfo:
        """光标行信息。对应 ``LineInfo()``。"""
        row = self.line()
        rows = self.lines()
        start = sum(len(item) + 1 for item in rows[:row])
        return LineInfo(
            start_column=start,
            column_offset=0,
            column=self._cursor - start,
            height=1,
            char_offset=len(rows[row]) if row < len(rows) else 0,
        )

    def cursor_up(self) -> None:
        """光标上移一行（列尽量保持）。对应 ``CursorUp``。"""
        self._move_vertical(-1)

    def set_cursor(self, column: int) -> None:
        """设置行内列。对应 ``SetCursor``。"""
        row = self.line()
        rows = self.lines()
        start = sum(len(item) + 1 for item in rows[:row])
        length = len(rows[row]) if row < len(rows) else 0
        self._cursor = start + max(0, min(column, length))

    # -- 更新 ---------------------------------------------------------------
    def update(self, msg: KeyMsg) -> None:
        """处理一个按键。对应 ``textarea.Update``。"""
        name = msg.string()
        if name == KEY_ENTER:
            self._insert("\n")
            return
        if name in (KEY_BACKSPACE, KEY_CTRL_H):
            self._delete_backward()
            return
        if name in (KEY_DELETE, KEY_CTRL_D):
            self._delete_forward()
            return
        if name == KEY_LEFT:
            self._cursor = max(0, self._cursor - 1)
            self._desired_column = -1
            return
        if name == KEY_RIGHT:
            self._cursor = min(len(self._value), self._cursor + 1)
            self._desired_column = -1
            return
        if name == KEY_UP:
            self._move_vertical(-1)
            return
        if name == KEY_DOWN:
            self._move_vertical(1)
            return
        if name == KEY_HOME:
            self._cursor -= self.line_info().column
            self._desired_column = 0
            return
        if name == KEY_END:
            info = self.line_info()
            self._cursor += info.char_offset - info.column
            self._desired_column = -1
            return
        if msg.type == KEY_RUNES and not msg.alt and msg.runes:
            self._insert(msg.runes)

    # -- 内部 ---------------------------------------------------------------
    def _insert(self, value: str) -> None:
        if not value:
            return
        head = self._value[: self._cursor]
        tail = self._value[self._cursor :]
        room = self.char_limit - len(self._value)
        if room <= 0:
            return
        inserted = value[:room]
        self._value = head + inserted + tail
        self._cursor = len(head) + len(inserted)
        self._desired_column = -1

    def _delete_backward(self) -> None:
        if self._cursor <= 0:
            return
        self._value = self._value[: self._cursor - 1] + self._value[self._cursor :]
        self._cursor -= 1
        self._desired_column = -1

    def _delete_forward(self) -> None:
        if self._cursor >= len(self._value):
            return
        self._value = self._value[: self._cursor] + self._value[self._cursor + 1 :]
        self._desired_column = -1

    def _move_vertical(self, delta: int) -> None:
        rows = self.lines()
        row = self.line()
        info = self.line_info()
        if self._desired_column < 0:
            self._desired_column = info.column
        target = row + delta
        if target < 0 or target >= len(rows):
            return
        start = sum(len(item) + 1 for item in rows[:target])
        self._cursor = start + min(self._desired_column, len(rows[target]))

    def _clamp(self, value: str) -> str:
        if self.char_limit and len(value) > self.char_limit:
            return value[: self.char_limit]
        return value
