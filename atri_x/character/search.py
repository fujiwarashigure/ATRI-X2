"""角色库检索：内存索引上的轻量匹配。

对应 Go：``internal/character/search.go``。

``match_mode`` 为 ``"or"``（默认）或 ``"and"``；``tag`` 可选按标签过滤。命中
片段优先取正文（文档）里第一次出现查询词的位置，其次回落到 description。
"""

from __future__ import annotations

import re
from typing import Any

from atri_x.character.store import (
    DEFAULT_SEARCH_LIMIT,
    Character,
    SearchResult,
    Store,
    Viewer,
)

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_SEARCH_LIMIT",
    "SNIPPET_RUNES",
    "has_tag",
    "make_snippet",
    "match_terms",
    "search",
    "split_query_terms",
]

#: 检索条数上限。对应 Go ``MaxSearchLimit``。
MAX_SEARCH_LIMIT = 20
#: 片段窗口（rune）。对应 Go ``snippetRunes``。
SNIPPET_RUNES = 160

_QUERY_SPLIT = re.compile(r"[ \t\n\r,，|;；]+")


def has_tag(tags: list[str], want: str) -> bool:
    """标签是否（不分大小写）命中。对应 Go ``hasTag``。"""
    return any(str(tag or "").strip().lower() == want for tag in tags)


def split_query_terms(query: str) -> list[str]:
    """按空白与常见分隔符切词并去重。对应 Go ``splitQueryTerms``。"""
    fields = _QUERY_SPLIT.split(str(query or "").lower())
    out: list[str] = []
    seen: set[str] = set()
    for field in fields:
        text = field.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def match_terms(character: Character, terms: list[str], match_all: bool) -> tuple[int, str]:
    """算分并生成片段。对应 Go ``Character.matchTerms``。"""
    meta = "\n".join(
        [
            character.id,
            character.name,
            character.description,
            " ".join(character.aliases),
            " ".join(character.tags),
        ]
    ).lower()
    kinds = sorted(character.docs)
    score = 0
    matched = 0
    snippet_kind = ""
    snippet_text = ""
    for term in terms:
        count = meta.count(term) * 3
        found = term in meta
        for kind in kinds:
            content = character.docs[kind]
            hits = content.lower().count(term)
            if hits > 0:
                found = True
                count += hits
                if snippet_text == "":
                    snippet_kind = kind
                    snippet_text = content
        if not found:
            if match_all:
                return 0, ""
            continue
        matched += 1
        score += count
    if matched == 0:
        return 0, ""
    snippet = str(character.description or "").strip()
    if snippet_text:
        snippet = make_snippet(snippet_text, terms)
    if snippet_kind:
        snippet = snippet_kind + ": " + snippet
    return score, snippet


def make_snippet(text: str, terms: list[str]) -> str:
    """截取命中词附近的窗口。对应 Go ``makeSnippet``。"""
    lower = str(text or "").lower()
    index = -1
    for term in terms:
        found = lower.find(term)
        if found < 0:
            continue
        index = found
        break
    if index < 0:
        index = 0
    start = index - SNIPPET_RUNES // 3
    if start < 0:
        start = 0
    end = start + SNIPPET_RUNES
    if end > len(text):
        end = len(text)
        if end - SNIPPET_RUNES > 0:
            start = end - SNIPPET_RUNES
    clipped = " ".join(text[start:end].split())
    if start > 0:
        clipped = "..." + clipped
    if end < len(text):
        clipped += "..."
    return clipped


def search(
    store: Store,
    ctx: Any,
    query: str,
    match_mode: str = "",
    tag: str = "",
    limit: int = 0,
    viewer: Viewer | None = None,
) -> list[SearchResult]:
    """在角色索引上做一次内存检索。对应 Go ``Store.Search``。"""
    person = viewer or Viewer()
    terms = split_query_terms(query)
    if not terms:
        return []
    store._ensure(ctx)
    match_all = str(match_mode or "").strip().lower() == "and"
    want_tag = str(tag or "").strip().lower()
    with store._lock:
        results: list[SearchResult] = []
        for item_id in store._order:
            item = store._entries[item_id]
            if not item.visible_to(person):
                continue
            if want_tag and not has_tag(item.tags, want_tag):
                continue
            score, snippet = match_terms(item, terms, match_all)
            if score == 0:
                continue
            results.append(
                SearchResult(
                    id=item.id,
                    name=item.name,
                    tags=list(item.tags),
                    snippet=snippet,
                    score=score,
                )
            )
    # Go：先按 score 降序，再按 id 升序（稳定排序）。
    results.sort(key=lambda item: (-item.score, item.id))
    if limit <= 0:
        limit = DEFAULT_SEARCH_LIMIT
    if limit > MAX_SEARCH_LIMIT:
        limit = MAX_SEARCH_LIMIT
    return results[:limit]
