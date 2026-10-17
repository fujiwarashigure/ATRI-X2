"""``/requests``、``/stop``、``/stopall`` 与请求树渲染。

对应 Go：``internal/agent/commands/request.go``（296 行）。

Go 的 ``writeRequestLine`` 用 ``time.Since`` 与 ``deps.Store.Sessions().Get``；
Python 侧存储层是同步的（``get(id)`` 不接 ``ctx``），时间统一用 aware UTC
``datetime``，时长用秒（float），渲染时再转成 Go ``Duration.String()`` 的等价文本。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.command import Info, Request, Result, new_func
from atri_x.errors import NotFoundError
from atri_x.request import KindHook, KindTool, KindTurn
from atri_x.runtime import PhaseDone, PhaseIdle, PhaseTool
from atri_x.security import RoleSuperadmin, RoleUser, actor_from_context
from atri_x.turn import tools_string
from atri_x.agent.commands.completion import (
    complete_request_ids,
    current_completion_token,
    is_first_arg,
)
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = [
    "RequestInfo",
    "RequestModule",
    "RequestNode",
    "RequestTree",
    "build_request_tree",
    "format_active_requests",
    "format_duration",
    "format_request_tree",
    "format_requests",
    "new_requests",
    "new_stop",
    "new_stop_all",
    "plural",
    "request_less",
    "resolve_request_arg",
    "stoppable_requests",
    "write_request_line",
]


@dataclass
class RequestInfo:
    """一条请求加上它的会话上下文。对应 Go ``RequestInfo``。"""

    request: Any = None
    session_title: str = ""
    session_mode: str = ""
    tools: dict[str, int] = field(default_factory=dict)


@dataclass
class RequestNode:
    """请求树的一个根节点。对应 Go ``requestNode``。"""

    request: Any = None
    children: list[Any] = field(default_factory=list)


@dataclass
class RequestTree:
    """请求树与"编号 → 请求"索引。对应 Go ``requestTree``。"""

    roots: list[RequestNode] = field(default_factory=list)
    by_number: dict[str, Any] = field(default_factory=dict)


def plural(count: int) -> str:
    """英文复数后缀。对应 Go ``plural``。"""
    if count == 1:
        return ""
    return "s"


def format_duration(seconds: float) -> str:
    """把秒数渲染成 Go ``time.Duration.String()`` 的等价文本。

    对应 Go ``formatDuration``：不足 1 秒显示 ``0s``，否则四舍五入到整秒。
    """
    if seconds is None or seconds < 1.0:
        return "0s"
    total = int(math.floor(seconds + 0.5))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60}s"
    return f"{total // 3600}h{(total % 3600) // 60}m{total % 60}s"


def _since(started_at: datetime | None) -> float:
    if started_at is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (now - started_at).total_seconds()


def new_requests(deps: Deps) -> Any:
    """``/requests``：列出全部活动请求。对应 Go ``NewRequests``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        requests = deps.requests.list()
        if not requests:
            return Result(content="no active requests")
        return Result(content=format_requests(ctx, deps, requests))

    return new_func(
        Info(
            name="requests",
            usage="/requests",
            description="List active requests.",
        ),
        handle,
    )


class _StopCommand:
    """对应 Go ``stopCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="stop",
            usage="/stop [request_id|number]",
            description=(
                "Stop a request or all requests in current session. Numbers like "
                "1 or 1.1 come from the requests command output."
            ),
            min_role=RoleUser,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        deps = self._deps
        arg = (req.args or "").strip()
        if arg != "":
            request_id, ok = resolve_request_arg(
                stoppable_requests(ctx, deps), arg
            )
            if not ok:
                return Result(content=f"request not found: {arg}")
            stopped, _ = deps.requests.get(request_id)
            if stopped is not None and stopped.kind == KindTurn:
                count = deps.requests.cancel_session(stopped.session_id)
                if deps.cancel_session_inbox is not None:
                    count += deps.cancel_session_inbox(stopped.session_id)
                deps.turns.stop_session(stopped.session_id)
                return Result(content=f"stopped {count} request{plural(count)}")
            if not deps.requests.cancel(request_id):
                return Result(content=f"request not found: {arg}")
            return Result(content="stopped 1 request")

        current = deps.sessions.current(deps.scope(ctx))
        count = deps.requests.cancel_session(current.id)
        if deps.cancel_session_inbox is not None:
            count += deps.cancel_session_inbox(current.id)
        deps.turns.stop_session(current.id)
        return Result(content=f"stopped {count} request{plural(count)}")

    async def complete(self, ctx: Any, req: Any) -> list[Any]:
        token = current_completion_token(req)
        if not is_first_arg(req, token):
            return []
        return complete_request_ids(
            stoppable_requests(ctx, self._deps), token.text, token.start, token.end
        )


def new_stop(deps: Deps) -> Any:
    """对应 Go ``NewStop``。"""
    return _StopCommand(deps)


def new_stop_all(deps: Deps) -> Any:
    """``/stopall``。对应 Go ``NewStopAll``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        count = deps.requests.cancel_all()
        deps.turns.stop_all()
        return Result(content=f"stopped {count} request{plural(count)}")

    return new_func(
        Info(
            name="stopall",
            usage="/stopall",
            description="Stop all active requests in this process.",
        ),
        handle,
    )


def stoppable_requests(ctx: Any, deps: Deps) -> list[Any]:
    """当前 actor 有权停止的请求集合。对应 Go ``stoppableRequests``。

    超管保留进程级视图；其他人只看当前 Session，避免普通用户取消别人的请求。
    """
    if deps.requests is None:
        return []
    actor, ok = actor_from_context(ctx)
    if ok and actor.role == RoleSuperadmin:
        return deps.requests.list()
    if deps.sessions is None or deps.scope is None:
        return []
    try:
        current = deps.sessions.current(deps.scope(ctx))
    except Exception:  # noqa: BLE001 - Go 里 err != nil 返回 nil
        return []
    if current is None:
        return []
    return deps.requests.list_by_session(current.id)


def format_requests(ctx: Any, deps: Deps, requests: list[Any]) -> str:
    """对应 Go ``formatRequests``。"""
    return format_request_tree(ctx, deps, requests, False)


def format_request_tree(
    ctx: Any, deps: Deps, requests: list[Any], show_none: bool
) -> str:
    """渲染请求树。对应 Go ``formatRequestTree``。"""
    if not requests and show_none:
        return "active requests: none"
    tree = build_request_tree(requests)
    out = ["active requests:"]
    if requests:
        out.append("\n")
    for index, root in enumerate(tree.roots):
        number = str(index + 1)
        out.append(write_request_line(ctx, deps, number, "", root.request))
        for child_index, child in enumerate(root.children):
            child_number = f"{number}.{child_index + 1}"
            out.append(write_request_line(ctx, deps, child_number, "└── ", child))
    return "".join(out).rstrip("\n")


def write_request_line(
    ctx: Any, deps: Deps, number: str, prefix: str, req: Any
) -> str:
    """渲染请求树的一行（含根节点的会话/阶段/模式/工具块）。对应 Go ``writeRequestLine``。"""
    kind = str(req.kind)
    label = (req.label or "").strip()
    if req.kind == KindTurn:
        if label == "":
            label = "chat"
        kind = "turn"
    elif label == "":
        label = "request"
    out = [
        f"  {prefix}[{number}] {label} {kind} request "
        f"{format_duration(_since(req.started_at))}\n"
    ]
    if prefix != "":
        if req.kind == KindTool:
            out.append(f"      tool: {label}\n")
        if req.kind == KindHook:
            out.append(f"      hook: {label}\n")
        return "".join(out)

    session = None
    if deps.store is not None:
        try:
            session = deps.store.sessions().get(req.session_id)
        except NotFoundError:
            session = None
        except Exception:  # noqa: BLE001 - Go 里 err 被丢弃
            session = None
    title = "(unknown)"
    mode = "(unknown)"
    if session is not None:
        if session.title:
            title = session.title
        mode = session.mode
    out.append(f"      session: {title}\n")
    if deps.runtime_status is not None:
        snapshot = deps.runtime_status(req.session_id)
        if (
            snapshot is not None
            and snapshot.phase
            and snapshot.phase not in (PhaseIdle, PhaseDone)
        ):
            phase_text = str(snapshot.phase)
            if snapshot.phase == PhaseTool and snapshot.tool_name:
                phase_text += " " + snapshot.tool_name
            out.append(
                f"      phase: {phase_text} "
                f"({format_duration(snapshot.stage_elapsed(datetime.now(timezone.utc)))})\n"
            )
    out.append(f"      mode: {mode}\n")
    tools = tools_string(deps.turns.snapshot(req.session_id).tools)
    if tools != "":
        out.append(f"      tools: {tools}\n")
    return "".join(out)


def resolve_request_arg(requests: list[Any], arg: str) -> tuple[str, bool]:
    """先按 request id、再按 ``/requests`` 树编号解析参数。对应 Go ``resolveRequestArg``。"""
    for req in requests:
        if req.id == arg:
            return req.id, True
    tree = build_request_tree(requests)
    req = tree.by_number.get(arg)
    if req is None:
        return "", False
    return req.id, True


def request_less(left: Any, right: Any) -> bool:
    """请求排序：先 started_at，再 id。对应 Go ``requestLess``。"""
    left_at = left.started_at
    right_at = right.started_at
    if left_at != right_at:
        if left_at is None:
            return True
        if right_at is None:
            return False
        return left_at < right_at
    return left.id < right.id


def build_request_tree(requests: list[Any]) -> RequestTree:
    """按 parent_id 组装请求树并编上 ``1`` / ``1.1`` 这类编号。对应 Go ``buildRequestTree``。"""
    by_id: dict[str, Any] = {}
    children: dict[str, list[Any]] = {}
    for req in requests:
        by_id[req.id] = req
        if req.parent_id != "":
            children.setdefault(req.parent_id, []).append(req)
    roots: list[RequestNode] = []
    for req in requests:
        if req.parent_id != "" and req.parent_id in by_id:
            continue
        roots.append(RequestNode(request=req))
    roots.sort(key=lambda node: _sort_key(node.request))
    by_number: dict[str, Any] = {}
    for index, root in enumerate(roots):
        root.children = list(children.get(root.request.id, []))
        root.children.sort(key=_sort_key)
        number = str(index + 1)
        by_number[number] = root.request
        for child_index, child in enumerate(root.children):
            by_number[f"{number}.{child_index + 1}"] = child
    return RequestTree(roots=roots, by_number=by_number)


def _sort_key(req: Any) -> tuple[int, datetime, str]:
    started = req.started_at
    if started is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc), req.id)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (1, started, req.id)


def format_active_requests(ctx: Any, deps: Deps, requests: list[Any]) -> str:
    """对应 Go ``formatActiveRequests``（``showNone`` 为真）。"""
    return format_request_tree(ctx, deps, requests, True)


class RequestModule:
    """对应 Go ``RequestModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_requests, new_stop, new_stop_all)
