"""日志类命令的输出渲染（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。

对应 Go：``internal/agent/commands/log.go`` 的格式化部分
（``formatAuditEntries`` / ``formatRuntimeLogEntries`` / ``formatAtriwispLogEntries`` /
``appendLLMUsageFields`` / ``appendRuntimeLogFields`` / ``formatRawLogEntries`` /
``formatDebugLogEntry`` / ``indentDebugBlock`` / ``isDebugQuery`` /
``formatLogEntryTime`` / ``fieldOr`` / ``appendField`` / ``formatDamagedLogNotice``）。

按 PORTING.md §2 从 820 行的 ``log.go`` 拆出。
"""

from __future__ import annotations

from typing import Callable

from atri_x.logging import FieldRunSource, LogEntry, QueryResult

__all__ = [
    "append_field",
    "append_llm_usage_fields",
    "append_runtime_log_fields",
    "field_or",
    "format_atriwisp_log_entries",
    "format_audit_entries",
    "format_damaged_log_notice",
    "format_debug_log_entry",
    "format_log_entry_time",
    "format_raw_log_entries",
    "format_runtime_log_entries",
    "indent_debug_block",
    "is_debug_query",
]

_RUNTIME_FIELD_ORDER = (
    "event", "point", "hook", "priority", "order", "mode",
    "hook_point", "hook_mode",
    "kind", "name", "platform",
    "session_id", "request_id", "request_kind", "request_phase",
    "actor_id", "actor_role",
    "provider", "model", "tool", "risk",
    "text", "raw_text", "first_system_message_json", "arguments", "result",
    "success", "error",
)


def format_damaged_log_notice(raw: bool, scanned: QueryResult) -> str:
    """汇报本次扫描跳过了多少损坏行。对应 Go ``formatDamagedLogNotice``。

    这些行既不占用 ``--limit`` 配额，也不在结果里冒充记录，但"结果可能不完整"
    这件事必须让用户看到；``--raw`` 时另外列出样本。
    """
    if scanned.damaged_count <= 0:
        return ""
    out = [
        f"\n\n本次扫描跳过 {scanned.damaged_count} 条损坏记录"
        "（无法解析 time/level/msg），结果可能不完整。"
    ]
    if raw and scanned.damaged_lines:
        out.append("\n损坏记录（不参与筛选匹配）：")
        for line in scanned.damaged_lines:
            out.append("\n  " + line)
        remaining = scanned.damaged_count - len(scanned.damaged_lines)
        if remaining > 0:
            out.append(f"\n  另有 {remaining} 条损坏记录未展示。")
    return "".join(out)


def format_log_entry_time(entry: LogEntry) -> str:
    """一条日志的时间列。对应 Go ``formatLogEntryTime``。"""
    if entry.time is None:
        return "unknown-time"
    return entry.time.strftime("%Y-%m-%d %H:%M:%S")


def field_or(fields: dict[str, str], key: str, fallback: str) -> str:
    """取字段值，空则回退。对应 Go ``fieldOr``。"""
    value = fields.get(key, "")
    if value != "":
        return value
    return fallback


def append_field(out: list[str], label: str, value: str) -> None:
    """追加 `` label=value``（空值跳过）。对应 Go ``appendField``。"""
    value = (value or "").strip()
    if value == "":
        return
    out.append(f" {label}={value}")


def append_llm_usage_fields(out: list[str], fields: dict[str, str]) -> None:
    """追加 token 用量与缓存命中率。对应 Go ``appendLLMUsageFields``。"""
    prompt = (fields.get("prompt_tokens") or "").strip()
    completion = (fields.get("completion_tokens") or "").strip()
    total = (fields.get("total_tokens") or "").strip()
    if prompt != "" or completion != "" or total != "":
        out.append(" tokens=")
        out.append(_or_question(prompt))
        out.append("/")
        out.append(_or_question(completion))
        out.append("/")
        out.append(_or_question(total))
    cache_hit = (fields.get("cache_hit_tokens") or "").strip()
    if cache_hit == "":
        return
    out.append(" cache_hit=")
    out.append(cache_hit)
    if prompt == "":
        return
    try:
        cache_hit_tokens = int(cache_hit)
        prompt_tokens = int(prompt)
    except ValueError:
        return
    if prompt_tokens <= 0:
        return
    out.append(f"/{prompt_tokens}({cache_hit_tokens * 100 / prompt_tokens:.1f}%)")


def format_audit_entries(raw: bool) -> Callable[[list[LogEntry]], str]:
    """``/audit`` 的渲染器工厂。对应 Go ``formatAuditEntries``。"""

    def render(entries: list[LogEntry]) -> str:
        if raw:
            return format_raw_log_entries("audit events", entries)
        out: list[str] = ["audit events:\n"]
        for index, entry in enumerate(entries):
            if index > 0:
                out.append("\n")
            fields = entry.fields
            out.append(
                f"  {format_log_entry_time(entry)} "
                f"{field_or(fields, 'event', entry.message)}"
            )
            append_field(out, "session", fields.get("session_id", ""))
            append_field(out, "actor", fields.get("actor_id", ""))
            append_field(out, "tool", fields.get("tool", ""))
            append_field(out, "run_source", fields.get(FieldRunSource, ""))
            append_field(out, "risk", fields.get("risk", ""))
            append_field(out, "provider", fields.get("provider", ""))
            append_field(out, "model", fields.get("model", ""))
            append_field(out, "elapsed_ms", fields.get("elapsed_ms", ""))
            append_llm_usage_fields(out, fields)
            append_field(out, "action", fields.get("action", ""))
            append_field(out, "args", fields.get("arguments", ""))
            append_field(out, "result", fields.get("result", ""))
            append_field(out, "text", fields.get("text", ""))
            append_field(out, "reason", fields.get("reason", ""))
            append_field(out, "error", fields.get("error", ""))
        return "".join(out)

    return render


def format_runtime_log_entries(raw: bool) -> Callable[[list[LogEntry]], str]:
    """``/log`` 的渲染器工厂。对应 Go ``formatRuntimeLogEntries``。"""

    def render(entries: list[LogEntry]) -> str:
        if raw:
            return format_raw_log_entries("runtime logs", entries)
        out: list[str] = ["runtime logs:\n"]
        for index, entry in enumerate(entries):
            if index > 0:
                out.append("\n")
            out.append(
                f"  {format_log_entry_time(entry)} "
                f"{(entry.level or '').lower()} {entry.message}"
            )
            append_runtime_log_fields(out, entry.fields)
        return "".join(out)

    return render


def format_atriwisp_log_entries(raw: bool) -> Callable[[list[LogEntry]], str]:
    """``/atriwisp`` 的渲染器工厂。对应 Go ``formatAtriwispLogEntries``。"""

    def render(entries: list[LogEntry]) -> str:
        if raw:
            return format_raw_log_entries("atriwisp logs", entries)
        out: list[str] = ["atriwisp logs:\n"]
        for index, entry in enumerate(entries):
            if index > 0:
                out.append("\n")
            fields = entry.fields
            out.append(
                f"  {format_log_entry_time(entry)} "
                f"{(entry.level or '').lower()} {entry.message}"
            )
            append_field(out, "atriwisp", fields.get("atriwisp_name", ""))
            append_field(out, "source", fields.get("source", ""))
            append_field(out, "id", fields.get("source_id", ""))
            append_field(out, "mode", fields.get("mode", ""))
            append_field(out, "tags", fields.get("tags", ""))
            append_field(out, "event_key", fields.get("event_key", ""))
            append_field(out, "event_id", fields.get("event_id", ""))
            append_field(out, "token", fields.get("token_name", ""))
            append_field(out, "session", fields.get("session_id", ""))
            append_field(out, "error", fields.get("error", ""))
        return "".join(out)

    return render


def append_runtime_log_fields(out: list[str], fields: dict[str, str]) -> None:
    """追加运行时日志字段（固定顺序）。对应 Go ``appendRuntimeLogFields``。"""
    for key in _RUNTIME_FIELD_ORDER:
        append_field(out, key, fields.get(key, ""))


def format_raw_log_entries(title: str, entries: list[LogEntry]) -> str:
    """``--raw`` 的原始记录渲染。对应 Go ``formatRawLogEntries``。"""
    out: list[str] = [title, ":\n"]
    for index, entry in enumerate(entries):
        if index > 0:
            out.append("\n")
        out.append("  ")
        out.append(format_debug_log_entry(entry))
    return "".join(out)


def format_debug_log_entry(entry: LogEntry) -> str:
    """一条原始记录（``body_json`` 展开成缩进块）。对应 Go ``formatDebugLogEntry``。"""
    body = (entry.fields.get("body_json") or "").strip()
    if body == "":
        return entry.raw
    line = (entry.raw or "").strip()
    index = line.find(" body_json=")
    if index >= 0:
        line = line[:index]
    return line + "\n    body_json:\n" + indent_debug_block(body, "      ")


def indent_debug_block(text: str, prefix: str) -> str:
    """给多行块统一加前缀。对应 Go ``indentDebugBlock``。"""
    return "\n".join(prefix + line for line in text.split("\n"))


def is_debug_query(min_level: str) -> bool:
    """``min_level`` 是不是 debug（大小写不敏感）。对应 Go ``isDebugQuery``。"""
    return (min_level or "").strip().lower() == "debug"


def _or_question(value: str) -> str:
    """Go ``firstNonEmptyLogField(value, "?")``：空白时用 ``?``。"""
    if (value or "").strip() != "":
        return value
    return "?"
