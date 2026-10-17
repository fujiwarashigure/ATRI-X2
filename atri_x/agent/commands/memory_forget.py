"""``/forget``（别名 ``/forget_memory``、``/delete_memory``）：删除长期/常驻记忆。

对应 Go：``internal/agent/commands/memory_forget.go``（179 行）。

群聊中普通成员只能删除"来源成员是自己"的长期记忆；群主、群管理员和超管可以删除
当前群 scope 的任意长期记忆；私聊 scope 中当前用户可删除整个 scope 的记忆。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result
from atri_x.memory.resident import actor_scope
from atri_x.security import RoleUser, actor_from_context
from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.memory_common import (
    audit_command,
    memory_can_delete,
    memory_entry_line,
    memory_parse_limit,
    memory_visible_filter,
    resolve_scoped_memory,
    short_memory_id,
)

__all__ = ["ForgetCommand", "new_forget"]

_HELP = """Usage:
  /forget list [n]                 # 列出自己可删除的长期记忆（默认 20）
  /forget <id>                     # 删除一条长期记忆；id 支持唯一前缀
  /forget source <平台消息id>       # 删除由该消息派生的、自己可删除的记忆
  /forget resident normal          # 清空自己的普通常驻记忆
  /forget resident core --confirm  # 清空自己的核心常驻记忆
  /forget resident all --confirm   # 清空自己的全部常驻记忆

说明:
  群聊中普通成员只能删除来源成员为自己的长期记忆；
  群主、群管理员和超级管理员可以删除当前群 scope 的任意长期记忆。
  private/单聊 scope 中当前用户可删除整个 scope 的记忆。"""


async def _maybe_await(value: Any) -> Any:
    """服务实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


class ForgetCommand:
    """对应 Go ``forgetCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="forget",
            aliases=["forget_memory", "delete_memory"],
            usage=(
                "/forget list [n] | <id> | source <消息id> | "
                "resident normal|core|all [--confirm]"
            ),
            description="删除当前范围中自己可管理的长期记忆，或清空自己的常驻记忆。",
            min_role=RoleUser,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        args = (req.args or "").strip()
        if args in ("", "help", "?"):
            return Result(content=self.info().help)
        scope = self._deps.scope(ctx)
        fields = args.split()
        action = fields[0].lower()
        if action in ("list", "ls"):
            return await self._handle_list(ctx, scope, fields)
        if action == "source":
            return await self._handle_source(ctx, scope, fields)
        if action == "resident":
            return await self._handle_resident(ctx, scope, fields)
        return await self._delete_one(ctx, scope, fields[0])

    async def _handle_list(self, ctx: Any, scope: Any, fields: list[str]) -> Result:
        service = self._deps.angel_memory
        if service is None or not service.ready():
            return Result(content="angel memory 未启用或未配置。")
        limit = 20
        if len(fields) > 1:
            limit = memory_parse_limit(fields[1], 20, 100)
        filt = memory_visible_filter(ctx, scope)
        memories = await _maybe_await(
            service.list(scope.platform, scope.platform_scope_id, filt, limit)
        )
        if not memories:
            return Result(content="当前没有你可删除的长期记忆。")
        lines = [f"可管理的长期记忆：{len(memories)} 条"]
        for index, memory in enumerate(memories):
            lines.append(memory_entry_line(index + 1, memory))
        lines.append("删除请用：/forget <id>")
        return Result(content="\n".join(lines))

    async def _handle_source(self, ctx: Any, scope: Any, fields: list[str]) -> Result:
        service = self._deps.angel_memory
        if service is None or not service.ready():
            return Result(content="angel memory 未启用或未配置。")
        if len(fields) < 2:
            return Result(content="用法：/forget source <平台消息id>")
        message_id = (fields[1] or "").strip()
        if message_id == "":
            return Result(content="用法：/forget source <平台消息id>")
        filt = memory_visible_filter(ctx, scope)
        filt.message_id = message_id
        count = await _maybe_await(
            service.delete_by_source(scope.platform, scope.platform_scope_id, filt)
        )
        audit_command(
            self._deps,
            "angel_memory_forget_source",
            "platform",
            scope.platform,
            "scope",
            scope.platform_scope_id,
            "source_message_id",
            message_id,
            "count",
            count,
        )
        return Result(content=f"已删除 {count} 条由消息 {message_id} 派生的记忆。")

    async def _delete_one(self, ctx: Any, scope: Any, prefix: str) -> Result:
        service = self._deps.angel_memory
        if service is None or not service.ready():
            return Result(content="angel memory 未启用或未配置。")
        filt = memory_visible_filter(ctx, scope)
        memory = resolve_scoped_memory(
            ctx, service, scope.platform, scope.platform_scope_id, prefix, filt
        )
        if not memory_can_delete(ctx, scope, memory):
            return Result(content="你没有权限删除这条记忆。")
        await _maybe_await(
            service.delete(scope.platform, scope.platform_scope_id, memory.id)
        )
        audit_command(
            self._deps,
            "angel_memory_forget",
            "platform",
            scope.platform,
            "scope",
            scope.platform_scope_id,
            "memory_id",
            memory.id,
            "source_actor_id",
            memory.source_actor_id,
            "source_message_id",
            memory.source_message_id,
        )
        return Result(content=f"已删除记忆 {short_memory_id(memory.id)}。")

    async def _handle_resident(
        self, ctx: Any, scope: Any, fields: list[str]
    ) -> Result:
        store = self._deps.resident_memory
        if store is None:
            return Result(content="常驻记忆未启用或未配置。")
        action = "normal"
        if len(fields) > 1:
            action = (fields[1] or "").strip().lower()
        confirmed = False
        for field in fields[2:]:
            if (field or "").strip().lower() in ("--confirm", "-y", "--yes", "confirm"):
                confirmed = True
        actor, ok = actor_from_context(ctx)
        if not ok:
            raise ValueError("无法确定当前用户身份")
        resident_scope = actor_scope(actor)
        if (resident_scope.platform or "").strip() == "":
            resident_scope.platform = scope.platform
        if (resident_scope.actor_id or "").strip() == "":
            resident_scope.actor_id = scope.actor_id
        if (
            (resident_scope.platform or "").strip() == ""
            or (resident_scope.actor_id or "").strip() == ""
        ):
            return Result(content="当前上下文没有足够的用户信息，无法清空常驻记忆。")
        requires_confirm = action in ("core", "all")
        if requires_confirm and not confirmed:
            return Result(
                content=(
                    f"清空 {action} 常驻记忆需要确认："
                    f"/forget resident {action} --confirm"
                )
            )
        if action == "normal":
            await _maybe_await(store.delete_normal(resident_scope))
        elif action == "core":
            await _maybe_await(store.write_core(resident_scope, ""))
        elif action in ("all", "clear"):
            await _maybe_await(store.delete_normal(resident_scope))
            await _maybe_await(store.write_core(resident_scope, ""))
        else:
            return Result(content="用法：/forget resident normal|core|all [--confirm]")
        audit_command(
            self._deps,
            "resident_memory_forget",
            "platform",
            resident_scope.platform,
            "actor_id",
            resident_scope.actor_id,
            "section",
            action,
        )
        if action == "normal":
            return Result(content="已清空你的普通常驻记忆。")
        return Result(content=f"已清空你的 {action} 常驻记忆。")


def new_forget(deps: Deps) -> Any:
    """对应 Go ``forgetCommand{deps: deps}``。"""
    return ForgetCommand(deps)
