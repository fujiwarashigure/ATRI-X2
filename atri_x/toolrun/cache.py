"""工具发现的缓存（``CachedTool`` 列表的编解码与合并）。

对应 Go：``internal/toolrun/cache.go``。

去重键是 **``canonical_name`` 优先、否则 ``name``**：Atriwisp 注入的工具
``name`` 可能与本地工具重名，只有 ``canonical_name``（``atriwisp_<hub>_<tool>``）
能区分它们。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.llm.llm import ToolFunctionSchema, ToolSchema

#: 工具来源。对应 Go ``SourceKind``。
SOURCE_KIND_NATIVE = "native"
SOURCE_KIND_ATRIWISP = "atriwisp"

SourceKindNative = SOURCE_KIND_NATIVE
SourceKindAtriwisp = SOURCE_KIND_ATRIWISP


@dataclass
class CachedTool:
    """一条被缓存的可调用工具。对应 Go ``CachedTool``。

    JSON 键名与 Go 的 tag 逐字对齐（``canonical_name``/``foreground_only`` 等），
    因为这份缓存会**落进 Session metadata**，跨版本读取要能对上。
    """

    name: str = ""
    canonical_name: str = ""
    source: str = SOURCE_KIND_NATIVE
    description: str = ""
    schema: ToolSchema = field(default_factory=ToolSchema)
    atriwisp_name: str = ""
    event_key: str = ""
    endpoint: str = ""
    timeout_seconds: int = 0
    foreground_only: bool = False

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 omitempty 语义序列化。"""
        out: dict[str, Any] = {"name": self.name, "source": self.source, "schema": _schema_json(self.schema)}
        if self.canonical_name:
            out["canonical_name"] = self.canonical_name
        if self.description:
            out["description"] = self.description
        if self.atriwisp_name:
            out["atriwisp_name"] = self.atriwisp_name
        if self.event_key:
            out["event_key"] = self.event_key
        if self.endpoint:
            out["endpoint"] = self.endpoint
        if self.timeout_seconds:
            out["timeout_seconds"] = self.timeout_seconds
        if self.foreground_only:
            out["foreground_only"] = True
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CachedTool":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            name=str(raw.get("name") or ""),
            canonical_name=str(raw.get("canonical_name") or ""),
            source=str(raw.get("source") or SOURCE_KIND_NATIVE),
            description=str(raw.get("description") or ""),
            schema=_schema_from_json(raw.get("schema")),
            atriwisp_name=str(raw.get("atriwisp_name") or ""),
            event_key=str(raw.get("event_key") or ""),
            endpoint=str(raw.get("endpoint") or ""),
            timeout_seconds=int(raw.get("timeout_seconds") or 0),
            foreground_only=bool(raw.get("foreground_only")),
        )


def _schema_json(schema: ToolSchema) -> dict[str, Any]:
    return {
        "type": schema.type or "function",
        "function": {
            "name": schema.function.name,
            "description": schema.function.description,
            "parameters": schema.function.parameters or {},
        },
    }


def _schema_from_json(raw: Any) -> ToolSchema:
    if not isinstance(raw, dict):
        return ToolSchema()
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    return ToolSchema(
        type=str(raw.get("type") or "function"),
        function=ToolFunctionSchema(
            name=str(fn.get("name") or ""),
            description=str(fn.get("description") or ""),
            parameters=dict(fn.get("parameters") or {}),
        ),
    )


@dataclass
class Cache:
    """一份工具发现缓存。对应 Go ``Cache``。"""

    tools: list[CachedTool] = field(default_factory=list)


def decode_cache(raw: Any) -> Cache:
    """对应 Go ``DecodeCache``：空输入返回空缓存，解析失败也返回空（不抛）。"""
    if raw is None or raw == "" or raw == b"":
        return Cache()
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return Cache()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return Cache()
    if not isinstance(raw, dict):
        return Cache()
    tools = raw.get("tools")
    cache = Cache(
        tools=[CachedTool.from_json(item) for item in tools] if isinstance(tools, list) else []
    )
    cache.tools = normalize_cached_tools(cache.tools)
    return cache


def encode_cache(cache: Cache) -> str | None:
    """对应 Go ``EncodeCache``：没有工具时返回 ``None``（落库为 NULL）。"""
    tools = normalize_cached_tools(cache.tools)
    if not tools:
        return None
    return json.dumps(
        {"tools": [t.to_json() for t in tools]}, ensure_ascii=False, separators=(",", ":")
    )


def normalize_cached_tools(tools: list[CachedTool]) -> list[CachedTool]:
    """去重（canonical 优先）→ 补默认 source → 按名排序。对应 Go ``NormalizeCachedTools``。"""
    seen: set[str] = set()
    out: list[CachedTool] = []
    for cached in tools:
        key = cached.canonical_name or cached.name
        if not key or key in seen:
            continue
        seen.add(key)
        if not cached.source:
            cached = dataclasses.replace(cached, source=SOURCE_KIND_NATIVE)
        out.append(cached)
    return sort_cached_tools(out)


def merge_cached_tools(
    existing: list[CachedTool], incoming: list[CachedTool]
) -> list[CachedTool]:
    """按 key 合并（**incoming 覆盖 existing**），再规范化。对应 Go ``MergeCachedTools``。"""
    by_key: dict[str, CachedTool] = {}
    for cached in list(existing) + list(incoming):
        key = cached.canonical_name or cached.name
        if key:
            by_key[key] = cached
    return normalize_cached_tools(list(by_key.values()))


def sort_cached_tools(tools: list[CachedTool]) -> list[CachedTool]:
    """按 (canonical_name 或 name) 升序。对应 Go ``SortCachedTools``。"""
    return sorted(tools, key=lambda t: t.canonical_name or t.name)


# Go 的导出名
DecodeCache = decode_cache
EncodeCache = encode_cache
NormalizeCachedTools = normalize_cached_tools
MergeCachedTools = merge_cached_tools
SortCachedTools = sort_cached_tools

__all__ = [
    "SOURCE_KIND_ATRIWISP",
    "SOURCE_KIND_NATIVE",
    "Cache",
    "CachedTool",
    "DecodeCache",
    "EncodeCache",
    "MergeCachedTools",
    "NormalizeCachedTools",
    "SortCachedTools",
    "SourceKindAtriwisp",
    "SourceKindNative",
    "decode_cache",
    "encode_cache",
    "merge_cached_tools",
    "normalize_cached_tools",
    "sort_cached_tools",
]
