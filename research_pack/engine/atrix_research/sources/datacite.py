"""DataCite：arXiv 预印本、数据集、机构库的 DOI 注册机构。

Crossref 只覆盖期刊/会议类 DOI；arXiv 的 ``10.48550/arXiv.xxxx`` 属于
DataCite，必须换注册机构查，否则会把"存在且可解析"的预印本误判为
``NOT_FOUND``。这里只实现按 DOI 精确反查（引文核验用），不做检索。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..http import HttpClient, HttpError
from ..models import Author, Work, guess_language

__all__ = ["NAME", "SOURCE", "fetch_doi", "parse"]

NAME = "datacite"
SOURCE = {
    "id": NAME,
    "label": "DataCite",
    "needs_key": False,
    "supports_year": False,
    "supports_abstract": False,
    "supports_retraction": False,
    "homepage": "https://datacite.org",
}

_BASE = "https://api.datacite.org/dois"


def parse(payload: Any) -> Work | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        return None
    titles = attributes.get("titles") or []
    title = ""
    if isinstance(titles, list) and titles and isinstance(titles[0], dict):
        title = str(titles[0].get("title") or "").strip()
    if not title:
        return None
    authors: list[Author] = []
    for raw in attributes.get("creators") or []:
        if not isinstance(raw, dict):
            continue
        family = str(raw.get("familyName") or "").strip()
        given = str(raw.get("givenName") or "").strip()
        name = str(raw.get("name") or "").strip() or " ".join(
            part for part in (given, family) if part
        )
        authors.append(Author(family=family, given=given, name=name))
    year = attributes.get("publicationYear")
    types = attributes.get("types") if isinstance(attributes.get("types"), dict) else {}
    resource_type = str(types.get("resourceTypeGeneral") or "")
    publisher = str(attributes.get("publisher") or "")
    doi = str(attributes.get("doi") or attributes.get("id") or "")
    url = str(attributes.get("url") or "")
    return Work(
        title=title,
        authors=authors,
        year=year if isinstance(year, int) else None,
        venue=publisher or ("arXiv" if doi.lower().startswith("10.48550/arxiv.") else ""),
        type="preprint" if "arxiv" in doi.lower() else resource_type.lower(),
        language=guess_language(title),
        doi=doi,
        url=url,
        source_hits=[NAME],
    )


def fetch_doi(http: HttpClient, doi: str) -> Work | None:
    """按 DOI 精确反查；404 返回 ``None``。"""
    clean = (doi or "").strip()
    if not clean:
        return None
    url = f"{_BASE}/{urllib.parse.quote(clean, safe='')}"
    try:
        payload = http.get_json(url, headers={"Accept": "application/json"})
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    return parse(payload)
