"""内嵌提示词库检索。

对应 Go：``internal/imagegen/search.go``。

两个入口：:func:`search` 返回结构化命中（供 ``prompt_library_search`` 工具与
管理命令使用），:func:`library_hints` 渲染成紧凑文本（供 LLM 改写器当作
``[TAG 库参考]``）。改名后的模型改写不在本包，见
``internal/app/image_rewriter.go``（Python 侧 ``atri_x/app/*``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from atri_x.imagegen.optimize import split_tag_words

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注
    from atri_x.imagegen.library import PromptLibrary

__all__ = ["LibraryMatch", "LibraryScopes", "library_hints", "search"]

#: 支持的检索范围。对应 Go ``LibraryScopes``。
LibraryScopes = ["entry", "keyword", "negative", "preset", "tip"]

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20


@dataclass(slots=True)
class LibraryMatch:
    """库检索的一条命中。对应 Go ``LibraryMatch``。"""

    scope: str = ""
    title: str = ""
    detail: str = ""
    anchors: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    score: int = 0


def search(
    library: "PromptLibrary | None",
    query: str,
    scopes: list[str] | None = None,
    limit: int = 0,
) -> list[LibraryMatch]:
    """按自由文本检索内嵌库；``scopes`` 为空表示全部范围。对应 Go ``Search``。"""
    if library is None:
        return []
    tokens = split_tag_words(query)
    if not tokens:
        return []
    if limit <= 0:
        limit = _DEFAULT_LIMIT
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT
    allowed: set[str] = set()
    if not scopes:
        allowed.update(LibraryScopes)
    else:
        for scope in scopes:
            name = str(scope or "").strip().lower()
            if name:
                allowed.add(name)

    def score(text: str) -> int:
        lower = text.lower()
        total = 0
        for token in tokens:
            if token in lower:
                total += len(token)
        return total

    out: list[LibraryMatch] = []
    if "entry" in allowed:
        for entry in library.entries:
            text = " ".join([entry.category, entry.scene, entry.description, entry.prompt])
            value = score(text)
            if value > 0:
                out.append(
                    LibraryMatch(
                        scope="entry",
                        title=entry.category + " / " + entry.scene,
                        detail=entry.prompt,
                        anchors=list(entry.anchors),
                        terms=list(entry.terms),
                        score=value,
                    )
                )
    if "keyword" in allowed:
        for name, items in library.keywords.items():
            joined = ", ".join(items)
            value = score(name + " " + joined)
            if value > 0:
                out.append(LibraryMatch(scope="keyword", title=name, detail=joined, score=value))
    if "negative" in allowed:
        for group in library.negatives:
            joined = ", ".join(group.items)
            value = score(group.category + " " + joined)
            if value > 0:
                out.append(
                    LibraryMatch(scope="negative", title=group.category, detail=joined, score=value)
                )
    if "preset" in allowed:
        for preset in library.presets:
            text = " ".join([preset.use, preset.ratio, preset.scene, preset.supplement])
            value = score(text)
            if value > 0:
                out.append(
                    LibraryMatch(
                        scope="preset",
                        title=preset.use,
                        detail=preset.ratio + " | " + preset.supplement,
                        score=value,
                    )
                )
    if "tip" in allowed:
        for tip in library.tips:
            text = " ".join([tip.module, tip.title, tip.detail, tip.example])
            value = score(text)
            if value > 0:
                out.append(
                    LibraryMatch(
                        scope="tip",
                        title=tip.module + " / " + tip.title,
                        detail=tip.detail + " | " + tip.example,
                        score=value,
                    )
                )

    # Go 用 sort.SliceStable + (score 降序, title 升序)：title 相同时保持原插入
    # 顺序，Python 的 sorted 是稳定排序，同样口径。
    out.sort(key=lambda item: (-item.score, item.title))
    if len(out) > limit:
        out = out[:limit]
    return out


def library_hints(library: "PromptLibrary | None", scene: str, limit: int = 0) -> str:
    """渲染给 LLM 改写器的紧凑提示。对应 Go ``LibraryHints``。"""
    matches = search(library, scene, None, limit)
    if not matches:
        return ""
    lines: list[str] = []
    for match in matches:
        line = "- [" + match.scope + "] " + match.title
        if match.scope == "entry":
            if match.anchors:
                line += " → anchors: " + ", ".join(match.anchors)
            else:
                line += " → " + match.detail
        else:
            line += " → " + match.detail
        lines.append(line)
    return "\n".join(lines)
