"""筛选（PRISMA）规则引擎。

为什么用规则而不是让模型自由裁量：筛选是**可审计**的环节，评审需要知道
"为什么排除这篇"。因此每条排除都有固定原因码，规则、阈值、人工覆盖全部
落盘（``criteria.json`` / ``decisions.json``），重跑可复现。

两个阶段：

* ``title_abstract``：只看元数据（年份/类型/语言/摘要是否缺失/关键词）；
* ``full_text``：需要**人工或模型给出全文判断**（写在 ``decisions.json``），
  引擎只做一致性校验与 PRISMA 计数。没给判断的条目保持 ``pending``，
  闸门不会让流程带着未决条目进入写作。
"""

from __future__ import annotations

from typing import Any, Iterable

from .dedupe import normalize_title
from .models import Work, has_cjk

__all__ = [
    "DEFAULT_CRITERIA",
    "REASON_LABELS",
    "decide",
    "prisma_markdown",
    "run_screening",
]

#: 原因码 → 人类可读说明（报告与 PRISMA 图共用）。
REASON_LABELS = {
    "I-OK": "符合纳入标准",
    "E-YEAR": "年份不在范围内",
    "E-TYPE": "文献类型不在范围内",
    "E-LANG": "语言不在范围内",
    "E-NOABS": "缺少摘要，无法做标题摘要筛选",
    "E-RETRACTED": "已撤稿",
    "E-KEYWORD": "命中排除关键词",
    "E-VENUE": "发表于排除的期刊/会议",
    "E-MANUAL": "人工排除",
    "U-PENDING": "全文阶段未给出判断",
    "U-NOTFOUND": "缺少全文，无法判断",
}

#: 默认纳入标准：不设年份下限（由用户按领域决定），类型宽松，接受中英文。
DEFAULT_CRITERIA: dict[str, Any] = {
    "schema": "atrix-research/criteria/1",
    "include": {
        "year_from": None,
        "year_to": None,
        "languages": ["en", "zh"],
        "types": ["article", "review", "preprint", "conference", "book", "chapter", "other"],
        "require_abstract": False,
        "title_keywords": [],
        "abstract_keywords": [],
    },
    "exclude": {
        "title_keywords": [],
        "abstract_keywords": [],
        "venues": [],
        "retracted": True,
    },
    "notes": "include.title_keywords 非空时，标题必须命中其中一个才纳入。",
}

#: Crossref / OpenAlex / S2 的类型词汇归一化。
_TYPE_MAP = {
    "journal-article": "article",
    "article": "article",
    "review": "review",
    "review-article": "review",
    "posted-content": "preprint",
    "preprint": "preprint",
    "proceedings-article": "conference",
    "conference": "conference",
    "book": "book",
    "book-chapter": "chapter",
    "chapter": "chapter",
    "monograph": "book",
    "dissertation": "thesis",
    "thesis": "thesis",
    "report": "report",
    "dataset": "dataset",
    "editorial": "other",
    "letter": "other",
    "news": "other",
    "erratum": "other",
}


def normalize_type(raw: str) -> str:
    text = (raw or "").strip().lower()
    if not text:
        return ""
    return _TYPE_MAP.get(text, "other")


def _contains_any(haystack: str, needles: Iterable[str]) -> str:
    text = (haystack or "").lower()
    for needle in needles:
        cleaned = str(needle or "").strip().lower()
        if cleaned and cleaned in text:
            return cleaned
    return ""


def _year_of(work: Work) -> int | None:
    return work.year


def decide(work: Work, criteria: dict[str, Any], manual: dict[str, Any] | None = None) -> dict[str, Any]:
    """对单条文献给出筛选判定。

    返回 ``{"decision": "include"|"exclude"|"pending", "code": ..., "detail": ...}``。
    顺序：人工判断 > 撤稿 > 年份 > 类型 > 语言 > 摘要 > 纳入关键词 > 排除关键词 > 期刊。
    """
    manual = manual or {}
    include = criteria.get("include") or {}
    exclude = criteria.get("exclude") or {}

    override = manual.get(work.key)
    if isinstance(override, dict):
        decision = str(override.get("decision") or "").strip().lower()
        if decision in ("include", "exclude"):
            return {
                "decision": decision,
                "code": "I-OK" if decision == "include" else "E-MANUAL",
                "detail": str(override.get("reason") or "人工判断"),
                "manual": True,
            }

    if exclude.get("retracted", True) and work.is_retracted is True:
        return {"decision": "exclude", "code": "E-RETRACTED", "detail": "该记录被标记为已撤稿"}

    year = _year_of(work)
    year_from = include.get("year_from")
    year_to = include.get("year_to")
    if year is not None:
        if year_from is not None and year < int(year_from):
            return {"decision": "exclude", "code": "E-YEAR", "detail": f"{year} < {year_from}"}
        if year_to is not None and year > int(year_to):
            return {"decision": "exclude", "code": "E-YEAR", "detail": f"{year} > {year_to}"}
    elif year_from is not None or year_to is not None:
        # 年份缺失时不直接排除，只记账，避免把高质量预印本误杀
        pass

    allowed_types = [str(item).lower() for item in include.get("types") or []]
    work_type = normalize_type(work.type)
    if allowed_types and work_type and work_type not in allowed_types:
        return {
            "decision": "exclude",
            "code": "E-TYPE",
            "detail": f"类型 {work.type or work_type} 不在 {allowed_types}",
        }

    allowed_languages = [str(item).lower() for item in include.get("languages") or []]
    language = (work.language or "").strip().lower()
    if not language:
        language = "zh" if has_cjk(work.title) else "en"
    if allowed_languages and language not in allowed_languages:
        return {
            "decision": "exclude",
            "code": "E-LANG",
            "detail": f"语言 {language} 不在 {allowed_languages}",
        }

    if include.get("require_abstract") and not (work.abstract or "").strip():
        return {"decision": "exclude", "code": "E-NOABS", "detail": "缺少摘要"}

    include_titles = include.get("title_keywords") or []
    if include_titles:
        hit = _contains_any(work.title, include_titles)
        if not hit:
            return {
                "decision": "exclude",
                "code": "E-KEYWORD",
                "detail": f"标题未命中纳入关键词 {include_titles}",
            }

    title_hit = _contains_any(work.title, exclude.get("title_keywords") or [])
    if title_hit:
        return {"decision": "exclude", "code": "E-KEYWORD", "detail": f"标题命中排除词 {title_hit!r}"}
    abstract_hit = _contains_any(work.abstract, exclude.get("abstract_keywords") or [])
    if abstract_hit:
        return {
            "decision": "exclude",
            "code": "E-KEYWORD",
            "detail": f"摘要命中排除词 {abstract_hit!r}",
        }
    venue_hit = _contains_any(work.venue_or_container(), exclude.get("venues") or [])
    if venue_hit:
        return {"decision": "exclude", "code": "E-VENUE", "detail": f"命中排除期刊 {venue_hit!r}"}

    return {"decision": "include", "code": "I-OK", "detail": "符合纳入标准"}


def _merge_ranges(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in decisions:
        if item["decision"] == "exclude":
            code = item["code"]
            counts[code] = counts.get(code, 0) + 1
    return counts


def run_screening(
    works: list[Work],
    criteria: dict[str, Any],
    *,
    manual: dict[str, Any] | None = None,
    stage: str = "title_abstract",
    duplicates_removed: int = 0,
    identified: int | None = None,
) -> dict[str, Any]:
    """跑一轮筛选，返回判定明细 + PRISMA 计数。"""
    manual = manual or {}
    decisions: list[dict[str, Any]] = []
    included: list[str] = []
    pending: list[str] = []
    for work in works:
        if stage == "full_text":
            override = manual.get(work.key)
            if not isinstance(override, dict) or str(override.get("decision") or "").lower() not in (
                "include",
                "exclude",
            ):
                decisions.append(
                    {
                        "key": work.key,
                        "title": work.title,
                        "decision": "pending",
                        "code": "U-PENDING",
                        "detail": "全文阶段需要显式判断（写入 decisions.json）",
                    }
                )
                pending.append(work.key)
                continue
            outcome = decide(work, criteria, manual)
        else:
            outcome = decide(work, criteria, manual)
        decisions.append(
            {
                "key": work.key,
                "title": work.title,
                "year": work.year,
                "venue": work.venue,
                "decision": outcome["decision"],
                "code": outcome["code"],
                "detail": outcome["detail"],
            }
        )
        if outcome["decision"] == "include":
            included.append(work.key)
        elif outcome["decision"] == "exclude" and outcome.get("manual"):
            # 人工排除有可能"应纳入但作者决定排除"，仍计入排除原因
            pass

    excluded_by_reason = _merge_ranges(decisions)
    screened = len(works)
    excluded = sum(excluded_by_reason.values())
    counts = {
        "identified": int(identified if identified is not None else screened + duplicates_removed),
        "duplicates_removed": int(duplicates_removed),
        "screened": screened,
        "excluded": excluded,
        "excluded_pending": len(pending),
        "included": len(included),
        "excluded_by_reason": excluded_by_reason,
        "excluded_by_reason_labels": {
            code: REASON_LABELS.get(code, code) for code in excluded_by_reason
        },
    }
    return {
        "schema": "atrix-research/screening/1",
        "stage": stage,
        "criteria": criteria,
        "counts": counts,
        "included_keys": included,
        "pending_keys": pending,
        "decisions": decisions,
        "gate": "PASS" if not pending else "FAIL",
    }


def prisma_markdown(counts: dict[str, Any], *, title: str = "筛选流程（PRISMA 2020）") -> str:
    """把计数渲染成可贴进论文的 PRISMA 流程说明。"""
    by_reason = counts.get("excluded_by_reason") or {}
    lines = [
        f"## {title}",
        "",
        f"- 检索获得记录（identified）：{counts.get('identified', 0)}",
        f"- 去重后进入筛选（screened）：{counts.get('screened', 0)}",
        f"- 去重删除（duplicates removed）：{counts.get('duplicates_removed', 0)}",
        f"- 排除（excluded）：{counts.get('excluded', 0)}",
    ]
    if by_reason:
        lines.append("- 排除原因：")
        labels = counts.get("excluded_by_reason_labels") or {}
        for code, value in sorted(by_reason.items()):
            lines.append(f"  - {labels.get(code, code)}（{code}）：{value}")
    if counts.get("excluded_pending"):
        lines.append(f"- 待判定（pending）：{counts['excluded_pending']}")
    lines.append(f"- 纳入（included）：{counts.get('included', 0)}")
    lines.append("")
    lines.append(
        "> 说明：以上数字由引擎按固定规则计算；每条排除的判定与原因码见 screening.json，"
        "复核时无需重跑检索。"
    )
    return "\n".join(lines)
