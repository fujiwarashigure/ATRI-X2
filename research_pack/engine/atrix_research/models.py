"""领域模型：作者、文献条目、运行状态。

字段取舍原则：只保留"格式化引用 + 筛选 + 核验"真正需要的字段，其余留在
``extra`` 里原样透传。这样新增数据源时不用改模型，也不会因为某个源缺字段
就把整条记录丢掉。

``Work.key`` 是稳定标识：优先 DOI，其次 arXiv/PMID/OpenAlex，最后标题哈希。
它同时被用在去重的并查集、筛选决策文件的键、引文标记者三处，所以必须
"同一篇文献在任何一次运行里都得到同一个 key"。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from . import SCHEMA
from .store import now_iso

__all__ = ["Author", "Work", "new_run_state", "validate_run_state"]

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_doi(value: str) -> str:
    """DOI 归一化：去 URL 前缀、去空白、小写。"""
    text = _clean(value)
    text = _DOI_PREFIX_RE.sub("", text)
    return text.strip().lower()


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def guess_language(text: str) -> str:
    """粗粒度语言判断，只区分 ``zh`` / ``en`` / 空。"""
    if not text:
        return ""
    if has_cjk(text):
        return "zh"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return ""


@dataclass
class Author:
    """作者。``family``/``given`` 用于 APA，``name`` 用于 GB/T 与兜底。"""

    family: str = ""
    given: str = ""
    name: str = ""
    orcid: str = ""
    affiliation: str = ""

    def display(self) -> str:
        if self.name:
            return self.name
        parts = [part for part in (self.given, self.family) if part]
        return " ".join(parts) or self.family

    def to_dict(self) -> dict[str, Any]:
        out = {
            "family": self.family,
            "given": self.given,
            "name": self.name,
            "orcid": self.orcid,
            "affiliation": self.affiliation,
        }
        return {key: value for key, value in out.items() if value}

    @classmethod
    def from_dict(cls, data: Any) -> "Author":
        if isinstance(data, str):
            return cls(name=_clean(data))
        if not isinstance(data, dict):
            return cls()
        return cls(
            family=_clean(data.get("family")),
            given=_clean(data.get("given")),
            name=_clean(data.get("name")),
            orcid=_clean(data.get("orcid")),
            affiliation=_clean(data.get("affiliation")),
        )

    @classmethod
    def coerce_list(cls, data: Any) -> list["Author"]:
        if data is None:
            return []
        if isinstance(data, (list, tuple)):
            return [cls.from_dict(item) for item in data if item]
        return [cls.from_dict(data)]


@dataclass
class Work:
    """一条文献记录。"""

    title: str = ""
    authors: list[Author] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    type: str = ""
    language: str = ""
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    pmcid: str = ""
    openalex_id: str = ""
    s2_id: str = ""
    url: str = ""
    abstract: str = ""
    citation_count: int | None = None
    is_retracted: bool | None = None
    source_hits: list[str] = field(default_factory=list)
    retrieved_at: str = ""
    cite_key: str = ""
    #: 去重时被合并进来的其它 key（可追溯"这条记录由哪些命中合成"）
    merged_from: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 标识 --------------------------------------------------------------

    @property
    def key(self) -> str:
        """稳定标识；同一识别码在任何运行里都产生同一个 key。"""
        doi = normalize_doi(self.doi)
        if doi:
            return f"doi:{doi}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.lower()}"
        if self.pmid:
            return f"pmid:{self.pmid}"
        if self.openalex_id:
            return f"openalex:{self.openalex_id.rsplit('/', 1)[-1].lower()}"
        if self.s2_id:
            return f"s2:{self.s2_id}"
        normalized = unicodedata.normalize("NFKC", self.title or "").lower()
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"title:{digest}"

    def short_key(self) -> str:
        return self.key.split(":", 1)[-1][:24]

    # -- 展示 --------------------------------------------------------------

    def first_author_family(self) -> str:
        if not self.authors:
            return ""
        first = self.authors[0]
        if first.family:
            return first.family
        name = first.display()
        # 中文姓名没有空格，取首字之前的部分无意义，直接用全名
        if has_cjk(name):
            return name
        return name.split(" ")[-1] if name else ""

    def venue_or_container(self) -> str:
        return self.venue or self.publisher or ""

    def page_range(self) -> str:
        return (self.pages or "").strip()

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "key": self.key,
            "title": self.title,
            "authors": [author.to_dict() for author in self.authors],
            "year": self.year,
            "venue": self.venue,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "publisher": self.publisher,
            "type": self.type,
            "language": self.language,
            "doi": normalize_doi(self.doi),
            "arxiv_id": self.arxiv_id,
            "pmid": self.pmid,
            "pmcid": self.pmcid,
            "openalex_id": self.openalex_id,
            "s2_id": self.s2_id,
            "url": self.url,
            "abstract": self.abstract,
            "citation_count": self.citation_count,
            "is_retracted": self.is_retracted,
            "source_hits": list(self.source_hits),
            "retrieved_at": self.retrieved_at or now_iso(),
            "cite_key": self.cite_key,
            "merged_from": list(self.merged_from),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Work":
        if not isinstance(data, dict):
            return cls()
        year = data.get("year")
        try:
            year_int = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year_int = None
        citation_count = data.get("citation_count")
        try:
            count_int = int(citation_count) if citation_count is not None else None
        except (TypeError, ValueError):
            count_int = None
        retracted = data.get("is_retracted")
        return cls(
            title=_clean(data.get("title")),
            authors=Author.coerce_list(data.get("authors")),
            year=year_int,
            venue=_clean(data.get("venue")),
            volume=_clean(data.get("volume")),
            issue=_clean(data.get("issue")),
            pages=_clean(data.get("pages")),
            publisher=_clean(data.get("publisher")),
            type=_clean(data.get("type")),
            language=_clean(data.get("language")),
            doi=normalize_doi(_clean(data.get("doi"))),
            arxiv_id=_clean(data.get("arxiv_id")),
            pmid=_clean(data.get("pmid")),
            pmcid=_clean(data.get("pmcid")),
            openalex_id=_clean(data.get("openalex_id")),
            s2_id=_clean(data.get("s2_id")),
            url=_clean(data.get("url")),
            abstract=_clean(data.get("abstract")),
            citation_count=count_int,
            is_retracted=retracted if isinstance(retracted, bool) else None,
            source_hits=[_clean(item) for item in data.get("source_hits", []) or []],
            retrieved_at=_clean(data.get("retrieved_at")),
            cite_key=_clean(data.get("cite_key")),
            merged_from=[_clean(item) for item in data.get("merged_from", []) or []],
            extra=dict(data.get("extra") or {}),
        )


# ---------------------------------------------------------------------------
# 运行状态
# ---------------------------------------------------------------------------


def new_run_state(
    *,
    run_id: str,
    title: str,
    question: str,
    language: str,
    style: str,
    doc_type: str,
    target_words: int,
    owner: str = "",
) -> dict[str, Any]:
    stamp = now_iso()
    return {
        "schema": f"{SCHEMA}/run",
        "id": run_id,
        "title": title,
        "question": question,
        "language": language,
        "style": style,
        "doc_type": doc_type,
        "target_words": int(target_words),
        "owner": owner,
        "stage": "init",
        "created_at": stamp,
        "updated_at": stamp,
        "counts": {},
        "notes": [],
    }


_REQUIRED_STATE_KEYS = ("id", "title", "stage", "created_at")


def validate_run_state(state: Any) -> dict[str, Any]:
    """校验 run.json 的骨架；缺关键字段说明文件被外部改坏了。"""
    from .errors import ARTIFACT_MISSING, ResearchError

    if not isinstance(state, dict):
        raise ResearchError(ARTIFACT_MISSING, "run.json 顶层不是对象", hint="重新创建运行。")
    missing = [key for key in _REQUIRED_STATE_KEYS if not state.get(key)]
    if missing:
        raise ResearchError(
            ARTIFACT_MISSING,
            f"run.json 缺少字段: {', '.join(missing)}",
            hint="该运行目录已损坏；用 research_doctor 的 status 查看其它运行。",
        )
    return state
