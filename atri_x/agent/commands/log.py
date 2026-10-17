"""``/log``、``/audit``、``/atriwisp``、``/usage`` 的注册与派发。

对应 Go：``internal/agent/commands/log.go``（820 行）的命令部分 +
``LogModule``。参数解析见 :mod:`atri_x.agent.commands.log_args`，渲染见
:mod:`atri_x.agent.commands.log_format`；``/usage`` 自己的实现见
:mod:`atri_x.agent.commands.usage`（Go 里就在 ``usage.go``）。
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.command import Completion, Info, Request, Result
from atri_x.logging import (
    LogEntry,
    LogQuery,
    RunSourceAtrinis,
    RunSourceCron,
    RunSourceForeground,
)
from atri_x.agent.commands.completion import (
    CompletionOption,
    complete_static_options,
    complete_string_options,
    complete_tool_names,
    current_completion_token,
)
from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.agent.commands.help import format_command_help, wants_command_help
from atri_x.agent.commands.log_args import (
    parse_atriwisp_log_query,
    parse_audit_query,
    parse_runtime_log_query,
    previous_log_token,
)
from atri_x.agent.commands.log_format import (
    format_atriwisp_log_entries,
    format_audit_entries,
    format_damaged_log_notice,
    format_runtime_log_entries,
)

__all__ = [
    "LogModule",
    "atriwisp_info",
    "audit_info",
    "log_completion_options",
    "log_info",
    "new_atriwisp",
    "new_audit",
    "new_log",
    "query_logs",
]

_AUDIT_HELP = """Options:
  -n, --limit <n>       Number of events to show. Default: 5.
  --days <n>            Read logs from the last n days. Default: 1.
  --level <level>       Minimum level: debug, info, warn, error. Default: debug.
  -d, -i, -w, -e       Shorthand for --level debug/info/warn/error.
  --raw                Show original physical log records.
  -u, -a, -t           Filter user/assistant/tool events.
  --hook               Filter hook events.
  --since <time>        Show events after a time, e.g. 2h, 30m, 2026-06-03, 2026-06-03T15:04:05.

  --until <time>        Show events before a time.
  --event <name>        Filter by event, e.g. tool_call, llm_usage, permission_denied.
  --risk <level>        Filter by risk.
  --actor <id>          Filter by actor_id.
  --session <id>        Filter by session_id.
  --tool <name>         Filter by tool.
  --run_source <name>   Filter tool calls by run source: foreground, cron, atrinis.
  --msg <text>          Filter by msg field.
  --contains <text>     Filter by text/arguments/result/raw fields.

Examples:
  /audit
  /audit --event tool_call --risk high -n 10
  /audit -t --run_source cron -n 20
  /audit -d --contains "hello"
  /audit --actor cli:local --since 24h"""

_ATRIWISP_HELP = """Options:
  [name]                Filter by Atriwisp name. Equivalent to --name.
  -n, --limit <n>       Number of log lines to show. Default: 5.
  --days <n>            Read logs from the last n days. Default: 1.
  --level <level>       Minimum level: debug, info, warn, error. Default: debug.
  -d, -i, -w, -e       Shorthand for --level debug/info/warn/error.
  --raw                Show original physical log records.
  --since <time>        Show logs after a time, e.g. 2h, 30m, 2026-06-03, 2026-06-03T15:04:05.
  --until <time>        Show logs before a time.
  --name <name>         Filter by Atriwisp name.
  --atriwisp <name>       Alias of --name.
  --source <source>     Filter by event source.
  --id <id>             Filter by external source id.
  --source-id <id>      Alias of --id.
  --mode <mode>         Filter by mode: record, direct, llm.
  --event-key <key>     Filter by Atrinis event key.
  --event-id <id>       Filter by internal Atrinis event id.
  --token <name>        Filter by Atrinis token name.
  --tag <tag>           Filter by Atriwisp tag. Can be repeated.
  --msg <text>          Filter by msg field.
  --contains <text>     Filter by text/arguments/result/raw fields.

Examples:
  /atriwisp
  /atriwisp server-watchdog -n 20
  /atriwisp --name server-watchdog --since 2h
  /atriwisp --source minecraft-main --mode llm -w"""

_LOG_HELP = """Options:
  -n, --limit <n>       Number of log lines to show. Default: 5.
  --days <n>            Read logs from the last n days. Default: 1.
  --level <level>       Minimum level: debug, info, warn, error. Default: debug.
  -d, -i, -w, -e       Shorthand for --level debug/info/warn/error.
  --raw                Show original physical log records.
  -u, -a               Filter user/assistant events.
  -s, --system         Filter system prompt events.
  --hook               Filter hook events.
  --since <time>        Show logs after a time, e.g. 2h, 30m, 2026-06-03, 2026-06-03T15:04:05.

  --until <time>        Show logs before a time.
  --msg <text>          Filter by msg field.
  --contains <text>     Filter by text/arguments/result/raw fields.

Examples:
  /log
  /log -w -n 10
  /log --msg startup --days 3"""


def audit_info() -> Info:
    """``/audit`` 的 ``Info``。对应 Go ``auditInfo``。"""
    return Info(
        name="audit",
        usage="/audit [options]",
        description="Show recent audit events.",
        help=_AUDIT_HELP,
    )


def atriwisp_info() -> Info:
    """``/atriwisp`` 的 ``Info``。对应 Go ``atriwispInfo``。"""
    return Info(
        name="atriwisp",
        usage="/atriwisp [name] [options]",
        description="Show recent Atrinis/Atriwisp logs.",
        help=_ATRIWISP_HELP,
    )


def log_info() -> Info:
    """``/log`` 的 ``Info``。对应 Go ``logInfo``。"""
    return Info(
        name="log",
        usage="/log [options]",
        description="Show recent runtime logs.",
        help=_LOG_HELP,
    )


async def query_logs(
    ctx: Any,
    deps: Deps,
    query: LogQuery,
    formatter: Callable[[list[LogEntry]], str],
) -> Result:
    """执行一次日志查询并渲染。对应 Go ``queryLogs``。

    Go 的 ``QueryLogs`` 是同步方法；这里用 ``_maybe_await`` 调用，同步读日志的实现
    与协程实现都能用（``result`` 为普通返回值时不会被 ``await``）。
    """
    if deps.logs is None:
        return Result(content="log reader is not configured")
    scanned = await _maybe_await(deps.logs.query_logs(ctx, query))
    damaged = format_damaged_log_notice(query.raw, scanned)
    if not scanned.entries:
        return Result(content=("no log entries found\n" + damaged).strip())
    return Result(content=formatter(scanned.entries) + damaged)


class LogCommand:
    """``/log`` / ``/audit`` / ``/atriwisp`` 共用的命令体。对应 Go ``logCommand``。"""

    def __init__(
        self,
        deps: Deps,
        info: Info,
        audit: bool = False,
        atriwisp: bool = False,
    ) -> None:
        self._deps = deps
        self._info = info
        self._audit = audit
        self._atriwisp = atriwisp

    def info(self) -> Info:
        return self._info

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if wants_command_help(req.args):
            return format_command_help(req.prefix, self._info)
        if self._audit:
            query = parse_audit_query(req.args)
            return await query_logs(
                ctx, self._deps, query, format_audit_entries(query.raw)
            )
        if self._atriwisp:
            query = parse_atriwisp_log_query(req.args)
            return await query_logs(
                ctx, self._deps, query, format_atriwisp_log_entries(query.raw)
            )
        query = parse_runtime_log_query(req.args)
        return await query_logs(
            ctx, self._deps, query, format_runtime_log_entries(query.raw)
        )

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        previous = previous_log_token(req.raw, token.start)
        if previous == "--level":
            return complete_string_options(
                ["debug", "info", "warn", "error"],
                token.text,
                token.start,
                token.end,
                "log_level",
            )
        if self._audit and previous == "--risk":
            return complete_string_options(
                ["low", "medium", "high", "critical"],
                token.text,
                token.start,
                token.end,
                "risk",
            )
        if self._audit and previous == "--event":
            return complete_string_options(
                [
                    "user_input",
                    "assistant_output",
                    "llm_usage",
                    "tool_call",
                    "permission_denied",
                    "session_resume",
                    "session_fork",
                    "hook",
                ],
                token.text,
                token.start,
                token.end,
                "audit_event",
            )
        if self._audit and previous == "--tool":
            return complete_tool_names(
                self._deps, token.text, token.start, token.end
            )
        if self._audit and previous == "--run_source":
            return complete_string_options(
                [RunSourceForeground, RunSourceCron, RunSourceAtrinis],
                token.text,
                token.start,
                token.end,
                "run_source",
            )
        if self._atriwisp and previous == "--mode":
            return complete_string_options(
                ["record", "direct", "llm"],
                token.text,
                token.start,
                token.end,
                "atrinis_mode",
            )
        if token.text.startswith("-") or token.text == "":
            return complete_static_options(
                log_completion_options(self._audit, self._atriwisp),
                token.text,
                token.start,
                token.end,
                "option",
            )
        return []


def log_completion_options(audit: bool, atriwisp: bool) -> list[CompletionOption]:
    """按命令种类给出选项补全。对应 Go ``logCompletionOptions``。"""
    options = [
        CompletionOption("-n", description="Number of entries"),
        CompletionOption("--limit", description="Number of entries"),
        CompletionOption("--days", description="Read logs from recent days"),
        CompletionOption("--level", description="Minimum level"),
        CompletionOption("--raw", description="Original physical records"),
        CompletionOption("-d", description="Minimum debug level"),
        CompletionOption("-i", description="Info level"),
        CompletionOption("-w", description="Warn level"),
        CompletionOption("-e", description="Error level"),
        CompletionOption("--since", description="Show entries after time"),
        CompletionOption("--until", description="Show entries before time"),
        CompletionOption("--msg", description="Filter msg field"),
        CompletionOption("--contains", description="Filter text fields"),
    ]
    if not atriwisp:
        options.extend(
            [
                CompletionOption("-u", description="User events"),
                CompletionOption("-a", description="Assistant events"),
                CompletionOption("--hook", description="Hook events"),
            ]
        )
    if not audit and not atriwisp:
        options.extend(
            [
                CompletionOption("-s", description="System prompt events"),
                CompletionOption("--system", description="System prompt events"),
            ]
        )
    if audit:
        options.extend(
            [
                CompletionOption("-t", description="Tool events"),
                CompletionOption("--event", description="Filter audit event"),
                CompletionOption("--risk", description="Filter risk level"),
                CompletionOption("--actor", description="Filter actor ID"),
                CompletionOption("--session", description="Filter session ID"),
                CompletionOption("--tool", description="Filter tool name"),
                CompletionOption("--run_source", description="Filter tool run source"),
            ]
        )
    if atriwisp:
        options.extend(
            [
                CompletionOption("--name", description="Filter Atriwisp name"),
                CompletionOption("--atriwisp", description="Filter Atriwisp name"),
                CompletionOption("--source", description="Filter event source"),
                CompletionOption("--tag", description="Filter Atriwisp tag"),
                CompletionOption("--id", description="Filter source event ID"),
                CompletionOption("--source-id", description="Filter source event ID"),
                CompletionOption("--mode", description="Filter Atrinis mode"),
                CompletionOption("--event-key", description="Filter event key"),
                CompletionOption("--event-id", description="Filter internal event ID"),
                CompletionOption("--token", description="Filter token name"),
            ]
        )
    return options


def new_log(deps: Deps) -> Any:
    """对应 Go ``NewLog``。"""
    return LogCommand(deps, log_info())


def new_audit(deps: Deps) -> Any:
    """对应 Go ``NewAudit``。"""
    return LogCommand(deps, audit_info(), audit=True)


def new_atriwisp(deps: Deps) -> Any:
    """对应 Go ``NewAtriwisp``。"""
    return LogCommand(deps, atriwisp_info(), atriwisp=True)


async def _maybe_await(value: Any) -> Any:
    """接口实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


class LogModule:
    """对应 Go ``LogModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        from atri_x.agent.commands.usage import new_usage

        register_factories(registrar, deps, new_log, new_audit, new_atriwisp, new_usage)
