"""Crossref：DOI 权威元数据源，也用作引文核验（按 DOI 反查）。

文档：https://api.crossref.org/swagger-ui/index.html

要点：

* ``select`` 限制返回字段，减少体积；
* 摘要常是 JATS XML 片段，需要剥标签；
* 礼貌池靠 ``mailto`` 参数（配置项 ``contact_mailto``）。
"""

from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any

from ..http import HttpClient
from ..models import Author, Work, guess_language

__all__ = ["NAME", "SOURCE", "fetch_doi", "parse", "parse_item", "search", "strip_jats"]

NAME = "crossref"
SOURCE = {
    "id": NAME,
    "label": "Crossref",
    "needs_key": False,
    "supports_year": True,
    "supports_abstract": True,
    "supports_retraction": False,
    "homepage": "https://www.crossref.org",
}

_BASE = "https://api.crossref.org/works"
_HEADERS = {"Accept": "application/json"}
_JATS = re.compile(r"<[^>]+>")
_SELECT = (
    "DOI,title,author,container-title,issued,volume,issue,page,publisher,type,"
    "abstract,is-referenced-by-count,URL"
)


def build_search_url(
    query: str,
    *,
    limit: int,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str = "",
    select: str = _SELECT,
) -> str:
    params: dict[str, str] = {
        "query.bibliographic": query,
        "rows": str(max(1, min(int(limit), 100))),
        "select": select,
    }
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{int(year_from)}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{int(year_to)}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    if mailto:
        params["mailto"] = mailto
    return f"{_BASE}?{urllib.parse.urlencode(params)}"


def strip_jats(text: str) -> str:
    """去掉 JATS/HTML 标签与实体，压缩空白。"""
    if not text:
        return ""
    without_tags = _JATS.sub(" ", str(text))
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _first(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0] or "").strip()
    return str(value or "").strip()


def _year(item: dict[str, Any]) -> int | None:
    issued = item.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                return None
    return None


def _author(raw: Any) -> Author:
    if not isinstance(raw, dict):
        return Author(name=str(raw or "").strip())
    family = str(raw.get("family") or "").strip()
    given = str(raw.get("given") or "").strip()
    name = str(raw.get("name") or "").strip() or " ".join(part for part in (given, family) if part)
    affiliations = raw.get("affiliation") or []
    affiliation = ""
    if isinstance(affiliations, list) and affiliations:
        first = affiliations[0]
        if isinstance(first, dict):
            affiliation = str(first.get("name") or "")
    return Author(family=family, given=given, name=name, orcid=str(raw.get("ORCID") or ""), affiliation=affiliation)


def parse_item(item: Any) -> Work | None:
    if not isinstance(item, dict):
        return None
    title = _first(item.get("title"))
    if not title:
        return None
    subtitle = _first(item.get("subtitle"))
    if subtitle:
        title = f"{title}: {subtitle}"
    raw_type = str(item.get("type") or "")
    work = Work(
        title=title,
        authors=[_author(raw) for raw in item.get("author") or []],
        year=_year(item),
        venue=_first(item.get("container-title")),
        volume=str(item.get("volume") or ""),
        issue=str(item.get("issue") or ""),
        pages=str(item.get("page") or ""),
        publisher=str(item.get("publisher") or ""),
        type=raw_type,
        language=str(item.get("language") or "") or guess_language(title),
        doi=str(item.get("DOI") or ""),
        url=str(item.get("URL") or ""),
        abstract=strip_jats(str(item.get("abstract") or "")),
        citation_count=item.get("is-referenced-by-count")
        if isinstance(item.get("is-referenced-by-count"), int)
        else None,
        source_hits=[NAME],
    )
    return work


def parse(payload: Any) -> list[Work]:
    if not isinstance(payload, dict):
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    items = message.get("items")
    if not isinstance(items, list):
        return []
    works = []
    for item in items:
        work = parse_item(item)
        if work is not None:
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
    url = build_search_url(
        query, limit=limit, year_from=year_from, year_to=year_to, mailto=mailto
    )
    return parse(http.get_json(url, headers=_HEADERS))


def fetch_doi(http: HttpClient, doi: str) -> Work | None:
    """按 DOI 精确反查；不存在返回 ``None``（404 不抛错，供核验统计）。"""
    from ..http import HttpError

    clean = (doi or "").strip()
    if not clean:
        return None
    url = f"{_BASE}/{urllib.parse.quote(clean, safe='')}"
    try:
        payload = http.get_json(url, headers=_HEADERS)
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(payload, dict):
        return None
    return parse_item(payload.get("message"))
