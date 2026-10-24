"""``/usage``：从审计日志聚合 token 用量。

对应 Go：``internal/agent/commands/usage.go``（385 行）。

``UsageValue`` 复刻 Go ``usageValue`` 的三态语义：**字段缺失、非法、溢出都算
"未知"**，输出 ``null``；只有显式的 ``0`` 是已知的 0。未知会向上传递到受影响
的分组与总计项 —— 否则"没记录"会和"真的是 0"混成同一样东西，让统计看起来完整
而实际不是。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.command import Completion, Info, Request, Result
from atri_x.logging import LogEntry, LogQuery
from atri_x.security import RoleSuperadmin
from atri_x.agent.commands.completion import (
    CompletionOption,
    complete_static_options,
    complete_string_options,
    current_completion_token,
)
from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.help import format_command_help, wants_command_help
from atri_x.agent.commands.log_args import (
    next_arg,
    parse_log_time,
    parse_positive_int,
    previous_log_token,
    split_log_args,
)

__all__ = [
    "USAGE_QUERY_LIMIT",
    "UsageBucket",
    "UsageGroupKey",
    "UsageValue",
    "format_elapsed",
    "format_thousands",
    "format_usage_bucket",
    "format_usage_summary",
    "known_usage_value",
    "new_usage",
    "parse_usage_query",
    "parse_usage_value",
    "usage_damaged_notice",
    "usage_group_key_for",
    "usage_info",
    "usage_range_desc",
]

USAGE_QUERY_LIMIT = 100000

_USAGE_HELP = """Options:
  -d, --days <n>        Days to look back. Default: 1.
  -m, --model <name>    Filter by model name.
  -s, --session <id>    Filter by session ID.
  --by <key>            Group by: model (default), day, session, trigger.
  --since <time>        Show usage after a time, e.g. 2h, 30m, 2026-06-03.
  --until <time>        Show usage before a time.

Examples:
  /usage
  /usage -d 7
  /usage -m gpt-4o
  /usage -s sess-xxx
  /usage --by day -d 30
  /usage --by trigger
  /usage --since 2h"""


class UsageGroupKey:
    """分组维度。对应 Go ``usageGroupKey`` 及其常量。

    本仓扩展 ``TRIGGER``：按 ``reason`` 归因（``direct_message`` / ``cron`` /
    ``side:compaction`` 等，见 :mod:`atri_x.agent.call_reason`）。
    """

    MODEL = "model"
    DAY = "day"
    SESSION = "session"
    TRIGGER = "trigger"


def usage_info() -> Info:
    """``/usage`` 的 ``Info``。对应 Go ``usageInfo``。"""
    return Info(
        name="usage",
        usage="/usage [options]",
        description="Aggregate token usage from audit logs.",
        min_role=RoleSuperadmin,
        help=_USAGE_HELP,
    )


@dataclass
class UsageValue:
    """"已知数值 / 未知"的三态值。对应 Go ``usageValue``。"""

    known: bool = False
    value: int = 0

    def add(self, other: "UsageValue") -> "UsageValue":
        """并入一个来源值：任一来源未知，累计结果就未知。对应 Go ``add``。"""
        if not self.known or not other.known:
            return UsageValue()
        return UsageValue(known=True, value=self.value + other.value)

    def format(self) -> str:
        """渲染成 ``null`` 或带千分位的数字。对应 Go ``format``。"""
        if not self.known:
            return "null"
        return format_thousands(self.value)


def known_usage_value(value: int) -> UsageValue:
    """构造一个已知数值；累计器必须用它起步。对应 Go ``knownUsageValue``。"""
    return UsageValue(known=True, value=value)


def parse_usage_value(raw: str) -> UsageValue:
    """解析一个用量字段。对应 Go ``parseUsageValue``。"""
    raw = (raw or "").strip()
    if raw == "":
        return UsageValue()
    try:
        return known_usage_value(int(raw))
    except ValueError:
        return UsageValue()


@dataclass
class UsageBucket:
    """一个分组（或总计）的累计器。对应 Go ``usageBucket``。"""

    key: str = ""
    calls: int = 0
    prompt_tokens: UsageValue = field(default_factory=UsageValue)
    completion_tokens: UsageValue = field(default_factory=UsageValue)
    total_tokens: UsageValue = field(default_factory=UsageValue)
    cache_hit_tokens: UsageValue = field(default_factory=UsageValue)
    elapsed_ms: UsageValue = field(default_factory=UsageValue)

    def add_entry(self, entry: LogEntry) -> None:
        """并入一条日志。对应 Go ``addEntry``。"""
        self.calls += 1
        fields = entry.fields
        self.prompt_tokens = self.prompt_tokens.add(
            parse_usage_value(fields.get("prompt_tokens", ""))
        )
        self.completion_tokens = self.completion_tokens.add(
            parse_usage_value(fields.get("completion_tokens", ""))
        )
        self.total_tokens = self.total_tokens.add(
            parse_usage_value(fields.get("total_tokens", ""))
        )
        self.cache_hit_tokens = self.cache_hit_tokens.add(
            parse_usage_value(fields.get("cache_hit_tokens", ""))
        )
        self.elapsed_ms = self.elapsed_ms.add(
            parse_usage_value(fields.get("elapsed_ms", ""))
        )


def new_usage_bucket(key: str) -> UsageBucket:
    """对应 Go ``newUsageBucket``（五个累计器都从已知 0 起步）。"""
    return UsageBucket(
        key=key,
        prompt_tokens=known_usage_value(0),
        completion_tokens=known_usage_value(0),
        total_tokens=known_usage_value(0),
        cache_hit_tokens=known_usage_value(0),
        elapsed_ms=known_usage_value(0),
    )


def parse_usage_query(args: str) -> tuple[LogQuery, str]:
    """解析 ``/usage`` 的参数。对应 Go ``parseUsageQuery``。"""
    query = LogQuery(
        prefix="audit",
        limit=USAGE_QUERY_LIMIT,
        days=1,
        min_level="debug",
        fields={"event": "llm_usage"},
        field_contains={},
    )
    group_by = UsageGroupKey.MODEL

    parts = split_log_args(args)
    index = 0
    while index < len(parts):
        name = parts[index]
        if name in ("-d", "--days"):
            value, index = next_arg(parts, index, name)
            query.days = parse_positive_int(value, "days")
        elif name in ("-m", "--model"):
            value, index = next_arg(parts, index, name)
            query.fields["model"] = value
        elif name in ("-s", "--session"):
            value, index = next_arg(parts, index, name)
            query.fields["session_id"] = value
        elif name == "--by":
            value, index = next_arg(parts, index, name)
            normalized = (value or "").strip().lower()
            if normalized not in (
                UsageGroupKey.MODEL,
                UsageGroupKey.DAY,
                UsageGroupKey.SESSION,
                UsageGroupKey.TRIGGER,
            ):
                raise ValueError("--by must be model, day, session or trigger")
            group_by = normalized
        elif name == "--since":
            value, index = next_arg(parts, index, name)
            query.since = parse_log_time(value)
        elif name == "--until":
            value, index = next_arg(parts, index, name)
            query.until = parse_log_time(value)
        else:
            raise ValueError(f"unknown option: {name}")
        index += 1
    return query, group_by


def usage_damaged_notice(scanned: Any) -> str:
    """提醒本次统计跳过了损坏行。对应 Go ``usageDamagedNotice``。

    用量统计本来就可能因为"没有记录用量"而偏高或偏低，但整行读不出来是另一种
    不完整，必须与 ``null`` 区分开。
    """
    if scanned.damaged_count <= 0:
        return ""
    return (
        f"\n\n本次扫描跳过 {scanned.damaged_count} 条损坏记录"
        "（无法解析 time/level/msg），用量统计可能不完整。"
    )


def usage_group_key_for(entry: LogEntry, group_by: str) -> str:
    """按分组维度算出一个日志条目的 key。对应 Go ``usageGroupKeyFor``。"""
    if group_by == UsageGroupKey.DAY:
        if entry.time is None:
            return "unknown-date"
        return entry.time.strftime("%Y-%m-%d")
    if group_by == UsageGroupKey.SESSION:
        value = entry.fields.get("session_id", "")
        return value if value != "" else "unknown-session"
    if group_by == UsageGroupKey.TRIGGER:
        value = (entry.fields.get("reason") or "").strip()
        return value if value != "" else "unknown-trigger"
    provider = (entry.fields.get("provider") or "").strip()
    model = (entry.fields.get("model") or "").strip()
    if provider != "" and model != "":
        return provider + "/" + model
    for candidate in (model, provider):
        if candidate != "":
            return candidate
    return "unknown-model"


def usage_range_desc(query: LogQuery) -> str:
    """描述统计区间。对应 Go ``usageRangeDesc``。"""
    parts: list[str] = []
    if query.since is not None:
        parts.append("since " + query.since.strftime("%Y-%m-%d %H:%M"))
    if query.until is not None:
        parts.append("until " + query.until.strftime("%Y-%m-%d %H:%M"))
    if parts:
        return ", ".join(parts)
    if query.days > 0:
        return f"last {query.days} day(s)"
    return "all time"


def format_thousands(n: int) -> str:
    """按千分位渲染整数（Go 侧手写，不用 ``%v``）。对应 Go ``formatThousands``。"""
    if n == 0:
        return "0"
    negative = n < 0
    if negative:
        n = -n
    digits = str(n)
    if len(digits) <= 3:
        return ("-" if negative else "") + digits
    out: list[str] = []
    rem = len(digits) % 3
    if rem > 0:
        out.append(digits[:rem])
        if len(digits) > rem:
            out.append(",")
    for i in range(rem, len(digits), 3):
        out.append(digits[i : i + 3])
        if i + 3 < len(digits):
            out.append(",")
    return ("-" if negative else "") + "".join(out)


def format_elapsed(elapsed: UsageValue) -> str:
    """毫秒渲染成秒。对应 Go ``formatElapsed``。"""
    if not elapsed.known:
        return "null"
    return f"{elapsed.value / 1000:.1f}s"


def format_usage_bucket(out: list[str], label: str, bucket: UsageBucket) -> None:
    """渲染一个分组。对应 Go ``formatUsageBucket``。"""
    out.append(f"[{label}] calls: {bucket.calls}\n")
    out.append(
        f"  prompt: {bucket.prompt_tokens.format()} | "
        f"completion: {bucket.completion_tokens.format()} | "
        f"total: {bucket.total_tokens.format()}\n"
    )
    out.append(
        f"  cache: {bucket.cache_hit_tokens.format()} | "
        f"elapsed: {format_elapsed(bucket.elapsed_ms)}\n"
    )


def format_usage_summary(
    entries: list[LogEntry], query: LogQuery, group_by: str
) -> str:
    """渲染完整的 ``/usage`` 报表。对应 Go ``formatUsageSummary``。"""
    buckets: dict[str, UsageBucket] = {}
    order: list[str] = []
    grand = new_usage_bucket("total")

    for entry in entries:
        key = usage_group_key_for(entry, group_by)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = new_usage_bucket(key)
            buckets[key] = bucket
            order.append(key)
        bucket.add_entry(entry)
        grand.add_entry(entry)

    order.sort()

    out: list[str] = [f"token usage ({usage_range_desc(query)}, by {group_by}):\n\n"]
    for key in order:
        format_usage_bucket(out, key, buckets[key])
        out.append("\n")
    out.append("─────────────────────────────\n")
    format_usage_bucket(out, "total", grand)
    notice = unpriced_usage_notice(entries)
    if notice:
        out.append(notice)
    return "".join(out)


def unpriced_usage_notice(entries: list[LogEntry]) -> str:
    """费用未知的调用提示。

    ``price_known=false``（价格表里没有这个模型）与 ``usage_source=side``（旁路
    调用只统计 token、不算费用）都属于"费用未知"。未知**不是** 0，不能当成免费。
    """
    unknown = 0
    side = 0
    for entry in entries:
        price_known = (entry.fields.get("price_known") or "").strip().lower()
        if price_known in ("false", "0"):
            unknown += 1
        if (entry.fields.get("usage_source") or "").strip().lower() == "side":
            side += 1
    if unknown == 0 and side == 0:
        return ""
    parts = ["\n\n"]
    if unknown:
        parts.append(f"{unknown} 次调用的模型价格未知（费用未计入）")
    if side:
        if unknown:
            parts.append("；")
        parts.append(f"{side} 次旁路调用（压缩 / 群分析 / 视图 / 命名 / 改写）只统计 token")
    parts.append("。以上都不等于 0 费用。\n")
    return "".join(parts)


def usage_completion_options() -> list[CompletionOption]:
    """``/usage`` 的选项补全。对应 Go ``usageCompletionOptions``。"""
    return [
        CompletionOption("-d", description="Days to look back"),
        CompletionOption("--days", description="Days to look back"),
        CompletionOption("-m", description="Filter by model"),
        CompletionOption("--model", description="Filter by model"),
        CompletionOption("-s", description="Filter by session"),
        CompletionOption("--session", description="Filter by session"),
        CompletionOption("--by", description="Group by model/day/session/trigger"),
        CompletionOption("--since", description="Show after time"),
        CompletionOption("--until", description="Show before time"),
    ]


class _UsageCommand:
    """对应 Go ``usageCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return usage_info()

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if wants_command_help(req.args):
            return format_command_help(req.prefix, usage_info())
        query, group_by = parse_usage_query(req.args)
        if self._deps.logs is None:
            return Result(content="log reader is not configured")
        scanned = self._deps.logs.query_logs(ctx, query)
        if hasattr(scanned, "__await__"):  # 实现可能同步也可能异步
            scanned = await scanned
        if not scanned.entries:
            return Result(
                content=("no usage data found\n" + usage_damaged_notice(scanned)).strip()
            )
        return Result(
            content=format_usage_summary(scanned.entries, query, group_by)
            + usage_damaged_notice(scanned)
        )

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        previous = previous_log_token(req.raw, token.start)
        if previous == "--by":
            return complete_string_options(
                [
                    UsageGroupKey.MODEL,
                    UsageGroupKey.DAY,
                    UsageGroupKey.SESSION,
                    UsageGroupKey.TRIGGER,
                ],
                token.text,
                token.start,
                token.end,
                "group_by",
            )
        if token.text.startswith("-") or token.text == "":
            return complete_static_options(
                usage_completion_options(),
                token.text,
                token.start,
                token.end,
                "option",
            )
        return []


def new_usage(deps: Deps) -> Any:
    """对应 Go ``NewUsage``。"""
    return _UsageCommand(deps)
