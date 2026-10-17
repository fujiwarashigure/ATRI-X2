"""画师串库的检索与打分（含"只要沾边就返回"的推荐排序）。

对应 Go：``internal/artist/search.go``。

调用方只传当前 viewer 能看到的条目；本模块**故意不判断权限**——可见性只有一处
决定，检索再过滤一遍就等于多了一个出错的地方。

* 空查询 = 列出全部（用户浏览自己的库就是这么用的）。
* :func:`search` 要求每个词都命中（加词 = 缩小范围）；
  :func:`rank` 允许丢词（推荐场景：风格描述很少把 preset 的每个标签都写全）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import normalize_tag
from .render import describe_style
from .types import Artist, Preset, PresetStatus, Scope

#: 命中类型。对应 Go ``MatchArtist`` / ``MatchPreset``。
MATCH_ARTIST = "artist"
MATCH_PRESET = "preset"

#: 检索条数上限。对应 Go ``DefaultSearchLimit`` / ``MaxSearchLimit``。
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50

#: 分词分隔符（与 Go ``strings.FieldsFunc`` 的字符集一致）。
_TERM_SEPARATORS = re.compile(
    r"[\s,\uff0c\u3001\u3002;\uff1b|/()\uff08\uff09\[\]{}:\uff1a\"']+"
)


@dataclass
class Match:
    """一条检索命中。对应 Go ``Match``。"""

    kind: str = ""
    id: str = ""
    title: str = ""
    detail: str = ""
    score: int = 0
    scope: Scope = Scope.GLOBAL
    status: PresetStatus = PresetStatus.APPROVED
    artists: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)


@dataclass
class _WeightedField:
    value: str
    weight: int


def search(
    artists: list[Artist], presets: list[Preset], query: str, limit: int
) -> list[Match]:
    """全词命中式检索。对应 Go ``Search``。"""
    return _match_library(artists, presets, query, limit, True)


def rank(
    artists: list[Artist], presets: list[Preset], query: str, limit: int
) -> list[Match]:
    """部分命中式排序（推荐用）。对应 Go ``Rank``。"""
    return _match_library(artists, presets, query, limit, False)


def _match_library(
    artists: list[Artist],
    presets: list[Preset],
    query: str,
    limit: int,
    require_all: bool,
) -> list[Match]:
    if limit <= 0:
        limit = DEFAULT_SEARCH_LIMIT
    if limit > MAX_SEARCH_LIMIT:
        limit = MAX_SEARCH_LIMIT
    terms = query_terms(query)
    tags: dict[str, str] = {}
    for item in artists or []:
        tags[item.id] = item.canonical_tag

    out: list[Match] = []
    for item in artists or []:
        score = _score_artist(terms, item, require_all)
        if score < 0:
            continue
        detail = (item.note or "").strip()
        if detail == "":
            detail = item.canonical_tag
        out.append(
            Match(
                kind=MATCH_ARTIST,
                id=item.id,
                title=item.canonical_tag,
                detail=detail,
                score=score,
                scope=Scope.GLOBAL,
                status=PresetStatus.APPROVED,
                style_tags=list(item.style_tags or []),
            )
        )
    for item in presets or []:
        score = _score_preset(terms, item, tags, require_all)
        if score < 0:
            continue
        names: list[str] = []
        for slot in item.artists or []:
            if slot.artist_id in tags:
                names.append(tags[slot.artist_id])
                continue
            names.append(slot.artist_id)
        out.append(
            Match(
                kind=MATCH_PRESET,
                id=item.id,
                title=item.name,
                detail=describe_style(item),
                score=score,
                scope=item.scope,
                status=item.status,
                artists=names,
                style_tags=list(item.positive_tags or []),
            )
        )
    out.sort(key=_match_sort_key)
    if len(out) > limit:
        out = out[:limit]
    return out


def _match_sort_key(item: Match) -> tuple[int, int, str, str]:
    """稳定排序键：分数降序 → 画师优先 → 标题升序 → id 升序。"""
    return (-item.score, 0 if item.kind == MATCH_ARTIST else 1, item.title, item.id)


def query_terms(query: str) -> list[str]:
    """查询分词：小写、去重、保序。对应 Go ``queryTerms``。"""
    fields = _TERM_SEPARATORS.split((query or "").lower())
    out: list[str] = []
    seen: set[str] = set()
    for field_value in fields:
        text = field_value.strip()
        if text == "" or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def field_score(term: str, value: str) -> int:
    """单个词对单个字段的得分。对应 Go ``fieldScore``。"""
    field_value = normalize_tag(value)
    if field_value == "":
        return 0
    if field_value == term:
        return 100
    if field_value.startswith(term):
        return 70
    if term in field_value:
        return 40
    return 0


def _best_score(term: str, fields: list[_WeightedField]) -> int:
    best = 0
    for item in fields:
        score = field_score(term, item.value)
        if score == 0:
            continue
        score = score * item.weight // 100
        if score > best:
            best = score
    return best


def _total_score(
    terms: list[str], fields: list[_WeightedField], require_all: bool
) -> int:
    if not terms:
        return 1
    total = 0
    matched = 0
    for term in terms:
        score = _best_score(term, fields)
        if score == 0:
            if require_all:
                return -1
            continue
        matched += 1
        total += score
    if not require_all and matched == 0:
        return -1
    return total


def _score_artist(terms: list[str], item: Artist, require_all: bool) -> int:
    fields = [
        _WeightedField(item.canonical_tag, 100),
        _WeightedField(item.id, 100),
        _WeightedField(" ".join(item.aliases or []), 90),
    ]
    for alias in item.aliases or []:
        fields.append(_WeightedField(alias, 100))
    for tag in item.style_tags or []:
        fields.append(_WeightedField(tag, 60))
    if item.note:
        fields.append(_WeightedField(item.note, 30))
    return _total_score(terms, fields, require_all)


def _score_preset(
    terms: list[str], item: Preset, tags: dict[str, str], require_all: bool
) -> int:
    source_note = item.source.note if item.source is not None else ""
    fields = [
        _WeightedField(item.name, 100),
        _WeightedField(item.id, 100),
        _WeightedField(item.raw_string, 80),
        _WeightedField(item.style_description, 60),
        _WeightedField(source_note, 30),
    ]
    for model in item.compatible_models or []:
        fields.append(_WeightedField(model, 80))
    for tag in item.positive_tags or []:
        fields.append(_WeightedField(tag, 60))
    for tag in item.negative_tags or []:
        fields.append(_WeightedField(tag, 40))
    for slot in item.artists or []:
        if slot.artist_id in tags:
            fields.append(_WeightedField(tags[slot.artist_id], 90))
        fields.append(_WeightedField(slot.artist_id, 80))
    return _total_score(terms, fields, require_all)


__all__ = [name for name in dir() if not name.startswith("_")]
