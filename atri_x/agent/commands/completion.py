"""命令参数补全的公共助手。

对应 Go：``internal/agent/commands/completion_helpers.go``。

``currentCompletionToken`` 在 Go 里按 **byte** 下标切片；Python 的字符串下标是
码点，两者在纯 ASCII 命令文本上等价，非 ASCII 参数会得到"码点下标"。补全候选取
的 ``replace_start`` / ``replace_end`` 会被前端按同一套下标消费，所以这里保持
码点语义并在文档里写明（Go 侧一个中文字符占 3 个 byte，前端拿到的是 byte 下标；
Python 前端按码点，行为更正确）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.command import Completion, CompletionRequest
from atri_x.agent.commands.deps import Deps

__all__ = [
    "CompletionOption",
    "CompletionToken",
    "complete_confirm_flag",
    "complete_request_ids",
    "complete_session_ids",
    "complete_static_options",
    "complete_string_options",
    "complete_tool_names",
    "current_completion_token",
    "first_non_empty",
    "is_first_arg",
]


@dataclass
class CompletionToken:
    """光标所在的参数片段。对应 Go ``completionToken``。"""

    start: int = 0
    end: int = 0
    text: str = ""


@dataclass
class CompletionOption:
    """一个静态候选项。对应 Go ``completionOption``。"""

    text: str = ""
    label: str = ""
    description: str = ""


def current_completion_token(req: CompletionRequest) -> CompletionToken:
    """定位光标所在的参数片段。对应 Go ``currentCompletionToken``。"""
    cursor = req.cursor
    if cursor <= 0 or cursor > len(req.raw):
        cursor = len(req.raw)
    start = cursor
    while start > 0 and req.raw[start - 1] not in (" ", "\t"):
        start -= 1
    return CompletionToken(start=start, end=cursor, text=req.raw[start:cursor])


def complete_static_options(
    options: list[CompletionOption], query: str, start: int, end: int, kind: str
) -> list[Completion]:
    """按前缀过滤静态候选项。对应 Go ``completeStaticOptions``。"""
    out: list[Completion] = []
    for option in options:
        if query != "" and not option.text.startswith(query):
            continue
        out.append(
            Completion(
                text=option.text,
                label=first_non_empty(option.label, option.text),
                description=option.description,
                kind=kind,
                replace_start=start,
                replace_end=end,
            )
        )
    return out


def complete_string_options(
    values: list[str], query: str, start: int, end: int, kind: str
) -> list[Completion]:
    """按前缀过滤字符串候选项。对应 Go ``completeStringOptions``。"""
    out: list[Completion] = []
    for value in values:
        if value == "" or (query != "" and not value.startswith(query)):
            continue
        out.append(
            Completion(
                text=value,
                label=value,
                kind=kind,
                replace_start=start,
                replace_end=end,
            )
        )
    return out


def complete_session_ids(
    ctx: Any, deps: Deps, query: str, archived: bool, start: int, end: int
) -> list[Completion]:
    """补全当前 scope 可见的 Session id。对应 Go ``completeSessionIDs``。"""
    from atri_x.agent.commands.session_format import (
        empty_title,
        list_session_page,
        session_page_size,
    )

    if deps.sessions is None or deps.scope is None:
        return []
    try:
        sessions, _ = list_session_page(
            ctx, deps, "", 1, session_page_size(deps), archived
        )
    except Exception:  # noqa: BLE001 - Go 里 err != nil 直接返回 nil
        return []
    out: list[Completion] = []
    for session in sessions:
        if query != "" and not session.id.startswith(query):
            continue
        out.append(
            Completion(
                text=session.id,
                label=session.id,
                description=empty_title(session.title),
                kind="session_id",
                replace_start=start,
                replace_end=end,
            )
        )
    return out


def complete_request_ids(
    requests: list[Any], query: str, start: int, end: int
) -> list[Completion]:
    """补全活动 request id。对应 Go ``completeRequestIDs``。"""
    out: list[Completion] = []
    for req in requests:
        if query != "" and not req.id.startswith(query):
            continue
        description = (str(getattr(req, "kind", "")) + " " + req.label).strip()
        out.append(
            Completion(
                text=req.id,
                label=req.id,
                description=description,
                kind="request_id",
                replace_start=start,
                replace_end=end,
            )
        )
    return out


def complete_tool_names(
    deps: Deps, query: str, start: int, end: int
) -> list[Completion]:
    """补全已注册工具名。对应 Go ``completeToolNames``。"""
    if deps.tools is None:
        return []
    out: list[Completion] = []
    for info in deps.tools.list():
        if query != "" and not info.name.startswith(query):
            continue
        out.append(
            Completion(
                text=info.name,
                label=info.name,
                description=info.description,
                kind="tool_name",
                replace_start=start,
                replace_end=end,
            )
        )
    return out


def complete_confirm_flag(args: str, token: CompletionToken) -> list[Completion]:
    """在还没写 ``--confirm`` 时补出这个选项。对应 Go ``completeConfirmFlag``。"""
    if " --confirm " in " " + args + " ":
        return []
    return complete_static_options(
        [CompletionOption(text="--confirm", description="Confirm this destructive action")],
        token.text,
        token.start,
        token.end,
        "option",
    )


def is_first_arg(req: CompletionRequest, token: CompletionToken) -> bool:
    """当前补全的是不是命令名之后的第一个参数。对应 Go ``isFirstArg``。"""
    args_start = len(req.prefix) + len(req.name)
    if token.start < args_start:
        return False
    before = req.raw[args_start : token.start].strip()
    return before == ""


def first_non_empty(*values: str) -> str:
    """返回第一个非空白值。对应 Go ``firstNonEmpty``。"""
    for value in values:
        if (value or "").strip() != "":
            return value
    return ""
