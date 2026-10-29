"""``compose`` 硬工具：结构化成稿 + 契约校验 + 自动质检。

写作环节里"模型质量"体现在**内容**（观点、证据选择、论证），而**格式**（章节顺序、
引用标记位置、引用预算、字数、占位符）本来就不该由模型负责——那正是最容易在换模型
后崩掉的部分。因此这里把契约固定成一份结构化输入：

    {"sections": [
        {"heading": "引言",
         "paragraphs": [
            {"text": "检索增强已被证明……", "cites": ["zhang2023rag", "liu2022survey"]}
         ]}
    ]}

模型只产出 ``text`` 与 ``cites``，引擎负责：校验标题来自大纲、引用键来自
``references.json``、按大纲顺序重排、把 ``cites`` 渲染成 ``[@key]``、统计引用预算、
拒绝占位符、写盘后用 ``qa.py`` 再判一次。同样的输入在任何模型下得到**逐字节相同**
的 Markdown。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .errors import ARG_INVALID, ResearchError
from .markdown_lite import parse as parse_markdown
from .qa import check_draft, qa_markdown
from .runtime import MARKER_RE, draft_path, open_run, result, word_count
from .store import now_iso

__all__ = ["compose_markdown", "render", "tool_compose", "validate"]

_PLACEHOLDER_RE = re.compile(
    r"\bTODO\b|\bTBD\b|\bFIXME\b|\[EVIDENCE[^\]]*\]|\bXXX+\b|\{\{[^}]*\}\}", re.IGNORECASE
)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _cites_of(paragraph: dict[str, Any]) -> list[str]:
    """收集一段文字声明的引用键（``cites`` / ``cite`` 都接受，允许字符串）。"""
    raw = paragraph.get("cites", paragraph.get("cite"))
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [part for part in re.split(r"[,;，；\s]+", raw) if part]
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = [str(raw)]
    out: list[str] = []
    for item in items:
        key = item.strip().strip("[]@").strip()
        if key and key not in out:
            out.append(key)
    return out


def _extract_inline(text: str) -> tuple[str, list[str]]:
    """把文字里已经写好的 ``[@key]`` 抽出来，避免渲染时重复标记。"""
    keys = MARKER_RE.findall(text)
    cleaned = MARKER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, keys


def validate(
    payload: Any,
    outline: dict[str, Any] | None,
    references: dict[str, Any] | None,
    *,
    allow_extra: bool = False,
) -> dict[str, Any]:
    """校验结构化稿件，返回 ``{ok, errors, warnings, normalized, stats}``。

    错误分两级：``errors`` 是**拒绝写入**的硬缺陷（未知引用键、未知标题、占位符），
    ``warnings`` 是写入但会被质检/评审看到的风险（引用预算不足、字数偏差、顺序被重排）。
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def err(code: str, path: str, message: str, fix: str) -> None:
        errors.append({"code": code, "path": path, "message": message, "fix": fix})

    def warn(code: str, path: str, message: str, fix: str) -> None:
        warnings.append({"code": code, "path": path, "message": message, "fix": fix})

    empty = {"ok": False, "errors": errors, "warnings": warnings, "normalized": None, "stats": {}}
    if not isinstance(payload, dict):
        err("E_PAYLOAD", "$", "payload 必须是对象", '形如 {"sections": [...]}')
        return empty
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        err(
            "E_SECTIONS",
            "$.sections",
            "sections 必须是非空数组",
            '{"sections":[{"heading":"引言","paragraphs":[...]}]}',
        )
        return empty

    outline_sections = list((outline or {}).get("sections") or [])
    outline_titles = [str(item.get("title") or "") for item in outline_sections]
    budget_by_title = {
        str(item.get("title") or ""): int(item.get("citation_budget") or 0) for item in outline_sections
    }
    words_by_title = {
        str(item.get("title") or ""): int(item.get("target_words") or 0) for item in outline_sections
    }
    known_keys = set((references or {}).get("by_cite_key") or {})
    if not known_keys:
        known_keys = {
            str(entry.get("cite_key"))
            for entry in (references or {}).get("entries") or []
            if entry.get("cite_key")
        }

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_citations = 0
    for index, section in enumerate(sections):
        path = f"$.sections[{index}]"
        if not isinstance(section, dict):
            err("E_SECTION_TYPE", path, "小节必须是对象", '{"heading": "...", "paragraphs": [...]}')
            continue
        heading = _as_text(section.get("heading") or section.get("title"))
        if not heading:
            err("E_HEADING", path, "缺少 heading", "给出大纲里的节标题")
            continue
        if heading in seen:
            err("E_DUP_HEADING", path, f"节标题重复: {heading}", "同一节只写一次；续写请合并到同一小节")
            continue
        seen.add(heading)
        if outline_titles and heading not in outline_titles:
            if not allow_extra:
                err(
                    "E_UNKNOWN_HEADING",
                    path,
                    f"大纲里没有这一节: {heading}",
                    "改用 outline.json 的节标题；确需新增子节时传 allow_extra=true",
                )
                continue
            warn("W_EXTRA_HEADING", path, f"新增了大纲外的小节: {heading}", "确认这是有意加的子节，而非写错标题")

        paragraphs_raw = section.get("paragraphs", section.get("claims"))
        if not isinstance(paragraphs_raw, list) or not paragraphs_raw:
            err("E_PARAGRAPHS", path, "paragraphs/claims 必须是非空数组", "每节至少一段，每段给 text")
            continue
        paragraphs: list[dict[str, Any]] = []
        section_citations = 0
        for p_index, paragraph in enumerate(paragraphs_raw):
            p_path = f"{path}.paragraphs[{p_index}]"
            if isinstance(paragraph, str):
                paragraph = {"text": paragraph}
            if not isinstance(paragraph, dict):
                err("E_PARAGRAPH_TYPE", p_path, "段落必须是对象或字符串", '{"text": "...", "cites": ["key"]}')
                continue
            text, inline_keys = _extract_inline(_as_text(paragraph.get("text")))
            if not text:
                err("E_EMPTY_TEXT", p_path, "段落 text 为空", "写入真实内容；未完成的节不要提交")
                continue
            if _PLACEHOLDER_RE.search(text):
                err(
                    "E_PLACEHOLDER",
                    p_path,
                    "段落含占位符（TODO/TBD/FIXME/[EVIDENCE]/XXX/{{...}}）",
                    "删掉占位符，写入真实内容",
                )
                continue
            cites = _cites_of(paragraph)
            for key in inline_keys:
                if key not in cites:
                    cites.append(key)
            unknown = [key for key in cites if known_keys and key not in known_keys]
            if unknown:
                err(
                    "E_UNKNOWN_CITE",
                    p_path,
                    f"引用了参考文献表里没有的 key: {', '.join(unknown)}",
                    "只用 references.json 的 by_cite_key；缺文献先回去检索/筛选，不要编造 key",
                )
                continue
            cites = list(dict.fromkeys(cites))
            paragraphs.append({"text": text, "cites": cites})
            section_citations += len(cites)
        if not paragraphs:
            # 整节段落都被拒时不再计入小节，避免产生空标题
            continue
        normalized.append({"heading": heading, "paragraphs": paragraphs})
        total_citations += section_citations

        budget = budget_by_title.get(heading, 0)
        if budget and section_citations < budget:
            warn(
                "W_CITE_BUDGET_LOW",
                path,
                f"该节引用 {section_citations} 处，低于大纲预算 {budget}",
                "补充证据段（每段 1-2 个 cite_key），或把无证据的推测降级为展望",
            )
        if budget and section_citations > budget * 4:
            warn(
                "W_CITE_BUDGET_HIGH",
                path,
                f"该节引用 {section_citations} 处，超过预算 {budget} 的 4 倍",
                "合并同一文献的重复引用；堆引用不等于有论证",
            )
        target = words_by_title.get(heading, 0)
        section_words = sum(word_count(paragraph["text"]) for paragraph in paragraphs)
        if target and not (0.5 * target <= section_words <= 2.0 * target):
            warn(
                "W_SECTION_WORDS",
                path,
                f"该节 {section_words} 字，偏离配额 {target} 较多",
                "按配额增删；偏离过大时质检会报 LENGTH_DEVIATION",
            )

    # 顺序：引擎按大纲顺序重排，保证换模型后结构一致
    if outline_titles:
        order = {title: position for position, title in enumerate(outline_titles)}
        ordered = sorted(
            normalized,
            key=lambda item: (order.get(item["heading"], len(order)), item["heading"]),
        )
        if [item["heading"] for item in ordered] != [item["heading"] for item in normalized]:
            warn(
                "W_REORDERED",
                "$.sections",
                "小节顺序与大纲不一致，已按大纲顺序重排",
                "按 outline.json 的顺序提交可避免该提示",
            )
        normalized = ordered
        missing = [title for title in outline_titles if title not in {item["heading"] for item in normalized}]
        if missing:
            warn(
                "W_MISSING_SECTIONS",
                "$.sections",
                f"本次提交未覆盖 {len(missing)} 节：{', '.join(missing[:6])}",
                "逐节提交；action=save 要求覆盖全部小节，append 允许分批",
            )

    stats = {
        "sections": len(normalized),
        "paragraphs": sum(len(item["paragraphs"]) for item in normalized),
        "citations": total_citations,
        "words": sum(
            word_count(paragraph["text"]) for item in normalized for paragraph in item["paragraphs"]
        ),
    }
    if outline and outline.get("target_words") and stats["sections"] == len(outline_titles):
        target = int(outline["target_words"])
        if target and not (0.6 * target <= stats["words"] <= 1.6 * target):
            warn(
                "W_TOTAL_WORDS",
                "$",
                f"全文 {stats['words']} 字，偏离目标 {target}",
                "按逐节配额调整",
            )
    return {"ok": not errors, "errors": errors, "warnings": warnings, "normalized": normalized, "stats": stats}


def render(normalized: list[dict[str, Any]], *, level: int = 2) -> str:
    """把校验后的结构渲染成 Markdown（确定性：输入相同则逐字节相同）。"""
    lines: list[str] = []
    for section in normalized:
        lines.append(f"{'#' * level} {section['heading']}")
        lines.append("")
        for paragraph in section["paragraphs"]:
            text = paragraph["text"]
            if paragraph["cites"]:
                markers = "".join(f"[@{key}]" for key in paragraph["cites"])
                lines.append(f"{text} {markers}")
            else:
                lines.append(text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def compose_markdown(
    payload: Any,
    outline: dict[str, Any] | None,
    references: dict[str, Any] | None,
    *,
    allow_extra: bool = False,
) -> tuple[str, dict[str, Any]]:
    """校验并渲染，返回 ``(markdown, 校验结果)``；有 error 时 markdown 为空串。"""
    checked = validate(payload, outline, references, allow_extra=allow_extra)
    if not checked["ok"]:
        return "", checked
    return render(checked["normalized"]), checked


def _existing_headings(draft: str) -> set[str]:
    return {
        block.text.strip()
        for block in parse_markdown(draft)
        if block.kind == "heading" and block.text.strip()
    }


def _load_payload(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and not text.startswith(("{", "[")):
            if os.path.isfile(text):
                with open(text, "r", encoding="utf-8") as handle:
                    return json.load(handle)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ResearchError(
                ARG_INVALID,
                f"sections 不是合法 JSON: {exc}",
                hint='形如 {"sections":[{"heading":"引言","paragraphs":[{"text":"...","cites":["key"]}]}]}',
            ) from None
    return value


def tool_compose(args: Any) -> dict[str, Any]:
    action = (getattr(args, "action", "") or "validate").strip().lower()
    if action not in ("validate", "append", "save"):
        raise ResearchError(ARG_INVALID, f"未知 action: {action}", hint="可用 action: validate / append / save。")
    store = open_run(getattr(args, "run", ""))
    state = store.load_state()
    outline = store.read("outline.json", required=False, default=None)
    references = store.read("references.json", required=False, default=None)
    payload = _load_payload(getattr(args, "sections", None))
    if payload is None:
        raise ResearchError(
            ARG_INVALID,
            "缺少 sections",
            hint="传 JSON 对象/数组，或一个保存该 JSON 的文件路径。",
        )
    if not isinstance(payload, dict):
        payload = {"sections": payload}
    allow_extra = str(getattr(args, "allow_extra", "") or "").strip().lower() in ("1", "true", "yes", "on")

    checked = validate(payload, outline, references, allow_extra=allow_extra)
    checked["run"] = state.get("id")
    checked["action"] = action
    checked["checked_at"] = now_iso()
    artifact_list: list[dict[str, Any]] = []
    degraded: list[str] = []
    if not checked["ok"]:
        checked["next"] = ["按 errors[].fix 修正结构化输入后重新提交；validate 不会写盘"]
        degraded.append("CONTRACT_VIOLATION")
        return result(checked, artifacts=[], degraded=degraded)

    markdown = render(checked["normalized"])
    if action == "validate":
        checked["markdown_preview"] = markdown[:2000]
        checked["markdown_chars"] = len(markdown)
        checked["next"] = ["校验通过；用 action=append 写入（或继续提交其余小节）"]
        return result(checked)

    existing = store.read("draft.md", required=False, default="") or ""
    if action == "append":
        clashes = [
            item["heading"] for item in checked["normalized"] if item["heading"] in _existing_headings(existing)
        ]
        if clashes:
            checked["ok"] = False
            checked["errors"] = [
                {
                    "code": "E_ALREADY_WRITTEN",
                    "path": "$.sections",
                    "message": f"这些小节已在正文里：{', '.join(clashes)}",
                    "fix": "append 只写没写过的小节；要重写整节请用 action=save（整体覆盖）",
                }
            ]
            checked["next"] = ["改用 action=save 整体覆盖，或只提交尚未写入的小节"]
            degraded.append("CONTRACT_VIOLATION")
            return result(checked, degraded=degraded)
        merged = (existing.rstrip() + "\n\n" + markdown).strip() + "\n"
    else:  # save：整体覆盖，要求覆盖全部大纲小节
        outline_titles = [str(item.get("title") or "") for item in (outline or {}).get("sections") or []]
        missing = [title for title in outline_titles if title not in {item["heading"] for item in checked["normalized"]}]
        if outline_titles and missing:
            checked["ok"] = False
            checked["errors"] = [
                {
                    "code": "E_INCOMPLETE_SAVE",
                    "path": "$.sections",
                    "message": f"save 需要覆盖全部 {len(outline_titles)} 节，缺：{', '.join(missing[:8])}",
                    "fix": "补齐缺的小节后重试；分批写请用 action=append",
                }
            ]
            checked["next"] = ["补齐缺失小节，或改用 action=append"]
            degraded.append("CONTRACT_VIOLATION")
            return result(checked, degraded=degraded)
        merged = markdown

    artifact_list.append(store.write("draft.md", merged))
    state["stage"] = "writing"
    state["counts"] = {**(state.get("counts") or {}), "draft_words": word_count(merged)}
    store.save_state(state)

    # 写盘后立刻跑同一套质检：模型不需要"自我感觉良好"，结论来自 qa.py
    if references and references.get("entries"):
        qa = check_draft(
            merged,
            references,
            outline,
            style=(references.get("style") or state.get("style") or "gbt7714"),
        )
        qa["run"] = state.get("id")
        qa["checked_at"] = now_iso()
        artifact_list.append(store.write("qa.json", qa))
        artifact_list.append(store.write("qa.md", qa_markdown(qa)))
        state["counts"] = {
            **(state.get("counts") or {}),
            "qa_score": qa["score"],
            "qa_errors": qa["errors"],
        }
        if qa["errors"]:
            state["stage"] = "check"
        store.save_state(state)
        if qa["verdict"] == "FAIL":
            degraded.append("QA_FAILED")
        checked["qa"] = {
            "verdict": qa["verdict"],
            "score": qa["score"],
            "errors": qa["errors"],
            "warnings": qa["warnings"],
            "issues": qa["issues"][:20],
        }
        remaining = [
            str(item.get("title") or "")
            for item in (outline or {}).get("sections") or []
            if str(item.get("title") or "") not in _existing_headings(merged)
        ]
        checked["remaining_sections"] = remaining
        checked["next"] = (
            (["按 qa.issues 的 fix 修正后重新提交"] if qa["errors"] else [])
            + ([f"继续 append 剩余小节：{', '.join(remaining[:5])}"] if remaining else [])
            + ["research_write action=check 复核", "research_audit 确定性评审"]
        )
    else:
        checked["next"] = ["先 research_cite 生成参考文献，再写下一节"]
    return result(checked, artifacts=artifact_list, degraded=degraded)
