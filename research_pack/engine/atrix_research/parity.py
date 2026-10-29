"""模型无关性回归：用"模拟不同模型的输出"跑同一套确定性链路。

这是把"换模型质量还能保持一致"从口号变成可验证事实的手段。做法：用三份**固定
样本**代替三个模型（好模型 / 敷衍模型 / 乱编模型），它们只影响"内容"，然后断言：

* 同样的内容输入 → ``compose`` 渲染出的 Markdown 逐字节相同（确定性）；
* 敷衍输出（小节不全、无引用）→ 质检/评审必须报出来，不能 PASS；
* 乱编输出（编造 cite_key、占位符、绝对化措辞）→ 契约直接拒绝写入；
* 评审总分排序稳定：好 > 敷衍，好 > 乱编；
* ``flow`` 的计划只由产物存在性决定，与模型无关。

跑法：``run.py doctor --action=parity``（离线，不联网，秒级）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .audit import evaluate as audit_evaluate
from .compose import render, validate as compose_validate
from .flow import plan as flow_plan
from .qa import check_draft

__all__ = ["run_parity"]

#: 共享事实：引用表 / 大纲（模拟一次已完成检索-筛选-核验的运行）。
REFERENCES = {
    "schema": "atrix-research/references/1",
    "style": "gbt7714",
    "count": 4,
    "unused_in_text": 0,
    "entries": [
        {"index": 1, "cite_key": "zhang2023rag", "work_key": "w1", "text": "[1] 张三. 检索增强生成综述[J]. 2023.", "cited": True},
        {"index": 2, "cite_key": "liu2022survey", "work_key": "w2", "text": "[2] Liu. A survey of RAG[J]. 2022.", "cited": True},
        {"index": 3, "cite_key": "wang2024eval", "work_key": "w3", "text": "[3] 王五. RAG 评测方法[J]. 2024.", "cited": True},
        {"index": 4, "cite_key": "chen2021dense", "work_key": "w4", "text": "[4] Chen. Dense retrieval[J]. 2021.", "cited": True},
    ],
    "by_cite_key": {
        "zhang2023rag": "w1",
        "liu2022survey": "w2",
        "wang2024eval": "w3",
        "chen2021dense": "w4",
    },
}

OUTLINE = {
    "schema": "atrix-research/outline/1",
    "doc_type": "review",
    "language": "zh",
    "style": "gbt7714",
    "target_words": 320,
    "sections": [
        {"id": "abstract", "title": "摘要", "target_words": 60, "citation_budget": 0, "must_cover": []},
        {"id": "intro", "title": "引言", "target_words": 70, "citation_budget": 2, "must_cover": []},
        {"id": "status", "title": "研究现状", "target_words": 80, "citation_budget": 3, "must_cover": []},
        {"id": "discussion", "title": "讨论", "target_words": 60, "citation_budget": 2, "must_cover": []},
        {"id": "conclusion", "title": "结论", "target_words": 50, "citation_budget": 1, "must_cover": []},
    ],
    "evidence_pool": [{"cite_key": key} for key in REFERENCES["by_cite_key"]],
    "evidence_count": 4,
    "citation_total_budget": 8,
}

#: 好模型：覆盖全部小节、引用预算达标、表述克制。
GOOD_PAYLOAD = {
    "sections": [
        {
            "heading": "摘要",
            "paragraphs": [
                {
                    "text": (
                        "本文梳理检索增强生成（RAG）在开放域问答中的研究进展。方法上汇总三个数据库的"
                        "近三年文献，按检索器、生成器与评测三条线索归类；主要发现是稠密检索与重排显著"
                        "改善事实一致性，但跨域泛化仍受检索质量限制。结论是 RAG 的有效性更多取决于"
                        "检索环节而非生成模型规模。本文为综述，不含原始实验。"
                    ),
                    "cites": [],
                }
            ],
        },
        {
            "heading": "引言",
            "paragraphs": [
                {
                    "text": (
                        "大模型在开放域问答中仍会出现事实性错误，检索增强被提出用于把外部证据引入生成过程，"
                        "从而降低无依据陈述的比例，这一思路在多个综述中被系统归纳。"
                    ),
                    "cites": ["zhang2023rag", "liu2022survey"],
                }
            ],
        },
        {
            "heading": "研究现状",
            "paragraphs": [
                {
                    "text": (
                        "检索环节的主流做法从稀疏检索转向稠密向量检索，并在其后接重排以提升证据精度；"
                        "已有工作显示稠密检索在语义匹配上更稳定，但在领域外查询上召回下降明显。"
                    ),
                    "cites": ["chen2021dense"],
                },
                {
                    "text": (
                        "生成环节的核心问题是如何约束输出与证据一致。常见方案包括把证据拼进上下文、"
                        "做后置校验，以及在解码阶段加入一致性惩罚；评测表明这些方案能降低幻觉率，"
                        "但会牺牲一定的回答完整度。"
                    ),
                    "cites": ["wang2024eval", "liu2022survey"],
                },
            ],
        },
        {
            "heading": "讨论",
            "paragraphs": [
                {
                    "text": (
                        "现有研究的结论存在分歧：一部分工作把收益归因于检索质量提升，另一部分强调生成"
                        "阶段的约束机制。方法差异主要体现在数据集、检索器与评测口径上，因此跨研究比较需谨慎。"
                    ),
                    "cites": ["wang2024eval"],
                }
            ],
        },
        {
            "heading": "结论",
            "paragraphs": [
                {
                    "text": (
                        "在本文覆盖的文献范围内，RAG 的收益主要来自检索与重排环节；后续研究应在统一的"
                        "跨域基准上比较不同方案，并报告检索质量指标。"
                    ),
                    "cites": ["zhang2023rag"],
                }
            ],
        },
    ]
}

#: 敷衍模型：只写两节、没有引用、缺摘要。
WEAK_PAYLOAD = {
    "sections": [
        {"heading": "引言", "paragraphs": [{"text": "检索增强生成最近很受关注，很多工作都在做。", "cites": []}]},
        {"heading": "研究现状", "paragraphs": [{"text": "有人做检索，有人做生成，效果都还可以。", "cites": []}]},
    ]
}

#: 乱编模型：编造 cite_key、留占位符、绝对化措辞。
SLOPPY_PAYLOAD = {
    "sections": [
        {
            "heading": "引言",
            "paragraphs": [
                {"text": "检索增强彻底解决了大模型幻觉问题，首次证明其可以替代人工核验。", "cites": ["fake2024nonexistent"]},
                {"text": "TODO: 这里补一段综述，引用 [@madeup2020key]。", "cites": []},
            ],
        }
    ]
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _draft_of(payload: dict[str, Any]) -> str:
    """把 payload 渲染成正文（不校验，模拟"模型直接写出来的稿子"）。"""
    checked = compose_validate(payload, OUTLINE, REFERENCES)
    if checked["ok"]:
        return render(checked["normalized"])
    # 校验失败时用原样拼接，模拟"没有走硬工具、直接写盘"的场景
    lines: list[str] = []
    for section in payload.get("sections") or []:
        lines.append(f"## {section.get('heading')}")
        lines.append("")
        for paragraph in section.get("paragraphs") or []:
            markers = "".join(f"[@{key}]" for key in paragraph.get("cites") or [])
            lines.append((paragraph.get("text") or "") + (f" {markers}" if markers else ""))
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _facts(draft: str, *, qa: dict[str, Any] | None = None, exports: int = 4) -> dict[str, Any]:
    verification = {
        "schema": "atrix-research/verification/1",
        "counts": {"VERIFIED": 4},
        "total": 4,
        "verified_ratio": 1.0,
        "min_verified_ratio": 0.9,
        "gate": "PASS",
        "summary": {"verified": 4, "partial": 0, "mismatch": 0, "not_found": 0, "no_doi": 0, "not_run": 0},
    }
    screening = {
        "schema": "atrix-research/screening/1",
        "stage": "title_abstract",
        "counts": {
            "identified": 40,
            "duplicates_removed": 10,
            "screened": 30,
            "excluded": 26,
            "excluded_pending": 0,
            "included": 4,
        },
        "included_keys": ["w1", "w2", "w3", "w4"],
        "pending_keys": [],
        "gate": "PASS",
    }
    search = {
        "schema": "atrix-research/search/1",
        "queries": ["retrieval augmented generation"],
        "steps": [{"source": "openalex", "ok": True}, {"source": "crossref", "ok": True}],
    }
    qa = qa or check_draft(draft, REFERENCES, OUTLINE)
    return {
        "state": {"id": "parity-fixture"},
        "outline": OUTLINE,
        "references": REFERENCES,
        "verification": verification,
        "screening": screening,
        "search": search,
        "qa": qa,
        "draft": draft,
        "has_report": True,
        "exports": [f"report.{ext}" for ext in ("md", "html", "docx", "pdf")][:exports],
    }


def _empty_facts() -> dict[str, Any]:
    return {
        "state": {"id": "parity-empty"},
        "outline": {},
        "references": {},
        "verification": {},
        "screening": {},
        "search": {},
        "qa": {},
        "draft": "",
        "has_report": False,
        "exports": [],
    }


def run_parity() -> dict[str, Any]:
    """跑全部模型无关性断言；返回 ``{passed, total, checks}``。"""
    checks: list[dict[str, Any]] = []

    def check(name: str, predicate: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(predicate), "detail": detail})

    # 1. compose 校验 / 渲染
    good_checked = compose_validate(GOOD_PAYLOAD, OUTLINE, REFERENCES)
    good_draft = _draft_of(GOOD_PAYLOAD)
    good_draft_again = _draft_of(GOOD_PAYLOAD)
    check(
        "compose.render_deterministic",
        _sha(good_draft) == _sha(good_draft_again),
        f"sha256={_sha(good_draft)[:12]}…（两次渲染一致）",
    )
    check(
        "compose.good_payload_valid",
        good_checked["ok"] and not good_checked["errors"],
        f"sections={good_checked['stats'].get('sections')} citations={good_checked['stats'].get('citations')}",
    )
    sloppy_checked = compose_validate(SLOPPY_PAYLOAD, OUTLINE, REFERENCES)
    codes = {item["code"] for item in sloppy_checked["errors"]}
    check(
        "compose.rejects_invented_key",
        "E_UNKNOWN_CITE" in codes,
        f"error codes={sorted(codes)}",
    )
    check(
        "compose.rejects_placeholder",
        "E_PLACEHOLDER" in codes,
        "占位符段落被契约拒绝（不会写进正文）",
    )
    reversed_payload = {"sections": list(reversed(GOOD_PAYLOAD["sections"]))}
    reversed_checked = compose_validate(reversed_payload, OUTLINE, REFERENCES)
    order_source = [item["heading"] for item in reversed_checked["normalized"]]
    check(
        "compose.reorders_to_outline",
        order_source == [item["title"] for item in OUTLINE["sections"]],
        f"输入倒序 → 输出顺序 {order_source}",
    )

    # 2. 质检：敷衍/乱编不能 PASS，好稿不能有 error
    good_qa = check_draft(good_draft, REFERENCES, OUTLINE)
    weak_draft = _draft_of(WEAK_PAYLOAD)
    weak_qa = check_draft(weak_draft, REFERENCES, OUTLINE)
    sloppy_draft = _draft_of(SLOPPY_PAYLOAD)
    sloppy_qa = check_draft(sloppy_draft, REFERENCES, OUTLINE)
    check("qa.good_has_no_error", good_qa["errors"] == 0, f"verdict={good_qa['verdict']} score={good_qa['score']}")
    check("qa.weak_not_passed", weak_qa["verdict"] != "PASS", f"verdict={weak_qa['verdict']} score={weak_qa['score']}")
    check(
        "qa.sloppy_fails",
        sloppy_qa["verdict"] == "FAIL" and sloppy_qa["errors"] > 0,
        f"verdict={sloppy_qa['verdict']} errors={sloppy_qa['errors']}",
    )

    # 3. 评审：分数排序稳定且可复算
    good_audit = audit_evaluate(_facts(good_draft, qa=good_qa))
    good_audit_again = audit_evaluate(_facts(good_draft, qa=good_qa))
    weak_audit = audit_evaluate(_facts(weak_draft, qa=weak_qa))
    sloppy_audit = audit_evaluate(_facts(sloppy_draft, qa=sloppy_qa))
    empty_audit = audit_evaluate(_empty_facts())
    check(
        "audit.deterministic",
        json.dumps(good_audit, ensure_ascii=False, sort_keys=True)
        == json.dumps(good_audit_again, ensure_ascii=False, sort_keys=True),
        f"total={good_audit['weighted_total']} gate={good_audit['gate']}",
    )
    check(
        "audit.ranks_good_over_weak",
        float(good_audit["weighted_total"]) > float(weak_audit["weighted_total"]),
        f"good={good_audit['weighted_total']} weak={weak_audit['weighted_total']}",
    )
    check(
        "audit.ranks_good_over_sloppy",
        float(good_audit["weighted_total"]) > float(sloppy_audit["weighted_total"]),
        f"good={good_audit['weighted_total']} sloppy={sloppy_audit['weighted_total']}",
    )
    check(
        "audit.sloppy_gate_fail",
        sloppy_audit["gate"] == "FAIL",
        f"gate={sloppy_audit['gate']} blocks={[b['code'] for b in sloppy_audit['blocks']]}",
    )
    check(
        "audit.empty_is_fail_and_honest",
        empty_audit["gate"] == "FAIL" and len(empty_audit["unmeasured"]) >= 3,
        f"unmeasured={empty_audit['unmeasured']}",
    )

    # 4. flow：计划只由产物存在性决定
    start_facts = {"state": {"id": "r-x"}, "library": {}, "screening": {}, "verification": {}, "references": {}, "outline": {}, "qa": {}, "audit": {}, "draft": "", "has_report": False, "exports": [], "draft_headings": [], "draft_citations": 0}
    plan_a = flow_plan(json.loads(json.dumps(start_facts)))
    plan_b = flow_plan(json.loads(json.dumps(start_facts)))
    check(
        "flow.deterministic",
        json.dumps(plan_a, ensure_ascii=False, sort_keys=True) == json.dumps(plan_b, ensure_ascii=False, sort_keys=True),
        f"stage={plan_a['stage']} next={plan_a['next'][0]['tool'] if plan_a['next'] else '-'}",
    )
    check(
        "flow.starts_with_search",
        plan_a["next"] and plan_a["next"][0]["tool"] == "research_search",
        f"next={plan_a['next'][0]['tool'] if plan_a['next'] else '-'}",
    )
    after_search = dict(start_facts)
    after_search["library"] = {"works": [{"key": "w1", "title": "t"}]}
    plan_search = flow_plan(after_search)
    check(
        "flow.moves_to_screen",
        plan_search["next"] and plan_search["next"][0]["tool"] == "research_screen",
        f"next={plan_search['next'][0]['tool'] if plan_search['next'] else '-'}",
    )
    pending_facts = json.loads(json.dumps(after_search))
    pending_facts["screening"] = {
        "counts": {"included": 3, "excluded_pending": 5},
        "pending_keys": ["w2"],
        "gate": "FAIL",
        "included_keys": ["w1"],
    }
    plan_pending = flow_plan(pending_facts)
    check(
        "flow.blocks_on_pending_screen",
        plan_pending["gate"] == "FAIL" and any(item["code"] == "SCREEN_PENDING" for item in plan_pending["blockers"]),
        f"gate={plan_pending['gate']} blockers={[b['code'] for b in plan_pending['blockers']]}",
    )

    # 5. 模型无关性总结：所有断言都通过才说明"换模型不改判定"
    passed = sum(1 for item in checks if item["ok"])
    return {
        "schema": "atrix-research/parity/1",
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "personas": {
            "good": {"words": good_audit["facts"]["words"], "audit": good_audit["weighted_total"], "qa": good_qa["verdict"]},
            "weak": {"words": weak_audit["facts"]["words"], "audit": weak_audit["weighted_total"], "qa": weak_qa["verdict"]},
            "sloppy": {"words": sloppy_audit["facts"]["words"], "audit": sloppy_audit["weighted_total"], "qa": sloppy_qa["verdict"]},
        },
        "verdict": "PASS" if passed == len(checks) else "FAIL",
        "note": (
            "三份固定样本代替三个模型：内容不同，但排版/计数/门禁/评分由同一套代码算出。"
            "任一断言失败，说明引擎里混进了模型相关的判断。"
        ),
    }
