"""TUI 的 ANSI 样式与布局助手：``lipgloss`` 的最小等价物。

对应 Go：``tui.go`` / ``tui_copy.go`` 里 ``lipgloss`` 的用法。

只实现本 TUI 真正用到的那一小组能力 —— 前景/背景 256 色、粗体、反显、
最小宽度补齐、左内边距、左边框、圆角边框、水平拼接、垂直居中、可见宽度、
以及去 ANSI 的测量/换行。颜色编号**逐字**沿用 Go 里的
``lipgloss.Color("NN")``（即 ANSI 256 色号），保证换实现后观感不变。

``AdaptiveColor``（``tuiInputSeparatorStyle``）取 Go 里的 Dark 值，因为 TUI 默认跑在
深色终端上；这是刻意的选择，不是遗漏。

块级函数（``join_horizontal`` / ``place_vertical``）与 ``Style`` 一样对**多行文本**
逐行处理，与 lipgloss 的块语义一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Sequence

from atri_x.utils.runes import rune_width, string_width

#: SGR 复位。
RESET = "\x1b[0m"

_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

__all__ = [
    "RESET",
    "Style",
    "fit_width",
    "join_horizontal",
    "place_vertical",
    "strip_ansi",
    "style_width",
    "visible_width",
]


def strip_ansi(text: str) -> str:
    """去掉全部 SGR 序列（测试与宽度测量用）。"""
    return _ANSI_PATTERN.sub("", text)


def visible_width(text: str) -> int:
    """去掉 ANSI 后的显示宽度（宽字符按 2 列计）。"""
    return string_width(strip_ansi(text))


def style_width(text: str) -> int:
    """对应 ``lipgloss.Width``。"""
    return visible_width(text)


def fit_width(line: str, width: int) -> str:
    """把一行补空格到 ``width`` 显示列（已超宽则原样返回）。"""
    if width <= 0:
        return line
    current = visible_width(line)
    if current >= width:
        return line
    return line + " " * (width - current)


@dataclass(frozen=True)
class Style:
    """一段渲染样式。对应 ``lipgloss.Style`` 的子集。"""

    fg: int | None = None
    bg: int | None = None
    bold: bool = False
    reverse: bool = False
    width: int = 0
    padding_left: int = 0
    border: str = ""  # "" / "left" / "rounded"

    def with_(self, **changes: object) -> "Style":
        """返回改了若干属性的新样式（lipgloss 的 ``Style.X(...)`` 是值语义）。"""
        return replace(self, **changes)

    def bold_(self, value: bool = True) -> "Style":
        return self.with_(bold=value)

    def width_(self, value: int) -> "Style":
        return self.with_(width=value)

    # -- 渲染 ---------------------------------------------------------------
    def render(self, text: str) -> str:
        """按样式渲染一段（可多行）文本。"""
        lines = (text if text != "" else "").split("\n")
        rendered = [self._render_line(line) for line in lines]
        if self.border == "rounded":
            return _box(rendered, self.width or (max((visible_width(x) for x in rendered), default=0) + 2))
        return "\n".join(rendered)

    def _render_line(self, line: str) -> str:
        if self.padding_left:
            line = " " * self.padding_left + line
        if self.border == "left":
            line = "│" + line
        if self.width:
            line = fit_width(line, self.width)
        codes = self.sgr()
        if not codes:
            return line
        return codes + line + RESET

    def sgr(self) -> str:
        """本样式对应的 SGR 序列（无样式时返回空串）。"""
        codes: list[str] = []
        if self.bold:
            codes.append("1")
        if self.reverse:
            codes.append("7")
        if self.fg is not None:
            codes.append(f"38;5;{self.fg}")
        if self.bg is not None:
            codes.append(f"48;5;{self.bg}")
        return "\x1b[" + ";".join(codes) + "m" if codes else ""


def _box(lines: Sequence[str], width: int) -> str:
    """圆角边框盒子（``tuiCompletionStyle`` 用）。"""
    inner = max(1, width - 2)
    top = "╭" + "─" * inner + "╮"
    bottom = "╰" + "─" * inner + "╯"
    body = ["│" + fit_width(line, inner) + "│" for line in lines]
    return "\n".join([top, *body, bottom])


def join_horizontal(position: str, *blocks: str) -> str:
    """把若干块按行水平拼接（``lipgloss.JoinHorizontal``）。

    ``position`` 目前只支持 ``"top"``（本 TUI 只用它）：每一行依次取各块的第
    ``i`` 行，缺行按空串补齐。
    """
    split = [block.split("\n") for block in blocks]
    height = max((len(lines) for lines in split), default=0)
    out: list[str] = []
    for index in range(height):
        parts = [lines[index] if index < len(lines) else "" for lines in split]
        out.append("".join(parts))
    return "\n".join(out)


def place_vertical(height: int, position: str, block: str) -> str:
    """把块在 ``height`` 行内垂直居中/靠上（``lipgloss.PlaceVertical``）。"""
    if height <= 0:
        return block
    lines = block.split("\n") if block != "" else [""]
    if len(lines) >= height:
        return block
    pad = height - len(lines)
    if position == "center":
        top = pad // 2
        bottom = pad - top
    elif position == "bottom":
        top, bottom = pad, 0
    else:
        top, bottom = 0, pad
    return "\n".join([""] * top + lines + [""] * bottom)


def truncate_left(text: str, width: int) -> str:
    """保留右端并在左侧加省略号（对应 Go ``truncateLeftDisplayWidth``）。"""
    if width <= 0:
        return ""
    if string_width(text) <= width:
        return text
    if width == 1:
        return "…"
    used = 0
    start = len(text)
    for index in range(len(text) - 1, -1, -1):
        char_width = rune_width(text[index])
        if used + char_width > width - 1:
            break
        used += char_width
        start = index
    return "…" + text[start:]


def wrap_display_width(text: str, width: int) -> str:
    """按显示宽度硬换行（对应 Go ``wrapDisplayWidth``）。"""
    if width <= 0:
        return text
    out: list[str] = []
    line_width = 0
    for char in text:
        if char == "\n":
            out.append("\n")
            line_width = 0
            continue
        char_width = rune_width(char)
        if line_width > 0 and line_width + char_width > width:
            out.append("\n")
            line_width = 0
        out.append(char)
        line_width += char_width
    return "".join(out)
