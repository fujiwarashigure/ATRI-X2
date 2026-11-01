"""文本归一化：把"绕过写法"折叠回可匹配的骨架。

词表匹配是子串匹配，天然怕三类绕过：

1. **同形/全角**：``ｍｉｎｏｒ`` / 全角数字；
2. **不可见字符**：零宽空格、变体选择符、软连字符；
3. **间隔符**：``制 毒`` / ``制·毒`` / ``制_毒``。

做法：**只对"检测用的副本"归一化**，不改动原文。原因：敏感信息（手机号、身份证）
的正则替换必须落在原文上才不会错位；而词表命中是整段 block，不需要位置。
**不做什么**：不做拼音/繁简/同音归一（会把误杀率推高，留给远程语义审核）。
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "INVISIBLE_CHARS",
    "normalize_skeleton",
    "strip_invisible",
]

#: 需要直接删掉的不可见字符（零宽、变体选择符、软连字符、BOM）。
INVISIBLE_CHARS: frozenset[str] = frozenset(
    {
        "\u200b",  # zero width space
        "\u200c",  # zero width non-joiner
        "\u200d",  # zero width joiner
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\u2060",  # word joiner
        "\ufeff",  # BOM / zero width no-break space
        "\u00ad",  # soft hyphen
        "\u034f",  # combining grapheme joiner
        "\ufe00",  # variation selector-1
        "\ufe0f",  # variation selector-16
    }
)

#: 会被当作"词内分隔符"删掉的字符（只在两侧都是 CJK 时才删）。
_SEPARATORS: frozenset[str] = frozenset(
    " \t\u3000-_·•.。,，、|/\\~～+*#@!！?？:：;；'\"“”‘’()（）[]【】<>《》"
)


def _is_cjk(ch: str) -> bool:
    if len(ch) != 1:
        return False
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def strip_invisible(text: str) -> str:
    """删掉不可见字符（零宽 / 变体选择符 / 软连字符 / BOM）。"""
    if not text:
        return ""
    return "".join(ch for ch in text if ch not in INVISIBLE_CHARS)


def normalize_skeleton(text: str) -> str:
    """把文本折成"检测用骨架"：NFKC + 去不可见 + 去 CJK 之间的分隔符。

    返回的字符串**只用于匹配**，不要拿去替换原文。映射规则：

    * ``NFKC``：全角字母/数字/兼容字符 → 半角（``ｍ`` → ``m``）；
    * 去 :data:`INVISIBLE_CHARS`；
    * 两个 CJK 字符之间的 :data:`_SEPARATORS` 删除（``制 毒`` → ``制毒``）；
    * 大小写折叠（``BOMB`` → ``bomb``）。

    只删"CJK 之间"的分隔符，是为了不为难英文短语（``kill yourself`` 里的空格必须保留）。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    folded = strip_invisible(folded)
    if not folded:
        return ""
    chars: list[str] = []
    length = len(folded)
    for index, ch in enumerate(folded):
        if ch in _SEPARATORS:
            previous = folded[index - 1] if index > 0 else ""
            following = folded[index + 1] if index + 1 < length else ""
            if _is_cjk(previous) and _is_cjk(following):
                continue
        chars.append(ch)
    return "".join(chars).casefold()
