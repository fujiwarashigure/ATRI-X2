"""PubMed（NCBI E-utilities）：生物医学检索的权威源，两步查询。

``esearch`` 拿 PMID 列表，``esummary`` 拿元数据。免密钥，但 NCBI 要求
工具标识与（推荐）邮箱；本模块通过 User-Agent 与 ``tool``/``email`` 参数满足。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..http import HttpClient
from ..models import Author, Work

__all__ = ["NAME", "SOURCE", "build_esearch_url", "build_esummary_url", "parse", "search"]

NAME = "pubmed"
SOURCE = {
    "id": NAME,
    "label": "PubMed",
    "needs_key": False,
    "supports_year": True,
    "supports_abstract": False,
    "supports_retraction": False,
    "homepage": "https://pubmed.ncbi.nlm.nih.gov",
}

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TOOL = "atrix-research"
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_LANG = {"eng": "en", "chi": "zh", "zho": "zh"}


def build_esearch_url(query: str, *, limit: int, year_from: int | None = None,
                      year_to: int | None = None, email: str = "") -> str:
    term = query
    if year_from or year_to:
        start = str(year_from) if year_from else "1800"
        end = str(year_to) if year_to else "3000"
        term = f"({query}) AND {start}:{end}[dp]"
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(max(1, min(int(limit), 100))),
        "sort": "relevance",
        "tool": _TOOL,
    }
    if email:
        params["email"] = email
    return f"{_BASE}/esearch.fcgi?{urllib.parse.urlencode(params)}"


def build_esummary_url(pmids: list[str], *, email: str = "") -> str:
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
        "tool": _TOOL,
    }
    if email:
        params["email"] = email
    return f"{_BASE}/esummary.fcgi?{urllib.parse.urlencode(params)}"


def _year(text: str) -> int | None:
    match = re.search(r"(1[6-9]\d{2}|20\d{2}|21\d{2})", text or "")
    return int(match.group(1)) if match else None


def _author(raw: Any) -> Author:
    if not isinstance(raw, dict):
        return Author(name=str(raw or ""))
    name = str(raw.get("name") or "").strip()
    # PubMed 形如 "Smith J" / "van der Berg J"
    parts = name.split(" ")
    if len(parts) >= 2:
        family, given = " ".join(parts[:-1]), parts[-1]
    else:
        family, given = name, ""
    return Author(family=family, given=given, name=name)


def _parse_summary(payload: Any) -> list[Work]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    works: list[Work] = []
    for uid in result.get("uids") or []:
        item = result.get(uid)
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip().rstrip(".")
        if not title:
            continue
        doi = ""
        pmcid = ""
        for identifier in item.get("articleids") or []:
            if not isinstance(identifier, dict):
                continue
            kind = str(identifier.get("idtype") or "")
            value = str(identifier.get("value") or "")
            if kind == "doi":
                doi = value
            elif kind == "pmc":
                pmcid = value
        langs = item.get("lang") or []
        language = _LANG.get(str(langs[0]).lower(), "") if isinstance(langs, list) and langs else ""
        pub_types = item.get("pubtype") or []
        pub_type = ""
        if isinstance(pub_types, list) and pub_types:
            pub_type = "review" if any("Review" in str(kind) for kind in pub_types) else "article"
        works.append(
            Work(
                title=title,
                authors=[_author(raw) for raw in item.get("authors") or []],
                year=_year(str(item.get("pubdate") or "")),
                venue=str(item.get("fulljournalname") or item.get("source") or ""),
                volume=str(item.get("volume") or ""),
                issue=str(item.get("issue") or ""),
                pages=str(item.get("pages") or ""),
                type=pub_type,
                language=language,
                doi=doi,
                pmid=str(uid),
                pmcid=pmcid,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                source_hits=[NAME],
            )
        )
    return works


def parse(payload: Any) -> list[Work]:
    return _parse_summary(payload)


def search(
    http: HttpClient,
    query: str,
    *,
    limit: int,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str = "",
    **_: Any,
) -> list[Work]:
    http.set_host_interval("eutils.ncbi.nlm.nih.gov", 0.4)
    search_url = build_esearch_url(
        query, limit=limit, year_from=year_from, year_to=year_to, email=mailto
    )
    payload = http.get_json(search_url)
    if not isinstance(payload, dict):
        return []
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        return []
    ids = [str(uid) for uid in result.get("idlist") or [] if str(uid).strip()]
    if not ids:
        return []
    summary_payload = http.get_json(build_esummary_url(ids, email=mailto))
    return _parse_summary(summary_payload)
