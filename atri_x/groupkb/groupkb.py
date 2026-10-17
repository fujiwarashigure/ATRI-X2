"""确定性 FAQ 匹配引擎（纯本地、无模型依赖）。

对应 Go：``internal/groupkb/groupkb.go``。

职责（与 Go 逐函数对齐）：

* :func:`normalize_match_mode` —— 把配置里的 ``match`` 归一到 exact / contains /
  keywords 三种模式，**无法识别一律回落到 exact**。
* :func:`normalize` —— 构造三种模式共用的确定性比较键：全角转半角 → 小写 →
  空白折叠为单个空格 → 去掉句读标点。
* :func:`match` —— 按 exact → contains → keywords 的顺序扫描，同一模式内保持
  存储顺序，命中即返回。

**全角折叠的等价物**：Go 用 ``golang.org/x/text/width`` 的 ``width.Narrow``，
覆盖面比本模块宽（半角/全角假名、部分 W 类字符等）。本模块只实现上游测试与
配置实际依赖的子集：``U+FF01–U+FF5E → U+0021–U+007E`` 与 ``U+3000 → 空格``
（见 :data:`_FULLWIDTH_FOLD`）。这是有意的收窄，不是遗漏。

**条目来源**：Go 用 ``config.GroupKnowledgeEntry``。Python 侧同一类型已存在于
:mod:`atri_x.config.state`（``GroupKnowledgeEntry``，字段与 Go 逐字对应），本模块
直接消费它；匹配逻辑只用鸭子类型（``is_enabled()`` / ``question`` / ``aliases`` /
``keywords`` / ``match``），因此任何字段同名的对象都能用。
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = [
    "MatchContains",
    "MatchExact",
    "MatchKeywords",
    "Normalize",
    "NormalizeMatchMode",
    "PUNCTUATION",
    "equals_any",
    "contains_any",
    "is_sentence_punctuation",
    "limit_runes",
    "match",
    "match_all_keywords",
    "normalize",
    "normalize_match_mode",
]

# --- 常量（Go 同名导出常量） -----------------------------------------------

#: 精确匹配（含别名）。对应 Go ``MatchExact``。
MatchExact = "exact"
#: 子串匹配（含别名）。对应 Go ``MatchContains``。
MatchContains = "contains"
#: 全部关键词都命中才算匹配。对应 Go ``MatchKeywords``。
MatchKeywords = "keywords"

#: ``NormalizeMatchMode`` 里 keywords 的别名集合（小写、去空白后比较）。
_KEYWORD_MODE_ALIASES = frozenset({"keywords", "keyword", "all_keywords", "all-keywords"})

#: 句读标点集合，逐字对齐 Go ``isSentencePunctuation``（30 个字符）。
PUNCTUATION = frozenset(
    ".,!?;:"
    "、，。！？；："
    "…—～"
    "()（）[]【】"
    "\"'“”‘’"
)

#: 全角 → 半角折叠表（``U+FF01–U+FF5E`` 整体平移，``U+3000`` 折叠为空格）。
_FULLWIDTH_FOLD = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_FOLD[0x3000] = 0x20


def normalize_match_mode(mode: str) -> str:
    """归一化匹配模式，未知值回落到 :data:`MatchExact`。

    对应 Go ``NormalizeMatchMode``：小写 + 去首尾空白后比较；``contains`` →
    contains；``keywords`` / ``keyword`` / ``all_keywords`` / ``all-keywords`` →
    keywords；**其余（含空串）一律 exact**。
    """
    normalized = (mode or "").strip().lower()
    if normalized == MatchContains:
        return MatchContains
    if normalized in _KEYWORD_MODE_ALIASES:
        return MatchKeywords
    return MatchExact


def _is_space(ch: str) -> bool:
    """等价 Go ``unicode.IsSpace``（White_Space 属性）。

    Python 的 ``str.isspace()`` 覆盖面更宽（含 U+001C..U+001F 等），这里显式
    列出以保证与 Go 一致。
    """
    if ch in " \t\n\v\f\r":
        return True
    code = ord(ch)
    if code < 0x80:
        return False
    return (
        ch in "\x85\xa0"
        or 0x1680 == code
        or 0x2000 <= code <= 0x200A
        or code in (0x2028, 0x2029, 0x202F, 0x205F, 0x3000)
    )


def is_sentence_punctuation(ch: str) -> bool:
    """该字符是否属于被剥除的句读标点。对应 Go ``isSentencePunctuation``。"""
    return ch in PUNCTUATION


def normalize(value: str) -> str:
    """构造确定性比较键。对应 Go ``Normalize``。

    顺序与 Go 完全一致：``trim`` → 全角转半角 → ``lower`` → 空白折叠为单个空格
    （**首个字符是空白时不写前导空格**）→ 去掉句读标点 → 末尾 ``trim``。
    """
    value = (value or "").strip()
    if value == "":
        return ""
    value = _narrow(value)
    value = value.lower()
    out: list[str] = []
    last_space = False
    for ch in value:
        if _is_space(ch):
            if out and not last_space:
                out.append(" ")
            last_space = True
            continue
        if is_sentence_punctuation(ch):
            continue
        out.append(ch)
        last_space = False
    return "".join(out).strip()


def _narrow(value: str) -> str:
    """全角 ASCII 变体与表意空格折叠为半角（``width.Narrow`` 的等价子集）。"""
    out: list[str] = []
    for ch in value:
        mapped = _FULLWIDTH_FOLD.get(ord(ch))
        if mapped is None:
            out.append(ch)
        else:
            out.append(chr(mapped))
    return "".join(out)


def limit_runes(value: str, max_runes: int) -> str:
    """``trim`` 后按 **rune** 截断；``max_runes <= 0`` 不截断。对应 Go ``limitRunes``。"""
    value = (value or "").strip()
    if value == "" or max_runes <= 0:
        return value
    if len(value) <= max_runes:
        return value
    return value[:max_runes]


def equals_any(normalized: str, primary: str, aliases: Sequence[str] | None) -> bool:
    """归一化后相等即命中（问题或任一别名）。对应 Go ``equalsAny``。"""
    key = normalize(primary)
    if key != "" and normalized == key:
        return True
    for alias in aliases or ():
        key = normalize(alias)
        if key != "" and normalized == key:
            return True
    return False


def contains_any(normalized: str, primary: str, aliases: Sequence[str] | None) -> bool:
    """归一化后是子串即命中（问题或任一别名）。对应 Go ``containsAny``。"""
    key = normalize(primary)
    if key != "" and key in normalized:
        return True
    for alias in aliases or ():
        key = normalize(alias)
        if key != "" and key in normalized:
            return True
    return False


def match_all_keywords(normalized: str, keywords: Sequence[str] | None) -> bool:
    """全部非空关键词都是子串，且至少有一个非空关键词。对应 Go ``matchAllKeywords``。

    全空（或全部归一化后为空）→ ``False``，即"没有关键词"不等于"匹配一切"。
    """
    found = False
    for keyword in keywords or ():
        key = normalize(keyword)
        if key == "":
            continue
        if key not in normalized:
            return False
        found = True
    return found


def match(
    entries: Sequence[Any], text: str, max_scan_runes: int
) -> tuple[Any | None, bool]:
    """返回优先级最高且启用中的命中条目。对应 Go ``Match``。

    扫描顺序固定为 exact → contains → keywords；每个模式内保持 ``entries`` 的
    存储顺序，命中即返回 ``(entry, True)``。未命中返回 ``(None, False)``。

    先按 rune 截断再归一化；归一化后为空直接未命中。``entry.is_enabled()`` 为假
    的条目跳过，且 ``normalize_match_mode(entry.match)`` 必须等于当前模式。
    """
    normalized = normalize(limit_runes(text, max_scan_runes))
    if normalized == "":
        return None, False
    for mode in (MatchExact, MatchContains, MatchKeywords):
        for entry in entries or ():
            if not _entry_enabled(entry):
                continue
            if normalize_match_mode(_entry_field(entry, "match", "")) != mode:
                continue
            question = _entry_field(entry, "question", "")
            aliases = _entry_field(entry, "aliases", ()) or ()
            if mode == MatchContains:
                if contains_any(normalized, question, aliases):
                    return entry, True
            elif mode == MatchKeywords:
                if match_all_keywords(normalized, _entry_field(entry, "keywords", ()) or ()):
                    return entry, True
            else:
                if equals_any(normalized, question, aliases):
                    return entry, True
    return None, False


def _entry_enabled(entry: Any) -> bool:
    """``entry.is_enabled()``；没有该方法时按 Go 的 ``nil`` 语义视为启用。"""
    checker = getattr(entry, "is_enabled", None)
    if callable(checker):
        return bool(checker())
    enabled = getattr(entry, "enabled", None)
    return enabled is None or bool(enabled)


def _entry_field(entry: Any, name: str, default: Any) -> Any:
    value = getattr(entry, name, default)
    return default if value is None else value


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
Normalize = normalize
NormalizeMatchMode = normalize_match_mode
Match = match
