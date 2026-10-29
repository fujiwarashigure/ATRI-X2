"""``flow`` 硬工具：把"下一步该做什么"变成可计算的状态机。

为什么需要它：多轮科研流程里最容易被模型带偏的不是单步计算，而是**顺序**——
弱模型会跳过筛选直接写引言、在没核验引文时就导出、或者反复重跑检索。
以前这靠 ``academic_research`` 这份提示词约束，等于把质量控制交给了模型能力。

现在顺序由本模块计算：读运行目录里真实存在的产物，输出

    {"stage", "progress", "steps": [{status, action:{tool,args}, ...}], "next": [...], "blockers": [...]}

模型只需要执行 ``next[0].action``，不参与判断"现在到第几步"。同一份产物状态
在任何模型/任何机器上都会得到同一份计划（纯函数，无网络、无随机）。
"""

from __future__ import annotations

import os
from typing import Any

from .errors import ARG_INVALID, ResearchError
from .runtime import MARKER_RE, open_run, result

__all__ = ["collect_facts", "plan", "flow_markdown", "tool_flow"]

#: 有序步骤（顺序即依赖；每一步只依赖前面步骤的产物）。
STEP_ORDER = ("search", "screen", "verify", "cite", "outline", "write", "check", "audit", "export")

_STEP_TITLES = {
    "search": "多源检索与去重",
    "screen": "PRISMA 纳排筛选",
    "verify": "引文核验（DOI 反查）",
    "cite": "生成参考文献",
    "outline": "生成逐节写作契约",
    "write": "按契约成稿",
    "check": "稿件质检",
    "audit": "确定性评审打分",
    "export": "装配报告与导出",
}


def _headings(markdown_text: str) -> list[str]:
    """取出正文里的标题（``##`` 及以上），用于比对大纲完成度。"""
    titles: list[str] = []
    for line in (markdown_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            if text:
                titles.append(text)
    return titles


def collect_facts(store: Any) -> dict[str, Any]:
    """从运行目录收集计划所需的**全部事实**（只读，不修改任何文件）。"""
    state = store.load_state()

    def read(name: str) -> Any:
        return store.read(name, required=False, default=None)

    draft = store.read("draft.md", required=False, default="") or ""
    exports_dir = store.path("exports")
    exports: list[str] = []
    if os.path.isdir(exports_dir):
        exports = sorted(name for name in os.listdir(exports_dir) if not name.startswith("."))
    outline = read("outline.json") or {}
    qa = read("qa.json") or {}
    audit = read("audit.json") or {}
    screening = read("screening.json") or {}
    verification = read("verification.json") or {}
    references = read("references.json") or {}
    library = read("library.json") or {}
    return {
        "state": state,
        "library": library,
        "screening": screening,
        "verification": verification,
        "references": references,
        "outline": outline,
        "draft": draft,
        "qa": qa,
        "audit": audit,
        "has_report": store.exists("report.md"),
        "exports": exports,
        "draft_headings": _headings(draft),
        "draft_citations": len(MARKER_RE.findall(draft)),
    }


def _section_plan(outline: dict[str, Any], draft_headings: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """比对大纲与正文标题，返回（已完成小节，待写小节契约）。"""
    done: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = []
    written = set(draft_headings)
    for section in outline.get("sections") or []:
        title = str(section.get("title") or "")
        if not title:
            continue
        item = {
            "heading": title,
            "target_words": int(section.get("target_words") or 0),
            "citation_budget": int(section.get("citation_budget") or 0),
            "must_cover": list(section.get("must_cover") or []),
        }
        if title in written:
            done.append(item)
        else:
            todo.append(item)
    return done, todo


def plan(facts: dict[str, Any]) -> dict[str, Any]:
    """把事实映射成有序计划（纯函数：同样输入必然同样输出）。"""
    state = facts.get("state") or {}
    run_id = str(state.get("id") or "")
    library = facts.get("library") or {}
    screening = facts.get("screening") or {}
    verification = facts.get("verification") or {}
    references = facts.get("references") or {}
    outline = facts.get("outline") or {}
    qa = facts.get("qa") or {}
    audit = facts.get("audit") or {}
    draft = facts.get("draft") or ""
    draft_words = int(state.get("counts", {}).get("draft_words") or 0)
    if not draft_words and draft:
        from .runtime import word_count

        draft_words = word_count(draft)

    steps: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []

    def add(step_id: str, status: str, *, action: dict[str, Any] | None = None, detail: str = "", gate: str = "") -> None:
        steps.append(
            {
                "step": step_id,
                "title": _STEP_TITLES[step_id],
                "status": status,
                "gate": gate,
                "detail": detail,
                "action": action,
            }
        )

    # 1. search
    works_count = len(library.get("works") or [])
    if works_count:
        add("search", "done", detail=f"文献库 {works_count} 条")
    else:
        add(
            "search",
            "ready",
            action={
                "tool": "research_search",
                "args": {
                    "run": run_id,
                    "queries": ["<从研究问题拆出的 2-5 组关键词，中英文各一组>"],
                    "limit": 20,
                },
            },
            detail="还没有文献库",
        )

    # 2. screen
    counts = screening.get("counts") or {}
    pending = int(counts.get("excluded_pending") or len(screening.get("pending_keys") or []))
    included = int(counts.get("included") or len(screening.get("included_keys") or []))
    if screening and included and not pending:
        add("screen", "done", gate=str(screening.get("gate") or ""), detail=f"纳入 {included} 条")
    elif screening and pending:
        add(
            "screen",
            "blocked",
            action={"tool": "research_screen", "args": {"run": run_id, "stage": "full_text"}},
            detail=f"{pending} 条待全文判定",
        )
        blockers.append(
            {
                "code": "SCREEN_PENDING",
                "message": f"全文筛选还有 {pending} 条未判定，流程不能进入写作",
                "fix": "对 pending_keys 逐条给 include/exclude 后重跑 research_screen stage=full_text",
            }
        )
    else:
        add(
            "screen",
            "ready",
            action={"tool": "research_screen", "args": {"run": run_id, "stage": "title_abstract"}},
            detail="还没有筛选结果",
        )

    # 3. verify
    verify_gate = str(verification.get("gate") or "")
    if verification and verify_gate != "FAIL":
        add(
            "verify",
            "done",
            gate=verify_gate,
            detail=f"通过率 {verification.get('verified_ratio')}（{verification.get('summary')}）",
        )
    elif verification and verify_gate == "FAIL":
        add("verify", "blocked", action={"tool": "research_verify", "args": {"run": run_id}}, detail="核验全部失败")
        blockers.append(
            {
                "code": "VERIFY_FAIL",
                "message": "引文核验闸门 FAIL（全部未跑通，通常是网络或数据源不可用）",
                "fix": "先 research_doctor action=env 检查网络，再重跑 research_verify",
            }
        )
    else:
        add("verify", "ready", action={"tool": "research_verify", "args": {"run": run_id}}, detail="还没有核验结果")

    # 4. cite
    entries = len(references.get("entries") or [])
    if entries:
        add("cite", "done", detail=f"{entries} 条（{references.get('style')}）")
    else:
        add("cite", "ready", action={"tool": "research_cite", "args": {"run": run_id}}, detail="还没有参考文献表")

    # 5. outline
    sections = outline.get("sections") or []
    if sections:
        add("outline", "done", detail=f"{len(sections)} 节 / 目标 {outline.get('target_words')} 字")
    else:
        add(
            "outline",
            "ready",
            action={"tool": "research_plan", "args": {"run": run_id, "action": "outline"}},
            detail="还没有写作大纲",
        )

    # 6. write
    done_sections, todo_sections = _section_plan(outline, facts.get("draft_headings") or [])
    target_words = int(outline.get("target_words") or state.get("target_words") or 0)
    if sections and not todo_sections and target_words and draft_words >= int(target_words * 0.6):
        add(
            "write",
            "done",
            detail=f"{len(done_sections)} 节 / {draft_words} 字 / {facts.get('draft_citations')} 处引用",
        )
    elif sections:
        add(
            "write",
            "ready",
            action={
                "tool": "research_compose",
                "args": {
                    "run": run_id,
                    "action": "append",
                    "sections": [
                        {
                            "heading": item["heading"],
                            "target_words": item["target_words"],
                            "citation_budget": item["citation_budget"],
                            "claims": [],
                        }
                        for item in (todo_sections or done_sections)[:2]
                    ],
                },
            },
            detail=(
                f"待写 {len(todo_sections)} 节；已完成 {len(done_sections)} 节"
                if todo_sections
                else f"已有 {len(done_sections)} 节但字数 {draft_words} 低于目标 {target_words}"
            ),
        )
    else:
        add("write", "todo", detail="先完成大纲")

    # 7. check
    qa_errors = int(qa.get("errors") or 0)
    qa_verdict = str(qa.get("verdict") or "")
    if qa and qa_verdict in ("PASS", "WARN") and not qa_errors:
        add("check", "done", gate=qa_verdict, detail=f"得分 {qa.get('score')}")
    elif qa and qa_errors:
        add(
            "check",
            "blocked",
            action={"tool": "research_write", "args": {"run": run_id, "action": "check"}},
            detail=f"还有 {qa_errors} 个 error",
        )
        blockers.append(
            {
                "code": "QA_ERRORS",
                "message": f"稿件质检有 {qa_errors} 个 error（必须修完才能交付）",
                "fix": "按 qa.json 的 issues[].fix 逐条修改，再重新 check",
            }
        )
    else:
        add(
            "check",
            "ready",
            action={"tool": "research_write", "args": {"run": run_id, "action": "check"}},
            detail="还没有质检结果",
        )

    # 8. audit
    audit_gate = str(audit.get("gate") or "")
    if audit and audit_gate != "FAIL":
        add("audit", "done", gate=audit_gate, detail=f"确定性评分 {audit.get('weighted_total')}")
    elif audit and audit_gate == "FAIL":
        add("audit", "blocked", action={"tool": "research_audit", "args": {"run": run_id}}, detail="存在硬性缺陷")
        blockers.append(
            {
                "code": "AUDIT_FAIL",
                "message": "确定性评审存在硬性缺陷（见 audit.json 的 blockers）",
                "fix": "按 audit.json 的 blockers[].fix 修复后重跑 research_audit",
            }
        )
    else:
        add("audit", "ready", action={"tool": "research_audit", "args": {"run": run_id}}, detail="还没有评审结果")

    # 9. export
    exports = facts.get("exports") or []
    if facts.get("has_report") and exports:
        add("export", "done", detail=f"{len(exports)} 个导出文件")
    else:
        add(
            "export",
            "ready",
            action={"tool": "research_export", "args": {"run": run_id, "action": "all"}},
            detail="还没有交付文件",
        )

    done_count = sum(1 for step in steps if step["status"] == "done")
    ready = [step for step in steps if step["status"] == "ready"]
    blocked = [step for step in steps if step["status"] == "blocked"]
    next_actions: list[dict[str, Any]] = []
    if blocked:
        next_actions = [dict(blocked[0]["action"] or {}, why=blocked[0]["detail"])]
    elif ready:
        next_actions = [dict(ready[0]["action"] or {}, why=ready[0]["detail"])]
    worst = "PASS"
    for step in steps:
        if step["gate"] == "FAIL":
            worst = "FAIL"
            break
        if step["gate"] == "WARN" and worst == "PASS":
            worst = "WARN"
    return {
        "schema": "atrix-research/flow/1",
        "run": run_id,
        "stage": state.get("stage") or "init",
        "progress": {
            "done": done_count,
            "total": len(steps),
            "percent": round(100.0 * done_count / len(steps), 1) if steps else 0.0,
        },
        "steps": steps,
        "next": next_actions,
        "blockers": blockers,
        "gate": "FAIL" if blockers else worst,
        "deterministic": True,
    }


def flow_markdown(plan_data: dict[str, Any]) -> str:
    """把计划渲染成人读的一页（也是给弱模型看的"照着做"清单）。"""
    progress = plan_data.get("progress") or {}
    lines = [
        f"# 研究流程计划（{plan_data.get('run')}）",
        "",
        f"- 阶段：`{plan_data.get('stage')}`",
        f"- 进度：{progress.get('done')}/{progress.get('total')}（{progress.get('percent')}%）",
        f"- 闸门：**{plan_data.get('gate')}**",
        "",
        "| # | 步骤 | 状态 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for index, step in enumerate(plan_data.get("steps") or [], start=1):
        lines.append(
            f"| {index} | {step['title']} | {step['status']} | {step.get('detail') or '-'} |"
        )
    if plan_data.get("blockers"):
        lines.append("")
        lines.append("## 阻塞项")
        for item in plan_data["blockers"]:
            lines.append(f"- **{item['code']}** {item['message']} → 修法：{item['fix']}")
    lines.append("")
    lines.append("> 本计划由引擎按产物存在性计算；**不要自己判断顺序**，照 `next` 执行即可。")
    return "\n".join(lines) + "\n"


def tool_flow(args: Any) -> dict[str, Any]:
    action = (getattr(args, "action", "") or "plan").strip().lower()
    if action == "list":
        from .config import list_runs, paths

        runs = list_runs()
        return result({"home": paths().home, "runs": runs[:20], "count": len(runs)})
    if action not in ("plan", "markdown"):
        raise ResearchError(ARG_INVALID, f"未知 action: {action}", hint="可用 action: plan / markdown / list。")
    store = open_run(getattr(args, "run", ""))
    facts = collect_facts(store)
    planned = plan(facts)
    artifacts: list[dict[str, Any]] = []
    if action == "markdown":
        artifacts.append(store.write("notes.md", flow_markdown(planned)))
        planned["markdown_written"] = "notes.md"
    if not planned["next"] and planned.get("gate") != "FAIL":
        planned["next"] = [
            {
                "tool": "research_export",
                "args": {"run": planned["run"], "action": "all"},
                "why": "流程已完成，重导出交付文件",
            }
        ]
    planned["hint"] = (
        "执行 next[0].action 后再次调用 research_flow；顺序由引擎计算，不要跳步。"
        if planned["gate"] != "FAIL"
        else "先修 blockers，再调用 research_flow。"
    )
    return result(planned, artifacts=artifacts)
