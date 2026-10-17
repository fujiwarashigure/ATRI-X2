"""Atriwisp 注入的工具声明 → ``CachedTool``。

对应 Go：``internal/toolrun/atriwisp.go``。

命名规则（上游用它隔离命名空间）：``canonical_name = atriwisp_<hub>_<tool>``，
其中 ``hub`` 与 ``tool`` 都先把 ``[^a-zA-Z0-9_-]+`` 替换成 ``_``。
这样即使注入的工具与本地工具同名，也不会互相覆盖（`cache` 的去重键是
``canonical_name``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.toolrun.cache import (
    SOURCE_KIND_ATRIWISP,
    CachedTool,
    normalize_cached_tools,
)

#: 对应 Go ``atriwispToolNameReplacer``。
ATRIWISP_TOOL_NAME_REPLACER = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass
class AtriwispToolDeclaration:
    """Atriwisp 声明的一个工具。对应 Go ``AtriwispToolDeclaration``。"""

    name: str = ""
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 0
    endpoint: str = ""
    foreground_only: bool = False


@dataclass
class AtriwispInjection:
    """一次 Atriwisp 注入。对应 Go ``AtriwispInjection``。"""

    atriwisp_name: str = ""
    event_key: str = ""
    tools: list[AtriwispToolDeclaration] = field(default_factory=list)


def cached_tools_from_atriwisp(injection: AtriwispInjection) -> list[CachedTool]:
    """对应 Go ``CachedToolsFromAtriwisp``（返回规范化后的列表）。"""
    atriwisp_name = (injection.atriwisp_name or "").strip()
    if not atriwisp_name:
        return []
    out: list[CachedTool] = []
    for declared in injection.tools:
        name = (declared.name or "").strip()
        if not name:
            continue
        canonical = atriwisp_tool_name(atriwisp_name, name)
        out.append(
            CachedTool(
                name=name,
                canonical_name=canonical,
                source=SOURCE_KIND_ATRIWISP,
                description=declared.description,
                schema=ToolSchema(
                    type="function",
                    function=ToolFunctionSchema(
                        name=canonical,
                        description=declared.description,
                        parameters=declared.schema or {},
                    ),
                ),
                atriwisp_name=atriwisp_name,
                event_key=injection.event_key,
                endpoint=(declared.endpoint or "").strip(),
                timeout_seconds=declared.timeout_seconds,
                foreground_only=declared.foreground_only,
            )
        )
    return normalize_cached_tools(out)


def atriwisp_tool_name(atriwisp_name: str, name: str) -> str:
    """对应 Go ``atriwispToolName``。"""
    hub = ATRIWISP_TOOL_NAME_REPLACER.sub("_", (atriwisp_name or "").strip())
    tool = ATRIWISP_TOOL_NAME_REPLACER.sub("_", (name or "").strip())
    return f"atriwisp_{hub}_{tool}"


# Go 的导出名
CachedToolsFromAtriwisp = cached_tools_from_atriwisp

__all__ = [
    "ATRIWISP_TOOL_NAME_REPLACER",
    "AtriwispInjection",
    "AtriwispToolDeclaration",
    "CachedToolsFromAtriwisp",
    "atriwisp_tool_name",
    "cached_tools_from_atriwisp",
]
