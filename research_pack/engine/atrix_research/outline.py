"""大纲生成：把"写论文"变成可校验的骨架 + 写作契约。

输出不是一段自然语言建议，而是结构化的：

* 每节的目标字数、引用预算、必须回答的问题、允许/禁止的写法；
* 一节对应文献库里的哪些候选证据（带引用键，写作时直接 ``[@key]``）；
* 总字数与各节之和一致（不允许"写着看"）。

这样"写作"环节由任何模型完成都能被 ``research_cite action=check`` 逐条核对。
"""

from __future__ import annotations

from typing import Any

from .cite import assign_cite_keys
from .models import Work

__all__ = ["TEMPLATES", "build_outline", "outline_markdown", "render_section_contract"]

#: 三种常见文体。每个 section：id/title/weight（占目标字数比例）/citations/purpose/must。
TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "review": [
        {
            "id": "abstract",
            "title": "摘要",
            "weight": 0.06,
            "citations": 0,
            "purpose": "用 150-300 字说明：问题、范围、方法（检索与筛选）、主要发现、结论。",
            "must": ["研究问题", "检索与筛选口径", "主要结论", "局限一句"],
        },
        {
            "id": "introduction",
            "title": "1 引言",
            "weight": 0.14,
            "citations": 6,
            "purpose": "建立问题的重要性与缺口，明确本文的综述问题与贡献。",
            "must": ["领域现状", "已有综述的不足", "本文问题与范围", "贡献列表"],
        },
        {
            "id": "method",
            "title": "2 综述方法",
            "weight": 0.14,
            "citations": 3,
            "purpose": "写清检索源、检索式、时间范围、纳排标准与筛选流程（用 PRISMA 数字）。",
            "must": ["检索源与检索式", "纳排标准", "PRISMA 计数", "质量评估方式"],
        },
        {
            "id": "landscape",
            "title": "3 研究现状",
            "weight": 0.20,
            "citations": 12,
            "purpose": "按技术路线/问题类型分组陈述，每组给代表工作与关键结论，不做流水账。",
            "must": ["分组依据", "各组代表工作", "组间差异"],
        },
        {
            "id": "synthesis",
            "title": "4 综合与讨论",
            "weight": 0.22,
            "citations": 10,
            "purpose": "跨研究比较：一致结论、冲突证据、方法学差异如何解释分歧。",
            "must": ["一致结论", "冲突证据及可能原因", "证据强度评价"],
        },
        {
            "id": "gaps",
            "title": "5 研究缺口与展望",
            "weight": 0.14,
            "citations": 4,
            "purpose": "给出可执行的研究议程：缺口 → 为什么重要 → 怎么做。",
            "must": ["缺口清单", "优先级", "可验证的研究设计建议"],
        },
        {
            "id": "limitations",
            "title": "6 局限",
            "weight": 0.10,
            "citations": 0,
            "purpose": "诚实说明检索覆盖、语言偏倚、筛选主观性、证据等级。",
            "must": ["检索局限", "筛选局限", "结论适用边界"],
        },
    ],
    "empirical": [
        {
            "id": "abstract",
            "title": "摘要",
            "weight": 0.05,
            "citations": 0,
            "purpose": "结构化摘要：背景、目的、方法、结果（含关键数字）、结论。",
            "must": ["背景", "方法", "关键结果", "结论"],
        },
        {
            "id": "introduction",
            "title": "1 引言",
            "weight": 0.14,
            "citations": 8,
            "purpose": "从领域问题收敛到本文假设，给出可检验的贡献陈述。",
            "must": ["问题背景", "研究缺口", "研究问题/假设", "贡献"],
        },
        {
            "id": "related",
            "title": "2 相关工作",
            "weight": 0.16,
            "citations": 12,
            "purpose": "按与本文的关系组织（同问题/同方法/同数据），指出差异。",
            "must": ["对比维度", "代表性工作", "与本文差异"],
        },
        {
            "id": "method",
            "title": "3 方法",
            "weight": 0.22,
            "citations": 4,
            "purpose": "可复现：数据、变量、模型/实验设计、评价指标、统计检验。",
            "must": ["数据与预处理", "实验设计", "指标与统计方法", "可复现性说明"],
        },
        {
            "id": "results",
            "title": "4 结果",
            "weight": 0.23,
            "citations": 2,
            "purpose": "只陈述结果，不做解释；每个数字都要能追溯到表格/图。",
            "must": ["主结果", "消融/敏感性", "统计显著性"],
        },
        {
            "id": "discussion",
            "title": "5 讨论",
            "weight": 0.14,
            "citations": 6,
            "purpose": "解释结果、与文献对话、承认替代解释。",
            "must": ["结果解释", "与文献对照", "替代解释"],
        },
        {
            "id": "limitations",
            "title": "6 局限与结论",
            "weight": 0.06,
            "citations": 0,
            "purpose": "限制条件与下一步工作。",
            "must": ["局限", "结论", "未来工作"],
        },
    ],
    "proposal": [
        {
            "id": "abstract",
            "title": "摘要",
            "weight": 0.06,
            "citations": 0,
            "purpose": "问题、目标、方法、预期贡献与可行性。",
            "must": ["问题", "目标", "方法", "预期贡献"],
        },
        {
            "id": "background",
            "title": "1 研究背景与问题",
            "weight": 0.18,
            "citations": 10,
            "purpose": "用证据说明问题真实存在且尚未解决。",
            "must": ["现状证据", "缺口", "研究问题"],
        },
        {
            "id": "review",
            "title": "2 相关工作",
            "weight": 0.18,
            "citations": 12,
            "purpose": "按方法脉络梳理，指出可迁移与不可迁移之处。",
            "must": ["方法脉络", "可借鉴点", "不足"],
        },
        {
            "id": "plan",
            "title": "3 研究设计与方法",
            "weight": 0.24,
            "citations": 5,
            "purpose": "技术路线、数据、评价方式、风险与备选方案。",
            "must": ["技术路线", "数据来源", "评价指标", "风险与备选"],
        },
        {
            "id": "timeline",
            "title": "4 计划与预期成果",
            "weight": 0.14,
            "citations": 0,
            "purpose": "阶段划分、里程碑、可交付物。",
            "must": ["阶段划分", "里程碑", "可交付物"],
        },
        {
            "id": "feasibility",
            "title": "5 可行性与伦理",
            "weight": 0.12,
            "citations": 2,
            "purpose": "资源、数据合规、伦理审查要点。",
            "must": ["资源条件", "数据合规", "伦理"],
        },
        {
            "id": "limitations",
            "title": "6 局限",
            "weight": 0.08,
            "citations": 0,
            "purpose": "方案本身的风险与边界。",
            "must": ["方案风险", "适用边界"],
        },
    ],
}


def _section_title(doc_type: str, section_id: str, fallback: str, language: str) -> str:
    """英文稿件的章节标题；没有映射时退回模板标题。"""
    if (language or "zh").lower() != "en":
        return fallback
    return _EN_TITLES.get(doc_type, {}).get(section_id, fallback)


#: 英文稿件的章节标题（编号按各文体自己的顺序）。
_EN_TITLES: dict[str, dict[str, str]] = {
    "review": {
        "abstract": "Abstract",
        "introduction": "1 Introduction",
        "method": "2 Review Methodology",
        "landscape": "3 State of the Art",
        "synthesis": "4 Synthesis and Discussion",
        "gaps": "5 Research Gaps and Agenda",
        "limitations": "6 Limitations",
    },
    "empirical": {
        "abstract": "Abstract",
        "introduction": "1 Introduction",
        "related": "2 Related Work",
        "method": "3 Method",
        "results": "4 Results",
        "discussion": "5 Discussion",
        "limitations": "6 Limitations and Conclusion",
    },
    "proposal": {
        "abstract": "Abstract",
        "background": "1 Background and Problem",
        "review": "2 Related Work",
        "plan": "3 Research Design",
        "timeline": "4 Plan and Expected Outcomes",
        "feasibility": "5 Feasibility and Ethics",
        "limitations": "6 Limitations",
    },
}


def _evidence_pool(works: list[Work], limit: int = 40) -> list[dict[str, Any]]:
    """按"被引数 + 年份新"排序的证据池，写作时从这里取证。"""
    def score(work: Work) -> tuple[int, int]:
        return (work.citation_count or 0, work.year or 0)

    ranked = sorted(works, key=score, reverse=True)[:limit]
    return [
        {
            "key": work.key,
            "cite_key": work.cite_key,
            "year": work.year,
            "authors": _author_hint(work),
            "title": work.title,
            "venue": work.venue,
            "citation_count": work.citation_count,
            "has_abstract": bool(work.abstract),
            "doi": work.doi,
        }
        for work in ranked
    ]


def _author_hint(work: Work) -> str:
    family = work.first_author_family()
    if not family:
        return ""
    if len(work.authors) > 1:
        return f"{family} 等" if any("\u3400" <= ch <= "\u9fff" for ch in family) else f"{family} et al."
    return family


def build_outline(
    state: dict[str, Any],
    works: list[Work],
    *,
    sections: int | None = None,
    target_words: int | None = None,
) -> dict[str, Any]:
    """生成结构化大纲。

    ``sections`` 只允许在模板基础上**增加/合并**（这里按模板实现，参数保留给
    以后扩展），``target_words`` 缺省取运行记录里的值。
    """
    doc_type = (state.get("doc_type") or "review").strip().lower()
    template = TEMPLATES.get(doc_type) or TEMPLATES["review"]
    total_words = int(target_words or state.get("target_words") or 6000)
    assign_cite_keys(works)

    rendered: list[dict[str, Any]] = []
    language = (state.get("language") or "zh").strip().lower()
    for spec in template:
        words = max(60, int(round(total_words * float(spec["weight"]))))
        rendered.append(
            {
                "id": spec["id"],
                "title": _section_title(doc_type, spec["id"], spec["title"], language),
                "target_words": words,
                "citation_budget": int(spec["citations"]),
                "purpose": spec["purpose"],
                "must_cover": list(spec["must"]),
                "allowed_markers": "使用 [@cite_key] 标注引用；不得无标记陈述他人结论。",
            }
        )
    # 字数配额按比例缩放后可能和 total_words 有几十字出入，差额记在最后
    allocated = sum(item["target_words"] for item in rendered)
    if rendered:
        rendered[-1]["target_words"] += total_words - allocated
    pool = _evidence_pool(works)
    return {
        "schema": "atrix-research/outline/1",
        "doc_type": doc_type,
        "language": state.get("language") or "zh",
        "style": state.get("style") or "gbt7714",
        "target_words": total_words,
        "sections": rendered,
        "evidence_pool": pool,
        "evidence_count": len(pool),
        "citation_total_budget": sum(item["citation_budget"] for item in rendered),
        "writing_contract": {
            "markers": "正文引用统一写成 [@cite_key]；cite_key 见 evidence_pool 与 references.json。",
            "unmarked_claims": "不允许：任何他人的发现、数字、结论都必须带标记。",
            "placeholders": "不允许保留 TODO / TBD / [EVIDENCE] / XXX 等占位符。",
            "numbers": "实验/统计数字必须来自数据或引文；不得编造。",
            "hedging": "结论强度要与证据强度匹配，禁止 '首次证明''彻底解决' 这类表述。",
        },
    }


def render_section_contract(outline: dict[str, Any]) -> str:
    """给写作模型看的逐节契约（也是 outline.md 的主体）。"""
    lines = [
        f"# 写作大纲：{outline.get('doc_type')} / {outline.get('language')}",
        "",
        f"- 目标总字数：{outline.get('target_words')}",
        f"- 引用总预算：{outline.get('citation_total_budget')}",
        f"- 证据池：{outline.get('evidence_count')} 条",
        "",
        "## 逐节契约",
        "",
    ]
    for section in outline.get("sections") or []:
        lines.append(f"### {section['title']}（约 {section['target_words']} 字 / ≥{section['citation_budget']} 处引用）")
        lines.append(f"- 目的：{section['purpose']}")
        lines.append("- 必须覆盖：" + "；".join(section["must_cover"]))
        lines.append("")
    lines.append("## 写作硬约束")
    lines.append("")
    for key, value in (outline.get("writing_contract") or {}).items():
        lines.append(f"- `{key}`：{value}")
    return "\n".join(lines).rstrip() + "\n"


def outline_markdown(outline: dict[str, Any]) -> str:
    """outline.md：契约 + 证据池表格（供模型与人工共用）。"""
    lines = [render_section_contract(outline), "## 证据池", ""]
    lines.append("| cite_key | 年份 | 作者 | 标题 | 来源 | 被引 | 有摘要 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in outline.get("evidence_pool") or []:
        title = (item["title"] or "").replace("|", "\\|")[:110]
        venue = (item["venue"] or "").replace("|", "\\|")[:40]
        lines.append(
            f"| {item['cite_key']} | {item['year'] or ''} | {item['authors']} | {title} | {venue} | "
            f"{item['citation_count'] if item['citation_count'] is not None else ''} | "
            f"{'是' if item['has_abstract'] else '否'} |"
        )
    return "\n".join(lines).rstrip() + "\n"
