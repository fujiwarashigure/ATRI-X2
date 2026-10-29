"""九个硬工具的实现（每个对应一个 AgentSkill）。

约定：每个 ``tool_*`` 接收 argparse 命名空间，返回

    {"data": {...}, "artifacts": [...], "degraded": [...], "warnings": [...]}

CLI 负责包信封、打印和退出码。这样"业务"与"协议"分开，测试可以直接
调 ``tool_search(ns)`` 而不必走子进程。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import time
from typing import Any

from . import VERSION, __name__ as _pkg
from .cite import STYLES, assign_cite_keys, build_references
from .config import (
    DEFAULT_SETTINGS,
    ResearchSettings,
    load_settings,
    paths,
    resolve_run_id,
    list_runs,
    safe_run_id,
)
from .dedupe import dedupe
from .errors import (
    ARG_INVALID,
    ARTIFACT_MISSING,
    DEPENDENCY_MISSING,
    ENV_INVALID,
    GATE_FAILED,
    ResearchError,
)
from .exporter import export_document
from .http import HttpClient
from .markdown_lite import parse as parse_markdown
from .models import Work, new_run_state, validate_run_state
from .outline import build_outline, outline_markdown
from .panel import DEFAULT_RUBRIC, aggregate, panel_markdown, review_template
from .qa import check_draft, qa_markdown
from .report import build_report_markdown
from .screen import DEFAULT_CRITERIA, prisma_markdown, run_screening
from .sources import describe_sources, resolve_sources, search_all
from .store import RunStore, atomic_write_json, now_iso
from .verify import verify_library

__all__ = [
    "TOOL_HANDLERS",
    "tool_cite",
    "tool_doctor",
    "tool_export",
    "tool_panel",
    "tool_plan",
    "tool_screen",
    "tool_search",
    "tool_verify",
    "tool_write",
]

from .runtime import (
    MARKER_RE as _MARKER_RE,
    as_bool as _as_bool,
    as_list as _as_list,
    draft_path as _draft_path,
    http_client as _http,
    included_works as _included_works,
    json_arg as _json_arg,
    library as _library,
    next_steps as _next_steps,
    open_run as _open_run,
    result as _result,
    roots as _roots,
    run_id_for as _run_id_for,
    screening as _screening,
    settings as _settings,
    word_count as _word_count,
    works as _works,
)


# ---------------------------------------------------------------------------
# 1. plan
# ---------------------------------------------------------------------------


def tool_plan(args: Any) -> dict[str, Any]:
    action = (getattr(args, "action", "") or "status").strip().lower()
    settings = _settings()
    if action == "init":
        title = (getattr(args, "title", "") or "").strip()
        question = (getattr(args, "question", "") or "").strip()
        if not title:
            raise ResearchError(ARG_INVALID, "init 需要 title", hint="给出一句话题目。")
        if not question:
            raise ResearchError(
                ARG_INVALID, "init 需要 question", hint="给出可回答的研究问题（一句话）。"
            )
        language = (getattr(args, "language", "") or "zh").strip().lower()
        if language not in ("zh", "en"):
            raise ResearchError(ARG_INVALID, f"language 只能是 zh/en，收到 {language!r}")
        style = (getattr(args, "style", "") or settings.section("cite").get("default_style") or "gbt7714").strip().lower()
        if style not in STYLES:
            raise ResearchError(ARG_INVALID, f"style 只能是 {', '.join(STYLES)}")
        doc_type = (getattr(args, "doc_type", "") or "review").strip().lower()
        if doc_type not in ("review", "empirical", "proposal"):
            raise ResearchError(ARG_INVALID, "doc_type 只能是 review/empirical/proposal")
        target_words = int(getattr(args, "target_words", 0) or 6000)
        if not 800 <= target_words <= 60000:
            raise ResearchError(ARG_INVALID, "target_words 需在 800-60000 之间")
        root = _roots()
        run_id = _run_id_for(title)
        run_dir = root.run_dir(run_id)
        store = RunStore.create_at(run_dir, root.runs_dir)
        state = new_run_state(
            run_id=run_id,
            title=title,
            question=question,
            language=language,
            style=style,
            doc_type=doc_type,
            target_words=target_words,
            owner=(getattr(args, "owner", "") or "").strip(),
        )
        store.write("run.json", state)
        criteria = json.loads(json.dumps(DEFAULT_CRITERIA))
        store.write("criteria.json", criteria)
        state["stage"] = "init"
        store.save_state(state)
        return _result(
            {
                "run": run_id,
                "dir": run_dir,
                "record": state,
                "next": [
                    "用 research_search 检索（queries 给 2-5 组中英文关键词）",
                    f"引用样式已定为 {style}，参考文献将按该样式生成",
                ],
            },
            artifacts=[{"path": "run.json", "bytes": os.path.getsize(store.path("run.json"))}],
        )

    if action == "status":
        run_arg = (getattr(args, "run", "") or "").strip()
        if run_arg:
            store = _open_run(run_arg)
            state = validate_run_state(store.load_state())
            artifacts = []
            for name in (
                "search.json",
                "library.json",
                "criteria.json",
                "screening.json",
                "verification.json",
                "references.json",
                "outline.json",
                "draft.md",
                "qa.json",
                "reviews.json",
                "panel.json",
                "report.md",
            ):
                if store.exists(name):
                    path = store.path(name)
                    artifacts.append({"path": name, "bytes": os.path.getsize(path)})
            return _result(
                {
                    "run": state.get("id"),
                    "title": state.get("title"),
                    "stage": state.get("stage"),
                    "counts": state.get("counts") or {},
                    "artifacts": artifacts,
                    "dir": store.run_dir,
                    "next": _next_steps(state, store),
                }
            )
        runs = list_runs()
        return _result(
            {
                "home": paths().home,
                "runs": runs[:20],
                "count": len(runs),
                "hint": "传 run=<id> 查看单次运行的产物明细。",
            }
        )

    if action == "outline":
        store = _open_run(getattr(args, "run", ""))
        state = store.load_state()
        library = _library(store)
        works = _included_works(store, library)
        if not works:
            raise ResearchError(
                ARTIFACT_MISSING,
                "没有可用于大纲的文献",
                hint="先 research_search；若已筛选，检查 screening.json 的 included_keys 是否非空。",
            )
        outline = build_outline(
            state,
            works,
            sections=getattr(args, "sections", None),
            target_words=getattr(args, "target_words", None),
        )
        artifact_list = [store.write("outline.json", outline)]
        artifact_list.append(store.write("outline.md", outline_markdown(outline)))
        state["stage"] = "outline"
        store.save_state(state)
        return _result(
            {
                "run": state.get("id"),
                "doc_type": outline["doc_type"],
                "target_words": outline["target_words"],
                "sections": [
                    {"title": section["title"], "words": section["target_words"], "citations": section["citation_budget"]}
                    for section in outline["sections"]
                ],
                "evidence_count": outline["evidence_count"],
                "citation_total_budget": outline["citation_total_budget"],
                "next": [
                    "按 outline.md 的逐节契约写作，正文引用写成 [@cite_key]",
                    "每次 5000-15000 字符用 research_write action=append 追加",
                    "写完后 research_cite 生成参考文献，再 research_write action=check 质检",
                ],
            },
            artifacts=artifact_list,
        )
    raise ResearchError(ARG_INVALID, f"未知 action: {action}", hint="可用 action: init / status / outline。")


def tool_search(args: Any) -> dict[str, Any]:
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    settings = _settings()
    queries = _as_list(getattr(args, "queries", None)) or _as_list(getattr(args, "query", None))
    if not queries:
        raise ResearchError(ARG_INVALID, "需要 queries", hint="给出 2-5 组关键词，例如 ['retrieval augmented generation', '检索增强生成']")
    sources = resolve_sources(_as_list(getattr(args, "sources", None)) or None)
    search_cfg = settings.section("search")
    limit = int(getattr(args, "limit", 0) or search_cfg.get("per_source_limit") or 20)
    limit = max(1, min(limit, 100))

    criteria = store.read("criteria.json", required=False, default=None) or {}
    include = (criteria or {}).get("include") or {}
    year_from = getattr(args, "year_from", None) or include.get("year_from")
    year_to = getattr(args, "year_to", None) or include.get("year_to")

    client = _http(settings)
    started = time.time()
    works, steps = search_all(
        client,
        queries,
        sources=sources,
        limit=limit,
        year_from=int(year_from) if year_from else None,
        year_to=int(year_to) if year_to else None,
        mailto=settings.mailto,
    )
    deduped, stats = dedupe(works)
    max_items = int(settings.section("limits").get("max_library_items") or 500)
    truncated = 0
    if len(deduped) > max_items:
        truncated = len(deduped) - max_items
        deduped = deduped[:max_items]

    artifact_payload = {
        "schema": "atrix-research/search/1",
        "queries": queries,
        "sources": sources,
        "limit_per_source": limit,
        "year_from": int(year_from) if year_from else None,
        "year_to": int(year_to) if year_to else None,
        "steps": steps,
        "http": client.stats,
        "elapsed_ms": int((time.time() - started) * 1000),
        "retrieved_at": now_iso(),
    }
    library = {
        "schema": "atrix-research/library/1",
        "run": state.get("id"),
        "query": state.get("question"),
        "queries": queries,
        "sources": sources,
        "works": [work.to_dict() for work in deduped],
        "dedupe": stats,
        "built_at": now_iso(),
    }
    artifacts = [store.write("search.json", artifact_payload), store.write("library.json", library)]
    state["counts"] = {
        "identified": stats.get("input", 0),
        "deduped": len(deduped),
        "duplicates_removed": stats.get("merged", 0),
    }
    state["stage"] = "search"
    store.save_state(state)

    warnings: list[str] = []
    degraded: list[str] = []
    failed = [step for step in steps if not step.get("ok")]
    if failed:
        degraded.append(
            "部分检索源失败: "
            + ", ".join(f"{step['source']}({step['query'][:20]})" for step in failed[:5])
        )
    possible = stats.get("possible_duplicates") or []
    if possible:
        warnings.append(
            f"有 {len(possible)} 组标题高度相似但证据不足未合并，请人工确认（见 library.json 的 dedupe.possible_duplicates）"
        )
    if truncated:
        warnings.append(f"文献库超过上限，已截断 {truncated} 条")
    if not deduped:
        warnings.append("本次检索没有命中任何文献；换更宽的关键词或放宽年份后重试")
    return _result(
        {
            "run": state.get("id"),
            "queries": queries,
            "sources": sources,
            "identified": stats.get("input", 0),
            "deduped": len(deduped),
            "duplicates_removed": stats.get("merged", 0),
            "by_source": _by_source(deduped),
            "steps": [
                {"source": step["source"], "ok": step["ok"], "count": step["count"], "error": step.get("error", {}).get("code", "")}
                for step in steps
            ],
            "next": ["research_screen stage=title_abstract（可先用 criteria 指定年份/类型）"],
        },
        artifacts=artifacts,
        degraded=degraded,
        warnings=warnings,
    )


def _by_source(works: list[Work]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for work in works:
        for source in work.source_hits or ["unknown"]:
            counts[source] = counts.get(source, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 3. screen
# ---------------------------------------------------------------------------


def tool_screen(args: Any) -> dict[str, Any]:
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    library = _library(store)
    works = _works(library)
    stage = (getattr(args, "stage", "") or "title_abstract").strip().lower()
    if stage not in ("title_abstract", "full_text"):
        raise ResearchError(ARG_INVALID, "stage 只能是 title_abstract / full_text")

    criteria_arg = _json_arg(getattr(args, "criteria", None), expect=dict)
    criteria = criteria_arg or store.read("criteria.json", required=False, default=None)
    if not criteria:
        criteria = json.loads(json.dumps(DEFAULT_CRITERIA))
    store.write("criteria.json", criteria)

    existing_manual = store.read("decisions.json", required=False, default=None) or {}
    if not isinstance(existing_manual, dict):
        existing_manual = {}
    manual_arg = _json_arg(getattr(args, "decisions", None), expect=dict) or {}
    manual = {**existing_manual, **manual_arg}

    degraded: list[str] = []
    warnings: list[str] = []
    selected = works
    if stage == "full_text":
        screening_ta = _screening(store)
        if screening_ta and screening_ta.get("stage") == "title_abstract":
            included = set(screening_ta.get("included_keys") or [])
            selected = [work for work in works if work.key in included]
        if not selected:
            raise ResearchError(
                ARTIFACT_MISSING,
                "还没有标题摘要阶段的纳入文献",
                hint="先 research_screen stage=title_abstract 完成第一轮筛选。",
            )
        pdf_dir = (getattr(args, "pdf_dir", "") or "").strip()
        fulltext_dir = store.directory("fulltext")
        if pdf_dir:
            from . import pdftext

            key_for = {f"{_safe_name(work.key)}.txt": work.key for work in selected}
            try:
                extracted = pdftext.extract_directory(
                    pdf_dir,
                    fulltext_dir,
                    max_bytes=int(_settings().section("screen").get("max_fulltext_bytes") or 8 * 1024 * 1024),
                    key_for=key_for,
                )
                store.write("fulltext.json", extracted)
                if extracted.get("skipped"):
                    warnings.append(
                        f"{len(extracted['skipped'])} 个 PDF 未抽取成功（见 fulltext.json 的 skipped）"
                    )
            except ResearchError as exc:
                degraded.append(f"PDF 抽取不可用: {exc.message}")
        keyword_rules = _json_arg(getattr(args, "fulltext_keywords", None), expect=dict) or {}
        include_words = [str(item).lower() for item in keyword_rules.get("include") or []]
        exclude_words = [str(item).lower() for item in keyword_rules.get("exclude") or []]
        if include_words or exclude_words:
            for work in selected:
                if work.key in manual:
                    continue
                text_path = os.path.join(fulltext_dir, f"{_safe_name(work.key)}.txt")
                if not os.path.isfile(text_path):
                    continue
                with open(text_path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read().lower()
                if include_words and not any(word in text for word in include_words):
                    manual[work.key] = {"decision": "exclude", "reason": "全文未命中纳入关键词（自动）"}
                elif any(word in text for word in exclude_words):
                    manual[work.key] = {"decision": "exclude", "reason": "全文命中排除关键词（自动）"}
                else:
                    manual[work.key] = {"decision": "include", "reason": "全文命中纳入关键词（自动）"}
            if manual:
                warnings.append("全文阶段含自动关键词判定，属于辅助初筛，最终判断仍需人工确认")
                degraded.append("FULLTEXT_AUTO_SCREENING: 部分全文判定由关键词规则给出")

    duplicates_removed = int((library.get("dedupe") or {}).get("merged") or 0)
    identified = int((library.get("dedupe") or {}).get("input") or len(works))
    result = run_screening(
        selected,
        criteria,
        manual=manual,
        stage=stage,
        duplicates_removed=duplicates_removed,
        identified=identified,
    )
    result["run"] = state.get("id")
    result["decided_at"] = now_iso()
    artifacts = [store.write("screening.json", result)]
    artifacts.append(store.write("decisions.json", manual))
    artifacts.append(store.write("prisma.md", prisma_markdown(result["counts"])))
    state["stage"] = "screen"
    state["counts"] = {
        **(state.get("counts") or {}),
        "screened": result["counts"]["screened"],
        "included": result["counts"]["included"],
        "excluded": result["counts"]["excluded"],
    }
    store.save_state(state)

    counts = result["counts"]
    next_steps = ["research_verify 核验纳入文献的可核查性"]
    if result["gate"] == "FAIL":
        warnings.append(
            f"全文阶段仍有 {counts['excluded_pending']} 条未判定；在 decisions 里给出 include/exclude 后再跑一次"
        )
        next_steps = ["补齐 decisions（逐条 include/exclude + reason）后重跑 research_screen stage=full_text"]
    return _result(
        {
            "run": state.get("id"),
            "stage": stage,
            "counts": counts,
            "gate": result["gate"],
            "pending": counts.get("excluded_pending", 0),
            "next": next_steps,
        },
        artifacts=artifacts,
        degraded=degraded,
        warnings=warnings,
    )


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:80] or "doc"


# ---------------------------------------------------------------------------
# 4. verify
# ---------------------------------------------------------------------------


def tool_verify(args: Any) -> dict[str, Any]:
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    settings = _settings()
    library = _library(store)
    works = _included_works(store, library)
    if not works:
        raise ResearchError(ARTIFACT_MISSING, "没有可核验的文献", hint="先完成 screening 或确认 library.json 非空。")
    cfg = settings.section("verify")
    min_ratio = float(getattr(args, "min_ratio", 0) or cfg.get("min_verified_ratio") or 0.9)
    suggestion = _as_bool(getattr(args, "suggest_doi", None), True)
    client = _http(settings)
    result = verify_library(
        client,
        works,
        title_pass=float(cfg.get("title_similarity_pass") or 0.9),
        title_warn=float(cfg.get("title_similarity_warn") or 0.75),
        min_verified_ratio=min_ratio,
        limit=int(getattr(args, "limit", 0) or 0) or None,
    )
    result["run"] = state.get("id")
    result["verified_at"] = now_iso()
    result["http"] = client.stats
    artifacts = [store.write("verification.json", result)]
    state["stage"] = "verify"
    state["counts"] = {**(state.get("counts") or {}), "verified_ratio": result["verified_ratio"]}
    store.save_state(state)

    degraded: list[str] = []
    warnings: list[str] = []
    if result["gate"] == "FAIL":
        degraded.append("CITATION_VERIFICATION_FAILED: 全部条目都未能核验（网络或数据源问题）")
    elif result["gate"] == "WARN":
        warnings.append(
            f"核验通过率 {result['verified_ratio']} 低于阈值 {min_ratio}；写作时应只使用 VERIFIED/PARTIAL 的文献下结论"
        )
    summary = result["summary"]
    if summary.get("mismatch"):
        warnings.append(f"{summary['mismatch']} 条 DOI 与元数据冲突，引用前必须人工确认")
    if summary.get("not_found"):
        warnings.append(f"{summary['not_found']} 条 DOI 无法解析（可能已失效）")
    if summary.get("no_doi"):
        warnings.append(f"{summary['no_doi']} 条缺 DOI（若已给候选 DOI 需人工确认后补录）")
    return _result(
        {
            "run": state.get("id"),
            "gate": result["gate"],
            "verified_ratio": result["verified_ratio"],
            "summary": summary,
            "next": [
                "research_cite 生成参考文献",
                "research_plan action=outline 生成写作大纲",
            ],
        },
        artifacts=artifacts,
        degraded=degraded,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 5. cite
# ---------------------------------------------------------------------------


def tool_cite(args: Any) -> dict[str, Any]:
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    library = _library(store)
    works = _included_works(store, library)
    if not works:
        raise ResearchError(ARTIFACT_MISSING, "没有可格式化的文献", hint="先检索并筛选。")
    style = (getattr(args, "style", "") or state.get("style") or "gbt7714").strip().lower()
    if style not in STYLES:
        raise ResearchError(ARG_INVALID, f"style 只能是 {', '.join(STYLES)}")

    order_keys: list[str] = []
    cited: set[str] = set()
    if store.exists("draft.md"):
        draft = store.read("draft.md")
        for match in _MARKER_RE.finditer(draft):
            cited.add(match.group(1))
    # [@key] 标记的是 cite_key，需要反查到 work.key 供排序使用
    assign_cite_keys(works)
    by_cite_key = {work.cite_key: work.key for work in works}
    order_keys = [by_cite_key[key] for key in cited if key in by_cite_key]
    cited_work_keys = set(order_keys)

    references = build_references(
        works,
        style=style,
        order_keys=order_keys if style == "gbt7714" else None,
        cited_keys=cited_work_keys if cited_work_keys else None,
    )
    references["run"] = state.get("id")
    references["generated_at"] = now_iso()
    artifact_list = [store.write("references.json", references)]
    from .cite import reference_markdown

    artifact_list.append(store.write("references.md", reference_markdown(references)))
    state["style"] = style
    state["stage"] = "cite" if state.get("stage") in ("verify", "screen", "") else state.get("stage")
    store.save_state(state)

    warnings: list[str] = []
    if cited and references["unused_in_text"]:
        warnings.append(f"{references['unused_in_text']} 条参考文献未被正文引用（质检会再提示）")
    return _result(
        {
            "run": state.get("id"),
            "style": style,
            "count": references["count"],
            "unused_in_text": references["unused_in_text"],
            "sample": [entry["text"] for entry in references["entries"][:3]],
            "next": [
                "写作时正文引用写 [@cite_key]（键见 references.json 的 by_cite_key）",
                "research_plan action=outline 生成大纲（若还没生成）",
            ],
        },
        artifacts=artifact_list,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 6. write
# ---------------------------------------------------------------------------


def tool_write(args: Any) -> dict[str, Any]:
    action = (getattr(args, "action", "") or "").strip().lower()
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    path = _draft_path(store)
    content = getattr(args, "content", "") or ""

    if action in ("save", "append"):
        if not content.strip():
            raise ResearchError(ARG_INVALID, f"{action} 需要 content", hint="传入 Markdown 正文片段。")
        if action == "append" and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
            merged = existing.rstrip() + "\n\n" + content.strip() + "\n"
        else:
            merged = content.strip() + "\n"
        store.write("draft.md", merged)
        state["stage"] = "writing"
        state["counts"] = {**(state.get("counts") or {}), "draft_words": _word_count(merged)}
        store.save_state(state)
        sections = [block.text for block in parse_markdown(merged) if block.kind == "heading"]
        return _result(
            {
                "run": state.get("id"),
                "action": action,
                "words": _word_count(merged),
                "sections": len([title for title in sections if title]),
                "chars": len(merged),
                "next": ["继续 append 下一节", "全部写完后 research_write action=check 质检"],
            },
            artifacts=[{"path": "draft.md", "bytes": os.path.getsize(path)}],
        )

    if action == "status":
        if not os.path.isfile(path):
            raise ResearchError(ARTIFACT_MISSING, "还没有稿件", hint="用 action=append 写入第一节。")
        draft = store.read("draft.md")
        blocks = parse_markdown(draft)
        headings = [block for block in blocks if block.kind == "heading"]
        return _result(
            {
                "run": state.get("id"),
                "words": _word_count(draft),
                "chars": len(draft),
                "headings": [f"{'#' * block.level} {block.text}" for block in headings],
                "citation_markers": len(_MARKER_RE.findall(draft)),
            }
        )

    if action == "check":
        if not os.path.isfile(path):
            raise ResearchError(ARTIFACT_MISSING, "还没有稿件", hint="先用 action=append 写入正文。")
        draft = store.read("draft.md")
        references = store.read("references.json", required=False, default=None)
        if not references or not references.get("entries"):
            raise ResearchError(
                ARTIFACT_MISSING,
                "缺少参考文献表",
                hint="先 research_cite 生成 references.json，再跑质检。",
            )
        outline = store.read("outline.json", required=False, default=None)
        qa = check_draft(
            draft,
            references,
            outline,
            style=(references.get("style") or state.get("style") or "gbt7714"),
            min_section_citations=getattr(args, "min_section_citations", None),
        )
        qa["run"] = state.get("id")
        qa["checked_at"] = now_iso()
        blocked = int(qa["errors"])
        artifact_list = [store.write("qa.json", qa)]
        artifact_list.append(store.write("qa.md", qa_markdown(qa)))
        state["stage"] = "check"
        state["counts"] = {**(state.get("counts") or {}), "qa_score": qa["score"], "qa_errors": blocked}
        store.save_state(state)
        degraded = ["QA_FAILED"] if qa["verdict"] == "FAIL" else []
        warnings = [
            f"{qa['errors']} 个错误必须修复后才适合作为交付物"
        ] if qa["errors"] else []
        return _result(
            {
                "run": state.get("id"),
                "verdict": qa["verdict"],
                "score": qa["score"],
                "errors": qa["errors"],
                "warnings": qa["warnings"],
                "stats": qa["stats"],
                "issues": qa["issues"][:20],
                "next": (
                    ["按 issues 的 fix 逐条修改，然后重新 check"]
                    if qa["verdict"] != "PASS"
                    else ["research_export action=all 生成报告与导出文件"]
                ),
            },
            artifacts=artifact_list,
            degraded=degraded,
            warnings=warnings,
        )
    raise ResearchError(ARG_INVALID, f"未知 action: {action}", hint="可用 action: save / append / check / status。")


# ---------------------------------------------------------------------------
# 7. panel
# ---------------------------------------------------------------------------


def tool_panel(args: Any) -> dict[str, Any]:
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    reviews = _json_arg(getattr(args, "reviews", None), expect=dict)
    if not reviews:
        reviews = store.read("reviews.json", required=False, default=None)
    if not reviews:
        template = review_template(int(_settings().section("panel").get("scale") or 5))
        store.write("reviews.json", template)
        return _result(
            {
                "run": state.get("id"),
                "template_written": "reviews.json",
                "instruction": (
                    "按 reviews.json 的结构填写评审：每位评审给 5 个维度 1-5 分，"
                    "independence 填 independent 或 same-model，然后再次调用本工具聚合。"
                ),
                "reviews": template,
                "next": ["填写 reviews.json 后重新调用 research_panel"],
            }
        )
    rubric = json.loads(json.dumps(DEFAULT_RUBRIC))
    panel_cfg = _settings().section("panel")
    if isinstance(panel_cfg.get("weights"), dict):
        for name, weight in panel_cfg["weights"].items():
            rubric["dimensions"].setdefault(name, {"label": name, "weight": weight})["weight"] = float(weight)
    if isinstance(panel_cfg.get("thresholds"), dict):
        rubric["thresholds"].update({key: float(value) for key, value in panel_cfg["thresholds"].items()})
    if panel_cfg.get("scale"):
        rubric["scale"] = int(panel_cfg["scale"])
    if panel_cfg.get("disagreement_warn") is not None:
        rubric["disagreement_warn"] = float(panel_cfg["disagreement_warn"])

    result = aggregate(reviews, rubric=rubric)
    result["run"] = state.get("id")
    result["aggregated_at"] = now_iso()
    artifact_list = [store.write("panel.json", result)]
    artifact_list.append(store.write("panel.md", panel_markdown(result)))
    state["stage"] = "review"
    store.save_state(state)

    degraded = list(result.get("degraded") or [])
    warnings: list[str] = []
    if result.get("needs_discussion"):
        warnings.append(
            "分歧较大的维度: "
            + ", ".join(str(name) for name in result["needs_discussion"])
            + "（同一模型多份意见不构成独立评审）"
        )
    return _result(
        {
            "run": state.get("id"),
            "reviewers": result["reviewers"],
            "decision": result["decision"],
            "weighted_total": result["weighted_total"],
            "dimensions": result["dimensions"],
            "agreement": result["agreement"],
            "degraded": result["degraded"],
            "required_fixes": result["required_fixes"],
            "next": ["research_export action=all 生成报告与导出文件"],
        },
        artifacts=artifact_list,
        degraded=degraded,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 8. export
# ---------------------------------------------------------------------------


def _build_report(store: RunStore, state: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    library = store.read("library.json", required=False, default=None)
    if not library or not library.get("works"):
        raise ResearchError(ARTIFACT_MISSING, "缺少文献库，无法装配报告", hint="先 research_search。")
    works = _included_works(store, library)
    report = build_report_markdown(
        state,
        works,
        search=store.read("search.json", required=False, default=None),
        screening=store.read("screening.json", required=False, default=None),
        verification=store.read("verification.json", required=False, default=None),
        references=store.read("references.json", required=False, default=None),
        outline=store.read("outline.json", required=False, default=None),
        panel=store.read("panel.json", required=False, default=None),
        qa=store.read("qa.json", required=False, default=None),
        engine_version=VERSION,
        generated_at=now_iso(),
    )
    artifacts = [store.write("report.md", report)]
    degraded: list[str] = []
    verification = store.read("verification.json", required=False, default=None)
    if verification and verification.get("gate") != "PASS":
        degraded.append(f"REPORT_WITH_{verification.get('gate')}: 报告生成时引文核验未完全通过")
    return report, artifacts, degraded


def tool_export(args: Any) -> dict[str, Any]:
    action = (getattr(args, "action", "") or "report").strip().lower()
    store = _open_run(getattr(args, "run", ""))
    state = store.load_state()
    degraded: list[str] = []
    warnings: list[str] = []
    artifacts: list[dict[str, Any]] = []
    data: dict[str, Any] = {"run": state.get("id"), "action": action}

    if action in ("report", "all"):
        report, report_artifacts, report_degraded = _build_report(store, state)
        artifacts.extend(report_artifacts)
        degraded.extend(report_degraded)
        data["report"] = {
            "path": "report.md",
            "lines": len(report.splitlines()),
            "chars": len(report),
        }
        state["stage"] = "report"

    if action == "pdf":
        pdf_dir = (getattr(args, "pdf_dir", "") or "").strip()
        if not pdf_dir:
            raise ResearchError(ARG_INVALID, "pdf 动作需要 pdf_dir", hint="给出存放 PDF 的目录绝对路径。")
        from . import pdftext

        result = pdftext.extract_directory(
            pdf_dir,
            store.directory("fulltext"),
            max_bytes=int(_settings().section("screen").get("max_fulltext_bytes") or 8 * 1024 * 1024),
        )
        artifacts.append(store.write("fulltext.json", result))
        data["pdf"] = {"extracted": result["count"], "skipped": len(result["skipped"])}
        if result["skipped"]:
            warnings.append(f"{len(result['skipped'])} 个 PDF 未能抽取（见 fulltext.json）")

    if action in ("export", "all"):
        formats = _as_list(getattr(args, "formats", None)) or None
        exports_dir = store.directory("exports")
        targets: list[tuple[str, str]] = []
        if store.exists("draft.md"):
            targets.append(("draft", store.read("draft.md")))
        if store.exists("report.md") or action == "all":
            if not store.exists("report.md"):
                report, report_artifacts, report_degraded = _build_report(store, state)
                artifacts.extend(report_artifacts)
                degraded.extend(report_degraded)
            targets.append(("report", store.read("report.md")))
        if not targets:
            raise ResearchError(
                ARTIFACT_MISSING,
                "没有可导出的内容（既无 draft.md 也无 report.md）",
                hint="先写作或先 action=report 生成报告。",
            )
        export_results: list[dict[str, Any]] = []
        for name, markdown_text in targets:
            outcome = export_document(
                markdown_text,
                os.path.join(exports_dir, name),
                formats=formats,
                title=state.get("title") or name,
            )
            export_results.append({"name": name, "results": outcome["results"]})
            degraded.extend(f"{name}: {item}" for item in outcome["degraded"])
            for item in outcome["results"]:
                if item["status"] == "ok":
                    artifacts.append(
                        {
                            "path": f"exports/{name}.{item['format']}",
                            "bytes": item["bytes"],
                        }
                    )
        data["exports"] = export_results
        state["stage"] = "export"

    store.save_state(state)
    if not artifacts:
        raise ResearchError(ARG_INVALID, f"action={action} 没有产生任何产物", hint="可用 action: report / export / pdf / all。")
    return _result(
        data,
        artifacts=artifacts,
        degraded=degraded,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 9. doctor
# ---------------------------------------------------------------------------


def _network_probe(client: HttpClient) -> list[dict[str, Any]]:
    probes = [
        ("openalex", "https://api.openalex.org/works?per_page=1&search=test"),
        ("crossref", "https://api.crossref.org/works?rows=1&query=test"),
        ("arxiv", "http://export.arxiv.org/api/query?search_query=all:test&max_results=1"),
        ("pubmed", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=test&retmode=json&retmax=1"),
    ]
    out: list[dict[str, Any]] = []
    for name, url in probes:
        started = time.time()
        try:
            client.get(url)
            out.append({"source": name, "reachable": True, "ms": int((time.time() - started) * 1000)})
        except ResearchError as exc:
            out.append(
                {
                    "source": name,
                    "reachable": False,
                    "ms": int((time.time() - started) * 1000),
                    "error": exc.to_dict(),
                }
            )
    return out


def _module_present(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def tool_doctor(args: Any) -> dict[str, Any]:
    action = (getattr(args, "action", "") or "env").strip().lower()
    settings = _settings()
    if action == "status":
        runs = list_runs()
        latest = runs[0] if runs else None
        detail: dict[str, Any] = {}
        if latest:
            store = RunStore(os.path.join(paths().runs_dir, latest["id"]), paths().runs_dir)
            for name in ("library.json", "screening.json", "verification.json", "references.json"):
                if store.exists(name):
                    payload = store.read(name)
                    if name == "library.json":
                        detail[name] = {"works": len(payload.get("works") or [])}
                    elif name == "screening.json":
                        detail[name] = payload.get("counts") or {}
                    else:
                        detail[name] = {"gate": payload.get("gate"), "count": payload.get("count")}
        return _result(
            {
                "home": paths().home,
                "runs": [{k: run[k] for k in ("id", "title", "stage", "updated_at")} for run in runs[:20]],
                "count": len(runs),
                "latest": latest["id"] if latest else "",
                "latest_artifacts": detail,
            }
        )

    if action == "selftest":
        checks = _selftest()
        passed = sum(1 for item in checks if item["ok"])
        return _result(
            {
                "ok": passed == len(checks),
                "passed": passed,
                "total": len(checks),
                "checks": checks,
            },
            degraded=[] if passed == len(checks) else ["SELFTEST_FAILED"],
        )

    if action == "parity":
        from .parity import run_parity

        parity = run_parity()
        return _result(
            parity,
            degraded=[] if parity["verdict"] == "PASS" else ["PARITY_FAILED"],
            warnings=(
                []
                if parity["verdict"] == "PASS"
                else ["有断言失败：说明引擎里混进了模型相关的判断，换模型可能改结论"]
            ),
        )

    if action != "env":
        raise ResearchError(
            ARG_INVALID,
            f"未知 action: {action}",
            hint="可用 action: env / status / selftest / parity。",
        )

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, *, critical: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "critical": critical})

    import platform
    import sys

    check("python", sys.version_info >= (3, 10), f"{sys.version.split()[0]} ({platform.platform()})", critical=True)
    check("engine", True, f"atrix-research {VERSION}", critical=True)
    root = paths().ensure()
    try:
        probe = os.path.join(root.home, ".write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        check("home_writable", True, root.home, critical=True)
    except OSError as exc:
        check("home_writable", False, f"{root.home}: {exc}", critical=True)
    try:
        settings = load_settings()
        check("settings", True, settings.origin)
    except ResearchError as exc:
        check("settings", False, exc.message, critical=True)
    optional = {
        "docx (python-docx 增强)": "docx",
        "pdf (reportlab)": "reportlab",
        "pdf 抽取 (pypdf)": "pypdf",
    }
    optional_report: dict[str, bool] = {}
    for label, module in optional.items():
        present = _module_present(module)
        optional_report[module] = present
        if module == "docx":
            note = "已安装（排版增强）" if present else "缺失（用零依赖 DOCX 写入器，导出不受影响）"
        else:
            note = "已安装" if present else "缺失（对应导出/抽取功能会降级）"
        check(f"optional:{label}", present, note)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    venv_exists = os.path.isdir(root.venv_dir)
    check("venv", in_venv or venv_exists, f"当前解释器 {'在' if in_venv else '不在'} venv；安装目录 {root.venv_dir}")

    network = _as_bool(getattr(args, "network", None), True)
    probes: list[dict[str, Any]] = []
    if network:
        probes = _network_probe(_http(settings))
        reachable = [probe for probe in probes if probe["reachable"]]
        check(
            "network",
            bool(reachable),
            f"{len(reachable)}/{len(probes)} 个数据源可达",
        )
    cache_files = 0
    cache_bytes = 0
    if os.path.isdir(root.http_cache_dir):
        for name in os.listdir(root.http_cache_dir):
            path = os.path.join(root.http_cache_dir, name)
            if os.path.isfile(path):
                cache_files += 1
                cache_bytes += os.path.getsize(path)
    check("cache", True, f"{cache_files} 个缓存文件 / {cache_bytes} 字节")
    runs = list_runs()
    check("runs", True, f"{len(runs)} 个运行记录")

    critical_ok = all(item["ok"] for item in checks if item["critical"])
    return _result(
        {
            "ok": critical_ok,
            "checks": checks,
            "network": probes,
            "optional_dependencies": optional_report,
            "sources": describe_sources(),
            "paths": {
                "home": root.home,
                "runs": root.runs_dir,
                "cache": root.cache_dir,
                "venv": root.venv_dir,
            },
            "runs": len(runs),
        },
        degraded=[] if critical_ok else ["ENV_CHECK_FAILED"],
        warnings=[] if all(optional_report.values()) else ["部分可选依赖缺失，相关导出/抽取会降级（见 checks）"],
    )


def _selftest() -> list[dict[str, Any]]:
    """离线确定性自检：不联网，验证五个核心算法的关键性质。"""
    checks: list[dict[str, Any]] = []

    def check(name: str, fn: Any) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    def dedupe_case() -> tuple[bool, str]:
        left = Work(title="Retrieval-Augmented Generation for Knowledge-Intensive NLP", doi="10.1/abc", year=2020)
        right = Work(title="Retrieval augmented generation for knowledge intensive NLP", year=2020)
        merged, stats = dedupe([left, right])
        return len(merged) == 1, f"merged={len(merged)} stats={stats['merged']}"

    def screen_case() -> tuple[bool, str]:
        work = Work(title="Old paper", year=1990, language="en", type="article")
        criteria = json.loads(json.dumps(DEFAULT_CRITERIA))
        criteria["include"]["year_from"] = 2015
        outcome = {"decision": ""}
        from .screen import decide

        outcome = decide(work, criteria)
        return outcome["decision"] == "exclude" and outcome["code"] == "E-YEAR", f"{outcome}"

    def cite_case() -> tuple[bool, str]:
        work_a = Work(
            title="A study of things",
            authors=[__import__("atrix_research.models", fromlist=["Author"]).Author(family="Smith", given="John A.")],
            year=2021,
            venue="Journal of Things",
            volume="3",
            issue="2",
            pages="10-20",
            doi="10.1000/xyz",
            type="article",
        )
        apa = build_references([Work.from_dict(work_a.to_dict())], style="apa7")["entries"][0]["text"]
        gbt = build_references([Work.from_dict(work_a.to_dict())], style="gbt7714")["entries"][0]["text"]
        ok = "Smith" in apa and "2021" in apa and "https://doi.org/10.1000/xyz" in apa
        ok = ok and gbt.startswith("[1]") and "[J]" in gbt
        return ok, f"apa={apa[:60]} / gbt={gbt[:60]}"

    def qa_case() -> tuple[bool, str]:
        refs = {"style": "gbt7714", "entries": [{"cite_key": "smith2021", "index": 1, "text": "[1] SMITH J. A study[J]. J, 2021. DOI: 10.1/x."}]}
        report = check_draft("## 1 引言\n\n这是没有标记的断言 [@nokey]。", refs)
        codes = {issue["code"] for issue in report["issues"]}
        return "CITATION_UNKNOWN_KEY" in codes and report["verdict"] == "FAIL", f"codes={sorted(codes)}"

    def panel_case() -> tuple[bool, str]:
        payload = {
            "reviews": [
                {"reviewer": "R1", "independence": "independent", "scores": {"originality": 5, "rigor": 5, "clarity": 5, "significance": 5, "ethics": 5}},
                {"reviewer": "R2", "independence": "independent", "scores": {"originality": 4, "rigor": 5, "clarity": 4, "significance": 4, "ethics": 5}},
            ]
        }
        result = aggregate(payload)
        return result["decision"] == "accept" and not result["degraded"], f"decision={result['decision']} total={result['weighted_total']}"

    def markdown_case() -> tuple[bool, str]:
        blocks = parse_markdown("# T\n\n段落 **粗**\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- x\n- y")
        kinds = [block.kind for block in blocks]
        return kinds == ["heading", "para", "table", "bullet"], f"kinds={kinds}"

    check("dedupe.merge_doi_and_title", dedupe_case)
    check("screen.year_rule", screen_case)
    check("cite.apa_and_gbt", cite_case)
    check("qa.unknown_citation_key", qa_case)
    check("panel.accept_decision", panel_case)
    check("markdown.parse_blocks", markdown_case)
    return checks


TOOL_HANDLERS = {
    "plan": tool_plan,
    "search": tool_search,
    "screen": tool_screen,
    "verify": tool_verify,
    "cite": tool_cite,
    "write": tool_write,
    "panel": tool_panel,
    "export": tool_export,
    "doctor": tool_doctor,
}
