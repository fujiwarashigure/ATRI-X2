"""稿件质量检查（QA）：把"看起来像论文"变成"可以逐条核对"。

检查项分三级：

* ``error``：结构性问题（引用键不存在、占位符残留、引用表未闭合），必须修；
* ``warn``：质量风险（某节引用不足、长度偏差大、出现绝对化措辞）；
* ``info``：可读性建议。

这些判定全部基于规则，因此换任何模型写作都能得到同一套结论。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["BANNED_PHRASES", "check_draft", "qa_markdown"]

#: 绝对化/夸大措辞（中英）。命中只 warn，因为确实存在合法用法，交给人看。
BANNED_PHRASES = [
    "首次证明",
    "彻底解决",
    "完全消除",
    "毫无疑问",
    "100%",
    "proves that",
    "revolutionary",
    "unprecedented",
    "solves all",
    "guarantees",
    "显然",
    "众所周知",
]

_MARKER_RE = re.compile(r"\[@([A-Za-z0-9_\-\u4e00-\u9fff]+)\]")
_NUM_MARKER_RE = re.compile(r"(?<![\w\[])\[(\d{1,3})\]")
_SECTION_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)
_PLACEHOLDER_PATTERNS = [
    (re.compile(r"\bTODO\b", re.IGNORECASE), "TODO"),
    (re.compile(r"\bTBD\b", re.IGNORECASE), "TBD"),
    (re.compile(r"\bFIXME\b", re.IGNORECASE), "FIXME"),
    (re.compile(r"\[EVIDENCE[^\]]*\]", re.IGNORECASE), "[EVIDENCE]"),
    (re.compile(r"\bXXX+\b"), "XXX"),
    (re.compile(r"\{\{[^}]*\}\}"), "{{...}}"),
    (re.compile(r"\bNone\b"), "None（常见的格式化失败残留）"),
]


def _split_sections(text: str) -> list[dict[str, Any]]:
    """按二级标题切节；没标题时整篇算一节。"""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return [{"title": "（全文）", "level": 0, "body": text}]
    sections: list[dict[str, Any]] = []
    # 第一个标题之前的内容（标题/作者等）单独成节
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append({"title": "（前言）", "level": 0, "body": preamble})
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            {
                "title": match.group(2).strip(),
                "level": len(match.group(1)),
                "body": text[start:end].strip(),
            }
        )
    return sections


def _word_count(text: str) -> int:
    """中英混合字数：CJK 按字计，其它按词计。"""
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + latin


def _aggregate(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把可合并的同类问题收成一条（保留样例，计数写进 message）。"""
    groupable = {"REFERENCE_NO_IDENTIFIER", "OVERCLAIM_PHRASE"}
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["code"]] = counts.get(issue["code"], 0) + 1
    merged: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for issue in issues:
        code = issue["code"]
        if code not in groupable or counts.get(code, 0) <= 2:
            merged.append(issue)
            continue
        if code in emitted:
            continue
        emitted.add(code)
        copies = [item for item in issues if item["code"] == code]
        detail = {
            **issue,
            "count": len(copies),
            "message": f"{issue['message']}（共 {len(copies)} 处）",
            "occurrences": [item.get("where") or item["message"] for item in copies[:10]],
        }
        merged.append(detail)
    return merged


def check_draft(
    draft: str,
    references: dict[str, Any],
    outline: dict[str, Any] | None = None,
    *,
    style: str = "gbt7714",
    min_section_citations: int | None = None,
    banned_phrases: list[str] | None = None,
) -> dict[str, Any]:
    """对稿件做结构化检查，返回问题清单与总分。"""
    issues: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, *, where: str = "", fix: str = "") -> None:
        issues.append(
            {"severity": severity, "code": code, "message": message, "where": where, "fix": fix}
        )

    known: dict[str, str] = {}
    if references:
        for entry in references.get("entries") or []:
            known[str(entry.get("cite_key"))] = str(entry.get("work_key"))
    cite_keys = set(known)
    sections = _split_sections(draft)
    markers_found: dict[str, int] = {}

    for section in sections:
        body = section["body"]
        found = _MARKER_RE.findall(body)
        numeric = _NUM_MARKER_RE.findall(body)
        for marker in found:
            markers_found[marker] = markers_found.get(marker, 0) + 1
            if marker not in cite_keys:
                add(
                    "error",
                    "CITATION_UNKNOWN_KEY",
                    f"正文引用了不存在的 cite_key: {marker}",
                    where=section["title"],
                    fix="改用以 references.json 里存在的 key，或把该文献加入文献库后重新生成引用。",
                )
        if numeric:
            max_index = len(references.get("entries") or []) if references else 0
            for raw in numeric:
                if int(raw) > max_index:
                    add(
                        "error",
                        "CITATION_INDEX_OUT_OF_RANGE",
                        f"数字引用 [{raw}] 超出参考文献数量（{max_index}）",
                        where=section["title"],
                        fix="检查编号是否误用；正文编号必须与参考文献表一一对应。",
                    )
        budget = 0
        if outline:
            target = next(
                (
                    item
                    for item in outline.get("sections") or []
                    if item.get("title") == section["title"]
                ),
                None,
            )
            if target:
                budget = int(target.get("citation_budget") or 0)
        minimum = min_section_citations if min_section_citations is not None else budget
        citation_count = len(found) + len(numeric)
        if minimum and citation_count < minimum and section["level"] >= 2:
            add(
                "warn",
                "SECTION_CITATION_LOW",
                f"该节引用 {citation_count} 处，低于预算 {minimum}",
                where=section["title"],
                fix="补充证据或把无引用的推测性表述删掉/降级为展望。",
            )

    # 引用闭合：参考文献里没被正文引用的条目
    uncited = [entry for entry in (references.get("entries") or []) if entry.get("cite_key") not in markers_found]
    if uncited and markers_found:
        sample = ", ".join(str(entry.get("cite_key")) for entry in uncited[:5])
        add(
            "warn",
            "REFERENCES_UNUSED",
            f"{len(uncited)} 条参考文献未被正文引用（示例：{sample}）",
            fix="删除未引用条目，或在正文相应位置补引用（至少保持一一对应）。",
        )

    # 占位符与绝对化措辞
    for pattern, label in _PLACEHOLDER_PATTERNS:
        count = len(pattern.findall(draft))
        if count:
            add(
                "error",
                "PLACEHOLDER_REMAINS",
                f"残留占位符 {label} × {count}",
                fix="删除占位符并写入真实内容；未完成的部分应删节而不是留空。",
            )
    phrases = banned_phrases if banned_phrases is not None else BANNED_PHRASES
    for phrase in phrases:
        if phrase and phrase.lower() in draft.lower():
            add(
                "warn",
                "OVERCLAIM_PHRASE",
                f"出现绝对化措辞：{phrase}",
                fix="改为与证据强度匹配的表述（如'在…条件下观察到'）。",
            )

    # 摘要长度
    abstract = next(
        (section["body"] for section in sections if section["title"] in ("摘要", "Abstract")),
        "",
    )
    if abstract:
        abstract_words = _word_count(abstract)
        if not 120 <= abstract_words <= 400:
            add(
                "warn",
                "ABSTRACT_LENGTH",
                f"摘要 {abstract_words} 字/词，常见要求为 150-300（中文）或 150-250（英文）",
                where="摘要",
                fix="压缩背景与方法描述，保留问题、方法、主要结果、结论。",
            )

    # 总长度
    total_words = _word_count(draft)
    if outline:
        target = int(outline.get("target_words") or 0)
        if target:
            deviation = (total_words - target) / target
            if abs(deviation) > 0.30:
                add(
                    "warn",
                    "LENGTH_DEVIATION",
                    f"总字数 {total_words}，偏离目标 {target}（{deviation:+.0%}）",
                    fix="按逐节配额增删，优先补齐引用预算未达标的节。",
                )

    # 参考文献格式粗检
    for entry in references.get("entries") or [] if references else []:
        text = str(entry.get("text") or "").strip()
        if not text:
            add("error", "REFERENCE_EMPTY", f"参考文献第 {entry.get('index')} 条为空")
            continue
        if re.search(r"\b(None|undefined|nan)\b", text, re.IGNORECASE):
            add(
                "error",
                "REFERENCE_DIRTY",
                f"参考文献第 {entry.get('index')} 条含格式化残留：{text[:80]}",
                fix="检查该文献的元数据（作者/年份/期刊）是否缺失。",
            )
        if style == "gbt7714" and not re.match(r"^\[\d+\]", text):
            add(
                "error",
                "REFERENCE_NUMBERING",
                f"GB/T 7714 条目缺少 [n] 编号：{text[:60]}",
            )
        if not re.search(r"(doi\.org/|DOI:|https?://)", text):
            add(
                "warn",
                "REFERENCE_NO_IDENTIFIER",
                f"参考文献第 {entry.get('index')} 条没有 DOI/URL，可核查性弱",
            )

    # 同类"缺标识符"提示合并成一条，否则 30 条文献就能把问题列表淹没
    issues = _aggregate(issues)

    # 汇总评分
    errors = sum(1 for issue in issues if issue["severity"] == "error")
    warns = sum(1 for issue in issues if issue["severity"] == "warn")
    score = max(0, 100 - errors * 12 - warns * 4)
    if errors:
        verdict = "FAIL"
    elif warns:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "schema": "atrix-research/qa/1",
        "verdict": verdict,
        "score": score,
        "errors": errors,
        "warnings": warns,
        "stats": {
            "words": total_words,
            "sections": len([section for section in sections if section["level"] >= 2]),
            "citations": sum(markers_found.values()),
            "unique_cited": len(markers_found),
            "reference_entries": len((references or {}).get("entries") or []),
        },
        "issues": issues,
    }


def qa_markdown(report: dict[str, Any]) -> str:
    """把 QA 结果渲染成可放进交付物的一页。"""
    lines = [
        "## 稿件质量检查",
        "",
        f"- 结论：**{report['verdict']}**（得分 {report['score']}）",
        f"- 错误 {report['errors']} 项 / 警告 {report['warnings']} 项",
        f"- 统计：{report['stats']}",
        "",
    ]
    if not report.get("issues"):
        lines.append("未发现问题。")
        return "\n".join(lines) + "\n"
    lines.append("| 级别 | 代码 | 位置 | 说明 | 修法 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for issue in report["issues"]:
        lines.append(
            f"| {issue['severity']} | {issue['code']} | {issue.get('where') or '-'} | "
            f"{issue['message']} | {issue.get('fix') or '-'} |"
        )
    return "\n".join(lines).rstrip() + "\n"
