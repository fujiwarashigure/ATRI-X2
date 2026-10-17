"""群知识库（确定性 FAQ 匹配）。对应 Go：``internal/groupkb/groupkb.go`` 的包声明。

实现见 :mod:`atri_x.groupkb.groupkb`；本模块只做再导出，使
``from atri_x.groupkb import match, MatchExact`` 与
``from atri_x.groupkb.groupkb import match`` 两种写法都可用。
"""

from __future__ import annotations

from atri_x.groupkb.groupkb import (
    Match,
    MatchContains,
    MatchExact,
    MatchKeywords,
    Normalize,
    NormalizeMatchMode,
    PUNCTUATION,
    contains_any,
    equals_any,
    is_sentence_punctuation,
    limit_runes,
    match,
    match_all_keywords,
    normalize,
    normalize_match_mode,
)

__all__ = [
    "Match",
    "MatchContains",
    "MatchExact",
    "MatchKeywords",
    "Normalize",
    "NormalizeMatchMode",
    "PUNCTUATION",
    "contains_any",
    "equals_any",
    "is_sentence_punctuation",
    "limit_runes",
    "match",
    "match_all_keywords",
    "normalize",
    "normalize_match_mode",
]
