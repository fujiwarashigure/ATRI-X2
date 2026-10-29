"""引文核验：把"模型说它存在"变成"数据源证明它存在"。

核验分三件事：

1. **DOI 可解析**：Crossref 按 DOI 精确反查是否 200；
2. **元数据一致**：标题相似度、年份、第一作者姓氏是否对得上；
3. **撤稿状态**：来源里带了 ``is_retracted`` 就采信（OpenAlex / S2）。

缺失 DOI 的条目会用标题在 Crossref 里反查一次，给出"建议 DOI"，但**不会**
自动写入记录——自动补 DOI 会把"看起来像"变成"就是它"，正是伪造引文的起点。
"""

from __future__ import annotations

from typing import Any, Callable

from .dedupe import jaccard, title_tokens
from .http import HttpClient, HttpError
from .models import Work
from .sources import crossref, datacite

__all__ = [
    "STATUS_LABELS",
    "TITLE_MATCH_OK",
    "title_score",
    "verify_library",
    "verify_work",
]

TITLE_MATCH_OK = 0.90
TITLE_MATCH_WARN = 0.75
TITLE_MATCH_SUGGEST = 0.92

STATUS_LABELS = {
    "VERIFIED": "DOI 可解析且元数据一致",
    "PARTIAL": "DOI 可解析，但部分元数据不一致",
    "MISMATCH": "DOI 可解析，但标题/年份/作者冲突（疑似错配）",
    "NOT_FOUND": "DOI 无法解析（可能已失效或不存在）",
    "NO_DOI": "记录本身没有 DOI",
    "NOT_RUN": "未核验",
}


def title_score(left: str, right: str) -> float:
    """标题相似度：Jaccard 与"短标题被长标题包含"取较大者。

    只比 Jaccard 会把"正标题相同、副标题不同"误判成冲突（例如
    ``Retrieval-Augmented Generation`` 对
    ``Retrieval-Augmented Generation: A Survey of ...``）。但包含系数对
    极短标题很敏感，所以要求短标题至少 3 个词元、长标题不超过其 3 倍。
    """
    a = title_tokens(left or "")
    b = title_tokens(right or "")
    base = jaccard(a, b)
    if not a or not b:
        return base
    short, long_tokens = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 3 and len(long_tokens) <= len(short) * 3:
        containment = len(a & b) / len(short)
        return max(base, containment)
    return base


def _resolve_doi(http: HttpClient, doi: str) -> tuple[Work | None, str]:
    """按注册机构解析 DOI，返回 ``(记录, 注册机构)``。

    arXiv / 数据集 DOI 在 DataCite，期刊 DOI 在 Crossref；交叉兜底一次，
    避免把注册机构不同误判成"DOI 不存在"。
    """
    clean = (doi or "").strip()
    if not clean:
        return None, ""
    if clean.lower().startswith("10.48550/"):
        found = datacite.fetch_doi(http, clean)
        if found is not None:
            return found, datacite.NAME
        return None, ""
    found = crossref.fetch_doi(http, clean)
    if found is not None:
        return found, crossref.NAME
    found = datacite.fetch_doi(http, clean)
    if found is not None:
        return found, datacite.NAME
    return None, ""


def _similarity(left: str, right: str) -> float:
    return title_score(left, right)


def verify_work(
    http: HttpClient,
    work: Work,
    *,
    title_pass: float = TITLE_MATCH_OK,
    title_warn: float = TITLE_MATCH_WARN,
    title_suggest: float = TITLE_MATCH_SUGGEST,
    suggest_missing_doi: bool = True,
) -> dict[str, Any]:
    """核验单条记录。网络错误向上抛，由 :func:`verify_library` 记为 NOT_RUN。"""
    result: dict[str, Any] = {
        "key": work.key,
        "title": work.title,
        "doi": work.doi,
        "status": "NO_DOI",
        "checks": {},
        "notes": [],
    }
    evidence = work.doi
    if not evidence and suggest_missing_doi:
        # 用标题反查一次，只给建议，不改记录
        try:
            candidates = crossref.search(http, work.title, limit=3)
        except HttpError as exc:
            candidates = []
            result["notes"].append(f"标题反查失败: {exc.message}")
        best = None
        best_score = 0.0
        for candidate in candidates:
            score = _similarity(work.title, candidate.title)
            year_ok = work.year is None or candidate.year is None or work.year == candidate.year
            author_ok = (
                not work.first_author_family()
                or not candidate.first_author_family()
                or work.first_author_family().lower() == candidate.first_author_family().lower()
            )
            if score > best_score and year_ok and author_ok:
                best, best_score = candidate, score
        if best is not None and best_score >= title_suggest and best.doi:
            result["suggested_doi"] = best.doi
            result["suggested_similarity"] = round(best_score, 3)
            result["notes"].append(
                "记录缺 DOI；已按标题+年份+作者反查给出候选 DOI（未自动写入，需人工确认）"
            )
        return result

    if not evidence:
        return result

    found, registrar = _resolve_doi(http, evidence)
    result["checks"]["doi_resolvable"] = found is not None
    if found is not None:
        result["checks"]["registrar"] = registrar
    if found is None:
        result["status"] = "NOT_FOUND"
        return result

    similarity = _similarity(work.title, found.title)
    year_match = work.year is None or found.year is None or work.year == found.year
    author_match = (
        not work.first_author_family()
        or not found.first_author_family()
        or work.first_author_family().lower() == found.first_author_family().lower()
    )
    result["checks"].update(
        {
            "title_similarity": round(similarity, 3),
            "year_match": year_match,
            "first_author_match": author_match,
            "found_title": found.title,
            "found_year": found.year,
            "found_venue": found.venue,
        }
    )
    if similarity >= title_pass and year_match and author_match:
        result["status"] = "VERIFIED"
    elif similarity >= title_warn and (year_match or author_match):
        result["status"] = "PARTIAL"
    else:
        result["status"] = "MISMATCH"
        result["notes"].append("DOI 指向的元数据与该条目明显不同，引用前请人工确认")

    if work.is_retracted is True:
        result["notes"].append("来源标记为已撤稿（引用前必须处理）")
        result["retracted"] = True
    return result


def verify_library(
    http: HttpClient,
    works: list[Work],
    *,
    title_pass: float = TITLE_MATCH_OK,
    title_warn: float = TITLE_MATCH_WARN,
    min_verified_ratio: float = 0.90,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """逐条核验；单条网络失败记 ``NOT_RUN``，不让整批失败。"""
    items: list[dict[str, Any]] = []
    selected = works[: limit] if limit else works
    total = len(selected)
    for index, work in enumerate(selected):
        try:
            item = verify_work(
                http, work, title_pass=title_pass, title_warn=title_warn
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败要降级成 NOT_RUN
            message = getattr(exc, "message", None) or str(exc)
            item = {
                "key": work.key,
                "title": work.title,
                "doi": work.doi,
                "status": "NOT_RUN",
                "checks": {},
                "notes": [f"核验失败: {message}"],
            }
        items.append(item)
        if progress is not None:
            progress(index + 1, total)

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    verified = counts.get("VERIFIED", 0) + counts.get("PARTIAL", 0)
    ratio = verified / len(items) if items else 0.0
    not_run = counts.get("NOT_RUN", 0)
    gate = "PASS"
    if not items:
        gate = "FAIL"
    elif not_run and not_run == len(items):
        gate = "FAIL"
    elif ratio < min_verified_ratio:
        gate = "WARN"
    return {
        "schema": "atrix-research/verification/1",
        "counts": counts,
        "total": len(items),
        "verified_ratio": round(ratio, 4),
        "min_verified_ratio": min_verified_ratio,
        "gate": gate,
        "summary": {
            "verified": counts.get("VERIFIED", 0),
            "partial": counts.get("PARTIAL", 0),
            "mismatch": counts.get("MISMATCH", 0),
            "not_found": counts.get("NOT_FOUND", 0),
            "no_doi": counts.get("NO_DOI", 0),
            "not_run": not_run,
        },
        "items": items,
    }
