"""``/hooks``：列出/查看 hook、重载配置、控制有状态 hook。

对应 Go：``internal/agent/commands/hook.go``（334 行）。

``internal/hook/runtime`` 的 ``Info``（有状态 hook 的运行视图）在 Python 侧没有
对应模块（``atri_x/hook/`` 只有 ``event`` / ``manager`` / ``match`` / ``process``），
因此这里对 ``hook.Info`` / ``hook.ReloadReport`` / 有状态 hook 视图一律按鸭子类型
消费：只读 ``plugin_id`` / ``name`` / ``point`` / ``priority`` / ``description`` /
``detail`` / ``active`` / ``notices`` 以及 ``id`` / ``mode`` / ``status`` /
``waiting`` 这些属性，不做 ``isinstance`` 判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.command import Completion, Info, Request, Result
from atri_x.agent.commands.completion import (
    CompletionOption,
    complete_static_options,
    current_completion_token,
    is_first_arg,
)
from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.agent.commands.help import trim_trailing_newlines

__all__ = [
    "HOOK_LIST_DESCRIPTION_LIMIT",
    "HookGroup",
    "HookModule",
    "collect_hook_groups",
    "format_hook_detail",
    "format_hook_list",
    "format_hook_reload_result",
    "format_plugin_hook_detail",
    "format_single_hook_detail",
    "hook_completion_description",
    "hook_group_active",
    "hook_group_description",
    "new_hooks",
    "truncate_hook_description",
    "write_plugin_hook_list_line",
    "write_plugin_rule_detail",
    "write_standalone_hook_list_line",
]

HOOK_LIST_DESCRIPTION_LIMIT = 60


@dataclass
class HookGroup:
    """列表里的一组 hook（外置插件按 plugin id 聚合）。对应 Go ``hookGroup``。"""

    name: str = ""
    plugin: bool = False
    infos: list[Any] = field(default_factory=list)
    stateful: Any = None


class HooksCommand:
    """对应 Go ``hooksCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="hooks",
            usage="/hooks [reload|start|stop|restart|<name>]",
            description=(
                "List hooks, inspect hooks, reload configuration, or control "
                "stateful hooks."
            ),
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        deps = self._deps
        if deps.hooks is None:
            return Result(content="hook service is not configured")
        fields = (req.args or "").split()
        if not fields:
            return Result(content=format_hook_list(deps))
        action = fields[0]
        if action == "reload":
            try:
                report = await _maybe_await(deps.hooks.hook_reload())
            except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成文本
                return Result(content=f"hook reload failed: {exc}")
            return Result(content=format_hook_reload_result(report))
        if action in ("start", "stop", "restart"):
            if len(fields) != 2:
                return Result(content=f"usage: {req.prefix}hooks {action} <id>")
            try:
                if action == "start":
                    await _maybe_await(deps.hooks.start_stateful_hook(fields[1]))
                elif action == "stop":
                    await _maybe_await(deps.hooks.stop_hook(ctx, fields[1]))
                else:
                    await _maybe_await(deps.hooks.restart_stateful_hook(ctx, fields[1]))
            except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成文本
                return Result(content=f"hook {action} failed: {exc}")
            return Result(content=f"hook {action} completed: {fields[1]}")
        return Result(content=format_hook_detail(deps, req.prefix, action))

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        if self._deps.hooks is None:
            return []
        token = current_completion_token(req)
        if is_first_arg(req, token):
            options = [
                CompletionOption("reload", description="Reload all hooks"),
                CompletionOption("start", description="Start a stateful hook"),
                CompletionOption("stop", description="Stop a stateful hook"),
                CompletionOption("restart", description="Restart a stateful hook"),
            ]
            for group in collect_hook_groups(self._deps):
                options.append(
                    CompletionOption(
                        group.name, description=hook_completion_description(group)
                    )
                )
            return complete_static_options(
                options, token.text, token.start, token.end, "hooks_arg"
            )
        fields = (req.args or "").split()
        if fields and fields[0] in ("start", "stop", "restart"):
            options = [
                CompletionOption(info.id, description=str(info.status))
                for info in self._deps.hooks.stateful_hooks()
            ]
            return complete_static_options(
                options, token.text, token.start, token.end, "stateful_hook"
            )
        return []


def new_hooks(deps: Deps) -> Any:
    """对应 Go ``NewHooks``。"""
    return HooksCommand(deps)


def collect_hook_groups(deps: Deps) -> list[HookGroup]:
    """按 plugin id 聚合 hook。对应 Go ``collectHookGroups``。"""
    groups: list[HookGroup] = []
    plugins: dict[str, HookGroup] = {}
    for info in deps.hooks.hook_list():
        plugin_id = (info.plugin_id or "").strip()
        if plugin_id == "":
            groups.append(HookGroup(name=info.name, infos=[info]))
            continue
        group = plugins.get(plugin_id)
        if group is None:
            group = HookGroup(name=plugin_id, plugin=True)
            plugins[plugin_id] = group
            groups.append(group)
        group.infos.append(info)
    for info in deps.hooks.stateful_hooks():
        id_ = (info.id or "").strip()
        group = plugins.get(id_)
        if group is None:
            group = HookGroup(name=id_, plugin=True)
            plugins[id_] = group
            groups.append(group)
        group.stateful = info
    # sort.SliceStable：名字相同则"插件组"排在"独立 hook"之前，否则按名字升序。
    groups.sort(key=lambda group: (group.name, not group.plugin))
    return groups


def format_hook_list(deps: Deps) -> str:
    """``/hooks`` 的列表。对应 Go ``formatHookList``。"""
    groups = collect_hook_groups(deps)
    if not groups:
        return "hooks: none"
    out = ["hooks:\n"]
    for group in groups:
        if group.plugin:
            write_plugin_hook_list_line(out, group)
            continue
        write_standalone_hook_list_line(out, group.infos[0])
    return trim_trailing_newlines("".join(out))


def write_plugin_hook_list_line(out: list[str], group: HookGroup) -> None:
    """插件组的列表行。对应 Go ``writePluginHookListLine``。"""
    if group.stateful is not None:
        out.append(f"  {group.name}  [{group.stateful.mode}:{group.stateful.status}]")
    else:
        out.append(f"  {group.name}  [plugin]")
    if group.infos:
        out.append(f"  rules={len(group.infos)}")
    description = hook_group_description(group)
    if description != "":
        out.append(" - " + truncate_hook_description(description))
    if group.stateful is not None:
        if group.stateful.active > 0:
            out.append(f" | {group.stateful.active} actived")
        if group.stateful.waiting > 0:
            out.append(f" | {group.stateful.waiting} waiting")
    else:
        active = hook_group_active(group)
        if active > 0:
            out.append(f" | {active} actived")
    out.append("\n")


def write_standalone_hook_list_line(out: list[str], hook_info: Any) -> None:
    """独立 hook 的列表行。对应 Go ``writeStandaloneHookListLine``。"""
    out.append(
        f"  {hook_info.name}  [{hook_info.point}]  priority={hook_info.priority}"
    )
    description = (hook_info.description or "").strip()
    if description != "":
        out.append(" - " + truncate_hook_description(description))
    if hook_info.active > 0:
        out.append(f" | {hook_info.active} actived")
    out.append("\n")


def hook_group_description(group: HookGroup) -> str:
    """组描述：优先有状态 hook，其次第一条规则。对应 Go ``hookGroupDescription``。"""
    if group.stateful is not None:
        description = (group.stateful.description or "").strip()
        if description != "":
            return description
    for hook_info in group.infos:
        description = (hook_info.description or "").strip()
        if description != "":
            return description
    return ""


def hook_group_active(group: HookGroup) -> int:
    """组内各规则的 active 之和。对应 Go ``hookGroupActive``。"""
    return sum(hook_info.active for hook_info in group.infos)


def hook_completion_description(group: HookGroup) -> str:
    """补全候选的描述。对应 Go ``hookCompletionDescription``。"""
    if not group.plugin:
        return str(group.infos[0].point)
    parts: list[str] = []
    if group.infos:
        parts.append(f"{len(group.infos)} rules")
    if group.stateful is not None:
        parts.append(f"{group.stateful.mode}:{group.stateful.status}")
    return ", ".join(parts)


def format_hook_reload_result(report: Any) -> str:
    """``/hooks reload`` 的结果。对应 Go ``formatHookReloadResult``。"""
    if not report.notices:
        return "hook reload completed"
    out = ["hook reload completed with warnings:\n"]
    for notice in report.notices:
        notice = (notice or "").strip()
        if notice == "":
            continue
        out.append("- ")
        out.append(notice)
        out.append("\n")
    return trim_trailing_newlines("".join(out))


def format_hook_detail(deps: Deps, prefix: str, name: str) -> str:
    """``/hooks <name>`` 的详情。对应 Go ``formatHookDetail``。"""
    groups = collect_hook_groups(deps)
    for group in groups:
        if group.plugin and group.name == name:
            return format_plugin_hook_detail(group)
    for group in groups:
        for hook_info in group.infos:
            if hook_info.name == name:
                return format_single_hook_detail(hook_info)
    return f'hook "{name}" not found. Use {prefix}hooks to list all hooks.'


def format_plugin_hook_detail(group: HookGroup) -> str:
    """插件组详情。对应 Go ``formatPluginHookDetail``。"""
    out = [f"name: {group.name}"]
    if group.stateful is not None:
        out.append(f"\nmode: {group.stateful.mode}\nstatus: {group.stateful.status}")
        if group.stateful.active > 0:
            out.append(f"\nactive: {group.stateful.active}")
        if group.stateful.waiting > 0:
            out.append(f"\nwaiting: {group.stateful.waiting}")
    description = hook_group_description(group)
    if description != "":
        out.append("\ndescription: " + description)
    if group.stateful is not None:
        detail = (group.stateful.detail or "").strip()
        if detail != "":
            out.append("\ndetail: " + detail)
    if group.infos:
        out.append(f"\nrules: {len(group.infos)}")
        for hook_info in group.infos:
            out.append("\n\n")
            write_plugin_rule_detail(out, hook_info, description)
    return "".join(out)


def write_plugin_rule_detail(
    out: list[str], hook_info: Any, plugin_description: str
) -> None:
    """插件组里的一条规则。对应 Go ``writePluginRuleDetail``。"""
    out.append(
        f"rule: {hook_info.name}\npoint: {hook_info.point}\n"
        f"priority: {hook_info.priority}"
    )
    if hook_info.active > 0:
        out.append(f"\nactive: {hook_info.active}")
    description = (hook_info.description or "").strip()
    if description != "" and description != plugin_description:
        out.append("\ndescription: " + description)
    detail = (hook_info.detail or "").strip()
    if detail != "":
        out.append("\n" + detail)


def format_single_hook_detail(hook_info: Any) -> str:
    """独立 hook 详情。对应 Go ``formatSingleHookDetail``。"""
    out = [
        f"name: {hook_info.name}\npoint: {hook_info.point}\n"
        f"priority: {hook_info.priority}"
    ]
    description = (hook_info.description or "").strip()
    if description != "":
        out.append("\ndescription: " + description)
    detail = (hook_info.detail or "").strip()
    if detail != "":
        out.append("\n" + detail)
    return "".join(out)


def truncate_hook_description(description: str) -> str:
    """列表里的描述截断到 60 个码点。对应 Go ``truncateHookDescription``。"""
    description = (description or "").strip()
    if len(description) <= HOOK_LIST_DESCRIPTION_LIMIT:
        return description
    return description[:HOOK_LIST_DESCRIPTION_LIMIT] + "..."


async def _maybe_await(value: Any) -> Any:
    """接口实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


class HookModule:
    """对应 Go ``HookModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_hooks)
