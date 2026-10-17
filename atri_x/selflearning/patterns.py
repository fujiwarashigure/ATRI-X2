"""候选词抽取与判定 helper。

对应 Go：``internal/selflearning/store.go`` 的 ``stopCandidates`` /
``isStopCandidate`` / ``isWordRune`` / ``isASCIIWord`` / ``extractCandidates`` /
``containsCJK``。

抽取规则（与 Go 逐条一致）：

* CJK n-gram 只在**连续的字母/数字段内部**跑，长度 2–4，且至少含一个 Han 字符；
* ASCII 单词（长度 ≥ 3、全部 ASCII 字母/数字且至少一个字母）**每条消息只抽一次**；
* 同一条消息里同一个 token 只计一次（``Mine`` 侧另有去重，这里先做本地去重）。

Han 判定用本地范围表：Go 的 ``unicode.Han`` 在 Python 标准库里没有等价物，本仓
:mod:`atri_x.textmatch` 有一份同源的 ``_HAN_RANGES``。这里刻意**不复用**它的私有名
（并行开发期跨模块依赖私有常量会被别人的重构打断）；两张表都以 Go 的
``unicode.Han`` 为准，集成期若要收敛，把本表删掉改 import 即可。
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "STOP_CANDIDATES",
    "contains_cjk",
    "extract_candidates",
    "is_ascii_word",
    "is_han",
    "is_stop_candidate",
    "is_word_rune",
]

#: 明显无信息量的高频词，永不成为候选。对应 Go ``stopCandidates``。
STOP_CANDIDATES = frozenset(
    {
        "这个",
        "那个",
        "什么",
        "怎么",
        "不是",
        "可以",
        "没有",
        "就是",
        "因为",
        "所以",
        "但是",
        "如果",
        "现在",
        "今天",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "you",
    }
)

#: Unicode Han 区块（对齐 Go 的 ``unicode.Han`` 表，与 ``atri_x.textmatch`` 同源）。
_HAN_RANGES: tuple[tuple[int, int], ...] = (
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


def is_han(ch: str) -> bool:
    """该字符是否属于 Han 区块（Go ``unicode.Is(unicode.Han, r)``）。"""
    code = ord(ch)
    for low, high in _HAN_RANGES:
        if low <= code <= high:
            return True
    return False


def is_word_rune(ch: str) -> bool:
    """字母或数字。对应 Go ``isWordRune``（``unicode.IsLetter`` / ``unicode.IsDigit``）。"""
    category = unicodedata.category(ch)
    return category.startswith("L") or category == "Nd"


def is_ascii_word(token: str) -> bool:
    """纯 ASCII 的字母/数字且至少含一个字母。对应 Go ``isASCIIWord``。"""
    if not token:
        return False
    has_letter = False
    for ch in token:
        if ord(ch) > 0x7F or not is_word_rune(ch):
            return False
        if unicodedata.category(ch).startswith("L"):
            has_letter = True
    return has_letter


def contains_cjk(value: str) -> bool:
    """是否含 Han 字符。对应 Go ``containsCJK``。"""
    return any(is_han(ch) for ch in value)


def is_stop_candidate(token: str) -> bool:
    """是否是停用候选词（小写后比较）。对应 Go ``isStopCandidate``。"""
    return token.lower() in STOP_CANDIDATES


def extract_candidates(text: str) -> list[str]:
    """从一条观察文本里抽出候选 token。对应 Go ``extractCandidates``。

    返回顺序：先 CJK n-gram（按连续段、按下标、按 2→4 长度），再 ASCII 单词；
    同一 token 只出现一次。
    """
    text = (text or "").strip()
    if text == "":
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add(token: str) -> None:
        token = token.strip()
        if token == "" or token in seen:
            return
        seen.add(token)
        out.append(token)

    index = 0
    while index < len(text):
        if not is_word_rune(text[index]):
            index += 1
            continue
        end = index
        while end < len(text) and is_word_rune(text[end]):
            end += 1
        segment = text[index:end]
        for start in range(len(segment)):
            size = 2
            while size <= 4 and start + size <= len(segment):
                part = segment[start : start + size]
                size += 1
                if contains_cjk(part):
                    add(part)
        index = end

    for word in _fields_func(text):
        word = word.strip().lower()
        if len(word) >= 3 and is_ascii_word(word):
            add(word)
    return out


def _fields_func(value: str) -> list[str]:
    """等价 Go ``strings.FieldsFunc(value, !isWordRune)``。"""
    out: list[str] = []
    current: list[str] = []
    for ch in value:
        if is_word_rune(ch):
            current.append(ch)
            continue
        if current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out
