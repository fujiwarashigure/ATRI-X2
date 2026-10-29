"""``audit`` 硬工具：确定性评审打分。

评审环节的风险同样是"换模型就换标准"：同一篇稿件，弱模型说"结构完整、证据充分"，
强模型说"方法有缺口"。这里把**可度量的部分**（证据覆盖、引文可靠性、方法可复现性、
结构完整度、表述克制、交付完整度）用固定公式算成 0-100 分，并列出可执行的缺陷清单。

不可度量的部分（原创性、伦理判断）**不做假**：明确列为 ``unmeasured``，交给
``research_panel`` 的人工/模型评审。这样"换模型"只影响那两项的主观判断，
不影响六项硬指标——评审结论至少有一半在任何模型下是同一份。
"""

from __future__ import annotations

import os
from typing import Any

from .errors import ARTIFACT_MISSING, ResearchError
from .qa import BANNED_PHRASES
from .runtime import MARKER_RE, open_run, result, word_count
from .store import now_iso

__all__ = ["DIMENSIONS", "audit_markdown", "evaluate", "tool_audit"]

#: 维度定义：权重之和为 1；``source`` 说明分从哪个产物算出来。
DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "evidence_coverage",
        "label": "证据覆盖",
        "weight": 0.20,
        "source": "draft.md / references.json / outline.json",
        "how": "正文引用数 ÷ 大纲引用预算（60）+ 参考文献被引用比例（20）+ 证据池是否足够每节 2 条（20）",
    },
    {
        "id": "citation_reliability",
        "label": "引文可靠性",
        "weight": 0.22,
        "source": "verification.json",
        "how": "DOI 核验通过率（70）+ 非错配比例（30）",
    },
    {
        "id": "method_reproducibility",
        "label": "方法可复现性",
        "weight": 0.15,
        "source": "search.json / screening.json / criteria.json",
        "how": "检索步骤全部成功（40）+ 筛选无待判定（30）+ PRISMA 计数自洽（30）",
    },
    {
        "id": "structural_completeness",
        "label": "结构完整度",
        "weight": 0.18,
        "source": "outline.json / draft.md",
        "how": "大纲小节覆盖率（50）+ 字数在目标 70%-140% 之间（30）+ 有摘要（20）",
    },
    {
        "id": "restraint",
        "label": "表述克制",
        "weight": 0.13,
        "source": "qa.json / draft.md",
        "how": "无占位符（50）+ 绝对化措辞每处扣 15（50）",
    },
    {
        "id": "delivery_completeness",
        "label": "交付完整度",
        "weight": 0.12,
        "source": "report.md / exports/",
        "how": "报告存在（30）+ 已导出 md/html/docx/pdf 的覆盖率（70）",
    },
)

_DELIVERABLE_FORMATS = ("md", "html", "docx", "pdf")


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _draft_sections(draft: str) -> list[str]:
    titles: list[str] = []
    for line in (draft or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            if text:
                titles.append(text)
    return titles


def _has_abstract(sections: list[str]) -> bool:
    return any(title in ("摘要", "Abstract", "ABSTRACT") for title in sections)


def evaluate(facts: dict[str, Any]) -> dict[str, Any]:
    """纯函数：由运行产物事实算出评审分（无 IO、无时间、无随机）。"""
    state = facts.get("state") or {}
    outline = facts.get("outline") or {}
    references = facts.get("references") or {}
    verification = facts.get("verification") or {}
    screening = facts.get("screening") or {}
    search = facts.get("search") or {}
    qa = facts.get("qa") or {}
    draft = facts.get("draft") or ""
    exports = facts.get("exports") or []
    blocks: list[dict[str, Any]] = []
    scores: dict[str, Any] = {}

    words = word_count(draft)
    citations = len(MARKER_RE.findall(draft))
    outline_sections = [str(item.get("title") or "") for item in outline.get("sections") or []]
    draft_sections = _draft_sections(draft)
    covered = [title for title in outline_sections if title in draft_sections]

    # 1. 证据覆盖
    if not draft or not outline_sections:
        scores["evidence_coverage"] = None
        blocks.append(
            {
                "code": "AUDIT_NO_DRAFT",
                "message": "缺少正文或大纲，无法计算证据覆盖",
                "fix": "先 research_plan action=outline，再 research_compose 成稿",
            }
        )
    else:
        budget = int(outline.get("citation_total_budget") or 0) or max(1, len(outline_sections) * 3)
        budget_ratio = min(1.0, citations / budget) if budget else 0.0
        entries = references.get("entries") or []
        uncited = [entry for entry in entries if not entry.get("cited", True)]
        cited_ratio = (len(entries) - len(uncited)) / len(entries) if entries else 0.0
        pool = int(outline.get("evidence_count") or 0)
        pool_ratio = min(1.0, pool / max(1, len(outline_sections) * 2))
        score = _clamp(budget_ratio * 60 + cited_ratio * 20 + pool_ratio * 20)
        scores["evidence_coverage"] = round(score, 1)
        if budget_ratio < 0.6:
            blocks.append(
                {
                    "code": "AUDIT_CITATIONS_LOW",
                    "message": f"正文引用 {citations} 处，只有大纲预算 {budget} 的 {budget_ratio:.0%}",
                    "fix": "给缺乏引用的论断补证据（cite_key 来自 references.json），或删掉无证据的断言",
                }
            )
        if uncited:
            blocks.append(
                {
                    "code": "AUDIT_REFS_UNUSED",
                    "message": f"{len(uncited)} 条参考文献未在正文引用",
                    "fix": "在对应位置补引用，或从参考文献表删除未使用条目",
                }
            )

    # 2. 引文可靠性
    if not verification or not verification.get("total"):
        scores["citation_reliability"] = None
        blocks.append(
            {
                "code": "AUDIT_NO_VERIFICATION",
                "message": "还没有引文核验结果，可靠性不可评",
                "fix": "先 research_verify（需要网络；离线环境会降级为 NOT_RUN）",
            }
        )
    else:
        total = int(verification.get("total") or 0)
        ratio = float(verification.get("verified_ratio") or 0.0)
        mismatch = int((verification.get("summary") or {}).get("mismatch") or 0)
        non_mismatch = 1.0 - (mismatch / total if total else 0.0)
        scores["citation_reliability"] = round(_clamp(ratio * 70 + non_mismatch * 30), 1)
        if mismatch:
            blocks.append(
                {
                    "code": "AUDIT_CITATION_MISMATCH",
                    "message": f"{mismatch} 条引文的元数据与 DOI 记录不一致",
                    "fix": "核对 verification.json 的 items，改正作者/年份/标题或替换文献",
                }
            )
        if ratio < float(verification.get("min_verified_ratio") or 0.9):
            blocks.append(
                {
                    "code": "AUDIT_VERIFY_RATIO",
                    "message": f"核验通过率 {ratio:.0%} 低于闸门 {verification.get('min_verified_ratio')}",
                    "fix": "给缺 DOI 的文献补 DOI/候选，或把无法核验的文献降级为背景引用并在局限中说明",
                }
            )

    # 3. 方法可复现性
    steps = search.get("steps") or []
    step_ok = (sum(1 for step in steps if step.get("ok")) / len(steps)) if steps else 0.0
    pending = int((screening.get("counts") or {}).get("excluded_pending") or 0)
    screen_ok = 1.0 if screening and not pending else (0.5 if screening else 0.0)
    counts = (screening.get("counts") or {})
    prisma_ok = 0.0
    if counts:
        identified = int(counts.get("identified") or 0)
        screened = int(counts.get("screened") or 0)
        duplicates = int(counts.get("duplicates_removed") or 0)
        prisma_ok = 1.0 if identified == screened + duplicates else 0.0
    scores["method_reproducibility"] = round(_clamp(step_ok * 40 + screen_ok * 30 + prisma_ok * 30), 1)
    if not steps:
        blocks.append(
            {
                "code": "AUDIT_NO_SEARCH_LOG",
                "message": "缺少 search.json：检索过程不可复现",
                "fix": "用 research_search 重新检索（会写入检索式、来源与每源成败）",
            }
        )
    if pending:
        blocks.append(
            {
                "code": "AUDIT_SCREEN_PENDING",
                "message": f"全文筛选还有 {pending} 条待判定",
                "fix": "补 decisions 后重跑 research_screen stage=full_text",
            }
        )
    if counts and not prisma_ok:
        blocks.append(
            {
                "code": "AUDIT_PRISMA_INCONSISTENT",
                "message": "PRISMA 计数不自洽（identified ≠ screened + duplicates_removed）",
                "fix": "重跑 research_search 与 research_screen，不要手工改 screening.json",
            }
        )

    # 4. 结构完整度
    if not outline_sections:
        scores["structural_completeness"] = None
        blocks.append(
            {
                "code": "AUDIT_NO_OUTLINE",
                "message": "缺少大纲，结构完整度不可评",
                "fix": "research_plan action=outline",
            }
        )
    else:
        coverage = len(covered) / len(outline_sections)
        target = int(outline.get("target_words") or 0)
        words_ok = 1.0 if target and 0.7 * target <= words <= 1.4 * target else 0.0
        abstract_ok = 1.0 if _has_abstract(draft_sections) else 0.0
        scores["structural_completeness"] = round(_clamp(coverage * 50 + words_ok * 30 + abstract_ok * 20), 1)
        missing = [title for title in outline_sections if title not in draft_sections]
        if missing:
            blocks.append(
                {
                    "code": "AUDIT_SECTIONS_MISSING",
                    "message": f"大纲有 {len(missing)} 节未写入正文：{', '.join(missing[:6])}",
                    "fix": "用 research_compose action=append 补齐（不要自己拼格式）",
                }
            )
        if not abstract_ok:
            blocks.append(
                {"code": "AUDIT_NO_ABSTRACT", "message": "正文缺摘要节", "fix": "补 摘要（问题/方法/发现/结论/局限）"}
            )
        if target and not words_ok:
            blocks.append(
                {
                    "code": "AUDIT_LENGTH_DEVIATION",
                    "message": f"正文 {words} 字，偏离目标 {target} 超过 ±30%",
                    "fix": "按逐节配额增删；优先补齐引用预算不达标的节",
                }
            )

    # 5. 表述克制
    issues = qa.get("issues") or []
    overclaims = int(qa.get("warnings") or 0) and sum(1 for item in issues if item.get("code") == "OVERCLAIM_PHRASE")
    placeholders = sum(1 for item in issues if item.get("code") == "PLACEHOLDER_REMAINS")
    # qa 对同代码做过合并，合并条目带 count
    overclaims = sum(int(item.get("count") or 1) for item in issues if item.get("code") == "OVERCLAIM_PHRASE")
    placeholders = sum(int(item.get("count") or 1) for item in issues if item.get("code") == "PLACEHOLDER_REMAINS")
    if not draft:
        scores["restraint"] = None
    else:
        if not issues:
            # 没有 qa.json 时退化到词汇表直查（仍然确定性）
            lowered = draft.lower()
            overclaims = sum(1 for phrase in BANNED_PHRASES if phrase.lower() in lowered)
        scores["restraint"] = round(_clamp((50 if placeholders == 0 else 0) + max(0, 50 - overclaims * 15)), 1)
        if placeholders:
            blocks.append(
                {"code": "AUDIT_PLACEHOLDERS", "message": f"正文残留 {placeholders} 处占位符", "fix": "删掉占位符并写入真实内容"}
            )
        if overclaims:
            blocks.append(
                {
                    "code": "AUDIT_OVERCLAIM",
                    "message": f"{overclaims} 处绝对化措辞（如'首次证明''彻底解决'）",
                    "fix": "改成与证据强度匹配的条件化表述",
                }
            )

    # 6. 交付完整度
    present = 0
    lowered_exports = [name.lower() for name in exports]
    for fmt in _DELIVERABLE_FORMATS:
        if any(name.endswith("." + fmt) for name in lowered_exports):
            present += 1
    report_ok = 1.0 if facts.get("has_report") else 0.0
    if not report_ok and not present:
        scores["delivery_completeness"] = None
        blocks.append(
            {
                "code": "AUDIT_NO_DELIVERABLES",
                "message": "还没有报告与导出文件",
                "fix": "research_export action=all（docx/pdf 缺依赖时会降级并说明）",
            }
        )
    else:
        scores["delivery_completeness"] = round(_clamp(report_ok * 30 + (present / len(_DELIVERABLE_FORMATS)) * 70), 1)
        if present < len(_DELIVERABLE_FORMATS):
            blocks.append(
                {
                    "code": "AUDIT_EXPORT_PARTIAL",
                    "message": f"只导出了 {present}/{len(_DELIVERABLE_FORMATS)} 种格式",
                    "fix": "research_export action=all formats=md,html,docx,pdf；缺依赖时看 degraded 说明",
                }
            )

    # 加权总分：缺失维度不参与（权重归一化），避免"没做就给 0 分"这类不公平
    weighted = 0.0
    weight_sum = 0.0
    for dimension in DIMENSIONS:
        score = scores.get(dimension["id"])
        if score is None:
            continue
        weighted += float(score) * float(dimension["weight"])
        weight_sum += float(dimension["weight"])
    total = round(weighted / weight_sum, 1) if weight_sum else 0.0

    hard_fail = (
        int(qa.get("errors") or 0) > 0
        or pending > 0
        or (verification and int(verification.get("counts", {}).get("NOT_RUN") or 0) == int(verification.get("total") or 0))
        or not draft
    )
    if hard_fail:
        gate = "FAIL"
    elif total < 85:
        gate = "WARN"
    else:
        gate = "PASS"
    if total >= 85:
        decision = "accept"
    elif total >= 70:
        decision = "minor_revision"
    elif total >= 55:
        decision = "major_revision"
    else:
        decision = "reject"

    measured = [dimension["id"] for dimension in DIMENSIONS if scores.get(dimension["id"]) is not None]
    return {
        "schema": "atrix-research/audit/1",
        "run": str(state.get("id") or facts.get("run") or ""),
        "weighted_total": total,
        "gate": gate,
        "decision": decision,
        "dimensions": [
            {
                **{key: dimension[key] for key in ("id", "label", "weight", "source", "how")},
                "score": scores.get(dimension["id"]),
            }
            for dimension in DIMENSIONS
        ],
        "measured": measured,
        "unmeasured": [dimension["id"] for dimension in DIMENSIONS if scores.get(dimension["id"]) is None],
        "facts": {
            "words": words,
            "citations": citations,
            "sections_covered": f"{len(covered)}/{len(outline_sections)}" if outline_sections else "0/0",
            "references": len(references.get("entries") or []),
            "verified_ratio": verification.get("verified_ratio"),
            "qa_verdict": qa.get("verdict"),
            "exports": len(exports),
        },
        "blocks": blocks,
        "panel_note": (
            "原创性与伦理两项不可由产物度量，必须走 research_panel 的评审；"
            "本结论只覆盖可度量维度，换模型不影响这些分数。"
        ),
    }


def audit_markdown(audit: dict[str, Any]) -> str:
    lines = [
        f"# 确定性评审（{audit.get('run')}）",
        "",
        f"- 总分：**{audit.get('weighted_total')}**（{audit.get('decision')}）",
        f"- 闸门：**{audit.get('gate')}**",
        f"- 事实：{audit.get('facts')}",
        "",
        "| 维度 | 权重 | 得分 | 算法出处 |",
        "| --- | --- | --- | --- |",
    ]
    for dimension in audit.get("dimensions") or []:
        score = dimension.get("score")
        lines.append(
            f"| {dimension['label']} | {dimension['weight']} | "
            f"{'-' if score is None else score} | {dimension['source']} |"
        )
    if audit.get("unmeasured"):
        lines.append("")
        lines.append(f"未度量（不参与总分）：{', '.join(audit['unmeasured'])}")
    if audit.get("blocks"):
        lines.append("")
        lines.append("## 必须处理")
        for block in audit["blocks"]:
            lines.append(f"- **{block['code']}** {block['message']} → {block['fix']}")
    lines.append("")
    lines.append(f"> {audit.get('panel_note')}")
    return "\n".join(lines) + "\n"


def tool_audit(args: Any) -> dict[str, Any]:
    store = open_run(getattr(args, "run", ""))
    state = store.load_state()
    draft = store.read("draft.md", required=False, default="") or ""
    if not draft.strip():
        raise ResearchError(
            ARTIFACT_MISSING,
            "还没有稿件，无法评审",
            hint="先用 research_compose 成稿，再跑 research_audit。",
        )
    facts = {
        "state": state,
        "outline": store.read("outline.json", required=False, default=None) or {},
        "references": store.read("references.json", required=False, default=None) or {},
        "verification": store.read("verification.json", required=False, default=None) or {},
        "screening": store.read("screening.json", required=False, default=None) or {},
        "search": store.read("search.json", required=False, default=None) or {},
        "qa": store.read("qa.json", required=False, default=None) or {},
        "draft": draft,
        "has_report": store.exists("report.md"),
        "exports": _exports(store),
    }
    audit = evaluate(facts)
    audit["generated_at"] = now_iso()
    artifacts = [store.write("audit.json", audit), store.write("audit.md", audit_markdown(audit))]
    state["stage"] = "review" if audit["gate"] != "FAIL" else state.get("stage")
    store.save_state(state)
    degraded = ["AUDIT_FAIL"] if audit["gate"] == "FAIL" else []
    warnings = [f"{block['code']}: {block['message']}" for block in audit.get("blocks") or []]
    audit["next"] = (
        ["按 blocks[].fix 修复后重跑 research_audit"]
        if audit["gate"] == "FAIL"
        else ["research_export action=all 生成交付文件", "需要主观评审时用 research_panel"]
    )
    return result(audit, artifacts=artifacts, degraded=degraded, warnings=warnings[:8])


def _exports(store: Any) -> list[str]:
    directory = store.path("exports")
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory) if not name.startswith("."))


def _missing_draft() -> Any:
    from .errors import ARTIFACT_MISSING as _MISSING
    from .errors import ResearchError

    return ResearchError(_MISSING, "还没有稿件，无法评审", hint="先用 research_compose 成稿，再跑 research_audit。")
