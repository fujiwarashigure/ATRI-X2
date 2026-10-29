"""Semantic Scholar Graph API：提供引用数与撤稿标记，匿名可用但限速严格。

文档：https://api.semanticscholar.org/api-docs/graph

匿名调用经常返回 429；本模块把它转成可识别的 :class:`HttpError`，
由 :func:`atrix_research.sources.search_all` 记为"该源本次降级"而不是整次检索失败。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..http import HttpClient
from ..models import Author, Work, guess_language

__all__ = ["NAME", "SOURCE", "build_url", "parse", "search"]

NAME = "semanticscholar"
SOURCE = {
    "id": NAME,
    "label": "Semantic Scholar",
    "needs_key": False,
    "supports_year": True,
    "supports_abstract": True,
    "supports_retraction": True,
    "homepage": "https://www.semanticscholar.org",
}

_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = (
    "title,abstract,year,authors,venue,externalIds,citationCount,isRetracted,"
    "publicationTypes,url,publicationVenue"
)


def build_url(query: str, *, limit: int, year_from: int | None = None, year_to: int | None = None) -> str:
    params: dict[str, str] = {
        "query": query,
        "limit": str(max(1, min(int(limit), 100))),
        "fields": _FIELDS,
    }
    if year_from or year_to:
        start = str(year_from) if year_from else ""
        end = str(year_to) if year_to else ""
        params["year"] = f"{start}-{end}"
    return f"{_API}?{urllib.parse.urlencode(params)}"


def _author(raw: Any) -> Author:
    if not isinstance(raw, dict):
        return Author(name=str(raw or ""))
    name = str(raw.get("name") or "").strip()
    family, given = "", ""
    if name and " " in name and not any("\u3400" <= ch <= "\u9fff" for ch in name):
        parts = name.split(" ")
        given, family = " ".join(parts[:-1]), parts[-1]
    else:
        family = name
    return Author(family=family, given=given, name=name)


def parse(payload: Any) -> list[Work]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    works: list[Work] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        external = raw.get("externalIds") if isinstance(raw.get("externalIds"), dict) else {}
        venue_obj = raw.get("publicationVenue")
        venue = str(raw.get("venue") or "")
        if not venue and isinstance(venue_obj, dict):
            venue = str(venue_obj.get("name") or "")
        types = raw.get("publicationTypes")
        pub_type = str(types[0]) if isinstance(types, list) and types else ""
        works.append(
            Work(
                title=title,
                authors=[_author(item) for item in raw.get("authors") or []],
                year=raw.get("year") if isinstance(raw.get("year"), int) else None,
                venue=venue,
                type=pub_type,
                language=guess_language(title),
                doi=str(external.get("DOI") or ""),
                arxiv_id=str(external.get("ArXiv") or ""),
                pmid=str(external.get("PubMed") or ""),
                s2_id=str(raw.get("paperId") or ""),
                url=str(raw.get("url") or ""),
                abstract=str(raw.get("abstract") or ""),
                citation_count=raw.get("citationCount")
                if isinstance(raw.get("citationCount"), int)
                else None,
                is_retracted=raw.get("isRetracted") if isinstance(raw.get("isRetracted"), bool) else None,
                source_hits=[NAME],
            )
        )
    return works


def search(
    http: HttpClient,
    query: str,
    *,
    limit: int,
    year_from: int | None = None,
    year_to: int | None = None,
    **_: Any,
) -> list[Work]:
    http.set_host_interval("api.semanticscholar.org", 1.2)
    url = build_url(query, limit=limit, year_from=year_from, year_to=year_to)
    return parse(http.get_json(url))
