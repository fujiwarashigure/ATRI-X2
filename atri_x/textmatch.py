"""确定性的关键词抽取与 LIKE 转义助手。

对应 Go：``internal/textmatch/textmatch.go``。

clean-room 记忆与学习检索路径共用这一份实现：CJK 连续段生成 2-4 rune 的
n-gram，ASCII 单词（两个及以上字母/数字）整体保留；输入与候选生成都有界，
CJK 与 ASCII 交错输出以免互相饿死，超长输入从头尾各取一半采样。
"""

from __future__ import annotations

import unicodedata

__all__ = ["Keywords", "LikePattern", "keywords", "like_pattern"]

MAX_KEYWORDS = 24
#: 真正被扫描的文本上限；更长的输入从头尾采样，避免句尾实体被丢掉。
MAX_INPUT_RUNES = 1200
#: CJK n-gram 与 ASCII 词的生成上限，用于约束对抗性查询的临时字符串与表项。
MAX_CANDIDATES = 256
MAX_ASCII_WORDS = 64
#: ASCII 关键词上限：两者同时出现时，英文不能挤掉 CJK 词。
MAX_ASCII_KEYWORDS = 8

# Unicode Han 区块（对应 Go 的 unicode.Han 表）。
_HAN_RANGES = (
    (0x2E80, 0x2EFF),  # CJK 部首补充
    (0x3005, 0x3005),
    (0x3007, 0x3007),
    (0x3021, 0x3029),
    (0x3038, 0x303B),
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 基本区
    (0xF900, 0xFAFF),  # 兼容表意文字
    (0x20000, 0x2A6DF),  # 扩展 B
    (0x2A700, 0x2EBEF),  # 扩展 C-F
    (0x2F800, 0x2FA1F),  # 兼容表意文字补充
    (0x30000, 0x323AF),  # 扩展 G-H
)


def _is_han(ch: str) -> bool:
    code = ord(ch)
    for low, high in _HAN_RANGES:
        if low <= code <= high:
            return True
    return False


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def _is_digit(ch: str) -> bool:
    # Go 的 unicode.IsDigit 只对 Nd 为真。
    return unicodedata.category(ch) == "Nd"


def _is_word_rune(ch: str) -> bool:
    return _is_letter(ch) or _is_digit(ch)


class _KeywordSet:
    """有界的去重关键词集合（对应 Go ``keywordSet``）。"""

    __slots__ = ("ascii", "ascii_generated", "cjk", "cjk_generated", "seen")

    def __init__(self) -> None:
        self.cjk: list[str] = []
        self.ascii: list[str] = []
        self.seen: dict[str, None] = {}
        self.cjk_generated = 0
        self.ascii_generated = 0

    def add(self, term: str) -> bool:
        term = (term or "").strip()
        if not term:
            return False
        key = term.lower()
        if key in self.seen:
            return False
        self.seen[key] = None
        return True

    def keywords(self) -> list[str]:
        """合并两个池：CJK 先出，ASCII 在 CJK 未耗尽前最多 8 个。"""
        if not self.cjk and not self.ascii:
            return []
        ascii_cap = MAX_KEYWORDS
        if self.cjk:
            ascii_cap = MAX_ASCII_KEYWORDS
        out: list[str] = []
        ci, ai = 0, 0
        while len(out) < MAX_KEYWORDS and (ci < len(self.cjk) or ai < len(self.ascii)):
            if ci < len(self.cjk):
                out.append(self.cjk[ci])
                ci += 1
                if len(out) == MAX_KEYWORDS:
                    break
            if ai < len(self.ascii):
                if ai < ascii_cap:
                    out.append(self.ascii[ai])
                    ai += 1
                elif ci >= len(self.cjk):
                    out.append(self.ascii[ai])
                    ai += 1
        return out


def keywords(value: str) -> list[str]:
    """从用户查询里抽取有界、去重的检索词。"""
    value = (value or "").strip()
    if not value:
        return []
    result = _KeywordSet()
    parts = _sample_input(value)
    for part in parts:
        _collect_cjk(part, result)
    for part in parts:
        _collect_ascii(part, result)
    out = result.keywords()
    if not out:
        # 单个 CJK 字符的查询（例如"猫"）仍然应该可检索；多字符查询刻意
        # 不产出单字词，因为噪声太大。
        for ch in value:
            if _is_han(ch):
                if result.add(ch):
                    out.append(ch)
                if len(out) >= MAX_KEYWORDS:
                    break
    return out


def _sample_input(value: str) -> list[str]:
    if len(value) <= MAX_INPUT_RUNES:
        return [value]
    half = MAX_INPUT_RUNES // 2
    return [value[:half], value[len(value) - half :]]


def _collect_cjk(value: str, result: _KeywordSet) -> None:
    runes = value
    index = 0
    while index < len(runes) and result.cjk_generated < MAX_CANDIDATES:
        if not _is_word_rune(runes[index]):
            index += 1
            continue
        end = index
        while end < len(runes) and _is_word_rune(runes[end]):
            end += 1
        segment = runes[index:end]
        start = 0
        while start < len(segment) and result.cjk_generated < MAX_CANDIDATES:
            size = 2
            while (
                size <= 4
                and start + size <= len(segment)
                and result.cjk_generated < MAX_CANDIDATES
            ):
                part = segment[start : start + size]
                size += 1
                if not _contains_cjk(part):
                    continue
                # 只有"新词"才计入预算：同一短语的失控重复不会在扫到消息
                # 末尾之前耗尽预算。输入长度已经有界，因此原始生成量也有界。
                if result.add(part):
                    result.cjk.append(part)
                    result.cjk_generated += 1
            start += 1
        index = end


def _collect_ascii(value: str, result: _KeywordSet) -> None:
    for word in _fields_func(value):
        if result.ascii_generated >= MAX_ASCII_WORDS:
            return
        if not _is_ascii_word(word) or len(word) < 2:
            continue
        lowered = word.lower()
        if result.add(lowered):
            result.ascii.append(lowered)
            result.ascii_generated += 1


def _fields_func(value: str) -> list[str]:
    """等价于 Go ``strings.FieldsFunc(value, !isWordRune)``。"""
    out: list[str] = []
    current: list[str] = []
    for ch in value:
        if _is_word_rune(ch):
            current.append(ch)
            continue
        if current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _is_ascii_word(value: str) -> bool:
    if not value:
        return False
    has_letter = False
    for ch in value:
        if ord(ch) > 0x7F or not (_is_letter(ch) or _is_digit(ch)):
            return False
        if _is_letter(ch):
            has_letter = True
    return has_letter


def _contains_cjk(value: str) -> bool:
    return any(_is_han(ch) for ch in value)


def like_pattern(term: str) -> str:
    """转义 LIKE 通配符并包成子串匹配；调用方必须写 ``LIKE ? ESCAPE '\\\\'``。"""
    term = (term or "").strip()
    if not term:
        return "%"
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
Keywords = keywords
LikePattern = like_pattern
