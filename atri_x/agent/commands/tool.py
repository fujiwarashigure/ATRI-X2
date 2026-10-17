"""``/tools``：列出工具或管理外置 skill。

对应 Go：``internal/agent/commands/tool.go``（109 行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Completion, Info, Request, Result
from atri_x.logging import quote_go
from atri_x.agent.commands.completion import (
    CompletionOption,
    complete_confirm_flag,
    complete_static_options,
    complete_tool_names,
    current_completion_token,
    is_first_arg,
)
from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.agent.commands.help import trim_trailing_newlines

__all__ = ["ToolModule", "format_tools", "has_flag", "new_tools"]


def has_flag(fields: list[str], flag: str) -> bool:
    """参数里是否出现某个 flag。对应 Go ``hasFlag``。"""
    return flag in fields


def format_tools(deps: Deps) -> str:
    """列出工具。对应 Go ``formatTools``。"""
    infos = deps.tools.list()
    if not infos:
        return "tools: none"
    out = ["tools:\n"]
    for info in infos:
        tags = ""
        if info.tags:
            tags = " tags=" + ",".join(info.tags)
        out.append(f"  {info.name} [{info.source}]{tags} {info.description}\n")
    return trim_trailing_newlines("".join(out))


class ToolsCommand:
    """对应 Go ``toolsCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="tools",
            usage="/tools [reload|uninstall|remove] [name]",
            description="List tools or manage external skills.",
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        deps = self._deps
        if deps.tools is None:
            return Result(content="tool runtime is not configured")
        fields = (req.args or "").split()
        if not fields:
            return Result(content=format_tools(deps))
        action = fields[0]
        if action == "reload":
            try:
                await _maybe_await(deps.tools.reload(ctx))
            except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成文本返回
                return Result(content=f"reload failed: {exc}")
            return Result(content="skill reload completed")
        if action in ("uninstall", "remove"):
            if len(fields) < 2:
                return Result(
                    content=f"usage: {req.prefix}tools remove <name> --confirm"
                )
            name = fields[1]
            if not has_flag(fields[2:], "--confirm"):
                return Result(
                    content=(
                        f"将删除外置 skill {quote_go(name)} 及其目录。确认请执行："
                        f"{req.prefix}tools remove {name} --confirm"
                    )
                )
            try:
                await _maybe_await(deps.tools.remove(ctx, name))
            except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成文本返回
                return Result(content=f"remove failed: {exc}")
            return Result(content=f"removed skill: {name}")
        return Result(
            content=f"usage: {req.prefix}tools [reload|uninstall|remove] [name]"
        )

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        fields = (req.args or "").split()
        if not fields or is_first_arg(req, token):
            return complete_static_options(
                [
                    CompletionOption("reload", description="Reload external skills"),
                    CompletionOption("remove", description="Remove an external skill"),
                    CompletionOption("uninstall", description="Remove an external skill"),
                ],
                token.text,
                token.start,
                token.end,
                "tools_action",
            )
        if fields[0] not in ("remove", "uninstall"):
            return []
        if len(fields) <= 2 and not token.text.startswith("--"):
            return complete_tool_names(
                self._deps, token.text, token.start, token.end
            )
        return complete_confirm_flag(req.args, token)


def new_tools(deps: Deps) -> Any:
    """对应 Go ``NewTools``。"""
    return ToolsCommand(deps)


async def _maybe_await(value: Any) -> Any:
    """接口实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


class ToolModule:
    """对应 Go ``ToolModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_tools)
