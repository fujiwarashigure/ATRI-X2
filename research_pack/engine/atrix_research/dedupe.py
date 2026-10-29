"""跨源去重。

同一条文献会同时出现在 OpenAlex / Crossref / arXiv / PubMed 里，各自带
不同的标识符和略有出入的标题。这里用"并查集 + 保守的模糊匹配"合并：

1. **确定匹配**：DOI、arXiv id、PMID、OpenAlex id 任一相同即合并；
2. **标题匹配**：归一化后完全相同（且长度 ≥ 16 字符）即合并；
3. **模糊匹配**：token Jaccard ≥ 阈值，且年份相差 ≤ 1，且第一作者姓氏相同
   （任一侧缺失时只靠标题高度相似 ≥ 0.97 兜底）。

宁可漏合不可错合：模糊但证据不足的组合不合并，只记进
``possible_duplicates`` 供人工/模型复核。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import Work, normalize_doi

__all__ = [
    "DEFAULT_TITLE_THRESHOLD",
    "dedupe",
    "jaccard",
    "normalize_title",
    "title_tokens",
]

DEFAULT_TITLE_THRESHOLD = 0.90

#: 标题里的高频虚词，参与相似度计算只会拉高噪声。
_STOPWORDS = frozenset(
    """a an the of for and or in on at to from by with without into over under
    via using based toward towards is are was were be been being this that these
    those we our their its it as not no than then thus study analysis approach
    method methods results review paper article research""".split()
)

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD = re.compile(r"[a-z0-9]+")
_PUNCT = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+")


def normalize_title(title: str) -> str:
    """标题归一化：NFKC、小写、去首冠词、去标点、压空白、统一破折号。"""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    text = _PUNCT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for article in ("a ", "an ", "the "):
        if text.startswith(article):
            text = text[len(article) :]
            break
    return text


def title_tokens(title: str) -> set[str]:
    """标题词元集合；中文按字切分（中文标题没有空格边界）。"""
    normalized = normalize_title(title)
    tokens: set[str] = set()
    for word in _WORD.findall(normalized):
        if word not in _STOPWORDS and len(word) > 1:
            tokens.add(word)
    for char in _CJK.findall(normalized):
        tokens.add(f"c:{char}")
    # 中文双字组，能显著提升中文标题的区分度
    cjk_chars = _CJK.findall(normalized)
    for index in range(len(cjk_chars) - 1):
        tokens.add(f"b:{cjk_chars[index]}{cjk_chars[index + 1]}")
    return tokens


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: int, right: int) -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return False
        self.parent[right_root] = left_root
        return True


def _source_priority(work: Work) -> int:
    """合并时选谁当主记录的偏好：字段越全、标识越权威越优先。"""
    score = 0
    score += 6 if normalize_doi(work.doi) else 0
    score += 3 if work.openalex_id else 0
    score += 2 if work.pmid else 0
    score += 2 if work.arxiv_id else 0
    score += 3 if work.authors else 0
    score += 3 if work.abstract else 0
    score += 2 if work.venue else 0
    score += 2 if work.year else 0
    return score


def _merge_into(primary: Work, secondary: Work) -> None:
    """把 secondary 的缺失字段补进 primary（不改 primary 已有值）。"""
    for name in (
        "title",
        "venue",
        "volume",
        "issue",
        "pages",
        "publisher",
        "type",
        "language",
        "doi",
        "arxiv_id",
        "pmid",
        "pmcid",
        "openalex_id",
        "s2_id",
        "url",
    ):
        if not getattr(primary, name) and getattr(secondary, name):
            setattr(primary, name, getattr(secondary, name))
    if primary.year is None and secondary.year is not None:
        primary.year = secondary.year
    if not primary.abstract and secondary.abstract:
        primary.abstract = secondary.abstract
    elif secondary.abstract and len(secondary.abstract) > len(primary.abstract) * 1.5:
        primary.abstract = secondary.abstract
    if len(secondary.authors) > len(primary.authors):
        primary.authors = secondary.authors
    if primary.citation_count is None and secondary.citation_count is not None:
        primary.citation_count = secondary.citation_count
    elif (
        primary.citation_count is not None
        and secondary.citation_count is not None
        and secondary.citation_count > primary.citation_count
    ):
        # 引用数取各源最大值（各源统计口径不同，取大值对"影响力"判断更保守）
        primary.citation_count = secondary.citation_count
    if secondary.is_retracted is True:
        primary.is_retracted = True
    elif primary.is_retracted is None and secondary.is_retracted is not None:
        primary.is_retracted = secondary.is_retracted
    for source in secondary.source_hits:
        if source not in primary.source_hits:
            primary.source_hits.append(source)
    secondary_key = secondary.key
    if secondary_key != primary.key and secondary_key not in primary.merged_from:
        primary.merged_from.append(secondary_key)
    for key, value in secondary.extra.items():
        primary.extra.setdefault(key, value)


def dedupe(
    works: list[Work], *, title_threshold: float = DEFAULT_TITLE_THRESHOLD
) -> tuple[list[Work], dict[str, Any]]:
    """合并重复记录，返回 ``(去重后的列表, 统计)``。

    结果列表保持输入顺序的稳定性：以每组最早出现的记录的位置为准，
    这样同一批候选重复跑得到同一顺序，便于 diff 产物。
    """
    items = [work for work in works if (work.title or "").strip()]
    uf = _UnionFind(len(items))
    exact_ids: dict[str, int] = {}
    title_keys: dict[str, int] = {}

    for index, work in enumerate(items):
        for identifier in (
            f"doi:{normalize_doi(work.doi)}" if work.doi else "",
            f"arxiv:{work.arxiv_id.lower()}" if work.arxiv_id else "",
            f"pmid:{work.pmid}" if work.pmid else "",
            f"oa:{work.openalex_id}" if work.openalex_id else "",
        ):
            if not identifier:
                continue
            if identifier in exact_ids:
                uf.union(exact_ids[identifier], index)
            else:
                exact_ids[identifier] = index
        normalized = normalize_title(work.title)
        if len(normalized) >= 16:
            if normalized in title_keys:
                uf.union(title_keys[normalized], index)
            else:
                title_keys[normalized] = index

    tokens = [title_tokens(work.title) for work in items]
    possible: list[dict[str, Any]] = []
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if uf.find(left) == uf.find(right):
                continue
            similarity = jaccard(tokens[left], tokens[right])
            if similarity < min(title_threshold, 0.85):
                continue
            year_left, year_right = items[left].year, items[right].year
            year_ok = (
                year_left is None
                or year_right is None
                or abs(year_left - year_right) <= 1
            )
            family_left = items[left].first_author_family().lower()
            family_right = items[right].first_author_family().lower()
            author_ok = not family_left or not family_right or family_left == family_right
            strong_title = similarity >= 0.97
            if similarity >= title_threshold and year_ok and (author_ok or strong_title):
                uf.union(left, right)
            elif similarity >= 0.85:
                possible.append(
                    {
                        "left": items[left].key,
                        "right": items[right].key,
                        "title_similarity": round(similarity, 3),
                        "year_ok": year_ok,
                        "first_author_match": author_ok,
                        "reason": "证据不足未合并，请人工确认是否同一篇",
                    }
                )

    groups: dict[int, list[int]] = {}
    for index in range(len(items)):
        groups.setdefault(uf.find(index), []).append(index)

    merge_log: list[dict[str, Any]] = []
    merged_pairs: list[tuple[int, Work]] = []
    for root, members in groups.items():
        primary_index = max(members, key=lambda idx: (_source_priority(items[idx]), -idx))
        primary = items[primary_index]
        for index in members:
            if index == primary_index:
                continue
            kept_before = primary.key
            _merge_into(primary, items[index])
            merge_log.append(
                {
                    "kept": kept_before,
                    "merged": items[index].key,
                    "title": items[index].title[:160],
                }
            )
        # 以"组内最早出现位置"排序，保证同一批候选重复跑得到同一顺序
        merged_pairs.append((min(members), primary))
    merged_pairs.sort(key=lambda pair: pair[0])
    merged: list[Work] = [work for _, work in merged_pairs]

    stats = {
        "input": len(works),
        "considered": len(items),
        "output": len(merged),
        "merged": len(works) - len(merged),
        "exact_matches": sum(
            1 for work in merged if len(work.merged_from) > 0
        ),
        "merge_log": merge_log,
        "possible_duplicates": possible[:50],
        "title_threshold": title_threshold,
    }
    return merged, stats
