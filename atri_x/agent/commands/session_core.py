"""``/new``、``/status``、``/work``、``/chat``。

对应 Go：``internal/agent/commands/session_core.go``（85 行）+
``session_mode.go``（63 行）。两个文件都是"当前 Session 的开关与状态"，
按 PORTING.md §2「Go 侧小文件按职责合并」合成一个模块。

``formatEmptyStatus`` 在 Go 里落在 ``session_format.go``；本仓放在这里，因为它只被
``/status`` 消费，而 ``session_format`` 需要保持"不依赖 request 渲染"以免与
:mod:`atri_x.agent.commands.request` 形成 import 环。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import (
    Continuation,
    Info,
    Request,
    Result,
    SessionEffectSwitchCurrent,
    new_func,
)
from atri_x.errors import ChatModeRequiresEmptySessionError, NotFoundError
from atri_x.security import RoleUser
from atri_x.session import ActivateModeRequest
from atri_x.storage import SessionModeChat, SessionModeWork
from atri_x.turn import tools_string
from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.request import format_active_requests
from atri_x.agent.commands.session_format import (
    empty_todo,
    format_time,
    indent_status_block,
)

__all__ = [
    "format_empty_status",
    "format_tool_usage",
    "new_chat",
    "new_new",
    "new_status",
    "new_work",
]

_STATUS_TEMPLATE = """session status:
  id: %s
  title: %s
  mode: %s
  current model: %s/%s
  message count: %d
  archived: %s
  pinned: %s
  created at: %s
  updated at: %s
  last ask: %s
  last answer: %s
%s  turn phase: %s
  pending input: %s
  tool usage: %s
%s"""


def new_new(deps: Deps) -> Any:
    """``/new``：重置当前 Session。对应 Go ``NewNew``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        deps.sessions.reset_current(deps.scope(ctx))
        return Result(
            content=(
                "new session ready; it will be created when you send the first message"
            )
        )

    return new_func(
        Info(
            name="new",
            usage="/new",
            description=(
                "Reset the current session; the next message creates a new one."
            ),
            session_effect=SessionEffectSwitchCurrent,
            min_role=RoleUser,
        ),
        handle,
    )


def new_status(deps: Deps) -> Any:
    """``/status``：当前 Session 状态。对应 Go ``NewStatus``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        try:
            status = deps.sessions.status(deps.scope(ctx))
        except NotFoundError:
            return Result(content=format_empty_status(ctx, deps))
        session = status.session
        archived = "no"
        if session.archived_at is not None:
            archived = format_time(session.archived_at)
        pinned = "no"
        if session.pinned_at is not None:
            pinned = format_time(session.pinned_at)
        active = format_active_requests(
            ctx, deps, deps.requests.list_by_session(session.id)
        )
        turn_snapshot = deps.turns.snapshot(session.id)
        pending_input = "none"
        if turn_snapshot.pending_count > 0:
            pending_input = f"{turn_snapshot.pending_count} message(s)"
        current_model = deps.models.current_model_for_mode(session.mode)
        content = _STATUS_TEMPLATE % (
            session.id,
            session.title,
            session.mode,
            current_model.provider,
            current_model.model,
            status.message_count,
            archived,
            pinned,
            format_time(session.created_at),
            format_time(session.updated_at),
            empty_todo(status.last_user_preview),
            empty_todo(status.last_answer_preview),
            indent_status_block(active),
            turn_snapshot.phase,
            pending_input,
            format_tool_usage(ctx, deps, session.id, turn_snapshot.tools),
            deps.context_status.context_status(ctx, session),
        )
        return Result(content=content)

    return new_func(
        Info(
            name="status",
            usage="/status",
            description="Show current session status.",
            min_role=RoleUser,
        ),
        handle,
    )


def format_tool_usage(
    ctx: Any, deps: Deps, session_id: str, live: dict[str, int] | None
) -> str:
    """合并存储层与当前 turn 的工具计数。对应 Go ``formatToolUsage``。"""
    usage: dict[str, int] = {}
    store = deps.store
    if store is not None and store.tool_calls() is not None:
        try:
            summaries = store.tool_calls().usage_by_session(session_id)
        except Exception:  # noqa: BLE001 - Go 里 err != nil 时跳过存储统计
            summaries = []
        for summary in summaries:
            if summary.tool_name != "" and summary.count > 0:
                usage[summary.tool_name] = usage.get(summary.tool_name, 0) + summary.count
    for name, count in (live or {}).items():
        if count > 0:
            usage[name] = usage.get(name, 0) + count
    out = tools_string(usage)
    if out != "":
        return out
    return "none"


def format_empty_status(ctx: Any, deps: Deps) -> str:
    """没有当前会话时的 ``/status``。对应 Go ``formatEmptyStatus``。"""
    scope = deps.scope(ctx)
    mode_model = deps.models.current_mode_model()
    compact_model = deps.models.current_compact_model()
    active = format_active_requests(ctx, deps, [])
    text = """session status:
  current session: none
  default mode: %s
  current mode model: %s/%s
  compact model: %s/%s
  platform: %s/%s
%s  turn phase: %s
  pending input: %s
  tools: available in work mode
""" % (
        deps.sessions.default_mode(),
        mode_model.provider,
        mode_model.model,
        compact_model.provider,
        compact_model.model,
        scope.platform,
        scope.platform_scope_id,
        indent_status_block(active),
        "idle",
        "none",
    )
    return text.rstrip("\n")


def new_work(deps: Deps) -> Any:
    """``/work``：切到 work 模式。对应 Go ``NewWork``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        activated = deps.sessions.activate_mode(
            deps.scope(ctx),
            ActivateModeRequest(mode=SessionModeWork, new_session_title="New work session"),
        )
        session = activated.session
        content = (
            f"work mode active:\n  id: {session.id}\n  mode: {session.mode}\n  tools: enabled"
        )
        if activated.already_active:
            content = f"already in work mode:\n  id: {session.id}\n  tools: enabled"
        result = Result(content=content)
        if req.args != "":
            result.continuation = Continuation(text=req.args, session_id=session.id)
        return result

    return new_func(
        Info(
            name="work",
            usage="/work [message]",
            description="Switch current session to work mode.",
            session_effect=SessionEffectSwitchCurrent,
            min_role=RoleUser,
        ),
        handle,
    )


def new_chat(deps: Deps) -> Any:
    """``/chat``：切到（或新建）chat 模式会话。对应 Go ``NewChat``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        try:
            activated = deps.sessions.activate_mode(
                deps.scope(ctx),
                ActivateModeRequest(
                    mode=SessionModeChat, new_session_title="New chat session"
                ),
            )
        except ChatModeRequiresEmptySessionError:
            return Result(
                content=(
                    "current work session has history; run "
                    f"{req.prefix}new then {req.prefix}chat to start a clean chat session"
                )
            )
        session = activated.session
        content = f"chat mode active:\n  id: {session.id}\n  mode: {session.mode}"
        if activated.already_active:
            content = f"already in chat mode:\n  id: {session.id}"
        result = Result(content=content)
        if req.args != "":
            result.continuation = Continuation(text=req.args, session_id=session.id)
        return result

    return new_func(
        Info(
            name="chat",
            usage="/chat [message]",
            description=(
                "Switch an empty session to chat mode, or create a chat session."
            ),
            session_effect=SessionEffectSwitchCurrent,
            min_role=RoleUser,
        ),
        handle,
    )
