"""内嵌提示词库：数据类型与加载。

对应 Go：``internal/imagegen/library.go``（数据文件 ``prompts/library.json``
由 ``//go:embed`` 内嵌；Python 侧改为按需 ``json.load`` + 惰性缓存，
抽取脚本见 :mod:`atri_x.imagegen._extract_library`）。

库内容来自上游表格（``GPT_Image_Prompts_大全.xlsx``）的六张 sheet：万能模板、
全部提示词汇总、关键词库、负面提示词、参数速查、进阶技巧，另有单词级词频表。
Go 用 ``sync.Once`` 只加载一次并把错误一起缓存；这里用模块级锁 + 缓存值复刻，
因此后续调用会抛同一个异常对象。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from atri_x.imagegen.optimize import OptimizeOptions, OptimizeResult, optimize, optimize_tags
from atri_x.imagegen.search import LibraryMatch, library_hints, search

__all__ = [
    "NegativeGroup",
    "PromptEntry",
    "PromptLibrary",
    "PromptTemplate",
    "PromptTip",
    "TermCount",
    "UseCasePreset",
    "library_path",
    "prompt_lib",
    "reset_cache",
]

#: 数据文件相对本模块的路径，与 Go 包里 ``prompts/library.json`` 一致。
LIBRARY_RELATIVE = os.path.join("prompts", "library.json")

_LOCK = threading.Lock()
_VALUE: "PromptLibrary | None" = None
_ERROR: BaseException | None = None


def library_path() -> str:
    """返回数据文件的绝对路径。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), LIBRARY_RELATIVE)


@dataclass(slots=True)
class PromptTemplate:
    """``万能模板`` sheet 的一行。对应 Go ``PromptTemplate``。"""

    type: str = ""
    name: str = ""
    scene: str = ""
    template: str = ""
    variables: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PromptEntry:
    """``全部提示词汇总`` sheet 的一行。对应 Go ``PromptEntry``。"""

    category: str = ""
    scene: str = ""
    description: str = ""
    prompt: str = ""
    variables: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    generic_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NegativeGroup:
    """``负面提示词`` sheet 的一行。对应 Go ``NegativeGroup``。"""

    key: str = ""
    category: str = ""
    items: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TermCount:
    """单词级词频表的一项。对应 Go ``TermCount``。"""

    word: str = ""
    count: int = 0


@dataclass(slots=True)
class UseCasePreset:
    """``参数速查`` sheet 的一行。对应 Go ``UseCasePreset``。"""

    use: str = ""
    ratio: str = ""
    scene: str = ""
    supplement: str = ""
    keys: list[str] = field(default_factory=list)
    scene_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PromptTip:
    """``进阶技巧`` sheet 的一行。对应 Go ``PromptTip``。"""

    module: str = ""
    title: str = ""
    detail: str = ""
    example: str = ""


@dataclass(slots=True)
class PromptLibrary:
    """内嵌的提示词优化知识库。对应 Go ``PromptLibrary``。"""

    version: int = 0
    source: str = ""
    templates: list[PromptTemplate] = field(default_factory=list)
    entries: list[PromptEntry] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    keyword_terms: dict[str, list[str]] = field(default_factory=dict)
    negatives: list[NegativeGroup] = field(default_factory=list)
    presets: list[UseCasePreset] = field(default_factory=list)
    tips: list[PromptTip] = field(default_factory=list)
    vocabulary: list[TermCount] = field(default_factory=list)
    #: ``word -> count`` 索引，加载时构建（对应 Go 的 vocabIndex）。
    vocab_index: dict[str, int] = field(default_factory=dict)

    # -- 查询 ---------------------------------------------------------------

    def vocabulary_count(self, word: str) -> int:
        """某个词在整个库里出现了多少次。对应 Go ``VocabularyCount``。"""
        if not self.vocab_index:
            return 0
        return self.vocab_index.get(str(word or "").strip().lower(), 0)

    def keyword_group(self, prefix: str) -> list[str]:
        """按前缀取一组关键词（例如 ``"画质"``）。对应 Go ``KeywordGroup``。

        Go 遍历的是 map，同前缀多组时的胜出者不确定；这里按 JSON 里的插入
        顺序取第一组，结果是确定的。
        """
        text = str(prefix or "")
        for name, items in self.keywords.items():
            if name.startswith(text):
                return items
        return []

    def negative_items(self, key: str) -> list[str]:
        """取某组负面提示词。对应 Go ``NegativeItems``。"""
        for group in self.negatives:
            if group.key == key:
                return group.items
        return []

    # -- 方法转发（实现体在 optimize / search，按 Go 的文件边界拆分） ---------

    def optimize(self, scene: str, opts: OptimizeOptions | None = None) -> OptimizeResult:
        """对应 Go ``PromptLibrary.Optimize``。"""
        return optimize(self, scene, opts)

    def optimize_tags(
        self, original: str, folded: str, opts: OptimizeOptions, max_negatives: int
    ) -> OptimizeResult:
        """对应 Go ``optimizeTags``（内部入口，测试与 optimize 复用）。"""
        return optimize_tags(self, original, folded, opts, max_negatives)

    def search(
        self, query: str, scopes: list[str] | None = None, limit: int = 0
    ) -> list[LibraryMatch]:
        """对应 Go ``PromptLibrary.Search``。"""
        return search(self, query, scopes, limit)

    def library_hints(self, scene: str, limit: int = 0) -> str:
        """对应 Go ``PromptLibrary.LibraryHints``。"""
        return library_hints(self, scene, limit)


def _from_payload(payload: dict[str, Any]) -> PromptLibrary:
    library = PromptLibrary(
        version=int(payload.get("version") or 0),
        source=str(payload.get("source") or ""),
    )
    library.templates = [
        PromptTemplate(
            type=str(item.get("type") or ""),
            name=str(item.get("name") or ""),
            scene=str(item.get("scene") or ""),
            template=str(item.get("template") or ""),
            variables=[str(v) for v in item.get("variables") or []],
        )
        for item in payload.get("templates") or []
    ]
    library.entries = [
        PromptEntry(
            category=str(item.get("category") or ""),
            scene=str(item.get("scene") or ""),
            description=str(item.get("description") or ""),
            prompt=str(item.get("prompt") or ""),
            variables=[str(v) for v in item.get("variables") or []],
            anchors=[str(v) for v in item.get("anchors") or []],
            terms=[str(v) for v in item.get("terms") or []],
            keys=[str(v) for v in item.get("keys") or []],
            generic_keys=[str(v) for v in item.get("generic_keys") or []],
        )
        for item in payload.get("entries") or []
    ]
    library.keywords = {
        str(name): [str(v) for v in items] for name, items in (payload.get("keywords") or {}).items()
    }
    library.keyword_terms = {
        str(name): [str(v) for v in items]
        for name, items in (payload.get("keyword_terms") or {}).items()
    }
    library.negatives = [
        NegativeGroup(
            key=str(item.get("key") or ""),
            category=str(item.get("category") or ""),
            items=[str(v) for v in item.get("items") or []],
            terms=[str(v) for v in item.get("terms") or []],
        )
        for item in payload.get("negatives") or []
    ]
    library.presets = [
        UseCasePreset(
            use=str(item.get("use") or ""),
            ratio=str(item.get("ratio") or ""),
            scene=str(item.get("scene") or ""),
            supplement=str(item.get("supplement") or ""),
            keys=[str(v) for v in item.get("keys") or []],
            scene_keys=[str(v) for v in item.get("scene_keys") or []],
        )
        for item in payload.get("presets") or []
    ]
    library.tips = [
        PromptTip(
            module=str(item.get("module") or ""),
            title=str(item.get("title") or ""),
            detail=str(item.get("detail") or ""),
            example=str(item.get("example") or ""),
        )
        for item in payload.get("tips") or []
    ]
    library.vocabulary = [
        TermCount(word=str(item.get("word") or ""), count=int(item.get("count") or 0))
        for item in payload.get("vocabulary") or []
    ]
    library.vocab_index = {item.word: item.count for item in library.vocabulary}
    return library


def prompt_lib() -> PromptLibrary:
    """加载内嵌提示词库（只加载一次）。对应 Go ``PromptLib``。

    文件缺失 / 解析失败 / ``entries`` 为空都抛异常；与 Go 的 ``sync.Once`` 一样，
    失败结果也会被缓存，后续调用抛同一个异常对象。
    """
    global _VALUE, _ERROR
    with _LOCK:
        if _VALUE is not None:
            return _VALUE
        if _ERROR is not None:
            raise _ERROR
        path = library_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            _ERROR = RuntimeError(f"parse embedded prompt library: {exc}")
            raise _ERROR from exc
        library = _from_payload(payload)
        if not library.entries:
            _ERROR = RuntimeError("embedded prompt library is empty")
            raise _ERROR
        _VALUE = library
        return _VALUE


def reset_cache() -> None:
    """清空加载缓存（测试用；Go 侧没有对应物）。"""
    global _VALUE, _ERROR
    with _LOCK:
        _VALUE = None
        _ERROR = None
