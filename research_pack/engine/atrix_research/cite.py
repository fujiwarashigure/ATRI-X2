"""参考文献格式化（APA 7 / GB/T 7714-2015）与引用键分配。

设计取舍：

* 只做"能从元数据确定推出"的格式，宁可比标准少一点修饰，也不猜（例如不做
  标题句首化改写，因为会在专有名词和缩写上出错；在 ``notes`` 里显式声明）。
* 引用键（``cite_key``）由"第一作者姓 + 年份"生成并保证唯一，供正文标记
  ``[@key]`` 使用；GB/T 的数字编号在最终排序后生成，因此同一份文献库可以
  同时导出两种样式。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from .models import Author, Work, has_cjk

__all__ = [
    "STYLES",
    "assign_cite_keys",
    "build_references",
    "format_apa",
    "format_gbt",
    "intext",
    "reference_markdown",
]

STYLES = ("apa7", "gbt7714")

#: GB/T 文献类型标识。
_GBT_TYPE = {
    "article": "J",
    "review": "J",
    "journal-article": "J",
    "preprint": "EB/OL",
    "conference": "C",
    "proceedings-article": "C",
    "book": "M",
    "chapter": "M",
    "book-chapter": "M",
    "thesis": "D",
    "dissertation": "D",
    "report": "R",
    "dataset": "DS",
}


def _ascii(text: str) -> str:
    """把姓名转成可用于引用键的 ASCII。"""
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^A-Za-z0-9]", "", stripped)


def _family(work: Work) -> str:
    return work.first_author_family() or ""


def assign_cite_keys(works: Iterable[Work]) -> dict[str, str]:
    """为每条文献分配唯一的 ``cite_key`` 并写回对象，返回 key→cite_key 映射。"""
    used: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for work in works:
        family = _family(work)
        if has_cjk(family):
            base = re.sub(r"\s+", "", family)[:3] or "anon"
        else:
            base = _ascii(family).lower() or "anon"
        year = f"{work.year}" if work.year else "nd"
        candidate = f"{base}{year}"
        count = used.get(candidate, 0)
        used[candidate] = count + 1
        if count:
            # a, b, c... 用于同作者同年份的多篇
            suffix = ""
            value = count
            while value >= 0:
                suffix = chr(ord("a") + value % 26) + suffix
                value = value // 26 - 1
                if value < 0:
                    break
            candidate = f"{candidate}{suffix}"
        work.cite_key = candidate
        mapping[work.key] = candidate
    return mapping


# ---------------------------------------------------------------------------
# APA 7
# ---------------------------------------------------------------------------


def _initials(given: str) -> str:
    """``Jean-Paul Marie`` → ``J.-M.`` / ``J. M.``（保留连字符）。"""
    text = (given or "").strip()
    if not text:
        return ""
    chunks = re.split(r"[\s]+", text)
    rendered: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        parts = chunk.split("-")
        initials = [f"{part[0].upper()}." for part in parts if part]
        rendered.append("-".join(initials))
    return " ".join(rendered)


def _apa_author(author: Author) -> str:
    family = author.family or author.display()
    if has_cjk(family):
        return family
    given = _initials(author.given)
    return f"{family}, {given}" if given else family


def _apa_authors(work: Work) -> str:
    authors = [author for author in work.authors if (author.display() or author.family)]
    if not authors:
        return ""
    rendered = [_apa_author(author) for author in authors]
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return f"{rendered[0]}, & {rendered[1]}"
    if len(rendered) <= 20:
        return ", ".join(rendered[:-1]) + ", & " + rendered[-1]
    return ", ".join(rendered[:19]) + ", ... " + rendered[-1]


def _apa_year(work: Work) -> str:
    return f"{work.year}" if work.year else "n.d."


def _locator(work: Work) -> str:
    """没有 DOI 时也要给一个可解析的定位符（arXiv/PubMed/OpenAlex 页面）。"""
    if work.arxiv_id:
        return f"https://arxiv.org/abs/{work.arxiv_id}"
    if work.pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{work.pmid}/"
    if work.openalex_id:
        short = work.openalex_id.rstrip("/").rsplit("/", 1)[-1]
        return f"https://openalex.org/{short}"
    return work.url or ""


def _same_title(left: str, right: str) -> bool:
    from .dedupe import normalize_title

    return bool(left) and normalize_title(left) == normalize_title(right)


def format_apa(work: Work) -> str:
    """APA 7 参考文献条目（期刊/会议/预印本/图书四类近似）。"""
    authors = _apa_authors(work)
    year = _apa_year(work)
    title = (work.title or "").strip().rstrip(".")
    venue = work.venue_or_container()
    pieces: list[str] = []
    if authors:
        pieces.append(f"{authors} ({year}).")
        pieces.append(f"{title}.")
    else:
        pieces.append(f"{title}. ({year}).")

    kind = (work.type or "").lower()
    if kind == "preprint" or work.arxiv_id:
        pieces.append("[Preprint].")
        if venue:
            pieces.append(f"*{venue}*.")
        if work.arxiv_id:
            pieces.append(f"arXiv:{work.arxiv_id}.")
    elif venue and not _same_title(title, venue):
        journal = f"*{venue}*"
        detail = ""
        if work.volume:
            detail = f"*{work.volume}*"
            if work.issue:
                detail += f"({work.issue})"
        if work.pages:
            detail = f"{detail}, {work.pages}" if detail else work.pages
        pieces.append(f"{journal}, {detail}." if detail else f"{journal}.")
    if work.doi:
        pieces.append(f"https://doi.org/{work.doi}")
    else:
        locator = _locator(work)
        if locator:
            pieces.append(locator)
    return " ".join(piece.strip() for piece in pieces if piece.strip())


# ---------------------------------------------------------------------------
# GB/T 7714-2015（顺序编码制）
# ---------------------------------------------------------------------------


def _gbt_author(author: Author) -> str:
    family = author.family or author.display()
    if has_cjk(family):
        return family
    given = _initials(author.given).replace(".", "").replace(" ", " ")
    family_upper = _ascii(family).upper() or family.upper()
    return f"{family_upper} {given}".strip()


def _gbt_authors(work: Work) -> tuple[str, bool]:
    """返回 ``(作者串, 是否中文)``；3 人以上取前 3 加"等/et al."。"""
    authors = [author for author in work.authors if (author.display() or author.family)]
    if not authors:
        return ("佚名" if has_cjk(work.title) else "Anon"), has_cjk(work.title)
    chinese = has_cjk(authors[0].display() or authors[0].family)
    rendered = [_gbt_author(author) for author in authors]
    if len(rendered) > 3:
        head = ", ".join(rendered[:3])
        return (f"{head}, 等" if chinese else f"{head}, et al"), chinese
    return ", ".join(rendered), chinese


def _gbt_type(work: Work) -> str:
    kind = (work.type or "").lower()
    marker = _GBT_TYPE.get(kind, "J")
    if not work.pages and not work.volume and kind not in ("book", "thesis", "report"):
        marker = marker if marker != "J" else "J/OL"
    return marker


def format_gbt(work: Work, index: int) -> str:
    """GB/T 7714-2015 顺序编码制条目。"""
    authors, _ = _gbt_authors(work)
    title = (work.title or "").strip().rstrip(".")
    marker = _gbt_type(work)
    venue = work.venue_or_container()
    year = f"{work.year}" if work.year else "出版年不详"
    pieces = [f"[{index}] {authors}. {title}[{marker}]."]
    if venue and not _same_title(title, venue):
        detail = venue
        if work.volume:
            detail += f", {year}, {work.volume}"
            if work.issue:
                detail += f"({work.issue})"
            if work.pages:
                detail += f": {work.pages}"
        else:
            detail += f", {year}"
            if work.pages:
                detail += f": {work.pages}"
        pieces.append(f"{detail}.")
    else:
        pieces.append(f"{year}.")
    if work.doi:
        pieces.append(f"DOI: {work.doi}.")
    else:
        locator = _locator(work)
        if locator:
            pieces.append(f"{locator}.")
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _sort_key(work: Work) -> tuple[str, int, str]:
    family = _family(work)
    return (family.lower(), work.year or 0, (work.title or "").lower())


def build_references(
    works: list[Work],
    *,
    style: str = "gbt7714",
    order_keys: list[str] | None = None,
    cited_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """生成参考文献表。

    ``order_keys``：正文出现的顺序（GB/T 数字编号按它排；缺省按年份+作者排）。
    ``cited_keys``：正文实际引用的 key 集合，用于标记"未引用条目"。
    """
    normalized_style = (style or "gbt7714").strip().lower()
    if normalized_style not in STYLES:
        from .errors import ARG_INVALID, ResearchError

        raise ResearchError(
            ARG_INVALID,
            f"未知引用样式: {style}",
            hint="可用样式: apa7（APA 第 7 版）、gbt7714（GB/T 7714-2015 顺序编码制）。",
        )
    assign_cite_keys(works)
    by_key = {work.key: work for work in works}
    ordered: list[Work] = []
    if normalized_style == "gbt7714" and order_keys:
        seen: set[str] = set()
        for key in order_keys:
            work = by_key.get(key)
            if work is not None and key not in seen:
                ordered.append(work)
                seen.add(key)
        for work in works:
            if work.key not in seen:
                ordered.append(work)
    else:
        ordered = sorted(works, key=_sort_key)

    entries: list[dict[str, Any]] = []
    unused = 0
    cited = set(cited_keys or [])
    for index, work in enumerate(ordered, start=1):
        if normalized_style == "apa7":
            text = format_apa(work)
        else:
            text = format_gbt(work, index)
        is_cited = (work.key in cited) if cited else True
        if cited and not is_cited:
            unused += 1
        entries.append(
            {
                "index": index,
                "cite_key": work.cite_key,
                "work_key": work.key,
                "text": text,
                "cited": is_cited,
                "year": work.year,
                "doi": work.doi,
            }
        )
    notes = [
        "APA 条目的标题按原文保留大小写（未强转句首大写，避免破坏专有名词与缩写）。",
        "会议论文/书籍章节按期刊格式近似输出；若目标期刊有特殊要求，请在导出后人工微调。",
    ]
    return {
        "schema": "atrix-research/references/1",
        "style": normalized_style,
        "count": len(entries),
        "unused_in_text": unused,
        "entries": entries,
        "by_cite_key": {entry["cite_key"]: entry["work_key"] for entry in entries},
        "notes": notes,
    }


def intext(cite_key: str, style: str) -> str:
    """把引用键渲染成正文引用标记（写作时用，不是最终排版）。"""
    if (style or "").lower() == "apa7":
        match = re.match(r"^(.*?)(\d{4}|nd)([a-z]*)$", cite_key)
        if not match:
            return f"({cite_key})"
        name, year, suffix = match.groups()
        display = "n.d." if year == "nd" else year + suffix
        return f"({name.capitalize()}, {display})"
    return f"[{cite_key}]"


def reference_markdown(references: dict[str, Any], *, heading: str = "参考文献") -> str:
    """把参考文献表渲染成 Markdown（APA 用悬挂缩进，GB/T 用编号）。"""
    lines = [f"## {heading}", ""]
    style = references.get("style")
    for entry in references.get("entries") or []:
        if style == "apa7":
            lines.append(entry["text"])
            lines.append("")
        else:
            lines.append(entry["text"])
    return "\n".join(lines).rstrip() + "\n"
