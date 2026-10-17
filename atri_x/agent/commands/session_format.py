"""Session 列表/消息的查询与文本渲染。

对应 Go：``internal/agent/commands/session_format.go``（258 行）。

与 Go 的差异（并发/异常模型导致，非行为差异）：

- ``recent_conversation_messages`` / ``format_resume_result`` / ``format_fork_result`` /
  ``append_recent_messages`` 在 Go 里是同步函数，但内部走 ``contextmgr.Loader.Load``；
  Python 侧 ``Loader.load`` 是 ``async``，所以这几个函数也是 ``async``。
- Go 的分页字符串下标在 Python 里是码点下标（见 :mod:`atri_x.agent.commands.completion`）。

Go 把 ``resumeHistoryTurns`` / ``defaultSessionListPageSize`` / ``messageListPageSize``
/ ``historyPreviewRunes`` 四个常量写在 ``session_lifecycle.go``；本仓集中放在本模块
（列表/分页意义上的归属地），``session_lifecycle`` 反向 import，避免两处重复定义。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from atri_x.storage import (
    Message,
    RoleAssistant,
    RoleUser,
    Session,
    SessionSummary,
)
from atri_x.agent.commands.deps import Deps

__all__ = [
    "DEFAULT_SESSION_LIST_PAGE_SIZE",
    "HISTORY_PREVIEW_RUNES",
    "MESSAGE_LIST_PAGE_SIZE",
    "RESUME_HISTORY_TURNS",
    "append_recent_messages",
    "assistant_messages",
    "current_session_id",
    "empty_title",
    "empty_todo",
    "format_fork_result",
    "format_message_page",
    "format_resume_result",
    "format_resumable_sessions_page",
    "format_sessions",
    "format_sessions_page",
    "format_sessions_page_with_offset",
    "format_sessions_with_offset",
    "format_time",
    "indent_status_block",
    "list_resumable_page",
    "list_session_page",
    "message_preview",
    "next_page_command",
    "preview_content",
    "recent_conversation_messages",
    "session_markers",
    "session_page_size",
    "truncate_runes",
]

# resume_history_turns 是 /resume 与 /fork 输出里展示的历史轮数上限。
RESUME_HISTORY_TURNS = 3
DEFAULT_SESSION_LIST_PAGE_SIZE = 10
MESSAGE_LIST_PAGE_SIZE = 20
# history_preview_runes 限制单条历史预览的长度，免得一条长消息刷屏。
HISTORY_PREVIEW_RUNES = 200

# Go ``time.Time{}`` 的 ``Format("2006-01-02 15:04:05")`` 输出。
_ZERO_TIME_TEXT = "0001-01-01 00:00:00"


def format_time(value: datetime | None) -> str:
    """渲染一个时间戳；``None`` 等价 Go 的零值 ``time.Time``。对应 Go ``formatTime``。"""
    if value is None:
        return _ZERO_TIME_TEXT
    return value.strftime("%Y-%m-%d %H:%M:%S")


def empty_title(title: str) -> str:
    """空标题的展示占位。对应 Go ``emptyTitle``。"""
    if (title or "").strip() == "":
        return "(untitled)"
    return title


def empty_todo(text: str) -> str:
    """Go ``emptyTODO``：空串显示成 ``TODO``。"""
    if text == "":
        return "TODO"
    return text


def truncate_runes(text: str, max_runes: int) -> str:
    """按码点截断并加省略号。对应 Go ``truncateRunes``。"""
    if max_runes <= 0 or len(text) <= max_runes:
        return text
    return text[:max_runes] + "..."


def session_page_size(deps: Deps) -> int:
    """当前配置的会话列表页大小。对应 Go ``sessionPageSize``。"""
    page_size = 0
    if deps.session_state is not None:
        page_size, _ = deps.session_state.config()
    if page_size > 0:
        return page_size
    return DEFAULT_SESSION_LIST_PAGE_SIZE


def list_session_page(
    ctx: Any,
    deps: Deps,
    query: str,
    page: int,
    page_size: int,
    archived_only: bool,
) -> tuple[list[SessionSummary], bool]:
    """取一页会话。对应 Go ``listSessionPage``。

    Go 返回 ``(sessions, hasNext, error)``；Python 侧出错抛异常，所以是二元组。
    """
    if page < 1:
        raise ValueError("page must be a positive number")
    if page_size <= 0:
        page_size = DEFAULT_SESSION_LIST_PAGE_SIZE
    sessions = deps.sessions.list_page(
        deps.scope(ctx), query, page_size + 1, (page - 1) * page_size, archived_only
    )
    has_next = len(sessions) > page_size
    if has_next:
        sessions = sessions[:page_size]
    return sessions, has_next


def list_resumable_page(
    ctx: Any, deps: Deps, page: int, page_size: int
) -> tuple[list[SessionSummary], bool]:
    """取一页可恢复会话。对应 Go ``listResumablePage``。"""
    if page < 1:
        raise ValueError("page must be a positive number")
    if page_size <= 0:
        page_size = DEFAULT_SESSION_LIST_PAGE_SIZE
    sessions = deps.sessions.list_resumable_page(
        deps.scope(ctx), page_size + 1, (page - 1) * page_size
    )
    has_next = len(sessions) > page_size
    if has_next:
        sessions = sessions[:page_size]
    return sessions, has_next


def format_sessions_page(
    sessions: list[SessionSummary],
    current_id: str,
    page: int,
    query: str,
    has_next: bool,
    command_prefix: str,
) -> str:
    """对应 Go ``formatSessionsPage``。"""
    return format_sessions_page_with_offset(
        sessions, current_id, page, query, has_next, command_prefix, 0
    )


def format_resumable_sessions_page(
    sessions: list[SessionSummary],
    page: int,
    page_size: int,
    has_next: bool,
    prefix: str,
) -> str:
    """对应 Go ``formatResumableSessionsPage``。"""
    return format_sessions_page_with_offset(
        sessions,
        "",
        page,
        "",
        has_next,
        prefix + "resume --page",
        (page - 1) * page_size,
    )


def format_sessions_page_with_offset(
    sessions: list[SessionSummary],
    current_id: str,
    page: int,
    query: str,
    has_next: bool,
    command_prefix: str,
    number_offset: int,
) -> str:
    """对应 Go ``formatSessionsPageWithOffset``。"""
    content = format_sessions_with_offset(sessions, current_id, number_offset)
    out = [content]
    if content != "":
        out.append("\n")
    out.append(f"page: {page}\n")
    if page > 1:
        out.append(f"prev: {next_page_command(command_prefix, page - 1, query)}\n")
    if has_next:
        out.append(f"next: {next_page_command(command_prefix, page + 1, query)}\n")
    return "".join(out).rstrip("\n")


def next_page_command(command_prefix: str, page: int, query: str) -> str:
    """翻页提示行。对应 Go ``nextPageCommand``。"""
    if (query or "").strip() == "":
        return f"{command_prefix} {page}"
    return f"{command_prefix} {page} {query}"


def current_session_id(ctx: Any, deps: Deps) -> str:
    """当前 Session id；没有当前会话时返回空串。对应 Go ``currentSessionID``。"""
    try:
        current = deps.sessions.current(deps.scope(ctx))
    except Exception:  # noqa: BLE001 - Go 里 err != nil 返回 ""
        return ""
    if current is None:
        return ""
    return current.id


async def format_resume_result(ctx: Any, deps: Deps, session: Session) -> str:
    """``/resume`` 的成功回执。对应 Go ``formatResumeResult``。"""
    out = [f"resumed session:\n  id: {session.id}\n  title: {session.title}\n"]
    out.append(await append_recent_messages(ctx, deps, session.id))
    return "".join(out).rstrip("\n")


async def format_fork_result(ctx: Any, deps: Deps, session: Session) -> str:
    """``/fork`` 的成功回执。对应 Go ``formatForkResult``。"""
    out = [
        "forked session:\n"
        f"  id: {session.id}\n"
        f"  parent: {session.parent_session_id}\n"
        f"  from message: {session.fork_from_message_id}\n"
        f"  mode: {session.mode}\n"
    ]
    out.append(await append_recent_messages(ctx, deps, session.id))
    return "".join(out).rstrip("\n")


async def append_recent_messages(ctx: Any, deps: Deps, session_id: str) -> str:
    """追加"最近 N 轮"预览块。对应 Go ``appendRecentMessages``。"""
    history = await recent_conversation_messages(
        ctx, deps, session_id, RESUME_HISTORY_TURNS
    )
    if not history:
        return ""
    out = ["recent messages:\n"]
    for message in history:
        out.append(f"  {message.role}: {preview_content(message.content)}\n")
    return "".join(out)


async def recent_conversation_messages(
    ctx: Any, deps: Deps, session_id: str, turns: int
) -> list[Message]:
    """取最近 ``turns`` 轮 user/assistant 消息（按时间正序）。对应 Go ``recentConversationMessages``。"""
    from atri_x.contextmgr import Loader

    try:
        loaded = await Loader(store=deps.store).load(ctx, session_id)
    except Exception:  # noqa: BLE001 - Go 里 err != nil 返回 nil
        return []
    limit = turns * 2
    out: list[Message] = []
    for index in range(len(loaded.messages) - 1, -1, -1):
        if len(out) >= limit:
            break
        message = loaded.messages[index]
        if message.role in (RoleUser, RoleAssistant) and (message.content or "").strip() != "":
            out.append(message)
    out.reverse()
    return out


def preview_content(text: str) -> str:
    """把一条历史消息压成单行预览。对应 Go ``previewContent``。"""
    return truncate_runes(
        (text or "").replace("\n", " ").strip(), HISTORY_PREVIEW_RUNES
    )


def assistant_messages(messages: list[Message]) -> list[Message]:
    """挑出有正文的 assistant 消息。对应 Go ``assistantMessages``。"""
    return [
        message
        for message in messages
        if message.role == RoleAssistant and (message.content or "").strip() != ""
    ]


def format_message_page(messages: list[Message], page: int, page_size: int) -> str:
    """``/messages`` 的一页。对应 Go ``formatMessagePage``（页码越界时抛异常）。"""
    if page_size <= 0:
        page_size = MESSAGE_LIST_PAGE_SIZE
    total_pages = (len(messages) + page_size - 1) // page_size
    if page < 1 or page > total_pages:
        raise ValueError(
            f"page {page} out of range; available pages: 1-{total_pages}"
        )
    start = (page - 1) * page_size
    end = min(start + page_size, len(messages))
    out = [f"messages page {page}/{total_pages}:\n"]
    for message in messages[start:end]:
        out.append(f"  {message.id}: {message_preview(message.content)}\n")
    return "".join(out).rstrip("\n")


def message_preview(text: str) -> str:
    """一条消息的单行 40 字符预览。对应 Go ``messagePreview``。"""
    return truncate_runes(" ".join((text or "").split()), 40)


def format_sessions(
    sessions: list[SessionSummary], current_id: str
) -> str:
    """对应 Go ``formatSessions``。"""
    return format_sessions_with_offset(sessions, current_id, 0)


def format_sessions_with_offset(
    sessions: list[SessionSummary], current_id: str, number_offset: int
) -> str:
    """对应 Go ``formatSessionsWithOffset``。"""
    out = ["sessions:\n"]
    for index, summary in enumerate(sessions):
        title = summary.title or "(untitled)"
        out.append(
            f"  [{number_offset + index + 1}] {title}"
            f"{session_markers(summary, current_id)}\n"
            f"      id: {summary.id}\n"
            f"      platform: {summary.platform}/{summary.platform_scope_id}\n"
            f"      updated: {format_time(summary.updated_at)}\n"
            f"      messages: {summary.message_count}\n"
        )
        if summary.message_preview != "":
            out.append(f"      preview: {summary.message_preview}\n")
    return "".join(out).rstrip("\n")


def session_markers(summary: SessionSummary, current_id: str) -> str:
    """``current`` / ``pinned`` / ``archived`` 标记。对应 Go ``sessionMarkers``。"""
    markers: list[str] = []
    if summary.id == current_id:
        markers.append("current")
    if summary.pinned_at is not None:
        markers.append("pinned")
    if summary.archived_at is not None:
        markers.append("archived")
    if not markers:
        return ""
    return " [" + ", ".join(markers) + "]"


def indent_status_block(text: str) -> str:
    """把一块多行文本整体缩进两格（空行跳过）。对应 Go ``indentStatusBlock``。"""
    out = []
    for line in (text or "").split("\n"):
        if line != "":
            out.append("  " + line + "\n")
    return "".join(out)
