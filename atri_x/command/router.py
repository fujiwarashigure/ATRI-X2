"""命令路由：注册、解析、派发、按 actor 过滤与整行补全。

对应 Go：``internal/command/router.go``。

``Dispatch`` 会等待 handler（Go 侧返回 error），因此是异步方法；解析/补全这类纯
字符串处理保持同步（对应 Go 里不返回 error 的那批函数）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Sequence

from atri_x.errors import InvalidError
from atri_x.command.types import (
    Actor,
    Handler,
    Info,
    Request,
    Result,
    can_access,
)

# default_command_prefix 是未显式配置命令前缀时的默认值。
DEFAULT_COMMAND_PREFIX = "/"


@dataclass
class Parsed:
    """一条命令文本的解析结果。对应 Go ``Parsed``。"""

    ok: bool = False
    prefix: str = ""
    name: str = ""
    args: str = ""


def primary_prefix(prefixes: Sequence[str] | None) -> str:
    """返回配置里第一个非空命令前缀，用于提示文案和平台按钮。

    对应 Go ``PrimaryPrefix``。
    """
    for prefix in prefixes or []:
        prefix = (prefix or "").strip()
        if prefix:
            return prefix
    return DEFAULT_COMMAND_PREFIX


def normalize_prefixes(prefixes: Sequence[str] | None) -> list[str]:
    """对应 Go ``normalizePrefixes``：去空、去重、按长度降序稳定排序。"""
    if not prefixes:
        prefixes = [DEFAULT_COMMAND_PREFIX]
    seen: set[str] = set()
    out: list[str] = []
    for prefix in prefixes:
        prefix = (prefix or "").strip()
        if prefix == "" or prefix in seen:
            continue
        seen.add(prefix)
        out.append(prefix)
    if not out:
        out = [DEFAULT_COMMAND_PREFIX]
    out.sort(key=len, reverse=True)
    return out


def normalize_name(name: str) -> str:
    """对应 Go ``normalizeName``：先去一个前导 ``/``，再 trim，最后小写。"""
    return (name or "").removeprefix("/").strip().lower()


def _cut_space(text: str) -> tuple[str, str, bool]:
    """对应 Go ``strings.Cut(text, " ")``：只在第一个空格处切。"""
    head, sep, tail = text.partition(" ")
    return head, tail, sep != ""


class Router:
    """命令路由器。对应 Go ``Router``。"""

    def __init__(self, prefixes: Sequence[str] | None = None) -> None:
        self._default_prefix = primary_prefix(prefixes)
        self._prefixes = normalize_prefixes(prefixes)
        self._handlers: dict[str, Handler] = {}
        self._primary: dict[str, bool] = {}
        self._order: list[str] = []

    # -- 注册 ---------------------------------------------------------------
    def register(self, handler: Handler | None) -> None:
        """对应 Go ``Router.Register``。名字或别名冲突时抛 :class:`InvalidError`。"""
        if handler is None:
            raise InvalidError("command handler is nil")
        info = handler.info()
        name = normalize_name(info.name)
        if name == "":
            raise InvalidError("command name is empty")
        if name in self._handlers:
            raise InvalidError(f"command {name!r} already registered")

        names = [name]
        for alias in info.aliases:
            alias = normalize_name(alias)
            if alias:
                names.append(alias)
        seen: set[str] = set()
        for candidate in names:
            if candidate in seen:
                raise InvalidError(f"command {name!r} has duplicate alias {candidate!r}")
            seen.add(candidate)
            if candidate in self._handlers:
                raise InvalidError(f"command alias {candidate!r} already registered")

        for candidate in names:
            self._handlers[candidate] = handler
        self._primary[name] = True
        self._order.append(name)

    def register_handlers(self, *handlers: Handler) -> None:
        """对应 Go ``RegisterHandlers``：逐个注册，遇到错误立即抛出。"""
        for handler in handlers:
            self.register(handler)

    # -- 派发 ---------------------------------------------------------------
    async def dispatch(self, ctx: Any, raw: str) -> Result | None:
        """对应 Go ``Router.Dispatch``。不是命令时抛 :class:`InvalidError`。"""
        parsed = self.parse(raw)
        if not parsed.ok:
            raise InvalidError("not a command")
        handler = self._handlers.get(parsed.name)
        if handler is None:
            return Result(
                content=(
                    f"unknown command: {parsed.prefix}{parsed.name}\n"
                    f"type {parsed.prefix}help for available commands"
                )
            )
        return await handler.handle(
            ctx,
            Request(raw=raw, prefix=parsed.prefix, name=parsed.name, args=parsed.args),
        )

    def is_command(self, text: str) -> bool:
        """对应 Go ``Router.IsCommand``。"""
        return self.parse(text).ok

    def parse(self, text: str) -> Parsed:
        """对应 Go ``Router.Parse``：按最长前缀优先解析。"""
        text = (text or "").strip()
        if text == "":
            return Parsed()
        for prefix in self._prefixes:
            if not text.startswith(prefix):
                continue
            rest = text[len(prefix):].strip()
            if rest == "":
                return Parsed(ok=True, prefix=prefix)
            name, args, _ = _cut_space(rest)
            return Parsed(
                ok=True, prefix=prefix, name=normalize_name(name), args=args.strip()
            )
        return Parsed()

    # -- 查询 ---------------------------------------------------------------
    def command_info(self, name: str) -> tuple[Info | None, bool]:
        """对应 Go ``Router.CommandInfo``（主名与别名都可查）。

        Go 拿到的是一份 ``Info`` 值拷贝，改名字不会影响 handler；Python 用
        ``dataclasses.replace`` 复刻这一点。
        """
        handler = self._handlers.get(normalize_name(name))
        if handler is None:
            return None, False
        return _normalized_info(handler), True

    def command_info_for_actor(self, name: str, actor: Actor) -> tuple[Info | None, bool]:
        """对应 Go ``Router.CommandInfoForActor``。"""
        info, ok = self.command_info(name)
        if not ok or info is None or not can_access(info, actor):
            return None, False
        return info, True

    def handler(self, name: str) -> tuple[Handler | None, bool]:
        """对应 Go ``Router.Handler``。"""
        handler = self._handlers.get(normalize_name(name))
        return handler, handler is not None

    def commands(self) -> list[Info]:
        """对应 Go ``Router.Commands``：按注册顺序返回主命令的 Info。"""
        infos: list[Info] = []
        for name in self._order:
            handler = self._handlers.get(name)
            if handler is None:
                continue
            infos.append(_normalized_info(handler))
        return infos

    def commands_for_actor(self, actor: Actor) -> list[Info]:
        """对应 Go ``Router.CommandsForActor``。"""
        return [info for info in self.commands() if can_access(info, actor)]

    def primary_prefix(self) -> str:
        """返回该 Router 的主命令前缀。对应 Go ``Router.PrimaryPrefix``。"""
        if not self._default_prefix:
            return DEFAULT_COMMAND_PREFIX
        return self._default_prefix

    def prefixes(self) -> list[str]:
        """对应 Go ``Router.Prefixes``。"""
        return list(self._prefixes)

    # -- 补全 ---------------------------------------------------------------
    def complete(self, text: str) -> list[str]:
        """返回当前输入的整行命令补全。对应 Go ``Router.Complete``。"""
        return self._complete(text, None)

    def complete_for_actor(self, text: str, actor: Actor) -> list[str]:
        """对应 Go ``Router.CompleteForActor``。"""
        return self._complete(text, actor)

    def _complete(self, text: str, actor: Actor | None) -> list[str]:
        text = text or ""
        for prefix in self._prefixes:
            if not text.startswith(prefix):
                continue
            rest = text[len(prefix):]
            if " " in rest or "\t" in rest:
                return []
            query = normalize_name(rest)
            out: list[str] = []
            for name in self._completion_names(actor):
                if name.startswith(query):
                    out.append(prefix + name)
            return out
        return []

    def _completion_names(self, actor: Actor | None) -> list[str]:
        """对应 Go ``Router.completionNames``：主名与别名按注册顺序、去重。"""
        seen: set[str] = set()
        out: list[str] = []
        for primary_name in self._order:
            handler = self._handlers.get(primary_name)
            if handler is None:
                continue
            info = handler.info()
            if actor is not None and not can_access(info, actor):
                continue
            for name in [info.name, *info.aliases]:
                name = normalize_name(name)
                if name == "" or name in seen:
                    continue
                seen.add(name)
                out.append(name)
        return out


def new_router(prefixes: Sequence[str] | None = None) -> Router:
    """对应 Go ``NewRouter``。"""
    return Router(prefixes)


def _normalized_info(handler: Handler) -> Info:
    """取一份名字已归一化的 ``Info`` 拷贝（不动 handler 自己的那份）。"""
    info = handler.info()
    normalized = normalize_name(info.name)
    if normalized == info.name:
        return dataclasses.replace(info)
    return dataclasses.replace(info, name=normalized)


__all__ = [name for name in dir() if not name.startswith("_")]
