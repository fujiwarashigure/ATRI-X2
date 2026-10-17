"""``prompt_library_search``：检索内置的 GPT Image 提示词库。

对应 Go：``internal/tool/builtin/prompt_library.go``。

库本体在 :mod:`atri_x.imagegen.library`（Go 的 ``imagegen.PromptLib``），本模块
只负责工具声明、参数解析与结果渲染。检索实现完全复用已落地模块，不重复实现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
from atri_x.imagegen import prompt_lib
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder, required
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

PROMPT_LIBRARY_SEARCH_NAME = "prompt_library_search"

_DESCRIPTION = (
    "检索内置的 GPT Image 提示词库（GPT_Image_Prompts_大全）：场景条目、关键词词库、"
    "负面词、用途比例、进阶技巧。生图前可以用它查该配什么镜头/光线/画风。"
)


@dataclass
class PromptLibrarySearchArgs:
    """对应 Go ``promptLibrarySearchArgs``。"""

    query: str = ""
    scope: str = ""
    limit: int = 0


class PromptLibrarySearchTool:
    """对应 Go ``PromptLibrarySearchTool``（Go 是空结构体值类型）。"""

    def name(self) -> str:
        return PROMPT_LIBRARY_SEARCH_NAME

    def info(self) -> Info:
        return (
            Builder(PROMPT_LIBRARY_SEARCH_NAME)
            .description(_DESCRIPTION)
            .risk(RiskLow)
            .tags("image")
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            Builder(PROMPT_LIBRARY_SEARCH_NAME)
            .description("检索内置生图提示词库。")
            .string("query", "检索词，例如 cyberpunk、头像、水彩、构图。", required())
            .string("scope", "可选：entry、keyword、negative、preset、tip；不填检索全部。")
            .integer("limit", "返回条数，默认 5，最大 20。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``PromptLibrarySearchTool.Call``。"""
        args = parse_prompt_library_search_args(req.arguments)
        if (args.query or "").strip() == "":
            return Result(content="query 不能为空。")
        library = prompt_lib()
        scopes: list[str] = []
        scope = (args.scope or "").strip()
        if scope != "":
            scopes.append(scope)
        matches = library.search(args.query, scopes, args.limit)
        if not matches:
            return Result(content="提示词库没有匹配结果。")
        lines = [f"命中 {len(matches)} 条："]
        for match in matches:
            lines.append("- [" + match.scope + "] " + match.title)
            if match.anchors:
                lines.append(
                    "  anchors: " + truncate_prompt_lib_line(", ".join(match.anchors), 320)
                )
            if match.terms:
                lines.append(
                    "  terms: " + truncate_prompt_lib_line(", ".join(match.terms), 240)
                )
            detail = (match.detail or "").strip()
            if detail != "":
                lines.append("  " + truncate_prompt_lib_line(detail, 320))
        lines.append(
            "把这些词条写进 image_generate 的 prompt，或直接让 image_generate 自动套用。"
        )
        return Result(content="\n".join(lines))


def truncate_prompt_lib_line(text: str, max_chars: int) -> str:
    """对应 Go ``truncatePromptLibLine``：压空白后按**字符**截断。"""
    text = " ".join((text or "").strip().split())
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def parse_prompt_library_search_args(raw: str) -> PromptLibrarySearchArgs:
    """对应 Go 的 ``json.Unmarshal`` 到 ``promptLibrarySearchArgs``。"""
    if not raw:
        return PromptLibrarySearchArgs()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse prompt_library_search arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(
            "parse prompt_library_search arguments: expected a JSON object"
        )
    args = PromptLibrarySearchArgs()
    args.query = _as_str(data.get("query"))
    args.scope = _as_str(data.get("scope"))
    try:
        args.limit = 0 if data.get("limit") is None else int(data.get("limit"))
    except (TypeError, ValueError):
        args.limit = 0
    return args


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


__all__ = [name for name in dir() if not name.startswith("_")]
