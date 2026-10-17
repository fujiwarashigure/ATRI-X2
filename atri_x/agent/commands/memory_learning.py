"""``/memory``（别名 ``/angel_memory``）与 ``/learning``（别名 ``/selflearning``）。

对应 Go：``internal/agent/commands/memory_learning.go``（322 行）+
``MemoryLearningModule``。

``/memory`` 只能由超管使用；``/learning`` 在超管之外额外放行群主/群管理员，
且还需要服务端的群级授权（``GroupAdminNeedsGrant``）。
"""

from __future__ import annotations

from typing import Any

from atri_x.angelmemory import LegacySourceBackfill, SourceFilter
from atri_x.command import Info, Request, Result
from atri_x.config import (
    LEARNING_MODERATION_DECIDE,
    LEARNING_MODERATION_DELETE,
    LEARNING_MODERATION_EXPORT,
    LEARNING_MODERATION_MINE,
    LEARNING_MODERATION_VIEW,
)
from atri_x.security import RoleSuperadmin, actor_from_context
from atri_x.agent.commands.deps import Deps, Registrar
from atri_x.agent.commands.memory_common import (
    audit_command,
    memory_entry_detail,
    memory_entry_line,
    memory_parse_limit,
    resolve_scoped_memory,
    short_memory_id,
)
from atri_x.agent.commands.memory_forget import ForgetCommand
from atri_x.agent.commands.tool import has_flag

__all__ = [
    "LearningCommand",
    "MemoryCommand",
    "MemoryLearningModule",
    "format_legacy_backfill_preview",
    "learning_action_permission",
    "new_learning",
    "new_memory",
    "reviewer_name",
]

_MEMORY_HELP = """Usage:
  /memory status                     # 查看当前范围条数
  /memory list [n]                   # 列出记忆（默认 20，上限 100）
  /memory recall [关键词]             # 按关键词检索
  /memory show <id>                  # 查看一条记忆的来源与内容
  /memory delete <id>                # 删除一条记忆
  /memory source <平台消息id>         # 删除由该消息派生的记忆
  /memory backfill                   # 预检旧记忆来源回填（全库统计）
  /memory backfill --confirm         # 为旧记忆回填可确定的 source_kind

说明:
  id 支持唯一前缀；删除只作用于当前平台/会话 scope。
  旧记忆没有记录来源成员 / 消息 ID / Session ID，无法回填，也不会被按来源删除命中。"""

_LEARNING_HELP = """Usage:
  /learning status
  /learning mine
  /learning review [pending|approved|rejected]
  /learning approve <id> [含义]
  /learning reject <id>
  /learning undo <id>
  /learning history <id>"""


async def _maybe_await(value: Any) -> Any:
    """服务实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


class MemoryCommand:
    """对应 Go ``memoryCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="memory",
            aliases=["angel_memory"],
            usage=(
                "/memory status | list [n] | recall [关键词] | show <id> | "
                "delete <id> | source <消息id> | backfill [--confirm]"
            ),
            description="查看、检索或删除当前平台/会话的 clean-room 长期记忆。",
            min_role=RoleSuperadmin,
            help=_MEMORY_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        service = self._deps.angel_memory
        if service is None or not service.ready():
            return Result(content="angel memory 未启用或未配置。")
        scope = self._deps.scope(ctx)
        args = (req.args or "").split()
        action = "status"
        if args:
            action = (args[0] or "").strip().lower()
        if action == "status":
            count = await _maybe_await(
                service.count(scope.platform, scope.platform_scope_id)
            )
            return Result(content=f"当前范围长期记忆：{count} 条。")
        if action in ("list", "ls"):
            limit = 20
            if len(args) > 1:
                limit = memory_parse_limit(args[1], 20, 100)
            memories = await _maybe_await(
                service.list(
                    scope.platform, scope.platform_scope_id, SourceFilter(), limit
                )
            )
            if not memories:
                return Result(content="当前范围没有长期记忆。")
            lines = [f"长期记忆 {len(memories)} 条（最多显示 {limit} 条）："]
            for index, memory in enumerate(memories):
                lines.append(memory_entry_line(index + 1, memory))
            return Result(content="\n".join(lines))
        if action == "recall":
            query = (req.args or "").strip()
            if args:
                query = query[len(args[0]) :].strip()
            memories = await _maybe_await(
                service.recall(scope.platform, scope.platform_scope_id, query, 10)
            )
            if not memories:
                return Result(content="没有找到符合条件的长期记忆。")
            lines = [f"记忆 {len(memories)} 条："]
            for index, memory in enumerate(memories):
                lines.append(memory_entry_line(index + 1, memory))
            return Result(content="\n".join(lines))
        if action in ("show", "detail"):
            if len(args) < 2:
                return Result(content="用法：/memory show <id>")
            memory = resolve_scoped_memory(
                ctx,
                service,
                scope.platform,
                scope.platform_scope_id,
                args[1],
                SourceFilter(),
            )
            return Result(content=memory_entry_detail(memory))
        if action in ("delete", "forget", "rm"):
            if len(args) < 2:
                return Result(content="用法：/memory delete <id>")
            memory = resolve_scoped_memory(
                ctx,
                service,
                scope.platform,
                scope.platform_scope_id,
                args[1],
                SourceFilter(),
            )
            await _maybe_await(
                service.delete(scope.platform, scope.platform_scope_id, memory.id)
            )
            audit_command(
                self._deps,
                "angel_memory_delete",
                "platform",
                scope.platform,
                "scope",
                scope.platform_scope_id,
                "memory_id",
                memory.id,
                "source_message_id",
                memory.source_message_id,
            )
            return Result(content=f"已删除记忆 {short_memory_id(memory.id)}。")
        if action == "source":
            if len(args) < 2:
                return Result(content="用法：/memory source <平台消息id>")
            count = await _maybe_await(
                service.delete_by_source(
                    scope.platform,
                    scope.platform_scope_id,
                    SourceFilter(message_id=args[1]),
                )
            )
            audit_command(
                self._deps,
                "angel_memory_delete_source",
                "platform",
                scope.platform,
                "scope",
                scope.platform_scope_id,
                "source_message_id",
                args[1],
                "count",
                count,
            )
            return Result(content=f"已删除 {count} 条由消息 {args[1]} 派生的记忆。")
        if action == "backfill":
            stats = await _maybe_await(service.legacy_source_backfill_stats())
            if not has_flag(args[1:], "--confirm"):
                return Result(content=format_legacy_backfill_preview(stats))
            count = await _maybe_await(service.backfill_legacy_source_kind())
            audit_command(
                self._deps,
                "angel_memory_backfill_legacy_source",
                "updated",
                count,
                "total",
                stats.total,
                "linked",
                stats.linked,
            )
            return Result(
                content=(
                    f"已为 {count} 条旧记忆回填 source_kind"
                    f"（全库共 {stats.total} 条，其中 {stats.linked} 条已有结构化来源）。\n"
                    "旧记忆的来源成员、消息 ID 和 Session ID 无法回填，"
                    "仍按“不匹配、不误删”处理。"
                )
            )
        return Result(
            content="用法：/memory status|list|recall|show|delete|source|backfill"
        )


def format_legacy_backfill_preview(stats: LegacySourceBackfill) -> str:
    """旧记忆回填的预检报告。对应 Go ``formatLegacyBackfillPreview``。"""
    unlinked = stats.total - stats.linked
    if unlinked < 0:
        unlinked = 0
    lines = [
        "旧记忆来源回填预检（全库统计，尚未修改任何数据）：",
        f"记忆总数：{stats.total} 条",
        f"已有结构化来源：{stats.linked} 条",
        (
            f"可确定性回填 source_kind=tool：{stats.backfillable} 条"
            '（旧版只写自由文本 source="tool"，写入方唯一，可直接确定）'
        ),
        (
            f"无法回填的来源成员 / 消息 ID / Session ID：涉及 {unlinked} 条旧记忆"
            "（旧数据没有记录这些字段）"
        ),
        (
            "这部分按“不匹配、不误删”处理：不会按来源消息或 Session 删除旧记忆，"
            "也不会被 /forget source、撤回清理和 /delete Session 命中。"
        ),
    ]
    if stats.backfillable == 0:
        lines.append("当前没有需要回填的条目。")
    else:
        lines.append("执行回填：/memory backfill --confirm")
    return "\n".join(lines)


def new_memory(deps: Deps) -> Any:
    """对应 Go ``memoryCommand{deps: deps}``。"""
    return MemoryCommand(deps)


def learning_action_permission(action: str) -> str:
    """把 ``/learning`` 子命令映射到群级授权动作。对应 Go ``learningActionPermission``。"""
    key = (action or "").strip().lower()
    if key in ("status", "review", "history"):
        return LEARNING_MODERATION_VIEW
    if key in ("approve", "reject", "undo"):
        return LEARNING_MODERATION_DECIDE
    if key == "mine":
        return LEARNING_MODERATION_MINE
    if key == "delete":
        return LEARNING_MODERATION_DELETE
    if key == "export":
        return LEARNING_MODERATION_EXPORT
    return ""


def reviewer_name(ctx: Any) -> str:
    """审核人展示名。对应 Go ``reviewerName``。"""
    actor, ok = actor_from_context(ctx)
    if not ok:
        return ""
    for value in (actor.id, actor.platform_user_id, actor.nickname, actor.display_name):
        value = (value or "").strip()
        if value != "":
            return value
    return ""


class LearningCommand:
    """对应 Go ``learningCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="learning",
            aliases=["selflearning"],
            usage=(
                "/learning status | mine | review [status] | approve <id> [meaning] | "
                "reject <id> | undo <id> | history <id>"
            ),
            description="管理 clean-room 表达/黑话候选；只有 approved 的内容会注入上下文。",
            min_role=RoleSuperadmin,
            allow_group_admin=True,
            group_admin_needs_grant=True,
            help=_LEARNING_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        service = self._deps.self_learning
        if service is None or not service.ready():
            return Result(content="self learning 未启用或未配置。")
        if (
            self._deps.group_policy is not None
            and not self._deps.group_policy.group_learning_enabled(ctx)
        ):
            return Result(
                content=(
                    "本群已关闭学习观察；请让超级管理员使用 "
                    "/grouppolicy learning on 开启。"
                )
            )
        scope = self._deps.scope(ctx)
        args = (req.args or "").split()
        action = "status"
        if args:
            action = (args[0] or "").strip().lower()
        actor, _ = actor_from_context(ctx)
        if actor.role != RoleSuperadmin:
            required = learning_action_permission(action)
            if (
                required == ""
                or self._deps.group_policy is None
                or not self._deps.group_policy.authorize_learning_action(ctx, required)
            ):
                return Result(
                    content=(
                        "当前群管理员未获授权执行该学习操作；请让机器人超级管理员使用 "
                        "/grouppolicy learning-moderation-actions 授予具体权限。"
                    )
                )
        if action == "status":
            pending, approved = await _maybe_await(
                service.stats(scope.platform, scope.platform_scope_id)
            )
            return Result(
                content=f"self learning：待审 {pending} 条，已批准 {approved} 条。"
            )
        if action == "mine":
            stats = await _maybe_await(
                service.mine(scope.platform, scope.platform_scope_id, 3, 100)
            )
            summary = (
                f"候选挖掘完成：新增 {stats.created} 条，更新 {stats.updated} 条，"
                f"跳过 {stats.skipped} 条；扫描 {stats.scanned} 条 / "
                f"{stats.total_chars} 字。"
            )
            if stats.truncated:
                summary += " 已达到挖掘预算，结果可能不完整。"
            if stats.timed_out:
                summary += " 挖掘超时，结果可能不完整。"
            return Result(content=summary)
        if action == "review":
            status = "pending"
            if len(args) > 1:
                status = (args[1] or "").strip()
            candidates = await _maybe_await(
                service.review(scope.platform, scope.platform_scope_id, status, 100)
            )
            if not candidates:
                return Result(content="没有符合条件的候选。")
            lines = [f"候选 {len(candidates)} 条："]
            for index, candidate in enumerate(candidates):
                lines.append(
                    f"{index + 1}. [{candidate.status}] id={candidate.id} "
                    f"count={candidate.count} pattern={candidate.pattern}"
                )
            return Result(content="\n".join(lines))
        if action in ("approve", "reject"):
            if len(args) < 2:
                return Result(content="approve/reject 需要候选 ID。")
            status = "approved" if action == "approve" else "rejected"
            meaning = ""
            if len(args) > 2:
                meaning = " ".join(args[2:])
            await _maybe_await(
                service.decide(
                    scope.platform,
                    scope.platform_scope_id,
                    args[1],
                    status,
                    meaning,
                    reviewer_name(ctx),
                )
            )
            return Result(content=f"候选 {args[1]} 已标记为 {status}。")
        if action == "undo":
            if len(args) < 2:
                return Result(content="undo 需要候选 ID。")
            await _maybe_await(
                service.undo(
                    scope.platform,
                    scope.platform_scope_id,
                    args[1],
                    reviewer_name(ctx),
                )
            )
            return Result(content=f"候选 {args[1]} 已撤回为待审。")
        if action == "history":
            if len(args) < 2:
                return Result(content="history 需要候选 ID。")
            records = await _maybe_await(
                service.history(scope.platform, scope.platform_scope_id, args[1], 20)
            )
            if not records:
                return Result(content="没有审核记录。")
            lines = [f"审核历史 {len(records)} 条："]
            for index, record in enumerate(records):
                lines.append(
                    f"{index + 1}. {record.from_status} → {record.to_status} "
                    f"by {record.reviewer}"
                )
            return Result(content="\n".join(lines))
        return Result(
            content="用法：/learning status|mine|review|approve|reject|undo|history"
        )


def new_learning(deps: Deps) -> Any:
    """对应 Go ``learningCommand{deps: deps}``。"""
    return LearningCommand(deps)


class MemoryLearningModule:
    """对应 Go ``MemoryLearningModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        registrar.register(new_memory(deps))
        registrar.register(ForgetCommand(deps))
        registrar.register(new_learning(deps))
