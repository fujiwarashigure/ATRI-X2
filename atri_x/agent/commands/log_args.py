"""日志类命令的参数解析（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。

对应 Go：``internal/agent/commands/log.go`` 的参数解析部分
（``splitLogArgs`` / ``nextArg`` / ``parsePositiveInt`` / ``parseLogTime`` /
``parseLogArgs`` / ``parseLogArgsWithOptions`` / ``baseLogQuery`` /
``jsonQuotedLogValue`` / ``parseAuditQuery`` / ``parseRuntimeLogQuery`` /
``parseAtriwispLogQuery`` / ``previousLogToken``）。

从 ``log.go`` 拆出来是因为它 820 行里约一半是"解析"，一半是"格式化"；
按 PORTING.md §2 拆成 :mod:`atri_x.agent.commands.log_args` /
:mod:`atri_x.agent.commands.log_format` / :mod:`atri_x.agent.commands.log` 三个模块。

Go 的 ``nextArg(fields, *index, option)`` 靠指针改调用方的下标；Python 没有指针，
改成 ``(value, new_index)`` 返回，语义等价。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from atri_x.logging import (
    FieldRunSource,
    LogQuery,
    valid_run_source,
)

__all__ = [
    "DEFAULT_LOG_LIST_LIMIT",
    "LogArgOptions",
    "base_log_query",
    "json_quoted_log_value",
    "next_arg",
    "parse_atriwisp_log_query",
    "parse_audit_query",
    "parse_go_duration",
    "parse_log_args",
    "parse_log_args_with_options",
    "parse_log_time",
    "parse_positive_int",
    "parse_runtime_log_query",
    "previous_log_token",
    "split_log_args",
]

DEFAULT_LOG_LIST_LIMIT = 5

# Go ``time.ParseDuration`` 的词法：可选符号 + 若干 "数字+单位"。
_DURATION_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d*)?|\.\d+)([a-zµμ]+)")


def parse_go_duration(value: str) -> float | None:
    """解析 Go ``time.Duration`` 字面量，返回秒数；不合法返回 ``None``。

    对应 Go ``time.ParseDuration`` 的常用子集：``2h`` / ``30m`` / ``1h30m`` /
    ``-1.5h`` / ``300ms`` / ``0``。
    """
    text = (value or "").strip()
    if text == "":
        return None
    if text == "0":
        return 0.0
    sign = 1.0
    if text[0] in "+-":
        if text[0] == "-":
            sign = -1.0
        text = text[1:]
    if text == "":
        return None
    total = 0.0
    position = 0
    while position < len(text):
        match = _DURATION_TOKEN.match(text, position)
        if match is None:
            return None
        raw_number, unit = match.group(1), match.group(2)
        if unit not in _DURATION_UNITS:
            return None
        total += float(raw_number) * _DURATION_UNITS[unit]
        position = match.end()
    return sign * total


def split_log_args(args: str) -> list[str]:
    """按 shell 风格切分日志参数（支持引号与反斜杠转义）。对应 Go ``splitLogArgs``。"""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for rune in args or "":
        if escaped:
            current.append(rune)
            escaped = False
            continue
        if rune == "\\":
            escaped = True
            continue
        if quote != "":
            if rune == quote:
                quote = ""
                continue
            current.append(rune)
            continue
        if rune in ("'", '"'):
            quote = rune
            continue
        if rune in (" ", "\t", "\n", "\r"):
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(rune)
    if escaped:
        current.append("\\")
    if quote != "":
        raise ValueError("unterminated quoted argument")
    if current:
        parts.append("".join(current))
    return parts


def next_arg(fields: list[str], index: int, option: str) -> tuple[str, int]:
    """取下一个参数值。对应 Go ``nextArg``（返回新下标而不是改指针）。"""
    if index + 1 >= len(fields):
        raise ValueError(f"{option} requires a value")
    return fields[index + 1], index + 1


def parse_positive_int(value: str, name: str) -> int:
    """解析一个 >= 1 的整数。对应 Go ``parsePositiveInt``。"""
    try:
        parsed = int((value or "").strip())
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if parsed < 1:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def parse_log_time(value: str) -> datetime:
    """解析 ``--since`` / ``--until`` 的时间参数。对应 Go ``parseLogTime``。

    先试 Go duration（相对现在往前推），再试三种绝对时间格式；后者按**本地时区**
    解释（与 :mod:`atri_x.logging.reader` 解析日志行的口径一致），再折算成 UTC。
    """
    duration = parse_go_duration(value)
    if duration is not None:
        return datetime.now(timezone.utc) - timedelta(seconds=duration)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        # naive.astimezone() 按系统本地时区解释，等价 Go ParseInLocation(..., time.Local)。
        return naive.astimezone().astimezone(timezone.utc)
    raise ValueError(f"invalid time: {value}")


def previous_log_token(raw: str, token_start: int) -> str:
    """当前 token 之前的那个 token。对应 Go ``previousLogToken``。"""
    before = (raw or "")[:token_start].strip()
    if before == "":
        return ""
    fields = before.split()
    if not fields:
        return ""
    return fields[-1]


def json_quoted_log_value(value: str) -> str:
    """把值编码成 Go ``json.Marshal(string)`` 形式的带引号字符串。

    对应 Go ``jsonQuotedLogValue``：``/atriwisp --tag`` 过滤的是日志里以 JSON
    数组形式落盘的 ``tags`` 字段，所以要先按 JSON 规则转义。
    """
    out = ['"']
    for rune in (value or "").strip():
        code = ord(rune)
        if rune == '"':
            out.append('\\"')
        elif rune == "\\":
            out.append("\\\\")
        elif rune == "\n":
            out.append("\\n")
        elif rune == "\r":
            out.append("\\r")
        elif rune == "\t":
            out.append("\\t")
        elif rune in ("<", ">", "&"):
            out.append(f"\\u{code:04x}")
        elif code < 0x20 or code in (0x2028, 0x2029):
            out.append(f"\\u{code:04x}")
        else:
            out.append(rune)
    out.append('"')
    return "".join(out)


def base_log_query(prefix: str) -> LogQuery:
    """日志查询的公共初值。对应 Go ``baseLogQuery``。"""
    return LogQuery(
        prefix=prefix,
        limit=DEFAULT_LOG_LIST_LIMIT,
        days=1,
        field_contains={},
    )


@dataclass
class LogArgOptions:
    """``parseLogArgsWithOptions`` 的开关。对应 Go ``logArgOptions``。

    ``message_filters`` 表示这条命令看的是"消息事件"（``-u`` / ``-a``）：此时
    ``-t`` 不适用，工具调用只记录在 audit（见 ``docs/commands.md`` 的日志和审计）。
    """

    event_filters: bool = False
    system_filter: bool = False
    message_filters: bool = False
    positional: Callable[[str], None] | None = None


def parse_log_args(
    args: str,
    query: LogQuery,
    fields: dict[str, str],
    extra: Callable[[str, str], None],
) -> None:
    """``/audit`` 用的解析入口。对应 Go ``parseLogArgs``。"""
    parse_log_args_with_options(
        args, query, fields, extra, LogArgOptions(event_filters=True)
    )


def parse_log_args_with_options(
    args: str,
    query: LogQuery,
    fields: dict[str, str],
    extra: Callable[[str, str], None],
    opts: LogArgOptions,
) -> None:
    """共享的日志参数解析状态机。对应 Go ``parseLogArgsWithOptions``。"""
    parts = split_log_args(args)
    index = 0
    while index < len(parts):
        name = parts[index]
        if name in ("-n", "--limit"):
            value, index = next_arg(parts, index, name)
            query.limit = parse_positive_int(value, "limit")
        elif name == "--days":
            value, index = next_arg(parts, index, name)
            query.days = parse_positive_int(value, "days")
        elif name == "--since":
            value, index = next_arg(parts, index, name)
            query.since = parse_log_time(value)
        elif name == "--until":
            value, index = next_arg(parts, index, name)
            query.until = parse_log_time(value)
        elif name == "--level":
            value, index = next_arg(parts, index, name)
            # 等级筛选与展示方式互相独立：--level debug 只降低门槛，
            # 不再顺带打开原始记录。
            query.min_level = value
        elif name == "--raw":
            query.raw = True
        elif name == "-d":
            # -d 等价 --level debug；要看原始记录必须显式加 --raw。
            query.min_level = "debug"
        elif name == "-i":
            query.min_level = "info"
        elif name == "-w":
            query.min_level = "warn"
        elif name == "-e":
            query.min_level = "error"
        elif name == "-u":
            if not opts.event_filters:
                raise ValueError(f"unknown option: {name}")
            fields["event"] = "user_message"
        elif name == "-a":
            if not opts.event_filters:
                raise ValueError(f"unknown option: {name}")
            fields["event"] = "assistant_message"
        elif name == "-t":
            if not opts.event_filters or opts.message_filters:
                raise ValueError(f"unknown option: {name}")
            fields["event"] = "tool_call"
        elif name in ("-s", "--system"):
            if not opts.system_filter:
                raise ValueError(f"unknown option: {name}")
            fields["event"] = "system_message"
        elif name == "--hook":
            if not opts.event_filters:
                raise ValueError(f"unknown option: {name}")
            # Hook 记录靠 module 而不是一个虚构的 event 名识别；与 --event
            # 同用时必须保留那个 event 过滤。
            fields["module"] = "hook"
        elif name == "--msg":
            value, index = next_arg(parts, index, name)
            query.msg_contains = value
        elif name == "--contains":
            value, index = next_arg(parts, index, name)
            query.contains = value
        elif name.startswith("--"):
            value, index = next_arg(parts, index, name)
            extra(name[2:], value)
        elif name.startswith("-"):
            raise ValueError(f"unknown option: {name}")
        elif opts.positional is not None:
            opts.positional(name)
        else:
            raise ValueError(f"unexpected argument: {name}")
        index += 1


def parse_audit_query(args: str) -> LogQuery:
    """``/audit`` 的参数解析。对应 Go ``parseAuditQuery``。"""
    query = base_log_query("audit")
    query.min_level = "debug"
    fields: dict[str, str] = {}

    def extra(name: str, value: str) -> None:
        if name == "event":
            fields["event"] = value
        elif name == "risk":
            fields["risk"] = value
        elif name == "actor":
            fields["actor_id"] = value
        elif name == "session":
            fields["session_id"] = value
        elif name == "tool":
            fields["tool"] = value
        elif name == "run_source":
            value = (value or "").strip()
            if not valid_run_source(value):
                raise ValueError(
                    f"invalid --run_source {value!r}: expected foreground, cron or atrinis"
                )
            fields[FieldRunSource] = value
        else:
            raise ValueError(f"unknown option: --{name}")

    # Go 的 ``parseAuditQuery`` 走 ``parseLogArgs``（只开 eventFilters）：
    # ``-t`` / ``-u`` / ``-a`` / ``--hook`` 可用，``-s`` / ``--system`` 不可用。
    parse_log_args(args, query, fields, extra)
    if fields.get("event") == "user_input":
        fields["event"] = "user_message"
    if fields.get("event") == "assistant_output":
        fields["event"] = "assistant_message"
    query.fields = fields
    return query


def parse_runtime_log_query(args: str) -> LogQuery:
    """``/log`` 的参数解析。对应 Go ``parseRuntimeLogQuery``。"""
    query = base_log_query("atri-x")
    query.min_level = "debug"
    fields: dict[str, str] = {}

    def extra(name: str, _value: str) -> None:
        raise ValueError(f"unknown option: --{name}")

    parse_log_args_with_options(
        args,
        query,
        fields,
        extra,
        LogArgOptions(event_filters=True, system_filter=True, message_filters=True),
    )
    query.fields = fields
    return query


def parse_atriwisp_log_query(args: str) -> LogQuery:
    """``/atriwisp`` 的参数解析。对应 Go ``parseAtriwispLogQuery``。"""
    query = base_log_query("atrinis")
    query.min_level = "debug"
    fields: dict[str, str] = {}

    def set_atriwisp_name(value: str) -> None:
        if fields.get("atriwisp_name", "") != "":
            raise ValueError("atriwisp name specified more than once")
        fields["atriwisp_name"] = value

    def extra(name: str, value: str) -> None:
        if name in ("name", "atriwisp"):
            set_atriwisp_name(value)
        elif name == "source":
            fields["source"] = value
        elif name in ("id", "source-id"):
            fields["source_id"] = value
        elif name == "mode":
            fields["mode"] = value
        elif name == "event-key":
            fields["event_key"] = value
        elif name == "event-id":
            fields["event_id"] = value
        elif name == "token":
            fields["token_name"] = value
        elif name == "tag":
            query.field_contains.setdefault("tags", []).append(
                json_quoted_log_value(value)
            )
        else:
            raise ValueError(f"unknown option: --{name}")

    parse_log_args_with_options(
        args, query, fields, extra, LogArgOptions(positional=set_atriwisp_name)
    )
    query.fields = fields
    return query
