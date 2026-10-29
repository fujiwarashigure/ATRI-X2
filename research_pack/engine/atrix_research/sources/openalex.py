"""OpenAlex：免密钥、覆盖广、带摘要与撤稿标记，作为默认主检索源。

文档：https://docs.openalex.org/api-entities/works/search-works

注意事项：

* 摘要以 ``abstract_inverted_index``（词 → 位置列表）形式返回，需要重建；
* 支持 ``filter=from_publication_date:YYYY-01-01`` 做年份区间过滤；
* 大量请求时应带 ``mailto`` 进 polite pool（配置项 ``contact_mailto``）。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..http import HttpClient
from ..models import Author, Work, guess_language

__all__ = ["NAME", "SOURCE", "build_url", "parse", "search"]

NAME = "openalex"
SOURCE = {
    "id": NAME,
    "label": "OpenAlex",
    "needs_key": False,
    "supports_year": True,
    "supports_abstract": True,
    "supports_retraction": True,
    "homepage": "https://openalex.org",
}

_BASE = "https://api.openalex.org/works"
_MAX_PER_PAGE = 50


def build_url(
    query: str,
    *,
    limit: int,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str = "",
) -> str:
    params: dict[str, str] = {
        "search": query,
        "per_page": str(max(1, min(int(limit), _MAX_PER_PAGE))),
        "page": "1",
    }
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{int(year_from)}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{int(year_to)}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    if mailto:
        params["mailto"] = mailto
    return f"{_BASE}?{urllib.parse.urlencode(params)}"


def _reconstruct_abstract(index: Any) -> str:
    """把 ``{词: [位置...]}`` 还原成词序文本。"""
    if not isinstance(index, dict) or not index:
        return ""
    slots: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                slots.append((position, str(word)))
    slots.sort(key=lambda item: item[0])
    return " ".join(word for _, word in slots)


def _author(raw: Any) -> Author:
    if not isinstance(raw, dict):
        return Author()
    person = raw.get("author") if isinstance(raw.get("author"), dict) else raw
    display = str(person.get("display_name") or "").strip()
    family, given = "", ""
    # OpenAlex 没有结构化的 family/given，按"最后一段是姓"的惯例拆（中文名不拆）
    if display and " " in display and not any("\u3400" <= ch <= "\u9fff" for ch in display):
        parts = display.split(" ")
        given = " ".join(parts[:-1])
        family = parts[-1]
    else:
        family = display
    institutions = raw.get("institutions") or []
    affiliation = ""
    if isinstance(institutions, list) and institutions:
        first = institutions[0]
        if isinstance(first, dict):
            affiliation = str(first.get("display_name") or "")
    orcid = str(person.get("orcid") or "").strip()
    if orcid.startswith("https://orcid.org/"):
        orcid = orcid[len("https://orcid.org/") :]
    return Author(family=family, given=given, name=display, orcid=orcid, affiliation=affiliation)


def _ids(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if isinstance(value, str) and value:
            out[key] = value
    return out


def _doi_from(raw: str) -> str:
    text = (raw or "").strip()
    marker = "doi.org/"
    if marker in text:
        return text.split(marker, 1)[1]
    return text


def _pmid_from(raw: str) -> str:
    text = (raw or "").strip()
    return text.rsplit("/", 1)[-1] if text else ""


def parse(payload: Any) -> list[Work]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    works: list[Work] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("display_name") or raw.get("title") or "").strip()
        if not title:
            continue
        ids = _ids(raw.get("ids"))
        location = raw.get("primary_location") if isinstance(raw.get("primary_location"), dict) else {}
        source = location.get("source") if isinstance(location.get("source"), dict) else {}
        biblio = raw.get("biblio") if isinstance(raw.get("biblio"), dict) else {}
        first_page = str(biblio.get("first_page") or "")
        last_page = str(biblio.get("last_page") or "")
        pages = first_page
        if first_page and last_page and last_page != first_page:
            pages = f"{first_page}-{last_page}"
        language = str(raw.get("language") or "").strip()
        work = Work(
            title=title,
            authors=[_author(item) for item in raw.get("authorships") or [] if isinstance(item, dict)],
            year=raw.get("publication_year") if isinstance(raw.get("publication_year"), int) else None,
            venue=str(source.get("display_name") or ""),
            volume=str(biblio.get("volume") or ""),
            issue=str(biblio.get("issue") or ""),
            pages=pages,
            publisher=str(source.get("host_organization_name") or ""),
            type=str(raw.get("type") or ""),
            language=language or guess_language(title),
            doi=_doi_from(str(raw.get("doi") or ids.get("doi") or "")),
            pmid=_pmid_from(ids.get("pmid") or ""),
            openalex_id=str(raw.get("id") or ""),
            url=str(location.get("landing_page_url") or raw.get("id") or ""),
            abstract=_reconstruct_abstract(raw.get("abstract_inverted_index")),
            citation_count=raw.get("cited_by_count") if isinstance(raw.get("cited_by_count"), int) else None,
            is_retracted=raw.get("is_retracted") if isinstance(raw.get("is_retracted"), bool) else None,
            source_hits=[NAME],
        )
        works.append(work)
    return works


def search(
    http: HttpClient,
    query: str,
    *,
    limit: int,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str = "",
) -> list[Work]:
    url = build_url(query, limit=limit, year_from=year_from, year_to=year_to, mailto=mailto)
    return parse(http.get_json(url))
