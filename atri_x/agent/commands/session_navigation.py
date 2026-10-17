"""``/sessions``、``/archives``、``/resume``、``/messages``、``/fork``。

对应 Go：``internal/agent/commands/session_navigation.go``（174 行）。

Go 的 ``resumeCommand.Complete`` 手写了一遍 token 定位；本模块复用
:func:`atri_x.agent.commands.completion.current_completion_token`，但保留 Go
"对 token 做 ``TrimSpace`` 后再判断 ``--page``"的细节。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import (
    Completion,
    Info,
    Request,
    Result,
    SessionEffectSwitchCurrent,
    new_func,
)
from atri_x.security import RoleUser
from atri_x.agent.commands.completion import current_completion_token
from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.session_format import (
    MESSAGE_LIST_PAGE_SIZE,
    assistant_messages,
    current_session_id,
    empty_title,
    format_fork_result,
    format_message_page,
    format_resumable_sessions_page,
    format_resume_result,
    format_sessions_page,
    list_resumable_page,
    list_session_page,
    session_page_size,
)
from atri_x.agent.commands.session_lifecycle import (
    audit_command,
    parse_resume_page_arg,
    parse_sessions_args,
)

__all__ = [
    "ResumeCommand",
    "new_archives",
    "new_fork",
    "new_messages",
    "new_resume",
    "new_sessions",
    "resume_page",
]


def new_sessions(deps: Deps) -> Any:
    """``/sessions [keyword]``。对应 Go ``NewSessions``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        page, query = parse_sessions_args(req.args)
        sessions, has_next = list_session_page(
            ctx, deps, query, page, session_page_size(deps), False
        )
        deps.session_state.set(deps.scope(ctx), sessions)
        if not sessions:
            return Result(content="no sessions found")
        return Result(
            content=format_sessions_page(
                sessions,
                current_session_id(ctx, deps),
                page,
                query,
                has_next,
                req.prefix + "sessions",
            )
        )

    return new_func(
        Info(
            name="sessions",
            usage="/sessions [keyword]",
            description="List or search sessions.",
            min_role=RoleUser,
        ),
        handle,
    )


class ResumeCommand:
    """``/resume``。对应 Go ``resumeCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="resume",
            usage="/resume [number|session_id]",
            description="Resume a previous session.",
            session_effect=SessionEffectSwitchCurrent,
            min_role=RoleUser,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        deps = self._deps
        arg = (req.args or "").strip()
        if arg.startswith("--page"):
            page = parse_resume_page_arg(req.prefix, arg)
            return await resume_page(ctx, deps, req.prefix, page)
        if arg == "":
            return await resume_page(ctx, deps, req.prefix, 1)
        session_id = arg
        try:
            index = int(arg)
        except ValueError:
            index = None
        if index is not None:
            if index < 1:
                raise ValueError("session index must be a positive number")
            sessions = deps.sessions.list_resumable_page(
                deps.scope(ctx), 1, index - 1
            )
            if not sessions:
                raise ValueError(f"session index {index} out of range")
            session_id = sessions[0].id
        session = deps.sessions.resume(deps.scope(ctx), session_id)
        audit_command(
            deps, "session_resume", "session_id", session.id, "title", session.title
        )
        return Result(content=await format_resume_result(ctx, deps, session))

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        query = token.text.strip()
        if query == "--page" or query.startswith("--"):
            return []
        sessions, _ = list_resumable_page(
            ctx, self._deps, 1, session_page_size(self._deps)
        )
        out: list[Completion] = []
        for session in sessions:
            if query == "" or session.id.startswith(query):
                out.append(
                    Completion(
                        text=session.id,
                        label=session.id,
                        description=empty_title(session.title),
                        kind="session_id",
                        replace_start=token.start,
                        replace_end=token.end,
                    )
                )
        return out


def new_resume(deps: Deps) -> Any:
    """对应 Go ``NewResume``。"""
    return ResumeCommand(deps)


async def resume_page(ctx: Any, deps: Deps, prefix: str, page: int) -> Result:
    """``/resume --page <n>`` 的分页列表。对应 Go ``resumePage``。"""
    page_size = session_page_size(deps)
    sessions, has_next = list_resumable_page(ctx, deps, page, page_size)
    if not sessions:
        return Result(content="no sessions found")
    return Result(
        content=format_resumable_sessions_page(
            sessions, page, page_size, has_next, prefix
        )
    )


def new_archives(deps: Deps) -> Any:
    """``/archives [page] [keyword]``。对应 Go ``NewArchives``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        page, query = parse_sessions_args(req.args)
        sessions, has_next = list_session_page(
            ctx, deps, query, page, session_page_size(deps), True
        )
        deps.session_state.set(deps.scope(ctx), sessions)
        if not sessions:
            return Result(content="no archived sessions found")
        return Result(
            content=format_sessions_page(
                sessions,
                current_session_id(ctx, deps),
                page,
                query,
                has_next,
                req.prefix + "archives",
            )
        )

    return new_func(
        Info(
            name="archives",
            usage="/archives [page] [keyword]",
            description="List archived sessions.",
            min_role=RoleUser,
        ),
        handle,
    )


def new_messages(deps: Deps) -> Any:
    """``/messages [page]``。对应 Go ``NewMessages``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        from atri_x.contextmgr import Loader

        page = 1
        arg = (req.args or "").strip()
        if arg != "":
            try:
                parsed = int(arg)
            except ValueError:
                raise ValueError("page must be a positive number") from None
            if parsed < 1:
                raise ValueError("page must be a positive number")
            page = parsed
        session = deps.sessions.current(deps.scope(ctx))
        loaded = await Loader(store=deps.store).load(ctx, session.id)
        assistants = assistant_messages(loaded.messages)
        if not assistants:
            return Result(content="no assistant messages found")
        return Result(content=format_message_page(assistants, page, MESSAGE_LIST_PAGE_SIZE))

    return new_func(
        Info(
            name="messages",
            usage="/messages [page]",
            description="List assistant message IDs in current session for fork.",
            min_role=RoleUser,
        ),
        handle,
    )


def new_fork(deps: Deps) -> Any:
    """``/fork <message_id>``。对应 Go ``NewFork``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        message_id = (req.args or "").strip()
        if message_id == "":
            raise ValueError(f"usage: {req.prefix}fork <message_id>")
        session = deps.sessions.fork(deps.scope(ctx), message_id)
        audit_command(
            deps,
            "session_fork",
            "session_id",
            session.id,
            "parent_session_id",
            session.parent_session_id,
            "from_message_id",
            session.fork_from_message_id,
        )
        return Result(content=await format_fork_result(ctx, deps, session))

    return new_func(
        Info(
            name="fork",
            usage="/fork <message_id>",
            description="Fork current conversation from an assistant message.",
            session_effect=SessionEffectSwitchCurrent,
            min_role=RoleUser,
        ),
        handle,
    )
