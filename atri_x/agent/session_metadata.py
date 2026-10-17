"""Session metadata 的编解码。

对应 Go：``internal/agent/session_metadata.go``。

JSON 键名逐字照抄 Go struct tag，``omitempty`` 逐字段复刻：零值字段在编码时
**删除**该键（而不是写 ``null`` / ``""`` / ``[]``）。解码时对若干列表字段做
"去重 + 排序"归一化，与 Go 的 ``sortedUnique`` 一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.llm import Usage
from atri_x.toolrun import CachedTool, normalize_cached_tools

__all__ = [
    "ContextCompactState",
    "LLMOriginState",
    "SessionMetadata",
    "decode_session_metadata",
    "encode_session_metadata",
    "encode_session_metadata_into",
    "set_metadata_field",
    "sorted_unique",
    "tool_cache_items_normalized",
]


@dataclass
class LLMOriginState:
    """会话的协议来源。对应 Go ``llmOriginState``。"""

    protocol: str = ""
    provider: str = ""
    model: str = ""

    def is_zero(self) -> bool:
        return not self.protocol and not self.provider and not self.model

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.protocol:
            out["protocol"] = self.protocol
        if self.provider:
            out["provider"] = self.provider
        if self.model:
            out["model"] = self.model
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "LLMOriginState":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            protocol=str(raw.get("protocol") or ""),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
        )


@dataclass
class ContextCompactState:
    """待生效 / 已生效的上下文压缩种子。对应 Go ``contextCompactState``。"""

    pending: bool = False
    summary: str = ""
    source_session_id: str = ""
    from_message_id: str = ""
    to_message_id: str = ""
    provider: str = ""
    model: str = ""
    trigger_reason: str = ""
    source_tokens: int = 0
    summary_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    generation: int = 0
    base_title: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        set_metadata_field(out, "pending", self.pending)
        for key, value in (
            ("summary", self.summary),
            ("source_session_id", self.source_session_id),
            ("from_message_id", self.from_message_id),
            ("to_message_id", self.to_message_id),
            ("provider", self.provider),
            ("model", self.model),
            ("trigger_reason", self.trigger_reason),
            ("source_tokens", self.source_tokens),
            ("summary_tokens", self.summary_tokens),
            ("total_tokens", self.total_tokens),
            ("cache_hit_tokens", self.cache_hit_tokens),
            ("generation", self.generation),
            ("base_title", self.base_title),
        ):
            set_metadata_field(out, key, value)
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "ContextCompactState | None":
        if not isinstance(raw, dict):
            return None
        return cls(
            pending=bool(raw.get("pending")),
            summary=str(raw.get("summary") or ""),
            source_session_id=str(raw.get("source_session_id") or ""),
            from_message_id=str(raw.get("from_message_id") or ""),
            to_message_id=str(raw.get("to_message_id") or ""),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            trigger_reason=str(raw.get("trigger_reason") or ""),
            source_tokens=int(raw.get("source_tokens") or 0),
            summary_tokens=int(raw.get("summary_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            cache_hit_tokens=int(raw.get("cache_hit_tokens") or 0),
            generation=int(raw.get("generation") or 0),
            base_title=str(raw.get("base_title") or ""),
        )


@dataclass
class SessionMetadata:
    """会话 metadata 的强类型视图。对应 Go ``sessionMetadata``。"""

    discovered_tools: list[str] = field(default_factory=list)
    tool_cache: list[CachedTool] = field(default_factory=list)
    tool_tags: list[str] = field(default_factory=list)
    shown_rule_card_formats: list[str] = field(default_factory=list)
    last_usage: Usage | None = None
    background_kind: str = ""
    workspace_dir: str = ""
    workspace_agent_notice_dirs: list[str] = field(default_factory=list)
    context_compact: ContextCompactState | None = None
    title_renamed: bool = False
    title_source: str = ""
    llm_origin: LLMOriginState = field(default_factory=LLMOriginState)

    def normalize(self) -> "SessionMetadata":
        """就地归一化列表字段与用量字段，返回自身。"""
        self.discovered_tools = sorted_unique(self.discovered_tools)
        self.tool_cache = tool_cache_items_normalized(self.tool_cache)
        self.tool_tags = sorted_unique(self.tool_tags)
        self.shown_rule_card_formats = sorted_unique(self.shown_rule_card_formats)
        self.workspace_agent_notice_dirs = sorted_unique(self.workspace_agent_notice_dirs)
        if self.last_usage is not None and not _usage_nonzero(self.last_usage):
            self.last_usage = None
        return self


def decode_session_metadata(raw: str) -> SessionMetadata:
    """解析 ``Session.metadata``。对应 Go ``decodeSessionMetadata``。

    非法 JSON 与 Go 一致地退化成空 metadata（不抛异常）。
    """
    if not raw:
        return SessionMetadata()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return SessionMetadata().normalize()
    if not isinstance(parsed, dict):
        return SessionMetadata().normalize()
    usage_raw = parsed.get("last_usage")
    metadata = SessionMetadata(
        discovered_tools=_string_list(parsed.get("discovered_tools")),
        tool_cache=_tool_cache_list(parsed.get("tool_cache")),
        tool_tags=_string_list(parsed.get("tool_tags")),
        shown_rule_card_formats=_string_list(parsed.get("shown_rule_card_formats")),
        last_usage=_usage_from_json(usage_raw),
        background_kind=str(parsed.get("background_kind") or ""),
        workspace_dir=str(parsed.get("workspace_dir") or ""),
        workspace_agent_notice_dirs=_string_list(parsed.get("workspace_agent_notice_dirs")),
        context_compact=ContextCompactState.from_json(parsed.get("context_compact")),
        title_renamed=bool(parsed.get("title_renamed")),
        title_source=str(parsed.get("title_source") or ""),
        llm_origin=LLMOriginState.from_json(parsed.get("llm_origin")),
    )
    return metadata.normalize()


def encode_session_metadata(metadata: SessionMetadata) -> str:
    """编码成 JSON 字符串。对应 Go ``encodeSessionMetadata``。"""
    return encode_session_metadata_into("", metadata)


def encode_session_metadata_into(raw: str, metadata: SessionMetadata) -> str:
    """把强类型字段合并进既有 JSON（保留未知键）。对应 Go ``encodeSessionMetadataInto``。"""
    metadata.normalize()
    base: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            base = parsed
    compact = metadata.context_compact.to_json() if metadata.context_compact else None
    origin = metadata.llm_origin.to_json()
    set_metadata_field(base, "discovered_tools", list(metadata.discovered_tools))
    set_metadata_field(base, "tool_cache", _tool_cache_json(metadata.tool_cache))
    set_metadata_field(base, "tool_tags", list(metadata.tool_tags))
    set_metadata_field(base, "shown_rule_card_formats", list(metadata.shown_rule_card_formats))
    set_metadata_field(base, "last_usage", _usage_to_json(metadata.last_usage))
    set_metadata_field(base, "background_kind", metadata.background_kind)
    set_metadata_field(base, "workspace_dir", metadata.workspace_dir)
    set_metadata_field(
        base, "workspace_agent_notice_dirs", list(metadata.workspace_agent_notice_dirs)
    )
    set_metadata_field(base, "context_compact", compact)
    set_metadata_field(base, "title_renamed", metadata.title_renamed)
    set_metadata_field(base, "title_source", metadata.title_source)
    set_metadata_field(base, "llm_origin", origin)
    text = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
    if text == "{}":
        return ""
    return text


def set_metadata_field(data: dict[str, Any], key: str, value: Any) -> None:
    """零值删除、非零写入。对应 Go ``setMetadataField``。

    ``bool`` 的零值是 ``False``，``int`` 的零值是 ``0``：两者都按
    "键不存在"处理，这就是 Go ``omitempty`` 的行为。
    """
    if value is None:
        data.pop(key, None)
        return
    if isinstance(value, bool):
        if not value:
            data.pop(key, None)
            return
    elif isinstance(value, (str, list, tuple, dict)):
        if len(value) == 0:
            data.pop(key, None)
            return
        if isinstance(value, (list, tuple)):
            value = list(value)
    elif isinstance(value, int) and value == 0:
        data.pop(key, None)
        return
    data[key] = value


def sorted_unique(values: list[str] | None) -> list[str]:
    """去空、去重、排序。对应 Go ``sortedUnique``。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    out.sort()
    return out


def tool_cache_items_normalized(items: list[CachedTool] | None) -> list[CachedTool]:
    """工具缓存归一化。对应 Go ``toolCacheItemsNormalized``。"""
    return normalize_cached_tools(items or [])


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------


def _usage_nonzero(usage: Usage) -> bool:
    return bool(
        usage.total_tokens or usage.cache_hit_tokens or usage.prompt_tokens or usage.completion_tokens
    )


def _usage_from_json(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
        cache_hit_tokens=int(raw.get("cache_hit_tokens") or 0),
    )


def _usage_to_json(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None or not _usage_nonzero(usage):
        return None
    out: dict[str, Any] = {}
    set_metadata_field(out, "prompt_tokens", usage.prompt_tokens)
    set_metadata_field(out, "completion_tokens", usage.completion_tokens)
    set_metadata_field(out, "total_tokens", usage.total_tokens)
    set_metadata_field(out, "cache_hit_tokens", usage.cache_hit_tokens)
    return out or None


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def _tool_cache_list(raw: Any) -> list[CachedTool]:
    if not isinstance(raw, list):
        return []
    out: list[CachedTool] = []
    for item in raw:
        tool = _cached_tool_from_json(item)
        if tool is not None:
            out.append(tool)
    return out


def _cached_tool_from_json(raw: Any) -> CachedTool | None:
    if isinstance(raw, CachedTool):
        return raw
    if not isinstance(raw, dict):
        return None
    return CachedTool.from_json(raw)


def _tool_cache_json(items: list[CachedTool]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if hasattr(item, "to_json"):
            out.append(item.to_json())
        else:  # pragma: no cover - 非本仓 CachedTool
            out.append(dict(getattr(item, "__dict__", {})))
    return out
