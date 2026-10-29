"""arXiv：预印本源。Atom XML，无摘要长度限制、更新快。

官方要求请求间隔 3 秒，因此 :func:`search` 会为 ``export.arxiv.org``
设置 3 秒的主机级限速。
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from ..http import HttpClient
from ..models import Author, Work

__all__ = ["NAME", "SOURCE", "build_url", "parse", "search"]

NAME = "arxiv"
SOURCE = {
    "id": NAME,
    "label": "arXiv",
    "needs_key": False,
    "supports_year": False,
    "supports_abstract": True,
    "supports_retraction": False,
    "homepage": "https://arxiv.org",
}

_API = "http://export.arxiv.org/api/query"
_HOST = "export.arxiv.org"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_ABS_RE = re.compile(r"/abs/([^/?#]+)")
_VERSION_RE = re.compile(r"v\d+$")
_TAG_RE = re.compile(r"\s+")


def build_url(query: str, *, limit: int) -> str:
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(max(1, min(int(limit), 100))),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    return f"{_API}?{urllib.parse.urlencode(params)}"


def _clean(text: str | None) -> str:
    return _TAG_RE.sub(" ", (text or "").strip())


def parse(xml_text: str) -> list[Work]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    works: list[Work] = []
    for entry in root.findall(f"{_ATOM}entry"):
        identifier = _clean(entry.findtext(f"{_ATOM}id"))
        match = _ABS_RE.search(identifier)
        if not match:
            continue
        arxiv_id = _VERSION_RE.sub("", match.group(1))
        title = _clean(entry.findtext(f"{_ATOM}title"))
        if not title:
            continue
        authors = []
        for author_node in entry.findall(f"{_ATOM}author"):
            name = _clean(author_node.findtext(f"{_ATOM}name"))
            if not name:
                continue
            parts = name.split(" ")
            family = parts[-1] if parts else name
            given = " ".join(parts[:-1])
            authors.append(Author(family=family, given=given, name=name))
        published = _clean(entry.findtext(f"{_ATOM}published"))
        year = int(published[:4]) if published[:4].isdigit() else None
        doi = _clean(entry.findtext(f"{_ARXIV}doi"))
        journal_ref = _clean(entry.findtext(f"{_ARXIV}journal_ref"))
        primary_category = entry.find(f"{_ARXIV}primary_category")
        category = primary_category.get("term", "") if primary_category is not None else ""
        summary = _clean(entry.findtext(f"{_ATOM}summary"))
        work = Work(
            title=title,
            authors=authors,
            year=year,
            venue=journal_ref or "arXiv",
            type="preprint",
            language="en",
            doi=doi,
            arxiv_id=arxiv_id,
            url=identifier.replace("http://", "https://", 1),
            abstract=summary,
            extra={"category": category},
            source_hits=[NAME],
        )
        works.append(work)
    return works


def search(http: HttpClient, query: str, *, limit: int, **_: Any) -> list[Work]:
    http.set_host_interval(_HOST, 3.0)
    url = build_url(query, limit=limit)
    return parse(http.get_text(url, accept="application/atom+xml"))
