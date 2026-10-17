"""Session 生命周期命令：``/archive`` 系列、``/rename``、``/delete``、``/clean``。

对应 Go：``internal/agent/commands/session_lifecycle.go``（424 行）。

Go 的 4 个常量（``resumeHistoryTurns`` / ``defaultSessionListPageSize`` /
``messageListPageSize`` / ``historyPreviewRunes``）定义在本文件；本仓把它们放到
:mod:`atri_x.agent.commands.session_format` 并在此再导出，避免与读取方重复定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

from atri_x.command import (
    Completion,
    Info,
    Request,
    Result,
    SessionEffectMutate,
)
from atri_x.errors import NotFoundError
from atri_x.logging import quote_go
from atri_x.security import RoleUser
from atri_x.storage import now as storage_now
from atri_x.agent.commands.completion import (
    complete_confirm_flag,
    complete_session_ids,
    current_completion_token,
    is_first_arg,
)
from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.session_format import (
    DEFAULT_SESSION_LIST_PAGE_SIZE,
    HISTORY_PREVIEW_RUNES,
    MESSAGE_LIST_PAGE_SIZE,
    RESUME_HISTORY_TURNS,
    empty_title,
    session_page_size,
)

__all__ = [
    "DEFAULT_SESSION_LIST_PAGE_SIZE",
    "HISTORY_PREVIEW_RUNES",
    "MESSAGE_LIST_PAGE_SIZE",
    "RESUME_HISTORY_TURNS",
    "SessionTargetCommand",
    "audit_command",
    "cleanup_retention_days",
    "confirm_command_message",
    "empty_title",
    "forget_session_memories",
    "new_archive",
    "new_clean",
    "new_delete",
    "new_pin",
    "new_rename",
    "new_unarchive",
    "new_unpin",
    "parse_rename_args",
    "parse_resume_page_arg",
    "parse_sessions_args",
    "parse_target_confirm",
    "resolve_rename_target",
    "resolve_session_target",
]

# 清理过期会话时的默认保留天数（``SessionCommandState`` 未配置时使用）。
_DEFAULT_RETENTION_DAYS = 30

_TargetHandler = Callable[[Any, Deps, Request], Awaitable[Result | None]]


def audit_command(deps: Deps, event: str, *attrs: Any) -> None:
    """写一条审计事件（未配置审计时静默）。对应 Go ``auditCommand``。"""
    if deps.audit is None:
        return
    deps.audit(event, *attrs)


def cleanup_retention_days(deps: Deps) -> int:
    """``/clean`` 的保留天数。对应 Go ``cleanupRetentionDays``。"""
    days = 0
    if deps.session_state is not None:
        _, days = deps.session_state.config()
    if days <= 0:
        return _DEFAULT_RETENTION_DAYS
    return days


def empty_title_or_untitled(title: str) -> str:
    """同 :func:`atri_x.agent.commands.session_format.empty_title`（保留别名便于检索）。"""
    return empty_title(title)


def parse_target_confirm(args: str) -> tuple[str, bool]:
    """拆出 ``--confirm`` 与去掉它之后的 target。对应 Go ``parseTargetConfirm``。"""
    fields = (args or "").split()
    kept: list[str] = []
    confirmed = False
    for field in fields:
        if field == "--confirm":
            confirmed = True
            continue
        kept.append(field)
    return " ".join(kept), confirmed


def parse_sessions_args(args: str) -> tuple[int, str]:
    """``/sessions [page] [keyword]`` 的参数解析。对应 Go ``parseSessionsArgs``。"""
    fields = (args or "").split()
    if not fields:
        return 1, ""
    try:
        page = int(fields[0])
    except ValueError:
        return 1, " ".join(fields)
    if page < 1:
        raise ValueError("page must be a positive number")
    return page, " ".join(fields[1:])


def parse_resume_page_arg(prefix: str, args: str) -> int:
    """解析 ``/resume --page <n>``。对应 Go ``parseResumePageArg``。"""
    fields = (args or "").split()
    if len(fields) != 2 or fields[0] != "--page":
        raise ValueError(f"usage: {prefix}resume --page <page>")
    try:
        page = int(fields[1])
    except ValueError:
        raise ValueError("page must be a positive number") from None
    if page < 1:
        raise ValueError("page must be a positive number")
    return page


def resolve_session_target(
    ctx: Any, deps: Deps, prefix: str, target: str
) -> str:
    """把 ``编号 | session_id | 空`` 解析成 session id。对应 Go ``resolveSessionTarget``。"""
    target = (target or "").strip()
    if target == "":
        current = deps.sessions.current(deps.scope(ctx))
        return current.id
    try:
        index = int(target)
    except ValueError:
        return target
    ids = deps.session_state.get(deps.scope(ctx))
    if index < 1 or index > len(ids):
        raise ValueError(
            f"session index {index} out of range; run {prefix}sessions or "
            f"{prefix}archives first"
        )
    return ids[index - 1]


def resolve_rename_target(
    ctx: Any, deps: Deps, prefix: str, target: str
) -> tuple[str, bool]:
    """``/rename`` 的第一个参数解析。对应 Go ``resolveRenameTarget``。

    返回 ``(session_id, resolved)``：``resolved`` 为假表示第一个参数其实是标题的
    一部分，调用方应把整串当标题。
    """
    try:
        index = int(target)
    except ValueError:
        index = None
    if index is not None:
        ids = deps.session_state.get(deps.scope(ctx))
        if index < 1 or index > len(ids):
            raise ValueError(
                f"session index {index} out of range; run {prefix}sessions or "
                f"{prefix}archives first"
            )
        return ids[index - 1], True
    if deps.store is None:
        return "", False
    try:
        session = deps.store.sessions().get(target)
        return session.id, True
    except NotFoundError:
        pass

    sessions = deps.sessions.list(deps.scope(ctx), target, session_page_size(deps))
    matches = [item.id for item in sessions if item.title == target]
    if not matches:
        return "", False
    if len(matches) == 1:
        return matches[0], True
    raise ValueError(
        f"session title {quote_go(target)} matches multiple sessions; use "
        f"{prefix}sessions and rename by number or session id"
    )


def parse_rename_args(
    ctx: Any, deps: Deps, prefix: str, args: str
) -> tuple[str, str]:
    """``/rename [number|session_id|current_title] <title>``。对应 Go ``parseRenameArgs``。"""
    fields = (args or "").split()
    if not fields:
        raise ValueError(f"usage: {prefix}rename [number|session_id|current_title] <title>")
    if len(fields) == 1:
        return "", fields[0]
    session_id, resolved = resolve_rename_target(ctx, deps, prefix, fields[0])
    if resolved:
        return session_id, " ".join(fields[1:])
    return "", " ".join(fields)


def confirm_command_message(
    ctx: Any, deps: Deps, prefix: str, name: str, target: str
) -> str:
    """破坏性命令的确认提示。对应 Go ``confirmCommandMessage``。"""
    session_id = resolve_session_target(ctx, deps, prefix, target)
    session = deps.store.sessions().get(session_id)
    return (
        f"{name} will modify session:\n"
        f"  title: {empty_title(session.title)}\n"
        f"  id: {session.id}\n"
        f"Run {prefix}{name} {session.id} --confirm to continue."
    )


def forget_session_memories(deps: Deps, ctx: Any, scope: Any, session_id: str) -> None:
    """删除结构化来源指向被删 Session 的长期记忆。对应 Go ``forgetSessionMemories``。

    没有来源列（结构化来源之前写入）的旧行**故意不动**：那些行没有记录 Session id，
    旧版的自由文本 ``source`` 不足以支撑删除。
    """
    from atri_x.angelmemory import SourceFilter

    service = deps.angel_memory
    if service is None or not service.ready():
        return
    try:
        count = service.delete_by_source(
            scope.platform,
            scope.platform_scope_id,
            SourceFilter(session_id=session_id),
        )
    except Exception as exc:  # noqa: BLE001 - Go 里记录审计后返回
        audit_command(
            deps,
            "angel_memory_delete_by_session_failed",
            "platform",
            scope.platform,
            "scope",
            scope.platform_scope_id,
            "session_id",
            session_id,
            "error",
            str(exc),
        )
        return
    if count > 0:
        audit_command(
            deps,
            "angel_memory_delete_by_session",
            "platform",
            scope.platform,
            "scope",
            scope.platform_scope_id,
            "session_id",
            session_id,
            "count",
            count,
        )


@dataclass
class SessionTargetCommand:
    """``/archive`` / ``/unarchive`` / ``/pin`` / ``/unpin`` / ``/delete`` 的公共形状。

    对应 Go ``sessionTargetCommand``。
    """

    deps: Deps
    name: str = ""
    usage: str = ""
    description: str = ""
    archived: bool = False
    confirm: bool = False
    handle_fn: _TargetHandler | None = None

    def info(self) -> Info:
        return Info(
            name=self.name,
            usage=self.usage,
            description=self.description,
            session_effect=SessionEffectMutate,
            min_role=RoleUser,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        return await self.handle_fn(ctx, self.deps, req)

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        fields = (req.args or "").split()
        if fields and fields[-1] == "--confirm":
            return []
        if self.confirm and fields and (
            not is_first_arg(req, token) or token.text.startswith("--")
        ):
            return complete_confirm_flag(req.args, token)
        if is_first_arg(req, token):
            return complete_session_ids(
                ctx, self.deps, token.text, self.archived, token.start, token.end
            )
        if self.confirm:
            return complete_confirm_flag(req.args, token)
        return []


def new_archive(deps: Deps) -> Any:
    """``/archive``。对应 Go ``NewArchive``。"""

    async def handle(ctx: Any, deps: Deps, req: Request) -> Result | None:
        target, confirmed = parse_target_confirm(req.args)
        if not confirmed:
            return Result(
                content=confirm_command_message(ctx, deps, req.prefix, "archive", target)
            )
        session_id = resolve_session_target(ctx, deps, req.prefix, target)
        session = deps.sessions.archive(deps.scope(ctx), session_id)
        return Result(
            content=f"archived session:\n  id: {session.id}\n  title: {session.title}"
        )

    return SessionTargetCommand(
        deps=deps,
        name="archive",
        usage="/archive [number|session_id] --confirm",
        description="Archive a session. Defaults to current session.",
        confirm=True,
        handle_fn=handle,
    )


def new_unarchive(deps: Deps) -> Any:
    """``/unarchive``。对应 Go ``NewUnarchive``。"""

    async def handle(ctx: Any, deps: Deps, req: Request) -> Result | None:
        session_id = resolve_session_target(
            ctx, deps, req.prefix, (req.args or "").strip()
        )
        session = deps.sessions.unarchive(deps.scope(ctx), session_id)
        return Result(
            content=f"unarchived session:\n  id: {session.id}\n  title: {session.title}"
        )

    return SessionTargetCommand(
        deps=deps,
        name="unarchive",
        usage="/unarchive [number|session_id]",
        description="Unarchive a session. Defaults to current session.",
        archived=True,
        handle_fn=handle,
    )


def new_pin(deps: Deps) -> Any:
    """``/pin``。对应 Go ``NewPin``。"""

    async def handle(ctx: Any, deps: Deps, req: Request) -> Result | None:
        session_id = resolve_session_target(
            ctx, deps, req.prefix, (req.args or "").strip()
        )
        session = deps.sessions.pin(deps.scope(ctx), session_id)
        return Result(
            content=f"pinned session:\n  id: {session.id}\n  title: {session.title}"
        )

    return SessionTargetCommand(
        deps=deps,
        name="pin",
        usage="/pin [number|session_id]",
        description="Pin a session. Defaults to current session.",
        handle_fn=handle,
    )


def new_unpin(deps: Deps) -> Any:
    """``/unpin``。对应 Go ``NewUnpin``。"""

    async def handle(ctx: Any, deps: Deps, req: Request) -> Result | None:
        session_id = resolve_session_target(
            ctx, deps, req.prefix, (req.args or "").strip()
        )
        session = deps.sessions.unpin(deps.scope(ctx), session_id)
        return Result(
            content=f"unpinned session:\n  id: {session.id}\n  title: {session.title}"
        )

    return SessionTargetCommand(
        deps=deps,
        name="unpin",
        usage="/unpin [number|session_id]",
        description="Unpin a session. Defaults to current session.",
        handle_fn=handle,
    )


class _RenameCommand:
    """对应 Go ``renameCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="rename",
            usage="/rename [number|session_id|current_title] <title>",
            description="Rename current or selected session.",
            session_effect=SessionEffectMutate,
            min_role=RoleUser,
            help=(
                "Usage:\n"
                "  /rename <title>\n"
                "  /rename <number|session_id|current_title> <title>\n"
                "\n"
                "If current_title matches more than one visible session, use "
                "/sessions and rename by number or session id."
            ),
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        session_id, title = parse_rename_args(
            ctx, self._deps, req.prefix, req.args
        )
        session = self._deps.sessions.rename(
            self._deps.scope(ctx), session_id, title
        )
        return Result(
            content=f"renamed session:\n  id: {session.id}\n  title: {session.title}"
        )

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        if not is_first_arg(req, token):
            return []
        return complete_session_ids(
            ctx, self._deps, token.text, False, token.start, token.end
        )


def new_rename(deps: Deps) -> Any:
    """对应 Go ``NewRename``。"""
    return _RenameCommand(deps)


def new_delete(deps: Deps) -> Any:
    """``/delete``。对应 Go ``NewDelete``。"""

    async def handle(ctx: Any, deps: Deps, req: Request) -> Result | None:
        target, confirmed = parse_target_confirm(req.args)
        if (target or "").strip() == "":
            raise ValueError(f"usage: {req.prefix}delete <number|session_id> --confirm")
        if not confirmed:
            return Result(
                content=confirm_command_message(ctx, deps, req.prefix, "delete", target)
            )
        session_id = resolve_session_target(ctx, deps, req.prefix, target)
        scope = deps.scope(ctx)
        deps.sessions.delete(scope, session_id)
        forget_session_memories(deps, ctx, scope, session_id)
        return Result(content=f"deleted session:\n  id: {session_id}")

    return SessionTargetCommand(
        deps=deps,
        name="delete",
        usage="/delete <number|session_id> --confirm",
        description="Delete a session permanently.",
        confirm=True,
        handle_fn=handle,
    )


class _CleanCommand:
    """对应 Go ``cleanCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="clean",
            usage="/clean --confirm",
            description="Delete expired non-archived and non-pinned sessions.",
            session_effect=SessionEffectMutate,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        deps = self._deps
        _, confirmed = parse_target_confirm(req.args)
        if not confirmed:
            return Result(
                content=(
                    "clean will permanently delete expired sessions that are not "
                    f"archived or pinned. Run {req.prefix}clean --confirm to continue."
                )
            )
        cutoff = storage_now() + timedelta(days=-cleanup_retention_days(deps))
        deleted = deps.sessions.cleanup_expired(cutoff)
        return Result(content=f"cleaned expired sessions: {deleted}")

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        if not is_first_arg(req, token):
            return []
        return complete_confirm_flag(req.args, token)


def new_clean(deps: Deps) -> Any:
    """对应 Go ``NewClean``。"""
    return _CleanCommand(deps)
