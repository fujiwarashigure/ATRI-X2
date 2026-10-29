"""研究报告装配：把各阶段产物拼成一份可审计的证据简报。

这份报告**不是**论文正文——正文由写作环节（模型）按 ``outline.json`` 的契约
填写，之后用 ``research_cite action=check`` 校验。报告负责的是"方法可复现、
证据可追溯"，因此所有数字都直接来自落盘产物，没有一处是叙述性估计。
"""

from __future__ import annotations

from typing import Any

from .cite import reference_markdown
from .models import Work
from .screen import prisma_markdown

__all__ = ["build_report", "build_report_markdown"]


def _md_cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")[:120]


def _status_of(verification: dict[str, Any] | None) -> dict[str, str]:
    if not verification:
        return {}
    return {item["key"]: item["status"] for item in verification.get("items") or []}


def _authors_short(work: Work) -> str:
    family = work.first_author_family()
    if not family:
        return "—"
    suffix = " 等" if any("\u3400" <= ch <= "\u9fff" for ch in family) else (" et al." if len(work.authors) > 1 else "")
    return f"{family}{suffix}"


def _limitations(
    state: dict[str, Any],
    works: list[Work],
    search: dict[str, Any] | None,
    screening: dict[str, Any] | None,
    verification: dict[str, Any] | None,
) -> list[str]:
    items: list[str] = []
    steps = (search or {}).get("steps") or []
    failed = [step for step in steps if not step.get("ok")]
    if failed:
        sources = sorted({str(step.get("source")) for step in failed})
        items.append(
            f"检索阶段有 {len(failed)} 个数据源调用失败（{', '.join(sources)}），"
            "可能存在覆盖缺口；失败明细见 search.json 的 steps。"
        )
    counts = (screening or {}).get("counts") or {}
    if counts.get("excluded_pending"):
        items.append(f"全文阶段仍有 {counts['excluded_pending']} 条待判定，PRISMA 数字尚未闭合。")
    if verification:
        summary = verification.get("summary") or {}
        if summary.get("no_doi"):
            items.append(f"{summary['no_doi']} 条记录没有 DOI，无法做 DOI 级核验，只按标题反查给出候选。")
        if summary.get("mismatch"):
            items.append(f"{summary['mismatch']} 条记录的 DOI 元数据与条目冲突，引用前必须人工确认。")
        if verification.get("gate") != "PASS":
            items.append(
                f"引文核验闸门为 {verification.get('gate')}（核验通过率 "
                f"{verification.get('verified_ratio')}，阈值 {verification.get('min_verified_ratio')}）。"
            )
    languages = {(work.language or "").lower() for work in works if work.language}
    if languages and "zh" not in languages:
        items.append("纳入文献只有外文（无中文文献），结论存在语言偏倚。")
    if not works:
        items.append("没有纳入任何文献，后续结论不应包含文献层面的判断。")
    if (state.get("doc_type") or "") == "review" and len(works) < 10:
        items.append(f"综述仅纳入 {len(works)} 条文献，证据体量偏小，适合作为快速综述而非系统综述。")
    return items


def build_report_markdown(
    state: dict[str, Any],
    works: list[Work],
    *,
    search: dict[str, Any] | None = None,
    screening: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    references: dict[str, Any] | None = None,
    outline: dict[str, Any] | None = None,
    panel: dict[str, Any] | None = None,
    qa: dict[str, Any] | None = None,
    engine_version: str = "",
    generated_at: str = "",
) -> str:
    """装配研究报告 Markdown。"""
    title = state.get("title") or "研究报告"
    lines: list[str] = [f"# {title}", ""]
    lines.append(
        f"> 运行 `{state.get('id', '')}` · 问题：{state.get('question', '（未填写）')}  "
    )
    lines.append(
        f"> 文体：{state.get('doc_type', '')} · 语言：{state.get('language', '')} · "
        f"引用样式：{state.get('style', '')} · 引擎：{engine_version or '-'} · 生成：{generated_at or '-'}"
    )
    lines.append("")

    # -- 1 研究问题 --------------------------------------------------------
    lines += ["## 1 研究问题与范围", ""]
    lines.append(str(state.get("question") or "（未填写）").strip())
    lines.append("")
    scope = []
    if state.get("target_words"):
        scope.append(f"目标篇幅 {state['target_words']} 字")
    if state.get("doc_type"):
        scope.append(f"文体 {state['doc_type']}")
    if scope:
        lines.append("- " + "；".join(scope))
        lines.append("")

    # -- 2 检索与筛选 ------------------------------------------------------
    lines += ["## 2 检索与筛选方法", ""]
    steps = (search or {}).get("steps") or []
    if steps:
        lines.append("### 2.1 检索执行")
        lines.append("")
        lines.append("| 数据源 | 检索式 | 状态 | 命中 | 耗时(ms) |")
        lines.append("| --- | --- | --- | --- | --- |")
        for step in steps:
            status = "成功" if step.get("ok") else "失败"
            lines.append(
                f"| {_md_cell(step.get('source'))} | {_md_cell(step.get('query'))} | {status} | "
                f"{step.get('count', 0)} | {step.get('elapsed_ms', '')} |"
            )
        lines.append("")
    criteria = (screening or {}).get("criteria") or {}
    if criteria:
        include = criteria.get("include") or {}
        exclude = criteria.get("exclude") or {}
        lines.append("### 2.2 纳入与排除标准")
        lines.append("")
        lines.append(
            f"- 年份：{include.get('year_from') or '不限'} → {include.get('year_to') or '不限'}"
        )
        lines.append(f"- 语言：{', '.join(include.get('languages') or []) or '不限'}")
        lines.append(f"- 类型：{', '.join(include.get('types') or []) or '不限'}")
        if include.get("title_keywords"):
            lines.append(f"- 标题须命中：{', '.join(include['title_keywords'])}")
        if include.get("require_abstract"):
            lines.append("- 必须有摘要")
        excluded_parts = []
        for key in ("title_keywords", "abstract_keywords", "venues"):
            values = exclude.get(key) or []
            if values:
                excluded_parts.append(f"{key}={', '.join(map(str, values))}")
        if exclude.get("retracted", True):
            excluded_parts.append("排除已撤稿")
        if excluded_parts:
            lines.append("- 排除：" + "；".join(excluded_parts))
        lines.append("")
    counts = (screening or {}).get("counts") or {}
    if counts:
        lines.append(prisma_markdown(counts))
        lines.append("")

    # -- 3 证据概览 --------------------------------------------------------
    lines += ["## 3 纳入证据概览", ""]
    status_map = _status_of(verification)
    if works:
        lines.append(f"共纳入 {len(works)} 条文献，按年份从新到旧排列。")
        lines.append("")
        lines.append("| # | 年份 | 作者 | 标题 | 来源 | 被引 | 核验 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        ordered = sorted(works, key=lambda work: (work.year or 0), reverse=True)
        for index, work in enumerate(ordered, start=1):
            lines.append(
                f"| {index} | {work.year or ''} | {_md_cell(_authors_short(work))} | "
                f"{_md_cell(work.title)} | {_md_cell(work.venue)} | "
                f"{work.citation_count if work.citation_count is not None else ''} | "
                f"{status_map.get(work.key, '未核验')} |"
            )
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    # -- 4 证据质量 --------------------------------------------------------
    lines += ["## 4 证据质量与可核查性", ""]
    if verification:
        summary = verification.get("summary") or {}
        lines.append(
            f"- 核验通过率：{verification.get('verified_ratio')}（闸门 {verification.get('gate')}，"
            f"阈值 {verification.get('min_verified_ratio')}）"
        )
        lines.append(
            f"- 明细：完全一致 {summary.get('verified', 0)}；部分一致 {summary.get('partial', 0)}；"
            f"冲突 {summary.get('mismatch', 0)}；不可解析 {summary.get('not_found', 0)}；"
            f"无 DOI {summary.get('no_doi', 0)}；未运行 {summary.get('not_run', 0)}"
        )
        retracted = [item for item in verification.get("items") or [] if item.get("retracted")]
        if retracted:
            lines.append("- **已撤稿（必须处理）**：")
            for item in retracted:
                lines.append(f"  - {item.get('title')}（{item.get('doi') or '无 DOI'}）")
        suggestions = [
            item for item in verification.get("items") or [] if item.get("suggested_doi")
        ]
        if suggestions:
            lines.append("- DOI 候选（未自动写入，需人工确认）：")
            for item in suggestions[:10]:
                lines.append(
                    f"  - {item.get('title')[:80]} → {item['suggested_doi']} "
                    f"(相似度 {item.get('suggested_similarity')})"
                )
        lines.append("")

    # -- 5 写作骨架 --------------------------------------------------------
    if outline:
        lines += ["## 5 正文写作骨架", ""]
        lines.append("| 章节 | 目标字数 | 引用预算 | 必须覆盖 |")
        lines.append("| --- | --- | --- | --- |")
        for section in outline.get("sections") or []:
            lines.append(
                f"| {_md_cell(section.get('title'))} | {section.get('target_words')} | "
                f"{section.get('citation_budget')} | {_md_cell('；'.join(section.get('must_cover') or []))} |"
            )
        lines.append("")

    # -- 6 评审/QA --------------------------------------------------------
    if panel:
        lines += ["## 6 评审聚合", ""]
        lines.append(
            f"- 决定：{panel.get('decision')}（加权总分 {panel.get('weighted_total')}）"
        )
        if panel.get("degraded"):
            lines.append(f"- 降级：{', '.join(panel['degraded'])}")
        lines.append("")
    if qa:
        lines += ["## 6 稿件检查", ""]
        lines.append(f"- 结论：{qa.get('verdict')}（得分 {qa.get('score')}）")
        lines.append(f"- 错误 {qa.get('errors')} / 警告 {qa.get('warnings')}")
        lines.append("")

    # -- 7 局限 ------------------------------------------------------------
    lines += ["## 7 局限", ""]
    limitations = _limitations(state, works, search, screening, verification)
    if limitations:
        for item in limitations:
            lines.append(f"- {item}")
    else:
        lines.append("- 未发现结构性局限（仍需人工复核检索覆盖与筛选判断）。")
    lines.append("")

    # -- 参考文献 ----------------------------------------------------------
    if references and references.get("entries"):
        lines.append(reference_markdown(references, heading="参考文献"))
        lines.append("")

    lines += [
        "---",
        "",
        "本报告由 ATRI-X 研究引擎按固定规则从落盘产物生成；改动任何数字都应重跑对应阶段，"
        "不要在报告里手工修订数字。",
    ]
    return "\n".join(lines).rstrip() + "\n"
