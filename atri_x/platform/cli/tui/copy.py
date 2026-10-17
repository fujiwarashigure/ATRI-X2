"""TUI 的 copy mode：vi 风格的区域选择、词移动、搜索与复制。

对应 Go：``internal/platform/cli/tui_copy.go``。

Go 把这些方法挂在 ``tuiModel`` 上；Python 侧拆成 :class:`CopyModeMixin`，由
:class:`atri_x.platform.cli.tui.model.TuiModel` 继承 —— 方法与 ``self`` 上的
``copy_state`` / ``viewport`` / ``notice_viewport`` / ``clipboard`` / ``notices``
等字段交互，字段契约与 Go 的 ``tuiModel`` 同名（snake_case）。
这样拆分让 ``model.py`` 保持在 600 行以内，同时不引入任何跨模块循环导入。

与 Go 的差异（仅形式）：

* ``tea.KeyMsg`` → :class:`atri_x.platform.cli.tui.keys.KeyMsg`，``msg.String()``
  的键名逐字一致。
* ``(tea.Model, tea.Cmd)`` → 直接改自身状态并返回 ``None``（Go 里 copy mode
  从不返回非 nil 的 Cmd）。
* ``sort.Slice`` 对两个光标排序 → :func:`ordered_cursors` 里的显式比较。
* 行内列一律是**码点**下标（Go 的 ``copyCursor.Col`` 也是 ``[]rune`` 下标，两者一致）；
  ``colFromDisplayWidth`` 用于把鼠标的显示列换算成码点列。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from atri_x.platform.cli.tui import theme
from atri_x.platform.cli.tui.keys import (
    KEY_BACKSPACE,
    KEY_CTRL_D,
    KEY_CTRL_H,
    KEY_CTRL_U,
    KEY_DOWN,
    KEY_END,
    KEY_ENTER,
    KEY_ESC,
    KEY_HOME,
    KEY_LEFT,
    KEY_PGDOWN,
    KEY_PGUP,
    KEY_RIGHT,
    KEY_RUNES,
    KEY_UP,
    KeyMsg,
)
from atri_x.platform.cli.tui.style import wrap_display_width
from atri_x.utils.runes import rune_width

#: copy mode 的模式常量。对应 Go ``tuiMode``。
MODE_INPUT = "input"
MODE_COPY_NORMAL = "copy_normal"
MODE_COPY_VISUAL_CHAR = "visual_char"
MODE_COPY_VISUAL_LINE = "visual_line"
MODE_COPY_SEARCH = "copy_search"

#: 界面分区常量。对应 Go ``tuiRegion``。
REGION_NONE = "none"
REGION_CHAT = "chat"
REGION_NOTICE = "notice"
REGION_INPUT = "input"
REGION_COMPLETION = "completion"

__all__ = [
    "MODE_COPY_NORMAL",
    "MODE_COPY_SEARCH",
    "MODE_COPY_VISUAL_CHAR",
    "MODE_COPY_VISUAL_LINE",
    "MODE_INPUT",
    "REGION_CHAT",
    "REGION_COMPLETION",
    "REGION_INPUT",
    "REGION_NONE",
    "REGION_NOTICE",
    "CopyCursor",
    "CopyModeMixin",
    "CopyModeState",
    "CopyRange",
    "clamp_cursor",
    "clamp_int",
    "col_from_display_width",
    "line_in_range",
    "line_in_visual_char_range",
    "mode_name",
    "next_word_end",
    "next_word_start",
    "ordered_cursors",
    "ordered_lines",
    "plural",
    "previous_word_start",
    "region_name",
    "render_char_selection",
    "render_cursor",
    "render_search_matches",
    "rune_len",
    "search_match_ranges",
    "selection_cols_for_line",
    "slice_runes_from",
    "slice_runes_inclusive",
    "slice_runes_to",
    "split_lines",
]


# ---------------------------------------------------------------------------
# 纯助手
# ---------------------------------------------------------------------------

def clamp_int(value: int, low: int, high: int) -> int:
    """对应 Go ``clampInt``。"""
    if high < low:
        return low
    if value < low:
        return low
    if value > high:
        return high
    return value


def rune_len(text: str) -> int:
    """对应 Go ``runeLen``。"""
    return len(text)


def plural(count: int, word: str) -> str:
    """对应 Go ``plural``。"""
    if count == 1:
        return f"1 {word}"
    return f"{count} {word}s"


def split_lines(text: str) -> list[str]:
    """去掉尾随换行后按 ``\\n`` 拆行。对应 Go ``splitLines``。"""
    text = text.rstrip("\n")
    if text == "":
        return []
    return text.split("\n")


def ordered_lines(a: int, b: int) -> tuple[int, int]:
    """对应 Go ``orderedLines``。"""
    return (a, b) if a <= b else (b, a)


def region_name(region: str) -> str:
    """对应 Go ``regionName``。"""
    return {
        REGION_CHAT: "chat",
        REGION_NOTICE: "notices",
        REGION_INPUT: "input",
        REGION_COMPLETION: "completion",
    }.get(region, "none")


def mode_name(mode: str) -> str:
    """对应 Go ``modeName``。"""
    return {
        MODE_COPY_NORMAL: "copy",
        MODE_COPY_VISUAL_CHAR: "visual",
        MODE_COPY_VISUAL_LINE: "visual line",
        MODE_COPY_SEARCH: "search",
    }.get(mode, "input")


def is_copy_word_rune(char: str) -> bool:
    """对应 Go ``isCopyWordRune``：字母/数字/下划线。"""
    return char == "_" or char.isalnum()


def col_from_display_width(text: str, width: int) -> int:
    """把显示列换算成码点列。对应 Go ``colFromDisplayWidth``。"""
    if width <= 0:
        return 0
    current = 0
    for index, char in enumerate(text):
        current += rune_width(char)
        if current > width:
            return index
    return max(0, rune_len(text) - 1)


@dataclass
class CopyCursor:
    """copy mode 的光标（行 + 行内码点列）。对应 Go ``copyCursor``。"""

    line: int = 0
    col: int = 0


@dataclass
class CopyRange:
    """一段命中区间（含两端）。对应 Go ``copyRange``。"""

    start: int = 0
    end: int = 0


@dataclass
class CopyModeState:
    """copy mode 的全部状态。对应 Go ``copyModeState``。"""

    mode: str = MODE_INPUT
    region: str = REGION_NONE
    cursor: CopyCursor = field(default_factory=CopyCursor)
    anchor: CopyCursor = field(default_factory=CopyCursor)
    search_input: str = ""
    search_query: str = ""
    status: str = ""

    def active(self) -> bool:
        """是否处于 copy mode。对应 Go ``copyModeState.active``。"""
        return self.mode != MODE_INPUT

    def visual(self) -> bool:
        """是否处于可视模式。对应 Go ``copyModeState.visual``。"""
        return self.mode in (MODE_COPY_VISUAL_CHAR, MODE_COPY_VISUAL_LINE)


def word_end_col(runes: list[str], start: int) -> int:
    """对应 Go ``wordEndCol``。"""
    end = start
    while end + 1 < len(runes) and is_copy_word_rune(runes[end + 1]):
        end += 1
    return end


def next_word_start(lines: list[str], cursor: CopyCursor) -> tuple[CopyCursor, bool]:
    """对应 Go ``nextWordStart``。"""
    for line in range(cursor.line, len(lines)):
        runes = list(lines[line])
        start = min(cursor.col + 1, len(runes)) if line == cursor.line else 0
        for col in range(start, len(runes)):
            if is_copy_word_rune(runes[col]) and (col == 0 or not is_copy_word_rune(runes[col - 1])):
                return CopyCursor(line=line, col=col), True
    return CopyCursor(), False


def next_word_end(lines: list[str], cursor: CopyCursor) -> tuple[CopyCursor, bool]:
    """对应 Go ``nextWordEnd``。"""
    for line in range(cursor.line, len(lines)):
        runes = list(lines[line])
        start = clamp_int(cursor.col, 0, len(runes)) if line == cursor.line else 0
        for col in range(start, len(runes)):
            if not is_copy_word_rune(runes[col]):
                continue
            if col > cursor.col or line != cursor.line or col == 0 or not is_copy_word_rune(runes[col - 1]):
                return CopyCursor(line=line, col=word_end_col(runes, col)), True
    return CopyCursor(), False


def previous_word_start(lines: list[str], cursor: CopyCursor) -> tuple[CopyCursor, bool]:
    """对应 Go ``previousWordStart``。"""
    if not lines:
        return CopyCursor(), False
    for line in range(cursor.line, -1, -1):
        runes = list(lines[line])
        start = len(runes) - 1
        if line == cursor.line:
            start = min(cursor.col - 1, len(runes) - 1)
        for col in range(start, -1, -1):
            if is_copy_word_rune(runes[col]) and (col == 0 or not is_copy_word_rune(runes[col - 1])):
                return CopyCursor(line=line, col=col), True
    return CopyCursor(), False


def clamp_cursor(cursor: CopyCursor, lines: list[str]) -> CopyCursor:
    """对应 Go ``clampCursor``。"""
    if not lines:
        return CopyCursor()
    line = clamp_int(cursor.line, 0, len(lines) - 1)
    col = clamp_int(cursor.col, 0, max(0, rune_len(lines[line]) - 1))
    return CopyCursor(line=line, col=col)


def ordered_cursors(a: CopyCursor, b: CopyCursor) -> tuple[CopyCursor, CopyCursor]:
    """按 (行, 列) 排序。对应 Go ``orderedCursors``。"""
    if (a.line, a.col) <= (b.line, b.col):
        return a, b
    return b, a


def line_in_range(line: int, a: int, b: int) -> bool:
    """对应 Go ``lineInRange``。"""
    start, end = ordered_lines(a, b)
    return start <= line <= end


def line_in_visual_char_range(line: int, a: CopyCursor, b: CopyCursor) -> bool:
    """对应 Go ``lineInVisualCharRange``。"""
    start, end = ordered_cursors(a, b)
    return start.line <= line <= end.line


def selection_cols_for_line(line: int, a: CopyCursor, b: CopyCursor) -> tuple[int, int]:
    """对应 Go ``selectionColsForLine``（跨行时用 ``1 << 30`` 表示行尾）。"""
    start, end = ordered_cursors(a, b)
    if line == start.line and line == end.line:
        return start.col, end.col
    if line == start.line:
        return start.col, 1 << 30
    if line == end.line:
        return 0, end.col
    return 0, 1 << 30


def slice_runes_inclusive(text: str, start: int, end: int) -> str:
    """对应 Go ``sliceRunesInclusive``。"""
    if not text:
        return ""
    start = clamp_int(start, 0, len(text) - 1)
    end = clamp_int(end, start, len(text) - 1)
    return text[start : end + 1]


def slice_runes_from(text: str, start: int) -> str:
    """对应 Go ``sliceRunesFrom``。"""
    if not text:
        return ""
    return text[clamp_int(start, 0, len(text) - 1) :]


def slice_runes_to(text: str, end: int) -> str:
    """对应 Go ``sliceRunesTo``。"""
    if not text:
        return ""
    return text[: clamp_int(end, 0, len(text) - 1) + 1]


def search_match_ranges(line: str, query: str) -> list[CopyRange]:
    """找出一行里全部（大小写不敏感）命中区间。对应 Go ``searchMatchRanges``。"""
    if not query or not line:
        return []
    lower_line = line.lower()
    lower_query = query.lower()
    query_len = len(query)
    if query_len == 0:
        return []
    matches: list[CopyRange] = []
    offset = 0
    while offset <= len(lower_line):
        index = lower_line.find(lower_query, offset)
        if index < 0:
            break
        start = index
        matches.append(CopyRange(start=start, end=start + query_len - 1))
        offset = index + len(lower_query)
    return matches


def render_cursor(line: str, col: int) -> str:
    """给光标处字符加反显。对应 Go ``renderCursor``。"""
    if not line:
        return theme.TUI_COPY_CURSOR_STYLE.render(" ")
    col = clamp_int(col, 0, len(line) - 1)
    return line[:col] + theme.TUI_COPY_CURSOR_STYLE.render(line[col]) + line[col + 1 :]


def render_char_selection(line: str, start: int, end: int) -> str:
    """给字符选区加底色。对应 Go ``renderCharSelection``。"""
    if not line:
        return theme.TUI_COPY_SELECTION_STYLE.render(" ")
    start = clamp_int(start, 0, len(line) - 1)
    end = clamp_int(end, start, len(line) - 1)
    return line[:start] + theme.TUI_COPY_SELECTION_STYLE.render(line[start : end + 1]) + line[end + 1 :]


def render_search_matches(
    line: str, matches: list[CopyRange], current: int, cursor_col: int, has_cursor: bool
) -> str:
    """渲染搜索命中（当前命中用另一种底色，光标处字符反显）。对应 Go ``renderSearchMatches``。"""
    if not line or not matches:
        return line
    if has_cursor:
        cursor_col = clamp_int(cursor_col, 0, len(line) - 1)

    def write_span(start: int, end: int, style: object, styled: bool) -> str:
        if end <= start:
            return ""
        if not has_cursor or cursor_col < start or cursor_col >= end:
            text = line[start:end]
            return style.render(text) if styled else text
        parts = []
        if start < cursor_col:
            text = line[start:cursor_col]
            parts.append(style.render(text) if styled else text)
        parts.append(theme.TUI_COPY_CURSOR_STYLE.render(line[cursor_col]))
        if cursor_col + 1 < end:
            text = line[cursor_col + 1 : end]
            parts.append(style.render(text) if styled else text)
        return "".join(parts)

    out: list[str] = []
    pos = 0
    for index, match in enumerate(matches):
        start = clamp_int(match.start, pos, len(line))
        end = clamp_int(match.end, start - 1, len(line) - 1)
        if start > pos:
            out.append(write_span(pos, start, theme.TUI_COPY_SEARCH_STYLE, False))
        if end >= start:
            style = (
                theme.TUI_COPY_SEARCH_CURRENT_STYLE if index == current else theme.TUI_COPY_SEARCH_STYLE
            )
            out.append(write_span(start, end + 1, style, True))
            pos = end + 1
    if pos < len(line):
        out.append(write_span(pos, len(line), theme.TUI_COPY_SEARCH_STYLE, False))
    return "".join(out)


def ensure_viewport_line_visible(viewport: object, line: int) -> None:
    """滚动视口让某行可见。对应 Go ``ensureViewportLineVisible``。"""
    height = getattr(viewport, "height", 0)
    if viewport is None or height <= 0:
        return
    offset = getattr(viewport, "y_offset", 0)
    if line < offset:
        viewport.set_y_offset(max(0, line))
        return
    bottom = offset + max(1, height) - 1
    if line > bottom:
        viewport.set_y_offset(max(0, line - max(1, height) + 1))


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class CopyModeMixin:
    """把 ``tui_copy.go`` 的方法挂到 :class:`TuiModel` 上。

    依赖宿主的字段（与 Go 的 ``tuiModel`` 同名）：``copy_state``、``viewport``、
    ``notice_viewport``、``clipboard``、``notices``、``input``。
    依赖宿主的方法：``wrapped_content()``、``layout_notice_visible()``、
    ``refresh_content()``、``refresh_notices()``、``copy_lines(region)``。
    """

    # -- 入口 ---------------------------------------------------------------
    def update_copy_mode(self, msg: KeyMsg) -> None:
        """copy mode 下的按键分派。对应 Go ``(*tuiModel).updateCopyMode``。"""
        if self.copy_state.mode == MODE_COPY_SEARCH:
            self.update_copy_search(msg)
            return
        name = msg.string()
        if name == "alt+h":
            if self.copy_state.mode == MODE_COPY_NORMAL:
                self.enter_copy_mode(REGION_CHAT)
            return
        if name == "alt+l":
            if self.copy_state.mode == MODE_COPY_NORMAL:
                if self.layout_notice_visible():
                    self.enter_copy_mode(REGION_NOTICE)
                else:
                    self.copy_state.status = "notices hidden"
            return
        if name == KEY_ESC:
            if self.copy_state.visual():
                self.cancel_copy_submode()
                return
            self.exit_copy_mode()
            return
        if name == "i":
            self.exit_copy_mode()
            return
        if name in ("h", KEY_LEFT):
            self.move_copy_cursor(-1, 0)
            return
        if name in ("l", KEY_RIGHT):
            self.move_copy_cursor(1, 0)
            return
        if name in ("j", KEY_DOWN):
            self.move_copy_cursor(0, 1)
            return
        if name in ("k", KEY_UP):
            self.move_copy_cursor(0, -1)
            return
        if name == "w":
            self.move_copy_cursor_word_forward()
            return
        if name == "e":
            self.move_copy_cursor_word_end()
            return
        if name == "b":
            self.move_copy_cursor_word_backward()
            return
        if name in (KEY_PGDOWN, KEY_CTRL_D):
            self.page_copy(1)
            return
        if name in (KEY_PGUP, KEY_CTRL_U):
            self.page_copy(-1)
            return
        if name in (KEY_HOME, "g"):
            self.copy_state.cursor = CopyCursor()
            self.ensure_copy_cursor_visible()
            self.refresh_copy_region()
            return
        if name in (KEY_END, "G"):
            lines = self.copy_lines(self.copy_state.region)
            if lines:
                self.copy_state.cursor = CopyCursor(line=len(lines) - 1)
                self.ensure_copy_cursor_visible()
                self.refresh_copy_region()
            return
        if name == "v":
            self.start_visual(MODE_COPY_VISUAL_CHAR)
            return
        if name == "V":
            self.start_visual(MODE_COPY_VISUAL_LINE)
            return
        if name == "y":
            self.yank_copy_selection()
            return
        if name == "/":
            self.begin_copy_search()
            return
        if name == "n":
            if not self.jump_copy_search(1):
                self.copy_state.status = "not found"
            return
        if name == "N":
            if not self.jump_copy_search(-1):
                self.copy_state.status = "not found"

    def update_copy_search(self, msg: KeyMsg) -> None:
        """搜索子模式的按键处理。对应 Go ``(*tuiModel).updateCopySearch``。"""
        if msg.alt:
            return
        name = msg.string()
        if name == KEY_ESC:
            self.cancel_copy_submode()
            return
        if name == KEY_ENTER:
            self.commit_copy_search()
            return
        if name in (KEY_BACKSPACE, KEY_CTRL_H):
            self.copy_state.search_input = self.copy_state.search_input[:-1]
            return
        if msg.type == KEY_RUNES:
            self.copy_state.search_input += msg.runes

    # -- 状态文本 -----------------------------------------------------------
    def copy_status_text(self) -> str:
        """状态栏里 copy mode 的提示。对应 Go ``copyStatusText``。"""
        if self.copy_state.mode == MODE_COPY_SEARCH:
            return (
                "SEARCH "
                + region_name(self.copy_state.region)
                + " /"
                + self.copy_state.search_input
                + " · Enter jump · Esc cancel"
            )
        status = (
            mode_name(self.copy_state.mode).upper()
            + " "
            + region_name(self.copy_state.region)
            + " · hjkl/w e b move · v/V select · y copy · / search · i input · Esc"
        )
        if self.copy_state.status:
            status += " · " + self.copy_state.status
        return status

    # -- 进出 ---------------------------------------------------------------
    def enter_copy_mode(self, region: str) -> None:
        """进入 copy mode。对应 Go ``enterCopyMode``。"""
        lines = self.copy_lines(region)
        if not lines:
            self.copy_state.status = "nothing to copy"
            return
        self.copy_state = CopyModeState(
            mode=MODE_COPY_NORMAL,
            region=region,
            cursor=CopyCursor(line=len(lines) - 1),
            status="copy " + region_name(region),
        )
        self.input.blur()
        self.refresh_copy_region()
        self.ensure_copy_cursor_visible()
        self.refresh_copy_region()

    def exit_copy_mode(self) -> None:
        """退出 copy mode。对应 Go ``exitCopyMode``。"""
        region = self.copy_state.region
        self.copy_state = CopyModeState()
        self.input.focus()
        if region == REGION_CHAT:
            self.refresh_content()
        elif region == REGION_NOTICE:
            self.refresh_notices()

    # -- 内容行 -------------------------------------------------------------
    def copy_lines(self, region: str) -> list[str]:
        """取某个区域的可见文本行。对应 Go ``copyLines``。"""
        if region == REGION_CHAT:
            return split_lines(self.wrapped_content())
        if region == REGION_NOTICE:
            return self.notice_copy_lines()
        return []

    def notice_copy_lines(self) -> list[str]:
        """通知面板的可复制行（通知之间空一行）。对应 Go ``noticeCopyLines``。"""
        if not self.notices:
            return []
        width = max(1, self.notice_viewport.width - 4)
        lines: list[str] = []
        for index, notice in enumerate(self.notices):
            if index > 0:
                lines.append("")
            lines.extend(split_lines(theme_wrap(notice.text, width)))
        return lines

    # -- 光标移动 -----------------------------------------------------------
    def move_copy_cursor(self, dx: int, dy: int) -> None:
        """对应 Go ``moveCopyCursor``。"""
        lines = self.copy_lines(self.copy_state.region)
        if not lines:
            self.copy_state.cursor = CopyCursor()
            return
        cursor = replace(self.copy_state.cursor)
        cursor.line = clamp_int(cursor.line + dy, 0, len(lines) - 1)
        cursor.col = clamp_int(cursor.col + dx, 0, max(0, rune_len(lines[cursor.line]) - 1))
        self.copy_state.cursor = cursor
        self.ensure_copy_cursor_visible()
        self.refresh_copy_region()

    def move_copy_cursor_word_forward(self) -> None:
        """对应 Go ``moveCopyCursorWordForward``。"""
        lines = self.copy_lines(self.copy_state.region)
        cursor, ok = next_word_start(lines, clamp_cursor(self.copy_state.cursor, lines))
        if not ok:
            return
        self.copy_state.cursor = cursor
        self.ensure_copy_cursor_visible()
        self.refresh_copy_region()

    def move_copy_cursor_word_end(self) -> None:
        """对应 Go ``moveCopyCursorWordEnd``。"""
        lines = self.copy_lines(self.copy_state.region)
        cursor, ok = next_word_end(lines, clamp_cursor(self.copy_state.cursor, lines))
        if not ok:
            return
        self.copy_state.cursor = cursor
        self.ensure_copy_cursor_visible()
        self.refresh_copy_region()

    def move_copy_cursor_word_backward(self) -> None:
        """对应 Go ``moveCopyCursorWordBackward``。"""
        lines = self.copy_lines(self.copy_state.region)
        cursor, ok = previous_word_start(lines, clamp_cursor(self.copy_state.cursor, lines))
        if not ok:
            return
        self.copy_state.cursor = cursor
        self.ensure_copy_cursor_visible()
        self.refresh_copy_region()

    def ensure_copy_cursor_visible(self) -> None:
        """对应 Go ``ensureCopyCursorVisible``。"""
        if self.copy_state.region == REGION_CHAT:
            ensure_viewport_line_visible(self.viewport, self.copy_state.cursor.line)
        elif self.copy_state.region == REGION_NOTICE:
            ensure_viewport_line_visible(self.notice_viewport, self.copy_state.cursor.line)

    def page_copy(self, delta: int) -> None:
        """整页移动。对应 Go ``pageCopy``。"""
        height = self.copy_region_height()
        if height <= 0:
            height = 1
        self.move_copy_cursor(0, delta * height)

    def copy_region_height(self) -> int:
        """对应 Go ``copyRegionHeight``。"""
        if self.copy_state.region == REGION_CHAT:
            return max(1, self.viewport.height)
        if self.copy_state.region == REGION_NOTICE:
            return max(1, self.notice_viewport.height)
        return 1

    # -- 可视模式 -----------------------------------------------------------
    def start_visual(self, mode: str) -> None:
        """进入可视模式。对应 Go ``startVisual``。"""
        self.copy_state.anchor = replace(self.copy_state.cursor)
        self.copy_state.mode = mode
        self.copy_state.status = mode_name(mode)
        self.refresh_copy_region()

    def cancel_copy_submode(self) -> None:
        """取消可视/搜索子模式。对应 Go ``cancelCopySubmode``。"""
        self.copy_state.mode = MODE_COPY_NORMAL
        self.copy_state.anchor = CopyCursor()
        self.copy_state.search_input = ""
        self.copy_state.status = "copy " + region_name(self.copy_state.region)
        self.refresh_copy_region()

    def yank_copy_selection(self) -> None:
        """把选区写进剪贴板。对应 Go ``yankCopySelection``。"""
        text = self.selected_copy_text()
        if text == "":
            self.copy_state.status = "nothing copied"
            return
        try:
            self.clipboard.write_all(text)
        except Exception as exc:  # noqa: BLE001 - 与 Go 一致地把错误写进状态栏
            self.copy_state.status = "copy failed: " + str(exc)
            return
        line_count = text.count("\n") + 1
        self.copy_state.status = "copied " + plural(line_count, "line")
        self.copy_state.mode = MODE_COPY_NORMAL
        self.copy_state.anchor = CopyCursor()
        self.refresh_copy_region()

    def selected_copy_text(self) -> str:
        """当前选区文本（非可视模式为光标所在整行）。对应 Go ``selectedCopyText``。"""
        lines = self.copy_lines(self.copy_state.region)
        if not lines:
            return ""
        cursor = clamp_cursor(self.copy_state.cursor, lines)
        if not self.copy_state.visual():
            return lines[cursor.line]
        anchor = clamp_cursor(self.copy_state.anchor, lines)
        if self.copy_state.mode == MODE_COPY_VISUAL_LINE:
            start, end = ordered_lines(anchor.line, cursor.line)
            return "\n".join(lines[start : end + 1])
        start, end = ordered_cursors(anchor, cursor)
        if start.line == end.line:
            return slice_runes_inclusive(lines[start.line], start.col, end.col)
        parts = [slice_runes_from(lines[start.line], start.col)]
        for line in range(start.line + 1, end.line):
            parts.append(lines[line])
        parts.append(slice_runes_to(lines[end.line], end.col))
        return "\n".join(parts)

    # -- 搜索 ---------------------------------------------------------------
    def begin_copy_search(self) -> None:
        """对应 Go ``beginCopySearch``。"""
        self.copy_state.mode = MODE_COPY_SEARCH
        self.copy_state.search_input = ""
        self.copy_state.status = "search"

    def commit_copy_search(self) -> None:
        """对应 Go ``commitCopySearch``。"""
        query = self.copy_state.search_input.strip()
        self.copy_state.mode = MODE_COPY_NORMAL
        self.copy_state.search_input = ""
        if query == "":
            self.copy_state.status = "empty search"
            return
        self.copy_state.search_query = query
        if not self.jump_copy_search(1):
            self.copy_state.status = "not found: " + query

    def jump_copy_search(self, direction: int) -> bool:
        """循环查找下一个命中。对应 Go ``jumpCopySearch``。"""
        query = self.copy_state.search_query
        if query == "":
            return False
        lines = self.copy_lines(self.copy_state.region)
        if not lines:
            return False
        start = self.copy_state.cursor.line
        for step in range(1, len(lines) + 1):
            index = (start + direction * step + len(lines) * step) % len(lines)
            col = lines[index].lower().find(query.lower())
            if col >= 0:
                self.copy_state.cursor = CopyCursor(line=index, col=col)
                self.copy_state.status = "found: " + query
                self.ensure_copy_cursor_visible()
                self.refresh_copy_region()
                return True
        return False

    # -- 渲染 ---------------------------------------------------------------
    def render_copy_lines(self, region: str, lines: list[str]) -> str:
        """渲染某个区域（copy mode 下加光标/选区/命中高亮）。对应 Go ``renderCopyLines``。"""
        if not self.copy_state.active() or self.copy_state.region != region:
            return "\n".join(lines)
        return "\n".join(self.render_copy_line(index, line) for index, line in enumerate(lines))

    def render_copy_line(self, line_index: int, line: str) -> str:
        """对应 Go ``renderCopyLine``。"""
        if self.copy_state.mode == MODE_COPY_VISUAL_LINE and line_in_range(
            line_index, self.copy_state.anchor.line, self.copy_state.cursor.line
        ):
            return theme.TUI_COPY_SELECTION_STYLE.render(line)
        if self.copy_state.mode == MODE_COPY_VISUAL_CHAR and line_in_visual_char_range(
            line_index, self.copy_state.anchor, self.copy_state.cursor
        ):
            start, end = selection_cols_for_line(
                line_index, self.copy_state.anchor, self.copy_state.cursor
            )
            return render_char_selection(line, start, end)
        matches = search_match_ranges(line, self.copy_state.search_query)
        if matches:
            current = -1
            has_cursor = line_index == self.copy_state.cursor.line
            if has_cursor:
                for index, match in enumerate(matches):
                    if match.start == self.copy_state.cursor.col:
                        current = index
                        break
            return render_search_matches(
                line, matches, current, self.copy_state.cursor.col, has_cursor
            )
        if line_index == self.copy_state.cursor.line:
            return render_cursor(line, self.copy_state.cursor.col)
        return line


def theme_wrap(text: str, width: int) -> str:
    """按显示宽度换行（对应 Go ``wrapDisplayWidth``，实现在 ``style`` 模块）。"""
    return wrap_display_width(text, width)
