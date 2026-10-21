"""``/tools``：查看工具与当前群的工具授权，群主/管理员可在群里直接授权。

对应 Go：``internal/agent/commands/tool.go``（109 行）。

Python 侧新增（Go 版没有）的两块，都是为了把"群工具白名单"从只写在
``state.toml`` 里变成**群内可见、可授权**：

* ``/tools`` / ``/tools perm`` 会附带当前群的工具白名单状态（继承全局 / 仅白名单 /
  禁止全部），普通成员也能看到"为什么某个工具用不了"；
* ``/tools allow <工具,工具|all|none>`` 把改动转交 ``/grouppolicy tool-allow``，
  由服务端再做一次权限裁决（群主 / 群管理员 / 超级管理员）。

只有 ``reload`` / ``uninstall`` / ``remove`` 这些会改运行态的动作用超级管理员门槛，
列出工具本身对所有用户开放 —— 否则普通群成员连"有哪些工具"都看不到。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Completion, Info, Request, Result
from atri_x.logging import quote_go
from atri_x.security import (
    GroupRoleAdmin,
    GroupRoleOwner,
    RoleSuperadmin,
    RoleUser,
    actor_from_context,
)
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

__all__ = [
    "ToolModule",
    "format_tool_allowlist_status",
    "format_tools",
    "has_flag",
    "new_tools",
]

#: 会改动运行态的 ``/tools`` 动作：只允许超级管理员。
_ADMIN_ACTIONS = ("reload", "uninstall", "remove")

#: 白名单里必须一起放行的工具：没有它，模型连"还有哪些工具可用"都发现不了。
DISCOVER_TOOL = "discover_tool"

#: ``/tools`` 的用法行（help 与未知动作的提示共用）。
_USAGE = (
    "/tools [list|perm|allow <工具名,工具名|all|none>|reload|uninstall|remove] [name]"
)

_HELP = """Usage:
  /tools                         # 列出工具 + 当前群的工具授权状态
  /tools perm                    # 只看当前群的工具授权状态
  /tools allow web_search,web_extract   # 群主/群管理员：只放行这些工具
  /tools allow all               # 群主/群管理员：取消群白名单（继承全局）
  /tools allow none              # 群主/群管理员：禁止本群全部工具
  /tools reload                  # 超级管理员：重载外置 skill
  /tools remove <name> --confirm # 超级管理员：删除外置 skill

说明:
  群白名单写在 state.toml 的 [group_policy."<平台:scope>"]，后台服务 15 秒内热加载，
  也可以在群里用 /grouppolicy tool-allow <...> 修改同一份策略。"""


def has_flag(fields: list[str], flag: str) -> bool:
    """参数里是否出现某个 flag。对应 Go ``hasFlag``。"""
    return flag in fields


def _split_tool_names(raw: str) -> list[str]:
    """把 ``a,b c`` 这类写法拆成工具名（中英文逗号与空白都算分隔符）。"""
    text = (raw or "").replace("，", ",")
    names: list[str] = []
    for chunk in text.replace("\t", ",").replace(" ", ",").split(","):
        name = chunk.strip()
        if name:
            names.append(name)
    return names


def format_tool_allowlist_status(deps: Deps, ctx: Any) -> str:
    """当前群的工具授权状态（未接线时返回空串）。"""
    render = getattr(deps, "tool_allowlist", None)
    if render is None:
        return ""
    try:
        return str(render(ctx) or "").strip()
    except Exception:  # noqa: BLE001 - 状态渲染失败不该让 /tools 整体失败
        return ""


def format_tools(deps: Deps, ctx: Any = None) -> str:
    """列出工具。对应 Go ``formatTools``（Python 侧追加群授权状态）。"""
    infos = deps.tools.list()
    if not infos:
        return "tools: none"
    out = ["tools:\n"]
    for info in infos:
        tags = ""
        if info.tags:
            tags = " tags=" + ",".join(info.tags)
        out.append(f"  {info.name} [{info.source}]{tags} {info.description}\n")
    status = format_tool_allowlist_status(deps, ctx) if ctx is not None else ""
    if status:
        out.append("\n本群工具授权：" + status + "\n")
        out.append("修改：/tools allow <工具名,工具名|all|none>（群主/群管理员）")
        if _in_group(deps, ctx):
            out.append(
                "\n群聊里想看「正在调用哪个工具」：桌面端「工具权限」页打开"
                "「群聊里显示工具调用进度」（[platform.qqonebot]"
                ".show_tool_preview_in_group）"
            )
    return trim_trailing_newlines("".join(out))


def _in_group(deps: Deps, ctx: Any) -> bool:
    """当前会话是不是群聊（只看 scope，不碰平台适配器）。"""
    reader = getattr(deps, "scope", None)
    if not callable(reader) or ctx is None:
        return False
    try:
        scope = reader(ctx)
    except Exception:  # noqa: BLE001 - 只是提示文案，取不到就按私聊处理
        return False
    scope_id = str(getattr(scope, "platform_scope_id", "") or "")
    return scope_id.startswith("group:") or scope_id.startswith("supergroup:")


def _is_superadmin(ctx: Any) -> bool:
    actor, _ok = actor_from_context(ctx)
    return getattr(actor, "role", "") == RoleSuperadmin


def _can_grant_group_tools(ctx: Any) -> bool:
    """群主 / 群管理员 / 超级管理员可以改当前群的工具白名单。"""
    actor, _ok = actor_from_context(ctx)
    if getattr(actor, "role", "") == RoleSuperadmin:
        return True
    return getattr(actor, "group_role", "") in (GroupRoleOwner, GroupRoleAdmin)


class ToolsCommand:
    """对应 Go ``toolsCommand``（Python 侧多 ``perm`` / ``allow`` 两个动作）。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="tools",
            usage=_USAGE,
            description="查看工具与当前群的工具授权，或在群里授权工具。",
            # 列出工具对普通群成员开放；改动类动作在 handle 里单独要求超级管理员。
            min_role=RoleUser,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        deps = self._deps
        fields = (req.args or "").split()
        action = (fields[0] if fields else "").strip().lower()

        if action in ("", "list", "ls"):
            if deps.tools is None:
                return Result(content="tool runtime is not configured")
            return Result(content=format_tools(deps, ctx))

        if action in ("perm", "permission", "permissions"):
            status = format_tool_allowlist_status(deps, ctx)
            if not status:
                return Result(content="当前进程没有接线群工具授权状态。")
            return Result(
                content=trim_trailing_newlines(
                    "本群工具授权："
                    + status
                    + "\n修改："
                    + req.prefix
                    + "tools allow <工具名,工具名|all|none>（群主/群管理员）"
                )
            )

        if action in ("allow", "grant", "deny"):
            return await self._grant(ctx, req, action, fields[1:])

        if deps.tools is None:
            return Result(content="tool runtime is not configured")

        if action in _ADMIN_ACTIONS and not _is_superadmin(ctx):
            return Result(
                content=(
                    f"{req.prefix}tools {action} 需要超级管理员权限；"
                    f"查看可用工具直接发送 {req.prefix}tools。"
                )
            )

        if action == "reload":
            try:
                await _maybe_await(deps.tools.reload(ctx))
            except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成文本返回
                return Result(content=f"reload failed: {exc}")
            return Result(content="skill reload completed")
        if action in ("uninstall", "remove"):
            if len(fields) < 2:
                return Result(content=f"usage: {req.prefix}tools remove <name> --confirm")
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
            except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成字符串返回
                return Result(content=f"remove failed: {exc}")
            return Result(content=f"removed skill: {name}")
        return Result(content=f"usage: {req.prefix}tools [list|perm|allow|reload|remove] [name]")

    async def _grant(
        self, ctx: Any, req: Request, action: str, values: list[str]
    ) -> Result:
        """``/tools allow|deny``：把改动转交群策略服务（服务端会再裁决一次）。"""
        deps = self._deps
        if deps.group_policy is None:
            return Result(content="群工具授权未配置（group policy service 缺失）。")
        if not _can_grant_group_tools(ctx):
            return Result(
                content=(
                    f"{req.prefix}tools {action} 需要群主、群管理员或超级管理员权限。"
                )
            )
        raw = " ".join(values).strip()
        if raw == "":
            return Result(
                content=(
                    f"用法：{req.prefix}tools allow <工具名,工具名|all|none>；"
                    f"例如 {req.prefix}tools allow web_search,web_extract"
                )
            )
        value = raw
        lowered = raw.lower()
        if action == "deny":
            # 没有"从白名单里删一个"的独立策略字段：deny 只作提示，避免误把
            # "继承全局"改成"只允许 X"。
            return Result(
                content=(
                    "暂不支持按工具拒绝；用 "
                    f"{req.prefix}tools allow <要保留的工具,工具> 设置白名单，"
                    f"或 {req.prefix}tools allow none 禁止全部工具。"
                )
            )
        if lowered in ("all", "*", "clear"):
            value = "clear"
        elif lowered in ("none", "off", "empty"):
            value = "none"
        else:
            # 白名单必须带上 discover_tool，否则模型连"还有哪些工具可用"都发现不了。
            names = [item.strip() for item in _split_tool_names(raw) if item.strip()]
            if DISCOVER_TOOL not in names:
                names.insert(0, DISCOVER_TOOL)
            value = ",".join(names)
        try:
            content = await _maybe_await(
                deps.group_policy.set_group_policy(ctx, "tool-allow", value)
            )
        except Exception as exc:  # noqa: BLE001 - Go 里错误渲染成文本返回
            return Result(content=f"工具授权失败：{exc}")
        status = format_tool_allowlist_status(deps, ctx)
        if status:
            content = f"{content}\n本群工具授权：{status}"
        return Result(content=content)

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        fields = (req.args or "").split()
        if not fields or is_first_arg(req, token):
            return complete_static_options(
                [
                    CompletionOption("list", description="列出工具与群工具授权"),
                    CompletionOption("perm", description="只看本群工具授权"),
                    CompletionOption("allow", description="群主/管理员授权工具"),
                    CompletionOption("reload", description="Reload external skills"),
                    CompletionOption("remove", description="Remove an external skill"),
                    CompletionOption("uninstall", description="Remove an external skill"),
                ],
                token.text,
                token.start,
                token.end,
                "tools_action",
            )
        if fields[0] in ("allow", "grant", "deny"):
            if len(fields) <= 2 and not token.text.startswith("--"):
                return [
                    Completion(
                        text=option,
                        label=option,
                        description=description,
                        kind="tools_allow",
                        replace_start=token.start,
                        replace_end=token.end,
                    )
                    for option, description in (
                        ("all", "取消群白名单，继承全局"),
                        ("none", "禁止本群全部工具"),
                    )
                    if option.startswith(token.text)
                ]
            return complete_tool_names(self._deps, token.text, token.start, token.end)
        if fields[0] in ("remove", "uninstall"):
            if len(fields) <= 2 and not token.text.startswith("--"):
                return complete_tool_names(self._deps, token.text, token.start, token.end)
            return complete_confirm_flag(req.args, token)
        return []


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
