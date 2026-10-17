"""``/remind``、``/poll``、``/vote``、``/signup``、``/join``：群确定性服务。

对应 Go：``internal/agent/commands/group_services.go``（248 行）。

``splitCommandArgs`` 由 :mod:`atri_x.agent.commands.group_knowledge` 提供（Go 里
它就定义在 ``group_knowledge.go``）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result
from atri_x.security import RoleUser
from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.agent.commands.group_knowledge import split_command_args

__all__ = [
    "GroupServicesModule",
    "new_join",
    "new_poll",
    "new_remind",
    "new_signup",
    "new_vote",
    "parse_poll_args",
    "parse_signup_args",
    "poll_usage",
    "remind_usage",
    "result_or_error",
    "signup_usage",
]

_REMIND_HELP = """Usage:
  /remind <时间> <内容>   # 例如：/remind 10m 开会
  /remind list
  /remind remove <id>

时间格式:
  10m / 1h30m / 2d / 2d3h
  15:04 / 15:04:05
  YYYY-MM-DD HH:MM / MM-DD HH:MM"""

_POLL_HELP = """Usage:
  /poll <问题> | <选项1> | <选项2>
  /poll list
  /poll show <id>
  /poll close <id>

示例:
  /poll 周末团建去哪 | 爬山 | 桌游 | 聚餐
  /vote p1 2"""

_SIGNUP_HELP = """Usage:
  /signup <标题> [人数]     # 人数省略或 0 表示不限
  /signup list
  /signup show <id>
  /signup join <id>
  /signup leave <id>
  /signup close <id>"""


async def _maybe_await(value: Any) -> Any:
    """服务实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def remind_usage() -> str:
    """``/remind`` 用法行。对应 Go ``remindUsage``。"""
    return "用法：/remind <时间> <内容>；/remind list；/remind remove <id>"


def poll_usage() -> str:
    """``/poll`` 用法行。对应 Go ``pollUsage``。"""
    return "用法：/poll <问题> | <选项1> | <选项2>；/poll list；/poll show <id>；/poll close <id>"


def signup_usage() -> str:
    """``/signup`` 用法行。对应 Go ``signupUsage``。"""
    return (
        "用法：/signup <标题> [人数]；/signup list；/signup show <id>；"
        "/signup join <id>；/signup leave <id>；/signup close <id>"
    )


def parse_poll_args(args: str) -> tuple[str, list[str]]:
    """按 ``|``（含全角）拆出问题与选项。对应 Go ``parsePollArgs``。"""
    parts: list[str] = []
    current: list[str] = []
    for char in args or "":
        if char in ("|", "\uff5c"):
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    if len(parts) < 2:
        return "", []
    clean = [part.strip() for part in parts if part.strip() != ""]
    if len(clean) < 2:
        return "", []
    return clean[0], clean[1:]


def parse_signup_args(args: str) -> tuple[str, int]:
    """拆出标题与人数。对应 Go ``parseSignupArgs``。"""
    fields = (args or "").split()
    if len(fields) >= 2:
        try:
            capacity = int(fields[-1])
        except ValueError:
            capacity = None
        if capacity is not None:
            return " ".join(fields[:-1]), capacity
    return " ".join(fields), 0


class _GroupServicesCommand:
    """``/remind`` / ``/poll`` / ``/vote`` / ``/signup`` / ``/join`` 的公共壳。

    每个子命令的实现都收敛到一个 ``(ctx, req)`` 协程，构造时注入即可。
    """

    def __init__(self, deps: Deps, info: Info, handler: Any) -> None:
        self._deps = deps
        self._info = info
        self._handler = handler

    def info(self) -> Info:
        return self._info

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        return await self._handler(ctx, req)


def _require_services(deps: Deps) -> Any:
    if deps.group_services is None:
        raise ValueError("group services are not configured")
    return deps.group_services


def new_remind(deps: Deps) -> Any:
    """``/remind``。对应 Go ``NewRemind``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        services = _require_services(deps)
        args = (req.args or "").strip()
        if args == "" or args.lower() == "list":
            return Result(content=await _maybe_await(services.reminder_list(ctx)))
        name, rest = split_command_args(args)
        key = name.lower()
        if key in ("remove", "delete", "rm"):
            return Result(content=await _maybe_await(services.reminder_remove(ctx, rest)))
        if key in ("help", "?"):
            return Result(content=remind_usage())
        if rest == "":
            raise ValueError("用法：/remind <时间> <内容>")
        return Result(content=await _maybe_await(services.reminder_create(ctx, name, rest)))

    return _GroupServicesCommand(
        deps,
        Info(
            name="remind",
            aliases=["reminder", "timer", "reminders"],
            usage="/remind <时间> <内容>",
            description="创建群提醒；支持相对时间、时刻和日期时间。",
            min_role=RoleUser,
            help=_REMIND_HELP,
        ),
        handle,
    )


def new_poll(deps: Deps) -> Any:
    """``/poll``。对应 Go ``NewPoll``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        services = _require_services(deps)
        args = (req.args or "").strip()
        if args == "" or args.lower() == "list":
            return Result(content=await _maybe_await(services.poll_list(ctx)))
        name, rest = split_command_args(args)
        key = name.lower()
        if key in ("show", "results", "result"):
            if rest.strip() == "":
                raise ValueError("请指定投票 id")
            return Result(content=await _maybe_await(services.poll_show(ctx, rest)))
        if key in ("close", "end"):
            if rest.strip() == "":
                raise ValueError("请指定投票 id")
            return Result(content=await _maybe_await(services.poll_close(ctx, rest)))
        if key in ("help", "?"):
            return Result(content=poll_usage())
        question, options = parse_poll_args(args)
        if question == "" or len(options) < 2:
            raise ValueError("用法：/poll <问题> | <选项1> | <选项2>")
        return Result(content=await _maybe_await(services.poll_create(ctx, question, options)))

    return _GroupServicesCommand(
        deps,
        Info(
            name="poll",
            aliases=["polls"],
            usage="/poll <问题> | <选项1> | <选项2>",
            description="创建群投票；用 /vote <id> <序号> 投票。",
            min_role=RoleUser,
            help=_POLL_HELP,
        ),
        handle,
    )


def new_vote(deps: Deps) -> Any:
    """``/vote``。对应 Go ``NewVote``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        services = _require_services(deps)
        id_, option = split_command_args((req.args or "").strip())
        if id_ == "" or option == "":
            raise ValueError("用法：/vote <投票id> <选项序号>")
        return Result(content=await _maybe_await(services.poll_vote(ctx, id_, option)))

    return _GroupServicesCommand(
        deps,
        Info(
            name="vote",
            aliases=["ballot"],
            usage="/vote <投票id> <选项序号>",
            description="为当前群的投票投票；可改票。",
            min_role=RoleUser,
        ),
        handle,
    )


def new_signup(deps: Deps) -> Any:
    """``/signup``。对应 Go ``NewSignup``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        services = _require_services(deps)
        args = (req.args or "").strip()
        if args == "" or args.lower() == "list":
            return Result(content=await _maybe_await(services.signup_list(ctx)))
        name, rest = split_command_args(args)
        key = name.lower()
        if key == "show":
            if rest.strip() == "":
                raise ValueError("请指定报名 id")
            return Result(content=await _maybe_await(services.signup_show(ctx, rest)))
        if key == "join":
            if rest.strip() == "":
                raise ValueError("请指定报名 id")
            return Result(content=await _maybe_await(services.signup_join(ctx, rest)))
        if key in ("leave", "quit"):
            if rest.strip() == "":
                raise ValueError("请指定报名 id")
            return Result(content=await _maybe_await(services.signup_leave(ctx, rest)))
        if key in ("close", "end"):
            if rest.strip() == "":
                raise ValueError("请指定报名 id")
            return Result(content=await _maybe_await(services.signup_close(ctx, rest)))
        if key in ("help", "?"):
            return Result(content=signup_usage())
        title, capacity = parse_signup_args(args)
        if title == "":
            raise ValueError("用法：/signup <标题> [人数]")
        return Result(content=await _maybe_await(services.signup_create(ctx, title, capacity)))

    return _GroupServicesCommand(
        deps,
        Info(
            name="signup",
            aliases=["sign", "signups"],
            usage="/signup <标题> [人数]",
            description="创建群报名；用 /join <id> 加入。",
            min_role=RoleUser,
            help=_SIGNUP_HELP,
        ),
        handle,
    )


def new_join(deps: Deps) -> Any:
    """``/join``。对应 Go ``NewJoin``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        services = _require_services(deps)
        id_ = (req.args or "").strip()
        if id_ == "":
            raise ValueError("用法：/join <报名id>")
        return Result(content=await _maybe_await(services.signup_join(ctx, id_)))

    return _GroupServicesCommand(
        deps,
        Info(
            name="join",
            usage="/join <报名id>",
            description="加入当前群的报名。",
            min_role=RoleUser,
        ),
        handle,
    )


def result_or_error(content: str, error: Exception | None) -> Result:
    """Go ``resultOrError`` 的 Python 形态：有错抛错，否则包成 ``Result``。

    本仓的服务调用直接抛异常，所以这个 helper 只接受"已经拿到的文本"，
    保留它是为了在源码里能检索到 Go 的同名函数。
    """
    if error is not None:
        raise error
    return Result(content=content)


class GroupServicesModule:
    """对应 Go ``GroupServicesModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(
            registrar, deps, new_remind, new_poll, new_vote, new_signup, new_join
        )
