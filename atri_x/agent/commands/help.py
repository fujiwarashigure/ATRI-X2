"""``/help`` 命令与帮助文本渲染。

对应 Go：``internal/agent/commands/help.go`` + ``help_util.go``。

两个 Go 文件都不足 90 行且互相依赖（``help.go`` 调 ``formatCommandHelp``），
按 PORTING.md §2 合并成一个模块。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Completion, CompletionRequest, Info, Request, Result
from atri_x.security import RoleUser, actor_from_context
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = [
    "HelpModule",
    "canonical_help_text",
    "command_usage",
    "format_command_help",
    "new_help",
    "rewrite_help_prefix",
    "trim_trailing_newlines",
    "wants_command_help",
]

_HELP_TEXT = """Usage:
  /help
  /help <command>

Examples:
  /help audit
  /help log"""


def wants_command_help(args: str) -> bool:
    """参数是不是 ``--help`` / ``-h``。对应 Go ``wantsCommandHelp``。"""
    arg = (args or "").strip()
    return arg in ("--help", "-h")


def trim_trailing_newlines(text: str) -> str:
    """对应 Go ``trimTrailingNewlines``（只去尾部换行，不去空格）。"""
    return (text or "").rstrip("\n")


def rewrite_help_prefix(prefix: str, text: str) -> str:
    """把帮助文本中行首的 ``/`` 命令示例替换成配置的前缀。

    对应 Go ``rewriteHelpPrefix``。
    """
    if prefix == "" or prefix == "/" or text == "":
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        trimmed = line.lstrip(" \t")
        if trimmed.startswith("/"):
            indent = line[: len(line) - len(trimmed)]
            lines[i] = indent + prefix + trimmed[1:]
    return "\n".join(lines)


def command_usage(prefix: str, info: Info) -> str:
    """一条命令在当前前缀下的用法行。对应 Go ``commandUsage``。"""
    usage = info.usage
    if usage == "":
        usage = prefix + info.name
    if prefix != "/" and usage.startswith("/"):
        usage = prefix + usage[1:]
    return usage


def format_command_help(prefix: str, info: Info) -> Result:
    """把一条命令的 ``Info`` 渲染成详细帮助。对应 Go ``formatCommandHelp``。"""
    out = ["command: " + info.name + "\n"]
    out.append("usage: " + command_usage(prefix, info) + "\n")
    if info.aliases:
        out.append("aliases: " + ", ".join(info.aliases) + "\n")
    if info.description:
        out.append("description: " + info.description + "\n")
    if (info.help or "").strip():
        out.append("\n")
        out.append(rewrite_help_prefix(prefix, info.help.strip()))
        out.append("\n")
    return Result(content=trim_trailing_newlines("".join(out)))


def canonical_help_text() -> str:
    """``/help`` 命令自带的帮助正文（Go 里是 ``Info.Help`` 的原始字符串）。"""
    return _HELP_TEXT


def _help_info() -> Info:
    return Info(
        name="help",
        usage="/help [command]",
        description="Show available commands or detailed command help.",
        min_role=RoleUser,
        help=_HELP_TEXT,
    )


class _HelpCommand:
    """对应 Go ``helpCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return _help_info()

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        arg = (req.args or "").strip()
        if arg != "":
            return await _detailed_help(ctx, req.prefix, self._deps, arg)

        actor, _ = actor_from_context(ctx)
        out = ["available commands:\n"]
        for info in self._deps.router.commands_for_actor(actor):
            usage = command_usage(req.prefix, info)
            out.append(f"  {usage:<24} {info.description}\n")
        out.append(f"\nUse {req.prefix}help <command> for details.")
        return Result(content="".join(out))

    async def complete(self, ctx: Any, req: CompletionRequest) -> list[Completion]:
        raw_router = self._deps.router
        if raw_router is None or any(
            ch in (req.args or "").strip() for ch in " \t"
        ):
            return []
        query = (req.args or "").strip()
        args_start = len(req.prefix) + len(req.name)
        if len(req.raw) > args_start:
            args_start += 1
        out: list[Completion] = []
        actor, _ = actor_from_context(ctx)
        for info in raw_router.commands_for_actor(actor):
            name = (info.name or "").strip()
            if name == "" or not name.startswith(query):
                continue
            out.append(
                Completion(
                    text=name,
                    label=name,
                    description=info.description,
                    kind="command_arg",
                    replace_start=args_start,
                    replace_end=req.cursor,
                )
            )
        return out


async def _detailed_help(
    ctx: Any, prefix: str, deps: Deps, name: str
) -> Result:
    """对应 Go ``detailedHelp``。"""
    actor, _ = actor_from_context(ctx)
    info, ok = deps.router.command_info_for_actor(name, actor)
    if not ok or info is None:
        return Result(content="unknown command: " + (name or "").strip())
    return format_command_help(prefix, info)


def new_help(deps: Deps) -> _HelpCommand:
    """对应 Go ``NewHelp``。"""
    return _HelpCommand(deps)


class HelpModule:
    """对应 Go ``HelpModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_help)
