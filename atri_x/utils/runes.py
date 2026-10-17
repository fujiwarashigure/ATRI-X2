"""rune（码点）语义与终端显示宽度。

对应 Go：``mattn/go-runewidth``（Go 的 ``[]rune`` 语义本身由语言提供，
Python 侧连同宽度计算一起实现）。

上游 Go 直接用 ``github.com/mattn/go-runewidth`` 做 TUI 布局；Python 标准库
没有等价物，本模块用 ``unicodedata`` 的 East Asian Width / General Category
属性表复刻同样的判定：

- 控制字符、``Soft Hyphen``、组合记号与格式字符宽度 0；
- East Asian ``W``/``F``（含 CJK、全角标点、emoji 主要区块）宽度 2；
- 其余宽度 1。

``Condition`` 对应 go-runewidth 的 ``Condition``（``EastAsianWidth`` /
``Strict`` / ``AmbiguousWidth``）。此处没有第三方依赖，也没有查表生成代码。
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "Condition",
    "fill_left",
    "fill_right",
    "is_ambiguous",
    "is_wide",
    "rune_count",
    "rune_slice",
    "rune_width",
    "string_width",
    "truncate",
    "truncate_left",
    "truncate_runes",
    "wrap",
]

_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cc", "Cf", "Cs"})


class Condition:
    """宽度判定条件（对应 go-runewidth 的 ``Condition``）。"""

    __slots__ = ("east_asian_width", "strict", "ambiguous_width")

    def __init__(
        self,
        east_asian_width: bool = False,
        strict: bool = False,
        ambiguous_width: int = 1,
    ) -> None:
        self.east_asian_width = east_asian_width
        self.strict = strict
        self.ambiguous_width = ambiguous_width

    # -- 单个 rune ----------------------------------------------------------

    def rune_width(self, value: str | int) -> int:
        """返回一个 rune 的显示宽度（``0``/``1``/``2``）。"""
        if isinstance(value, int):
            if value < 0 or value > 0x10FFFF:
                return 0
            ch = chr(value)
        else:
            if not value:
                return 0
            ch = value[0]
        code = ord(ch)
        if code < 0x20:
            return 0
        if 0x7F <= code <= 0x9F or code == 0xAD:  # nonprint
            return 0
        if code < 0x300:
            return 1
        category = unicodedata.category(ch)
        if category in _ZERO_WIDTH_CATEGORIES or unicodedata.combining(ch) != 0:
            return 0
        width = unicodedata.east_asian_width(ch)
        if width in ("W", "F"):
            return 2
        if width == "A" and self.east_asian_width:
            return 2 if self.strict else self.ambiguous_width
        return 1

    # -- 字符串 -------------------------------------------------------------

    def string_width(self, value: str) -> int:
        """返回字符串的显示宽度。"""
        return sum(self.rune_width(ch) for ch in value)

    def truncate(self, value: str, width: int, tail: str = "") -> str:
        """截断到 ``width`` 列，超出时追加 ``tail``。"""
        if self.string_width(value) <= width:
            return value
        tail_width = self.string_width(tail)
        limit = width - tail_width
        used = 0
        index = 0
        while index < len(value):
            rune_w = self.rune_width(value[index])
            if used + rune_w > limit:
                break
            used += rune_w
            index += 1
        return value[:index] + tail

    def truncate_left(self, value: str, width: int, prefix: str = "") -> str:
        """从左侧截断，保留最右 ``width`` 列（不足时原样返回）。"""
        if self.string_width(value) <= width:
            return value
        prefix_width = self.string_width(prefix)
        limit = width - prefix_width
        used = 0
        index = len(value)
        while index > 0:
            rune_w = self.rune_width(value[index - 1])
            if used + rune_w > limit:
                break
            used += rune_w
            index -= 1
        return prefix + value[index:]

    def wrap(self, value: str, width: int) -> str:
        """按显示宽度硬换行（``\\n`` 重置列计数）。"""
        if width <= 0:
            return value
        out: list[str] = []
        used = 0
        for ch in value:
            rune_w = self.rune_width(ch)
            if ch == "\n":
                used = 0
            if used + rune_w > width:
                out.append("\n")
                used = 0
            out.append(ch)
            used += rune_w
        return "".join(out)

    def fill_left(self, value: str, width: int) -> str:
        """左侧补空格到 ``width`` 列。"""
        padding = width - self.string_width(value)
        return " " * padding + value if padding > 0 else value

    def fill_right(self, value: str, width: int) -> str:
        """右侧补空格到 ``width`` 列。"""
        padding = width - self.string_width(value)
        return value + " " * padding if padding > 0 else value


# 默认条件：与 go-runewidth 的包级函数一致（非 East Asian、非 Strict）。
_DEFAULT = Condition()


def rune_width(value: str | int) -> int:
    """返回一个 rune 的显示宽度。"""
    return _DEFAULT.rune_width(value)


def string_width(value: str) -> int:
    """返回字符串的显示宽度。"""
    return _DEFAULT.string_width(value)


def truncate(value: str, width: int, tail: str = "") -> str:
    """截断到 ``width`` 列。"""
    return _DEFAULT.truncate(value, width, tail)


def truncate_left(value: str, width: int, prefix: str = "") -> str:
    """从左侧截断。"""
    return _DEFAULT.truncate_left(value, width, prefix)


def wrap(value: str, width: int) -> str:
    """按显示宽度硬换行。"""
    return _DEFAULT.wrap(value, width)


def fill_left(value: str, width: int) -> str:
    """左侧补空格。"""
    return _DEFAULT.fill_left(value, width)


def fill_right(value: str, width: int) -> str:
    """右侧补空格。"""
    return _DEFAULT.fill_right(value, width)


def is_wide(value: str) -> bool:
    """是否 East Asian ``W``/``F``（宽度 2）。"""
    if not value:
        return False
    return rune_width(value) == 2


def is_ambiguous(value: str) -> bool:
    """是否 East Asian ``A``（歧义宽度）。"""
    if not value:
        return False
    return unicodedata.east_asian_width(value[0]) == "A"


# -- Go 的 []rune 语义 -------------------------------------------------------

def rune_count(value: str) -> int:
    """等价于 Go ``utf8.RuneCountInString`` / ``len([]rune(s))``。"""
    return len(value)


def rune_slice(value: str, start: int, end: int | None = None) -> str:
    """等价于 Go ``[]rune(s)[start:end]``（负数按 Go 的越界规则视为非法）。"""
    if start < 0:
        raise ValueError("slice bounds out of range")
    if end is None:
        return value[start:]
    if end < start:
        raise ValueError("slice bounds out of range")
    return value[start:end]


def truncate_runes(value: str, max_runes: int) -> str:
    """按 rune 截断（不计算显示宽度）。"""
    if max_runes <= 0:
        return ""
    return value[:max_runes]
