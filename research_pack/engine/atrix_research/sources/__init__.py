"""检索源注册表与统一调度。

统一入口 :func:`search_all` 负责"多源 × 多查询"的编排，并把每个
"源+查询"的结果记成一条可审计的状态（成功/失败/条数/耗时），
单个源失败不会让整次检索失败——但**全部**失败会抛错，绝不返回空列表
让上游误以为"没搜到"。
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from ..errors import SOURCE_UNAVAILABLE, ResearchError
from ..http import HttpClient
from ..models import Work
from . import arxiv, crossref, openalex, pubmed, semanticscholar

__all__ = [
    "DEFAULT_SOURCES",
    "SOURCES",
    "available_sources",
    "describe_sources",
    "resolve_sources",
    "search_all",
]

#: 模块表；新增源只需在这里登记并实现 ``search(http, query, *, limit, ...)``。
SOURCES: dict[str, Any] = {
    openalex.NAME: openalex,
    crossref.NAME: crossref,
    arxiv.NAME: arxiv,
    semanticscholar.NAME: semanticscholar,
    pubmed.NAME: pubmed,
}

#: 默认三源：覆盖广、免密钥、限速宽松。
DEFAULT_SOURCES = ["openalex", "crossref", "arxiv"]

_ALIASES = {
    "oa": "openalex",
    "semantic": "semanticscholar",
    "s2": "semanticscholar",
    "pm": "pubmed",
    "medline": "pubmed",
    "arxiv.org": "arxiv",
}


def resolve_sources(names: Iterable[str] | None) -> list[str]:
    """把用户写的源名解析成已注册的 id，未知源直接报错并列出可选项。"""
    raw = [str(item).strip().lower() for item in (names or []) if str(item).strip()]
    if not raw:
        return list(DEFAULT_SOURCES)
    resolved: list[str] = []
    for name in raw:
        canonical = _ALIASES.get(name, name)
        if canonical not in SOURCES:
            raise ResearchError(
                "ARG_INVALID",
                f"未知检索源: {name}",
                hint=f"可用源: {', '.join(SOURCES)}（别名: {', '.join(_ALIASES)}）。",
            )
        if canonical not in resolved:
            resolved.append(canonical)
    return resolved


def available_sources() -> list[str]:
    return list(SOURCES)


def describe_sources() -> list[dict[str, Any]]:
    return [dict(module.SOURCE) for module in SOURCES.values()]


def search_all(
    http: HttpClient,
    queries: list[str],
    *,
    sources: list[str] | None = None,
    limit: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    mailto: str = "",
) -> tuple[list[Work], list[dict[str, Any]]]:
    """逐源逐查询检索。

    返回 ``(全部候选, 每步状态)``；候选保留 ``source_hits``，
    去重由 :mod:`atrix_research.dedupe` 负责（检索层不做合并，
    这样同一条记录能保留所有来源的原始字段）。
    """
    chosen = resolve_sources(sources) if sources else list(DEFAULT_SOURCES)
    clean_queries = [str(query).strip() for query in queries if str(query).strip()]
    if not clean_queries:
        raise ResearchError(
            "ARG_INVALID",
            "至少需要一个检索词",
            hint="在 --query 里给出研究问题拆出的关键词（可给多个）。",
        )
    works: list[Work] = []
    report: list[dict[str, Any]] = []
    ok_count = 0
    for source_id in chosen:
        module = SOURCES[source_id]
        for query in clean_queries:
            started = time.time()
            try:
                found = module.search(
                    http,
                    query,
                    limit=limit,
                    year_from=year_from,
                    year_to=year_to,
                    mailto=mailto,
                )
            except ResearchError as exc:
                elapsed = int((time.time() - started) * 1000)
                report.append(
                    {
                        "source": source_id,
                        "query": query,
                        "ok": False,
                        "count": 0,
                        "elapsed_ms": elapsed,
                        "error": exc.to_dict(),
                    }
                )
                continue
            elapsed = int((time.time() - started) * 1000)
            ok_count += 1
            works.extend(found)
            report.append(
                {
                    "source": source_id,
                    "query": query,
                    "ok": True,
                    "count": len(found),
                    "elapsed_ms": elapsed,
                }
            )
    if ok_count == 0 and report:
        first_error = next((item["error"] for item in report if item.get("error")), {})
        raise ResearchError(
            SOURCE_UNAVAILABLE,
            "所有检索源本次都失败了",
            hint="检查网络；或先用 --sources openalex 单源重试。",
            details={"first_error": first_error, "steps": len(report)},
        )
    return works, report
